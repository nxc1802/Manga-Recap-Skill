"""Status CLI command handler delegating to core workflow."""

import argparse
from typing import Any, Dict

from src.cli.exit_codes import CliValidationError
from src.core.workflow import get_project_status


def handle_status(args: argparse.Namespace) -> Dict[str, Any]:
    """Handle ``manga-recap status`` by delegating to domain status reporting."""
    project_slug = getattr(args, "project", "")
    if not project_slug:
        raise CliValidationError("Project slug is required.")
    chapter_arg = getattr(args, "chapter", None)
    return get_project_status(
        project_slug,
        chapter=str(chapter_arg) if chapter_arg is not None else None,
    )
