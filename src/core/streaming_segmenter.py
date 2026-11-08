"""
src/core/streaming_segmenter.py
Streaming Long-Strip Slicer using Virtual Continuous Coordinate Space (VCS)
and Downsampled 1D Luminance Profiling. Eliminates monolithic PIL canvas allocations.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
from PIL import Image

from src.core.logger import logger
from src.core.schemas import PanelItem, PanelStatus, RawImageInfo
from src.core.storage import calculate_sha256


@dataclass(frozen=True)
class RawPageMap:
    """Virtual continuous coordinate mapping for a single raw page image."""
    page_index: int           # 1, 2, ...
    filename: str             # "page_001.jpg"
    path: Path
    orig_width: int
    orig_height: int
    scale: float              # target_width / orig_width
    scaled_height: int        # round(orig_height * scale)
    v_start: int              # Virtual vertical start coordinate (inclusive)
    v_end: int                # Virtual vertical end coordinate (exclusive)


class StreamingSegmenter:
    """
    Streaming Long-Strip Slicer using Virtual Continuous Coordinate Space (VCS).
    Executes gutter detection via 64-column downsampled 1D luminance profiling,
    avoiding monolithic in-memory image allocations and PIL DecompressionBombErrors.
    """

    def __init__(
        self,
        target_width: int = 1080,
        threshold_variance: float = 8.0,
        white_threshold: float = 240.0,
        black_threshold: float = 18.0,
        min_gutter_height: int = 15,
        margin_padding: int = 12,
        sufficiency_variance: float = 100.0,
    ):
        self.target_width = target_width
        self.threshold_variance = threshold_variance
        self.white_threshold = white_threshold
        self.black_threshold = black_threshold
        self.min_gutter_height = min_gutter_height
        self.margin_padding = margin_padding
        self.sufficiency_variance = sufficiency_variance

    @staticmethod
    def _natural_sort_key(path: Path) -> List[Union[int, str]]:
        parts = re.split(r"(\d+)", path.name)
        return [int(p) if p.isdigit() else p.lower() for p in parts]

    def build_page_maps(self, raw_files: List[Path]) -> List[RawPageMap]:
        """
        Constructs Virtual Continuous Coordinate Space (VCS) table strictly
        from image file headers without loading full pixel bitmaps into memory.
        """
        sorted_files = sorted(raw_files, key=self._natural_sort_key)
        page_maps: List[RawPageMap] = []
        current_v = 0

        for idx, file_path in enumerate(sorted_files, start=1):
            with Image.open(file_path) as img:
                orig_w, orig_h = img.size

            scale = float(self.target_width) / float(orig_w) if orig_w > 0 else 1.0
            scaled_h = max(1, int(round(orig_h * scale)))
            v_start = current_v
            v_end = v_start + scaled_h
            current_v = v_end

            page_maps.append(
                RawPageMap(
                    page_index=idx,
                    filename=file_path.name,
                    path=file_path,
                    orig_width=orig_w,
                    orig_height=orig_h,
                    scale=scale,
                    scaled_height=scaled_h,
                    v_start=v_start,
                    v_end=v_end,
                )
            )

        logger.info(
            f"StreamingSegmenter: Mapped {len(page_maps)} pages to VCS. "
            f"Normalized width={self.target_width}px, total virtual height={current_v}px."
        )
        return page_maps

    def compute_1d_profiles(
        self, page_maps: List[RawPageMap]
    ) -> Tuple[np.ndarray, np.ndarray]:
        """
        Computes 1D row luminance means and variances by downsampling each raw page
        to 64 columns. Memory consumption is < 1 MB total profile vs hundreds of MBs.
        """
        all_means: List[np.ndarray] = []
        all_vars: List[np.ndarray] = []
        probe_width = 64

        for p in page_maps:
            with Image.open(p.path) as img:
                gray = img.convert("L")
                small = gray.resize((probe_width, p.scaled_height), Image.Resampling.BILINEAR)
                arr = np.asarray(small, dtype=np.float32)

            # Compute row-wise mean and variance across the 64 probe columns
            row_mean = np.mean(arr, axis=1)
            row_var = np.var(arr, axis=1)

            all_means.append(row_mean)
            all_vars.append(row_var)

        if not all_means:
            return np.array([], dtype=np.float32), np.array([], dtype=np.float32)

        row_means = np.concatenate(all_means)
        row_vars = np.concatenate(all_vars)
        return row_means, row_vars

    def detect_cut_intervals(
        self, row_means: np.ndarray, row_vars: np.ndarray
    ) -> List[Tuple[int, int]]:
        """
        Detects contiguous gutter intervals where variance is low and luminance is
        close to white or black. Applies midpoint cuts with clamped safety margins.
        """
        total_height = len(row_means)
        if total_height == 0:
            return []

        # Boolean mask of gutter rows
        is_gutter_row = (row_vars < self.threshold_variance) & (
            (row_means >= self.white_threshold) | (row_means <= self.black_threshold)
        )

        gutter_intervals: List[Tuple[int, int]] = []
        in_gutter = False
        gutter_start = 0

        for y in range(total_height):
            if is_gutter_row[y]:
                if not in_gutter:
                    in_gutter = True
                    gutter_start = y
            else:
                if in_gutter:
                    in_gutter = False
                    if (y - gutter_start) >= self.min_gutter_height:
                        gutter_intervals.append((gutter_start, y))

        if in_gutter:
            if (total_height - gutter_start) >= self.min_gutter_height:
                gutter_intervals.append((gutter_start, total_height))

        # If no gutters found, entire strip is a single candidate
        if not gutter_intervals:
            return [(0, total_height)]

        # Determine cut coordinates at the midpoint of each gutter with clamped margins
        cut_points: List[Tuple[int, int, int]] = []  # (g_start, g_cut, g_end)
        for g_start, g_end in gutter_intervals:
            g_cut = g_start + (g_end - g_start) // 2
            cut_points.append((g_start, g_cut, g_end))

        # Build panel slices between successive gutters
        slices: List[Tuple[int, int]] = []

        # 1. Panel before first gutter: only if first gutter does not start at 0
        first_start, first_cut, first_end = cut_points[0]
        if first_start > 0:
            p_top = 0
            p_bottom = min(first_end, first_cut + self.margin_padding)
            if p_bottom - p_top >= 5:
                slices.append((p_top, p_bottom))

        # 2. Panels between consecutive gutters
        for idx in range(len(cut_points) - 1):
            prev_start, prev_cut, prev_end = cut_points[idx]
            next_start, next_cut, next_end = cut_points[idx + 1]

            p_top = max(prev_start, prev_cut - self.margin_padding)
            p_bottom = min(next_end, next_cut + self.margin_padding)

            if p_bottom - p_top >= 5:
                slices.append((p_top, p_bottom))

        # 3. Panel after last gutter: only if last gutter does not reach total_height
        last_start, last_cut, last_end = cut_points[-1]
        if last_end < total_height:
            p_top = max(last_start, last_cut - self.margin_padding)
            p_bottom = total_height
            if p_bottom - p_top >= 5:
                slices.append((p_top, p_bottom))

        # Fallback if entire image was gutter or empty
        if not slices:
            slices.append((0, total_height))

        return slices

    def refine_intervals(
        self,
        intervals: List[Tuple[int, int]],
        row_vars: np.ndarray,
    ) -> List[Tuple[int, int]]:
        """
        Refines candidate panel intervals:
        - Gutter Merging: segments < force_merge_height are merged with nearest neighbor.
        - Tall Panel Splitting: segments > target_width * 3.5 are split at local variance minimums.
        """
        if not intervals:
            return []

        candidates = [{"coords": [top, bottom], "height": bottom - top} for top, bottom in intervals]
        force_merge_height = 60

        # 1. Gutter Merging
        while True:
            target_idx = -1
            for idx, cand in enumerate(candidates):
                if cand["height"] < force_merge_height:
                    target_idx = idx
                    break

            if target_idx == -1:
                break

            gutter_before = float("inf")
            if target_idx > 0:
                gutter_before = (
                    candidates[target_idx]["coords"][0]
                    - candidates[target_idx - 1]["coords"][1]
                )

            gutter_after = float("inf")
            if target_idx < len(candidates) - 1:
                gutter_after = (
                    candidates[target_idx + 1]["coords"][0]
                    - candidates[target_idx]["coords"][1]
                )

            if gutter_before == float("inf") and gutter_after == float("inf"):
                break

            if gutter_before <= gutter_after:
                merge_idx = target_idx - 1
                new_coords = [
                    candidates[merge_idx]["coords"][0],
                    candidates[target_idx]["coords"][1],
                ]
                candidates[merge_idx]["coords"] = new_coords
                candidates[merge_idx]["height"] = new_coords[1] - new_coords[0]
                candidates.pop(target_idx)
            else:
                merge_idx = target_idx + 1
                new_coords = [
                    candidates[target_idx]["coords"][0],
                    candidates[merge_idx]["coords"][1],
                ]
                candidates[target_idx]["coords"] = new_coords
                candidates[target_idx]["height"] = new_coords[1] - new_coords[0]
                candidates.pop(merge_idx)

        # 2. Tall Panel Splitting
        max_panel_height = int(self.target_width * 3.5)
        split_candidates: List[Dict[str, Any]] = []

        for cand in candidates:
            top, bottom = cand["coords"]
            h = cand["height"]
            if h > max_panel_height:
                current_top = top
                while bottom - current_top > max_panel_height:
                    search_start = current_top + self.target_width
                    search_end = min(bottom - self.target_width, current_top + max_panel_height)

                    if search_start >= search_end:
                        split_point = current_top + max_panel_height
                    else:
                        local_vars = row_vars[search_start:search_end]
                        if len(local_vars) > 0:
                            best_local_y = int(np.argmin(local_vars))
                            split_point = search_start + best_local_y
                        else:
                            split_point = current_top + max_panel_height

                    split_candidates.append({
                        "coords": [current_top, split_point],
                        "height": split_point - current_top
                    })
                    current_top = split_point

                if bottom > current_top:
                    split_candidates.append({
                        "coords": [current_top, bottom],
                        "height": bottom - current_top
                    })
            else:
                split_candidates.append(cand)

        return [(c["coords"][0], c["coords"][1]) for c in split_candidates]

    def extract_panels(
        self,
        page_maps: List[RawPageMap],
        intervals: List[Tuple[int, int]],
        output_dir: Path,
    ) -> List[PanelItem]:
        """
        Extracts panel crops based on virtual continuous coordinates.
        - Single page panels: crops directly from raw page file.
        - Page-crossing panels: stitches only the intersecting sub-strips.
        - Under-height panels (< target_width): even top/bottom padding to square.
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        panels: List[PanelItem] = []

        for idx, (v_top, v_bottom) in enumerate(intervals, start=1):
            h_virtual = v_bottom - v_top
            if h_virtual <= 0:
                continue

            # Identify intersecting pages
            intersecting = [
                p for p in page_maps
                if p.v_start < v_bottom and p.v_end > v_top
            ]
            if not intersecting:
                continue

            # Extract image for this panel
            if len(intersecting) == 1:
                p = intersecting[0]
                # Map virtual coords back to original page coordinates
                local_y1 = max(0, int(round((v_top - p.v_start) / p.scale)))
                local_y2 = min(p.orig_height, int(round((v_bottom - p.v_start) / p.scale)))
                if local_y2 <= local_y1:
                    local_y2 = min(p.orig_height, local_y1 + 10)

                with Image.open(p.path) as raw_img:
                    panel_crop = raw_img.convert("RGB").crop((0, local_y1, p.orig_width, local_y2))
                    if panel_crop.width != self.target_width:
                        scaled_h = max(1, int(round(panel_crop.height * p.scale)))
                        panel_crop = panel_crop.resize((self.target_width, scaled_h), Image.Resampling.LANCZOS)
                source_image_name = p.filename
                crop_box = (0, local_y1, p.orig_width, local_y2)

            else:
                # Panel crosses page boundary: stitch only intersecting sub-strips
                sub_strips: List[Image.Image] = []
                for p in intersecting:
                    sub_v_a = max(v_top, p.v_start)
                    sub_v_b = min(v_bottom, p.v_end)
                    local_y1 = max(0, int(round((sub_v_a - p.v_start) / p.scale)))
                    local_y2 = min(p.orig_height, int(round((sub_v_b - p.v_start) / p.scale)))
                    if local_y2 <= local_y1:
                        continue

                    with Image.open(p.path) as raw_img:
                        strip = raw_img.convert("RGB").crop((0, local_y1, p.orig_width, local_y2))
                        if strip.width != self.target_width:
                            scaled_h = max(1, int(round(strip.height * p.scale)))
                            strip = strip.resize((self.target_width, scaled_h), Image.Resampling.LANCZOS)
                        sub_strips.append(strip)

                if not sub_strips:
                    continue

                total_strip_h = sum(s.height for s in sub_strips)
                panel_crop = Image.new("RGB", (self.target_width, total_strip_h), (255, 255, 255))
                y_off = 0
                for s in sub_strips:
                    panel_crop.paste(s, (0, y_off))
                    y_off += s.height
                    s.close()

                source_image_name = f"{intersecting[0].filename}+{intersecting[-1].filename}"
                crop_box = (0, 0, self.target_width, total_strip_h)

            # Analyze variance
            gray_crop = panel_crop.convert("L")
            crop_var = float(np.var(np.asarray(gray_crop, dtype=np.float32)))

            final_w, final_h = panel_crop.size
            panel_status = PanelStatus.ACTIVE
            if crop_var < self.sufficiency_variance:
                # Low variance blank/spacer panel
                panel_status = PanelStatus.SKIP

            # Aspect Ratio & Padding
            if final_h < self.target_width:
                # Under-height: Even Padding to square
                pad_total = self.target_width - final_h
                pad_top = pad_total // 2
                pad_bottom = pad_total - pad_top

                gray_arr = np.asarray(gray_crop)
                top_mean = float(np.mean(gray_arr[0, :]))
                bottom_mean = float(np.mean(gray_arr[-1, :]))
                border_mean = (top_mean + bottom_mean) / 2.0
                bg_color = (0, 0, 0) if border_mean < 128.0 else (255, 255, 255)

                padded_img = Image.new("RGB", (self.target_width, self.target_width), bg_color)
                padded_img.paste(panel_crop, (0, pad_top))
                panel_crop.close()
                panel_crop = padded_img
                final_w, final_h = self.target_width, self.target_width

            panel_id = f"panel_{idx:03d}"
            filename = f"{panel_id}.jpg"
            out_file = output_dir / filename
            panel_crop.save(out_file, "JPEG", quality=90, progressive=True)
            panel_crop.close()

            sha256_hash = calculate_sha256(out_file)
            aspect_ratio = round(float(final_w) / float(final_h), 3) if final_h > 0 else 1.0

            panel_item = PanelItem(
                panel_id=panel_id,
                sequence_number=idx,
                filename=filename,
                relative_path=f"panels/{filename}",
                source_image=source_image_name,
                crop_box=crop_box,
                width=final_w,
                height=final_h,
                aspect_ratio=aspect_ratio,
                sha256=sha256_hash,
                status=panel_status,
                notes=f"variance={crop_var:.2f}"
            )
            panels.append(panel_item)

        logger.info(f"StreamingSegmenter: Extracted {len(panels)} panels to {output_dir}")
        return panels

    def segment(self, raw_pages_dir: Path, output_panels_dir: Path) -> List[PanelItem]:
        """High-level orchestration: loads headers, computes 1D profile, slices panels."""
        valid_exts = {".jpg", ".jpeg", ".png", ".webp"}
        raw_files = [f for f in raw_pages_dir.iterdir() if f.suffix.lower() in valid_exts and f.is_file()]
        if not raw_files:
            logger.warning(f"StreamingSegmenter: No raw images found in {raw_pages_dir}")
            return []

        page_maps = self.build_page_maps(raw_files)
        row_means, row_vars = self.compute_1d_profiles(page_maps)
        intervals = self.detect_cut_intervals(row_means, row_vars)
        refined_intervals = self.refine_intervals(intervals, row_vars)
        return self.extract_panels(page_maps, refined_intervals, output_panels_dir)
