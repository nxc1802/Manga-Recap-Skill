"""
src/render/subtitle.py
Vietnamese SubRip (.srt) subtitle generation, validation, and rendering engine for Video Engine V2.
Strictly synchronized to measured utterance audio durations with typographic layout constraints.
"""

from __future__ import annotations

import os
import re
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from PIL import Image, ImageDraw, ImageFont

from src.core.logger import logger
from src.core.schemas import RecapScript, ScriptSegment


def format_srt_time(seconds: float) -> str:
    """Formats float seconds into standard SubRip timestamp 'HH:MM:SS,mmm'."""
    if seconds < 0.0:
        seconds = 0.0
    total_ms = int(round(seconds * 1000.0))
    hours = total_ms // 3600000
    total_ms %= 3600000
    minutes = total_ms // 60000
    total_ms %= 60000
    secs = total_ms // 1000
    millis = total_ms % 1000
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def parse_srt_time(time_str: str) -> float:
    """Parses SubRip timestamp 'HH:MM:SS,mmm' or 'HH:MM:SS.mmm' into float seconds."""
    clean_str = time_str.strip().replace(".", ",")
    parts = clean_str.split(":")
    if len(parts) != 3:
        raise ValueError(f"Invalid SRT timestamp format: {time_str}")
    hours = int(parts[0])
    minutes = int(parts[1])
    sec_parts = parts[2].split(",")
    seconds = int(sec_parts[0])
    millis = int(sec_parts[1]) if len(sec_parts) > 1 else 0
    return hours * 3600.0 + minutes * 60.0 + seconds + millis / 1000.0


