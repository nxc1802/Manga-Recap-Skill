"""Compile CLI command handler delegating to core workflow."""

import argparse
from typing import Any, Dict

from src.cli.exit_codes import CliValidationError
from src.core.execution_policy import enforce_execution_policy
from src.core.workflow import compile_project_chapters


def handle_compile(args: argparse.Namespace) -> Dict[str, Any]:
    """Handle ``manga-recap compile`` by delegating to chapter compilation."""
    enforce_execution_policy("compile", args)

    project_slug = getattr(args, "project", "")
    if not project_slug:
        raise CliValidationError("Project slug is required.")

    return compile_project_chapters(
        project_slug,
        chapters=getattr(args, "chapters", None),
        output=getattr(args, "output", None),
        extract_audio=bool(getattr(args, "extract_audio", True)),
    )
