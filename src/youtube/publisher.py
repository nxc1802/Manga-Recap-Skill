"""Domain publishing workflow for YouTube channel gating, resumable recovery, and verification."""

from __future__ import annotations

import argparse
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from src.core.errors import DomainValidationError, StorageFileNotFoundError
from src.core.logger import logger
from src.core.project import get_chapter_dir, get_output_dir, get_project_dir
from src.core.schemas import (
    FeatureTimeline,
    YouTubeMetadata,
    YouTubeReceipt,
    utc_iso_now,
)
from src.core.series_setup import _normalize_bool, require_series_setup
from src.core.storage import atomic_write_json, calculate_sha256, format_chapter_filename, read_json
from src.providers.youtube import YouTubeDataClient
from src.youtube.account_manager import YouTubeAccountManager
from src.youtube.metadata import handle_youtube_metadata

_FEATURE_CHAPTER_ALIASES = frozenset({"feature", "all", "compilation", "full"})
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


def is_feature_chapter(chapter_num: Any) -> bool:
    return str(chapter_num).strip().lower() in _FEATURE_CHAPTER_ALIASES


def parse_q08_config(setup: Any) -> Tuple[bool, Optional[str], Optional[str]]:
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
    raise DomainValidationError("Invalid Q08 YouTube configuration in series_setup_answers.json.")


def account_matches(requested: str, expected: str, acc_info: Any) -> bool:
    req_norm = requested.strip().lower()
    exp_norm = expected.strip().lower()
    if req_norm == exp_norm:
        return True
    for attr in ("account_name", "channel_id", "channel_title"):
        val = getattr(acc_info, attr, None)
        if isinstance(val, str) and val.strip().lower() == exp_norm:
            return True
    return False


def resolve_upload_video_path(
    project_slug: str,
    chapter_num: Any,
    proj_dir: Path,
    chap_dir: Path,
    out_dir: Path,
) -> Path:
    if is_feature_chapter(chapter_num):
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


def resolve_upload_subtitle_path(
    project_slug: str,
    chapter_num: Any,
    proj_dir: Path,
    chap_dir: Path,
    out_dir: Path,
) -> Optional[Path]:
    if is_feature_chapter(chapter_num):
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


def resolve_upload_thumbnail_path(
    chapter_num: Any,
    proj_dir: Path,
    chap_dir: Path,
    out_dir: Path,
) -> Optional[Path]:
    if is_feature_chapter(chapter_num):
        candidates = [
            out_dir / "feature_thumbnail.jpg",
            out_dir / "feature_thumbnail.png",
            proj_dir / "thumbnail.jpg",
            proj_dir / "thumbnail.png",
        ]
    else:
        candidates = [
            chap_dir / "thumbnail.jpg",
            chap_dir / "thumbnail.png",
            out_dir / f"chapter_{chapter_num}_thumbnail.jpg",
            out_dir / f"chapter_{chapter_num}_thumbnail.png",
        ]

    for cand in candidates:
        if cand.is_file():
            return cand.resolve()
    return None


def validate_upload_readiness(
    project_slug: str,
    chapter_num: Any,
    proj_dir: Path,
    out_dir: Path,
    video_path: Path,
) -> None:
    """Checks preview approval and rendered artifact readiness before YouTube upload."""
    from src.core.human_review import require_preview_approval
    from src.render.chapter_renderer import validate_existing_chapter

    require_preview_approval(proj_dir)

    if is_feature_chapter(chapter_num):
        timeline_path = proj_dir / "feature_timeline.json"
        if not timeline_path.is_file():
            timeline_path = out_dir / f"{project_slug}_timeline.json"
        if timeline_path.is_file():
            timeline = FeatureTimeline.model_validate(read_json(timeline_path))
            for ch in timeline.chapters:
                try:
                    validate_existing_chapter(proj_dir, str(ch.chapter_number), quality="final")
                except (OSError, ValueError) as exc:
                    raise DomainValidationError(
                        f"Feature readiness failed for chapter {ch.chapter_number}: {exc}"
                    ) from exc
        if not video_path.is_file() or video_path.stat().st_size <= 0:
            raise DomainValidationError(f"Feature video missing or empty: {video_path}")
        return

    try:
        validate_existing_chapter(proj_dir, str(chapter_num), quality="final", video_path=video_path)
    except (OSError, ValueError) as exc:
        raise DomainValidationError(
            f"Chapter {chapter_num} final artifact validation failed before upload: {exc}"
        ) from exc


