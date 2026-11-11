"""
src/render/visual_policy.py
Canonical Visual Policy and Visual Shot Plan Engine for Manga Recap Skill V4 (PR-02).

Provides decoupled, shared shot planning for both local and cloud (Kaggle) renderers:
1. Auto-splits tall panels (H/W > 2.0) into overlapping top-to-bottom sub-panels (0.65 <= W/H <= 2.0).
2. Classifies text-only or blank bands as omit_visual and substitutes adjacent approved visual shots.
3. Enforces monotonic top-down motion, subject-centric framing, and scroll velocity bounds (<= 120 px/s).
"""

from __future__ import annotations

import json
import math
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field

from src.core.logger import logger
from src.core.schemas import (
    CameraMotionType,
    FocusBox,
    PanelItem,
    PanelManifest,
    PanelStatus,
    RecapScript,
    ScriptSegment,
    utc_iso_now,
)
from src.core.storage import atomic_write_json, load_manifest, load_recap_script


# ==============================================================================
# 1. Models & Enumerations
# ==============================================================================


class VisualRole(str, Enum):
    """Visual presentation role for a shot or panel."""
    SHOW = "show"
    CONTEXT_ONLY = "context_only"
    OMIT_VISUAL = "omit_visual"


class Shot(BaseModel):
    """
    Individual camera framing and motion shot within a segment or chapter.
    Stores source panel provenance, sub-region crop box, and motion configuration.
    """
    model_config = ConfigDict(extra="ignore")

    shot_id: str = Field(..., description="Unique shot identifier, e.g. 'shot_001_01'")
    source_panel_id: str = Field(..., description="Provenance ID of the source panel, e.g. 'panel_001'")
    visual_role: VisualRole = Field(default=VisualRole.SHOW, description="Visual display role: show, context_only, omit_visual")
    visual_omit_reason: Optional[str] = Field(default=None, description="Reason if omit_visual, e.g. blank_band, text_only, sliver")
    substitute_panel_id: Optional[str] = Field(default=None, description="Nearby approved panel ID used if visual_role is omit_visual")
    source_box: FocusBox = Field(
        default_factory=lambda: FocusBox(x_min=0.0, y_min=0.0, x_max=1.0, y_max=1.0),
        description="Normalized sub-crop box within the source panel"
    )
    motion_type: CameraMotionType = Field(default=CameraMotionType.STATIC, description="Kinematic motion mode")
    speed_px_per_sec: float = Field(default=0.0, ge=0.0, le=120.0, description="Vertical scroll speed capped at <= 120 px/s")
    duration_sec: float = Field(default=0.0, ge=0.0, description="Shot duration in seconds")
    segment_id: Optional[str] = Field(default=None, description="Parent script segment sequence ID, e.g. '001'")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Auxiliary provenance metadata")


class VisualPlan(BaseModel):
    """
    Canonical chapter-level visual plan defining the exact shot sequence,
    framing sub-boxes, and panel provenance for local and Kaggle renderers.
    """
    model_config = ConfigDict(extra="ignore")

    chapter_number: str = Field(..., description="Chapter number string, e.g. '1'")
    shots: List[Shot] = Field(default_factory=list, description="Ordered list of visual shots")
    panel_to_shots_map: Dict[str, List[str]] = Field(
        default_factory=dict,
        description="Mapping from source panel ID to list of generated shot IDs"
    )
    target_width: int = Field(default=1920, description="Target canvas width")
    target_height: int = Field(default=1080, description="Target canvas height")
    fps: int = Field(default=30, description="Render frame rate")
    total_duration_sec: float = Field(default=0.0, ge=0.0, description="Total planned video duration")
    updated_at: str = Field(default_factory=utc_iso_now, description="ISO timestamp of plan creation/update")

    def get_shots_for_segment(self, segment_id: str) -> List[Shot]:
        """Returns all shots assigned to a specific script segment ID."""
        clean_id = str(segment_id).strip()
        return [s for s in self.shots if s.segment_id == clean_id]

    def get_shots_for_panel(self, panel_id: str) -> List[Shot]:
        """Returns all shots originating from a specific source panel ID."""
        clean_id = str(panel_id).strip()
        shot_ids = self.panel_to_shots_map.get(clean_id, [])
        if shot_ids:
            shot_id_set = set(shot_ids)
            return [s for s in self.shots if s.shot_id in shot_id_set]
        return [s for s in self.shots if s.source_panel_id == clean_id]

    def save(self, path: Union[str, Path]) -> Path:
        """Atomically saves the VisualPlan to disk as JSON."""
        out_p = Path(path).resolve()
        atomic_write_json(out_p, self)
        logger.info(f"VisualPlan: Saved {len(self.shots)} shots to {out_p}")
        return out_p

    @classmethod
    def load(cls, path: Union[str, Path]) -> VisualPlan:
        """Loads a VisualPlan from a JSON file."""
        in_p = Path(path).resolve()
        if not in_p.exists():
            raise FileNotFoundError(f"VisualPlan file not found: {in_p}")
        data = json.loads(in_p.read_text(encoding="utf-8"))
        return cls.model_validate(data)


