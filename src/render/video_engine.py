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
        source_box: Optional[FocusBox] = None,
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
            cache_key = f"{p}_{mtime}_{self.width}x{self.height}_{video_mode}_{motion_type}_{str(focus_box)}_{str(source_box)}"
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

        if source_box is not None:
            bx0 = int(round(max(0.0, min(1.0, source_box.x_min)) * img.size[0]))
            by0 = int(round(max(0.0, min(1.0, source_box.y_min)) * img.size[1]))
            bx1 = int(round(max(0.0, min(1.0, source_box.x_max)) * img.size[0]))
            by1 = int(round(max(0.0, min(1.0, source_box.y_max)) * img.size[1]))
            if bx1 > bx0 and by1 > by0:
                img = img.crop((bx0, by0, bx1, by1))

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

    def render_chapter(self, project_dir, chapter_num, **options):
        """Production rendering always consumes the canonical VisualPlan."""
        from src.render.chapter_renderer import render_chapter
        return render_chapter(project_dir, chapter_num, **options)



# Default singleton instance
video_engine = VideoEngine()
