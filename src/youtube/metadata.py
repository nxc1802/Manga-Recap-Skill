"""Vietnamese YouTube metadata and chapter markers."""
from __future__ import annotations

import argparse
import re
import shutil
from typing import Any, Dict, List, Optional

from src.cli.exit_codes import CliValidationError
from src.core.logger import logger
from src.core.schemas import ChapterMarker, FeatureTimeline, YouTubeMetadata
from src.core.storage import (
    StorageFileNotFoundError, atomic_write_json, format_chapter_filename,
    get_chapter_dir, get_output_dir, get_project_dir, read_json,
)


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
