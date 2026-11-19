"""
src/core/panel_repack.py
Minimal deterministic VCS repack engine applying AI VLM `panel_plan.json` decisions:
only merge, split, keep, and 3-group classification (active / context_only / skip).
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.core.contact_sheet import ContactSheetGenerator
from src.core.schemas import PanelItem, PanelManifest, PanelStatus, utc_iso_now
from src.core.storage import atomic_write_json, calculate_sha256, load_manifest
from src.core.streaming_segmenter import StreamingSegmenter


class PanelPlanOp(str, Enum):
    KEEP = "keep"
    MERGE = "merge"
    SPLIT = "split"
    SPLIT_AT = "split_at"
    DROP = "drop"


class PanelPlanItem(BaseModel):
    """Single AI VLM structural + classification decision over 1+ raw segmented panels."""

    model_config = ConfigDict(extra="forbid")

    op: PanelPlanOp = PanelPlanOp.KEEP
    source_panel_ids: List[str] = Field(..., min_length=1)
    group: PanelStatus = PanelStatus.ACTIVE
    split_parts: int = Field(default=2, ge=2, le=8)
    split_ratios: Optional[List[float]] = None
    cut_ratios: Optional[List[float]] = None
    tall_motion_hint: Optional[str] = Field(default=None, pattern=r"^(scroll|static)$")
    reason: Optional[str] = None

    @model_validator(mode="after")
    def validate_item(self) -> "PanelPlanItem":
        if self.op == PanelPlanOp.MERGE and len(self.source_panel_ids) < 2:
            raise ValueError("merge op requires at least 2 source_panel_ids")
        if self.op != PanelPlanOp.MERGE and len(self.source_panel_ids) != 1:
            raise ValueError(f"{self.op.value} op requires exactly 1 source_panel_id")
        if self.op == PanelPlanOp.DROP:
            self.group = PanelStatus.SKIP
        ratios = self.cut_ratios if self.cut_ratios is not None else self.split_ratios
        if self.op == PanelPlanOp.SPLIT_AT and not ratios:
            raise ValueError("split_at op requires cut_ratios (or split_ratios)")
        if ratios is not None:
            if not ratios or any(r <= 0.0 or r >= 1.0 for r in ratios):
                raise ValueError("cut_ratios/split_ratios values must be strictly between 0.0 and 1.0")
            if sorted(ratios) != list(ratios) or len(set(ratios)) != len(ratios):
                raise ValueError("cut_ratios/split_ratios must be strictly increasing")
        return self


class PanelPlan(BaseModel):
    """Chapter-level AI VLM panel repack & 3-group classification plan."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "4.1.0"
    project_slug: str
    chapter_number: str
    items: List[PanelPlanItem] = Field(..., min_length=1)


def _compute_split_intervals(
    v_start: int,
    v_end: int,
    split_parts: int,
    split_ratios: Optional[List[float]],
    row_vars: Optional[np.ndarray],
) -> List[Tuple[int, int]]:
    """Computes balanced or ratio-guided VCS sub-intervals, snapping slightly to local min variance."""
    height = v_end - v_start
    if height < 16:
        return [(v_start, v_end)]

    ratios = split_ratios if split_ratios else [i / float(split_parts) for i in range(1, split_parts)]
    cuts = [v_start]
    snap_radius = max(12, int(round(height * 0.03)))

    for r in ratios:
        target_y = v_start + int(round(height * r))
        if row_vars is not None and len(row_vars) > 0:
            lo = max(cuts[-1] + 8, target_y - snap_radius)
            hi = min(v_end - 8, target_y + snap_radius)
            if hi > lo and hi <= len(row_vars):
                best_offset = int(np.argmin(row_vars[lo:hi]))
                target_y = lo + best_offset
        if cuts[-1] < target_y < v_end:
            cuts.append(target_y)
    cuts.append(v_end)
    return [(cuts[i], cuts[i + 1]) for i in range(len(cuts) - 1) if cuts[i + 1] > cuts[i]]


