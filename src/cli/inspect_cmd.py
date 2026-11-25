"""Inspect CLI command handler delegating to core workflow."""

import argparse
from typing import Any, Dict

from src.cli.exit_codes import CliValidationError
from src.core.workflow import inspect_project_or_chapter


def handle_inspect(args: argparse.Namespace) -> Dict[str, Any]:
    """Handle ``manga-recap inspect`` by delegating to domain inspection."""
    project_slug = getattr(args, "project", "")
    if not project_slug:
        raise CliValidationError("Project slug is required.")
    chapter_num = getattr(args, "chapter", None)
    return inspect_project_or_chapter(
        project_slug,
        str(chapter_num) if chapter_num is not None else None,
    )
