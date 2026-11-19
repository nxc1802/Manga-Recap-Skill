"""
src/render/watermark.py
Channel ownership watermark resolution, circular alpha masking, opacity blending,
and corner placement for video rendering.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple

from PIL import Image, ImageDraw

from src.core.storage import read_json

_IMAGE_EXTS = (".png", ".webp", ".jpg", ".jpeg")


def resolve_channel_watermark(
    project_dir: Optional[Path] = None,
    explicit_path: Optional[str] = None,
    channel_alias: Optional[str] = None,
    watermarks_dir: Optional[Path] = None,
) -> Optional[Path]:
    """
    Resolves the watermark image for a render job in priority order:
    1. Explicit path (`explicit_path`) if provided and exists.
    2. Explicit `channel_alias` or Q08 YouTube account alias from `series_setup_answers.json`
       matched against `data/watermarks/<alias>.{png,webp,jpg,jpeg}`.
    3. `data/watermarks/default.{png,webp,jpg,jpeg}` fallback if present.
    """
    if explicit_path:
        p = Path(explicit_path)
        if not p.is_absolute() and project_dir is not None:
            cand = (Path(project_dir) / p).resolve()
            if cand.is_file():
                return cand
        if p.is_file():
            return p.resolve()

    wm_root = Path(watermarks_dir) if watermarks_dir is not None else Path("data/watermarks")
    if not wm_root.is_dir():
        return None

    alias = (channel_alias or "").strip()
    if not alias and project_dir is not None:
        setup_file = Path(project_dir) / "series_setup_answers.json"
        if setup_file.is_file():
            try:
                setup = read_json(setup_file)
                q08 = (setup.get("answers") or {}).get("Q08") or {}
                raw_ans = str(q08.get("user_answer") or "").strip()
                if raw_ans and raw_ans.lower() not in {"no_upload", "none", "local"}:
                    # Handle formats like "muon_review:unlisted" or "muon_review (unlisted)"
                    alias = raw_ans.split(":")[0].split("(")[0].strip()
            except Exception:
                alias = ""

    candidates = [alias] if alias else []
    if "default" not in candidates:
        candidates.append("default")

    for name in candidates:
        if not name:
            continue
        for ext in _IMAGE_EXTS:
            cand = wm_root / f"{name}{ext}"
            if cand.is_file():
                return cand.resolve()
    return None


def prepare_watermark_rgba(
    image_path: Path,
    *,
    target_height: int = 1080,
    size_ratio: float = 0.207,
    opacity: float = 0.20,
    circular_crop: bool = True,
) -> Image.Image:
    """
    Loads a channel logo/stamp, resizes proportionally to `target_height * size_ratio`,
    optionally applies an anti-aliased circular mask (ideal for square JPG badges),
    and scales alpha by `opacity` (0.0 .. 1.0).
    """
    size_px = max(32, int(round(max(1, target_height) * max(0.03, min(0.35, float(size_ratio))))))
    alpha_scale = max(0.05, min(1.0, float(opacity)))

    with Image.open(image_path) as raw:
        has_alpha = "A" in raw.getbands()
        img = raw.convert("RGBA")

    # Center-crop to square when applying circular badge mask
    if circular_crop and img.width != img.height:
        side = min(img.width, img.height)
        left = (img.width - side) // 2
        top = (img.height - side) // 2
        img = img.crop((left, top, left + side, top + side))

    if img.width >= img.height:
        new_w = size_px
        new_h = max(1, int(round(img.height * (size_px / float(img.width)))))
    else:
        new_h = size_px
        new_w = max(1, int(round(img.width * (size_px / float(img.height)))))
    img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

    r, g, b, a = img.split()
    if circular_crop and not has_alpha:
        # 4x supersampled anti-aliased circular mask to remove square corners cleanly
        scale = 4
        mask_hi = Image.new("L", (new_w * scale, new_h * scale), 0)
        draw = ImageDraw.Draw(mask_hi)
        pad = scale
        draw.ellipse((pad, pad, new_w * scale - pad, new_h * scale - pad), fill=255)
        circle_mask = mask_hi.resize((new_w, new_h), Image.Resampling.LANCZOS)
        a = circle_mask

    a = a.point(lambda px: int(round(px * alpha_scale)))
    return Image.merge("RGBA", (r, g, b, a))


def compute_watermark_xy(
    frame_width: int,
    frame_height: int,
    wm_width: int,
    wm_height: int,
    position: str = "center_right",
    margin_px: int = 400,
) -> Tuple[int, int]:
    """Computes top-left (x, y) coordinates for corner/side watermark placement."""
    margin = max(0, int(margin_px))
    pos = (position or "center_right").strip().lower()
    if pos == "center_right":
        return max(0, frame_width - wm_width - margin), max(0, (frame_height - wm_height) // 2)
    if pos == "center_left":
        return margin, max(0, (frame_height - wm_height) // 2)
    if pos == "top_left":
        return margin, margin
    if pos == "bottom_left":
        return margin, max(0, frame_height - wm_height - margin)
    if pos == "bottom_right":
        return max(0, frame_width - wm_width - margin), max(0, frame_height - wm_height - margin)
    return max(0, frame_width - wm_width - margin), margin


def apply_watermark_to_frame(
    frame: Image.Image,
    watermark_rgba: Image.Image,
    *,
    position: str = "center_right",
    margin_px: int = 400,
) -> Image.Image:
    """Alpha-composites `watermark_rgba` onto a copy of `frame` and returns RGB."""
    base = frame.convert("RGBA")
    x, y = compute_watermark_xy(
        base.width, base.height, watermark_rgba.width, watermark_rgba.height,
        position=position, margin_px=margin_px,
    )
    base.alpha_composite(watermark_rgba, dest=(x, y))
    return base.convert("RGB")
