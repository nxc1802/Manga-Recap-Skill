"""
src/render/video_engine.py
VideoEngine V2 Coordinator: camera motion filterchains, utterance-level subtitles,
lossless concatenation, dynamic BGM ducking, and ffprobe stream validation.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import textwrap
import threading
import wave
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from src.core.logger import logger
from src.core.schemas import (
    CameraMotionType,
    CheckpointStatus,
    FocusBox,
    RecapScript,
    ScriptSegment,
    StageType,
    VideoMode,
    utc_iso_now,
)
from src.core.storage import (
    StorageFileNotFoundError,
    acquire_lock,
    atomic_write_json,
    cleanup_intermediate_files,
    get_chapter_dir,
    get_project_dir,
    load_chapter_metadata,
    load_manifest,
    load_recap_script,
    read_json,
    save_manifest,
    update_checkpoint,
)
from src.core.utils import (
    DependencyNotFoundError,
    FFmpegExecutionError,
    get_project_data_dir,
    resolve_video_output_path,
    run_ffmpeg,
)
from src.render.audio_mixer import AudioMixer
from src.render.camera import CameraMotionGenerator
from src.render.subtitle import SubtitleCue, SubtitleGenerator, sanitize_subtitle_text
from src.render.validator import StreamValidator, VideoValidationError
from src.tts.engine import get_wav_duration_seconds


def compute_file_sha256(file_path: Union[str, Path], chunk_size: int = 65536) -> str:
    """Computes SHA-256 checksum of a file in streaming chunks for memory safety."""
    p = Path(file_path)
    if not p.exists() or not p.is_file():
        return ""
    hasher = hashlib.sha256()
    with open(p, "rb") as f:
        while chunk := f.read(chunk_size):
            hasher.update(chunk)
    return hasher.hexdigest()


class VideoEngine:
    """
    VideoEngine V2 Coordinator.
    Orchestrates panel clip generation, camera motions, subtitle synchronization,
    audio concatenation & ducking, lossless muxing, and ffprobe validation.
    """

    def __init__(
        self,
        target_width: int = 1280,
        target_height: int = 720,
        fps: int = 30,
        max_workers: int = 2,
        hw_accel: Optional[str] = None,
        encoder: Optional[str] = None,
    ):
        self.width = target_width
        self.height = target_height
        self.fps = fps
        self.max_workers = max(1, int(max_workers))
        self.hw_accel = hw_accel
        self.encoder = encoder
        self.camera_generator = CameraMotionGenerator(target_width, target_height, fps)
        self.subtitle_generator = SubtitleGenerator(max_chars_per_line=42, max_lines=2)
        self.audio_mixer = AudioMixer(target_sample_rate=48000, target_channels=2)
        self.validator = StreamValidator()
        self._canvas_cache: Dict[str, Image.Image] = {}
        self._canvas_lock = threading.Lock()

    def clear_canvas_cache(self) -> None:
        """Clears in-memory composited canvas frame cache."""
        with self._canvas_lock:
            self._canvas_cache.clear()

    def _get_font(self, font_size: int = 26) -> ImageFont.FreeTypeFont:
        """Finds an appropriate system font or falls back to Pillow default."""
        font_search_paths = [
            "/System/Library/Fonts/Supplemental/Arial Unicode MS.ttf",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/Library/Fonts/Arial.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "C:/Windows/Fonts/arial.ttf",
            "C:/Windows/Fonts/segoeui.ttf",
        ]
        for path in font_search_paths:
            if os.path.exists(path):
                try:
                    return ImageFont.truetype(path, font_size)
                except Exception:
                    pass
        return ImageFont.load_default()

    def detect_video_mode(self, img_w: int, img_h: int, forced_mode: Optional[str] = None) -> VideoMode:
        """Backward-compatible video mode detection based on image aspect ratio."""
        if forced_mode and forced_mode != "auto" and forced_mode in [m.value for m in VideoMode]:
            return VideoMode(forced_mode)

        aspect = img_w / float(img_h) if img_h > 0 else 1.0
        if aspect >= 1.5:
            return VideoMode.LANDSCAPE_FIT
        elif aspect <= 0.7:
            return VideoMode.PORTRAIT_VERTICAL
        else:
            return VideoMode.PORTRAIT_PADDED

    def find_optimal_crop_box(self, img: Image.Image, crop_w: int, crop_h: int) -> Tuple[int, int, int, int]:
        """Finds optimal crop bounding box based on detail variance."""
        img_w, img_h = img.size
        if img_w <= crop_w and img_h <= crop_h:
            return 0, 0, img_w, img_h

        gray = img.convert("L")
        arr = np.array(gray, dtype=np.float32)

        if img_h > crop_h:
            row_vars = np.var(arr, axis=1)
            if len(row_vars) > crop_h:
                window_sums = np.convolve(row_vars, np.ones(crop_h), mode="valid")
                best_top = int(np.argmax(window_sums))
                top = min(max(0, best_top), img_h - crop_h)
            else:
                top = 0
        else:
            top = 0

        if img_w > crop_w:
            col_vars = np.var(arr, axis=0)
            if len(col_vars) > crop_w:
                window_sums = np.convolve(col_vars, np.ones(crop_w), mode="valid")
                best_left = int(np.argmax(window_sums))
                left = min(max(0, best_left), img_w - crop_w)
            else:
                left = 0
        else:
            left = 0

        return left, top, left + crop_w, top + crop_h

    def create_subtitle_overlay_png(self, text: str, output_path: Union[str, Path], font_size: int = 26) -> str:
        """Renders wrapped subtitle text onto a transparent PNG overlay."""
        out_p = Path(output_path).resolve()
        out_p.parent.mkdir(parents=True, exist_ok=True)

        scaled_font_size = max(18, int(round(font_size * (self.width / 1280.0))))
        overlay = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
        clean_text = sanitize_subtitle_text(text)
        if not clean_text:
            overlay.save(out_p, "PNG")
            return str(out_p)

        draw = ImageDraw.Draw(overlay)
        font = self._get_font(scaled_font_size)
        wrap_width = max(20, int(round(42 * (self.width / 1280.0))))
        raw_lines = clean_text.split("\n")
        lines = []
        for rl in raw_lines:
            rl_strip = rl.strip()
            if not rl_strip:
                continue
            if len(rl_strip) <= wrap_width:
                lines.append(rl_strip)
            else:
                sublines = textwrap.wrap(rl_strip, width=wrap_width)
                lines.extend(sublines if sublines else [rl_strip])
        max_lines_limit = getattr(self.subtitle_generator, "max_lines_per_block", 2)
        if len(lines) > max_lines_limit:
            lines = lines[:max_lines_limit]
        if not lines:
            overlay.save(out_p, "PNG")
            return str(out_p)

        line_height = scaled_font_size + 10
        line_widths = []
        for line in lines:
            try:
                left, top_b, right, bottom = font.getbbox(line)
                line_widths.append(right - left)
            except Exception:
                line_widths.append(len(line) * (scaled_font_size // 2))

        max_text_w = max(line_widths) if line_widths else 0
        pad_x = 24
        pad_y = 12
        box_w = min(self.width - 40, max_text_w + pad_x * 2)
        total_text_h = len(lines) * line_height
        box_h = total_text_h + pad_y * 2

        box_x0 = (self.width - box_w) // 2
        box_y0 = self.height - box_h - 35
        box_x1 = box_x0 + box_w
        box_y1 = box_y0 + box_h

        draw.rounded_rectangle(
            [box_x0, box_y0, box_x1, box_y1],
            radius=10,
            fill=(0, 0, 0, 185),
        )

        y = box_y0 + pad_y
        for i, line in enumerate(lines):
            text_width = line_widths[i]
            x = (self.width - text_width) // 2
            # Text shadow
            draw.text((x + 1, y + 1), line, font=font, fill=(0, 0, 0, 240))
            # Main white text
            draw.text((x, y), line, font=font, fill=(255, 255, 255, 255))
            y += line_height

        overlay.save(out_p, "PNG")
        return str(out_p)

    def create_panel_image_with_subs(
        self,
        raw_img_path: str,
        text: str,
        output_path: str,
        focus_x: int = 50,
        focus_y: int = 50,
        crop_scale: int = 100,
    ) -> str:
        """Pillow frame creation with centered crop and subtitles (legacy compatibility)."""
        with Image.open(raw_img_path) as img:
            img_w, img_h = img.size
            base_crop_size = min(img_w, img_h)
            crop_size = int(base_crop_size * (crop_scale / 100.0))
            crop_size = min(crop_size, img_w, img_h)

            center_x = int(img_w * (focus_x / 100.0))
            center_y = int(img_h * (focus_y / 100.0))

            left = max(0, min(img_w - crop_size, center_x - crop_size // 2))
            top = max(0, min(img_h - crop_size, center_y - crop_size // 2))

            cropped_sq = img.crop((left, top, left + crop_size, top + crop_size))
            bg = cropped_sq.resize((self.width, self.height), Image.Resampling.LANCZOS)
            bg = bg.filter(ImageFilter.GaussianBlur(radius=30))

            fg = cropped_sq.resize((self.height, self.height), Image.Resampling.LANCZOS)
            paste_x = (self.width - self.height) // 2
            bg.paste(fg, (paste_x, 0))

            final_frame = bg.convert("RGBA")
            if text:
                overlay_path = Path(output_path).with_suffix(".overlay.png")
                self.create_subtitle_overlay_png(text, overlay_path)
                with Image.open(overlay_path) as ov:
                    final_frame = Image.alpha_composite(final_frame, ov)
                if overlay_path.exists():
                    overlay_path.unlink()

            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            final_frame.convert("RGB").save(output_path, "WEBP", quality=90)
            return output_path

    def create_processed_panel_image(
        self,
        raw_img_path: str,
        output_path: str,
        video_mode=VideoMode.PORTRAIT_PADDED,
        focus_x: int = 50,
        focus_y: int = 50,
        crop_scale: int = 100,
    ) -> str:
        """Creates processed panel image based on VideoMode (legacy compatibility)."""
        if isinstance(video_mode, str):
            try:
                video_mode = VideoMode(video_mode)
            except ValueError:
                video_mode = VideoMode.PORTRAIT_PADDED

        with Image.open(raw_img_path) as img:
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGB")
            img_w, img_h = img.size

            if video_mode == VideoMode.LANDSCAPE_FIT:
                scale = min(self.width / float(img_w), self.height / float(img_h))
                new_w = int(img_w * scale)
                new_h = int(img_h * scale)
                fg = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
                bg = img.resize((self.width, self.height), Image.Resampling.LANCZOS).filter(
                    ImageFilter.GaussianBlur(radius=30)
                )
                bg.paste(fg, ((self.width - new_w) // 2, (self.height - new_h) // 2))
                Path(output_path).parent.mkdir(parents=True, exist_ok=True)
                bg.convert("RGB").save(output_path, "WEBP", quality=90)
            elif video_mode == VideoMode.PORTRAIT_PADDED:
                target_aspect = 828.0 / 720.0
                if img_w * target_aspect <= img_h:
                    crop_w = img_w
                    crop_h = int(img_w * target_aspect)
                else:
                    crop_h = img_h
                    crop_w = int(img_h / target_aspect)

                left, top, right, bottom = self.find_optimal_crop_box(img, crop_w, crop_h)
                cropped = img.crop((left, top, right, bottom))
                fg = cropped.resize((720, 828), Image.Resampling.LANCZOS)
                bg = cropped.resize((1280, 828), Image.Resampling.LANCZOS).filter(
                    ImageFilter.GaussianBlur(radius=30)
                )
                bg.paste(fg, ((1280 - 720) // 2, 0))
                Path(output_path).parent.mkdir(parents=True, exist_ok=True)
                bg.convert("RGB").save(output_path, "WEBP", quality=90)
            elif video_mode == VideoMode.PORTRAIT_VERTICAL:
                new_h = max(720, int(720 * (img_h / float(img_w))))
                fg = img.resize((720, new_h), Image.Resampling.LANCZOS)
                bg = img.resize((1280, new_h), Image.Resampling.LANCZOS).filter(
                    ImageFilter.GaussianBlur(radius=30)
                )
                bg.paste(fg, ((1280 - 720) // 2, 0))
                Path(output_path).parent.mkdir(parents=True, exist_ok=True)
                bg.convert("RGB").save(output_path, "WEBP", quality=90)

        return output_path

    def create_static_canvas(
        self,
        img_path: Union[str, Path, Image.Image],
        video_mode: Union[VideoMode, str] = VideoMode.PORTRAIT_PADDED,
        focus_box: Optional[Union[FocusBox, Dict[str, Any]]] = None,
        motion_type: Union[CameraMotionType, str] = CameraMotionType.STATIC,
    ) -> Image.Image:
        """
        Creates a single 16:9 composited canvas image frame (RGB) for static camera hold segments.
        Eliminates redundant multi-frame rasterization and FFmpeg filtergraph overhead.
        Utilizes an in-memory thread-safe cache for repeated panels.
        """
        # Resolve cache key if path provided
        cache_key: Optional[str] = None
        if isinstance(img_path, (str, Path)):
            p = Path(img_path).resolve()
            mtime = p.stat().st_mtime_ns if p.exists() else 0
            cache_key = f"{p}_{mtime}_{self.width}x{self.height}_{video_mode}_{motion_type}_{str(focus_box)}"
            with self._canvas_lock:
                if cache_key in self._canvas_cache:
                    return self._canvas_cache[cache_key].copy()

        # Open image
        if isinstance(img_path, Image.Image):
            img = img_path.copy()
        else:
            p = Path(img_path).resolve()
            if not p.exists():
                raise StorageFileNotFoundError(f"Panel image not found: {img_path}")
            img = Image.open(p)

        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")
        elif img.mode == "RGBA":
            img = img.convert("RGB")

        img_w, img_h = img.size
        ar_in = img_w / float(img_h) if img_h > 0 else 1.0
        target_ar = self.width / float(self.height)

        norm_motion = self.camera_generator.normalize_motion_type(motion_type)

        # 1. Static focus / focus crop
        if norm_motion in (CameraMotionType.STATIC_FOCUS, CameraMotionType.FOCUS_CROP):
            fx, fy, scale_ratio = self.camera_generator.calculate_focus_center(
                focus_box=focus_box if isinstance(focus_box, FocusBox) else None
            )
            if isinstance(focus_box, dict):
                fx, fy, scale_ratio = self.camera_generator.resolve_focus_coordinates(img_w, img_h, focus_box)

            clamped_scale = max(self.camera_generator.min_crop_scale, min(1.0, scale_ratio))
            if ar_in > target_ar:
                crop_h = self.camera_generator._ensure_even(round(img_h * clamped_scale))
                crop_w = self.camera_generator._ensure_even(round(crop_h * target_ar))
            else:
                crop_w = self.camera_generator._ensure_even(round(img_w * clamped_scale))
                crop_h = self.camera_generator._ensure_even(round(crop_w / target_ar))

            crop_w = min(crop_w, self.camera_generator._ensure_even(img_w))
            crop_h = min(crop_h, self.camera_generator._ensure_even(img_h))
            cx = round(fx * img_w)
            cy = round(fy * img_h)
            x_pos = self.camera_generator._ensure_even(max(0, min(img_w - crop_w, cx - crop_w // 2)))
            y_pos = self.camera_generator._ensure_even(max(0, min(img_h - crop_h, cy - crop_h // 2)))

            cropped = img.crop((x_pos, y_pos, x_pos + crop_w, y_pos + crop_h))
            canvas = cropped.resize((self.width, self.height), Image.Resampling.LANCZOS)
            if cache_key:
                with self._canvas_lock:
                    self._canvas_cache[cache_key] = canvas.copy()
            return canvas

        # 2. Aspect match (if input is practically 16:9 and mode is auto)
        resolved_vmode = video_mode
        if isinstance(resolved_vmode, str):
            try:
                resolved_vmode = VideoMode(resolved_vmode.lower())
            except ValueError:
                resolved_vmode = VideoMode.AUTO

        if (resolved_vmode is None or resolved_vmode == VideoMode.AUTO) and abs(ar_in - target_ar) < 0.05:
            canvas = img.resize((self.width, self.height), Image.Resampling.LANCZOS)
            if cache_key:
                with self._canvas_lock:
                    self._canvas_cache[cache_key] = canvas.copy()
            return canvas

        # 3. LANDSCAPE_FIT: Fit inside canvas with black padding
        if resolved_vmode == VideoMode.LANDSCAPE_FIT:
            scale = min(self.width / float(img_w), self.height / float(img_h))
            new_w = max(1, int(round(img_w * scale)))
            new_h = max(1, int(round(img_h * scale)))
            fg = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
            canvas = Image.new("RGB", (self.width, self.height), (0, 0, 0))
            canvas.paste(fg, ((self.width - new_w) // 2, ((self.height - new_h) // 2)))
            if cache_key:
                with self._canvas_lock:
                    self._canvas_cache[cache_key] = canvas.copy()
            return canvas

        # 4. PORTRAIT_VERTICAL: Fit vertically with black wings
        if resolved_vmode == VideoMode.PORTRAIT_VERTICAL:
            scale = self.height / float(img_h)
            new_w = max(1, int(round(img_w * scale)))
            new_h = self.height
            fg = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
            canvas = Image.new("RGB", (self.width, self.height), (0, 0, 0))
            canvas.paste(fg, ((self.width - new_w) // 2, 0))
            if cache_key:
                with self._canvas_lock:
                    self._canvas_cache[cache_key] = canvas.copy()
            return canvas

        # 5. Default: PORTRAIT_PADDED (Dual-layer blurred background wings)
        scale_bg = max(self.width / float(img_w), self.height / float(img_h))
        bg_w = max(self.width, int(round(img_w * scale_bg)))
        bg_h = max(self.height, int(round(img_h * scale_bg)))
        bg = img.resize((bg_w, bg_h), Image.Resampling.BILINEAR)
        left = (bg_w - self.width) // 2
        top = (bg_h - self.height) // 2
        bg = bg.crop((left, top, left + self.width, top + self.height)).filter(ImageFilter.BoxBlur(25))

        scale_fg = min(self.width / float(img_w), self.height / float(img_h))
        fg_w = max(1, int(round(img_w * scale_fg)))
        fg_h = max(1, int(round(img_h * scale_fg)))
        fg = img.resize((fg_w, fg_h), Image.Resampling.LANCZOS)
        bg.paste(fg, ((self.width - fg_w) // 2, ((self.height - fg_h) // 2)))

        canvas = bg.convert("RGB")
        if cache_key:
            with self._canvas_lock:
                self._canvas_cache[cache_key] = canvas.copy()
        return canvas

    @staticmethod
    def _build_clip_ffmpeg_cmd(
        input_args: List[str],
        filter_complex: Optional[str],
        map_args: List[str],
        duration_sec: float,
        codec_args: List[str],
        fps: int,
        output_path: Path,
    ) -> List[str]:
        """Constructs canonical FFmpeg CLI command for panel clip rendering."""
        cmd = [
            "ffmpeg",
            "-y",
            *input_args,
        ]
        if filter_complex:
            cmd.extend(["-filter_complex", filter_complex])
        cmd.extend(map_args)
        cmd.extend(["-t", f"{duration_sec:.4f}"])
        cmd.extend(codec_args)
        cmd.extend([
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-r",
            str(fps),
            str(output_path),
        ])
        return cmd

    def split_text_into_chunks(self, text: str, max_chars: int = 50) -> List[str]:
        """Splits text into readable subtitle chunks."""
        if not text:
            return []
        cleaned = re.sub(r"\s+", " ", text).strip()
        sentences = re.split(r"(?<=[.!?])\s+", cleaned)
        chunks = []
        for sent in sentences:
            if len(sent) <= max_chars:
                chunks.append(sent)
            else:
                words = sent.split(" ")
                current_chunk = []
                current_len = 0
                for word in words:
                    word_len = len(word) + (1 if current_chunk else 0)
                    if current_len + word_len <= max_chars:
                        current_chunk.append(word)
                        current_len += word_len
                    else:
                        if current_chunk:
                            chunks.append(" ".join(current_chunk))
                        current_chunk = [word]
                        current_len = len(word)
                if current_chunk:
                    chunks.append(" ".join(current_chunk))
        return [c.strip() for c in chunks if c.strip()]

    def generate_panel_clip(
        self,
        img_path: str,
        audio_path: str,
        duration_ms: int,
        output_path: str,
        video_mode=VideoMode.PORTRAIT_PADDED,
        seq: int = 1,
        panel_text: Optional[str] = None,
        burn_subtitles: bool = True,
        pan_direction: Optional[str] = None,
        camera_motion: Optional[Union[CameraMotionType, str]] = None,
        focus_box: Optional[FocusBox] = None,
        preset: str = "medium",
        crf: str = "20",
        allow_animation: bool = True,
        audio_offset_sec: float = 0.0,
        subtitle_cues: Optional[List[SubtitleCue]] = None,
        hw_accel: Optional[str] = None,
        encoder: Optional[str] = None,
        font_size: int = 26,
    ) -> bool:
        """
        Renders an individual panel video clip with camera motion, audio synchronization,
        and timed subtitle cue transitions.
        """
        out_p = Path(output_path).resolve()
        out_p.parent.mkdir(parents=True, exist_ok=True)

        duration_sec = max(1.0 / self.fps, float(duration_ms) / 1000.0)

        img_p = Path(img_path).resolve()
        if not img_p.exists():
            raise StorageFileNotFoundError(f"Panel image not found: {img_path}")
        if img_p.stat().st_size == 0:
            raise ValueError(f"Panel image is empty (0 bytes): {img_path}")

        # Inspect image dimensions & verify image integrity
        try:
            with Image.open(img_p) as tmp_img:
                tmp_img.verify()
            with Image.open(img_p) as tmp_img:
                img_w, img_h = tmp_img.size
        except Exception as e:
            logger.error(f"Failed to read image dimensions or corrupt image {img_p}: {e}")
            raise ValueError(f"Failed to read or corrupt panel image {img_p}: {e}") from e

        # Prepare audio input arguments (supports sliced audio for multi-panel segments)
        audio_input_args = ["-i", str(audio_path)]
        if audio_offset_sec > 0.0:
            audio_input_args = ["-ss", f"{audio_offset_sec:.3f}", "-i", str(audio_path)]

        # Determine camera motion mode
        # Determine camera motion mode
        motion_mode = camera_motion
        if motion_mode is None and pan_direction:
            pan_map = {
                "left": CameraMotionType.PAN_LEFT,
                "right": CameraMotionType.PAN_RIGHT,
                "up": CameraMotionType.PAN_UP,
                "down": CameraMotionType.PAN_DOWN,
                "vertical": CameraMotionType.VERTICAL_SCROLL,
                "zoom": CameraMotionType.ZOOM_IN,
                "static": CameraMotionType.STATIC,
            }
            motion_mode = pan_map.get(str(pan_direction).lower(), CameraMotionType.STATIC)
        if motion_mode is None or not allow_animation:
            motion_mode = CameraMotionType.STATIC

        normalized_motion = self.camera_generator.normalize_motion_type(motion_mode)
        has_focus = (focus_box is not None)

        # Guard against excessive crop on tall continuous webtoon panels (H > 2W)
        if img_h > 2 * img_w and normalized_motion in (
            CameraMotionType.ZOOM_IN,
            CameraMotionType.ZOOM_OUT,
            CameraMotionType.PAN_LEFT,
            CameraMotionType.PAN_RIGHT,
            CameraMotionType.SUBJECT_PAN,
        ):
            normalized_motion = CameraMotionType.STATIC

        if not allow_animation:
            if normalized_motion in (CameraMotionType.FOCUS_CROP, CameraMotionType.STATIC_FOCUS) or has_focus:
                normalized_motion = CameraMotionType.STATIC_FOCUS
            else:
                normalized_motion = CameraMotionType.STATIC

        is_static_motion = normalized_motion in (
            CameraMotionType.STATIC,
            CameraMotionType.STATIC_FOCUS,
            CameraMotionType.FOCUS_CROP,
        )

        active_cues: List[Tuple[SubtitleCue, float, float]] = []
        if burn_subtitles:
            cues_to_evaluate = subtitle_cues
            if not cues_to_evaluate and panel_text and sanitize_subtitle_text(panel_text):
                cues_to_evaluate = self.subtitle_generator.split_text_into_cues(
                    panel_text, duration_sec=duration_sec, start_offset=0.0
                )

            if cues_to_evaluate:
                for cue in cues_to_evaluate:
                    t_start = max(0.0, cue.start_seconds - audio_offset_sec)
                    t_end = min(duration_sec, cue.end_seconds - audio_offset_sec)
                    if t_start < t_end - 0.05:  # At least 50ms active window
                        active_cues.append((cue, t_start, t_end))

        temp_overlay_files: List[Path] = []
        try:
            subtitle_input_args: List[str] = []
            if burn_subtitles and active_cues:
                for idx_c, (cue, t_s, t_e) in enumerate(active_cues):
                    temp_overlay_png = out_p.parent / f"{out_p.stem}_sub_{idx_c}.png"
                    self.create_subtitle_overlay_png(cue.text, temp_overlay_png, font_size=font_size)
                    temp_overlay_files.append(temp_overlay_png)
                    subtitle_input_args.extend(["-loop", "1", "-i", str(temp_overlay_png)])

            if is_static_motion:
                # Single-Frame Static Motion Hold optimization:
                # Avoid generating thousands of redundant raster frames and expensive boxblur on every frame.
                # Render single composited 16:9 canvas frame once, then loop with FFmpeg -loop 1.
                canvas_img = self.create_static_canvas(
                    img_path=img_p,
                    video_mode=video_mode,
                    focus_box=focus_box,
                    motion_type=normalized_motion,
                )
                temp_canvas_png = out_p.parent / f"{out_p.stem}_static_canvas.png"
                canvas_img.save(temp_canvas_png, "PNG")
                temp_overlay_files.append(temp_canvas_png)
                img_input_args = ["-loop", "1", "-i", str(temp_canvas_png)]

                if active_cues:
                    filter_parts = []
                    for idx_c, (cue, t_s, t_e) in enumerate(active_cues):
                        in_label = "[0:v]" if idx_c == 0 else f"[v_{idx_c-1}]"
                        out_label = "[outv]" if idx_c == len(active_cues) - 1 else f"[v_{idx_c}]"
                        sub_idx = 2 + idx_c
                        filter_parts.append(
                            f"{in_label}[{sub_idx}:v]overlay=0:0:enable='between(t,{t_s:.3f},{t_e:.3f})'{out_label}"
                        )
                    filter_complex = ";".join(filter_parts)
                    map_args = ["-map", "[outv]", "-map", "1:a"]
                else:
                    filter_complex = None
                    map_args = ["-map", "0:v", "-map", "1:a"]
            else:
                cam_filter, is_zoompan = self.camera_generator.build_filterchain(
                    img_w=img_w,
                    img_h=img_h,
                    duration_sec=duration_sec,
                    motion_type=normalized_motion,
                    video_mode=video_mode,
                    focus_box=focus_box,
                    allow_animation=allow_animation,
                )
                img_input_args = ["-i", str(img_p)] if is_zoompan else ["-loop", "1", "-i", str(img_p)]

                if active_cues:
                    filter_parts = [f"[0:v]{cam_filter}[v_0]"]
                    for idx_c, (cue, t_s, t_e) in enumerate(active_cues):
                        in_label = f"v_{idx_c}"
                        out_label = f"v_{idx_c+1}" if idx_c < len(active_cues) - 1 else "outv"
                        sub_idx = 2 + idx_c
                        filter_parts.append(
                            f"[{in_label}][{sub_idx}:v]overlay=0:0:enable='between(t,{t_s:.3f},{t_e:.3f})'[{out_label}]"
                        )
                    filter_complex = ";".join(filter_parts)
                else:
                    filter_complex = f"[0:v]{cam_filter}[outv]"
                map_args = ["-map", "[outv]", "-map", "1:a"]

            active_hw = hw_accel if hw_accel is not None else getattr(self, "hw_accel", None)
            active_encoder = encoder if encoder is not None else getattr(self, "encoder", None)
            if not active_encoder:
                if active_hw in ("nvenc", "h264_nvenc"):
                    active_encoder = "h264_nvenc"
                elif active_hw in ("videotoolbox", "h264_videotoolbox"):
                    active_encoder = "h264_videotoolbox"
                else:
                    active_encoder = "libx264"

            from src.production.hardware import get_encoder_codec_args, get_hardware_encoder_lock

            codec_args = get_encoder_codec_args(active_encoder, preset=preset, crf=crf)

            all_input_args = [*img_input_args, *audio_input_args, *subtitle_input_args]
            cmd = self._build_clip_ffmpeg_cmd(
                input_args=all_input_args,
                filter_complex=filter_complex,
                map_args=map_args,
                duration_sec=duration_sec,
                codec_args=codec_args,
                fps=self.fps,
                output_path=out_p,
            )

            # Standardized Hardware Acceleration Execution with Robust Software Fallback
            vt_lock = get_hardware_encoder_lock()
            if active_encoder == "h264_videotoolbox":
                with vt_lock:
                    try:
                        run_ffmpeg(cmd, description=f"Render panel clip {seq} ({active_encoder})")
                    except Exception as err:
                        logger.warning(
                            f"VideoToolbox encoding failed for clip {seq} ({err}). "
                            "Falling back to libx264 software encoder."
                        )
                        fallback_codec_args = get_encoder_codec_args("libx264", preset=preset, crf=crf)
                        cmd_fallback = self._build_clip_ffmpeg_cmd(
                            input_args=all_input_args,
                            filter_complex=filter_complex,
                            map_args=map_args,
                            duration_sec=duration_sec,
                            codec_args=fallback_codec_args,
                            fps=self.fps,
                            output_path=out_p,
                        )
                        run_ffmpeg(cmd_fallback, description=f"Render panel clip {seq} (libx264 fallback)")
            elif active_encoder == "h264_nvenc":
                try:
                    run_ffmpeg(cmd, description=f"Render panel clip {seq} ({active_encoder})")
                except Exception as err:
                    logger.warning(
                        f"NVENC encoding failed for clip {seq} ({err}). "
                        "Falling back to libx264 software encoder."
                    )
                    fallback_codec_args = get_encoder_codec_args("libx264", preset=preset, crf=crf)
                    cmd_fallback = self._build_clip_ffmpeg_cmd(
                        input_args=all_input_args,
                        filter_complex=filter_complex,
                        map_args=map_args,
                        duration_sec=duration_sec,
                        codec_args=fallback_codec_args,
                        fps=self.fps,
                        output_path=out_p,
                    )
                    run_ffmpeg(cmd_fallback, description=f"Render panel clip {seq} (libx264 fallback)")
            else:
                run_ffmpeg(cmd, description=f"Render panel clip {seq} ({active_encoder})")

            return out_p.exists() and out_p.stat().st_size > 0
        except Exception as e:
            logger.error(f"Error rendering clip {output_path}: {e}")
            return False
        finally:
            for tmp_f in temp_overlay_files:
                if tmp_f.exists():
                    try:
                        tmp_f.unlink()
                    except Exception:
                        pass

    def concatenate_clips(self, clip_paths: List[str], output_path: str) -> bool:
        """Losslessly concatenates video clips using the FFmpeg concat demuxer."""
        out = Path(output_path).resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        list_file_path = out.parent / f"{out.stem}_concat_list.txt"

        with open(list_file_path, "w", encoding="utf-8") as f:
            for p in clip_paths:
                abs_p = str(Path(p).resolve()).replace("'", "'\\''")
                f.write(f"file '{abs_p}'\n")

        cmd = [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_file_path),
            "-c",
            "copy",
            str(out),
        ]

        try:
            run_ffmpeg(cmd, description="Concatenate video clips")
            return out.exists() and out.stat().st_size > 0
        finally:
            if list_file_path.exists():
                try:
                    list_file_path.unlink()
                except Exception:
                    pass

    def generate_srt_file(
        self,
        script_items: Union[List[dict], RecapScript],
        srt_path: str,
    ) -> Path:
        """Generates standard SubRip (.srt) subtitle file."""
        return self.subtitle_generator.generate_srt_file(script_items, srt_path)

    def mix_bgm_and_burn_subs(
        self,
        video_path: str,
        srt_path: str,
        bgm_path: Optional[str],
        output_path: str,
    ) -> bool:
        """Mixes BGM loop audio with video voiceover (backward compatibility)."""
        out = Path(output_path).resolve()
        out.parent.mkdir(parents=True, exist_ok=True)

        if not bgm_path or not os.path.exists(bgm_path):
            cmd = ["ffmpeg", "-y", "-i", str(video_path), "-c", "copy", str(out)]
        else:
            cmd = [
                "ffmpeg",
                "-y",
                "-i",
                str(video_path),
                "-stream_loop",
                "-1",
                "-i",
                str(bgm_path),
                "-filter_complex",
                "[1:a]volume=0.15[bgm];[0:a][bgm]amix=inputs=2:duration=first:dropout_transition=2[a]",
                "-map",
                "0:v",
                "-map",
                "[a]",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                str(out),
            ]

        try:
            run_ffmpeg(cmd, description="Mix BGM and assemble video")
            return out.exists() and out.stat().st_size > 0
        except Exception as e:
            logger.error(f"Failed to mix BGM: {e}")
            return False

    def render_chapter(
        self,
        project_dir: Union[str, Path],
        chapter_num: Union[int, str],
        quality: str = "final",
        bgm_path: Optional[Union[str, Path]] = None,
        burn_subtitles: bool = True,
        keep_temp: bool = False,
        force: bool = False,
        suffix: str = "final",
        output_path: Optional[Union[str, Path]] = None,
        video_mode: str = "auto",
        allow_animation: bool = True,
        allow_placeholders: bool = False,
        max_workers: Optional[int] = None,
        hw_accel: Optional[str] = None,
        encoder: Optional[str] = None,
        subtitle_lines: Optional[int] = None,
        subtitle_font_size: Optional[int] = None,
        subtitle_chunking: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Orchestrates full chapter video rendering using Video Engine V2.

        Steps:
            1. Validate prerequisites and update checkpoint to RUNNING.
            2. Compute frame-anchored timeline table (zero cumulative drift).
            3. Generate synchronized chapter SRT subtitle file.
            4. Render individual panel clips with camera motion and audio duration.
            5. Losslessly concatenate video stream.
            6. Fast PCM concatenate speech utterances and mix BGM with dynamic ducking.
            7. Lossless master container mux (video + audio).
            8. Automated ffprobe stream validation.
            9. Clean intermediate files and update checkpoint to COMPLETED.
        """
        # Ensure dependencies exist
        if not shutil.which("ffmpeg"):
            raise DependencyNotFoundError("FFmpeg executable not found on system PATH.")
        if not shutil.which("ffprobe"):
            raise DependencyNotFoundError("FFprobe executable not found on system PATH.")

        p_dir = Path(project_dir).resolve()
        chap_dir = p_dir / "chapters" / str(chapter_num)
        if not chap_dir.exists():
            fallback_chap = get_chapter_dir(p_dir.name, chapter_num)
            if fallback_chap.exists():
                chap_dir = fallback_chap
            else:
                raise StorageFileNotFoundError(f"Chapter directory not found: {chap_dir}")

        script_path = chap_dir / "script.json"
        if not script_path.exists():
            raise StorageFileNotFoundError(f"script.json not found in {chap_dir}")

        # Set stage to RUNNING
        update_checkpoint(chap_dir, StageType.RENDER, CheckpointStatus.RUNNING)

        active_workers = max_workers if max_workers is not None else getattr(self, "max_workers", 2)
        active_encoder = encoder if encoder is not None else getattr(self, "encoder", None)
        active_hw_accel = hw_accel if hw_accel is not None else getattr(self, "hw_accel", None)

        if active_encoder is None or active_encoder == "auto":
            if active_hw_accel in ("videotoolbox", "h264_videotoolbox"):
                active_encoder = "h264_videotoolbox"
            elif active_hw_accel in ("nvenc", "h264_nvenc"):
                active_encoder = "h264_nvenc"
            else:
                from src.production.hardware import resolve_video_encoder

                active_encoder, _, _ = resolve_video_encoder("auto")

        if active_encoder == "h264_nvenc":
            active_hw_accel = "nvenc"
        elif active_encoder == "h264_videotoolbox":
            active_hw_accel = "videotoolbox"
        else:
            active_hw_accel = "none"

        self.clear_canvas_cache()

        try:
            script = load_recap_script(chap_dir)
            if not script.segments:
                raise ValueError(f"RecapScript has no segments in {script_path}")

            quality_clean = quality.lower().strip()
            if quality_clean == "final" or suffix == "final":
                if allow_placeholders:
                    raise RuntimeError("Placeholder media is strictly disallowed in final production renders.")
                allow_placeholders = False

            if quality_clean == "preview":
                preset = "ultrafast"
                crf = "28"
                self.width = 1280
                self.height = 720
            else:
                preset = "medium"
                crf = "20"
                self.width = 1920
                self.height = 1080

            # Synchronize camera motion generator with target resolution (critical for 1080p final render)
            self.camera_generator = CameraMotionGenerator(self.width, self.height, self.fps)

            # Resolve subtitle settings (CLI overrides project settings)
            sub_lines = subtitle_lines
            sub_font_size = subtitle_font_size
            sub_chunking = subtitle_chunking

            project_cfg_path = p_dir / "project.json"
            if project_cfg_path.exists():
                with contextlib.suppress(Exception):
                    proj_data = json.loads(project_cfg_path.read_text(encoding="utf-8"))
                    render_cfg = proj_data.get("render_settings", {})
                    sub_cfg = render_cfg.get("subtitle", {})
                    if sub_lines is None and "max_lines" in sub_cfg:
                        sub_lines = sub_cfg.get("max_lines")
                    if sub_font_size is None and "font_size" in sub_cfg:
                        sub_font_size = sub_cfg.get("font_size")
                    if sub_chunking is None and "chunking" in sub_cfg:
                        sub_chunking = sub_cfg.get("chunking")

            sub_lines = sub_lines if sub_lines is not None else 1
            sub_font_size = sub_font_size if sub_font_size is not None else 26
            sub_chunking = sub_chunking if sub_chunking is not None else "sentence-aware"

            # Synchronize subtitle generator with resolved settings
            self.subtitle_generator = SubtitleGenerator(
                max_chars_per_line=42,
                max_lines_per_block=sub_lines,
                chunking_mode=sub_chunking,
            )

            video_dir = chap_dir / "video"
            clips_dir = video_dir / "clips"
            subtitles_dir = chap_dir / "subtitles"
            audio_dir = chap_dir / "audio"
            clips_dir.mkdir(parents=True, exist_ok=True)
            subtitles_dir.mkdir(parents=True, exist_ok=True)
            audio_dir.mkdir(parents=True, exist_ok=True)

            # Load manifest.json if present to prioritize panel relative_path resolution
            manifest = load_manifest(chap_dir)
            if not manifest:
                recut_manifest_p = p_dir / "image" / "recut" / f"chap_{chapter_num}" / "manifest.json"
                if recut_manifest_p.exists():
                    with contextlib.suppress(Exception):
                        manifest = load_manifest(recut_manifest_p.parent)

            panel_index: Dict[str, Path] = {}
            if manifest and manifest.panels:
                for panel in manifest.panels:
                    p_resolved: Optional[Path] = None
                    if panel.relative_path:
                        rel_p = Path(panel.relative_path)
                        cands = [
                            chap_dir / rel_p,
                            chap_dir / "panels" / rel_p.name,
                            chap_dir / rel_p.name,
                            p_dir / rel_p,
                            p_dir / "image" / "panels" / rel_p.name,
                        ]
                        for c in cands:
                            if c.exists():
                                p_resolved = c.resolve()
                                break
                    if not p_resolved and panel.filename:
                        fn = panel.filename
                        cands = [
                            chap_dir / "panels" / fn,
                            chap_dir / fn,
                            p_dir / "image" / "panels" / fn,
                        ]
                        for c in cands:
                            if c.exists():
                                p_resolved = c.resolve()
                                break
                    if p_resolved:
                        if panel.panel_id:
                            panel_index[panel.panel_id] = p_resolved
                        if panel.filename:
                            panel_index[panel.filename] = p_resolved
                            panel_index[Path(panel.filename).stem] = p_resolved
                        if panel.relative_path:
                            panel_index[panel.relative_path] = p_resolved
                            panel_index[Path(panel.relative_path).name] = p_resolved
                            panel_index[Path(panel.relative_path).stem] = p_resolved

            # 1. Resolve Segment Audio Files & Exact Measured Durations
            search_dirs = [
                chap_dir / "panels",
                chap_dir / "raw",
                chap_dir / "images",
                chap_dir,
                p_dir / "image" / "panels",
            ]

            resolved_segments: List[Tuple[ScriptSegment, List[Path], Path]] = []
            segment_audio_paths: List[Path] = []
            timeline_durations: List[float] = []
            script_dirty = False

            for seg in script.segments:
                # Resolve ALL referenced panels in sequence without skipping
                if seg.panel_ids:
                    target_panel_ids = list(seg.panel_ids)
                else:
                    # Fallback candidate resolution if panel_ids is empty
                    candidate_panel_names = [
                        f"panel_{seg.seq_id}.jpg",
                        f"panel_{seg.seq_id}.png",
                        f"panel_{seg.seq_id}.webp",
                        f"panel_{int(seg.seq_id):03d}.jpg" if seg.seq_id.isdigit() else f"panel_{seg.seq_id}.jpg",
                        f"panel_{seg.seq_id}",
                    ]
                    found_cand = None
                    for c_name in candidate_panel_names:
                        if c_name in panel_index:
                            found_cand = c_name
                            break
                        for s_dir in search_dirs:
                            if (s_dir / c_name).exists():
                                found_cand = str(s_dir / c_name)
                                break
                        if found_cand:
                            break
                    target_panel_ids = [found_cand if found_cand else f"panel_{seg.seq_id}.jpg"]

                resolved_panels_for_seg: List[Path] = []
                for pid in target_panel_ids:
                    panel_p: Optional[Path] = None
                    if pid in panel_index:
                        panel_p = panel_index[pid]
                    elif Path(pid).name in panel_index:
                        panel_p = panel_index[Path(pid).name]
                    elif Path(pid).stem in panel_index:
                        panel_p = panel_index[Path(pid).stem]

                    if not panel_p:
                        direct_p = Path(pid)
                        if direct_p.is_absolute() and direct_p.exists():
                            panel_p = direct_p.resolve()
                        else:
                            cands = [pid, f"{pid}.jpg", f"{pid}.png", f"{pid}.webp"]
                            if pid.isdigit():
                                cands.append(f"panel_{int(pid):03d}.jpg")
                            for s_dir in search_dirs:
                                for c in cands:
                                    cand_p = s_dir / c
                                    if cand_p.exists():
                                        panel_p = cand_p.resolve()
                                        break
                                if panel_p:
                                    break

                    if not panel_p or not panel_p.exists():
                        if quality_clean == "final" or suffix == "final":
                            raise RuntimeError(
                                f"Missing panel image '{pid}' for segment {seg.seq_id} in chapter {chapter_num}. "
                                "Placeholder media is strictly disallowed in final production renders."
                            )
                        if not allow_placeholders:
                            raise StorageFileNotFoundError(
                                f"Missing panel image '{pid}' for segment {seg.seq_id} in chapter {chapter_num}. "
                                "Placeholders are disabled for video render integrity."
                            )
                        panels_default_dir = chap_dir / "panels"
                        panels_default_dir.mkdir(parents=True, exist_ok=True)
                        panel_p = (panels_default_dir / f"panel_{pid}.jpg").resolve()
                        if not panel_p.exists():
                            dummy_img = Image.new("RGB", (self.width, self.height), color=(30, 45, 60))
                            d_draw = ImageDraw.Draw(dummy_img)
                            d_draw.text((60, 60), f"Chapter {chapter_num} - Panel {pid}", fill=(220, 220, 220))
                            dummy_img.save(panel_p, "JPEG")

                    if panel_p.stat().st_size == 0:
                        raise ValueError(f"Panel image '{panel_p}' is empty (0 bytes) for segment {seg.seq_id}.")
                    try:
                        with Image.open(panel_p) as tmp_im:
                            tmp_im.verify()
                    except Exception as e:
                        raise ValueError(f"Corrupt or unreadable panel image '{panel_p}' for segment {seg.seq_id}: {e}")

                    resolved_panels_for_seg.append(panel_p)

                # Find utterance WAV audio
                wav_p = None
                if seg.audio_path:
                    cand_audio = Path(seg.audio_path)
                    if not cand_audio.is_absolute():
                        for base in (chap_dir, p_dir):
                            if (base / cand_audio).exists():
                                wav_p = (base / cand_audio).resolve()
                                break
                    elif cand_audio.exists():
                        wav_p = cand_audio.resolve()

                if not wav_p:
                    audio_search_cands = [
                        chap_dir / "audio" / "clips" / f"audio_{chapter_num}_{seg.seq_id}.wav",
                        chap_dir / "audio" / "clips" / f"{seg.seq_id}.wav",
                        chap_dir / "audio" / f"audio_{chapter_num}_{seg.seq_id}.wav",
                        chap_dir / "audio" / f"{seg.seq_id}.wav",
                        p_dir / "audio" / f"audio_{chapter_num}_{seg.seq_id}.wav",
                    ]
                    for cand in audio_search_cands:
                        if cand.exists() and cand.stat().st_size > 44:
                            wav_p = cand.resolve()
                            break

                # If WAV missing, synthesize 48kHz silence WAV for segment duration or raise
                if not wav_p:
                    if quality_clean == "final" or suffix == "final":
                        raise RuntimeError(
                            f"Missing audio clip for segment {seg.seq_id} in chapter {chapter_num}. "
                            "Placeholder media is strictly disallowed in final production renders."
                        )
                    if not allow_placeholders:
                        raise StorageFileNotFoundError(
                            f"Missing audio clip for segment {seg.seq_id} in chapter {chapter_num}. "
                            f"Searched: {[str(c) for c in audio_search_cands]}. "
                            "Silence placeholders are disabled for video render integrity."
                        )
                    clip_audio_dir = chap_dir / "audio" / "clips"
                    clip_audio_dir.mkdir(parents=True, exist_ok=True)
                    wav_p = (clip_audio_dir / f"audio_{chapter_num}_{seg.seq_id}.wav").resolve()
                    dur_fallback = float(seg.duration or max(1.5, len(seg.narrator_text) * 0.08))
                    num_frames = int(round(dur_fallback * 48000))
                    with wave.open(str(wav_p), "wb") as wf:
                        wf.setnchannels(1)
                        wf.setsampwidth(2)
                        wf.setframerate(48000)
                        wf.writeframes(b"\x00\x00" * num_frames)

                # Read exact duration from binary WAV
                exact_dur = get_wav_duration_seconds(wav_p)
                if abs(float(seg.duration or 0.0) - exact_dur) > 0.001:
                    seg.duration = exact_dur
                    script_dirty = True
                else:
                    seg.duration = exact_dur
                resolved_segments.append((seg, resolved_panels_for_seg, wav_p))
                segment_audio_paths.append(wav_p)
                timeline_durations.append(exact_dur)

            # Persist exact durations back to script.json if any differed
            if script_dirty:
                atomic_write_json(script_path, script)

            # 2. Synchronized SRT Subtitle Generation
            srt_path = subtitles_dir / f"chapter_{chapter_num}.srt"
            self.subtitle_generator.generate_srt_file(script, srt_path)

            # 3. Concatenate Speech & Mix Dynamic BGM
            speech_master_wav = audio_dir / f"speech_master_{chapter_num}.wav"
            _, total_speech_dur = self.audio_mixer.concatenate_speech_utterances(
                segment_audio_paths, speech_master_wav
            )

            master_audio_path = audio_dir / f"master_audio_{chapter_num}.m4a"
            self.audio_mixer.mix_audio_track(
                speech_wav_path=speech_master_wav,
                output_path=master_audio_path,
                bgm_path=bgm_path,
            )

            # 4. Cumulative Frame-Anchoring Timeline Allocation
            # F_start, i = round(T_start, i * fps), F_end, i = round(T_end, i * fps)
            clip_paths: List[Path] = []
            cached_clips_count = 0
            rendered_clips_count = 0
            cumulative_time = 0.0

            render_tasks: List[Dict[str, Any]] = []

            for idx, (seg, panel_paths, wav_p) in enumerate(resolved_segments):
                seg_dur = timeline_durations[idx]
                t_start = cumulative_time
                t_end = cumulative_time + seg_dur
                cumulative_time = t_end

                f_start = round(t_start * self.fps)
                f_end = round(t_end * self.fps)
                f_total = max(len(panel_paths) if panel_paths else 1, f_end - f_start)

                audio_offset = 0.0
                seg_cues = self.subtitle_generator.split_text_into_cues(
                    text=seg.narrator_text,
                    duration_sec=seg_dur,
                    start_offset=0.0,
                )

                if seg.shots and len(seg.shots) > 0:
                    # Multi-shot visual director branch (PR-01)
                    shots = seg.shots
                    total_ratio = sum(s.duration_ratio for s in shots)
                    if total_ratio <= 0.0:
                        total_ratio = 1.0

                    cum_frames = 0
                    for s_idx, shot in enumerate(shots):
                        cum_ratio = sum(s.duration_ratio for s in shots[: s_idx + 1]) / total_ratio
                        end_frame = round(cum_ratio * f_total) if s_idx < len(shots) - 1 else f_total
                        f_k = max(1, end_frame - cum_frames)
                        cum_frames = end_frame
                        dur_k = f_k / float(self.fps)
                        duration_ms_k = int(round(dur_k * 1000.0))

                        # Resolve shot panel image
                        shot_panel_p = None
                        if shot.source and shot.source.panel_id:
                            pid = shot.source.panel_id
                            if pid in panel_index:
                                shot_panel_p = panel_index[pid]
                            elif Path(pid).stem in panel_index:
                                shot_panel_p = panel_index[Path(pid).stem]
                            else:
                                for s_dir in search_dirs:
                                    for ext in ("", ".jpg", ".png", ".webp"):
                                        c = s_dir / f"{pid}{ext}"
                                        if c.exists():
                                            shot_panel_p = c.resolve()
                                            break
                                    if shot_panel_p:
                                        break
                        if not shot_panel_p and panel_paths:
                            shot_panel_p = panel_paths[min(s_idx, len(panel_paths) - 1)]

                        if len(shots) == 1:
                            clip_p = clips_dir / f"clip_{quality_clean}_{seg.seq_id}.mp4"
                            clip_meta_p = clips_dir / f"clip_{quality_clean}_{seg.seq_id}.meta.json"
                        else:
                            clip_p = clips_dir / f"clip_{quality_clean}_{seg.seq_id}_shot_{s_idx}.mp4"
                            clip_meta_p = clips_dir / f"clip_{quality_clean}_{seg.seq_id}_shot_{s_idx}.meta.json"

                        clip_paths.append(clip_p)

                        audio_stat = wav_p.stat() if wav_p and wav_p.exists() else None
                        panel_stat = shot_panel_p.stat() if shot_panel_p and shot_panel_p.exists() else None
                        audio_hash = compute_file_sha256(wav_p) if wav_p and wav_p.exists() else ""
                        panel_hash = compute_file_sha256(shot_panel_p) if shot_panel_p and shot_panel_p.exists() else ""
                        script_hash = hashlib.sha256(seg.narrator_text.strip().encode("utf-8")).hexdigest()
                        shot_cam = shot.motion if shot.motion else seg.camera_motion
                        shot_focus = shot.focus_box if shot.focus_box else seg.focus_box
                        focus_box_spec = shot_focus.model_dump() if shot_focus else None

                        current_fingerprint = {
                            "quality": quality_clean,
                            "target_width": self.width,
                            "target_height": self.height,
                            "fps": self.fps,
                            "duration_ms": duration_ms_k,
                            "narrator_text": seg.narrator_text.strip(),
                            "script_hash": script_hash,
                            "audio_hash": audio_hash,
                            "panel_hash": panel_hash,
                            "camera_motion": shot_cam.value if hasattr(shot_cam, "value") else str(shot_cam),
                            "focus_box": focus_box_spec,
                            "burn_subtitles": burn_subtitles,
                            "allow_animation": allow_animation,
                            "video_mode": str(video_mode),
                            "audio_size": audio_stat.st_size if audio_stat else 0,
                            "panel_size": panel_stat.st_size if panel_stat else 0,
                            "audio_offset_sec": round(audio_offset, 3),
                            "panel_index": s_idx,
                            "shot_id": shot.shot_id,
                            "encoder": str(active_encoder),
                            "hw_accel": str(active_hw_accel or "none"),
                            "subtitle_lines": sub_lines,
                            "subtitle_font_size": sub_font_size,
                            "subtitle_chunking": str(sub_chunking),
                        }

                        is_cached = False
                        if not force and clip_p.exists() and clip_p.stat().st_size > 1000 and clip_meta_p.exists():
                            try:
                                cached_meta = json.loads(clip_meta_p.read_text(encoding="utf-8"))
                                if cached_meta == current_fingerprint:
                                    is_cached = True
                            except Exception:
                                is_cached = False

                        if is_cached:
                            cached_clips_count += 1
                        else:
                            if isinstance(video_mode, str) and video_mode != "auto":
                                resolved_mode = VideoMode(video_mode) if video_mode in [m.value for m in VideoMode] else VideoMode.PORTRAIT_PADDED
                            elif isinstance(video_mode, VideoMode) and video_mode != VideoMode.AUTO:
                                resolved_mode = video_mode
                            else:
                                try:
                                    with Image.open(shot_panel_p) as tmp_im:
                                        pw, ph = tmp_im.size
                                    resolved_mode = self.detect_video_mode(pw, ph)
                                except Exception:
                                    resolved_mode = VideoMode.PORTRAIT_PADDED

                            render_tasks.append({
                                "clip_p": clip_p,
                                "clip_meta_p": clip_meta_p,
                                "fingerprint": current_fingerprint,
                                "seg_id": seg.seq_id,
                                "panel_name": shot_panel_p.name if shot_panel_p else f"shot_{s_idx}",
                                "kwargs": {
                                    "img_path": str(shot_panel_p),
                                    "audio_path": str(wav_p),
                                    "duration_ms": dur_k * 1000.0,
                                    "output_path": str(clip_p),
                                    "video_mode": resolved_mode,
                                    "seq": idx + 1,
                                    "panel_text": seg.narrator_text,
                                    "burn_subtitles": burn_subtitles,
                                    "camera_motion": shot_cam,
                                    "focus_box": shot_focus,
                                    "preset": preset,
                                    "crf": crf,
                                    "allow_animation": allow_animation,
                                    "audio_offset_sec": audio_offset,
                                    "subtitle_cues": seg_cues,
                                    "hw_accel": active_hw_accel,
                                    "encoder": active_encoder,
                                    "font_size": sub_font_size,
                                },
                            })

                        audio_offset += dur_k
                else:
                    # Legacy panel_ids fallback
                    n_panels = max(1, len(panel_paths))
                    f_base = f_total // n_panels
                    r_frames = f_total % n_panels

                    for k, panel_p in enumerate(panel_paths):
                        f_k = f_base + (1 if k < r_frames else 0)
                        dur_k = f_k / float(self.fps)
                        duration_ms_k = int(round(dur_k * 1000.0))

                        if n_panels == 1:
                            clip_p = clips_dir / f"clip_{quality_clean}_{seg.seq_id}.mp4"
                            clip_meta_p = clips_dir / f"clip_{quality_clean}_{seg.seq_id}.meta.json"
                        else:
                            clip_p = clips_dir / f"clip_{quality_clean}_{seg.seq_id}_{k}.mp4"
                            clip_meta_p = clips_dir / f"clip_{quality_clean}_{seg.seq_id}_{k}.meta.json"

                        clip_paths.append(clip_p)

                        # Compute clip render fingerprint based on quality, resolution, motion, text, audio, image, and subtitle settings
                        audio_stat = wav_p.stat() if wav_p.exists() else None
                        panel_stat = panel_p.stat() if panel_p.exists() else None
                        audio_hash = compute_file_sha256(wav_p) if wav_p and wav_p.exists() else ""
                        panel_hash = compute_file_sha256(panel_p) if panel_p and panel_p.exists() else ""
                        script_hash = hashlib.sha256(seg.narrator_text.strip().encode("utf-8")).hexdigest()
                        focus_box_spec = seg.focus_box.model_dump() if (seg.focus_box and k == 0) else None
                        panel_cam = seg.camera_motion if (k == 0 or seg.camera_motion in (CameraMotionType.STATIC, "static")) else CameraMotionType.STATIC

                        current_fingerprint = {
                            "quality": quality_clean,
                            "target_width": self.width,
                            "target_height": self.height,
                            "fps": self.fps,
                            "duration_ms": duration_ms_k,
                            "narrator_text": seg.narrator_text.strip(),
                            "script_hash": script_hash,
                            "audio_hash": audio_hash,
                            "panel_hash": panel_hash,
                            "camera_motion": panel_cam.value if hasattr(panel_cam, "value") else str(panel_cam),
                            "focus_box": focus_box_spec,
                            "burn_subtitles": burn_subtitles,
                            "allow_animation": allow_animation,
                            "video_mode": str(video_mode),
                            "audio_size": audio_stat.st_size if audio_stat else 0,
                            "panel_size": panel_stat.st_size if panel_stat else 0,
                            "audio_offset_sec": round(audio_offset, 3),
                            "panel_index": k,
                            "encoder": str(active_encoder),
                            "hw_accel": str(active_hw_accel or "none"),
                            "subtitle_lines": sub_lines,
                            "subtitle_font_size": sub_font_size,
                            "subtitle_chunking": str(sub_chunking),
                        }

                        # Skip if already rendered, fingerprint matches, and force is False
                        is_cached = False
                        if not force and clip_p.exists() and clip_p.stat().st_size > 1000 and clip_meta_p.exists():
                            try:
                                cached_meta = json.loads(clip_meta_p.read_text(encoding="utf-8"))
                                if cached_meta == current_fingerprint:
                                    is_cached = True
                            except Exception:
                                is_cached = False

                        if is_cached:
                            cached_clips_count += 1
                        else:
                            if isinstance(video_mode, str) and video_mode != "auto":
                                resolved_mode = VideoMode(video_mode) if video_mode in [m.value for m in VideoMode] else VideoMode.PORTRAIT_PADDED
                            elif isinstance(video_mode, VideoMode) and video_mode != VideoMode.AUTO:
                                resolved_mode = video_mode
                            else:
                                try:
                                    with Image.open(panel_p) as tmp_im:
                                        pw, ph = tmp_im.size
                                    resolved_mode = self.detect_video_mode(pw, ph)
                                except Exception:
                                    resolved_mode = VideoMode.PORTRAIT_PADDED

                            render_tasks.append({
                                "clip_p": clip_p,
                                "clip_meta_p": clip_meta_p,
                                "fingerprint": current_fingerprint,
                                "seg_id": seg.seq_id,
                                "panel_name": panel_p.name,
                                "kwargs": {
                                    "img_path": str(panel_p),
                                    "audio_path": str(wav_p),
                                    "duration_ms": dur_k * 1000.0,
                                    "output_path": str(clip_p),
                                    "video_mode": resolved_mode,
                                    "seq": idx + 1,
                                    "panel_text": seg.narrator_text,
                                    "burn_subtitles": burn_subtitles,
                                    "camera_motion": panel_cam,
                                    "focus_box": seg.focus_box if k == 0 else None,
                                    "preset": preset,
                                    "crf": crf,
                                    "allow_animation": allow_animation,
                                    "audio_offset_sec": audio_offset,
                                    "subtitle_cues": seg_cues,
                                    "hw_accel": active_hw_accel,
                                    "encoder": active_encoder,
                                    "font_size": sub_font_size,
                                },
                            })

                        audio_offset += dur_k

            if render_tasks:
                from concurrent.futures import ThreadPoolExecutor, as_completed

                rendered_clips_count = len(render_tasks)
                logger.info(
                    f"Rendering {rendered_clips_count} panel clips (cached: {cached_clips_count}) "
                    f"using {active_workers} worker threads (Balanced mode)..."
                )

                def _execute_render_task(task: Dict[str, Any]) -> None:
                    c_p = task["clip_p"]
                    c_m = task["clip_meta_p"]
                    if c_p.exists():
                        try:
                            c_p.unlink()
                        except Exception:
                            pass
                    if c_m.exists():
                        try:
                            c_m.unlink()
                        except Exception:
                            pass

                    ok = self.generate_panel_clip(**task["kwargs"])
                    if not ok:
                        if c_p.exists():
                            try:
                                c_p.unlink()
                            except Exception:
                                pass
                        if c_m.exists():
                            try:
                                c_m.unlink()
                            except Exception:
                                pass
                        raise RuntimeError(
                            f"Failed to generate clip: {c_p} for segment {task['seg_id']}, panel {task['panel_name']}"
                        )
                    if not c_p.exists() or c_p.stat().st_size <= 1000:
                        if c_p.exists():
                            try:
                                c_p.unlink()
                            except Exception:
                                pass
                        if c_m.exists():
                            try:
                                c_m.unlink()
                            except Exception:
                                pass
                        raise RuntimeError(
                            f"Render verification failed for segment {task['seg_id']}, panel {task['panel_name']}: "
                            f"output clip {c_p} is missing or corrupted (size: {c_p.stat().st_size if c_p.exists() else 0} bytes)."
                        )
                    try:
                        c_m.write_text(json.dumps(task["fingerprint"], ensure_ascii=False, indent=2), encoding="utf-8")
                    except Exception as err:
                        logger.warning(f"Failed to write clip metadata fingerprint for {c_p}: {err}")

                if active_workers > 1 and len(render_tasks) > 1:
                    with ThreadPoolExecutor(max_workers=active_workers) as pool:
                        futures = [pool.submit(_execute_render_task, t) for t in render_tasks]
                        for f in as_completed(futures):
                            f.result()
                else:
                    for t in render_tasks:
                        _execute_render_task(t)

            # 5. Lossless Visual Stream Concatenation
            raw_video_path = video_dir / f"raw_video_{chapter_num}.mp4"
            concat_ok = self.concatenate_clips([str(c) for c in clip_paths], str(raw_video_path))
            if not concat_ok or not raw_video_path.exists():
                raise RuntimeError(f"Failed to concatenate panel clips into {raw_video_path}")

            # 6. Destination Path Resolution
            if output_path:
                final_video_path = Path(output_path).resolve()
            else:
                resolved_suffix = "preview" if quality_clean == "preview" and suffix == "final" else suffix
                final_video_path = resolve_video_output_path(chap_dir, chapter_num, suffix=resolved_suffix)

            final_video_path.parent.mkdir(parents=True, exist_ok=True)

            # 7. Master Mux: Combine Raw Video Stream and Master Audio Track
            # Explicitly map video from raw clips (0:v:0) and audio from master track (1:a:0)
            # Bound container duration cleanly to total_speech_dur.
            mux_cmd = [
                "ffmpeg",
                "-y",
                "-i",
                str(raw_video_path),
                "-i",
                str(master_audio_path),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:v",
                "copy",
                "-c:a",
                "copy",
                "-t",
                f"{total_speech_dur:.3f}",
                "-shortest",
                "-movflags",
                "+faststart",
                str(final_video_path),
            ]
            run_ffmpeg(mux_cmd, description="Mux final video and master audio")

            if not final_video_path.exists() or final_video_path.stat().st_size == 0:
                raise RuntimeError(f"Final video output is missing or empty: {final_video_path}")

            # Also maintain canonical static alias: video/final.mp4 or video/preview.mp4
            canonical_name = "preview.mp4" if quality_clean == "preview" else "final.mp4"
            canonical_path = video_dir / canonical_name
            if canonical_path != final_video_path:
                try:
                    shutil.copy2(str(final_video_path), str(canonical_path))
                except Exception:
                    pass

            # 8. Stream Validation via ffprobe (must be valid to proceed)
            validation_res = self.validator.validate_video(
                video_path=final_video_path,
                expected_duration=total_speech_dur,
                tolerance_seconds=0.15,
                expected_width=self.width,
                expected_height=self.height,
                expected_fps=float(self.fps),
                raise_on_error=True,
            )

            # 9. Clean intermediate clips if keep_temp is False
            cleaned_count = 0
            if not keep_temp:
                for c in clip_paths:
                    if c.exists():
                        try:
                            c.unlink()
                            cleaned_count += 1
                        except Exception:
                            pass
                    meta_c = c.with_suffix(".meta.json")
                    if meta_c.exists():
                        try:
                            meta_c.unlink()
                            cleaned_count += 1
                        except Exception:
                            pass
                for pattern in ("*static_canvas*.png", "*_sub_*.png"):
                    for tmp_f in clips_dir.glob(pattern):
                        try:
                            tmp_f.unlink()
                            cleaned_count += 1
                        except Exception:
                            pass
                if raw_video_path.exists():
                    try:
                        raw_video_path.unlink()
                        cleaned_count += 1
                    except Exception:
                        pass
                if speech_master_wav.exists():
                    try:
                        speech_master_wav.unlink()
                        cleaned_count += 1
                    except Exception:
                        pass

            self.clear_canvas_cache()

            # 10. Update Chapter Stage Checkpoint to COMPLETED
            artifacts = [
                str(final_video_path.relative_to(chap_dir)),
                str(srt_path.relative_to(chap_dir)),
            ]
            update_checkpoint(
                chap_dir,
                StageType.RENDER,
                CheckpointStatus.COMPLETED,
                artifacts=artifacts,
            )

            # Completing RENDER completes chapter checkpoint
            if quality_clean == "final":
                with contextlib.suppress(Exception):
                    with acquire_lock(chap_dir / "chapter.json"):
                        chap_meta = load_chapter_metadata(chap_dir)
                        chap_meta.overall_status = CheckpointStatus.COMPLETED
                        chap_meta.updated_at = utc_iso_now()
                        atomic_write_json(chap_dir / "chapter.json", chap_meta)

            return {
                "project_slug": p_dir.name,
                "chapter_number": str(chapter_num),
                "video_path": str(final_video_path),
                "subtitle_path": str(srt_path),
                "quality": quality_clean,
                "resolution": f"{self.width}x{self.height}",
                "fps": float(self.fps),
                "video_duration_seconds": validation_res.get("duration_seconds", total_speech_dur),
                "audio_duration_seconds": total_speech_dur,
                "duration_diff_seconds": validation_res.get("duration_diff_seconds", 0.0),
                "validation": validation_res,
                "encoder": str(active_encoder),
                "cached_clips_count": cached_clips_count,
                "rendered_clips_count": rendered_clips_count,
                "cleaned_files_count": cleaned_count,
            }

        except Exception as e:
            logger.error(f"Render failed for chapter {chapter_num}: {e}")
            update_checkpoint(chap_dir, StageType.RENDER, CheckpointStatus.FAILED, error=str(e))
            raise


# Default singleton instance
video_engine = VideoEngine()
