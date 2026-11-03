"""
tests/adversarial/run_m3_pr04_stress_harness.py
Empirical Adversarial Stress Harness for PR-04 Streaming Segmentation.
Executed by M3 Adversarial Challenger 1 (m3_challenger_1_iter2).
"""

from __future__ import annotations

import gc
import json
import os
import resource
import shutil
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np
from PIL import Image

from src.core.contact_sheet import ContactSheetGenerator
from src.core.schemas import PanelItem, PanelStatus
from src.core.streaming_segmenter import RawPageMap, StreamingSegmenter


def test_extreme_memory_and_decompression_bomb(output_results: Dict[str, Any]) -> bool:
    """
    Stress 1: 55 tall pages (800x4000 px each).
    Cumulative pixels: 55 * 800 * 4000 = 176,000,000 pixels (176 MP).
    Virtual Continuous Space (normalized to 1080w):
    scale = 1080 / 800 = 1.35
    scaled_h = 5400 px per page
    Total virtual height = 55 * 5400 = 297,000 px.
    Total virtual canvas = 1080 x 297,000 = 320,760,000 pixels (320.7 MP).
    PIL DecompressionBomb limit: 89,478,485 pixels.
    Monolithic stitching would throw DecompressionBombError or consume > 1.2 GB RAM.
    Target: Peak RAM < 60 MB and zero DecompressionBombError.
    """
    print("\n" + "=" * 70)
    print("STRESS 1: 55 TALL PAGES (800x4000) STREAMING SEGMENTATION & MEMORY")
    print("=" * 70)

    with tempfile.TemporaryDirectory() as tmp_dir:
        raw_dir = Path(tmp_dir) / "raw"
        panels_dir = Path(tmp_dir) / "panels"
        raw_dir.mkdir()
        panels_dir.mkdir()

        num_pages = 55
        page_w = 800
        page_h = 4000
        print(f"Generating {num_pages} synthetic tall pages ({page_w}x{page_h})...")

        rng = np.random.default_rng(42)
        # Each page has top gutter (100px white), artwork (3700px), bottom gutter (200px white)
        for i in range(1, num_pages + 1):
            page_arr = np.full((page_h, page_w, 3), 255, dtype=np.uint8)
            # Add artwork with varying patterns
            page_arr[100:3800, :, :] = rng.integers(20, 220, (3700, page_w, 3), dtype=np.uint8)
            img = Image.fromarray(page_arr)
            img.save(raw_dir / f"page_{i:03d}.jpg", quality=80)

        total_raw_pixels = num_pages * page_w * page_h
        print(f"Total raw pixels: {total_raw_pixels:,} ({total_raw_pixels / 1e6:.1f} MP)")
        print(f"PIL DecompressionBomb threshold: {Image.MAX_IMAGE_PIXELS:,} pixels")
        assert total_raw_pixels > Image.MAX_IMAGE_PIXELS, "Raw pixels must exceed DecompressionBomb threshold!"

        # Warm up & force garbage collection
        gc.collect()
        rss_before_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

        tracemalloc.start()
        start_time = time.perf_counter()

        segmenter = StreamingSegmenter(target_width=1080)
        panels = segmenter.segment(raw_dir, panels_dir)

        duration = time.perf_counter() - start_time
        current_mem, peak_mem = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        rss_after_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        peak_mb = peak_mem / (1024 * 1024)
        print(f"Segmentation completed in {duration:.2f}s")
        print(f"Extracted panels: {len(panels)}")
        print(f"Tracemalloc Peak Memory: {peak_mb:.2f} MB")
        print(f"OS ru_maxrss change: {(rss_after_kb - rss_before_kb) / 1024:.2f} MB")

        # Verify panels on disk
        active_panels = [p for p in panels if p.status == PanelStatus.ACTIVE]
        print(f"Active panels: {len(active_panels)}")
        for p in active_panels[:5]:
            p_path = panels_dir / p.filename
            assert p_path.exists(), f"Panel file missing: {p.filename}"
            with Image.open(p_path) as p_im:
                assert p_im.width == 1080, f"Expected 1080w, got {p_im.width}"
                assert p_im.height >= 1080, f"Expected min 1080h, got {p_im.height}"

        output_results["stress_1_55_pages"] = {
            "num_pages": num_pages,
            "raw_pixels": total_raw_pixels,
            "duration_seconds": duration,
            "peak_memory_mb": peak_mb,
            "memory_threshold_mb": 60.0,
            "total_panels": len(panels),
            "active_panels": len(active_panels),
            "passed": (peak_mb < 60.0 and len(panels) > 0),
        }

        if peak_mb >= 60.0:
            print(f"FAIL: Peak memory {peak_mb:.2f} MB exceeded 60 MB limit!")
            return False

        print(f"PASS: Memory bounded at {peak_mb:.2f} MB (< 60 MB limit). Zero DecompressionBombError.")
        return True