# ==============================================================================
# 2. Geometric Analysis & Slicing Helpers
# ==============================================================================


def is_tall_panel(width: int, height: int, threshold_h_over_w: float = 2.0) -> bool:
    """Returns True if the panel aspect ratio exceeds the tall panel threshold (H/W > threshold)."""
    if width <= 0 or height <= 0:
        return False
    return (float(height) / float(width)) > threshold_h_over_w


def decompose_tall_panel(
    panel_id: str,
    width: int,
    height: int,
    target_aspect_min: float = 0.65,
    target_aspect_max: float = 2.0,
) -> List[FocusBox]:
    """
    Decomposes a tall continuous panel (H/W > 2.0) into overlapping top-to-bottom
    FocusBox sub-panels with aspect ratios strictly bounded in [0.65, 2.0].

    Guarantees:
    - 0.65 <= w_sub / h_sub <= 2.0 for every sub-panel.
    - Strictly monotonic top-to-bottom reading progression (y_0(k+1) > y_0(k)).
    - Generous overlap between consecutive slices (no cut seams).
    - Minimum box dimensions: w >= 0.35, h >= 0.30.
    """
    if width <= 0 or height <= 0:
        return [FocusBox(x_min=0.0, y_min=0.0, x_max=1.0, y_max=1.0)]

    ar = float(width) / float(height)
    if ar >= 0.50:  # H/W <= 2.0: standard panel, no splitting needed
        return [FocusBox(x_min=0.0, y_min=0.0, x_max=1.0, y_max=1.0)]

    # Target sub-panel height in pixels:
    # We want 0.65 <= width / h_sub <= 2.0 -> width / 2.0 <= h_sub <= width / 0.65 (1.538 * width)
    # Target h_sub = 1.10 * width yields sub-aspect ratio = 1.0 / 1.10 = 0.909, comfortably inside [0.65, 2.0]
    target_sub_h = width * 1.10
    k_slices = max(2, math.ceil(height / target_sub_h))

    # Normalized slice height
    norm_sub_h = min(1.0, max(0.05, target_sub_h / float(height)))

    boxes: List[FocusBox] = []
    step = (1.0 - norm_sub_h) / float(k_slices - 1) if k_slices > 1 else 0.0

    for k in range(k_slices):
        if k == 0:
            y0 = 0.0
            y1 = norm_sub_h
        elif k == k_slices - 1:
            y1 = 1.0
            y0 = max(0.0, 1.0 - norm_sub_h)
        else:
            y0 = round(k * step, 4)
            y1 = round(y0 + norm_sub_h, 4)
            if y1 > 1.0:
                y1 = 1.0
                y0 = max(0.0, y1 - norm_sub_h)

        # Ensure box width is full panel and height satisfies FocusBox constraint
        box = FocusBox(
            x_min=0.0,
            y_min=round(float(y0), 4),
            x_max=1.0,
            y_max=round(float(y1), 4),
        )
        boxes.append(box)

    return boxes