def extract_upload_evidence(
    is_feature: bool,
    proj_dir: Path,
    chap_dir: Path,
    out_dir: Path,
) -> Dict[str, Any]:
    """Loads persisted internal upload evidence (falling back to metadata sidecar)."""
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


def _persist_upload_records(
    *,
    evidence_file: Path,
    receipt_file: Path,
    meta_file: Path,
    evidence_payload: Dict[str, Any],
    receipt_payload: Optional[Dict[str, Any]] = None,
) -> None:
    """Atomically writes upload evidence/video_id and optionally finalizes YouTubeReceipt."""
    evidence_file.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(evidence_file, evidence_payload)

    try:
        m_data = read_json(meta_file) if meta_file.exists() else {}
        m_data["video_id"] = evidence_payload["video_id"]
        m_data["account"] = evidence_payload["account"]
        m_data["video_sha256"] = evidence_payload["video_sha256"]
        m_data["subtitle_sha256"] = evidence_payload.get("subtitle_sha256")
        m_data["thumbnail_local_sha256"] = evidence_payload.get("thumbnail_local_sha256")
        m_data["thumbnail_upload_at"] = evidence_payload.get("thumbnail_upload_at")
        atomic_write_json(meta_file, m_data)
    except Exception as exc:
        logger.warning(f"Failed to update metadata with video_id/evidence: {exc}")

    if receipt_payload is not None:
        receipt_obj = YouTubeReceipt.model_validate(receipt_payload)
        receipt_file.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(receipt_file, receipt_obj)