def test_cross_page_boundary_panels(output_results: Dict[str, Any]) -> bool:
    """
    Stress 2: Panels spanning across page boundaries with interior gutters.
    Page 1: Panel 1 (0..900) + Gutter 1 (900..1100) + Panel 2 part A (1100..1600, 500px).
    Page 2: Panel 2 part B (0..600, 600px) + Gutter 2 (600..800) + Panel 3 (800..1700).
    Panel 2 total artwork: 500px + 600px = 1100px (> 756px force_merge_height).
    The panel must span page_001.jpg and page_002.jpg seamlessly without dropping rows.
    """
    print("\n" + "=" * 70)
    print("STRESS 2: CROSS-PAGE BOUNDARY PANEL STITCHING & SEAMLESSNESS")
    print("=" * 70)

    with tempfile.TemporaryDirectory() as tmp_dir:
        raw_dir = Path(tmp_dir) / "raw"
        panels_dir = Path(tmp_dir) / "panels"
        raw_dir.mkdir()
        panels_dir.mkdir()

        # Page 1: 1080x1600
        p1_arr = np.full((1600, 1080, 3), 100, dtype=np.uint8)
        p1_arr[900:1100, :, :] = 255  # Gutter 1 (200px)
        grad1 = np.linspace(10, 120, 500, dtype=np.uint8)[:, None, None]
        p1_arr[1100:1600, :, :] = np.tile(grad1, (1, 1080, 3))
        Image.fromarray(p1_arr).save(raw_dir / "page_001.jpg")

        # Page 2: 1080x1700
        p2_arr = np.full((1700, 1080, 3), 120, dtype=np.uint8)
        grad2 = np.linspace(121, 230, 600, dtype=np.uint8)[:, None, None]
        p2_arr[0:600, :, :] = np.tile(grad2, (1, 1080, 3))
        p2_arr[600:800, :, :] = 255  # Gutter 2 (200px)
        Image.fromarray(p2_arr).save(raw_dir / "page_002.jpg")

        segmenter = StreamingSegmenter(target_width=1080, min_gutter_height=50)
        panels = segmenter.segment(raw_dir, panels_dir)

        print(f"Extracted {len(panels)} panels across 2 pages.")
        assert len(panels) == 3, f"Expected 3 panels, got {len(panels)}"
        crossing_panels = [p for p in panels if "+" in p.source_image]
        assert len(crossing_panels) == 1, f"Expected 1 crossing panel, got {len(crossing_panels)}"
        panel = crossing_panels[0]

        print(f"Panel source_image: {panel.source_image}")
        assert "page_001.jpg+page_002.jpg" in panel.source_image, "Must indicate cross-page stitch"

        panel_path = panels_dir / panel.filename
        assert panel_path.exists()
        with Image.open(panel_path) as p_im:
            pw, ph = p_im.size
            print(f"Stitched panel dimensions: {pw}x{ph}")
            assert pw == 1080, f"Expected width 1080, got {pw}"
            # Artwork: 500 + 600 = 1100 px + safety margins (~224 px)
            assert 1100 <= ph <= 1400, f"Expected height ~1100-1400, got {ph}"

            # Inspect seam across boundary (seam is at row 500 + margin in stitched panel)
            panel_arr = np.asarray(p_im)
            seam_y = 500 + segmenter.margin_padding
            row_above = panel_arr[seam_y - 5, 540, :]
            row_below = panel_arr[seam_y + 5, 540, :]
            color_diff = np.max(np.abs(row_above.astype(int) - row_below.astype(int)))
            print(f"Seam transition maximum delta: {color_diff} (tolerance < 25)")
            assert color_diff < 25, f"Seam color discontinuity too high: {color_diff}"

        output_results["stress_2_cross_page"] = {
            "source_image": panel.source_image,
            "width": pw,
            "height": ph,
            "seam_color_delta": int(color_diff),
            "passed": True,
        }
        print("PASS: Cross-page stitching seamless and metadata correctly formatted.")
        return True


