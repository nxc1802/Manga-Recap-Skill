"""Fast-path canonical VisualPlan renderer, parallel shot encoder, and zero-reencode assembler."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFont

from src.core.logger import logger
from src.core.schemas import PanelManifest
from src.core.visual_contract import ProductionMotion, Region, Shot, VisualPlan
from src.production.hardware import (
    get_encoder_codec_args,
    get_hardware_encoder_lock,
    probe_ffmpeg_capabilities,
    resolve_video_encoder,
)
from src.render.subtitle import SubtitleCue, SubtitleGenerator, sanitize_subtitle_text
from src.render.visual_policy import evidence_path
from src.render.visual_plan_renderer import frame_schedule, render_frame

MAX_CANVAS_CACHE_ENTRIES = 512
_CANVAS_LOCK = threading.Lock()
_CANVAS_KEY_LOCKS: Dict[str, threading.Lock] = {}
_CAPABILITY_CACHE_LOCK = threading.Lock()
_FFMPEG_CAPS_CACHE: Optional[Dict[str, bool]] = None


def _get_ffmpeg_capabilities(force_refresh: bool = False) -> Dict[str, bool]:
    global _FFMPEG_CAPS_CACHE
    with _CAPABILITY_CACHE_LOCK:
        if _FFMPEG_CAPS_CACHE is None or force_refresh:
            _FFMPEG_CAPS_CACHE = probe_ffmpeg_capabilities()
        return dict(_FFMPEG_CAPS_CACHE)


def _escape_concat_path(path: Path) -> str:
    return str(path.resolve()).replace("'", "'\\''")


def _resolve_encoder_name(encoder: Optional[str]) -> str:
    req = (encoder or "libx264").strip().lower()
    if req == "auto":
        resolved, _, _ = resolve_video_encoder("auto")
        return resolved
    if req == "nvenc":
        return "h264_nvenc"
    if req == "videotoolbox":
        return "h264_videotoolbox"
    return req


def _region_from_any(value: Any) -> Region:
    if isinstance(value, Region):
        return value
    if isinstance(value, dict):
        return Region.model_validate(value)
    return Region()


def _motion_from_any(value: Any) -> ProductionMotion:
    if isinstance(value, ProductionMotion):
        return value
    return ProductionMotion(str(value))


def _compute_canvas_key(
    panel_id: str,
    panel_sha256: str,
    box: Region,
    target_width: int,
    target_height: int,
) -> str:
    raw = (
        f"{panel_id}:{panel_sha256}:"
        f"{box.x_min:.8f},{box.y_min:.8f},{box.x_max:.8f},{box.y_max:.8f}:"
        f"{target_width}x{target_height}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _compute_pan_geometry(
    img_w: int,
    img_h: int,
    box: Region,
    target_width: int,
    target_height: int,
    scroll_fraction_per_second: float,
) -> Dict[str, Any]:
    left = box.x_min * img_w
    top = box.y_min * img_h
    right = box.x_max * img_w
    bottom = box.y_max * img_h

    crop_w_f = right - left
    crop_h_f = bottom - top
    vis_h_f = min(crop_h_f, crop_w_f * target_height / float(target_width))
    raw_max_travel = max(0.0, crop_h_f - vis_h_f)

    x0 = max(0, min(img_w - 1, round(left)))
    crop_w = max(1, min(img_w - x0, round(right) - x0))
    y0 = max(0, min(img_h - 1, round(top)))
    crop_h = max(1, min(img_h - y0, round(vis_h_f)))
    safe_max_travel = max(0.0, min(raw_max_travel, float(img_h - y0 - crop_h)))
    rate_px_per_sec = max(0.0, float(scroll_fraction_per_second) * float(img_h))

    is_animated = safe_max_travel > 0.5 and rate_px_per_sec > 1e-6
    return {
        "x0": x0,
        "y0": y0,
        "crop_w": crop_w,
        "crop_h": crop_h,
        "safe_max_travel": safe_max_travel,
        "rate_px_per_sec": rate_px_per_sec,
        "is_animated": is_animated,
    }


def _get_or_create_static_canvas_png(
    panel_path: Path,
    box: Region,
    target_width: int,
    target_height: int,
    canvas_key: str,
    canvas_dir: Path,
    canvas_cache: Optional[Dict[str, Any]] = None,
) -> Path:
    with _CANVAS_LOCK:
        if canvas_cache is not None and canvas_key in canvas_cache:
            cached = Path(canvas_cache[canvas_key])
            if cached.is_file():
                return cached
        key_lock = _CANVAS_KEY_LOCKS.setdefault(canvas_key, threading.Lock())
        if len(_CANVAS_KEY_LOCKS) > MAX_CANVAS_CACHE_ENTRIES * 2:
            # Prune only unlocked/old entries when dictionary grows large.
            _CANVAS_KEY_LOCKS.clear()
            _CANVAS_KEY_LOCKS[canvas_key] = key_lock

    with key_lock:
        canvas_dir.mkdir(parents=True, exist_ok=True)
        png_path = canvas_dir / f"canvas_{canvas_key}.png"

        with _CANVAS_LOCK:
            if png_path.is_file() and png_path.stat().st_size > 0:
                if canvas_cache is not None:
                    _remember_canvas(canvas_cache, canvas_key, png_path)
                return png_path

        shot_proxy = Shot(
            shot_id="static_canvas",
            segment_id="canvas",
            source_panel_id="panel",
            source_box=box,
            motion_type=ProductionMotion.STATIC,
            duration_sec=1.0,
        )
        plan_proxy = VisualPlan(
            chapter_number="1",
            shots=[shot_proxy],
            target_width=target_width,
            target_height=target_height,
            fps=30,
            total_duration_sec=1.0,
        )
        with Image.open(panel_path) as image:
            rendered = render_frame(image, shot_proxy, plan_proxy, 0.0)
            tmp_png = canvas_dir / f".canvas_{canvas_key}.{os.getpid()}.{threading.get_ident()}.tmp.png"
            try:
                rendered.save(tmp_png, format="PNG", compress_level=1)
                with _CANVAS_LOCK:
                    if not png_path.is_file() or png_path.stat().st_size == 0:
                        tmp_png.replace(png_path)
                    else:
                        tmp_png.unlink(missing_ok=True)
                    if canvas_cache is not None:
                        _remember_canvas(canvas_cache, canvas_key, png_path)
            finally:
                tmp_png.unlink(missing_ok=True)

    return png_path


def _remember_canvas(cache: Dict[str, Any], key: str, path: Path) -> None:
    if len(cache) >= MAX_CANVAS_CACHE_ENTRIES and key not in cache:
        oldest = next(iter(cache), None)
        if oldest is not None:
            cache.pop(oldest, None)
    cache[key] = path


def _ensure_ffmpeg_safe_panel_image(panel_path: Path, cache_dir: Path, shot_id: str) -> Path:
    """Converts exotic image modes/containers (e.g. CMYK/WebP/palette) to RGB PNG for FFmpeg."""
    suffix = panel_path.suffix.lower()
    with Image.open(panel_path) as img:
        if suffix in {".jpg", ".jpeg", ".png"} and img.mode in {"RGB", "L", "RGBA"}:
            return panel_path
        cache_dir.mkdir(parents=True, exist_ok=True)
        norm_path = cache_dir / f"norm_{shot_id}_{hashlib.sha256(str(panel_path).encode()).hexdigest()[:10]}.png"
        if not norm_path.is_file():
            tmp = norm_path.with_suffix(".tmp.png")
            try:
                img.convert("RGB").save(tmp, format="PNG", compress_level=1)
                tmp.replace(norm_path)
            finally:
                tmp.unlink(missing_ok=True)
        return norm_path


def _resolve_ttf_font(font_size: int) -> ImageFont.ImageFont:
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Unicode MS.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ]
    fc_match = shutil.which("fc-match")
    if fc_match:
        try:
            res = subprocess.run(
                [fc_match, "-f", "%{file}", "DejaVu Sans:lang=vi"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            matched = res.stdout.strip()
            if matched and Path(matched).is_file():
                candidates.insert(0, matched)
        except Exception:
            pass
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, font_size)
            except Exception:
                continue
    return ImageFont.load_default()


def _render_cue_overlay_png(
    text: str,
    output_path: Path,
    canvas_width: int,
    canvas_height: int,
    font_size: int = 24,
) -> Path:
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    clean_text = sanitize_subtitle_text(text)
    img = Image.new("RGBA", (canvas_width, canvas_height), (0, 0, 0, 0))
    if not clean_text:
        img.save(output_path, format="PNG", compress_level=1)
        return output_path

    draw = ImageDraw.Draw(img)
    scaled_px = max(14, int(round(float(font_size) * (canvas_height / 720.0))))
    font = _resolve_ttf_font(scaled_px)

    lines = [ln.strip() for ln in clean_text.split("\n") if ln.strip()] or [clean_text]
    max_allowed_w = max(40, canvas_width - 32)
    while scaled_px > 12:
        widths = [draw.textbbox((0, 0), ln, font=font)[2] - draw.textbbox((0, 0), ln, font=font)[0] for ln in lines]
        if max(widths, default=0) <= max_allowed_w:
            break
        scaled_px -= 2
        font = _resolve_ttf_font(scaled_px)

    line_boxes = [draw.textbbox((0, 0), ln, font=font) for ln in lines]
    line_widths = [b[2] - b[0] for b in line_boxes]
    line_heights = [max(scaled_px, b[3] - b[1]) for b in line_boxes]

    line_gap = max(4, scaled_px // 5)
    total_text_h = sum(line_heights) + max(0, len(lines) - 1) * line_gap
    max_text_w = max(line_widths, default=0)
    pad_x = max(12, int(round(scaled_px * 0.7)))
    pad_y = max(6, int(round(scaled_px * 0.35)))
    box_w = min(canvas_width - 16, max_text_w + pad_x * 2)
    box_h = total_text_h + pad_y * 2
    margin_bottom = max(10, int(round(canvas_height * 0.045)))

    box_x0 = max(8, (canvas_width - box_w) // 2)
    box_y0 = max(8, canvas_height - margin_bottom - box_h)
    box_x1 = min(canvas_width - 8, box_x0 + box_w)
    box_y1 = min(canvas_height - 8, box_y0 + box_h)

    draw.rounded_rectangle([box_x0, box_y0, box_x1, box_y1], radius=8, fill=(0, 0, 0, 175))
    curr_y = box_y0 + pad_y
    outline = max(1, scaled_px // 14)
    for idx, line in enumerate(lines):
        lx = max(box_x0 + 4, (canvas_width - line_widths[idx]) // 2)
        for dx, dy in ((-outline, 0), (outline, 0), (0, -outline), (0, outline)):
            draw.text((lx + dx, curr_y + dy), line, font=font, fill=(0, 0, 0, 235))
        draw.text((lx, curr_y), line, font=font, fill=(255, 255, 255, 255))
        curr_y += line_heights[idx] + line_gap

    img.save(output_path, format="PNG", compress_level=1)
    return output_path


def _slice_cues_for_window(
    cues: Sequence[SubtitleCue],
    win_start: float,
    win_end: float,
) -> List[Dict[str, Any]]:
    sliced: List[Dict[str, Any]] = []
    for cue in cues:
        s = max(win_start, float(cue.start_seconds))
        e = min(win_end, float(cue.end_seconds))
        if e - s > 0.005:
            sliced.append(
                {
                    "start_sec": round(s - win_start, 6),
                    "end_sec": round(e - win_start, 6),
                    "text": cue.text,
                }
            )
    return sliced


def _build_overlay_concat_manifest(
    cues: Sequence[Dict[str, Any]],
    total_duration_sec: float,
    canvas_width: int,
    canvas_height: int,
    font_size: int,
    overlay_dir: Path,
) -> Optional[Path]:
    if not cues:
        return None

    overlay_dir.mkdir(parents=True, exist_ok=True)
    blank_png = overlay_dir / "blank.png"
    if not blank_png.is_file():
        Image.new("RGBA", (canvas_width, canvas_height), (0, 0, 0, 0)).save(blank_png, format="PNG", compress_level=1)

    timeline: List[Tuple[Path, float]] = []
    cursor = 0.0
    for idx, cue in enumerate(cues):
        s = max(cursor, min(total_duration_sec, float(cue["start_sec"])))
        e = max(s, min(total_duration_sec, float(cue["end_sec"])))
        if e - s <= 0.005:
            continue
        if s - cursor > 0.002:
            timeline.append((blank_png, s - cursor))
        cue_text = str(cue["text"])
        cue_hash = hashlib.sha256(f"{canvas_width}x{canvas_height}:{font_size}:{cue_text}".encode("utf-8")).hexdigest()[:16]
        cue_png = overlay_dir / f"cue_{idx:04d}_{cue_hash}.png"
        if not cue_png.is_file():
            _render_cue_overlay_png(cue_text, cue_png, canvas_width, canvas_height, font_size=font_size)
        timeline.append((cue_png, e - s))
        cursor = e

    if not timeline:
        return None
    if total_duration_sec - cursor > 0.002:
        timeline.append((blank_png, total_duration_sec - cursor))

    manifest_path = overlay_dir / "subs_concat.txt"
    lines: List[str] = []
    for png_p, dur in timeline:
        lines.append(f"file '{_escape_concat_path(png_p)}'\n")
        lines.append(f"duration {dur:.6f}\n")
    lines.append(f"file '{_escape_concat_path(timeline[-1][0])}'\n")
    manifest_path.write_text("".join(lines), encoding="utf-8")
    return manifest_path


def _write_local_srt(cues: Sequence[Dict[str, Any]], srt_path: Path) -> Path:
    from src.render.subtitle import format_srt_time

    lines: List[str] = []
    for idx, cue in enumerate(cues, 1):
        st = format_srt_time(float(cue["start_sec"]))
        et = format_srt_time(float(cue["end_sec"]))
        lines.append(f"{idx}\n{st} --> {et}\n{cue['text']}\n\n")
    srt_path.write_text("".join(lines), encoding="utf-8")
    return srt_path


def _run_ffmpeg_checked(cmd: List[str], *, cwd: Optional[Path] = None, encoder: str = "libx264", timeout: int = 600) -> None:
    if encoder == "h264_videotoolbox":
        with get_hardware_encoder_lock():
            res = subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=timeout)
    else:
        res = subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=timeout)
    if res.returncode:
        raise RuntimeError(f"FFmpeg fast renderer failed ({res.returncode}): {res.stderr.decode(errors='replace')[-2000:]}")


def build_shot_render_tasks(
    chapter_dir: Path,
    plan: VisualPlan,
    manifest: PanelManifest,
    *,
    work_dir: Path,
    encoder: str = "libx264",
    fps_override: Optional[int] = None,
    subtitle_cues: Optional[Sequence[SubtitleCue]] = None,
    burn_subtitles: bool = False,
    subtitle_font_size: int = 24,
    quality_mode: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Builds deterministic per-shot render tasks quantized to frame_schedule(plan)."""
    plan = VisualPlan.model_validate(plan.model_dump(mode="json"))
    effective_fps = int(fps_override) if fps_override is not None else plan.fps
    schedule_plan = plan if effective_fps == plan.fps else plan.model_copy(update={"fps": effective_fps})

    panels = {p.panel_id: p for p in manifest.panels}
    verified_paths: Dict[str, Path] = {}
    for shot in plan.shots:
        pid = shot.substitute_panel_id or shot.source_panel_id
        if pid not in panels or shot.source_panel_id not in panels:
            raise ValueError(f"Unknown shot evidence: {shot.shot_id}")
        if pid not in verified_paths:
            verified_paths[pid] = evidence_path(chapter_dir, panels[pid])

    work_dir = Path(work_dir).resolve()
    clips_dir = work_dir / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    resolved_enc = _resolve_encoder_name(encoder)

    tasks: List[Dict[str, Any]] = []
    cum_frames = 0
    for idx, (shot, frame_count) in enumerate(frame_schedule(schedule_plan)):
        pid = shot.substitute_panel_id or shot.source_panel_id
        panel = panels[pid]
        panel_path = verified_paths[pid]
        win_start = cum_frames / float(effective_fps)
        cum_frames += frame_count
        win_end = cum_frames / float(effective_fps)

        local_cues: List[Dict[str, Any]] = []
        if burn_subtitles and subtitle_cues:
            local_cues = _slice_cues_for_window(subtitle_cues, win_start, win_end)

        canvas_key = _compute_canvas_key(
            panel_id=pid,
            panel_sha256=panel.sha256,
            box=shot.source_box,
            target_width=plan.target_width,
            target_height=plan.target_height,
        )
        tasks.append(
            {
                "shot_index": idx,
                "shot_id": shot.shot_id,
                "segment_id": shot.segment_id,
                "panel_path": panel_path,
                "panel_width": int(panel.width),
                "panel_height": int(panel.height),
                "source_box": shot.source_box,
                "motion_type": shot.motion_type,
                "duration_sec": float(shot.duration_sec),
                "quantized_duration_sec": frame_count / float(effective_fps),
                "frame_count": int(frame_count),
                "scroll_fraction_per_second": float(shot.scroll_fraction_per_second),
                "target_width": int(plan.target_width),
                "target_height": int(plan.target_height),
                "fps": int(effective_fps),
                "encoder": resolved_enc,
                "quality_mode": quality_mode,
                "output_clip_path": clips_dir / f"shot_{idx:05d}_{shot.shot_id}.mp4",
                "canvas_key": canvas_key,
                "work_dir": work_dir,
                "burn_subtitles": bool(burn_subtitles),
                "subtitle_font_size": int(subtitle_font_size),
                "local_subtitle_cues": local_cues,
            }
        )
    return tasks