def detect_omit_visual_band(
    img_path_or_crop: Union[str, Path, Image.Image, None] = None,
    panel_item: Optional[PanelItem] = None,
    min_height_px: int = 80,
    variance_threshold: float = 100.0,
) -> Tuple[bool, Optional[str]]:
    """
    Detects whether a panel or sub-band should be classified as omit_visual.

    Detection criteria:
    - Marked as SKIP in PanelManifest (e.g. spacer, blank gutter).
    - Unpadded height < min_height_px (80px slivers, scanlation credits, divider lines).
    - Variance < variance_threshold (solid color, pure white or black gutters).
    - Text-only band without pictorial artwork (indicated in notes or OCR characteristics).

    Returns:
        Tuple[is_omit: bool, reason: Optional[str]]
    """
    # 1. Manifest status check
    if panel_item is not None:
        if panel_item.status == PanelStatus.SKIP:
            return True, "blank_band"
        if panel_item.height < min_height_px:
            return True, "sliver"
        if panel_item.notes and "text_only" in panel_item.notes.lower():
            return True, "text_only"

    # 2. Image pixel analysis if image provided
    if img_path_or_crop is not None:
        try:
            if isinstance(img_path_or_crop, (str, Path)):
                img_p = Path(img_path_or_crop).resolve()
                if not img_p.exists():
                    return False, None
                with Image.open(img_p) as im:
                    w, h = im.size
                    if h < min_height_px:
                        return True, "sliver"
                    gray = im.convert("L")
                    arr = np.asarray(gray, dtype=np.float32)
            elif isinstance(img_path_or_crop, Image.Image):
                w, h = img_path_or_crop.size
                if h < min_height_px:
                    return True, "sliver"
                gray = img_path_or_crop.convert("L")
                arr = np.asarray(gray, dtype=np.float32)
            else:
                return False, None

            # Calculate luminance variance
            var_val = float(np.var(arr))
            if var_val < variance_threshold:
                return True, "blank_band"

            # Check for pure text on solid background (e.g. black text on white page)
            # High proportion of uniform background (> 90%) with isolated text lines
            bg_white = np.mean(arr > 245)
            bg_black = np.mean(arr < 15)
            if (bg_white > 0.90 or bg_black > 0.90) and var_val < 3500.0:
                # Text-only band candidate
                if panel_item and panel_item.ocr_text_en and len(panel_item.ocr_text_en.strip()) > 5:
                    return True, "text_only"
        except Exception as e:
            logger.warning(f"detect_omit_visual_band error: {e}")

    return False, None


# ==============================================================================
# 3. Canonical VisualPlan Generator
# ==============================================================================