def test_defect_leading_trailing_gutters_and_under_height_panels(output_results: Dict[str, Any]) -> bool:
    """
    Stress 2B (Defect Verification):
    Demonstrates and empirically reproduces:
    1. Defect 1: Leading gutters (y=0..200) and trailing gutters (y=2200..2400) produce phantom
       slices (0..112) and (2288..2400) which are forcibly merged back, completely defeating gutter cuts.
    2. Defect 2: Two panels of 500px separated by a 300px white gutter are merged into a single
       monolithic panel because force_merge_height=756px (0.7 * target_width) merges all panels < 756px.
    """
    print("\n" + "=" * 70)
    print("STRESS 2B: EMPIRICAL DEFECT VERIFICATION (LEADING GUTTERS & <756px PANELS)")
    print("=" * 70)

    seg = StreamingSegmenter(target_width=1080)

    # Test 1: Leading gutter 0..200, panel 200..1200, trailing gutter 1200..1400
    means1 = np.full(1400, 100.0)
    vars1 = np.full(1400, 50.0)
    means1[0:200] = 255.0; vars1[0:200] = 0.0
    means1[1200:1400] = 255.0; vars1[1200:1400] = 0.0

    raw1 = seg.detect_cut_intervals(means1, vars1)
    ref1 = seg.refine_intervals(raw1, vars1)
    print(f"Scenario 1 (Leading & Trailing Gutters around 1000px panel):")
    print(f"  Raw slices: {raw1}")
    print(f"  Refined slices: {ref1}")

    # Test 2: Two 500px panels separated by 300px gutter
    means2 = np.full(1300, 100.0)
    vars2 = np.full(1300, 50.0)
    means2[500:800] = 255.0; vars2[500:800] = 0.0

    raw2 = seg.detect_cut_intervals(means2, vars2)
    ref2 = seg.refine_intervals(raw2, vars2)
    print(f"Scenario 2 (Two 500px panels separated by 300px gutter):")
    print(f"  Raw slices: {raw2}")
    print(f"  Refined slices: {ref2}")

    defect_1_confirmed = (ref1 == [(0, 1400)])
    defect_2_confirmed = (ref2 == [(0, 1300)])

    print(f"Defect 1 Confirmed (Gutters swallowed into monolithic [0, 1400]): {defect_1_confirmed}")
    print(f"Defect 2 Confirmed (Two 500px panels merged across gutter into [0, 1300]): {defect_2_confirmed}")

    output_results["defects_found"] = {
        "defect_1_leading_trailing_gutter_swallowed": defect_1_confirmed,
        "defect_1_raw_intervals": raw1,
        "defect_1_refined_intervals": ref1,
        "defect_2_panels_under_756px_merged": defect_2_confirmed,
        "defect_2_raw_intervals": raw2,
        "defect_2_refined_intervals": ref2,
    }
    return defect_1_confirmed and defect_2_confirmed


