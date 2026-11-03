"""
src/cli/render_cmd.py
Subcommand handler: manga-recap render
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path
from typing import Any, Dict

from src.cli.exit_codes import CliValidationError
from src.core.schemas import CheckpointStatus, StageType
from src.core.storage import (
    StorageFileNotFoundError,
    cleanup_intermediate_files,
    get_chapter_dir,
    get_project_dir,
    update_checkpoint,
)
from src.core.utils import (
    DependencyNotFoundError,
    get_project_data_dir,
    resolve_video_output_path,
)
from src.render.video_engine import VideoEngine


def handle_render(args: argparse.Namespace) -> Dict[str, Any]:
    """Renders panel video clips, mixes BGM, burns subtitles, and outputs MP4."""
    # Check binary dependencies first
    if not shutil.which("ffmpeg"):
        raise DependencyNotFoundError("FFmpeg executable not found on system PATH. Please install FFmpeg.")
    if not shutil.which("ffprobe"):
        raise DependencyNotFoundError("FFprobe executable not found on system PATH. Please install FFprobe.")

    project_slug = getattr(args, "project", "")
    chapter_num = getattr(args, "chapter", None)
    quality = getattr(args, "quality", "final")
    suffix = getattr(args, "suffix", "final")
    keep_temp = getattr(args, "keep_temp", False)
    bgm_path = getattr(args, "bgm", None)
    burn_subs = getattr(args, "burn_subtitles", True)
    allow_animation = getattr(args, "animation", True)
    force = getattr(args, "force", False)
    skip_review = getattr(args, "skip_review", False)
    allow_placeholders = getattr(args, "allow_placeholders", False)

    if not project_slug:
        raise CliValidationError("Project slug (--project) is required.")
    if chapter_num is None:
        raise CliValidationError("Chapter number (--chapter) is required.")

    project_dir = get_project_dir(project_slug)
    if not project_dir.exists():
        fallback_dir = get_project_data_dir(project_slug)
        if fallback_dir.exists():
            project_dir = fallback_dir
        else:
            raise StorageFileNotFoundError(
                f"Project '{project_slug}' directory not found at {project_dir}"
            )

    chap_dir = get_chapter_dir(project_slug, chapter_num)
    if not chap_dir.exists():
        fallback_chap = project_dir / "chapters" / str(chapter_num)
        if fallback_chap.exists():
            chap_dir = fallback_chap
        else:
            raise StorageFileNotFoundError(
                f"Chapter '{chapter_num}' directory not found at {chap_dir}"
            )

    script_path = chap_dir / "script.json"
    if not script_path.exists():
        raise StorageFileNotFoundError(
            f"script.json not found in chapter directory: {chap_dir}"
        )

    # Human Review Gate 1 verification: before final 1080p render, ensure voice_selection.json exists and is approved
    if quality == "final" and not skip_review:
        voice_sel_path = project_dir / "voice_selection.json"
        if not voice_sel_path.exists():
            chap_voice_sel = chap_dir / "voice_selection.json"
            if chap_voice_sel.exists():
                voice_sel_path = chap_voice_sel

        if not voice_sel_path.exists():
            raise CliValidationError(
                "Human Review Gate 1 not satisfied: 'voice_selection.json' not found. "
                "Voice strategy must be approved before final render, or specify --skip-review."
            )

        import json
        from src.core.schemas import VoiceSelectionApproval
        from src.tts.catalog import CATALOG_VOICES, resolve_voice_id

        try:
            raw_sel = json.loads(voice_sel_path.read_text(encoding="utf-8"))
            approval = VoiceSelectionApproval.model_validate(raw_sel)
        except Exception as err:
            raise CliValidationError(
                f"Human Review Gate 1 schema validation failed for '{voice_sel_path}': {err}."
            ) from err

        if not approval.is_approved():
            raise CliValidationError(
                f"Human Review Gate 1 rejected: '{voice_sel_path}' is not approved. "
                "A valid reviewer identifier ('reviewer' or 'approved_by') and primary voice are required."
            )

        resolved_v = resolve_voice_id(approval.primary_voice)
        if resolved_v not in CATALOG_VOICES:
            raise CliValidationError(
                f"Human Review Gate 1 invalid: approved voice '{approval.primary_voice}' is not recognized in official catalog."
            )

    # Execute full Video Engine V2 render pipeline
    engine = VideoEngine()
    render_result = engine.render_chapter(
        project_dir=project_dir,
        chapter_num=chapter_num,
        quality=quality,
        bgm_path=bgm_path,
        burn_subtitles=burn_subs,
        keep_temp=keep_temp,
        force=force,
        suffix=suffix,
        allow_animation=allow_animation,
        allow_placeholders=allow_placeholders,
    )

    # Clean intermediate files if keep_temp is False
    cleaned_files = cleanup_intermediate_files(chap_dir, keep_temp=keep_temp)

    return {
        "project_slug": project_slug,
        "chapter_number": str(chapter_num),
        "video_path": render_result["video_path"],
        "subtitle_path": render_result.get("subtitle_path"),
        "quality": quality,
        "suffix": suffix,
        "resolution": render_result.get("resolution", "1280x720"),
        "fps": render_result.get("fps", 30.0),
        "video_duration_seconds": render_result.get("video_duration_seconds", 0.0),
        "audio_duration_seconds": render_result.get("audio_duration_seconds", 0.0),
        "duration_diff_seconds": render_result.get("duration_diff_seconds", 0.0),
        "validation": render_result.get("validation", {}),
        "cleaned_files_count": len(cleaned_files) + render_result.get("cleaned_files_count", 0),
    }