def apply_panel_plan(
    chapter_dir: Path,
    plan: PanelPlan,
    target_width: int = 1080,
    cell_size: int = 480,
    grid_cols: int = 4,
    grid_rows: int = 4,
) -> Dict[str, Any]:
    """
    Applies AI VLM `panel_plan.json` directly against raw pages via VCS coordinates.
    Regenerates `panels/`, `contact_sheets/` (excluding SKIP), and `manifest.json`.
    """
    chap_dir = Path(chapter_dir)
    manifest = load_manifest(chap_dir)
    if manifest is None or not manifest.panels:
        raise ValueError(f"Missing or empty manifest.json in {chap_dir}")
    if plan.project_slug != manifest.project_slug or str(plan.chapter_number) != str(manifest.chapter_number):
        raise ValueError("PanelPlan project/chapter mismatch with manifest.json")

    by_id: Dict[str, PanelItem] = {p.panel_id: p for p in manifest.panels}
    order_idx: Dict[str, int] = {p.panel_id: i for i, p in enumerate(manifest.panels)}

    seen: List[str] = []
    for item in plan.items:
        for pid in item.source_panel_ids:
            if pid not in by_id:
                raise ValueError(f"Unknown panel_id in panel_plan: {pid}")
            seen.append(pid)
        if item.op == PanelPlanOp.MERGE:
            idxs = [order_idx[pid] for pid in item.source_panel_ids]
            if sorted(idxs) != list(range(min(idxs), max(idxs) + 1)):
                raise ValueError(f"merge source_panel_ids must be contiguous: {item.source_panel_ids}")

    if len(seen) != len(set(seen)):
        raise ValueError("Duplicate panel_id references in panel_plan")
    missing = [p.panel_id for p in manifest.panels if p.panel_id not in set(seen)]
    if missing:
        raise ValueError(f"panel_plan must cover all manifest panels; missing: {missing[:10]}")

    raw_dir = chap_dir / "raw"
    valid_exts = {".jpg", ".jpeg", ".png", ".webp"}
    raw_files = [f for f in raw_dir.iterdir() if f.suffix.lower() in valid_exts and f.is_file()]
    segmenter = StreamingSegmenter(target_width=target_width)
    page_maps = segmenter.build_page_maps(raw_files)
    _, row_vars = segmenter.compute_1d_profiles(page_maps)

    panels_dir = chap_dir / "panels"
    panels_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir = chap_dir / ".repack_panels_tmp"
    if tmp_dir.exists():
        for f in tmp_dir.glob("*"):
            f.unlink(missing_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    new_panels: List[PanelItem] = []
    seq = 1

    for item in plan.items:
        src_items = [by_id[pid] for pid in item.source_panel_ids]
        v_starts = [p.vcs_start for p in src_items if p.vcs_start is not None]
        v_ends = [p.vcs_end for p in src_items if p.vcs_end is not None]
        if len(v_starts) != len(src_items) or len(v_ends) != len(src_items):
            raise ValueError(f"Missing VCS coordinates on source panels {item.source_panel_ids}; re-run segment first.")

        v_top, v_bottom = min(v_starts), max(v_ends)
        if item.op in (PanelPlanOp.SPLIT, PanelPlanOp.SPLIT_AT):
            ratios = item.cut_ratios if item.cut_ratios is not None else item.split_ratios
            sub_intervals = _compute_split_intervals(
                v_top, v_bottom, item.split_parts, ratios, row_vars
            )
        else:
            sub_intervals = [(v_top, v_bottom)]

        for sub_top, sub_bottom in sub_intervals:
            extracted = segmenter.extract_vcs_crop(page_maps, sub_top, sub_bottom)
            if extracted is None:
                continue
            crop_img, source_name, crop_box = extracted
            final_w, final_h = crop_img.size

            panel_id = f"panel_{seq:03d}"
            filename = f"{panel_id}.jpg"
            out_file = tmp_dir / filename
            crop_img.save(out_file, "JPEG", quality=90, progressive=True)
            crop_img.close()

            meta: Dict[str, Any] = {
                "classification_group": item.group.value,
                "repack_op": item.op.value,
                "source_raw_panels": item.source_panel_ids,
            }
            if item.tall_motion_hint:
                meta["tall_motion_hint"] = item.tall_motion_hint

            notes = item.reason or f"repack:{item.op.value}"
            if item.group == PanelStatus.CONTEXT_ONLY and "text_only" not in notes.lower():
                notes = f"text_only | {notes}"

            new_panels.append(
                PanelItem(
                    panel_id=panel_id,
                    sequence_number=seq,
                    filename=filename,
                    relative_path=f"panels/{filename}",
                    source_image=source_name,
                    crop_box=crop_box,
                    vcs_start=sub_top,
                    vcs_end=sub_bottom,
                    width=final_w,
                    height=final_h,
                    aspect_ratio=round(float(final_w) / float(final_h), 3) if final_h > 0 else 1.0,
                    sha256=calculate_sha256(out_file),
                    status=item.group,
                    evidence_metadata=meta,
                    notes=notes,
                )
            )
            seq += 1

    # Atomically swap repacked panels into panels/
    for old_f in panels_dir.glob("*"):
        old_f.unlink(missing_ok=True)
    for new_f in tmp_dir.glob("*.jpg"):
        new_f.replace(panels_dir / new_f.name)
    tmp_dir.rmdir()

    # Regenerate clean Contact Sheets containing only non-SKIP panels (Group 1 & Group 2)
    sheets_dir = chap_dir / "contact_sheets"
    sheets_dir.mkdir(parents=True, exist_ok=True)
    for old_s in sheets_dir.glob("*"):
        old_s.unlink(missing_ok=True)

    reviewable_panels = [p for p in new_panels if p.status != PanelStatus.SKIP]
    sheet_gen = ContactSheetGenerator(cell_size=cell_size, columns=grid_cols, rows=grid_rows)
    contact_sheets = sheet_gen.generate_sheets(reviewable_panels, panels_dir, sheets_dir)

    updated_manifest = PanelManifest(
        schema_version=manifest.schema_version,
        project_slug=manifest.project_slug,
        chapter_number=manifest.chapter_number,
        source_url=manifest.source_url,
        created_at=utc_iso_now(),
        total_raw_images=manifest.total_raw_images,
        total_panels=len(new_panels),
        raw_images=manifest.raw_images,
        panels=new_panels,
        contact_sheets=contact_sheets,
        panel_plan_applied=True,
    )
    atomic_write_json(chap_dir / "manifest.json", updated_manifest)

    counts = {
        "active": sum(1 for p in new_panels if p.status == PanelStatus.ACTIVE),
        "context_only": sum(1 for p in new_panels if p.status == PanelStatus.CONTEXT_ONLY),
        "skip": sum(1 for p in new_panels if p.status == PanelStatus.SKIP),
    }
    return {
        "project_slug": manifest.project_slug,
        "chapter_number": manifest.chapter_number,
        "total_panels": len(new_panels),
        "counts_by_group": counts,
        "total_contact_sheets": len(contact_sheets),
        "panel_plan_applied": True,
    }


def inspect_panels_with_candidates(
    chapter_dir: Path,
    panel_ids: List[str],
    max_candidates: int = 8,
) -> Dict[str, Any]:
    """
    Renders readable inspection images for flagged panels with numbered candidate cut lines
    (at local row-variance minima) so VLM can choose exact `cut_ratios` for `split_at`.
    """
    from PIL import Image, ImageDraw

    chap_dir = Path(chapter_dir)
    manifest = load_manifest(chap_dir)
    if manifest is None:
        raise ValueError(f"Missing manifest.json in {chap_dir}")
    by_id = {p.panel_id: p for p in manifest.panels}
    inspect_dir = chap_dir / "panel_inspect"
    inspect_dir.mkdir(parents=True, exist_ok=True)

    results: List[Dict[str, Any]] = []
    for pid in panel_ids:
        norm_pid = pid if pid in by_id else Path(pid).stem
        if norm_pid not in by_id:
            raise ValueError(f"Unknown panel_id: {pid}")
        panel = by_id[norm_pid]
        img_path = chap_dir / panel.relative_path
        with Image.open(img_path) as im:
            rgb = im.convert("RGB")
            w, h = rgb.size
            gray = np.asarray(rgb.convert("L"), dtype=np.float32)
            row_var = np.var(gray, axis=1)

            # Find well-spaced local minima (ignoring top/bottom 10% edges)
            lo, hi = int(h * 0.10), int(h * 0.90)
            min_sep = max(60, int(h * 0.08))
            order = np.argsort(row_var[lo:hi]) + lo
            chosen_y: List[int] = []
            for y in order:
                if all(abs(int(y) - cy) >= min_sep for cy in chosen_y):
                    chosen_y.append(int(y))
                    if len(chosen_y) >= max_candidates:
                        break
            chosen_y.sort()

            draw = ImageDraw.Draw(rgb)
            candidates: List[Dict[str, Any]] = []
            for idx, cy in enumerate(chosen_y, start=1):
                ratio = round(cy / float(h), 3)
                candidates.append({"index": idx, "y": cy, "ratio": ratio})
                draw.line([(0, cy), (w, cy)], fill=(255, 60, 0), width=4)
                draw.rectangle([(10, max(0, cy - 26)), (170, cy)], fill=(255, 60, 0))
                draw.text((16, max(2, cy - 22)), f"CUT #{idx} ({ratio:.2f})", fill=(255, 255, 255))

            out_img = inspect_dir / f"inspect_{norm_pid}.jpg"
            rgb.save(out_img, "JPEG", quality=88)

        results.append({
            "panel_id": norm_pid,
            "width": w,
            "height": h,
            "inspect_image": str(out_img.relative_to(chap_dir)),
            "candidates": candidates,
        })
    return {"chapter_number": manifest.chapter_number, "inspected": results}