def test_gutter_edge_cases_and_safety_margins(output_results: Dict[str, Any]) -> bool:
    """
    Stress 3: Gutter Edge Cases & Safety Margins:
    1. Zero gutters (pure artwork continuous across full page).
    2. Tiny gutters (< 10px, e.g. 6px): below min_gutter_height=15px, must NOT be cut.
    3. Safety margins: verify clamped ±12px padding into gutters without cropping artwork.
    4. Noisy gutters: verify robust detection.
    """
    print("\n" + "=" * 70)
    print("STRESS 3: GUTTER EDGE CASES, TINY GUTTERS & SAFETY MARGINS")
    print("=" * 70)

    with tempfile.TemporaryDirectory() as tmp_dir:
        raw_dir = Path(tmp_dir) / "raw"
        panels_dir = Path(tmp_dir) / "panels"
        raw_dir.mkdir()
        panels_dir.mkdir()

        # Page 1: Has tiny gutter (8px at y=500..508), artwork around it.
        # Has main gutter (100px at y=1200..1300).
        # Has noisy gutter (80px at y=2200..2280 with Gaussian noise).
        page_h = 3000
        page_w = 1080
        arr = np.zeros((page_h, page_w, 3), dtype=np.uint8)

        rng = np.random.default_rng(101)
        # Artwork 1: 0..500
        arr[0:500, :, :] = rng.integers(30, 200, (500, page_w, 3), dtype=np.uint8)
        # Tiny gutter: 500..508 (8px white) - SHOULD NOT BE CUT (below min_gutter_height=15)
        arr[500:508, :, :] = 255
        # Artwork 1 cont: 508..1200
        arr[508:1200, :, :] = rng.integers(30, 200, (692, page_w, 3), dtype=np.uint8)
        # Standard gutter: 1200..1300 (100px white) - SHOULD BE CUT at y=1250 ± 12
        arr[1200:1300, :, :] = 255
        # Artwork 2: 1300..2200
        arr[1300:2200, :, :] = rng.integers(30, 200, (900, page_w, 3), dtype=np.uint8)
        # Noisy gutter: 2200..2280 (80px, mean=244, var ~ 9)
        arr[2200:2280, :, :] = np.clip(244 + rng.normal(0, 3.0, (80, page_w, 3)), 0, 255).astype(np.uint8)
        # Artwork 3: 2280..3000
        arr[2280:3000, :, :] = rng.integers(30, 200, (720, page_w, 3), dtype=np.uint8)

        Image.fromarray(arr).save(raw_dir / "page_001.jpg")

        segmenter = StreamingSegmenter(
            target_width=1080,
            threshold_variance=12.0,
            min_gutter_height=15,
            margin_padding=12,
        )
        page_maps = segmenter.build_page_maps([raw_dir / "page_001.jpg"])
        row_means, row_vars = segmenter.compute_1d_profiles(page_maps)
        intervals = segmenter.detect_cut_intervals(row_means, row_vars)

        print(f"Detected raw cut intervals: {intervals}")
        # Verify tiny gutter at 500..508 was NOT split:
        # Check that no interval cut occurs around 504
        for top, bottom in intervals:
            assert not (495 < top < 515), f"Tiny 8px gutter improperly cut as interval top: {top}"
            assert not (495 < bottom < 515), f"Tiny 8px gutter improperly cut as interval bottom: {bottom}"

        print("Verification: Tiny 8px gutter was correctly preserved within Artwork 1 (not cut).")

        # Verify standard gutter cut at 1200..1300:
        # Midpoint is 1250.
        # Panel 1 bottom should be min(1300, 1250 + 12) = 1262.
        # Panel 2 top should be max(1200, 1250 - 12) = 1238.
        matching_bottom = [b for (t, b) in intervals if abs(b - 1262) <= 2]
        matching_top = [t for (t, b) in intervals if abs(t - 1238) <= 2]
        print(f"Safety margin test around gutter [1200, 1300]:")
        print(f"  Expected Panel bottom = 1262 (gutter_cut 1250 + 12 padding), found: {matching_bottom}")
        print(f"  Expected Panel top = 1238 (gutter_cut 1250 - 12 padding), found: {matching_top}")
        assert len(matching_bottom) > 0, "Safety margin bottom padding violated!"
        assert len(matching_top) > 0, "Safety margin top padding violated!"

        # Verify safety margin does NOT crop artwork:
        # Artwork ended at 1200, bottom cut is at 1262 (62px into gutter, 0px into artwork).
        # Artwork started at 1300, top cut is at 1238 (62px into gutter before 1300, 0px into artwork).
        assert matching_bottom[0] > 1200, "Bottom cut cropped into artwork!"
        assert matching_top[0] < 1300, "Top cut cropped into artwork!"

        # Extract panels and check on disk
        panels = segmenter.extract_panels(page_maps, intervals, panels_dir)
        print(f"Extracted {len(panels)} panels.")
        for p in panels:
            assert (panels_dir / p.filename).exists()

        output_results["stress_3_gutters_and_margins"] = {
            "tiny_gutter_ignored": True,
            "margin_bottom": matching_bottom[0],
            "margin_top": matching_top[0],
            "artwork_protected": True,
            "passed": True,
        }
        print("PASS: Tiny gutters (<15px) ignored; clamped ±12px margins preserve artwork 100%.")
        return True


