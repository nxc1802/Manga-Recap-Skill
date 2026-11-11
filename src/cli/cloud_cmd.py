"""
src/cli/cloud_cmd.py
CLI handler for 'manga-recap cloud' subcommands (Kaggle Cloud execution).
"""

from __future__ import annotations

import argparse
from typing import Any, Dict

from src.cli.exit_codes import ExitCode
from src.cloud.kaggle_service import KaggleCloudService
from src.core.logger import logger


def handle_cloud(args: argparse.Namespace) -> Dict[str, Any]:
    """Handles manga-recap cloud commands."""
    from src.core.execution_policy import enforce_execution_policy
    enforce_execution_policy("cloud", args)
    subaction = getattr(args, "cloud_action", "run")

    service = KaggleCloudService()
    if not service.is_authenticated:
        return {
            "status": "error",
            "command": "cloud",
            "exit_code": ExitCode.DEPENDENCY_ERROR,
            "error": "Kaggle API authentication failed. Ensure KAGGLE_API_TOKEN is configured in ~/.kaggle/access_token or environment.",
        }

    if subaction == "info":
        return {
            "status": "success",
            "command": "cloud",
            "exit_code": ExitCode.SUCCESS,
            "data": {
                "provider": "kaggle",
                "authenticated": True,
                "username": service.get_username(),
            },
        }

    if subaction == "run":
        project_slug = getattr(args, "project", None)
        chapter_number = getattr(args, "chapter", None)
        if not project_slug or not chapter_number:
            return {
                "status": "error",
                "command": "cloud",
                "exit_code": ExitCode.VALIDATION_ERROR,
                "error": "--project and --chapter are required for 'cloud run'",
            }

        from src.core.series_setup import require_series_setup
        require_series_setup(project_slug)

        gpu = not getattr(args, "no_gpu", False)
        logger.info(f"Dispatching cloud production job for {project_slug} ch.{chapter_number} (GPU={gpu})...")

        try:
            res = service.dispatch_chapter_job(
                project_slug=project_slug,
                chapter_number=str(chapter_number),
                gpu=gpu,
                wait=True,
            )
            return {
                "status": "success",
                "command": "cloud",
                "exit_code": ExitCode.SUCCESS,
                "data": res,
            }
        except Exception as e:
            logger.error(f"Cloud execution failed: {e}")
            return {
                "status": "error",
                "command": "cloud",
                "exit_code": ExitCode.GENERAL_ERROR,
                "error": str(e),
            }

    if subaction in ("produce-series", "produce_series"):
        project_slug = getattr(args, "project", None)
        chapters_arg = getattr(args, "chapters", None) or getattr(args, "chapter", None)
        if not project_slug or not chapters_arg:
            return {
                "status": "error",
                "command": "cloud produce-series",
                "exit_code": ExitCode.VALIDATION_ERROR,
                "error": "--project and --chapters are required for 'cloud produce-series'",
            }

        try:
            from src.core.storage import get_project_dir
            proj_dir = get_project_dir(project_slug)
        except Exception:
            proj_dir = None

        try:
            from src.core.series_setup import require_series_setup
            require_series_setup(project_slug, project_dir=proj_dir, allow_legacy=False)
        except Exception as ve:
            return {
                "status": "error",
                "command": "cloud produce-series",
                "exit_code": ExitCode.VALIDATION_ERROR,
                "error": f"Strict Form Gate: {ve}",
            }

        from src.cli.batch_cmd import parse_chapter_list
        if isinstance(chapters_arg, list):
            chapters = [str(c) for c in chapters_arg]
        else:
            chapters = parse_chapter_list(str(chapters_arg))

        if not chapters:
            return {
                "status": "error",
                "command": "cloud produce-series",
                "exit_code": ExitCode.VALIDATION_ERROR,
                "error": "No valid chapters specified for 'cloud produce-series'",
            }

        skip_preview = getattr(args, "skip_preview", False)
        # Preview approval MUST be verified; missing service support or verification
        # errors are blockers, not debug-only notices.
        try:
            ok, preview_err = service.check_chapter_1_preview_approval(
                project_slug, skip_preview=False
            )
        except Exception as pe:
            ok, preview_err = False, f"Could not verify Chapter 1 preview approval: {pe}"
        if not ok:
            return {
                "status": "error",
                "command": "cloud produce-series",
                "exit_code": ExitCode.VALIDATION_ERROR,
                "error": preview_err or "Chapter 1 preview has not been approved.",
            }

        gpu = not getattr(args, "no_gpu", False)
        compile_feature = bool(getattr(args, "compile", False) or getattr(args, "compile_feature", False))
        render_workers = int(getattr(args, "render_workers", 4))
        video_mode = getattr(args, "video_mode", "auto")
        burn_subtitles = getattr(args, "burn_subtitles", False)
        fps = int(getattr(args, "fps", 30))

        logger.info(f"Dispatching cloud series production for {project_slug} chapters={chapters} (GPU={gpu})...")
        try:
            res = service.dispatch_series_job(
                project_slug=project_slug,
                chapters=chapters,
                gpu=gpu,
                wait=True,
                skip_preview=skip_preview,
                compile_feature=compile_feature,
                render_workers=render_workers,
                video_mode=video_mode,
                burn_subtitles=burn_subtitles,
                fps=fps,
            )
            return {
                "status": "success",
                "command": "cloud produce-series",
                "exit_code": ExitCode.SUCCESS,
                "data": res,
            }
        except ValueError as ve:
            logger.error(f"Cloud series validation error: {ve}")
            return {
                "status": "error",
                "command": "cloud produce-series",
                "exit_code": ExitCode.VALIDATION_ERROR,
                "error": str(ve),
            }
        except Exception as e:
            logger.error(f"Cloud series execution failed: {e}")
            return {
                "status": "error",
                "command": "cloud produce-series",
                "exit_code": ExitCode.GENERAL_ERROR,
                "error": str(e),
            }

    return {
        "status": "error",
        "command": "cloud",
        "exit_code": ExitCode.VALIDATION_ERROR,
        "error": f"Unknown cloud action: {subaction}",
    }
