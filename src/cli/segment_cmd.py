"""Segmentation, repack, panel-inspect, and decision CLI command handlers."""

import argparse
from typing import Any, Dict

from src.cli.exit_codes import CliValidationError
from src.core.contact_sheet import ContactSheetGenerator
from src.core.execution_policy import enforce_execution_policy
from src.core.streaming_segmenter import StreamingSegmenter
from src.core.workflow import (
    decide_chapter_panels,
    inspect_flagged_panels,
    repack_chapter_panels,
    segment_chapter_assets,
)


def handle_segment(args: argparse.Namespace) -> Dict[str, Any]:
    """Handle ``manga-recap segment`` with injectable segmenter/sheet classes for tests."""
    enforce_execution_policy("segment", args)

    project_slug = getattr(args, "project", "")
    chapter_num = getattr(args, "chapter", None)
    if not project_slug or chapter_num is None:
        raise CliValidationError("Both --project and --chapter are required for segmentation.")

    return segment_chapter_assets(
        project_slug,
        str(chapter_num),
        target_width=getattr(args, "target_width", 1080),
        threshold_variance=getattr(args, "threshold_variance", 8.0),
        min_gutter_height=getattr(args, "min_gutter_height", 15),
        margin_padding=getattr(args, "margin_padding", 12),
        cell_size=getattr(args, "cell_size", 480),
        force=bool(getattr(args, "force", False)),
        segmenter_cls=StreamingSegmenter,
        contact_sheet_cls=ContactSheetGenerator,
    )


def handle_repack(args: argparse.Namespace) -> Dict[str, Any]:
    """Handle ``manga-recap repack`` by delegating to panel repack workflow."""
    enforce_execution_policy("repack", args)

    project_slug = getattr(args, "project", "")
    chapter_num = getattr(args, "chapter", None)
    if not project_slug or chapter_num is None:
        raise CliValidationError("Both --project and --chapter are required for repack.")

    return repack_chapter_panels(
        project_slug,
        str(chapter_num),
        plan_path=getattr(args, "plan", None),
        target_width=getattr(args, "target_width", 1080),
        cell_size=getattr(args, "cell_size", 480),
        grid_cols=getattr(args, "grid_cols", 4),
        grid_rows=getattr(args, "grid_rows", 4),
    )


def handle_panel_inspect(args: argparse.Namespace) -> Dict[str, Any]:
    """Handle ``manga-recap panel-inspect`` by rendering candidate cut visualizations."""
    project_slug = getattr(args, "project", "")
    chapter_num = getattr(args, "chapter", None)
    raw_panels = getattr(args, "panels", "") or ""
    if not project_slug or chapter_num is None:
        raise CliValidationError("Both --project and --chapter are required.")
    panel_ids = [p.strip() for p in raw_panels.split(",") if p.strip()]
    if not panel_ids:
        raise CliValidationError("At least one panel ID must be specified with --panels.")
    return inspect_flagged_panels(project_slug, str(chapter_num), panel_ids)


def handle_decide(args: argparse.Namespace) -> Dict[str, Any]:
    """Handle ``manga-recap decide`` by delegating to Decision API panel routing."""
    enforce_execution_policy("decide", args)

    project_slug = getattr(args, "project", "")
    chapter_num = getattr(args, "chapter", None)
    if not project_slug or chapter_num is None:
        raise CliValidationError("Both --project and --chapter are required for decide.")

    return decide_chapter_panels(
        project_slug,
        str(chapter_num),
        workers=getattr(args, "workers", 2),
        reclassify_all=bool(getattr(args, "reclassify_all", False)),
    )
