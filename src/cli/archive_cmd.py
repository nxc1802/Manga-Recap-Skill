"""
src/cli/archive_cmd.py
Subcommand handler: manga-recap archive (push, verify).
"""

from __future__ import annotations

import argparse
from typing import Any, Dict

from src.archive.hf_archive import (
    archive_push,
    archive_verify,
)
from src.cli.exit_codes import CliValidationError
from src.core.execution_policy import enforce_execution_policy


def handle_archive(args: argparse.Namespace) -> Dict[str, Any]:
    """Top-level dispatch router for `manga-recap archive` subcommands."""
    project_slug = getattr(args, "project", "")
    if not project_slug:
        raise CliValidationError("Project slug (--project) is required for archive commands.")

    action = getattr(args, "archive_action", None)
    repo_id = getattr(args, "repo_id", None)
    token = getattr(args, "token", None)
    revision = getattr(args, "revision", None)

    if action == "push":
        enforce_execution_policy("archive", args)
        return archive_push(
            project_slug=project_slug,
            repo_id=repo_id,
            token=token,
        )
    elif action == "verify":
        enforce_execution_policy("archive", args)
        return archive_verify(
            project_slug=project_slug,
            repo_id=repo_id,
            token=token,
            revision=revision,
        )
    else:
        raise CliValidationError(
            f"Unknown archive action '{action}'. Available actions: push, verify."
        )
