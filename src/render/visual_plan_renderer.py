"""Reference frame compositor. Encoder choice never changes shot interpretation."""
from __future__ import annotations
from pathlib import Path
from PIL import Image, ImageFilter, ImageOps
from src.core.visual_contract import ProductionMotion, VisualPlan


def render_frame(image: Image.Image, shot, plan: VisualPlan, elapsed: float) -> Image.Image:
    if not 0 <= elapsed <= shot.duration_sec:
        raise ValueError("Frame time outside shot")
    image = image.convert("RGB")
    width, height = image.size
    box = shot.source_box
    left, top, right, bottom = box.x_min * width, box.y_min * height, box.x_max * width, box.y_max * height
    if shot.motion_type == ProductionMotion.SLOW_PAN_DOWN:
        visible_height = min(bottom - top, (right - left) * plan.target_height / plan.target_width)
        travel = min(bottom - top - visible_height, shot.scroll_fraction_per_second * height * elapsed)
        top += travel
        bottom = top + visible_height
    crop = image.crop((round(left), round(top), max(round(left)+1, round(right)), max(round(top)+1, round(bottom))))
    size = (plan.target_width, plan.target_height)
    canvas = ImageOps.fit(crop, size).filter(ImageFilter.GaussianBlur(radius=20))
    subject = ImageOps.contain(crop, size, Image.Resampling.LANCZOS)
    canvas.paste(subject, ((size[0] - subject.width) // 2, (size[1] - subject.height) // 2))
    return canvas


def frame_schedule(plan):
    # Quantize cumulative boundaries, never round each shot independently.
    end = 0
    elapsed = 0.0
    for shot in plan.shots:
        elapsed += shot.duration_sec
        next_end = round(elapsed * plan.fps)
        count = next_end - end
        if count <= 0:
            raise ValueError(f"Shot too short for requested fps: {shot.shot_id}")
        yield shot, count
        end = next_end


def render_shot(image, shot, plan, frame_count):
    for index in range(frame_count):
        yield render_frame(image, shot, plan, min(index / plan.fps, shot.duration_sec))


def render_visual_plan(
    plan,
    chapter_dir,
    manifest,
    output_path,
    encoder="libx264",
    *,
    audio_path=None,
    subtitle_path=None,
    burn_subtitles=False,
    subtitle_font_size=24,
    max_workers=4,
    gpu_index=0,
    quality_mode=None,
):
    from src.render.fast_renderer import render_visual_plan_fast

    return render_visual_plan_fast(
        plan=plan,
        chapter_dir=Path(chapter_dir),
        manifest=manifest,
        output_path=Path(output_path),
        encoder=encoder or "libx264",
        max_workers=int(max_workers),
        gpu_index=int(gpu_index),
        audio_path=Path(audio_path) if audio_path is not None else None,
        subtitle_path=Path(subtitle_path) if subtitle_path is not None else None,
        burn_subtitles=bool(burn_subtitles),
        subtitle_font_size=int(subtitle_font_size),
        quality_mode=quality_mode,
    )
