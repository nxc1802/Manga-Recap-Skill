"""
src/cli/production_cmd.py
Subcommand handler: manga-recap produce
Executes single-session cloud production run (TTS + Render + Compile + Export Bundle)
with crash resilience, persistent VieNeu-TTS singleton reuse, and performance telemetry.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Dict

from src.cli.batch_cmd import discover_project_chapters, parse_chapter_list
from src.cli.exit_codes import CliValidationError
from src.core.storage import (
    StorageFileNotFoundError,
    get_project_dir,
)
from src.core.utils import get_project_data_dir
from src.production.runner import SingleSessionProductionRunner
from src.production.schemas import ProductionJobConfig


def handle_produce(args: argparse.Namespace) -> Dict[str, Any]:
    """
    Subcommand handler for 'manga-recap produce'.
    Runs a self-contained, single-session cloud production job across multiple chapters.
    """
    from src.core.execution_policy import enforce_execution_policy
    enforce_execution_policy("produce", args)
    project_slug = getattr(args, "project", "")
    if not project_slug:
        raise CliValidationError("Project slug (--project) is required for produce command.")

    project_dir = get_project_dir(project_slug)
    if not project_dir.exists():
        fallback_dir = get_project_data_dir(project_slug)
        if fallback_dir.exists():
            project_dir = fallback_dir
        else:
            raise StorageFileNotFoundError(
                f"Project directory not found for: '{project_slug}' at {project_dir}"
            )

    # Resolve target chapters
    chapters_arg = getattr(args, "chapters", None)
    if chapters_arg:
        target_chapters = parse_chapter_list(chapters_arg)
    else:
        target_chapters = discover_project_chapters(project_dir)

    if not target_chapters:
        raise CliValidationError(
            f"No chapters found to produce for project '{project_slug}'. "
            "Please specify chapters via --chapters or ingest chapters first."
        )

    from src.core.storage import read_json
    project_settings = read_json(project_dir / "project.json")
    render = project_settings["render_settings"]
    subtitle = render["subtitle"]
    def explicit_or_inherit(name, value):
        supplied = getattr(args, name, None)
        return value if supplied is None else supplied

    # Build config
    config = ProductionJobConfig(
        project_slug=project_slug,
        target_chapters=target_chapters,
        encoder=getattr(args, "encoder", "auto") or "auto",
        tts_engine=getattr(args, "tts_engine", "neural") or "neural",
        tts_workers=getattr(args, "tts_workers", 2) or 2,
        render_workers=getattr(args, "render_workers", 2) or 2,
        quality=getattr(args, "quality", "final") or "final",
        video_mode=explicit_or_inherit("video_mode", render["video_mode"]),
        burn_subtitles=explicit_or_inherit("burn_subtitles", render["burn_subtitles"]),
        animation=explicit_or_inherit("animation", render["render_pan_mode"] == "animation"),
        bgm_path=explicit_or_inherit("bgm", project_settings["audio_settings"]["bgm_path"]),
        compile=getattr(args, "compile", True) if getattr(args, "compile", None) is not None else True,
        export_bundle=getattr(args, "export_bundle", True) if getattr(args, "export_bundle", None) is not None else True,
        cleanup_intermediates=getattr(args, "cleanup", False),
        force=getattr(args, "force", False),
        voice_override=getattr(args, "voice", None),
        speed_override=getattr(args, "speed", None),
        subtitle_lines=explicit_or_inherit("subtitle_lines", subtitle["max_lines"]),
        subtitle_font_size=explicit_or_inherit("subtitle_font_size", subtitle["font_size"]),
        subtitle_chunking=explicit_or_inherit("subtitle_chunking", subtitle["chunking"]),
    )

    job_file_arg = getattr(args, "job_file", None)
    job_file_path = Path(job_file_arg).resolve() if job_file_arg else (project_dir / "production_job.json")

    runner = SingleSessionProductionRunner.load_or_create(
        project_slug=project_slug,
        config=config,
        job_file_path=job_file_path,
        force=config.force,
    )

    # Execute 4-stage cloud pipeline
    manifest = runner.run()

    completed_chaps = [
        c for c, p in manifest.chapters.items()
        if p.render_status.value == "completed"
    ]

    return {
        "job_id": manifest.job_id,
        "project_slug": manifest.project_slug,
        "status": manifest.status.value,
        "current_stage": manifest.current_stage,
        "resolved_encoder": manifest.telemetry.resolved_encoder,
        "encoder_status_message": manifest.telemetry.encoder_status_message,
        "target_chapters": manifest.config.target_chapters,
        "completed_chapters": completed_chaps,
        "compiled_video_path": manifest.compiled_video_path,
        "feature_timeline_path": manifest.feature_timeline_path,
        "master_audio_path": manifest.master_audio_path,
        "export_bundle_dir": manifest.export_bundle_dir,
        "artifacts_count": len(manifest.artifacts),
        "telemetry": manifest.telemetry.model_dump(),
        "error": manifest.error,
    }