def publish_chapter_video(
    args: argparse.Namespace,
    *,
    account_mgr: YouTubeAccountManager,
    credentials_fn: Callable[..., Any],
    sleep_fn: Callable[[float], None] = time.sleep,
) -> Dict[str, Any]:
    """Uploads chapter/feature video with immediate video_id persistence and attachment guarantees."""
    project_slug = getattr(args, "project", "")
    chapter_num = getattr(args, "chapter", None)
    if not project_slug:
        raise DomainValidationError("Project slug (--project) is required.")
    if chapter_num is None:
        raise DomainValidationError("Chapter number (--chapter) is required.")

    is_feature = is_feature_chapter(chapter_num)
    proj_dir = get_project_dir(project_slug)
    chap_dir = get_chapter_dir(project_slug, chapter_num)
    out_dir = get_output_dir(project_slug)

    setup = require_series_setup(project_slug)
    no_upload, q08_account, q08_privacy = parse_q08_config(setup)
    if no_upload:
        raise DomainValidationError(
            f"YouTube upload rejected: Q08 for project '{project_slug}' is configured as 'no_upload'."
        )

    cli_account_raw = getattr(args, "account", None)
    cli_account = cli_account_raw.strip() if isinstance(cli_account_raw, str) else None
    if cli_account == "":
        cli_account = None
    gate_reconfirmed = bool(
        getattr(args, "confirm_channel", False)
        or getattr(args, "reconfirm_channel", False)
        or getattr(args, "gate_reconfirmed", False)
        or getattr(args, "confirm_gate", False)
    )
    if cli_account is None:
        raise DomainValidationError(
            "Mandatory Channel Selection Gate: explicit --account is required before YouTube upload."
        )
    target_account = cli_account
    channel_reconfirmed = gate_reconfirmed

    acc_info = account_mgr.get_account(target_account)
    if q08_account and not account_matches(target_account, q08_account, acc_info) and not channel_reconfirmed:
        raise DomainValidationError(
            f"Requested account '{target_account}' does not match Q08 configured account '{q08_account}' "
            "unless the channel gate is explicitly reconfirmed."
        )

    cli_privacy_raw = getattr(args, "privacy", None)
    cli_privacy = str(cli_privacy_raw).strip() if cli_privacy_raw is not None else None
    if cli_privacy == "":
        cli_privacy = None
    if cli_privacy is not None:
        if cli_privacy not in ("private", "unlisted", "public"):
            raise DomainValidationError(f"Invalid privacy_status '{cli_privacy}'. Must be private, unlisted, or public.")
        if q08_privacy and cli_privacy != q08_privacy and not gate_reconfirmed:
            raise DomainValidationError(
                f"Requested privacy '{cli_privacy}' does not match Q08 configured privacy '{q08_privacy}' "
                "unless the gate is explicitly reconfirmed."
            )

    video_path = resolve_upload_video_path(project_slug, chapter_num, proj_dir, chap_dir, out_dir)
    subtitle_path = resolve_upload_subtitle_path(project_slug, chapter_num, proj_dir, chap_dir, out_dir)
    thumbnail_path = resolve_upload_thumbnail_path(chapter_num, proj_dir, chap_dir, out_dir)

    validate_upload_readiness(project_slug, chapter_num, proj_dir, out_dir, video_path)

    thumbnail_choice = setup.answers.get("Q07")
    q07_enabled = thumbnail_choice is not None and _normalize_bool(thumbnail_choice.user_answer) is True
    if q07_enabled and (thumbnail_path is None or not thumbnail_path.is_file()):
        raise DomainValidationError(
            f"A custom thumbnail is required for '{project_slug}', but thumbnail.jpg is missing for chapter {chapter_num}."
        )

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

    account_name_resolved = acc_info.account_name
    channel_title = acc_info.channel_title
    channel_id = acc_info.channel_id

    creds = credentials_fn(account_name=target_account, allow_interactive=False)
    yt_client = YouTubeDataClient(creds, sleep_fn=sleep_fn)
    if not channel_id:
        fetched_id, fetched_title = yt_client.fetch_mine_channel_info()
        channel_id = fetched_id or channel_id
        channel_title = fetched_title or channel_title

    video_sha256 = calculate_sha256(video_path)
    evidence_file = (
        out_dir / "feature.youtube_upload_evidence.json"
        if is_feature
        else chap_dir / "youtube_upload_evidence.json"
    )
    receipt_file = (
        out_dir / "feature.youtube_receipt.json"
        if is_feature
        else chap_dir / "youtube_receipt.json"
    )

    skip_captions = bool(getattr(args, "no_captions", False))
    captions_required = bool(not skip_captions and subtitle_path is not None and subtitle_path.is_file())
    thumbnail_required = bool(q07_enabled or (thumbnail_path is not None and thumbnail_path.is_file()))

    # Resume incomplete previous upload of the exact same video+account instead of creating duplicate videos.
    resumed_video_id: Optional[str] = None
    captions_uploaded = False
    thumbnail_uploaded = False
    subtitle_sha256: Optional[str] = None
    thumbnail_local_sha256: Optional[str] = None
    thumbnail_upload_at: Optional[str] = None
    uploaded_at = utc_iso_now()

    if not getattr(args, "force", False) and evidence_file.is_file():
        try:
            prev_ev = read_json(evidence_file)
            if (
                prev_ev.get("partial_upload") is True
                and prev_ev.get("video_sha256") == video_sha256
                and prev_ev.get("account") == account_name_resolved
                and prev_ev.get("video_id")
            ):
                resumed_video_id = str(prev_ev["video_id"])
                captions_uploaded = bool(prev_ev.get("captions_uploaded", False))
                thumbnail_uploaded = bool(prev_ev.get("thumbnail_uploaded", False))
                subtitle_sha256 = prev_ev.get("subtitle_sha256")
                thumbnail_local_sha256 = prev_ev.get("thumbnail_local_sha256")
                thumbnail_upload_at = prev_ev.get("thumbnail_upload_at")
                uploaded_at = prev_ev.get("uploaded_at") or uploaded_at
                logger.info(f"Resuming partial YouTube upload for existing video ID {resumed_video_id}...")
        except Exception:
            resumed_video_id = None

    if resumed_video_id:
        video_id = resumed_video_id
    else:
        video_id = yt_client.insert_video(
            video_path=video_path,
            title=title,
            description=description,
            tags=tags_list,
            category_id=category_id,
            privacy_status=privacy,
            made_for_kids=made_for_kids,
        )
        uploaded_at = utc_iso_now()

    video_url = f"https://www.youtube.com/watch?v={video_id}"
    if not resumed_video_id:
        logger.info(f"Video uploaded successfully! Video ID: {video_id} - URL: {video_url}")

    # Persist video_id immediately upon insert so partial attachment failure never orphans uploaded videos.
    evidence_payload: Dict[str, Any] = {
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
        "captions_uploaded": captions_uploaded,
        "thumbnail_uploaded": thumbnail_uploaded,
        "uploaded_at": uploaded_at,
        "partial_upload": True,
    }
    _persist_upload_records(
        evidence_file=evidence_file,
        receipt_file=receipt_file,
        meta_file=meta_file,
        evidence_payload=evidence_payload,
        receipt_payload=None,
    )

    if captions_required and subtitle_path is not None and not captions_uploaded:
        subtitle_sha256 = calculate_sha256(subtitle_path)
        try:
            logger.info(f"Uploading Vietnamese closed captions from {subtitle_path.name}...")
            yt_client.upload_captions(video_id=video_id, subtitle_path=subtitle_path)
            captions_uploaded = True
            logger.info("Captions uploaded successfully!")
        except Exception as exc:
            evidence_payload["partial_error"] = f"captions: {exc}"
            _persist_upload_records(
                evidence_file=evidence_file,
                receipt_file=receipt_file,
                meta_file=meta_file,
                evidence_payload=evidence_payload,
                receipt_payload=None,
            )
            raise DomainValidationError(
                f"Failed to upload closed captions from {subtitle_path} for video {video_id}: {exc}"
            ) from exc

        evidence_payload["subtitle_sha256"] = subtitle_sha256
        evidence_payload["captions_uploaded"] = True
        evidence_payload.pop("partial_error", None)
        _persist_upload_records(
            evidence_file=evidence_file,
            receipt_file=receipt_file,
            meta_file=meta_file,
            evidence_payload=evidence_payload,
            receipt_payload=None,
        )

    if thumbnail_path is not None and thumbnail_path.is_file() and not thumbnail_uploaded:
        thumbnail_local_sha256 = calculate_sha256(thumbnail_path)
        try:
            logger.info(f"Uploading custom thumbnail from {thumbnail_path}...")
            yt_client.set_thumbnail(video_id=video_id, thumbnail_path=thumbnail_path)
            thumbnail_uploaded = True
            thumbnail_upload_at = utc_iso_now()
            logger.info("Thumbnail uploaded successfully!")
        except Exception as exc:
            evidence_payload["partial_error"] = f"thumbnail: {exc}"
            _persist_upload_records(
                evidence_file=evidence_file,
                receipt_file=receipt_file,
                meta_file=meta_file,
                evidence_payload=evidence_payload,
                receipt_payload=None,
            )
            raise DomainValidationError(
                f"Failed to upload custom thumbnail from {thumbnail_path} for video {video_id}: {exc}"
            ) from exc
    elif thumbnail_required and not thumbnail_uploaded:
        raise DomainValidationError(
            f"Required custom thumbnail missing for project '{project_slug}' chapter {chapter_num}."
        )

    evidence_payload.update({
        "thumbnail_local_sha256": thumbnail_local_sha256,
        "thumbnail_upload_at": thumbnail_upload_at,
        "captions_uploaded": captions_uploaded,
        "thumbnail_uploaded": thumbnail_uploaded,
        "partial_upload": False,
    })
    evidence_payload.pop("partial_error", None)
    receipt_payload: Dict[str, Any] = {
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
    }
    _persist_upload_records(
        evidence_file=evidence_file,
        receipt_file=receipt_file,
        meta_file=meta_file,
        evidence_payload=evidence_payload,
        receipt_payload=receipt_payload,
    )

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
        verify_res = verify_uploaded_video(
            verify_args,
            account_mgr=account_mgr,
            credentials_fn=credentials_fn,
            sleep_fn=sleep_fn,
        )
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