def render_shot_clip(
    task: Dict[str, Any],
    *,
    canvas_cache: Optional[Dict[str, Any]] = None,
    gpu_index: int = 0,
) -> Path:
    """Renders one shot clip using static-canvas or FFmpeg crop+scale pan fast paths."""
    panel_path = Path(task["panel_path"]).resolve()
    out_clip = Path(task["output_clip_path"]).resolve()
    out_clip.parent.mkdir(parents=True, exist_ok=True)
    work_dir = Path(task.get("work_dir") or out_clip.parent).resolve()

    target_w = int(task["target_width"])
    target_h = int(task["target_height"])
    fps = int(task["fps"])
    frame_count = int(task["frame_count"])
    duration_sec = float(task["duration_sec"])
    quantized_dur = float(task.get("quantized_duration_sec") or (frame_count / float(fps)))
    box = _region_from_any(task["source_box"])
    motion = _motion_from_any(task["motion_type"])
    scroll_rate = float(task.get("scroll_fraction_per_second", 0.0))
    encoder = _resolve_encoder_name(task.get("encoder", "libx264"))
    quality_mode = task.get("quality_mode")

    if "panel_width" in task and "panel_height" in task:
        img_w, img_h = int(task["panel_width"]), int(task["panel_height"])
    else:
        with Image.open(panel_path) as im:
            img_w, img_h = im.size

    canvas_key = task.get("canvas_key") or _compute_canvas_key(
        panel_id=str(panel_path.stem),
        panel_sha256=hashlib.sha256(panel_path.read_bytes()).hexdigest(),
        box=box,
        target_width=target_w,
        target_height=target_h,
    )

    pan_geom = (
        _compute_pan_geometry(img_w, img_h, box, target_w, target_h, scroll_rate)
        if motion == ProductionMotion.SLOW_PAN_DOWN
        else {"is_animated": False}
    )

    if pan_geom["is_animated"]:
        src_img = _ensure_ffmpeg_safe_panel_image(panel_path, work_dir / "canvases", str(task.get("shot_id", "shot")))
        input_args = ["-loop", "1", "-framerate", str(fps), "-i", str(src_img)]
        y_expr = (
            f"{pan_geom['y0']}+min({pan_geom['safe_max_travel']:.6f},"
            f"{pan_geom['rate_px_per_sec']:.6f}*min(t,{duration_sec:.6f}))"
        )
        base_vf = (
            f"format=rgb24,crop=w={pan_geom['crop_w']}:h={pan_geom['crop_h']}:"
            f"x={pan_geom['x0']}:y='{y_expr}',scale={target_w}:{target_h}:flags=lanczos"
        )
    else:
        canvas_png = _get_or_create_static_canvas_png(
            panel_path=panel_path,
            box=box,
            target_width=target_w,
            target_height=target_h,
            canvas_key=canvas_key,
            canvas_dir=work_dir / "canvases",
            canvas_cache=canvas_cache,
        )
        input_args = ["-loop", "1", "-framerate", str(fps), "-i", str(canvas_png)]
        base_vf = ""

    local_cues = list(task.get("local_subtitle_cues") or [])
    burn_subs = bool(task.get("burn_subtitles", False)) and bool(local_cues)
    font_size = int(task.get("subtitle_font_size", 24))

    partial = out_clip.with_name(f".{out_clip.stem}.{os.getpid()}.{threading.get_ident()}.partial.mp4")
    codec_args = get_encoder_codec_args(
        encoder,
        quality_mode=quality_mode,
        gpu_index=gpu_index,
        profile="high",
    )
    timescale = fps * 1000
    shot_sub_dir = work_dir / "shot_subs" / f"shot_{int(task.get('shot_index', 0)):05d}"

    try:
        cmd: List[str] = ["ffmpeg", "-v", "error", "-y", *input_args]
        run_cwd: Optional[Path] = None

        if burn_subs:
            caps = _get_ffmpeg_capabilities()
            shot_sub_dir.mkdir(parents=True, exist_ok=True)
            if caps.get("subtitles"):
                _write_local_srt(local_cues, shot_sub_dir / "shot_captions.srt")
                sub_f = f"subtitles=shot_captions.srt:force_style='Fontsize={font_size}'"
                vf_chain = ",".join(part for part in (base_vf, sub_f, "setsar=1,format=yuv420p") if part)
                cmd += ["-vf", vf_chain]
                run_cwd = shot_sub_dir
            else:
                ov_manifest = _build_overlay_concat_manifest(
                    local_cues,
                    total_duration_sec=quantized_dur,
                    canvas_width=target_w,
                    canvas_height=target_h,
                    font_size=font_size,
                    overlay_dir=shot_sub_dir,
                )
                if ov_manifest is not None:
                    cmd += ["-f", "concat", "-safe", "0", "-i", str(ov_manifest)]
                    pre = f"[0:v]{base_vf}[base];" if base_vf else ""
                    src_label = "[base]" if base_vf else "[0:v]"
                    fc = f"{pre}[1:v]format=rgba,fps={fps}[ov];{src_label}[ov]overlay=0:0:eof_action=pass,setsar=1,format=yuv420p[v]"
                    cmd += ["-filter_complex", fc, "-map", "[v]"]
                else:
                    vf_chain = ",".join(part for part in (base_vf, "setsar=1,format=yuv420p") if part)
                    cmd += ["-vf", vf_chain]
        else:
            vf_chain = ",".join(part for part in (base_vf, "setsar=1,format=yuv420p") if part)
            cmd += ["-vf", vf_chain]

        cmd += [
            *codec_args,
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(fps),
            "-fps_mode",
            "cfr",
            "-video_track_timescale",
            str(timescale),
            "-frames:v",
            str(frame_count),
            "-an",
            str(partial),
        ]
        _run_ffmpeg_checked(cmd, cwd=run_cwd, encoder=encoder)
        partial.replace(out_clip)
        return out_clip
    finally:
        partial.unlink(missing_ok=True)


