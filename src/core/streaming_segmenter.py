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

        # Boolean mask of gutter rows (near-white or near-black uniform bands)
        eff_white = min(self.white_threshold, 235.0)
        eff_black = max(self.black_threshold, 22.0)
        is_gutter_row = (row_vars < self.threshold_variance) & (
            (row_means >= eff_white) | (row_means <= eff_black)
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

        if in_gutter and (total_height - gutter_start) >= self.min_gutter_height:
            gutter_intervals.append((gutter_start, total_height))

        # If no gutters found, entire strip is a single candidate
        if not gutter_intervals:
            return [(0, total_height)]

        # Cut at gutter edges (with tiny breathing pad) to strip dead white/black margins
        edge_pad = min(self.margin_padding, 4)
        slices: List[Tuple[int, int]] = []

        first_start, first_end = gutter_intervals[0]
        if first_start > 0:
            p_top = 0
            p_bottom = min(first_end, first_start + edge_pad)
            if p_bottom - p_top >= 5:
                slices.append((p_top, p_bottom))

        for idx in range(len(gutter_intervals) - 1):
            prev_start, prev_end = gutter_intervals[idx]
            next_start, next_end = gutter_intervals[idx + 1]

            p_top = max(prev_start, prev_end - edge_pad)
            p_bottom = min(next_end, next_start + edge_pad)
            if p_bottom - p_top >= 5:
                slices.append((p_top, p_bottom))

        last_start, last_end = gutter_intervals[-1]
        if last_end < total_height:
            p_top = max(last_start, last_end - edge_pad)
            p_bottom = total_height
            if p_bottom - p_top >= 5:
                slices.append((p_top, p_bottom))

        if not slices:
            slices.append((0, total_height))

        return slices

    def refine_intervals(
        self,
        intervals: List[Tuple[int, int]],
        row_vars: np.ndarray,
    ) -> List[Tuple[int, int]]:
        """
        Minimal interval sanity filter. Drops only sub-8px noise slivers;
        structural merge/split decisions are delegated to AI VLM sheet review.
        """
        return [(top, bottom) for top, bottom in intervals if (bottom - top) >= 8]

    def extract_vcs_crop(
        self,
        page_maps: List[RawPageMap],
        v_top: int,
        v_bottom: int,
    ) -> Optional[Tuple[Image.Image, str, Tuple[int, int, int, int]]]:
        """Extracts a single or page-crossing panel image directly from raw pages via VCS."""
        if v_bottom <= v_top:
            return None

        intersecting = [
            p for p in page_maps
            if p.v_start < v_bottom and p.v_end > v_top
        ]
        if not intersecting:
            return None

        if len(intersecting) == 1:
            p = intersecting[0]
            local_y1 = max(0, int(round((v_top - p.v_start) / p.scale)))
            local_y2 = min(p.orig_height, int(round((v_bottom - p.v_start) / p.scale)))
            if local_y2 <= local_y1:
                local_y2 = min(p.orig_height, local_y1 + 10)

            with Image.open(p.path) as raw_img:
                panel_crop = raw_img.convert("RGB").crop((0, local_y1, p.orig_width, local_y2))
                if panel_crop.width != self.target_width:
                    scaled_h = max(1, int(round(panel_crop.height * p.scale)))
                    panel_crop = panel_crop.resize((self.target_width, scaled_h), Image.Resampling.LANCZOS)
            return panel_crop, p.filename, (0, local_y1, p.orig_width, local_y2)

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
            return None

        total_strip_h = sum(s.height for s in sub_strips)
        panel_crop = Image.new("RGB", (self.target_width, total_strip_h), (255, 255, 255))
        y_off = 0
        for s in sub_strips:
            panel_crop.paste(s, (0, y_off))
            y_off += s.height
            s.close()

        source_image_name = f"{intersecting[0].filename}+{intersecting[-1].filename}"
        crop_box = (0, 0, self.target_width, total_strip_h)
        return panel_crop, source_image_name, crop_box

    def extract_panels(
        self,
        page_maps: List[RawPageMap],
        intervals: List[Tuple[int, int]],
        output_dir: Path,
    ) -> List[PanelItem]:
        """
        Extracts natural-aspect panel crops directly from raw pages via VCS.
        No blind square padding or pixel-heuristic SKIP; AI VLM classifies on sheets.
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        panels: List[PanelItem] = []

        for idx, (v_top, v_bottom) in enumerate(intervals, start=1):
            extracted = self.extract_vcs_crop(page_maps, v_top, v_bottom)
            if extracted is None:
                continue
            panel_crop, source_image_name, crop_box = extracted

            gray_crop = panel_crop.convert("L")
            crop_var = float(np.var(np.asarray(gray_crop, dtype=np.float32)))
            gray_crop.close()
            final_w, final_h = panel_crop.size

            panel_id = f"panel_{idx:03d}"
            filename = f"{panel_id}.jpg"
            out_file = output_dir / filename
            panel_crop.save(out_file, "JPEG", quality=90, progressive=True)
            panel_crop.close()

            sha256_hash = calculate_sha256(out_file)
            aspect_ratio = round(float(final_w) / float(final_h), 3) if final_h > 0 else 1.0

            panels.append(
                PanelItem(
                    panel_id=panel_id,
                    sequence_number=idx,
                    filename=filename,
                    relative_path=f"panels/{filename}",
                    source_image=source_image_name,
                    crop_box=crop_box,
                    vcs_start=v_top,
                    vcs_end=v_bottom,
                    width=final_w,
                    height=final_h,
                    aspect_ratio=aspect_ratio,
                    sha256=sha256_hash,
                    status=PanelStatus.ACTIVE,
                    notes=f"variance={crop_var:.2f}",
                )
            )

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