def sanitize_subtitle_text(text: str) -> str:
    """Strips audible emotion cues (e.g. [cười], [thở dài]) and collapses whitespace."""
    if not text:
        return ""
    cleaned = re.sub(r"\[.*?\]", "", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def wrap_subtitle_lines(text: str, max_chars_per_line: int = 42, max_lines: int = 2) -> str:
    """Wraps text into at most max_lines for optimal video viewing."""
    if not text:
        return ""
    lines = textwrap.wrap(text, width=max_chars_per_line, break_long_words=False)
    if not lines:
        return ""
    if len(lines) > max_lines:
        first_lines = lines[: max_lines - 1]
        remaining = " ".join(lines[max_lines - 1 :])
        return "\n".join(first_lines + [remaining])
    return "\n".join(lines)


@dataclass
class SubtitleStyle:
    font_name: str = "Arial"
    font_size: int = 18          # Points for 1280x720 canvas
    primary_color: str = "&H00FFFFFF"  # White in ASS hex (&HAABBGGRR)
    outline_color: str = "&H00000000"  # Black outline
    outline_width: int = 2
    shadow_offset: int = 1
    margin_v: int = 28           # Pixels from bottom
    alignment: int = 2          # Bottom-center (num-pad 2)


@dataclass
class SubtitleCue:
    """A single SubRip subtitle card with timing and multi-line text."""
    index: int
    start_seconds: float
    end_seconds: float
    text: str

    @property
    def start_timecode(self) -> str:
        return format_srt_time(self.start_seconds)

    @property
    def end_timecode(self) -> str:
        return format_srt_time(self.end_seconds)

    @property
    def duration_seconds(self) -> float:
        return max(0.0, round(self.end_seconds - self.start_seconds, 4))

    def to_srt_block(self) -> str:
        return f"{self.index}\n{self.start_timecode} --> {self.end_timecode}\n{self.text}\n"


# Alias for backward compatibility with plan naming
SubtitleBlock = SubtitleCue


class SubtitleGenerator:
    """
    Generates and validates synchronized SRT subtitles and renders fallback PNG overlays.
    Enforces maximum 38 characters per line and maximum 2 lines per subtitle block.
    """

    def __init__(
        self,
        max_chars_per_line: int = 38,
        max_lines_per_block: int = 2,
        max_lines: Optional[int] = None,
        style: Optional[SubtitleStyle] = None,
    ):
        self.max_chars_per_line = max_chars_per_line
        self.max_lines_per_block = max_lines if max_lines is not None else max_lines_per_block
        self.style = style or SubtitleStyle()

    def sanitize(self, text: str) -> str:
        """Sanitizes text by removing audible cues and normalizing whitespace."""
        return sanitize_subtitle_text(text)

    def chunk_vietnamese_text(self, text: str) -> List[str]:
        """
        Splits Vietnamese text into subtitle blocks conforming to:
        - Maximum max_chars_per_line (default 38) characters per line
        - Maximum max_lines_per_block (default 2) lines per block
        - Preserves word boundaries and punctuation
        """
        clean_text = self.sanitize(text)
        if not clean_text:
            return []

        # If entire text fits within a single line
        if len(clean_text) <= self.max_chars_per_line:
            return [clean_text]

        words = clean_text.split()
        blocks: List[str] = []
        current_block_lines: List[str] = []
        current_line: List[str] = []
        current_line_len = 0

        for word in words:
            w_len = len(word)
            needed = w_len if not current_line else current_line_len + 1 + w_len
            if needed <= self.max_chars_per_line:
                current_line.append(word)
                current_line_len = needed
            else:
                if current_line:
                    current_block_lines.append(" ".join(current_line))
                current_line = [word]
                current_line_len = w_len

                if len(current_block_lines) == self.max_lines_per_block:
                    blocks.append("\n".join(current_block_lines))
                    current_block_lines = []

        if current_line:
            current_block_lines.append(" ".join(current_line))
        if current_block_lines:
            blocks.append("\n".join(current_block_lines))

        return blocks

    def build_cues_from_segments(
        self,
        segments: List[ScriptSegment],
        inter_segment_pause: float = 0.0,
    ) -> List[SubtitleCue]:
        """
        Constructs SubtitleCue objects aligned to actual audio durations.
        If an utterance splits into multiple blocks, duration is apportioned proportionally
        to character counts, with the final block clamped to exact utterance end time.
        """
        cues: List[SubtitleCue] = []
        current_time = 0.0
        cue_index = 1

        for seg in segments:
            dur = float(seg.duration or 0.0)
            clean_text = self.sanitize(seg.narrator_text)

            if not clean_text:
                current_time += max(0.0, dur) + inter_segment_pause
                continue

            if dur <= 0.0:
                # Fallback to character-count estimated duration to avoid dropping subtitle cues
                dur = max(0.5, len(clean_text) * 0.08)

            chunks = self.chunk_vietnamese_text(clean_text)
            if not chunks:
                chunks = [clean_text]

            total_chars = sum(len(c.replace("\n", "")) for c in chunks)
            seg_start_t = current_time
            seg_end_t = current_time + dur

            chunk_start_t = seg_start_t
            for i, chunk in enumerate(chunks):
                if total_chars > 0 and len(chunks) > 1:
                    chunk_len = len(chunk.replace("\n", ""))
                    chunk_dur = dur * (chunk_len / float(total_chars))
                    if i == len(chunks) - 1:
                        chunk_end_t = seg_end_t  # Clamp precisely to utterance end
                    else:
                        chunk_end_t = min(seg_end_t, chunk_start_t + chunk_dur)
                else:
                    chunk_end_t = seg_end_t

                cues.append(
                    SubtitleCue(
                        index=cue_index,
                        start_seconds=round(chunk_start_t, 3),
                        end_seconds=round(chunk_end_t, 3),
                        text=chunk,
                    )
                )
                cue_index += 1
                chunk_start_t = chunk_end_t

            current_time = seg_end_t + inter_segment_pause

        return cues

    def generate_srt_file(
        self,
        script: Union[RecapScript, List[ScriptSegment], List[Dict[str, Any]]],
        output_path: Union[str, Path],
        inter_segment_pause: float = 0.0,
    ) -> Path:
        """Writes standard UTF-8 .srt file from script data."""
        out = Path(output_path).resolve()
        out.parent.mkdir(parents=True, exist_ok=True)

        if isinstance(script, RecapScript):
            segments = script.segments
        elif isinstance(script, list) and script and isinstance(script[0], ScriptSegment):
            segments = script
        else:
            # Adapter for legacy dict lists: [{"duration": 2.5, "narrator_text": "..."}]
            segments = []
            for idx, item in enumerate(script):  # type: ignore
                dur_s = float(item.get("duration", 0.0))
                if "duration_ms" in item:
                    dur_s = float(item["duration_ms"]) / 1000.0
                text = item.get("narrator_text", "") or item.get("text", "")
                segments.append(
                    ScriptSegment(
                        seq_id=str(idx + 1),
                        narrator_text=str(text),
                        duration=dur_s,
                    )
                )

        cues = self.build_cues_from_segments(segments, inter_segment_pause=inter_segment_pause)

        with open(out, "w", encoding="utf-8") as f:
            for cue in cues:
                f.write(cue.to_srt_block() + "\n")

        logger.info(f"Generated SRT subtitle file with {len(cues)} cues at {out}")
        return out

    def parse_srt(self, srt_path: Union[str, Path]) -> List[SubtitleCue]:
        """Parses an existing SRT file into a list of SubtitleCue objects."""
        path = Path(srt_path).resolve()
        if not path.exists():
            raise FileNotFoundError(f"SRT file not found: {path}")

        content = path.read_text(encoding="utf-8").strip()
        if not content:
            return []

        blocks = re.split(r"\n\s*\n", content)
        cues: List[SubtitleCue] = []
        for block in blocks:
            lines = [line.strip() for line in block.split("\n") if line.strip()]
            if len(lines) < 3:
                continue
            try:
                idx = int(lines[0])
            except ValueError:
                continue

            time_match = re.match(r"(.*?)\s*-->\s*(.*)", lines[1])
            if not time_match:
                continue
            start_s = parse_srt_time(time_match.group(1))
            end_s = parse_srt_time(time_match.group(2))
            text = "\n".join(lines[2:])
            cues.append(SubtitleCue(index=idx, start_seconds=start_s, end_seconds=end_s, text=text))

        return cues

    def validate_srt_sync(
        self,
        srt_path: Union[str, Path],
        expected_total_duration: float,
        tolerance_seconds: float = 0.15,
    ) -> bool:
        """Validates timing monotonicity, sequential indices, and total duration alignment."""
        cues = self.parse_srt(srt_path)
        if not cues:
            return expected_total_duration <= tolerance_seconds

        prev_end = 0.0
        for i, cue in enumerate(cues):
            if cue.index != i + 1:
                logger.error(f"SRT validation failed: Non-sequential index {cue.index} (expected {i+1})")
                return False
            if cue.start_seconds < prev_end - 0.001:
                logger.error(f"SRT validation failed: Overlapping timestamps at index {cue.index}")
                return False
            if cue.end_seconds < cue.start_seconds:
                logger.error(f"SRT validation failed: Inverted duration at index {cue.index}")
                return False
            prev_end = cue.end_seconds

        last_end = cues[-1].end_seconds
        diff = abs(last_end - expected_total_duration)
        if diff > tolerance_seconds:
            logger.warning(
                f"SRT duration ({last_end:.2f}s) differs from expected ({expected_total_duration:.2f}s) by {diff:.2f}s"
            )
            return False

        return True

    def build_subtitle_filter(
        self,
        srt_path: Union[str, Path],
        font_size: Optional[int] = None,
        margin_v: Optional[int] = None,
    ) -> str:
        """
        Builds FFmpeg subtitles filter parameter with optional font_size and margin_v overrides.
        Escapes colons and backslashes in the path for safe FFmpeg command parsing.
        """
        escaped_path = str(srt_path).replace("\\", "/").replace(":", r"\:")
        st = self.style
        f_size = font_size if font_size is not None else st.font_size
        m_v = margin_v if margin_v is not None else st.margin_v
        style_expr = (
            f"FontName={st.font_name},"
            f"FontSize={f_size},"
            f"PrimaryColour={st.primary_color},"
            f"OutlineColour={st.outline_color},"
            f"Outline={st.outline_width},"
            f"Shadow={st.shadow_offset},"
            f"MarginV={m_v},"
            f"Alignment={st.alignment}"
        )
        return f"subtitles='{escaped_path}':force_style='{style_expr}'"

    def get_ffmpeg_subtitle_filter(self, srt_path: Union[str, Path]) -> str:
        """
        Builds FFmpeg subtitles filter parameter with styling.
        Escapes colons and backslashes in the path for safe FFmpeg command parsing.
        """
        return self.build_subtitle_filter(srt_path)

    def _get_pillow_font(self, font_size: int = 24) -> ImageFont.FreeTypeFont:
        """Finds a valid TrueType font for Pillow subtitle rendering with Vietnamese support."""
        font_paths = [
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/System/Library/Fonts/Supplemental/Arial Unicode MS.ttf",
            "/Library/Fonts/Arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "C:/Windows/Fonts/arial.ttf",
        ]
        for p in font_paths:
            if os.path.exists(p):
                try:
                    return ImageFont.truetype(p, font_size)
                except Exception:
                    pass
        return ImageFont.load_default()

    def render_overlay_png(
        self,
        text: str,
        output_path: Union[str, Path],
        canvas_width: int = 1280,
        canvas_height: int = 720,
    ) -> Path:
        """
        Pillow fallback: Renders a transparent 32-bit RGBA PNG image with a dark translucent
        pill box and crisp white subtitle text centered at the bottom of the canvas.
        Used when the FFmpeg binary lacks the native libass 'subtitles' filter.
        """
        out_p = Path(output_path).resolve()
        out_p.parent.mkdir(parents=True, exist_ok=True)

        clean_text = self.sanitize(text)
        img = Image.new("RGBA", (canvas_width, canvas_height), (0, 0, 0, 0))
        if not clean_text:
            img.save(out_p, format="PNG")
            return out_p

        draw = ImageDraw.Draw(img)
        font_size = max(18, int(canvas_height * 0.033))
        font = self._get_pillow_font(font_size)

        lines = clean_text.split("\n")
        line_heights = []
        line_widths = []
        for line in lines:
            bbox = draw.textbbox((0, 0), line, font=font)
            line_widths.append(bbox[2] - bbox[0])
            line_heights.append(bbox[3] - bbox[1])

        total_text_h = sum(line_heights) + (len(lines) - 1) * 6
        max_text_w = max(line_widths) if line_widths else 0

        pad_x = 20
        pad_y = 10
        box_w = max_text_w + pad_x * 2
        box_h = total_text_h + pad_y * 2

        margin_bottom = int(canvas_height * 0.04)
        box_x0 = (canvas_width - box_w) // 2
        box_y0 = canvas_height - margin_bottom - box_h
        box_x1 = box_x0 + box_w
        box_y1 = box_y0 + box_h

        # Draw dark translucent rounded background pill
        pill_radius = 8
        draw.rounded_rectangle(
            [box_x0, box_y0, box_x1, box_y1],
            radius=pill_radius,
            fill=(0, 0, 0, 160),
        )

        # Draw lines centered
        curr_y = box_y0 + pad_y
        for i, line in enumerate(lines):
            line_w = line_widths[i]
            line_x = (canvas_width - line_w) // 2
            # Text shadow / outline
            for dx, dy in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
                draw.text((line_x + dx, curr_y + dy), line, font=font, fill=(0, 0, 0, 220))
            # Text fill
            draw.text((line_x, curr_y), line, font=font, fill=(255, 255, 255, 255))
            curr_y += line_heights[i] + 6

        img.save(out_p, format="PNG")
        return out_p
