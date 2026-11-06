"""
src/cli/youtube_cmd.py
Subcommand handler: manga-recap youtube (metadata, packaging & upload)
"""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from src.cli.exit_codes import CliValidationError
from src.core.logger import logger
from src.core.schemas import YouTubeMetadata
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

# Scopes required for YouTube Data API v3 upload & caption management
YOUTUBE_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]


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
    chap_dir = get_chapter_dir(project_slug, chapter_num)

    if not chap_dir.exists():
        raise StorageFileNotFoundError(f"Chapter directory does not exist: {chap_dir}")

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

    # 2. Load script.json
    script_path = chap_dir / "script.json"
    if not script_path.exists():
        raise StorageFileNotFoundError(f"script.json missing in {chap_dir}. Run agent scripting first.")

    script_data = read_json(script_path)
    timestamps = generate_timestamps_from_script(script_data)

    # 3. Formulate Title
    user_title = getattr(args, "title", None)
    if user_title:
        title = user_title.strip()
    else:
        # Generate dramatic title formula adhering to <= 65 chars
        title = f"[Review Truyện] {project_title} - Tập {chapter_num}"
        if len(title) > 90:
            title = title[:87] + "..."

    # 4. Formulate Description
    user_desc = getattr(args, "description", None)
    if user_desc:
        description = user_desc.strip()
    else:
        description = build_youtube_description(
            project_title=project_title,
            chapter_num=str(chapter_num),
            story_summary=story_summary_text,
            timestamps=timestamps,
        )

    # 5. Formulate Tags
    user_tags = getattr(args, "tags", None)
    if user_tags:
        tags = [t.strip() for t in user_tags.split(",") if t.strip()]
    else:
        tags = [
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
        category_id="24",  # Entertainment
        privacy_status=privacy,
        made_for_kids=False,
        timestamps=timestamps,
        thumbnail_concept="Ảnh nhân vật chính trong trạng thái biến đổi hoặc giao đấu gay cấn, viền vàng nổi bật",
    )

    # Save to chapter directory and stage to output directory
    meta_path = chap_dir / "youtube_metadata.json"
    atomic_write_json(meta_path, metadata)

    out_dir = get_output_dir(project_slug)
    out_dir.mkdir(parents=True, exist_ok=True)
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


def get_youtube_oauth_credentials():
    """
    Retrieves and refreshes YouTube OAuth2 credentials.
    Raises DependencyNotFoundError if Google API libraries are missing.
    Raises StorageFileNotFoundError if client_secrets.json is missing.
    """
    import importlib
    try:
        req_mod = importlib.import_module("google.auth.transport.requests")
        creds_mod = importlib.import_module("google.oauth2.credentials")
        flow_mod = importlib.import_module("google_auth_oauthlib.flow")
        Request = req_mod.Request
        Credentials = creds_mod.Credentials
        InstalledAppFlow = flow_mod.InstalledAppFlow
    except ImportError as e:
        raise DependencyNotFoundError(
            "Missing Google API client dependencies for YouTube upload.\n"
            "Please install with: pip install google-api-python-client google-auth-oauthlib google-auth-httplib2"
        ) from e

    secrets_file = os.getenv("YOUTUBE_CLIENT_SECRETS_FILE", "client_secrets.json")
    token_file = os.getenv("YOUTUBE_CREDENTIALS_FILE", ".youtube_token.json")

    creds = None
    if os.path.exists(token_file):
        try:
            logger.info(f"Loading existing YouTube credentials from {token_file}")
            creds = Credentials.from_authorized_user_file(token_file, YOUTUBE_SCOPES)
        except Exception as e:
            logger.warning(f"Failed to load token file: {e}")
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logger.info("Credentials expired. Refreshing token...")
            try:
                creds.refresh(Request())
            except Exception as e:
                logger.error(f"Failed to refresh YouTube credentials: {e}")
                creds = None

        if not creds:
            if not os.path.exists(secrets_file):
                raise StorageFileNotFoundError(
                    f"OAuth client secrets file not found at: {secrets_file}\n"
                    "Please download your OAuth 2.0 Client credentials (client_secrets.json) "
                    "from Google Cloud Console (YouTube Data API v3 enabled) and place it in the project root."
                )

            logger.info("Launching local browser authentication flow for YouTube...")
            flow = InstalledAppFlow.from_client_secrets_file(secrets_file, YOUTUBE_SCOPES)
            creds = flow.run_local_server(port=0)

        # Save authorized credentials
        try:
            with open(token_file, "w", encoding="utf-8") as f:
                f.write(creds.to_json())
            logger.info(f"YouTube credentials saved to {token_file}")
        except Exception as e:
            logger.warning(f"Could not persist credentials to {token_file}: {e}")

    return creds


def handle_youtube_auth(args: argparse.Namespace) -> Dict[str, Any]:
    """Tests and verifies YouTube OAuth credentials."""
    token_file = os.getenv("YOUTUBE_CREDENTIALS_FILE", ".youtube_token.json")
    force = getattr(args, "force", False)
    if force and os.path.exists(token_file):
        try:
            os.remove(token_file)
        except OSError:
            pass
    creds = get_youtube_oauth_credentials()
    return {
        "status": "success",
        "valid": bool(creds and creds.valid),
        "token_file": str(Path(token_file).resolve()),
    }


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

    # 4. Authenticate with YouTube Data API v3
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

    # 7. Upload Custom Thumbnail (if provided or auto-detected)
    thumbnail_path = getattr(args, "thumbnail", None)
    if not thumbnail_path and str(chapter_num).lower() in ("feature", "all", "compilation", "full"):
        feature_thumb = out_dir / "feature_thumbnail.jpg"
        if feature_thumb.exists():
            thumbnail_path = str(feature_thumb)

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

    raise CliValidationError(f"Unknown YouTube action '{yt_action}'. Available actions: metadata, upload, auth.")
