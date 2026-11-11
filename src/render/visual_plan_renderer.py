"""Reference frame compositor. Encoder choice never changes shot interpretation."""
from __future__ import annotations
import math
import subprocess
from pathlib import Path
from PIL import Image, ImageFilter, ImageOps
from src.core.visual_contract import ProductionMotion, VisualPlan
from src.render.visual_policy import evidence_path


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


def render_visual_plan(plan, chapter_dir, manifest, output_path, encoder="libx264"):
    # Revalidate even constructed/mutated in-memory models at the trust boundary.
    plan = VisualPlan.model_validate(plan.model_dump(mode="json"))
    panels = {p.panel_id: p for p in manifest.panels}
    paths = {}
    for shot in plan.shots:
        pid = shot.substitute_panel_id or shot.source_panel_id
        if pid not in panels or shot.source_panel_id not in panels:
            raise ValueError(f"Unknown shot evidence: {shot.shot_id}")
        paths[pid] = evidence_path(chapter_dir, panels[pid])
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(f".{output.stem}.partial.mp4")
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{plan.target_width}x{plan.target_height}", "-r", str(plan.fps),
           "-i", "pipe:0", "-an", "-c:v", encoder, "-pix_fmt", "yuv420p", str(partial)]
    # stderr goes to a file so a failing encoder cannot deadlock the frame pipe.
    error_path = output.with_suffix(".encoder.log")
    try:
        with error_path.open("wb") as error_file:
            process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=error_file)
            try:
                for shot, count in frame_schedule(plan):
                    with Image.open(paths[shot.substitute_panel_id or shot.source_panel_id]) as image:
                        for frame in render_shot(image, shot, plan, count):
                            process.stdin.write(frame.tobytes())
                process.stdin.close()
                code = process.wait(timeout=120)
                if code:
                    raise RuntimeError(f"Encoder failed ({code}): {error_path.read_text()[-2000:]}")
            except BaseException:
                process.kill()
                process.wait()
                raise
        partial.replace(output)
    finally:
        partial.unlink(missing_ok=True)
    return output
