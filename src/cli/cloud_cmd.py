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

    return {
        "status": "error",
        "command": "cloud",
        "exit_code": ExitCode.VALIDATION_ERROR,
        "error": f"Unknown cloud action: {subaction}",
    }
