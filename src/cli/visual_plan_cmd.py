"""Visual-plan CLI command handler delegating to core workflow."""

import argparse
from typing import Any, Dict

from src.cli.exit_codes import CliValidationError
from src.core.execution_policy import enforce_execution_policy
from src.core.workflow import generate_chapter_visual_plan


def handle_visual_plan(args: argparse.Namespace) -> Dict[str, Any]:
    """Handle ``manga-recap visual-plan`` by delegating to visual plan generation."""
    enforce_execution_policy("visual-plan", args)

    project_slug = getattr(args, "project", "")
    chapter_num = getattr(args, "chapter", None)
    if not project_slug or chapter_num is None:
        raise CliValidationError("Both --project and --chapter are required for visual-plan.")

    return generate_chapter_visual_plan(
        project_slug,
        str(chapter_num),
        regenerate=bool(getattr(args, "regenerate", False)),
    )
