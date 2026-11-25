"""Crawl CLI command handler delegating to core workflow."""

import argparse
from typing import Any, Dict

from src.cli.exit_codes import CliValidationError
from src.core.workflow import crawl_chapter_assets


def handle_crawl(args: argparse.Namespace) -> Dict[str, Any]:
    """Handle ``manga-recap crawl`` by delegating to chapter ingestion."""
    project_slug = getattr(args, "project", "")
    chapter_num = getattr(args, "chapter", None)
    if not project_slug:
        raise CliValidationError("Project slug is required.")
    if chapter_num is None:
        raise CliValidationError("Chapter number is required.")

    return crawl_chapter_assets(
        project_slug,
        str(chapter_num),
        url=getattr(args, "url", None),
        import_dir=getattr(args, "import_dir", None),
        max_panels=getattr(args, "max_panels", None),
        verify=bool(getattr(args, "verify", False)),
    )
