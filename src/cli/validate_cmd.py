"""Validate CLI command handler delegating to core workflow."""

import argparse
from typing import Any, Dict

from src.cli.exit_codes import CliValidationError
from src.core.workflow import validate_project_or_chapter


def handle_validate(args: argparse.Namespace) -> Dict[str, Any]:
    """Handle ``manga-recap validate`` by delegating to domain validation."""
    project_slug = getattr(args, "project", "")
    if not project_slug:
        raise CliValidationError("Project slug is required.")
    chapter_arg = getattr(args, "chapter", None)
    return validate_project_or_chapter(
        project_slug,
        chapter=str(chapter_arg) if chapter_arg is not None else None,
        strict=bool(getattr(args, "strict", False)),
        pacing=bool(getattr(args, "pacing", False)),
    )
