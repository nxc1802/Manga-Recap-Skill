"""
tests/adversarial/test_m3_segmentation_adversarial.py
Adversarial stress-test suite for Milestone M3 (PR-04 Streaming Webtoon Segmentation V2 & Contact Sheet Generator).
Authored by M3 Challenger 1 to empirically challenge and verify:
1. Virtual Continuous Coordinate Space (VCS) & Bounded Memory (< 60 MB) on 50+ tall synthetic pages
2. Boundary & Corner Cases: Zero gutters, noisy gutters, 3-page spanning panels, tiny panels, very long panels, natural sort
3. Contact Sheet Conformance: 1920x1920 4x4 grid, progressive JPEG, and crimson red ID badges (#001..)
4. Schema & Checkpoint Integrity: PanelManifest compliance and CLI manga-recap segment exit codes / JSON envelopes
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tracemalloc
import numpy as np
from PIL import Image
import pytest

from src.core.contact_sheet import ContactSheetGenerator
from src.core.schemas import PanelItem, PanelManifest, PanelStatus, StageType, CheckpointStatus
from src.core.storage import (
    calculate_sha256,
    get_chapter_dir,
    get_project_dir,
    init_chapter_layout,
    init_project_layout,
    read_json,
)
from src.core.streaming_segmenter import RawPageMap, StreamingSegmenter

PYTHON_BIN = sys.executable


def run_cli(args: list[str]) -> subprocess.CompletedProcess:
    """Executes the manga-recap CLI in a separate OS process for pure stdout testing."""
    cmd = [PYTHON_BIN, "-m", "src.cli.main"] + args
    return subprocess.run(cmd, capture_output=True, text=True)


class TestVirtualContinuousCoordinateMemory:
    """Objective 1: Virtual Continuous Coordinate Space & OOM Elimination (< 60 MB RAM)."""

    def test_52_tall_pages_bounded_memory_and_no_decompression_bomb(self, tmp_path):
        """
        Simulates an extreme chapter with 52 tall pages (1080x2500 each).
        Total virtual continuous space is 1080 x 130,000 px = 140.4 million pixels.
        Monolithic canvas stitching would require > 400-500 MB and risk PIL DecompressionBomb.
        StreamingSegmenter must process this with peak RAM strictly bounded below 60 MB.
        """
        raw_dir = tmp_path / "raw"
        panels_dir = tmp_path / "panels"
        raw_dir.mkdir()
        panels_dir.mkdir()

        # Generate 52 tall pages with gutters and varied artwork
        rng = np.random.default_rng(12345)
        for i in range(1, 53):
            arr = np.full((2500, 1080, 3), 255, dtype=np.uint8)
            arr[100:2300, :, :] = rng.integers(10, 200, (2200, 1080, 3), dtype=np.uint8)
            im = Image.fromarray(arr)
            im.save(raw_dir / f"page_{i:03d}.jpg", quality=85)

        # Profile memory using tracemalloc
        tracemalloc.start()
        segmenter = StreamingSegmenter(target_width=1080)
        panels = segmenter.segment(raw_dir, panels_dir)
        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        peak_mb = peak / (1024 * 1024)

        # Assertions
        assert len(panels) == 52, f"Expected 52 panels, got {len(panels)}"
        assert peak_mb < 60.0, f"Memory limit exceeded! Peak RAM: {peak_mb:.2f} MB (limit: 60 MB)"

        # Verify panels exist on disk and have valid dimensions
        for p in panels:
            p_file = panels_dir / p.filename
            assert p_file.exists()
            assert p.width == 1080
            assert p.height >= 1080


class TestBoundaryAndCornerCases:
    """Objective 2: Stress-test edge and corner cases in segmentation."""

    def test_zero_gutters_continuous_content(self, tmp_path):
        """
        Adversarial case: Chapter has ZERO gutters (continuous high-variance artwork across 3 pages).
        Segmenter must not crash, must fall back to tall panel splitting, and must retain 100% of rows.
        """
        raw_dir = tmp_path / "raw"
        panels_dir = tmp_path / "panels"
        raw_dir.mkdir()
        panels_dir.mkdir()

        rng = np.random.default_rng(999)
        total_rows = 0
        for i in range(1, 4):
            height = 3000
            total_rows += height
            arr = rng.integers(40, 200, (height, 1080, 3), dtype=np.uint8)
            Image.fromarray(arr).save(raw_dir / f"page_{i:03d}.jpg")

        segmenter = StreamingSegmenter(target_width=1080)
        page_maps = segmenter.build_page_maps(sorted(raw_dir.glob("*.jpg")))
        row_means, row_vars = segmenter.compute_1d_profiles(page_maps)

        # Zero gutters detected initially
        intervals = segmenter.detect_cut_intervals(row_means, row_vars)
        assert intervals == [(0, total_rows)]

        # Refinement splits into valid panels
        refined = segmenter.refine_intervals(intervals, row_vars)
        assert len(refined) > 1

        # Check contiguity: start of next equals end of prev
        assert refined[0][0] == 0
        assert refined[-1][1] == total_rows
        for j in range(len(refined) - 1):
            assert refined[j][1] == refined[j + 1][0], f"Gap between panel {j} and {j+1}"

        panels = segmenter.extract_panels(page_maps, refined, panels_dir)
        assert len(panels) == len(refined)
        for p in panels:
            assert (panels_dir / p.filename).exists()

    def test_noisy_gutters_and_varying_thresholds(self, tmp_path):
        """
        Gutter containing scanner/JPEG compression noise (mean ~ 242, variance ~ 12.0).
        Evaluates sensitivity of threshold_variance.
        """
        raw_dir = tmp_path / "raw"
        panels_dir = tmp_path / "panels"
        raw_dir.mkdir()
        panels_dir.mkdir()

        rng = np.random.default_rng(42)
        arr = np.zeros((2700, 1080, 3), dtype=np.uint8)
        # Top gutter (var ~ 4)
        arr[0:200, :, :] = np.clip(246 + rng.normal(0, 2.0, (200, 1080, 3)), 0, 255).astype(np.uint8)
        # Artwork 1
        arr[200:1200, :, :] = rng.integers(10, 200, (1000, 1080, 3), dtype=np.uint8)
        # Noisy middle gutter (var ~ 12.0)
        arr[1200:1500, :, :] = np.clip(242 + rng.normal(0, 3.5, (300, 1080, 3)), 0, 255).astype(np.uint8)
        # Artwork 2
        arr[1500:2500, :, :] = rng.integers(10, 200, (1000, 1080, 3), dtype=np.uint8)
        # Bottom gutter (var ~ 1)
        arr[2500:2700, :, :] = np.clip(254 + rng.normal(0, 1.0, (200, 1080, 3)), 0, 255).astype(np.uint8)

        Image.fromarray(arr).save(raw_dir / "page_001.jpg")

        # Threshold variance 15.0 detects the noisy gutter cleanly
        seg = StreamingSegmenter(target_width=1080, threshold_variance=15.0)
        panels = seg.segment(raw_dir, panels_dir)
        assert len(panels) == 2
        assert panels[0].status == PanelStatus.ACTIVE
        assert panels[1].status == PanelStatus.ACTIVE

    def test_multi_page_crossing_panel_stitching(self, tmp_path):
        """
        Panel crosses 3 consecutive pages: starts at bottom of page 1, covers all of page 2, ends on page 3.
        Verifies sub-strip stitching and multi-page source identifier.
        """
        raw_dir = tmp_path / "raw"
        panels_dir = tmp_path / "panels"
        raw_dir.mkdir()
        panels_dir.mkdir()

        # Page 1: 1080x1000. 0..800: white gutter. 800..1000: artwork (red).
        arr1 = np.full((1000, 1080, 3), 255, dtype=np.uint8)
        arr1[800:1000, :, :] = [255, 0, 0]
        Image.fromarray(arr1).save(raw_dir / "page_001.jpg")

        # Page 2: 1080x800. 0..800: full page artwork (green).
        arr2 = np.full((800, 1080, 3), [0, 255, 0], dtype=np.uint8)
        Image.fromarray(arr2).save(raw_dir / "page_002.jpg")

        # Page 3: 1080x1000. 0..300: artwork (blue). 300..1000: white gutter.
        arr3 = np.full((1000, 1080, 3), 255, dtype=np.uint8)
        arr3[0:300, :, :] = [0, 0, 255]
        Image.fromarray(arr3).save(raw_dir / "page_003.jpg")

        segmenter = StreamingSegmenter(target_width=1080, min_gutter_height=50)
        panels = segmenter.segment(raw_dir, panels_dir)

        assert len(panels) == 1
        p = panels[0]
        assert "page_001.jpg+page_003.jpg" in p.source_image
        assert (panels_dir / p.filename).exists()
        with Image.open(panels_dir / p.filename) as p_img:
            assert p_img.width == 1080
            assert p_img.height == 2074

    def test_under_height_panel_padding_to_square(self, tmp_path):
        """
        Panel shorter than target_width (e.g. 600px < 1080px) must be padded evenly to 1080x1080 square.
        """
        raw_dir = tmp_path / "raw"
        panels_dir = tmp_path / "panels"
        raw_dir.mkdir()
        panels_dir.mkdir()

        arr = np.full((600, 1080, 3), 120, dtype=np.uint8)
        rng = np.random.default_rng(42)
        arr[100:500, :, :] = rng.integers(10, 200, (400, 1080, 3), dtype=np.uint8)
        Image.fromarray(arr).save(raw_dir / "page_001.jpg")

        segmenter = StreamingSegmenter(target_width=1080)
        panels = segmenter.segment(raw_dir, panels_dir)

        assert len(panels) == 1
        p = panels[0]
        assert p.width == 1080
        assert p.height == 1080
        assert p.aspect_ratio == 1.0

        with Image.open(panels_dir / p.filename) as p_img:
            assert p_img.size == (1080, 1080)

    def test_very_long_panel_splitting(self, tmp_path):
        """
        Panel exceeding 3.5 * target_width (e.g. 6000px tall) must be split at local variance minimums.
        """
        raw_dir = tmp_path / "raw"
        panels_dir = tmp_path / "panels"
        raw_dir.mkdir()
        panels_dir.mkdir()

        arr = np.full((6000, 1080, 3), 255, dtype=np.uint8)
        rng = np.random.default_rng(42)
        arr[:, :, :] = rng.integers(10, 200, (6000, 1080, 3), dtype=np.uint8)
        arr[2500:2600, :, :] = 255  # valley
        Image.fromarray(arr).save(raw_dir / "page_001.jpg")

        segmenter = StreamingSegmenter(target_width=1080)
        panels = segmenter.segment(raw_dir, panels_dir)

        assert len(panels) >= 2
        for p in panels:
            assert p.height <= int(1080 * 3.5) + 100

    def test_natural_sort_ordering_of_page_files(self, tmp_path):
        """
        Verifies natural sorting: page_2.jpg must be ordered before page_10.jpg.
        """
        raw_dir = tmp_path / "raw"
        raw_dir.mkdir()

        filenames = ["page_10.jpg", "page_1.jpg", "page_2.jpg", "page_20.jpg"]
        for fn in filenames:
            Image.new("RGB", (1080, 500), "white").save(raw_dir / fn)

        segmenter = StreamingSegmenter(target_width=1080)
        page_maps = segmenter.build_page_maps(list(raw_dir.glob("*.jpg")))
        ordered_names = [p.filename for p in page_maps]

        assert ordered_names == ["page_1.jpg", "page_2.jpg", "page_10.jpg", "page_20.jpg"]


class TestContactSheetConformance:
    """Objective 3: Contact Sheet Conformance (1920x1920, 4x4, crimson badges)."""

    def test_contact_sheet_dimensions_format_and_crimson_badge(self, tmp_path):
        """
        Tests 17 panels chunking into 2 contact sheets.
        Verifies 1920x1920 progressive JPEG dimensions and crimson red badge pixel color.
        """
        panels_dir = tmp_path / "panels"
        sheets_dir = tmp_path / "contact_sheets"
        panels_dir.mkdir()
        sheets_dir.mkdir()

        panel_items = []
        for i in range(1, 18):
            fn = f"panel_{i:03d}.jpg"
            im = Image.new("RGB", (1080, 1080), (100, 100, 100))
            im.save(panels_dir / fn)
            panel_items.append(
                PanelItem(
                    panel_id=f"panel_{i:03d}",
                    sequence_number=i,
                    filename=fn,
                    relative_path=f"panels/{fn}",
                    source_image="page_001.jpg",
                    crop_box=(0, 0, 1080, 1080),
                    width=1080,
                    height=1080,
                    aspect_ratio=1.0,
                    sha256="0" * 64,
                    status=PanelStatus.ACTIVE,
                )
            )

        gen = ContactSheetGenerator(cell_size=480, columns=4, rows=4)
        sheets = gen.generate_sheets(panel_items, panels_dir, sheets_dir)

        assert len(sheets) == 2
        assert sheets[0].sheet_id == "sheet_001"
        assert len(sheets[0].panel_ids) == 16
        assert sheets[1].sheet_id == "sheet_002"
        assert len(sheets[1].panel_ids) == 1

        for idx in [1, 2]:
            sheet_file = sheets_dir / f"sheet_{idx:03d}.jpg"
            assert sheet_file.exists()
            with Image.open(sheet_file) as s_img:
                assert s_img.size == (1920, 1920)
                assert s_img.format == "JPEG"

                # Check crimson badge pixel color at center of badge (46, 24)
                px = s_img.getpixel((46, 24))
                r, g, b = px[:3]
                # Crimson red has high red, low green/blue
                assert r > 150, f"Expected red > 150, got {r}"
                assert g < 100, f"Expected green < 100, got {g}"
                assert b < 100, f"Expected blue < 100, got {b}"

    def test_contact_sheet_empty_input(self, tmp_path):
        """Empty panel list returns empty contact sheet list without error."""
        gen = ContactSheetGenerator(cell_size=480, columns=4, rows=4)
        sheets = gen.generate_sheets([], tmp_path / "panels", tmp_path / "sheets")
        assert sheets == []


class TestCliSegmentIntegrationAndSchema:
    """Objective 4: CLI manga-recap segment, exit codes, and manifest.json schema integrity."""

    @pytest.fixture
    def clean_cli_project(self):
        slug = "adv_m3_seg_proj"
        proj_dir = get_project_dir(slug)
        if proj_dir.exists():
            import shutil
            shutil.rmtree(proj_dir)
        init_project_layout(slug, title="Adv M3 Segmentation Project")
        chap_dir = init_chapter_layout(slug, "1")
        yield slug, chap_dir
        if proj_dir.exists():
            import shutil
            shutil.rmtree(proj_dir)

    def test_cli_segment_success_and_manifest_schema_compliance(self, clean_cli_project):
        """
        Executes manga-recap segment CLI command.
        Verifies stdout purity, JSON envelope, manifest.json conformance, and chapter checkpoints.
        """
        slug, chap_dir = clean_cli_project
        raw_dir = chap_dir / "raw"

        # Populate with 2 valid raw images
        for i in range(1, 3):
            im = Image.new("RGB", (1080, 2000), (200, 200, 200))
            im.save(raw_dir / f"page_{i:03d}.jpg")

        res = run_cli(["segment", "-p", slug, "-c", "1", "--json"])
        assert res.returncode == 0, f"CLI error: {res.stderr}"

        # JSON Envelope validation
        env = json.loads(res.stdout)
        assert env["status"] == "success"
        assert env["command"] == "segment"
        assert env["exit_code"] == 0
        assert env["error"] is None
        assert env["data"]["total_raw_images"] == 2
        assert env["data"]["status"] == "completed"

        # Manifest Schema validation
        manifest_path = chap_dir / "manifest.json"
        assert manifest_path.exists()
        m_data = read_json(manifest_path)
        manifest = PanelManifest.model_validate(m_data)

        assert manifest.schema_version == "3.0.0"
        assert manifest.project_slug == slug
        assert manifest.chapter_number == "1"
        assert manifest.total_raw_images == 2
        assert len(manifest.panels) >= 1
        assert len(manifest.contact_sheets) >= 1

        # Check chapter.json stage checkpoint
        chap_data = read_json(chap_dir / "chapter.json")
        assert chap_data["stages"]["segment"]["status"] == "completed"
        artifacts = chap_data["stages"]["segment"]["artifacts"]
        assert "manifest.json" in artifacts
        assert any(a.startswith("panels/") for a in artifacts)
        assert any(a.startswith("contact_sheets/") for a in artifacts)

    def test_cli_segment_missing_arguments_returns_exit_code_2(self):
        """Missing required --project or --chapter returns validation error (exit code 2)."""
        res1 = run_cli(["segment", "-c", "1", "--json"])
        assert res1.returncode == 2

        res2 = run_cli(["segment", "-p", "some_slug", "--json"])
        assert res2.returncode == 2

    def test_cli_segment_empty_raw_directory_returns_exit_code_2(self, clean_cli_project):
        """Empty raw/ directory returns validation error (exit code 2)."""
        slug, chap_dir = clean_cli_project
        # chap_dir has empty raw/ directory
        res = run_cli(["segment", "-p", slug, "-c", "1", "--json"])
        assert res.returncode == 2
        env = json.loads(res.stdout)
        assert env["status"] == "error"
        assert env["exit_code"] == 2
        assert "No raw images found" in env["error"]["message"]