def _probe_clip_geometry(clip_path: Path, default_height: int) -> Tuple[int, int, float]:
    res = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(clip_path),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if res.returncode == 0 and res.stdout:
        parsed = json.loads(res.stdout)
        stream = (parsed.get("streams") or [{}])[0]
        fmt = parsed.get("format") or {}
        w = int(stream.get("width") or round(default_height * 16 / 9))
        h = int(stream.get("height") or default_height)
        dur = float(fmt.get("duration") or 0.0)
        return w, h, dur
    return round(default_height * 16 / 9), default_height, 0.0


def _total_clips_duration(shot_clips: Sequence[Path], default_height: int) -> Tuple[int, int, float]:
    width, height = round(default_height * 16 / 9), default_height
    total = 0.0
    for idx, clip in enumerate(shot_clips):
        cw, ch, cdur = _probe_clip_geometry(clip, default_height)
        if idx == 0:
            width, height = cw, ch
        total += cdur
    return width, height, total


def _audio_codec_args(audio_path: Path) -> List[str]:
    if audio_path.suffix.lower() in {".m4a", ".aac", ".mp4"}:
        return ["-c:a", "copy"]
    return ["-c:a", "aac", "-b:a", "192k"]


def concat_shot_clips(
    shot_clips: List[Path],
    output_video: Path,
    *,
    work_dir: Optional[Path] = None,
    audio_path: Optional[Path] = None,
    subtitle_path: Optional[Path] = None,
    burn_subtitles: bool = False,
    target_height: int = 1080,
    fps: int = 30,
    encoder: str = "libx264",
    subtitle_font_size: int = 24,
    quality_mode: Optional[str] = None,
    gpu_index: int = 0,
) -> Path:
    """Concatenates uniform shot clips with -c:v copy, optionally muxing audio and burning subtitles."""
    if not shot_clips:
        raise ValueError("shot_clips must contain at least one clip")
    resolved_clips = [Path(p).resolve() for p in shot_clips]
    for clip in resolved_clips:
        if not clip.is_file():
            raise FileNotFoundError(f"Missing shot clip for concat: {clip}")

    output = Path(output_video).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(f".{output.stem}.{os.getpid()}.partial.mp4")

    temp_ctx = None
    if work_dir is None:
        temp_ctx = tempfile.TemporaryDirectory(prefix="p8_concat_")
        active_work = Path(temp_ctx.name).resolve()
    else:
        active_work = Path(work_dir).resolve()
        active_work.mkdir(parents=True, exist_ok=True)

    try:
        concat_list = active_work / "concat_list.txt"
        concat_list.write_text(
            "".join(f"file '{_escape_concat_path(clip)}'\n" for clip in resolved_clips),
            encoding="utf-8",
        )
        resolved_audio = Path(audio_path).resolve() if audio_path is not None else None
        resolved_sub = Path(subtitle_path).resolve() if subtitle_path is not None else None

        should_burn = bool(burn_subtitles and resolved_sub is not None)
        if not should_burn:
            cmd = ["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list)]
            if resolved_audio is not None:
                cmd += ["-i", str(resolved_audio), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", *_audio_codec_args(resolved_audio)]
            else:
                cmd += ["-map", "0:v:0", "-c:v", "copy", "-an"]
            cmd += ["-movflags", "+faststart", str(partial)]
            _run_ffmpeg_checked(cmd, encoder="copy")
            partial.replace(output)
            return output

        assert resolved_sub is not None
        resolved_enc = _resolve_encoder_name(encoder)
        codec_args = get_encoder_codec_args(resolved_enc, quality_mode=quality_mode, gpu_index=gpu_index, profile="high")
        caps = _get_ffmpeg_capabilities()
        timescale = int(fps) * 1000

        if caps.get("subtitles"):
            local_srt = active_work / "captions.srt"
            shutil.copy2(resolved_sub, local_srt)
            cmd = ["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list)]
            if resolved_audio is not None:
                cmd += ["-i", str(resolved_audio), "-map", "0:v:0", "-map", "1:a:0"]
            else:
                cmd += ["-map", "0:v:0", "-an"]
            cmd += [
                "-vf",
                f"subtitles=captions.srt:force_style='Fontsize={int(subtitle_font_size)}',setsar=1,format=yuv420p",
                *codec_args,
                "-pix_fmt",
                "yuv420p",
                "-r",
                str(fps),
                "-fps_mode",
                "cfr",
                "-video_track_timescale",
                str(timescale),
            ]
            if resolved_audio is not None:
                cmd += _audio_codec_args(resolved_audio)
            cmd += ["-movflags", "+faststart", str(partial)]
            _run_ffmpeg_checked(cmd, cwd=active_work, encoder=resolved_enc)
            partial.replace(output)
            return output

        # Fallback when FFmpeg lacks libass (e.g., Kaggle Ubuntu FFmpeg 6.1.1 / Homebrew FFmpeg):
        # render transparent Pillow PNG subtitle overlays and composite in a single overlay pass.
        cues = SubtitleGenerator().parse_srt(resolved_sub)
        if not cues:
            return concat_shot_clips(
                resolved_clips,
                output,
                work_dir=active_work,
                audio_path=resolved_audio,
                subtitle_path=None,
                burn_subtitles=False,
                target_height=target_height,
                fps=fps,
                encoder=resolved_enc,
            )

        vid_w, vid_h, total_dur = _total_clips_duration(resolved_clips, target_height)
        total_dur = max(total_dur, cues[-1].end_seconds)
        cue_dicts = [{"start_sec": c.start_seconds, "end_sec": c.end_seconds, "text": c.text} for c in cues]
        ov_manifest = _build_overlay_concat_manifest(
            cue_dicts,
            total_duration_sec=total_dur,
            canvas_width=vid_w,
            canvas_height=vid_h,
            font_size=int(subtitle_font_size),
            overlay_dir=active_work / "concat_subs",
        )
        if ov_manifest is None:
            return concat_shot_clips(
                resolved_clips,
                output,
                work_dir=active_work,
                audio_path=resolved_audio,
                subtitle_path=None,
                burn_subtitles=False,
                target_height=target_height,
                fps=fps,
                encoder=resolved_enc,
            )

        cmd = [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_list),
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(ov_manifest),
        ]
        if resolved_audio is not None:
            cmd += ["-i", str(resolved_audio)]
        fc = f"[1:v]format=rgba,fps={fps}[ov];[0:v][ov]overlay=0:0:eof_action=pass,setsar=1,format=yuv420p[v]"
        cmd += ["-filter_complex", fc, "-map", "[v]"]
        if resolved_audio is not None:
            cmd += ["-map", "2:a:0", *_audio_codec_args(resolved_audio)]
        else:
            cmd += ["-an"]
        cmd += [
            *codec_args,
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(fps),
            "-fps_mode",
            "cfr",
            "-video_track_timescale",
            str(timescale),
            "-movflags",
            "+faststart",
            str(partial),
        ]
        _run_ffmpeg_checked(cmd, encoder=resolved_enc)
        partial.replace(output)
        return output
    finally:
        partial.unlink(missing_ok=True)
        if temp_ctx is not None:
            temp_ctx.cleanup()


def render_visual_plan_fast(
    plan: VisualPlan,
    chapter_dir: Path,
    manifest: PanelManifest,
    output_path: Path,
    *,
    encoder: str = "libx264",
    max_workers: int = 4,
    gpu_index: int = 0,
    audio_path: Optional[Path] = None,
    subtitle_path: Optional[Path] = None,
    burn_subtitles: bool = False,
    subtitle_font_size: int = 24,
    quality_mode: Optional[str] = None,
) -> Path:
    """Renders a VisualPlan end-to-end using cached canvases, parallel shot clips, and -c copy concat."""
    output = Path(output_path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    cues: Optional[List[SubtitleCue]] = None
    if burn_subtitles and subtitle_path is not None and Path(subtitle_path).is_file():
        cues = SubtitleGenerator().parse_srt(subtitle_path)

    with tempfile.TemporaryDirectory(prefix="p8_fast_render_") as tmp_dir:
        work_dir = Path(tmp_dir).resolve()
        tasks = build_shot_render_tasks(
            Path(chapter_dir),
            plan,
            manifest,
            work_dir=work_dir,
            encoder=encoder,
            subtitle_cues=cues,
            burn_subtitles=bool(burn_subtitles and cues),
            subtitle_font_size=subtitle_font_size,
            quality_mode=quality_mode,
        )

        # Pre-populate static canvases once per unique canvas_key before parallel encode
        canvas_cache: Dict[str, Any] = {}
        worker_count = max(1, min(int(max_workers), len(tasks)))
        if worker_count > 1:
            with ThreadPoolExecutor(max_workers=worker_count) as pool:
                shot_clips = list(
                    pool.map(
                        lambda t: render_shot_clip(t, canvas_cache=canvas_cache, gpu_index=gpu_index),
                        tasks,
                    )
                )
        else:
            shot_clips = [render_shot_clip(t, canvas_cache=canvas_cache, gpu_index=gpu_index) for t in tasks]

        # Subtitles (if enabled) were already burned once inside each shot clip above;
        # assemble all shots + optional audio via zero-reencode -c:v copy concat.
        return concat_shot_clips(
            shot_clips,
            output,
            work_dir=work_dir,
            audio_path=audio_path,
            subtitle_path=None,
            burn_subtitles=False,
            target_height=plan.target_height,
            fps=plan.fps,
            encoder=encoder,
            subtitle_font_size=subtitle_font_size,
            quality_mode=quality_mode,
            gpu_index=gpu_index,
        )
