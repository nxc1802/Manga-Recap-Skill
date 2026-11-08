"""
src/render/camera.py
Cinematic Camera Motion Generator for Video Engine V2.
Preserves artwork aspect ratios (circle test AR = 1.000) and synchronizes motion to audio duration.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Optional, Tuple, Union

from src.core.logger import logger
from src.core.schemas import CameraMotionType, FocusBox, VideoMode


class CameraMotionGenerator:
    """
    Generates dynamic FFmpeg video filter expressions for cinematic camera motions
    strictly synchronized to measured audio durations with aspect ratio preservation.
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
        """Normalizes motion type strings, legacy names, or enums to canonical CameraMotionType."""
        if motion_type is None:
            return CameraMotionType.STATIC

        if isinstance(motion_type, CameraMotionType):
            return motion_type

        motion_str = str(motion_type).strip().lower()
        alias_map = {
            "static": CameraMotionType.STATIC,
            "pan_left": CameraMotionType.PAN_LEFT,
            "left": CameraMotionType.PAN_LEFT,
            "pan_horizontal": CameraMotionType.PAN_LEFT,
            "horizontal": CameraMotionType.PAN_LEFT,
            "pan_right": CameraMotionType.PAN_RIGHT,
            "right": CameraMotionType.PAN_RIGHT,
            "pan_up": CameraMotionType.PAN_UP,
            "up": CameraMotionType.PAN_UP,
            "pan_down": CameraMotionType.PAN_DOWN,
            "down": CameraMotionType.PAN_DOWN,
            "pan_vertical": CameraMotionType.PAN_DOWN,
            "vertical": CameraMotionType.PAN_DOWN,
            "zoom_in": CameraMotionType.ZOOM_IN,
            "zoom": CameraMotionType.ZOOM_IN,
            "zoom_out": CameraMotionType.ZOOM_OUT,
            "focus_crop": CameraMotionType.FOCUS_CROP,
            "crop": CameraMotionType.FOCUS_CROP,
            "static_focus": CameraMotionType.STATIC_FOCUS,
            "vertical_scroll": CameraMotionType.VERTICAL_SCROLL,
            "scroll": CameraMotionType.VERTICAL_SCROLL,
            "content_aware_scroll": CameraMotionType.CONTENT_AWARE_SCROLL,
            "subject_pan": CameraMotionType.SUBJECT_PAN,
        }
        return alias_map.get(motion_str, CameraMotionType.STATIC)

    def calculate_focus_center(
        self,
        focus_box: Optional[FocusBox] = None,
        focus_x: Optional[float] = None,
        focus_y: Optional[float] = None,
        crop_scale: Optional[float] = None,
    ) -> Tuple[float, float, float]:
        """
        Extracts normalized center (fx, fy) and scale_ratio (0.05 to 1.0).
        Supports FocusBox or legacy (focus_x, focus_y, crop_scale) percentage tuples.
        """
        if focus_box is not None:
            fx = (focus_box.x_min + focus_box.x_max) / 2.0
            fy = (focus_box.y_min + focus_box.y_max) / 2.0
            scale_ratio = max(0.05, min(1.0, focus_box.x_max - focus_box.x_min))
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
        """Resolves normalized (fx, fy, scale) from FocusBox, legacy dict, or None."""
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

    def calculate_pre_crop(
        self,
        img_w: int,
        img_h: int,
        fx: float = 0.5,
        fy: float = 0.5,
    ) -> Tuple[int, int, int, int]:
        """
        Calculates a 16:9 pre-crop box (w, h, x, y) centered around (fx, fy)
        that completely prevents aspect ratio distortion when scaling to target resolution.
        Safely bounds vertical center away from extreme top/bottom gutters (chat boxes).
        """
        ar_in = img_w / img_h
        if ar_in >= self.target_ar:
            # Wider than 16:9: take full height, fit width
            base_h = self._ensure_even(img_h)
            base_w = self._ensure_even(round(base_h * self.target_ar))
            if base_w > img_w:
                base_w = self._ensure_even(img_w)
                base_h = self._ensure_even(round(base_w / self.target_ar))
            max_x = max(0, img_w - base_w)
            crop_x = self._ensure_even(round(max(0, min(max_x, (fx * img_w) - base_w / 2))))
            crop_y = 0
        else:
            # Taller than 16:9 (portrait): take full width, fit height
            base_w = self._ensure_even(img_w)
            base_h = self._ensure_even(round(base_w / self.target_ar))
            if base_h > img_h:
                base_h = self._ensure_even(img_h)
                base_w = self._ensure_even(round(base_h * self.target_ar))
            max_y = max(0, img_h - base_h)
            crop_x = 0
            # Safety: keep fy in [0.20, 0.80] character action safe zone
            safe_fy = max(0.20, min(0.80, fy))
            crop_y = self._ensure_even(round(max(0, min(max_y, (safe_fy * img_h) - base_h / 2))))

        return base_w, base_h, crop_x, crop_y

    def build_filterchain(
        self,
        img_w: int,
        img_h: int,
        duration_sec: float,
        motion_type: Union[CameraMotionType, str, None] = CameraMotionType.STATIC,
        video_mode: Optional[Union[VideoMode, str]] = None,
        focus_box: Optional[FocusBox] = None,
        focus_x: Optional[float] = None,
        focus_y: Optional[float] = None,
        crop_scale: Optional[float] = None,
        allow_animation: bool = True,
    ) -> Tuple[str, bool]:
        """
        Builds the complete FFmpeg video filter expression for the specified camera motion mode.
        If allow_animation is False, forces CameraMotionType.STATIC mode.
        Returns:
            Tuple[filter_expression_string, is_zoompan_based]
        """
        T = max(0.5, float(duration_sec or 0.0))
        N = round(self.fps * T)
        motion = self.normalize_motion_type(motion_type)
        has_focus = (focus_box is not None) or (focus_x is not None and focus_y is not None)
        if not allow_animation:
            if motion in (CameraMotionType.FOCUS_CROP, CameraMotionType.STATIC_FOCUS) or has_focus:
                motion = CameraMotionType.STATIC_FOCUS
            else:
                motion = CameraMotionType.STATIC

        # Guard against excessive crop on tall continuous webtoon panels (H > 2W)
        if img_h > 2 * img_w and motion in (
            CameraMotionType.ZOOM_IN,
            CameraMotionType.ZOOM_OUT,
            CameraMotionType.PAN_LEFT,
            CameraMotionType.PAN_RIGHT,
            CameraMotionType.SUBJECT_PAN,
        ):
            logger.info(
                f"Panel is tall webtoon ({img_w}x{img_h}, H > 2W); diverting {motion.value} to STATIC to prevent content crop."
            )
            motion = CameraMotionType.STATIC

        fx, fy, scale_ratio = self.calculate_focus_center(focus_box, focus_x, focus_y, crop_scale)
        ar_in = img_w / float(img_h) if img_h > 0 else 1.0

        # Resolve VideoMode
        resolved_vmode = video_mode
        if isinstance(resolved_vmode, str):
            try:
                resolved_vmode = VideoMode(resolved_vmode.lower())
            except ValueError:
                resolved_vmode = VideoMode.AUTO

        if resolved_vmode is None or resolved_vmode == VideoMode.AUTO:
            if ar_in >= 1.3:
                resolved_vmode = VideoMode.LANDSCAPE_FIT
            else:
                resolved_vmode = VideoMode.PORTRAIT_PADDED

        # ----------------------------------------------------------------------
        # 1. STATIC
        # ----------------------------------------------------------------------
        if motion == CameraMotionType.STATIC:
            if abs(ar_in - self.target_ar) < 0.05 and (video_mode is None or video_mode == VideoMode.AUTO or video_mode == "auto"):
                # Panel is already practically 16:9 and in auto mode
                return f"scale={self.target_width}:{self.target_height},fps={self.fps}", False

            if resolved_vmode == VideoMode.LANDSCAPE_FIT:
                filter_str = (
                    f"scale=w={self.target_width}:h={self.target_height}:force_original_aspect_ratio=decrease,"
                    f"pad={self.target_width}:{self.target_height}:(ow-iw)/2:(oh-ih)/2:black,fps={self.fps}"
                )
                return filter_str, False

            if resolved_vmode == VideoMode.PORTRAIT_VERTICAL:
                filter_str = (
                    f"scale=-2:{self.target_height},"
                    f"pad={self.target_width}:{self.target_height}:(ow-iw)/2:(oh-ih)/2:black,fps={self.fps}"
                )
                return filter_str, False

            # VideoMode.PORTRAIT_PADDED (default for portrait/square):
            # Dual-layer composite: blurred background + centered aspect-preserved foreground
            filter_str = (
                f"split=2[bg_in][fg_in];"
                f"[bg_in]scale={self.target_width}:{self.target_height}:force_original_aspect_ratio=increase,"
                f"crop={self.target_width}:{self.target_height},boxblur=25:5[bg];"
                f"[fg_in]scale={self.target_width}:{self.target_height}:force_original_aspect_ratio=decrease[fg];"
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2,fps={self.fps}"
            )
            return filter_str, False

        # ----------------------------------------------------------------------
        # 2. PAN_RIGHT / PAN_LEFT / SUBJECT_PAN (Horizontal Pan)
        # ----------------------------------------------------------------------
        if motion in (CameraMotionType.PAN_RIGHT, CameraMotionType.PAN_LEFT, CameraMotionType.SUBJECT_PAN):
            hv = self._ensure_even(img_h)
            wv = self._ensure_even(round(hv * self.target_ar))
            if wv > img_w:
                wv = self._ensure_even(img_w)
                hv = self._ensure_even(round(wv / self.target_ar))

            delta_x = max(0, img_w - wv)
            y_pos = self._ensure_even((img_h - hv) // 2)

            # Cosine ease-in-ease-out kinematics
            if motion in (CameraMotionType.PAN_RIGHT, CameraMotionType.SUBJECT_PAN):
                x_expr = f"'{delta_x}*0.5*(1-cos(PI*t/{T:.3f}))'"
            else:
                x_expr = f"'{delta_x}*0.5*(1+cos(PI*t/{T:.3f}))'"

            filter_str = f"crop={wv}:{hv}:{x_expr}:{y_pos},scale={self.target_width}:{self.target_height},fps={self.fps}"
            return filter_str, False

        # ----------------------------------------------------------------------
        # 3. PAN_DOWN / PAN_UP / VERTICAL_SCROLL / CONTENT_AWARE_SCROLL (Vertical Movement)
        # ----------------------------------------------------------------------
        if motion in (CameraMotionType.PAN_DOWN, CameraMotionType.PAN_UP, CameraMotionType.VERTICAL_SCROLL, CameraMotionType.CONTENT_AWARE_SCROLL):
            wv = self._ensure_even(img_w)
            hv = self._ensure_even(round(wv / self.target_ar))
            if hv > img_h:
                hv = self._ensure_even(img_h)
                wv = self._ensure_even(round(hv * self.target_ar))

            delta_y = max(0, img_h - hv)
            x_pos = self._ensure_even((img_w - wv) // 2)

            if motion == CameraMotionType.VERTICAL_SCROLL:
                # Linear progression for webtoon scrolling
                y_expr = f"'{delta_y}*(t/{T:.3f})'"
            elif motion in (CameraMotionType.PAN_DOWN, CameraMotionType.CONTENT_AWARE_SCROLL):
                # Cosine ease-in-ease-out top to bottom
                y_expr = f"'{delta_y}*0.5*(1-cos(PI*t/{T:.3f}))'"
            else:
                # Cosine ease-in-ease-out bottom to top
                y_expr = f"'{delta_y}*0.5*(1+cos(PI*t/{T:.3f}))'"

            filter_str = f"crop={wv}:{hv}:{x_pos}:{y_expr},scale={self.target_width}:{self.target_height},fps={self.fps}"
            return filter_str, False

        # ----------------------------------------------------------------------
        # 4. ZOOM_IN / ZOOM_OUT
        # ----------------------------------------------------------------------
        if motion in (CameraMotionType.ZOOM_IN, CameraMotionType.ZOOM_OUT):
            base_w, base_h, crop_x, crop_y = self.calculate_pre_crop(img_w, img_h, fx=fx, fy=fy)
            pre_crop = f"crop={base_w}:{base_h}:{crop_x}:{crop_y},"

            z_min = 1.0
            z_max = round(self.max_zoom, 2)
            if motion == CameraMotionType.ZOOM_IN:
                z_expr = f"'{z_min}+({z_max}-{z_min})*(on/{N})'"
            else:
                z_expr = f"'{z_max}-({z_max}-{z_min})*(on/{N})'"

            # Viewport centering on target focal center
            x_expr = f"'(iw-iw/zoom)*{fx:.4f}'"
            y_expr = f"'(ih-ih/zoom)*{fy:.4f}'"

            filter_str = (
                f"{pre_crop}zoompan=z={z_expr}:x={x_expr}:y={y_expr}:"
                f"d={N}:s={self.target_width}x{self.target_height}:fps={self.fps}"
            )
            return filter_str, True

        # ----------------------------------------------------------------------
        # 5. FOCUS_CROP & STATIC_FOCUS
        # ----------------------------------------------------------------------
        if motion in (CameraMotionType.FOCUS_CROP, CameraMotionType.STATIC_FOCUS):
            # Scale around focus center preserving 16:9 aspect ratio
            # Enforce minimum safe scale (0.45) to prevent micro-crops on chat boxes
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

        # Fallback
        return f"scale={self.target_width}:{self.target_height},fps={self.fps}", False
