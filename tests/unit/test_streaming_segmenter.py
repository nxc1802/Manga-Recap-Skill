"""
tests/unit/test_streaming_segmenter.py
Unit tests for StreamingSegmenter, VCS coordinate mapping, 1D luminance profiler,
and page-boundary panel stitching.
"""

import json
from pathlib import Path
import numpy as np
from PIL import Image
import pytest

from src.core.schemas import PanelManifest, PanelStatus
from src.core.streaming_segmenter import RawPageMap, StreamingSegmenter


@pytest.fixture
def segmenter():
    return StreamingSegmenter(
        target_width=1080,
        threshold_variance=8.0,
        white_threshold=240.0,
        black_threshold=18.0,
        min_gutter_height=10,
        margin_padding=12,
        sufficiency_variance=100.0,
    )


def test_streaming_segmenter_empty_dir(tmp_path, segmenter):
    raw_dir = tmp_path / "raw"
    out_dir = tmp_path / "panels"
    raw_dir.mkdir()
    out_dir.mkdir()

    panels = segmenter.segment(raw_dir, out_dir)
    assert panels == []


def test_vcs_page_maps_coordinate_accumulation(tmp_path, segmenter):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()

    # Create 3 pages with varying sizes
    im1 = Image.new("RGB", (1080, 2000), "white")
    im1.save(raw_dir / "page_001.jpg")

    im2 = Image.new("RGB", (540, 1000), "white")  # scale 2.0 -> scaled height 2000
    im2.save(raw_dir / "page_002.jpg")

    im3 = Image.new("RGB", (1080, 1500), "white")
    im3.save(raw_dir / "page_003.jpg")

    files = [raw_dir / f"page_{i:03d}.jpg" for i in range(1, 4)]
    page_maps = segmenter.build_page_maps(files)

    assert len(page_maps) == 3
    assert page_maps[0].v_start == 0
    assert page_maps[0].v_end == 2000
    assert page_maps[1].v_start == 2000
    assert page_maps[1].v_end == 4000
    assert page_maps[2].v_start == 4000
    assert page_maps[2].v_end == 5500


def test_1d_luminance_profile_and_gutter_detection(tmp_path, segmenter):
    raw_dir = tmp_path / "raw"
    out_dir = tmp_path / "panels"
    raw_dir.mkdir()
    out_dir.mkdir()

    # Page 1: 1080x2800
    # 0..200: white gutter
    # 200..1100: patterned artwork (high variance, 900px > 756px force_merge_height)
    # 1100..1400: white gutter (300px)
    # 1400..2300: patterned artwork (high variance, 900px)
    # 2300..2800: white gutter
    arr = np.full((2800, 1080, 3), 255, dtype=np.uint8)
    rng = np.random.default_rng(42)
    arr[200:1100, :, :] = rng.integers(0, 200, (900, 1080, 3), dtype=np.uint8)
    arr[1400:2300, :, :] = rng.integers(0, 200, (900, 1080, 3), dtype=np.uint8)

    im = Image.fromarray(arr)
    im.save(raw_dir / "page_001.jpg")

    panels = segmenter.segment(raw_dir, out_dir)
    assert len(panels) >= 2
    for p in panels:
        assert p.width == 1080
        assert p.height >= 1080  # Under-height padded to square 1080x1080
        assert p.status == PanelStatus.ACTIVE
        assert Path(out_dir / p.filename).exists()


def test_page_crossing_panel_stitching(tmp_path, segmenter):
    raw_dir = tmp_path / "raw"
    out_dir = tmp_path / "panels"
    raw_dir.mkdir()
    out_dir.mkdir()

    # Artwork starts at bottom of page 1 and continues onto top of page 2
    # Page 1: 1080x800. Gutter: 0..200. Artwork: 200..800.
    arr1 = np.full((800, 1080, 3), 255, dtype=np.uint8)
    arr1[200:800, :, :] = 50  # dark artwork
    Image.fromarray(arr1).save(raw_dir / "page_001.jpg")

    # Page 2: 1080x800. Artwork continues: 0..600. Gutter: 600..800.
    arr2 = np.full((800, 1080, 3), 255, dtype=np.uint8)
    arr2[0:600, :, :] = 50  # dark artwork
    Image.fromarray(arr2).save(raw_dir / "page_002.jpg")

    panels = segmenter.segment(raw_dir, out_dir)
    assert len(panels) >= 1

    # At least one panel should cross pages
    page_crossing_panels = [p for p in panels if "+" in p.source_image]
    assert len(page_crossing_panels) >= 1
    crossing = page_crossing_panels[0]
    assert "page_001.jpg+page_002.jpg" in crossing.source_image
    panel_file = out_dir / crossing.filename
    assert panel_file.exists()
    with Image.open(panel_file) as p_img:
        assert p_img.width == 1080
        assert p_img.height >= 1080
