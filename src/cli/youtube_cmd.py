"""
src/cli/youtube_cmd.py
Subcommand handler: manga-recap youtube (metadata, packaging & upload)
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from src.cli.exit_codes import CliValidationError
from src.core.schemas import YouTubeReceipt
from src.core.logger import logger
from src.core.schemas import FeatureTimeline, YouTubeMetadata, utc_iso_now
from src.core.series_setup import _normalize_bool, require_series_setup
from src.core.storage import (
    StorageFileNotFoundError,
    atomic_write_json,
    calculate_sha256,
    format_chapter_filename,
    get_chapter_dir,
    get_output_dir,
    get_project_dir,
    read_json,
)
from src.core.utils import DependencyNotFoundError
from src.youtube.account_manager import YOUTUBE_SCOPES, YouTubeAccountManager, account_manager


from src.youtube.metadata import (
    build_youtube_chapter_markers, build_youtube_description, format_seconds_to_timestamp,
    generate_timestamps_from_script, handle_youtube_metadata,
)


def get_youtube_oauth_credentials(account_name: Optional[str] = None, force_reauth: bool = False, *, allow_interactive: bool = False):
    """
    Retrieves and refreshes YouTube OAuth2 credentials for the specified or default account.
    Maintains 100% backward compatibility with legacy 0-argument callers.
    """
    creds, _ = account_manager.get_credentials(account_name=account_name, force_reauth=force_reauth,
                                               allow_interactive=allow_interactive)
    return creds


def handle_youtube_accounts(args: argparse.Namespace) -> Dict[str, Any]:
    """Manages multi-channel YouTube accounts (list, add, remove, default)."""
    action = getattr(args, "account_action", None) or "list"
    account_name = getattr(args, "account", None)

    if action == "list":
        accounts = account_manager.list_accounts()
        reg = account_manager.load_registry()
        return {
            "status": "success",
            "command": "youtube",
            "action": "accounts",
            "subaction": "list",
            "default_account": reg.default_account,
            "accounts_count": len(accounts),
            "accounts": accounts,
        }

    if action == "add":
        if not account_name:
            raise CliValidationError("--account is required to add an account (e.g. --account review_truyen).")
        secrets = getattr(args, "secrets", None)
        set_default = getattr(args, "set_default", False)
        acc = account_manager.add_or_update_account(
            account_name=account_name,
            client_secrets_file=secrets,
            set_default=set_default,
            allow_interactive=not (getattr(args, "json", False) or getattr(args, "json_mode", False)),
        )
        reg = account_manager.load_registry()
        return {
            "status": "success",
            "command": "youtube",
            "action": "accounts",
            "subaction": "add",
            "account": {
                "account_name": acc.account_name,
                "channel_title": acc.channel_title,
                "channel_id": acc.channel_id,
                "token_file": acc.token_file,
                "is_default": (acc.account_name == reg.default_account),
            },
        }

    if action == "remove":
        if not account_name:
            raise CliValidationError("--account is required to remove an account.")
        delete_token = getattr(args, "delete_token", True)
        reg = account_manager.remove_account(account_name, delete_token=delete_token)
        return {
            "status": "success",
            "command": "youtube",
            "action": "accounts",
            "subaction": "remove",
            "removed_account": account_name,
            "default_account": reg.default_account,
            "remaining_accounts_count": len(reg.accounts),
        }

    if action == "default":
        if not account_name:
            raise CliValidationError("--account is required to set default account.")
        reg = account_manager.set_default_account(account_name)
        return {
            "status": "success",
            "command": "youtube",
            "action": "accounts",
            "subaction": "default",
            "default_account": reg.default_account,
        }

    raise CliValidationError(f"Unknown account action '{action}'. Available: list, add, remove, default.")


def handle_youtube_auth(args: argparse.Namespace) -> Dict[str, Any]:
    """Tests and verifies YouTube OAuth credentials for specified or default account."""
    account_name = getattr(args, "account", None)
    force = getattr(args, "force", False)

    creds = get_youtube_oauth_credentials(account_name=account_name, force_reauth=force)

    legacy_token_env = os.getenv("YOUTUBE_CREDENTIALS_FILE")
    if legacy_token_env and not account_name:
        token_file = str(Path(legacy_token_env).resolve())
        acc_name = "default"
        c_title, c_id = "", ""
    else:
        try:
            acc_info = account_manager.get_account(account_name)
            token_file = str(Path(acc_info.token_file).resolve())
            acc_name = acc_info.account_name
            c_title, c_id = acc_info.channel_title, acc_info.channel_id
            if not c_title and creds and getattr(creds, "valid", False):
                c_title, c_id = account_manager.fetch_channel_info(creds)
                if c_title:
                    reg = account_manager.load_registry()
                    if acc_info.account_name in reg.accounts:
                        reg.accounts[acc_info.account_name].channel_title = c_title
                        reg.accounts[acc_info.account_name].channel_id = c_id
                        account_manager.save_registry(reg)
        except Exception:
            token_file = str(Path(legacy_token_env or ".youtube_token.json").resolve())
            acc_name = account_name or "default"
            c_title, c_id = "", ""

    return {
        "status": "success",
        "valid": bool(creds and getattr(creds, "valid", False)),
        "account": acc_name,
        "channel_title": c_title,
        "channel_id": c_id,
        "token_file": str(Path(token_file).resolve()),
    }


def prompt_interactive_channel_selection(manager: Optional[YouTubeAccountManager] = None) -> str:
    """
    Displays an interactive numbered terminal prompt to select a YouTube channel.
    Supports selecting 1, 2, 3... or pressing Enter to select the default account.
    """
    mgr = manager or account_manager
    accounts = mgr.list_accounts()
    if not accounts:
        raise CliValidationError(
            "No YouTube accounts registered.\n"
            "Please run 'manga-recap youtube accounts --account-action add --account <name>' first."
        )

    reg = mgr.load_registry()
    print("\n" + "=" * 64)
    print("  🎬 CHỌN KÊNH YOUTUBE ĐỂ TẢI VIDEO LÊN (INTERACTIVE MODE)")
    print("=" * 64)
    for idx, acc in enumerate(accounts, start=1):
        def_tag = " ⭐ [MẶC ĐỊNH]" if acc.get("is_default") else ""
        c_title = acc.get("channel_title") or "(Chưa đặt tên)"
        a_name = acc.get("account_name")
        c_id = acc.get("channel_id") or "(Chưa có ID)"
        print(f"  [{idx}] {c_title} (alias: '{a_name}') (ID: {c_id}){def_tag}")
    print("  [0] Hủy bỏ tải lên")
    print("=" * 64)

    default_alias = reg.default_account or accounts[0]["account_name"]
    while True:
        try:
            choice = input(f"👉 Chọn kênh (1-{len(accounts)}) hoặc [0] để hủy [mặc định: {default_alias}]: ").strip()
            if not choice:
                print(f"👉 Đã chọn mặc định: '{default_alias}'\n")
                return default_alias
            if choice == "0":
                raise CliValidationError("Người dùng đã hủy tiến trình upload.")
            if choice.isdigit() and 1 <= int(choice) <= len(accounts):
                chosen_acc = accounts[int(choice) - 1]["account_name"]
                c_title = accounts[int(choice) - 1]["channel_title"]
                print(f"✅ Đã chọn kênh: '{c_title}' (alias: {chosen_acc})\n")
                return chosen_acc
            print(f"❌ Lựa chọn không hợp lệ. Vui lòng nhập từ 1 đến {len(accounts)} (hoặc 0 để hủy).")
        except (EOFError, KeyboardInterrupt):
            raise CliValidationError("Upload bị hủy bởi người dùng.")


_FEATURE_CHAPTER_ALIASES = frozenset({"feature", "all", "compilation", "full"})
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_TRANSIENT_UPLOAD_STATUSES = frozenset({"uploaded", "processing"})
_TERMINAL_FAILURE_STATUSES = frozenset({"rejected", "failed", "deleted"})


def _is_feature_chapter(chapter_num: Any) -> bool:
    return str(chapter_num).strip().lower() in _FEATURE_CHAPTER_ALIASES


def _parse_q08_config(setup: Any) -> Tuple[bool, Optional[str], Optional[str]]:
    """Return (no_upload, account, privacy) from validated Q08 answer."""
    q08_item = setup.answers.get("Q08")
    u_ans = q08_item.user_answer if q08_item is not None else None
    if u_ans is False or (
        isinstance(u_ans, str)
        and u_ans.strip().lower() in ("không upload", "khong upload", "none", "no")
    ):
        return True, None, None
    if isinstance(u_ans, dict):
        if u_ans.get("no_upload") is True:
            return True, None, None
        acc = str(u_ans.get("account") or "").strip() or None
        priv = str(u_ans.get("privacy") or "").strip() or None
        return False, acc, priv
    raise CliValidationError("Invalid Q08 YouTube configuration in series_setup_answers.json.")


def _account_matches(requested: str, expected: str, acc_info: Any) -> bool:
    req_norm = requested.strip().lower()
    exp_norm = expected.strip().lower()
    if req_norm == exp_norm:
        return True
    for attr in ("account_name", "channel_id", "channel_title"):
        val = getattr(acc_info, attr, None)
        if isinstance(val, str) and val.strip().lower() == exp_norm:
            return True
    return False


def _resolve_upload_video_path(
    project_slug: str,
    chapter_num: Any,
    proj_dir: Path,
    chap_dir: Path,
    out_dir: Path,
    video_override: Optional[str],
) -> Path:
    if video_override:
        p = Path(video_override)
        if not p.is_file():
            raise StorageFileNotFoundError(f"Video override not found: {video_override}")
        return p.resolve()

    if _is_feature_chapter(chapter_num):
        candidates = [
            proj_dir / "video" / "final.mp4",
            out_dir / f"{project_slug}_full_recap.mp4",
            out_dir / "feature_final.mp4",
            out_dir / "chapter_feature.mp4",
        ]
        for cand in candidates:
            if cand.is_file():
                return cand.resolve()
        raise StorageFileNotFoundError(
            f"Feature compilation video not found in {proj_dir / 'video'} or {out_dir}"
        )

    padded_vid_name = format_chapter_filename(chapter_num, ext="mp4", padded=True)
    candidates = [
        chap_dir / "video" / "final.mp4",
        out_dir / padded_vid_name,
        chap_dir / "video" / f"chapter_{chapter_num}_final.mp4",
    ]
    for cand in candidates:
        if cand.is_file():
            return cand.resolve()
    raise StorageFileNotFoundError(
        f"Video not found for chapter {chapter_num}. Looked at {candidates[0]} and {candidates[1]}. "
        "Ensure chapter rendering is completed."
    )


def _resolve_upload_subtitle_path(
    project_slug: str,
    chapter_num: Any,
    proj_dir: Path,
    chap_dir: Path,
    out_dir: Path,
    subtitle_override: Optional[str],
) -> Optional[Path]:
    if subtitle_override:
        p = Path(subtitle_override)
        if not p.is_file():
            raise StorageFileNotFoundError(f"Subtitle override not found: {subtitle_override}")
        return p.resolve()

    if _is_feature_chapter(chapter_num):
        candidates = [
            proj_dir / "subtitles" / "final.srt",
            out_dir / f"{project_slug}_full_recap.srt",
            out_dir / "feature_final.srt",
            out_dir / "chapter_feature.srt",
        ]
    else:
        padded_srt_name = format_chapter_filename(chapter_num, ext="srt", padded=True)
        candidates = [
            chap_dir / "subtitles" / "final.srt",
            out_dir / padded_srt_name,
            chap_dir / "subtitles" / f"chapter_{chapter_num}.srt",
        ]

    for cand in candidates:
        if cand.is_file():
            return cand.resolve()
    return None


def _resolve_upload_thumbnail_path(
    chapter_num: Any,
    proj_dir: Path,
    chap_dir: Path,
    out_dir: Path,
    thumbnail_override: Optional[str],
) -> Optional[Path]:
    if thumbnail_override:
        p = Path(thumbnail_override)
        if not p.is_file():
            raise StorageFileNotFoundError(f"Thumbnail override not found: {thumbnail_override}")
        return p.resolve()

    if _is_feature_chapter(chapter_num):
        candidates = [
            out_dir / "feature_thumbnail.jpg",
            proj_dir / "thumbnail.jpg",
        ]
    else:
        candidates = [
            chap_dir / "thumbnail.jpg",
            out_dir / f"chapter_{chapter_num}_thumbnail.jpg",
        ]

    for cand in candidates:
        if cand.is_file():
            return cand.resolve()
    return None


def _validate_upload_readiness(
    project_slug: str,
    chapter_num: Any,
    proj_dir: Path,
    out_dir: Path,
    video_path: Path,
) -> None:
    """Check preview approval and required export files before upload."""
    from src.core.human_review import require_preview_approval
    from src.render.chapter_renderer import validate_existing_chapter

    require_preview_approval(proj_dir)

    if _is_feature_chapter(chapter_num):
        timeline_path = proj_dir / "feature_timeline.json"
        if not timeline_path.is_file():
            timeline_path = out_dir / f"{project_slug}_timeline.json"
        if timeline_path.is_file():
            timeline = FeatureTimeline.model_validate(read_json(timeline_path))
            for ch in timeline.chapters:
                try:
                    validate_existing_chapter(proj_dir, str(ch.chapter_number), quality="final")
                except (OSError, ValueError) as exc:
                    raise CliValidationError(
                        f"Feature readiness failed for chapter {ch.chapter_number}: {exc}"
                    ) from exc
        if not video_path.is_file() or video_path.stat().st_size <= 0:
            raise CliValidationError(f"Feature video missing or empty: {video_path}")
        return

    try:
        validate_existing_chapter(proj_dir, str(chapter_num), quality="final", video_path=video_path)
    except (OSError, ValueError) as exc:
        raise CliValidationError(
            f"Chapter {chapter_num} final artifact validation failed before upload: {exc}"
        ) from exc



def _extract_upload_evidence(
    is_feature: bool,
    proj_dir: Path,
    chap_dir: Path,
    out_dir: Path,
) -> Dict[str, Any]:
    """Load persisted internal upload evidence (falling back to metadata sidecar)."""
    evidence_file = (
        out_dir / "feature.youtube_upload_evidence.json"
        if is_feature
        else chap_dir / "youtube_upload_evidence.json"
    )
    if evidence_file.is_file():
        return read_json(evidence_file)

    meta_file = out_dir / "feature.youtube.json" if is_feature else chap_dir / "youtube_metadata.json"
    if meta_file.is_file():
        return read_json(meta_file)
    return {}


def handle_youtube_upload(args: argparse.Namespace) -> Dict[str, Any]:
    """Uploads chapter or feature compilation video to YouTube, attaches captions and custom thumbnail."""
    project_slug = getattr(args, "project", "")
    chapter_num = getattr(args, "chapter", None)

    if not project_slug:
        raise CliValidationError("Project slug (--project) is required.")
    if chapter_num is None:
        raise CliValidationError("Chapter number (--chapter) is required.")

    is_feature = _is_feature_chapter(chapter_num)
    proj_dir = get_project_dir(project_slug)
    chap_dir = get_chapter_dir(project_slug, chapter_num)
    out_dir = get_output_dir(project_slug)

    # 1. Enforce Q08 from confirmed series setup (fail closed on no_upload)
    setup = require_series_setup(project_slug)
    no_upload, q08_account, q08_privacy = _parse_q08_config(setup)
    if no_upload:
        raise CliValidationError(
            f"YouTube upload rejected: Q08 for project '{project_slug}' is configured as 'no_upload'."
        )

    # 2. Enforce Mandatory Channel Selection Gate & Q08 account/privacy before touching API
    cli_account_raw = getattr(args, "account", None)
    cli_account = cli_account_raw.strip() if isinstance(cli_account_raw, str) else None
    if cli_account == "":
        cli_account = None
    is_interactive_flag = bool(getattr(args, "interactive", False))
    is_json_mode = bool(getattr(args, "json", False) or getattr(args, "json_mode", False))
    gate_reconfirmed = bool(
        getattr(args, "confirm_channel", False)
        or getattr(args, "reconfirm_channel", False)
        or getattr(args, "gate_reconfirmed", False)
        or getattr(args, "confirm_gate", False)
    )

    allow_oauth = is_interactive_flag and not is_json_mode and sys.stdin.isatty()
    if is_interactive_flag and not allow_oauth:
        raise CliValidationError("Interactive channel selection requires a terminal without --json.")
    if is_interactive_flag or (cli_account is None and not is_json_mode and sys.stdin.isatty()):
        target_account = prompt_interactive_channel_selection(account_manager)
        channel_reconfirmed = True
    elif cli_account is not None:
        target_account = cli_account
        channel_reconfirmed = gate_reconfirmed
    else:
        raise CliValidationError(
            "Mandatory Channel Selection Gate: explicit --account is required in non-interactive/--json mode "
            "(or pass -i/--interactive)."
        )

    acc_info = account_manager.get_account(target_account)
    if q08_account and not _account_matches(target_account, q08_account, acc_info) and not channel_reconfirmed:
        raise CliValidationError(
            f"Requested account '{target_account}' does not match Q08 configured account '{q08_account}' "
            "unless the channel gate is explicitly reconfirmed."
        )

    cli_privacy_raw = getattr(args, "privacy", None)
    cli_privacy = str(cli_privacy_raw).strip() if cli_privacy_raw is not None else None
    if cli_privacy == "":
        cli_privacy = None
    if cli_privacy is not None:
        if cli_privacy not in ("private", "unlisted", "public"):
            raise CliValidationError(f"Invalid privacy_status '{cli_privacy}'. Must be private, unlisted, or public.")
        if q08_privacy and cli_privacy != q08_privacy and not gate_reconfirmed:
            raise CliValidationError(
                f"Requested privacy '{cli_privacy}' does not match Q08 configured privacy '{q08_privacy}' "
                "unless the gate is explicitly reconfirmed."
            )

    # 3. Resolve canonical video, subtitle, and thumbnail paths
    video_path = _resolve_upload_video_path(
        project_slug, chapter_num, proj_dir, chap_dir, out_dir, getattr(args, "video", None)
    )
    subtitle_path = _resolve_upload_subtitle_path(
        project_slug, chapter_num, proj_dir, chap_dir, out_dir, getattr(args, "subtitles", None)
    )
    thumbnail_path = _resolve_upload_thumbnail_path(
        chapter_num, proj_dir, chap_dir, out_dir, getattr(args, "thumbnail", None)
    )

    # Check preview approval and the files selected for upload.
    _validate_upload_readiness(project_slug, chapter_num, proj_dir, out_dir, video_path)

    # An older series can still require a custom thumbnail.
    thumbnail_choice = setup.answers.get("Q07")
    q07_enabled = thumbnail_choice is not None and _normalize_bool(thumbnail_choice.user_answer) is True
    if q07_enabled and (thumbnail_path is None or not thumbnail_path.is_file()):
        raise CliValidationError(
            f"A custom thumbnail is required for '{project_slug}', but thumbnail.jpg is missing for chapter {chapter_num}."
        )
    if (
        not is_feature
        and thumbnail_path is not None
        and (chap_dir / "thumbnail_selection.json").is_file()
        and thumbnail_path == (chap_dir / "thumbnail.jpg").resolve()
    ):
        selection = read_json(chap_dir / "thumbnail_selection.json")
        if selection.get("artifact_sha256") != calculate_sha256(thumbnail_path):
            raise CliValidationError("Chapter thumbnail.jpg differs from thumbnail_selection.json artifact_sha256.")

    # 6. Resolve metadata (eliminating hardcoded feature strings and meta_obj NameError)
    meta_file = out_dir / "feature.youtube.json" if is_feature else chap_dir / "youtube_metadata.json"
    if not meta_file.exists():
        logger.info(f"{meta_file.name} not found, auto-generating metadata...")
        meta_args = argparse.Namespace(**vars(args))
        if getattr(meta_args, "privacy", None) is None:
            meta_args.privacy = q08_privacy or "private"
        handle_youtube_metadata(meta_args)

    meta_obj = YouTubeMetadata.model_validate(read_json(meta_file))
    title = getattr(args, "title", None) or meta_obj.title
    description = getattr(args, "description", None) or meta_obj.description
    privacy = cli_privacy or q08_privacy or meta_obj.privacy_status or "private"
    tags = getattr(args, "tags", None)
    tags_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else meta_obj.tags
    category_id = meta_obj.category_id or "24"
    made_for_kids = bool(meta_obj.made_for_kids)

    # 7. Authenticate with YouTube Data API v3
    account_name_resolved = acc_info.account_name
    channel_title = acc_info.channel_title
    channel_id = acc_info.channel_id

    creds = get_youtube_oauth_credentials(account_name=target_account, allow_interactive=allow_oauth)

    import importlib
    try:
        discovery_mod = importlib.import_module("googleapiclient.discovery")
        http_mod = importlib.import_module("googleapiclient.http")
        build = discovery_mod.build
        MediaFileUpload = http_mod.MediaFileUpload
    except ImportError as e:
        raise DependencyNotFoundError("Missing googleapiclient. Install with: pip install google-api-python-client") from e

    youtube = build("youtube", "v3", credentials=creds)
    if not channel_id:
        try:
            ch_res = youtube.channels().list(mine=True, part="snippet").execute()
            if isinstance(ch_res, dict) and isinstance(ch_res.get("items"), list) and ch_res["items"]:
                first_ch = ch_res["items"][0]
                if isinstance(first_ch, dict):
                    channel_id = first_ch.get("id") or channel_id
                    channel_title = first_ch.get("snippet", {}).get("title") or channel_title
        except Exception:
            pass

    # 8. Execute resumable video upload
    video_sha256 = calculate_sha256(video_path)
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags_list,
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": made_for_kids,
        },
    }

    media = MediaFileUpload(
        str(video_path),
        mimetype="video/mp4",
        resumable=True,
        chunksize=2 * 1024 * 1024,
    )

    logger.info(f"Starting YouTube upload: {title} ({video_path.name})")
    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            progress = int(status.progress() * 100)
            logger.info(f"Upload progress: {progress}%")

    video_id = response.get("id")
    if not video_id:
        raise CliValidationError("YouTube videos().insert returned empty video ID.")
    video_url = f"https://www.youtube.com/watch?v={video_id}"
    logger.info(f"Video uploaded successfully! Video ID: {video_id} - URL: {video_url}")

    # 9. Upload Closed Captions (fail closed if requested/present)
    skip_captions = bool(getattr(args, "no_captions", False))
    captions_required = bool(not skip_captions and subtitle_path is not None and subtitle_path.is_file())
    captions_uploaded = False
    subtitle_sha256: Optional[str] = None
    if captions_required and subtitle_path is not None:
        subtitle_sha256 = calculate_sha256(subtitle_path)
        try:
            logger.info(f"Uploading Vietnamese closed captions from {subtitle_path.name}...")
            caption_body = {
                "snippet": {
                    "videoId": video_id,
                    "language": "vi",
                    "name": "Tiếng Việt",
                    "isDraft": False,
                }
            }
            caption_media = MediaFileUpload(str(subtitle_path), mimetype="application/x-subrip")
            youtube.captions().insert(
                part="snippet",
                body=caption_body,
                media_body=caption_media,
            ).execute()
            captions_uploaded = True
            logger.info("Captions uploaded successfully!")
        except Exception as e:
            raise CliValidationError(
                f"Failed to upload closed captions from {subtitle_path} for video {video_id}: {e}"
            ) from e

    # 10. Upload Custom Thumbnail (fail closed when Q07 enabled or thumbnail provided/detected)
    thumbnail_required = bool(q07_enabled or (thumbnail_path is not None and thumbnail_path.is_file()))
    thumbnail_uploaded = False
    thumbnail_local_sha256: Optional[str] = None
    thumbnail_upload_at: Optional[str] = None
    if thumbnail_path is not None and thumbnail_path.is_file():
        thumbnail_local_sha256 = calculate_sha256(thumbnail_path)
        try:
            logger.info(f"Uploading custom thumbnail from {thumbnail_path}...")
            youtube.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(str(thumbnail_path), mimetype="image/jpeg"),
            ).execute()
            thumbnail_uploaded = True
            thumbnail_upload_at = utc_iso_now()
            logger.info("Thumbnail uploaded successfully!")
        except Exception as e:
            raise CliValidationError(
                f"Failed to upload custom thumbnail from {thumbnail_path} for video {video_id}: {e}"
            ) from e
    elif thumbnail_required:
        raise CliValidationError(
            f"Required custom thumbnail missing for project '{project_slug}' chapter {chapter_num}."
        )

    uploaded_at = utc_iso_now()

    # 11. Persist internal upload evidence & update metadata sidecar
    evidence_payload = {
        "schema_version": "4.0.0",
        "project_slug": project_slug,
        "chapter_number": str(chapter_num),
        "video_id": video_id,
        "account": account_name_resolved,
        "channel_id": channel_id,
        "privacy_status": privacy,
        "video_sha256": video_sha256,
        "subtitle_sha256": subtitle_sha256,
        "thumbnail_local_sha256": thumbnail_local_sha256,
        "thumbnail_upload_at": thumbnail_upload_at,
        "uploaded_at": uploaded_at,
    }
    evidence_file = (
        out_dir / "feature.youtube_upload_evidence.json"
        if is_feature
        else chap_dir / "youtube_upload_evidence.json"
    )
    evidence_file.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(evidence_file, evidence_payload)

    try:
        m_data = read_json(meta_file) if meta_file.exists() else {}
        m_data["video_id"] = video_id
        m_data["account"] = account_name_resolved
        m_data["video_sha256"] = video_sha256
        m_data["subtitle_sha256"] = subtitle_sha256
        m_data["thumbnail_local_sha256"] = thumbnail_local_sha256
        m_data["thumbnail_upload_at"] = thumbnail_upload_at
        atomic_write_json(meta_file, m_data)
    except Exception as e:
        logger.warning(f"Failed to update metadata with video_id/evidence: {e}")

    # 12. Validate and emit immutable youtube_receipt.json (status="uploaded")
    receipt_obj = YouTubeReceipt.model_validate({
        "schema_version": "4.0.0",
        "receipt_type": "youtube_upload",
        "project_slug": project_slug,
        "chapter_number": str(chapter_num),
        "video_id": video_id,
        "video_url": video_url,
        "account": account_name_resolved,
        "channel_title": channel_title,
        "channel_id": channel_id,
        "title": title,
        "upload_status": None,
        "has_captions": None,
        "has_custom_thumbnail": None,
        "verified_at": None,
        "privacy_status": privacy,
        "captions_uploaded": captions_uploaded,
        "thumbnail_uploaded": thumbnail_uploaded,
        "captions_required": captions_required,
        "thumbnail_required": thumbnail_required,
        "uploaded_at": uploaded_at,
        "status": "uploaded",
    })
    receipt_file = (
        out_dir / "feature.youtube_receipt.json"
        if is_feature
        else chap_dir / "youtube_receipt.json"
    )
    receipt_file.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(receipt_file, receipt_obj)

    if getattr(args, "wait", False):
        verify_args = argparse.Namespace(
            youtube_action="verify",
            project=project_slug,
            chapter=str(chapter_num),
            video_id=video_id,
            account=account_name_resolved,
            poll_interval=getattr(args, "poll_interval", 5.0),
            timeout=getattr(args, "timeout", 300.0),
            max_backoff=getattr(args, "max_backoff", None),
        )
        verify_res = handle_youtube_verify(verify_args)
        verify_res.update({
            "account": account_name_resolved,
            "channel_title": channel_title,
            "channel_id": channel_id,
            "title": title,
            "captions_uploaded": captions_uploaded,
            "thumbnail_uploaded": thumbnail_uploaded,
            "video_sha256": video_sha256,
            "subtitle_sha256": subtitle_sha256,
            "thumbnail_local_sha256": thumbnail_local_sha256,
            "thumbnail_upload_at": thumbnail_upload_at,
            "evidence_path": str(evidence_file),
        })
        return verify_res

    return {
        "status": "success",
        "verified": False,
        "video_id": video_id,
        "video_url": video_url,
        "account": account_name_resolved,
        "channel_title": channel_title,
        "channel_id": channel_id,
        "title": title,
        "privacy_status": privacy,
        "captions_uploaded": captions_uploaded,
        "thumbnail_uploaded": thumbnail_uploaded,
        "video_sha256": video_sha256,
        "subtitle_sha256": subtitle_sha256,
        "thumbnail_local_sha256": thumbnail_local_sha256,
        "thumbnail_upload_at": thumbnail_upload_at,
        "receipt_path": str(receipt_file),
        "evidence_path": str(evidence_file),
    }


def _wait_for_youtube_processing(youtube, video_id, account, timeout, interval, max_backoff, max_attempts):
    deadline = time.monotonic() + timeout
    for attempt in range(1, max_attempts + 1):
        try:
            response = youtube.videos().list(part="status,snippet,contentDetails", id=video_id).execute()
        except Exception as exc:
            raise CliValidationError(f"Could not check YouTube video {video_id}: {exc}") from exc
        items = response.get("items", [])
        if not items:
            raise CliValidationError(f"YouTube video {video_id} was not found on account {account}")
        status = items[0].get("status", {})
        state = str(status.get("uploadStatus", "")).strip()
        if state == "processed":
            return items[0], attempt
        if state not in _TRANSIENT_UPLOAD_STATUSES:
            reason = status.get("rejectionReason") or status.get("failureReason") or "unknown"
            raise CliValidationError(f"YouTube processing failed for {video_id}: {state} ({reason})")
        remaining = deadline - time.monotonic()
        if remaining <= 0 or attempt >= max_attempts:
            raise CliValidationError(f"Timed out waiting for YouTube video {video_id} (last status: {state})")
        if interval > 0:
            time.sleep(min(interval, remaining))
            interval = min(max_backoff, interval * 1.5)


def handle_youtube_verify(args: argparse.Namespace) -> Dict[str, Any]:
    """
    Polls YouTube Data API until uploadStatus == 'processed' (within bounded timeout/backoff),
    verifies channel_id, privacy_status, closed captions, and thumbnail evidence,
    writes a YouTubeReceipt with status='verified'.
    """
    project_slug = getattr(args, "project", "")
    chapter_num = getattr(args, "chapter", "1")
    if not project_slug:
        raise CliValidationError("Project slug (--project) is required for verification.")

    poll_interval_raw = getattr(args, "poll_interval", 5.0)
    poll_interval = float(5.0 if poll_interval_raw is None else poll_interval_raw)
    timeout_raw = getattr(args, "timeout", 300.0)
    timeout = float(300.0 if timeout_raw is None else timeout_raw)
    max_backoff_raw = getattr(args, "max_backoff", None)
    max_backoff = float(max(poll_interval, 30.0) if max_backoff_raw is None else max_backoff_raw)
    if poll_interval < 0 or timeout < 0 or max_backoff < 0:
        raise CliValidationError("poll_interval, timeout, and max_backoff must be non-negative.")

    setup = require_series_setup(project_slug)
    no_upload, _, _ = _parse_q08_config(setup)
    if no_upload:
        raise CliValidationError(
            f"YouTube verification rejected: Q08 for project '{project_slug}' is configured as 'no_upload'."
        )

    is_feature = _is_feature_chapter(chapter_num)
    proj_dir = get_project_dir(project_slug)
    chap_dir = get_chapter_dir(project_slug, chapter_num)
    out_dir = get_output_dir(project_slug)

    # A successful videos.insert receipt is required; verifying an arbitrary ID
    # must never manufacture a cleanup-eligible YouTube upload receipt.
    upload_receipt_file = (
        out_dir / "feature.youtube_receipt.json"
        if is_feature else chap_dir / "youtube_receipt.json"
    )
    if not upload_receipt_file.exists():
        raise CliValidationError(
            "No YouTube upload receipt found for this chapter. "
            "Verification alone cannot establish that this project uploaded the video."
        )
    try:
        upload_receipt = YouTubeReceipt.model_validate(read_json(upload_receipt_file))
    except Exception as exc:
        raise CliValidationError(f"Invalid upload youtube_receipt.json: {exc}") from exc

    if (
        upload_receipt.project_slug != project_slug
        or str(upload_receipt.chapter_number) != str(chapter_num)
    ):
        raise CliValidationError("YouTube receipt project/chapter does not match verification target.")

    video_id = getattr(args, "video_id", None) or upload_receipt.video_id
    if not video_id or upload_receipt.video_id != video_id:
        raise CliValidationError("YouTube video ID/project does not match the original upload receipt.")

    target_account = getattr(args, "account", None) or upload_receipt.account
    if not target_account or target_account != upload_receipt.account:
        raise CliValidationError("YouTube verification must use the exact upload account.")
    acc_info = account_manager.get_account(target_account)
    account_name_resolved = acc_info.account_name
    creds = get_youtube_oauth_credentials(account_name=target_account)

    try:
        import importlib
        discovery_mod = importlib.import_module("googleapiclient.discovery")
        build = discovery_mod.build
    except ImportError as e:
        raise DependencyNotFoundError("Missing googleapiclient. Install with: pip install google-api-python-client") from e

    youtube = build("youtube", "v3", credentials=creds)
    v_item, attempt = _wait_for_youtube_processing(
        youtube, video_id, account_name_resolved, timeout, poll_interval, max_backoff,
        int(getattr(args, "max_attempts", 50 if poll_interval == 0 else 10_000)),
    )
    upload_status = "processed"

    snippet = v_item.get("snippet", {})
    v_status = v_item.get("status", {})
    privacy_status = v_status.get("privacyStatus", "")
    title = snippet.get("title", "") or upload_receipt.title
    channel_title = snippet.get("channelTitle", "") or upload_receipt.channel_title
    channel_id = snippet.get("channelId", "")

    expected_channel_id = upload_receipt.channel_id or acc_info.channel_id
    if not channel_id or (expected_channel_id and channel_id != expected_channel_id):
        raise CliValidationError(
            f"YouTube channel mismatch: expected {expected_channel_id!r}, got {channel_id!r}."
        )
    req_privacy = upload_receipt.privacy_status
    if privacy_status != req_privacy:
        raise CliValidationError(
            f"YouTube privacyStatus mismatch for video {video_id}: expected '{req_privacy}', found '{privacy_status}'"
        )

    if upload_receipt.captions_required and not upload_receipt.captions_uploaded:
        raise CliValidationError("Required captions upload failed; verification is not allowed.")

    has_captions = False
    captions_needed = bool(upload_receipt.captions_required or upload_receipt.captions_uploaded)
    try:
        cap_res = youtube.captions().list(part="snippet", videoId=video_id).execute()
        cap_items = cap_res.get("items", [])
        has_captions = any(
            it.get("snippet", {}).get("language") == "vi" for it in cap_items
        )
    except Exception as e:
        if captions_needed:
            raise CliValidationError(f"Failed to verify captions for video {video_id}: {e}") from e
        logger.warning(f"Could not query captions for video {video_id}: {e}")

    if captions_needed and not has_captions:
        raise CliValidationError(
            f"Requested Vietnamese captions not verified for video {video_id}."
        )

    if upload_receipt.thumbnail_required and not upload_receipt.thumbnail_uploaded:
        raise CliValidationError("Required custom thumbnail upload failed; verification is not allowed.")

    thumbnails = snippet.get("thumbnails", {})
    has_thumbnail = bool(
        thumbnails.get("maxres")
        or thumbnails.get("standard")
        or thumbnails.get("high")
        or thumbnails.get("medium")
        or thumbnails.get("default")
    )

    evidence = _extract_upload_evidence(is_feature, proj_dir, chap_dir, out_dir)
    if upload_receipt.thumbnail_required or upload_receipt.thumbnail_uploaded:
        if not has_thumbnail:
            raise CliValidationError(
                f"Requested thumbnail is missing from the video snippet for {video_id}."
            )
        thumb_upload_at = evidence.get("thumbnail_upload_at")
        thumb_sha = evidence.get("thumbnail_local_sha256")
        if (
            not isinstance(thumb_upload_at, str)
            or not thumb_upload_at.strip()
            or not isinstance(thumb_sha, str)
            or not _SHA256_RE.fullmatch(thumb_sha)
        ):
            raise CliValidationError(
                f"Missing custom thumbnail upload evidence (thumbnail_upload_at / thumbnail_local_sha256) for video {video_id}."
            )
        if evidence.get("video_id") and evidence["video_id"] != video_id:
            raise CliValidationError(f"Thumbnail upload evidence video_id mismatch for {video_id}.")

    now_ts = utc_iso_now()
    verified_receipt = YouTubeReceipt.model_validate({
        "schema_version": "4.0.0",
        "receipt_type": "youtube_verification",
        "project_slug": project_slug,
        "chapter_number": str(chapter_num),
        "video_id": video_id,
        "video_url": f"https://www.youtube.com/watch?v={video_id}",
        "account": account_name_resolved,
        "channel_title": channel_title,
        "channel_id": channel_id,
        "title": title,
        "upload_status": upload_status,
        "privacy_status": privacy_status,
        "has_captions": has_captions,
        "has_custom_thumbnail": bool(has_thumbnail and upload_receipt.thumbnail_uploaded),
        "captions_required": upload_receipt.captions_required,
        "thumbnail_required": upload_receipt.thumbnail_required,
        "captions_uploaded": upload_receipt.captions_uploaded,
        "thumbnail_uploaded": upload_receipt.thumbnail_uploaded,
        "uploaded_at": upload_receipt.uploaded_at,
        "status": "verified",
        "verified_at": now_ts,
    })

    receipt_file = out_dir / "feature.youtube_receipt.json" if is_feature else chap_dir / "youtube_receipt.json"
    receipt_file.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(receipt_file, verified_receipt)

    if not is_feature and out_dir.exists():
        staged_name = format_chapter_filename(chapter_num, ext="youtube_receipt.json", padded=True)
        atomic_write_json(out_dir / staged_name, verified_receipt)

    logger.info(f"YouTube verification successful for video {video_id}! Emitted receipt: {receipt_file}")
    return {
        "status": "success",
        "verified": True,
        "project_slug": project_slug,
        "chapter_number": str(chapter_num),
        "video_id": video_id,
        "video_url": f"https://www.youtube.com/watch?v={video_id}",
        "upload_status": upload_status,
        "privacy_status": privacy_status,
        "has_captions": has_captions,
        "has_custom_thumbnail": bool(has_thumbnail and upload_receipt.thumbnail_uploaded),
        "poll_attempts": attempt,
        "video_sha256": evidence.get("video_sha256"),
        "subtitle_sha256": evidence.get("subtitle_sha256"),
        "thumbnail_local_sha256": evidence.get("thumbnail_local_sha256"),
        "thumbnail_upload_at": evidence.get("thumbnail_upload_at"),
        "receipt_path": str(receipt_file),
    }


def handle_youtube(args: argparse.Namespace) -> Dict[str, Any]:
    """Top-level dispatch router for `manga-recap youtube` subcommands."""
    from src.core.execution_policy import enforce_execution_policy
    enforce_execution_policy("youtube", args)
    yt_action = getattr(args, "youtube_action", "metadata")
    if yt_action in ("metadata", "prepare"):
        return handle_youtube_metadata(args)
    if yt_action == "upload":
        return handle_youtube_upload(args)
    if yt_action == "verify":
        return handle_youtube_verify(args)
    if yt_action == "auth":
        return handle_youtube_auth(args)
    if yt_action in ("accounts", "account"):
        return handle_youtube_accounts(args)

    raise CliValidationError(
        f"Unknown YouTube action '{yt_action}'. Available actions: metadata, upload, verify, auth, accounts."
    )

