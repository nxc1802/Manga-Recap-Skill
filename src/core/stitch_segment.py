"""
src/core/stitch_segment.py
Backward-compatible wrapper around StreamingSegmenter.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from src.core.logger import logger
from src.core.streaming_segmenter import StreamingSegmenter


def preprocess_and_segment_images(
    raw_pages_dir: Path,
    output_panels_dir: Path,
    threshold_variance: float = 8.0,
    min_gutter_height: int = 12,
    min_panel_height: int = 40,
    sufficiency_variance: float = 100.0,
) -> List[Path]:
    """
    Backward-compatible wrapper around StreamingSegmenter.
    Scans raw_pages_dir, segments images using Virtual Continuous Coordinate Space (VCS),
    and returns list of saved panel Paths.
    """
    logger.info(f"Stitch & Segment: Scanning directory {raw_pages_dir}...")
    valid_exts = {".jpg", ".jpeg", ".png", ".webp"}
    raw_files = [f for f in raw_pages_dir.iterdir() if f.suffix.lower() in valid_exts and f.is_file()] if raw_pages_dir.exists() else []

    if not raw_files:
        logger.warning(f"Stitch & Segment: No raw pages found in {raw_pages_dir}")
        return []

    segmenter = StreamingSegmenter(
        target_width=1080,
        threshold_variance=threshold_variance,
        white_threshold=240.0,
        black_threshold=18.0,
        min_gutter_height=min_gutter_height,
        margin_padding=12,
        sufficiency_variance=sufficiency_variance,
    )

    panel_items = segmenter.segment(raw_pages_dir, output_panels_dir)
    saved_paths = [output_panels_dir / p.filename for p in panel_items]
    logger.info(f"Stitch & Segment: Completed! Generated {len(saved_paths)} panels.")
    return saved_paths