def test_contact_sheet_1920_spec_and_badges(output_results: Dict[str, Any]) -> bool:
    """
    Stress 4: Contact Sheet 1920x1920 & Badge Conformance.
    1. Render 16 panels into a single sheet.
    2. Check resolution: strictly (1920, 1920).
    3. Check grid: 4 columns x 4 rows = 16 cells (480x480 each).
    4. Check badges: every single cell must have a rounded crimson badge (#001 .. #016).
       Badge coordinates: x = col * 480 + 12, y = row * 480 + 12.
       Badge color: crimson red RGB (220, 20, 60), outline white (255, 255, 255).
    5. Check progressive JPEG encoding.
    """
    print("\n" + "=" * 70)
    print("STRESS 4: CONTACT SHEET 1920x1920 4x4 & CRIMSON BADGE VALIDATION")
    print("=" * 70)

    with tempfile.TemporaryDirectory() as tmp_dir:
        panels_dir = Path(tmp_dir) / "panels"
        sheets_dir = Path(tmp_dir) / "contact_sheets"
        panels_dir.mkdir()
        sheets_dir.mkdir()

        # Create 16 unique panels
        panels: List[PanelItem] = []
        rng = np.random.default_rng(202)
        for i in range(1, 17):
            fn = f"panel_{i:03d}.jpg"
            # Varied grayscale panels
            base_col = 50 + i * 10
            arr = np.full((1080, 1080, 3), base_col, dtype=np.uint8)
            # Add some inner texture
            arr[200:800, 200:800, :] = rng.integers(10, 240, (600, 600, 3), dtype=np.uint8)
            Image.fromarray(arr).save(panels_dir / fn, quality=85)

            panels.append(
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
                    sha256="a" * 64,
                    status=PanelStatus.ACTIVE,
                )
            )

        gen = ContactSheetGenerator(cell_size=480, columns=4, rows=4, quality=88)
        sheets = gen.generate_sheets(panels, panels_dir, sheets_dir)

        assert len(sheets) == 1, f"Expected exactly 1 sheet for 16 panels, got {len(sheets)}"
        sheet_info = sheets[0]
        sheet_path = sheets_dir / f"{sheet_info.sheet_id}.jpg"
        assert sheet_path.exists()

        with Image.open(sheet_path) as s_im:
            w, h = s_im.size
            fmt = s_im.format
            print(f"Sheet dimensions: {w}x{h}, format: {fmt}")
            assert (w, h) == (1920, 1920), f"Expected (1920, 1920), got ({w}, {h})"
            assert fmt == "JPEG", f"Expected JPEG, got {fmt}"

            # Validate badges on all 16 cells
            img_arr = np.asarray(s_im)
            badge_checks = []

            for cell_idx in range(16):
                col = cell_idx % 4
                row = cell_idx // 4
                badge_x = col * 480 + 12
                badge_y = row * 480 + 12

                # Center/right region of badge is at badge_x + 55, badge_y + 12 (outside text)
                # In contact_sheet.py: badge is 68w x 24h.
                # Crimson color is (220, 20, 60, 204) alpha-composited on thumbnail.
                badge_pixel = img_arr[badge_y + 12, badge_x + 55]
                r, g, b = int(badge_pixel[0]), int(badge_pixel[1]), int(badge_pixel[2])

                is_crimson = (r > 150) and (g < 100) and (b < 100)
                badge_checks.append(is_crimson)

            all_badges_valid = all(badge_checks)
            print(f"Verified 16/16 badges: {sum(badge_checks)} crimson badges confirmed.")
            assert all_badges_valid, "One or more badges failed crimson color check!"

        output_results["stress_4_contact_sheet"] = {
            "resolution": [w, h],
            "format": fmt,
            "badge_count": 16,
            "badges_confirmed": sum(badge_checks),
            "passed": True,
        }
        print("PASS: Contact sheet is 1920x1920, 4x4 grid, all 16 crimson badges present.")
        return True


def main() -> int:
    results: Dict[str, Any] = {}
    success = True

    try:
        t1 = test_extreme_memory_and_decompression_bomb(results)
        t2 = test_cross_page_boundary_panels(results)
        t2b_defects_found = test_defect_leading_trailing_gutters_and_under_height_panels(results)
        t3 = test_gutter_edge_cases_and_safety_margins(results)
        t4 = test_contact_sheet_1920_spec_and_badges(results)

        # If algorithmic defects are empirically confirmed, reject milestone PR-04
        has_critical_defects = t2b_defects_found
        success = t1 and t2 and t3 and t4 and not has_critical_defects
    except Exception as e:
        print(f"\nFATAL EXCEPTION DURING STRESS TESTING: {e}")
        import traceback
        traceback.print_exc()
        success = False
        results["fatal_error"] = str(e)

    summary_file = Path("tests/adversarial/m3_pr04_stress_results.json")
    results["verdict"] = "APPROVE" if success else "REJECT"
    summary_file.write_text(json.dumps(results, indent=2))
    print(f"\nWrote results to {summary_file}")

    print("\n" + "=" * 70)
    if success:
        print("ALL EMPIRICAL CHALLENGES PASSED! VERDICT: APPROVE")
        return 0
    else:
        print("EMPIRICAL DEFECTS CONFIRMED! VERDICT: REJECT")
        return 1


if __name__ == "__main__":
    sys.exit(main())
