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
    load_chapter_metadata,
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
    approved = getattr(args, "approved", False)
    video_mode = getattr(args, "video_mode", "auto")
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

    # Enforce Human Review Gates: before final 1080p render, ensure Gate 1 (Voice) and Gate 2 (Preview) are satisfied
    if quality == "final":
        if not approved:
            # Gate 1: Voice Selection Approval
            voice_sel_path = project_dir / "voice_selection.json"
            if not voice_sel_path.exists():
                chap_voice_sel = chap_dir / "voice_selection.json"
                if chap_voice_sel.exists():
                    voice_sel_path = chap_voice_sel

            if not voice_sel_path.exists():
                raise CliValidationError(
                    "Human Review Gate 1 not satisfied: 'voice_selection.json' not found. "
                    "Voice strategy must be approved before final render (or pass --approved)."
                )

            import json
            from src.core.schemas import PreviewApproval, VoiceSelectionApproval
            from src.tts.catalog import is_valid_voice_id

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

            if approval.project_slug and approval.project_slug.strip() not in (project_slug, "default_project", "manga_recap_project"):
                raise CliValidationError(
                    f"Human Review Gate 1 project mismatch: 'voice_selection.json' is for project '{approval.project_slug}', "
                    f"but current render target is '{project_slug}'."
                )

            if not is_valid_voice_id(approval.primary_voice):
                raise CliValidationError(
                    f"Human Review Gate 1 invalid: approved voice '{approval.primary_voice}' is not recognized in official catalog."
                )

            # Gate 2: Preview Video & Approval
            video_dir = chap_dir / "video"
            has_preview_video = (
                (video_dir / "preview.mp4").exists()
                or (video_dir / f"chapter_{chapter_num}_preview.mp4").exists()
                or (any(video_dir.glob("*preview*.mp4")) if video_dir.exists() else False)
            )
            if not has_preview_video:
                raise CliValidationError(
                    "Human Review Gate 2 not satisfied: preview video ('preview.mp4') not found. "
                    "A preview render must be generated before final render (or pass --approved)."
                )

            prev_sel_path = project_dir / "preview_approval.json"
            if not prev_sel_path.exists():
                chap_prev_sel = chap_dir / "preview_approval.json"
                if chap_prev_sel.exists():
                    prev_sel_path = chap_prev_sel

            if not prev_sel_path.exists():
                raise CliValidationError(
                    "Human Review Gate 2 not satisfied: 'preview_approval.json' not found. "
                    "Preview video must be approved before final render (or pass --approved)."
                )

            try:
                raw_prev = json.loads(prev_sel_path.read_text(encoding="utf-8"))
                prev_approval = PreviewApproval.model_validate(raw_prev)
            except Exception as err:
                raise CliValidationError(
                    f"Human Review Gate 2 schema validation failed for '{prev_sel_path}': {err}."
                ) from err

            if not prev_approval.is_approved():
                raise CliValidationError(
                    f"Human Review Gate 2 rejected: '{prev_sel_path}' is not approved. "
                    "A valid reviewer identifier is required."
                )

            if prev_approval.project_slug and prev_approval.project_slug.strip() not in (project_slug, "default_project", "manga_recap_project"):
                raise CliValidationError(
                    f"Human Review Gate 2 project mismatch: 'preview_approval.json' is for project '{prev_approval.project_slug}', "
                    f"but current render target is '{project_slug}'."
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
        video_mode=video_mode,
        allow_animation=allow_animation,
        allow_placeholders=allow_placeholders,
    )

    # Auto-mark StageType.FINALIZE upon final render completion if not already marked
    if quality == "final":
        try:
            chap_meta = load_chapter_metadata(chap_dir)
            fin_cp = chap_meta.stages.get(StageType.FINALIZE)
            if not fin_cp or fin_cp.status != CheckpointStatus.COMPLETED:
                update_checkpoint(
                    chap_dir,
                    StageType.FINALIZE,
                    CheckpointStatus.COMPLETED,
                    artifacts=[str(render_result["video_path"])] if isinstance(render_result.get("video_path"), (str, Path)) else [],
                )
        except Exception:
            pass

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