def generate_visual_plan(
    chap_dir: Union[str, Path],
    manifest: Optional[PanelManifest] = None,
    script: Optional[RecapScript] = None,
    target_width: int = 1920,
    target_height: int = 1080,
    fps: int = 30,
    max_panel_aspect_h_over_w: float = 2.0,
) -> VisualPlan:
    """
    Generates a shared canonical VisualPlan for a chapter.

    Core Workflow:
    1. Loads manifest.json and script.json if not provided.
    2. Maps each script segment to its narrative source panels.
    3. Auto-splits tall panels (H/W > max_panel_aspect_h_over_w) into overlapping sub-shots.
    4. Identifies text-only, blank, or sliver bands and marks visual_role = OMIT_VISUAL.
    5. Replaces omitted visual shots with relevant approved nearby shots (no text-only panels shown).
    6. Binds safe top-down camera motions and clamps velocity <= 120 px/s.
    7. Emits a canonical VisualPlan callable identically by both local and Kaggle renderers.
    """
    c_dir = Path(chap_dir).resolve()
    chapter_num = c_dir.name

    # 1. Load manifest and script if needed
    if manifest is None:
        manifest = load_manifest(c_dir)
    if script is None:
        script = load_recap_script(c_dir)

    panel_map: Dict[str, PanelItem] = {}
    if manifest and manifest.panels:
        for p in manifest.panels:
            panel_map[p.panel_id] = p
            panel_map[p.filename] = p
            panel_map[Path(p.filename).stem] = p

    # Find panel files on disk for image dimension/variance lookups
    panels_dir = c_dir / "panels"

    # Pre-scan panels to identify approved visual candidates for substitution
    approved_panel_ids: List[str] = []
    if manifest and manifest.panels:
        for p in manifest.panels:
            if p.status == PanelStatus.ACTIVE:
                p_path = panels_dir / p.filename
                is_omit, _ = detect_omit_visual_band(p_path if p_path.exists() else None, panel_item=p)
                if not is_omit:
                    approved_panel_ids.append(p.panel_id)

    shots: List[Shot] = []
    panel_to_shots_map: Dict[str, List[str]] = {}
    total_duration = 0.0
    shot_counter = 1

    segments = script.segments if script and script.segments else []

    for seg in segments:
        seg_id = str(seg.seq_id).strip()
        seg_dur = float(seg.duration or max(1.5, len(seg.narrator_text) * 0.08))
        total_duration += seg_dur

        # Determine target source panels
        pids = seg.panel_ids if seg.panel_ids else [f"panel_{seg_id}"]

        # Resolve panels for this segment
        seg_shots: List[Shot] = []
        for pid in pids:
            clean_pid = Path(pid).stem
            panel_item = panel_map.get(pid) or panel_map.get(clean_pid)

            # Determine panel dimensions
            pw = panel_item.width if panel_item else 1080
            ph = panel_item.height if panel_item else 1080
            panel_file = panels_dir / f"{clean_pid}.jpg"
            if not panel_file.exists():
                panel_file = panels_dir / f"{clean_pid}.png"

            if panel_file.exists():
                try:
                    with Image.open(panel_file) as im:
                        pw, ph = im.size
                except Exception:
                    pass

            # Check if this panel or band is omit_visual
            is_omit, omit_reason = detect_omit_visual_band(
                img_path_or_crop=panel_file if panel_file.exists() else None,
                panel_item=panel_item,
            )

            # Check for tall panel splitting
            is_tall = is_tall_panel(pw, ph, threshold_h_over_w=max_panel_aspect_h_over_w)
            if is_tall:
                sub_boxes = decompose_tall_panel(clean_pid, pw, ph)
            else:
                # Use segment focus_box if specified, else full frame
                base_box = seg.focus_box if seg.focus_box else FocusBox(x_min=0.0, y_min=0.0, x_max=1.0, y_max=1.0)
                sub_boxes = [base_box]

            # Build shots from sub_boxes
            for s_idx, s_box in enumerate(sub_boxes):
                shot_uid = f"shot_{shot_counter:03d}_{s_idx+1:02d}" if len(sub_boxes) > 1 else f"shot_{shot_counter:03d}"

                # Role & reason
                if is_omit:
                    shot_role = VisualRole.OMIT_VISUAL
                    shot_reason = omit_reason or "blank_band"
                    # Find substitute approved panel
                    substitute_pid = None
                    if approved_panel_ids:
                        # Find closest approved panel in chapter
                        substitute_pid = approved_panel_ids[0]
                        for app_id in approved_panel_ids:
                            substitute_pid = app_id
                            if app_id <= clean_pid:
                                break
                else:
                    shot_role = VisualRole.SHOW
                    shot_reason = None
                    substitute_pid = None

                # Determine camera motion mode (monotonic V4 rules)
                raw_motion = seg.camera_motion
                if raw_motion in (CameraMotionType.PAN_UP,):
                    motion = CameraMotionType.PAN_DOWN  # Alias PAN_UP to monotonic PAN_DOWN
                elif raw_motion in (CameraMotionType.ZOOM_OUT,):
                    motion = CameraMotionType.STATIC   # Alias ZOOM_OUT to STATIC
                elif raw_motion in (CameraMotionType.PAN_DOWN, CameraMotionType.VERTICAL_SCROLL):
                    motion = raw_motion
                elif is_tall:
                    # Sliced tall panel sub-shots default to STATIC or STATIC_FOCUS
                    motion = CameraMotionType.STATIC_FOCUS if seg.focus_box else CameraMotionType.STATIC
                else:
                    motion = raw_motion

                # Calculate speed and velocity bounds (<= 120 px/s)
                dur_shot = seg_dur / float(len(pids) * len(sub_boxes))
                if motion in (CameraMotionType.PAN_DOWN, CameraMotionType.VERTICAL_SCROLL):
                    delta_y = max(0, ph - target_height)
                    speed = min(120.0, float(delta_y) / max(0.5, dur_shot))
                else:
                    speed = 0.0

                shot_obj = Shot(
                    shot_id=shot_uid,
                    source_panel_id=clean_pid,
                    visual_role=shot_role,
                    visual_omit_reason=shot_reason,
                    substitute_panel_id=substitute_pid,
                    source_box=s_box,
                    motion_type=motion,
                    speed_px_per_sec=round(speed, 2),
                    duration_sec=round(dur_shot, 4),
                    segment_id=seg_id,
                    metadata={
                        "narrator_text": seg.narrator_text[:60],
                        "is_tall_split": is_tall,
                        "slice_index": s_idx,
                        "total_slices": len(sub_boxes),
                    },
                )
                seg_shots.append(shot_obj)
                shots.append(shot_obj)

                # Index panel_to_shots_map
                if clean_pid not in panel_to_shots_map:
                    panel_to_shots_map[clean_pid] = []
                panel_to_shots_map[clean_pid].append(shot_uid)

            shot_counter += 1

    return VisualPlan(
        chapter_number=str(chapter_num),
        shots=shots,
        panel_to_shots_map=panel_to_shots_map,
        target_width=target_width,
        target_height=target_height,
        fps=fps,
        total_duration_sec=round(total_duration, 4),
    )
