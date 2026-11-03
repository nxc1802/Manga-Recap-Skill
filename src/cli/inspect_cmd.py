"""
src/cli/inspect_cmd.py
Subcommand handler: manga-recap inspect
"""

from __future__ import annotations

import argparse
import os
import wave
from pathlib import Path
from typing import Any, Dict, List

from src.core.storage import (
    StorageFileNotFoundError,
    get_chapter_dir,
    get_project_dir,
    read_json,
)
from src.core.utils import get_project_data_dir


def handle_inspect(args: argparse.Namespace) -> Dict[str, Any]:
    """Inspects project or chapter assets, metrics, and checkpoints."""
    project_slug = getattr(args, "project", "")
    if not project_slug:
        raise StorageFileNotFoundError("Project slug is required.")

    # Resolve project directory (canonical or existing fallback)
    project_dir = get_project_dir(project_slug)
    if not project_dir.exists():
        fallback_dir = get_project_data_dir(project_slug)
        if fallback_dir.exists():
            project_dir = fallback_dir
        else:
            raise StorageFileNotFoundError(f"Project '{project_slug}' directory not found at {project_dir}")

    chapter_num = getattr(args, "chapter", None)

    # 1. Project-level inspection
    if chapter_num is None:
        project_json_path = project_dir / "project.json"
        project_cfg: Dict[str, Any] = {}
        if project_json_path.exists():
            try:
                project_cfg = read_json(project_json_path)
            except Exception:
                pass

        characters_path = project_dir / "characters.json"
        char_count = 0
        if characters_path.exists():
            try:
                char_data = read_json(characters_path)
                char_count = len(char_data.get("characters", {}))
            except Exception:
                pass

        chapters_dir = project_dir / "chapters"
        chapter_summaries: List[Dict[str, Any]] = []
        if chapters_dir.exists():
            for entry in sorted(os.listdir(chapters_dir)):
                chap_subdir = chapters_dir / entry
                if chap_subdir.is_dir():
                    chap_json = chap_subdir / "chapter.json"
                    status = "unknown"
                    if chap_json.exists():
                        try:
                            c_data = read_json(chap_json)
                            status = c_data.get("overall_status", c_data.get("status", "pending"))
                        except Exception:
                            pass
                    has_video = (chap_subdir / "video").exists() and any(
                        (chap_subdir / "video").glob("*.mp4")
                    )
                    chapter_summaries.append({
                        "chapter_number": entry,
                        "status": status,
                        "has_video": has_video,
                    })

        return {
            "project_slug": project_slug,
            "title": project_cfg.get("title", project_slug),
            "language": project_cfg.get("language", "vi"),
            "total_chapters": len(chapter_summaries),
            "chapters": chapter_summaries,
            "characters_count": char_count,
            "has_story_summary": (project_dir / "story_summary.md").exists(),
        }

    # 2. Chapter-level inspection
    chap_dir = get_chapter_dir(project_slug, chapter_num)
    if not chap_dir.exists():
        fallback_chap = project_dir / "chapters" / str(chapter_num)
        if fallback_chap.exists():
            chap_dir = fallback_chap
        else:
            raise StorageFileNotFoundError(
                f"Chapter '{chapter_num}' directory not found at {chap_dir}"
            )

    chap_json_path = chap_dir / "chapter.json"
    chap_meta: Dict[str, Any] = {}
    if chap_json_path.exists():
        try:
            chap_meta = read_json(chap_json_path)
        except Exception:
            pass

    # Inspect raw images
    raw_dir = chap_dir / "raw"
    raw_count = len([f for f in raw_dir.glob("*") if f.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}]) if raw_dir.exists() else 0

    # Inspect panels and manifest
    panels_dir = chap_dir / "panels"
    panels_count = len([f for f in panels_dir.glob("*") if f.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}]) if panels_dir.exists() else 0

    manifest_path = chap_dir / "manifest.json"
    contact_sheets_count = 0
    if manifest_path.exists():
        try:
            m_data = read_json(manifest_path)
            contact_sheets_count = len(m_data.get("contact_sheets", []))
            if panels_count == 0:
                panels_count = len(m_data.get("panels", []))
        except Exception:
            pass

    contact_sheets_dir = chap_dir / "contact_sheets"
    if contact_sheets_dir.exists() and contact_sheets_count == 0:
        contact_sheets_count = len([f for f in contact_sheets_dir.glob("*") if f.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}])

    # Inspect audio and durations
    audio_clips_dir = chap_dir / "audio" / "clips"
    if not audio_clips_dir.exists():
        audio_clips_dir = chap_dir / "audio"

    audio_count = 0
    total_audio_seconds = 0.0
    if audio_clips_dir.exists():
        wav_files = list(audio_clips_dir.glob("*.wav"))
        audio_count = len(wav_files)
        for wav_path in wav_files:
            try:
                with wave.open(str(wav_path), "rb") as wf:
                    frames = wf.getnframes()
                    rate = wf.getframerate()
                    if rate > 0:
                        total_audio_seconds += frames / float(rate)
            except Exception:
                pass

    # Inspect script.json
    script_path = chap_dir / "script.json"
    script_segments_count = 0
    if script_path.exists():
        try:
            s_data = read_json(script_path)
            script_segments_count = len(s_data.get("segments", []))
        except Exception:
            pass

    # Inspect video
    video_dir = chap_dir / "video"
    video_files = []
    if video_dir.exists():
        for vf in sorted(video_dir.glob("*.mp4")):
            video_files.append({
                "filename": vf.name,
                "size_bytes": vf.stat().st_size,
            })

    checkpoints_raw = chap_meta.get("stages", {})
    checkpoints = {}
    for stage_name, s_info in checkpoints_raw.items():
        if isinstance(s_info, dict):
            checkpoints[stage_name] = s_info.get("status", "unknown")
        else:
            checkpoints[stage_name] = str(s_info)

    return {
        "project_slug": project_slug,
        "chapter_number": str(chapter_num),
        "title": chap_meta.get("title", f"Chapter {chapter_num}"),
        "overall_status": chap_meta.get("overall_status", chap_meta.get("status", "pending")),
        "current_stage": chap_meta.get("current_stage", "crawl"),
        "assets": {
            "raw_images_count": raw_count,
            "panels_count": panels_count,
            "contact_sheets_count": contact_sheets_count,
            "audio_clips_count": audio_count,
            "total_audio_duration_seconds": round(total_audio_seconds, 2),
            "script_segments_count": script_segments_count,
            "video_files": video_files,
        },
        "checkpoints": checkpoints,
    }
