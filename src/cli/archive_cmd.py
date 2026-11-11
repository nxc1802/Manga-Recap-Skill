"""
src/cli/archive_cmd.py
Subcommand handler: manga-recap archive (push, verify, restore)
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Dict

from src.archive.hf_archive import (
    DEFAULT_HF_REPO_ID,
    archive_push,
    archive_restore,
    archive_verify,
)
from src.cli.exit_codes import CliValidationError


def handle_archive(args: argparse.Namespace) -> Dict[str, Any]:
    """Top-level dispatch router for `manga-recap archive` subcommands."""
    project_slug = getattr(args, "project", "")
    if not project_slug:
        raise CliValidationError("Project slug (--project) is required for archive commands.")

    action = getattr(args, "archive_action", None)
    repo_id = getattr(args, "repo_id", None) or DEFAULT_HF_REPO_ID
    token = getattr(args, "token", None)
    revision = getattr(args, "revision", None)
    target_dir_arg = getattr(args, "target_dir", None)
    target_dir = Path(target_dir_arg) if target_dir_arg else None

    if action == "push":
        return archive_push(
            project_slug=project_slug,
            repo_id=repo_id,
            token=token,
        )
    elif action == "verify":
        return archive_verify(
            project_slug=project_slug,
            repo_id=repo_id,
            token=token,
            revision=revision,
        )
    elif action == "restore":
        return archive_restore(
            project_slug=project_slug,
            revision=revision,
            repo_id=repo_id,
            token=token,
            target_dir=target_dir,
        )
    else:
        raise CliValidationError(
            f"Unknown archive action '{action}'. Available actions: push, verify, restore."
        )