def verify_uploaded_video(
    args: argparse.Namespace,
    *,
    account_mgr: YouTubeAccountManager,
    credentials_fn: Callable[..., Any],
    sleep_fn: Callable[[float], None] = time.sleep,
) -> Dict[str, Any]:
    """Polls YouTube Data API until processed, validates channel/privacy/captions/thumbnail, and emits verified receipt."""
    project_slug = getattr(args, "project", "")
    chapter_num = getattr(args, "chapter", "1")
    if not project_slug:
        raise DomainValidationError("Project slug (--project) is required for verification.")

    poll_interval_raw = getattr(args, "poll_interval", 5.0)
    poll_interval = float(5.0 if poll_interval_raw is None else poll_interval_raw)
    timeout_raw = getattr(args, "timeout", 300.0)
    timeout = float(300.0 if timeout_raw is None else timeout_raw)
    max_backoff_raw = getattr(args, "max_backoff", None)
    max_backoff = float(max(poll_interval, 30.0) if max_backoff_raw is None else max_backoff_raw)
    if poll_interval < 0 or timeout < 0 or max_backoff < 0:
        raise DomainValidationError("poll_interval, timeout, and max_backoff must be non-negative.")

    setup = require_series_setup(project_slug)
    no_upload, _, _ = parse_q08_config(setup)
    if no_upload:
        raise DomainValidationError(
            f"YouTube verification rejected: Q08 for project '{project_slug}' is configured as 'no_upload'."
        )

    is_feature = is_feature_chapter(chapter_num)
    proj_dir = get_project_dir(project_slug)
    chap_dir = get_chapter_dir(project_slug, chapter_num)
    out_dir = get_output_dir(project_slug)

    upload_receipt_file = (
        out_dir / "feature.youtube_receipt.json"
        if is_feature
        else chap_dir / "youtube_receipt.json"
    )
    if not upload_receipt_file.exists():
        raise DomainValidationError(
            "No YouTube upload receipt found for this chapter. "
            "Verification alone cannot establish that this project uploaded the video."
        )
    try:
        upload_receipt = YouTubeReceipt.model_validate(read_json(upload_receipt_file))
    except Exception as exc:
        raise DomainValidationError(f"Invalid upload youtube_receipt.json: {exc}") from exc

    if upload_receipt.project_slug != project_slug or str(upload_receipt.chapter_number) != str(chapter_num):
        raise DomainValidationError("YouTube receipt project/chapter does not match verification target.")

    video_id = getattr(args, "video_id", None) or upload_receipt.video_id
    if not video_id or upload_receipt.video_id != video_id:
        raise DomainValidationError("YouTube video ID/project does not match the original upload receipt.")

    target_account = getattr(args, "account", None) or upload_receipt.account
    if not target_account or target_account != upload_receipt.account:
        raise DomainValidationError("YouTube verification must use the exact upload account.")

    acc_info = account_mgr.get_account(target_account)
    account_name_resolved = acc_info.account_name
    creds = credentials_fn(account_name=target_account)

    yt_client = YouTubeDataClient(creds, sleep_fn=sleep_fn)
    v_item, attempt = yt_client.wait_for_processing(
        video_id=video_id,
        account=account_name_resolved,
        timeout=timeout,
        poll_interval=poll_interval,
        max_backoff=max_backoff,
        max_attempts=int(getattr(args, "max_attempts", 50 if poll_interval == 0 else 10_000)),
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
        raise DomainValidationError(
            f"YouTube channel mismatch: expected {expected_channel_id!r}, got {channel_id!r}."
        )
    req_privacy = upload_receipt.privacy_status
    if privacy_status != req_privacy:
        raise DomainValidationError(
            f"YouTube privacyStatus mismatch for video {video_id}: expected '{req_privacy}', found '{privacy_status}'"
        )

    if upload_receipt.captions_required and not upload_receipt.captions_uploaded:
        raise DomainValidationError("Required captions upload failed; verification is not allowed.")

    has_captions = False
    captions_needed = bool(upload_receipt.captions_required or upload_receipt.captions_uploaded)
    try:
        cap_items = yt_client.list_captions(video_id=video_id)
        has_captions = any(it.get("snippet", {}).get("language") == "vi" for it in cap_items)
    except Exception as exc:
        if captions_needed:
            raise DomainValidationError(f"Failed to verify captions for video {video_id}: {exc}") from exc
        logger.warning(f"Could not query captions for video {video_id}: {exc}")

    if captions_needed and not has_captions:
        raise DomainValidationError(f"Requested Vietnamese captions not verified for video {video_id}.")

    if upload_receipt.thumbnail_required and not upload_receipt.thumbnail_uploaded:
        raise DomainValidationError("Required custom thumbnail upload failed; verification is not allowed.")

    thumbnails = snippet.get("thumbnails", {})
    has_thumbnail = bool(
        thumbnails.get("maxres")
        or thumbnails.get("standard")
        or thumbnails.get("high")
        or thumbnails.get("medium")
        or thumbnails.get("default")
    )

    evidence = extract_upload_evidence(is_feature, proj_dir, chap_dir, out_dir)
    if upload_receipt.thumbnail_required or upload_receipt.thumbnail_uploaded:
        if not has_thumbnail:
            raise DomainValidationError(
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
            raise DomainValidationError(
                f"Missing custom thumbnail upload evidence (thumbnail_upload_at / thumbnail_local_sha256) for video {video_id}."
            )
        if evidence.get("video_id") and evidence["video_id"] != video_id:
            raise DomainValidationError(f"Thumbnail upload evidence video_id mismatch for {video_id}.")

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
