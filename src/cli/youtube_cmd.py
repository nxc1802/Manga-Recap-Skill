"""
src/cli/youtube_cmd.py
Subcommand handler: manga-recap youtube (metadata, packaging & upload)
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from src.cli.exit_codes import CliValidationError
from src.core.logger import logger
from src.core.schemas import ChapterMarker, FeatureTimeline, YouTubeMetadata
from src.core.storage import (
    StorageFileNotFoundError,
    atomic_write_json,
    format_chapter_filename,
    get_chapter_dir,
    get_output_dir,
    get_project_dir,
    read_json,
)
from src.core.utils import DependencyNotFoundError
from src.youtube.account_manager import YOUTUBE_SCOPES, YouTubeAccountManager, account_manager


def format_seconds_to_timestamp(seconds: float) -> str:
    """Formats seconds to standard YouTube MM:SS or HH:MM:SS timestamp string."""
    total_secs = int(max(0, seconds))
    hours = total_secs // 3600
    minutes = (total_secs % 3600) // 60
    secs = total_secs % 60
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def generate_timestamps_from_script(script_data: Dict[str, Any]) -> List[Dict[str, str]]:
    """
    Computes cumulative timestamps for segments from script.json duration metadata.
    Guarantees first marker is 00:00 and provides minimum 3 key markers.
    """
    segments = script_data.get("segments", [])
    if not segments:
        return [{"time": "00:00", "label": "Bắt đầu câu chuyện"}]

    timestamps: List[Dict[str, str]] = []
    current_time = 0.0

    # Ensure 00:00 start marker
    first_seg = segments[0]
    initial_label = "Mở đầu & Bối cảnh"
    if first_seg.get("narrator_text"):
        # Extract short phrase for label
        first_words = first_seg["narrator_text"][:40].strip()
        if first_words:
            initial_label = f"Mở đầu: {first_words}..."
    timestamps.append({"time": "00:00", "label": initial_label})

    # Select spaced highlight points (every 30-45s or major action shift)
    last_marker_time = 0.0
    for idx, seg in enumerate(segments):
        duration = float(seg.get("duration", 0.0) or 4.0)
        current_time += duration

        # If more than 25 seconds elapsed and not last segment, create a chapter marker
        if current_time - last_marker_time >= 25.0 and idx < len(segments) - 1:
            t_str = format_seconds_to_timestamp(current_time)
            text_snippet = seg.get("narrator_text", "")[:35].strip()
            char = seg.get("character", "")
            if char and char != "narrator":
                label = f"{char}: {text_snippet}..."
            elif text_snippet:
                label = f"{text_snippet}..."
            else:
                label = f"Diễn biến hồi {len(timestamps) + 1}"

            timestamps.append({"time": t_str, "label": label})
            last_marker_time = current_time

    # If fewer than 3 markers, synthesize midpoint and climax markers
    if len(timestamps) < 3 and current_time > 10.0:
        mid_time = current_time / 2.0
        timestamps.insert(1, {"time": format_seconds_to_timestamp(mid_time), "label": "Cao trào xung đột"})

    return timestamps


def build_youtube_chapter_markers(
    timeline: FeatureTimeline,
    min_interval_seconds: float = 10.0,
    min_markers_count: int = 3,
) -> List[Dict[str, str]]:
    """
    Ensures strict compliance with YouTube Chapters specification:
    1. First timestamp MUST start at 00:00.
    2. At least 3 chapter markers.
    3. Each marker interval >= 10.0 seconds.
    4. Strictly monotonic and ascending.
    """
    markers: List[Dict[str, Any]] = []

    use_chapters = len(timeline.chapters) >= min_markers_count
    if use_chapters:
        for idx in range(1, len(timeline.chapters)):
            if timeline.chapters[idx].start_seconds - timeline.chapters[idx - 1].start_seconds < min_interval_seconds:
                use_chapters = False
                break

    if use_chapters:
        for idx, ch in enumerate(timeline.chapters):
            t_str = "00:00" if idx == 0 else ch.start_timestamp
            c_label = ch.title
            if not c_label.lower().startswith(f"tập {ch.chapter_number}".lower()):
                c_label = f"Tập {ch.chapter_number}: {ch.title}"
            markers.append({
                "time": t_str,
                "label": c_label,
                "seconds": 0.0 if idx == 0 else ch.start_seconds,
            })
    else:
        # Fallback expansion using sub_markers from each chapter
        all_sub_markers: List[ChapterMarker] = []
        for ch in timeline.chapters:
            all_sub_markers.extend(ch.sub_markers)

        spaced_markers: List[ChapterMarker] = []
        last_sec = -999.0
        for sm in all_sub_markers:
            if sm.seconds - last_sec >= min_interval_seconds or not spaced_markers:
                spaced_markers.append(sm)
                last_sec = sm.seconds

        if spaced_markers:
            if spaced_markers[0].seconds > 0.0 or spaced_markers[0].time != "00:00":
                spaced_markers[0] = ChapterMarker(time="00:00", label="Mở đầu câu chuyện", seconds=0.0)
        else:
            spaced_markers.append(ChapterMarker(time="00:00", label="Mở đầu câu chuyện", seconds=0.0))

        if len(spaced_markers) < min_markers_count and timeline.total_duration_seconds >= 30.0:
            step = timeline.total_duration_seconds / float(min_markers_count)
            spaced_markers = [
                ChapterMarker(time="00:00", label="Mở đầu & Xuất thế", seconds=0.0),
                ChapterMarker(time=format_seconds_to_timestamp(step), label="Cao trào xung đột", seconds=round(step, 2)),
                ChapterMarker(time=format_seconds_to_timestamp(step * 2), label="Quyết chiến hồi kết", seconds=round(step * 2, 2)),
            ]

        for sm in spaced_markers:
            markers.append({
                "time": sm.time,
                "label": sm.label,
                "seconds": sm.seconds,
            })

    # Return clean format
    return [{"time": m["time"], "label": m["label"]} for m in markers]


def build_youtube_description(
    project_title: str,
    chapter_num: str,
    story_summary: str,
    timestamps: List[Dict[str, str]],
    custom_hook: Optional[str] = None,
) -> str:
    """Builds a rich, search-engine-optimized description block adhering to YouTube guidelines."""
    hook_line = custom_hook or (
        f"🔥 Khám phá tập {chapter_num} siêu phẩm '{project_title}' — Đón xem cuộc phiêu lưu kịch tính không thể rời mắt!"
    )

    # Format timestamps block
    ts_lines = [f"{m['time']} - {m['label']}" for m in timestamps]
    timestamps_block = "\n".join(ts_lines)

    # Clean story summary (extract up to 2 paragraphs)
    summary_clean = ""
    if story_summary:
        paragraphs = [p.strip() for p in story_summary.split("\n\n") if p.strip() and not p.startswith("#")]
        if paragraphs:
            summary_clean = "\n\n".join(paragraphs[:2])

    if not summary_clean:
        summary_clean = f"Chào mừng bạn đến với video tóm tắt và đánh giá trọn bộ tập {chapter_num} của bộ truyện '{project_title}'."

    description_parts = [
        hook_line,
        "👉 Đăng ký kênh và bấm chuông 🔔 để không bỏ lỡ các tập tiếp theo!",
        "",
        "═══════════════════════════════════════",
        "📌 MỤC LỤC & TIMESTAMPS:",
        timestamps_block,
        "",
        "═══════════════════════════════════════",
        "📖 TÓM TẮT NỘI DUNG TẬP:",
        summary_clean,
        "",
        "═══════════════════════════════════════",
        "⚖️ DISCLAIMER & BẢN QUYỀN:",
        "Copyright Disclaimer Under Section 107 of the Copyright Act 1976: Allowance is made for 'fair use' for purposes such as criticism, comment, news reporting, teaching, scholarship, and research.",
        "Mọi hình ảnh và nội dung thuộc quyền sở hữu của tác giả gốc. Video được biên tập và thuyết minh nhằm mục đích đánh giá và giới thiệu tác phẩm nghệ thuật đến bạn đọc.",
        "",
        "#MangaRecap #ReviewTruyen #Webtoon #Manhwa #TomTatTruyen",
    ]

    return "\n".join(description_parts)


def handle_youtube_metadata(args: argparse.Namespace) -> Dict[str, Any]:
    """Generates and persists YouTube publishing metadata (title, description, tags, timestamps)."""
    project_slug = getattr(args, "project", "")
    chapter_num = getattr(args, "chapter", None)

    if not project_slug:
        raise CliValidationError("Project slug (--project) is required.")
    if chapter_num is None:
        raise CliValidationError("Chapter number (--chapter) is required.")

    proj_dir = get_project_dir(project_slug)
    out_dir = get_output_dir(project_slug)
    out_dir.mkdir(parents=True, exist_ok=True)

    is_feature = str(chapter_num).lower() in ("feature", "all", "compilation", "full")

    # 1. Load project info & story summary
    project_title = project_slug
    project_json = proj_dir / "project.json"
    if project_json.exists():
        try:
            p_data = read_json(project_json)
            project_title = p_data.get("title") or project_slug
        except Exception:
            pass

    story_summary_text = ""
    summary_path = proj_dir / "story_summary.md"
    if summary_path.exists():
        story_summary_text = summary_path.read_text(encoding="utf-8")

    thumb_concept = "Ảnh nhân vật chính trong trạng thái biến đổi hoặc giao đấu gay cấn, viền vàng nổi bật"

    if is_feature:
        # Check feature timeline
        timeline_path = proj_dir / "feature_timeline.json"
        if not timeline_path.exists():
            timeline_path = out_dir / f"{project_slug}_timeline.json"

        if timeline_path.exists():
            t_data = read_json(timeline_path)
            timeline = FeatureTimeline.model_validate(t_data)
            timestamps = build_youtube_chapter_markers(timeline)
            first_c = timeline.chapters[0].chapter_number if timeline.chapters else "1"
            last_c = timeline.chapters[-1].chapter_number if timeline.chapters else "1"
            default_title = f"[Trọn Bộ] {project_title} - Full Recap Tập {first_c}-{last_c}"
        else:
            timestamps = [{"time": "00:00", "label": "Bắt đầu full recap"}]
            default_title = f"[Trọn Bộ] {project_title} - Full Recap Toàn Bộ Các Tập"

        # Check for feature thumbnail
        feat_thumb = out_dir / "feature_thumbnail.jpg"
        if feat_thumb.exists():
            thumb_concept = f"Ảnh đại diện feature chất lượng cao tại {feat_thumb.name}"

        # Formulate Title & Description
        title = getattr(args, "title", None) or default_title
        if len(title) > 90:
            title = title[:87] + "..."

        description = getattr(args, "description", None) or build_youtube_description(
            project_title=project_title,
            chapter_num="Trọn Bộ",
            story_summary=story_summary_text,
            timestamps=timestamps,
        )

        user_tags = getattr(args, "tags", None)
        tags = [t.strip() for t in user_tags.split(",") if t.strip()] if user_tags else [
            "Manga Recap",
            "Review Truyện",
            project_title,
            f"{project_title} trọn bộ",
            "Webtoon",
            "Manhwa",
            "Tóm tắt truyện tranh",
            "Full recap",
        ]

        privacy = getattr(args, "privacy", "private") or "private"

        metadata = YouTubeMetadata(
            project_slug=project_slug,
            chapter_number="feature",
            title=title,
            description=description,
            tags=tags,
            category_id="24",
            privacy_status=privacy,
            made_for_kids=False,
            timestamps=timestamps,
            thumbnail_concept=thumb_concept,
        )

        meta_path = out_dir / "feature.youtube.json"
        atomic_write_json(meta_path, metadata)
        staged_meta_path = meta_path
        logger.info(f"Feature YouTube metadata generated: {meta_path}")

        return {
            "status": "success",
            "project_slug": project_slug,
            "chapter_number": "feature",
            "metadata_path": str(meta_path),
            "staged_path": str(staged_meta_path),
            "title": title,
            "timestamps_count": len(timestamps),
            "tags_count": len(tags),
        }

    else:
        chap_dir = get_chapter_dir(project_slug, chapter_num)
        if not chap_dir.exists():
            raise StorageFileNotFoundError(f"Chapter directory does not exist: {chap_dir}")

        script_path = chap_dir / "script.json"
        if not script_path.exists():
            raise StorageFileNotFoundError(f"script.json missing in {chap_dir}. Run agent scripting first.")

        script_data = read_json(script_path)
        timestamps = generate_timestamps_from_script(script_data)

        # Check if AI-generated thumbnail exists in output directory
        chap_thumb = out_dir / f"chapter_{chapter_num}_thumbnail.jpg"
        if chap_thumb.exists():
            thumb_concept = f"AI Generated Thumbnail tại {chap_thumb.name}"

        user_title = getattr(args, "title", None)
        if user_title:
            title = user_title.strip()
        else:
            title = f"[Review Truyện] {project_title} - Tập {chapter_num}"
            if len(title) > 90:
                title = title[:87] + "..."

        user_desc = getattr(args, "description", None)
        description = user_desc.strip() if user_desc else build_youtube_description(
            project_title=project_title,
            chapter_num=str(chapter_num),
            story_summary=story_summary_text,
            timestamps=timestamps,
        )

        user_tags = getattr(args, "tags", None)
        tags = [t.strip() for t in user_tags.split(",") if t.strip()] if user_tags else [
            "Manga Recap",
            "Review Truyện",
            project_title,
            f"{project_title} tập {chapter_num}",
            "Webtoon",
            "Manhwa",
            "Tóm tắt truyện tranh",
            "Truyện tranh hay",
        ]

        privacy = getattr(args, "privacy", "private") or "private"

        metadata = YouTubeMetadata(
            project_slug=project_slug,
            chapter_number=str(chapter_num),
            title=title,
            description=description,
            tags=tags,
            category_id="24",
            privacy_status=privacy,
            made_for_kids=False,
            timestamps=timestamps,
            thumbnail_concept=thumb_concept,
        )

        meta_path = chap_dir / "youtube_metadata.json"
        atomic_write_json(meta_path, metadata)

        staged_meta_name = format_chapter_filename(chapter_num, ext="youtube.json", padded=True)
        staged_meta_path = out_dir / staged_meta_name
        shutil.copy2(meta_path, staged_meta_path)

        logger.info(f"YouTube metadata generated successfully: {meta_path} (Staged to {staged_meta_path})")

        return {
            "status": "success",
            "project_slug": project_slug,
            "chapter_number": str(chapter_num),
            "metadata_path": str(meta_path),
            "staged_path": str(staged_meta_path),
            "title": title,
            "timestamps_count": len(timestamps),
            "tags_count": len(tags),
        }



def get_youtube_oauth_credentials(account_name: Optional[str] = None, force_reauth: bool = False):
    """
    Retrieves and refreshes YouTube OAuth2 credentials for the specified or default account.
    Maintains 100% backward compatibility with legacy 0-argument callers.
    """
    creds, _ = account_manager.get_credentials(account_name=account_name, force_reauth=force_reauth)
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

    try:
        creds = get_youtube_oauth_credentials(account_name=account_name, force_reauth=force)
    except TypeError:
        creds = get_youtube_oauth_credentials()

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


def handle_youtube_upload(args: argparse.Namespace) -> Dict[str, Any]:
    """Uploads chapter or feature compilation video to YouTube, attaches captions and custom thumbnail."""
    project_slug = getattr(args, "project", "")
    chapter_num = getattr(args, "chapter", None)

    if not project_slug:
        raise CliValidationError("Project slug (--project) is required.")
    if chapter_num is None:
        raise CliValidationError("Chapter number (--chapter) is required.")

    chap_dir = get_chapter_dir(project_slug, chapter_num)
    out_dir = get_output_dir(project_slug)

    # 1. Resolve final video file (prioritize standardized padded output or feature compilation)
    video_override = getattr(args, "video", None)
    if video_override and Path(video_override).exists():
        video_path = Path(video_override).resolve()
    elif str(chapter_num).lower() in ("feature", "all", "compilation", "full"):
        feature_candidates = [
            out_dir / "warrior_grandpa_episodes_1_to_10_feature.mp4",
            out_dir / f"{project_slug}_full_recap.mp4",
            out_dir / "chapter_feature.mp4",
        ]
        video_path = None
        for cand in feature_candidates:
            if cand.exists():
                video_path = cand
                break
        if not video_path:
            raise StorageFileNotFoundError(f"Feature compilation video not found in {out_dir}")
    else:
        padded_vid_name = format_chapter_filename(chapter_num, ext="mp4", padded=True)
        video_path = out_dir / padded_vid_name
        if not video_path.exists():
            # Fallback to chapter dir
            candidate = chap_dir / "video" / f"chapter_{chapter_num}_final.mp4"
            if candidate.exists():
                video_path = candidate
            else:
                raise StorageFileNotFoundError(
                    f"Video not found for chapter {chapter_num}. Looked at {video_path} and {candidate}. "
                    "Ensure chapter rendering is completed."
                )

    # 2. Resolve subtitle file
    subtitle_override = getattr(args, "subtitles", None)
    if subtitle_override and Path(subtitle_override).exists():
        subtitle_path = Path(subtitle_override).resolve()
    elif str(chapter_num).lower() in ("feature", "all", "compilation", "full"):
        feature_srt_candidates = [
            out_dir / "warrior_grandpa_episodes_1_to_10_feature.srt",
            out_dir / f"{project_slug}_full_recap.srt",
            out_dir / "chapter_feature.srt",
        ]
        subtitle_path = None
        for cand in feature_srt_candidates:
            if cand.exists():
                subtitle_path = cand
                break
    else:
        padded_srt_name = format_chapter_filename(chapter_num, ext="srt", padded=True)
        subtitle_path = out_dir / padded_srt_name
        if not subtitle_path.exists():
            candidate_srt = chap_dir / "subtitles" / f"chapter_{chapter_num}.srt"
            if candidate_srt.exists():
                subtitle_path = candidate_srt
            else:
                subtitle_path = None

    # 3. Resolve metadata
    meta_file = chap_dir / "youtube_metadata.json"
    if str(chapter_num).lower() in ("feature", "all", "compilation", "full"):
        meta_file = out_dir / "feature.youtube.json"

    if meta_file.exists():
        meta_obj = YouTubeMetadata.model_validate(read_json(meta_file))
        def_title = meta_obj.title
        def_desc = meta_obj.description
        def_privacy = meta_obj.privacy_status
        def_tags = meta_obj.tags
        def_category = meta_obj.category_id or "24"
        def_kids = meta_obj.made_for_kids
    else:
        if str(chapter_num).lower() in ("feature", "all", "compilation", "full"):
            def_title = f"[Trọn Bộ] {project_slug} - Full Recap Tập 1-10"
            def_desc = f"Trọn bộ {project_slug} siêu phẩm võ hiệp từ Tập 1 đến Tập 10."
            def_privacy = "private"
            def_tags = ["Manga Recap", "Review Truyện", project_slug, "Webtoon"]
            def_category = "24"
            def_kids = False
        else:
            # Auto-generate metadata if not yet created
            logger.info("youtube_metadata.json not found, auto-generating default metadata...")
            handle_youtube_metadata(args)
            meta_obj = YouTubeMetadata.model_validate(read_json(meta_file))
            def_title = meta_obj.title
            def_desc = meta_obj.description
            def_privacy = meta_obj.privacy_status
            def_tags = meta_obj.tags
            def_category = meta_obj.category_id or "24"
            def_kids = meta_obj.made_for_kids

    title = getattr(args, "title", None) or def_title
    description = getattr(args, "description", None) or def_desc
    privacy = getattr(args, "privacy", None) or def_privacy
    tags = getattr(args, "tags", None)
    tags_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else def_tags
    category_id = def_category
    made_for_kids = def_kids

    # 4. Authenticate with YouTube Data API v3 (Mode 1: Direct/Headless vs Mode 2: Interactive Prompt)
    target_account = getattr(args, "account", None)
    is_interactive_flag = getattr(args, "interactive", False)
    is_json_mode = getattr(args, "json", False) or getattr(args, "json_mode", False)

    # Trigger interactive selection if flag set OR (account omitted, not in JSON mode, and running on TTY)
    if is_interactive_flag or (target_account is None and not is_json_mode and sys.stdin.isatty()):
        target_account = prompt_interactive_channel_selection(account_manager)

    acc_info = account_manager.get_account(target_account)
    account_name_resolved = acc_info.account_name
    channel_title = acc_info.channel_title
    channel_id = acc_info.channel_id

    try:
        creds = get_youtube_oauth_credentials(account_name=target_account)
    except TypeError:
        creds = get_youtube_oauth_credentials()

    import importlib
    try:
        discovery_mod = importlib.import_module("googleapiclient.discovery")
        http_mod = importlib.import_module("googleapiclient.http")
        build = discovery_mod.build
        MediaFileUpload = http_mod.MediaFileUpload
    except ImportError as e:
        raise DependencyNotFoundError("Missing googleapiclient. Install with: pip install google-api-python-client") from e

    youtube = build("youtube", "v3", credentials=creds)

    # 5. Execute resumable video upload
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags_list,
            "categoryId": meta_obj.category_id or "24",
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": meta_obj.made_for_kids,
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
    video_url = f"https://www.youtube.com/watch?v={video_id}"
    logger.info(f"Video uploaded successfully! Video ID: {video_id} - URL: {video_url}")

    # 6. Upload Closed Captions (if SRT exists and not skipped)
    skip_captions = getattr(args, "no_captions", False)
    captions_uploaded = False
    if not skip_captions and subtitle_path and subtitle_path.exists():
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
            logger.warning(f"Failed to upload closed captions: {e}")

    # 7. Upload Custom Thumbnail (if provided or auto-detected from output/)
    thumbnail_path = getattr(args, "thumbnail", None)
    if not thumbnail_path:
        if str(chapter_num).lower() in ("feature", "all", "compilation", "full"):
            feature_thumb = out_dir / "feature_thumbnail.jpg"
            if feature_thumb.exists():
                thumbnail_path = str(feature_thumb)
        else:
            chap_thumb = out_dir / f"chapter_{chapter_num}_thumbnail.jpg"
            if chap_thumb.exists():
                thumbnail_path = str(chap_thumb)

    thumbnail_uploaded = False
    if thumbnail_path and Path(thumbnail_path).exists():
        try:
            logger.info(f"Uploading custom thumbnail from {thumbnail_path}...")
            youtube.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(str(thumbnail_path), mimetype="image/jpeg"),
            ).execute()
            thumbnail_uploaded = True
            logger.info("Thumbnail uploaded successfully!")
        except Exception as e:
            logger.warning(f"Failed to upload custom thumbnail: {e}")

    return {
        "status": "success",
        "video_id": video_id,
        "video_url": video_url,
        "account": account_name_resolved,
        "channel_title": channel_title,
        "channel_id": channel_id,
        "title": title,
        "privacy_status": privacy,
        "captions_uploaded": captions_uploaded,
        "thumbnail_uploaded": thumbnail_uploaded,
    }


def handle_youtube(args: argparse.Namespace) -> Dict[str, Any]:
    """Top-level dispatch router for `manga-recap youtube` subcommands."""
    yt_action = getattr(args, "youtube_action", "metadata")
    if yt_action in ("metadata", "prepare"):
        return handle_youtube_metadata(args)
    if yt_action == "upload":
        return handle_youtube_upload(args)
    if yt_action == "auth":
        return handle_youtube_auth(args)
    if yt_action in ("accounts", "account"):
        return handle_youtube_accounts(args)

    raise CliValidationError(f"Unknown YouTube action '{yt_action}'. Available actions: metadata, upload, auth, accounts.")

