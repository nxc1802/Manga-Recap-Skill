"""
src/render/camera.py
Deterministic fixed landscape 16:9 camera framing and filter expressions
for Video Engine V7 (`static`, `static_focus`, `slow_pan_down`).
"""

from __future__ import annotations

import math
from typing import Any, Dict, Optional, Tuple, Union

from src.core.schemas import CameraMotionType, FocusBox


class CameraMotionGenerator:
    """
    Generates FFmpeg video filter expressions for supported canonical camera
    modes (`static`, `static_focus`, `slow_pan_down`) in fixed 16:9 landscape framing.
    """

    def __init__(self, target_width: int = 1280, target_height: int = 720, fps: int = 30, max_zoom: float = 1.15):
        self.target_width = self._ensure_even(target_width)
        self.target_height = self._ensure_even(target_height)
        self.fps = fps
        self.max_zoom = min(1.15, float(max_zoom))
        self.min_crop_scale = 0.45
        self.target_ar = self.target_width / self.target_height

    @staticmethod
    def _ensure_even(val: Union[int, float]) -> int:
        """Rounds val to the nearest even integer <= val."""
        ival = int(round(val))
        return ival - (ival % 2)

    def normalize_motion_type(self, motion_type: Union[CameraMotionType, str, None]) -> CameraMotionType:
        """Validates canonical CameraMotionType (`static`, `static_focus`, `slow_pan_down`)."""
        if motion_type is None:
            return CameraMotionType.STATIC
        if isinstance(motion_type, CameraMotionType):
            return motion_type
        motion_str = str(motion_type).strip().lower()
        try:
            return CameraMotionType(motion_str)
        except ValueError as exc:
            raise ValueError(
                f"Unsupported camera motion type {motion_type!r}; "
                "V7 supports 'static', 'static_focus', and 'slow_pan_down' only."
            ) from exc

    def calculate_focus_center(
        self,
        focus_box: Optional[FocusBox] = None,
        focus_x: Optional[float] = None,
        focus_y: Optional[float] = None,
        crop_scale: Optional[float] = None,
    ) -> Tuple[float, float, float]:
        """Extracts normalized center (fx, fy) and scale_ratio (0.05 to 1.0)."""
        if focus_box is not None:
            fx = (focus_box.x_min + focus_box.x_max) / 2.0
            fy = (focus_box.y_min + focus_box.y_max) / 2.0
            scale_ratio = max(0.35, min(1.0, focus_box.x_max - focus_box.x_min))
            return fx, fy, scale_ratio

        if focus_x is not None and focus_y is not None:
            fx = max(0.0, min(1.0, float(focus_x) / 100.0))
            fy = max(0.0, min(1.0, float(focus_y) / 100.0))
            cs = float(crop_scale if crop_scale is not None else 100.0)
            scale_ratio = max(0.05, min(1.0, cs / 100.0))
            return fx, fy, scale_ratio

        return 0.5, 0.5, 1.0

    def resolve_focus_coordinates(
        self,
        img_w: int,
        img_h: int,
        focus: Optional[Union[FocusBox, Dict[str, Any]]] = None,
    ) -> Tuple[float, float, float]:
        """Resolves normalized (fx, fy, scale) from FocusBox, focus dict, or None."""
        if focus is None:
            return 0.5, 0.5, 1.0
        if isinstance(focus, FocusBox):
            return self.calculate_focus_center(focus_box=focus)
        if isinstance(focus, dict):
            return self.calculate_focus_center(
                focus_x=focus.get("focus_x"),
                focus_y=focus.get("focus_y"),
                crop_scale=focus.get("crop_scale"),
            )
        return 0.5, 0.5, 1.0

    def build_filterchain(
        self,
        img_w: int,
        img_h: int,
        duration_sec: float,
        motion_type: Union[CameraMotionType, str, None] = CameraMotionType.STATIC,
        focus_box: Optional[FocusBox] = None,
        focus_x: Optional[float] = None,
        focus_y: Optional[float] = None,
        crop_scale: Optional[float] = None,
        allow_animation: bool = True,
    ) -> Tuple[str, bool]:
        """
        Builds FFmpeg filterchain in fixed 16:9 landscape fit framing.
        When allow_animation is False, downgrades SLOW_PAN_DOWN to STATIC_FOCUS or STATIC.
        """
        T = max(0.5, float(duration_sec or 0.0))
        motion = self.normalize_motion_type(motion_type)
        has_focus = (focus_box is not None) or (focus_x is not None and focus_y is not None)
        if not allow_animation and motion == CameraMotionType.SLOW_PAN_DOWN:
            motion = CameraMotionType.STATIC_FOCUS if has_focus else CameraMotionType.STATIC

        fx, fy, scale_ratio = self.calculate_focus_center(focus_box, focus_x, focus_y, crop_scale)
        ar_in = img_w / float(img_h) if img_h > 0 else 1.0

        if motion == CameraMotionType.STATIC:
            if abs(ar_in - self.target_ar) < 0.05:
                return f"scale={self.target_width}:{self.target_height},fps={self.fps}", False
            filter_str = (
                f"scale=w={self.target_width}:h={self.target_height}:force_original_aspect_ratio=decrease,"
                f"pad={self.target_width}:{self.target_height}:(ow-iw)/2:(oh-ih)/2:black,fps={self.fps}"
            )
            return filter_str, False

        if motion == CameraMotionType.SLOW_PAN_DOWN:
            wv = self._ensure_even(img_w)
            hv = self._ensure_even(round(wv / self.target_ar))
            if hv > img_h:
                hv = self._ensure_even(img_h)
                wv = self._ensure_even(round(hv * self.target_ar))

            delta_y = max(0, img_h - hv)
            x_pos = self._ensure_even((img_w - wv) // 2)
            max_peak_speed = 120.0
            max_delta_y = self._ensure_even(int(max_peak_speed * (2.0 * T / math.pi)))
            capped_delta_y = min(delta_y, max_delta_y)
            y_expr = f"'{capped_delta_y}*0.5*(1-cos(PI*min(1,max(0,t/{T:.3f}))))'"
            filter_str = f"crop={wv}:{hv}:{x_pos}:{y_expr},scale={self.target_width}:{self.target_height},fps={self.fps}"
            return filter_str, False

        if motion == CameraMotionType.STATIC_FOCUS:
            clamped_scale = max(self.min_crop_scale, min(1.0, scale_ratio))
            if ar_in > self.target_ar:
                crop_h = self._ensure_even(round(img_h * clamped_scale))
                crop_w = self._ensure_even(round(crop_h * self.target_ar))
            else:
                crop_w = self._ensure_even(round(img_w * clamped_scale))
                crop_h = self._ensure_even(round(crop_w / self.target_ar))

            crop_w = min(crop_w, self._ensure_even(img_w))
            crop_h = min(crop_h, self._ensure_even(img_h))

            cx = round(fx * img_w)
            cy = round(fy * img_h)
            x_pos = self._ensure_even(max(0, min(img_w - crop_w, cx - crop_w // 2)))
            y_pos = self._ensure_even(max(0, min(img_h - crop_h, cy - crop_h // 2)))

            filter_str = f"crop={crop_w}:{crop_h}:{x_pos}:{y_pos},scale={self.target_width}:{self.target_height},fps={self.fps}"
            return filter_str, False

        raise ValueError(f"Unsupported camera motion type: {motion!r}")
