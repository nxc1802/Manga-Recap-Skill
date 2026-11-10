"""
src/render/thumbnail_composer.py
Thumbnail composition engine for Manga Recap videos.
Supports:
- Offline Pillow 16:9 composition (1280x720 / 1920x1080).
- Candidate panel scanning, heuristic scoring, and role classification.
- Multi-panel layouts: 'split_duo_center' (Left + Center Hero + Right), 'single_hero_wings', 'cover_fill'.
- Vietnamese high-contrast typography with customizable colors, strokes, drop shadows, and fonts.
- YouTube duration pill safe-zone preservation (bottom-right 25%x20% clearance).
"""

from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageStat

from src.core.logger import setup_logger

logger = setup_logger("thumbnail_composer")


# Font discovery priority list for cross-platform Vietnamese rendering
FONT_CANDIDATE_PATHS = [
    # macOS
    "/System/Library/Fonts/Supplemental/Arial Black.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Tahoma Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Rounded Bold.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/HelveticaNeue.ttc",
    # Linux (Debian / Ubuntu / Kaggle)
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf",
]


def resolve_font(size: int, font_hint: Optional[str] = None) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Finds the best bold font supporting Vietnamese diacritics."""
    if font_hint and Path(font_hint).exists():
        try:
            return ImageFont.truetype(font_hint, size)
        except Exception as e:
            logger.warning(f"Failed to load user-specified font '{font_hint}': {e}")

    for fp in FONT_CANDIDATE_PATHS:
        if os.path.exists(fp):
            try:
                return ImageFont.truetype(fp, size)
            except Exception:
                continue

    logger.warning("No system TrueType font found; falling back to default PIL font.")
    return ImageFont.load_default()


def calculate_panel_score(img: Image.Image) -> float:
    """
    Computes an aesthetic/dramatic score for a panel:
    Higher score for high contrast, color variance, and appropriate size.
    """
    w, h = img.size
    if w < 100 or h < 100:
        return 0.0

    stat = ImageStat.Stat(img.convert("RGB"))
    # Color variance across RGB channels
    var = sum(stat.var) / 3.0
    # Saturation heuristic (mean difference between channels)
    r, g, b = stat.mean
    sat = (max(r, g, b) - min(r, g, b)) / max(1.0, max(r, g, b))

    # Resolution weight
    res_factor = min(1.0, math.sqrt(w * h) / 1000.0)

    # Edge contrast
    gray = img.convert("L")
    edges = gray.filter(ImageFilter.FIND_EDGES)
    edge_stat = ImageStat.Stat(edges)
    edge_mean = edge_stat.mean[0]

    score = (var / 1500.0) * 0.4 + (sat * 100.0) * 0.2 + (edge_mean / 25.0) * 0.3 + (res_factor * 10.0) * 0.1
    return round(score, 2)


def scan_thumbnail_candidates(
    panels_dir: Path,
    script_segments: Optional[List[Dict[str, Any]]] = None,
    limit: int = 8,
) -> List[Dict[str, Any]]:
    """
    Scans panel crops in chapter directory, scores them, and assigns suggested roles.
    """
    panels_dir = Path(panels_dir).resolve()
    if not panels_dir.exists():
        return []

    panel_files = sorted(list(panels_dir.glob("panel_*.jpg")) + list(panels_dir.glob("panel_*.png")))
    if not panel_files:
        return []

    # Map referenced panels from script
    script_referenced = set()
    character_mapping = {}
    if script_segments:
        for seg in script_segments:
            char = seg.get("character", "Narrator")
            for p_id in seg.get("panel_ids", []):
                p_name = Path(p_id).name
                script_referenced.add(p_name)
                if char not in character_mapping and char != "Narrator":
                    character_mapping[p_name] = char

    results = []
    total_panels = len(panel_files)

    for idx, p_path in enumerate(panel_files):
        try:
            with Image.open(p_path) as im:
                w, h = im.size
                score = calculate_panel_score(im)
                is_ref = p_path.name in script_referenced
                char = character_mapping.get(p_path.name, "")

                # Suggest role based on aspect ratio and position
                rel_pos = idx / max(1, total_panels)
                aspect = w / float(h)

                role = "center_hero"
                description = "Chiến đấu / Thần thái cao trào"

                if aspect > 1.3:
                    role = "background_scene"
                    description = "Toàn cảnh bối cảnh / Ma thú"
                elif rel_pos < 0.25:
                    role = "left_ally"
                    description = "Nhân vật mở đầu / Xuất phát điểm"
                elif rel_pos > 0.70:
                    role = "right_enemy"
                    description = "Kẻ địch / Biến cố cao trào"
                elif char:
                    role = "center_hero"
                    description = f"Khoảnh khắc hành động ({char})"

                results.append({
                    "panel_id": p_path.name,
                    "path": str(p_path),
                    "index": idx + 1,
                    "width": w,
                    "height": h,
                    "aspect_ratio": round(aspect, 2),
                    "score": score,
                    "is_script_referenced": is_ref,
                    "character": char,
                    "suggested_role": role,
                    "description": description,
                })
        except Exception as e:
            logger.debug(f"Skipping candidate {p_path}: {e}")

    # Sort primarily by score descending
    results.sort(key=lambda x: (x["is_script_referenced"], x["score"]), reverse=True)
    return results[:limit]


def draw_stroked_text(
    draw: ImageDraw.ImageDraw,
    xy: Tuple[int, int],
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    fill: str,
    stroke_fill: str = "black",
    stroke_width: int = 6,
    shadow_offset: Tuple[int, int] = (4, 4),
    shadow_fill: str = "black",
) -> None:
    """Draws high-contrast typography with outer stroke and drop shadow."""
    x, y = xy
    sx, sy = shadow_offset

    # Draw drop shadow
    if sx > 0 or sy > 0:
        for dx in range(-stroke_width, stroke_width + 1):
            for dy in range(-stroke_width, stroke_width + 1):
                draw.text((x + sx + dx, y + sy + dy), text, font=font, fill=shadow_fill)

    # Draw stroked text
    draw.text(
        (x, y),
        text,
        font=font,
        fill=fill,
        stroke_width=stroke_width,
        stroke_fill=stroke_fill,
    )


def compose_thumbnail(
    left_image_path: Optional[str | Path] = None,
    center_image_path: Optional[str | Path] = None,
    right_image_path: Optional[str | Path] = None,
    target_width: int = 1280,
    target_height: int = 720,
    # Typography settings
    has_text: bool = True,
    title: Optional[str] = None,
    subtitle: Optional[str] = None,
    badge: Optional[str] = None,
    font_hint: Optional[str] = None,
    color_preset: str = "yellow_black",
    stroke_width: int = 6,
    output_path: Optional[str | Path] = None,
) -> Image.Image:
    """
    Composes a 16:9 YouTube thumbnail following the Multimodal Split-Screen + Hero standard.
    """
    canvas = Image.new("RGB", (target_width, target_height), (15, 15, 20))

    # Resolve center, left, right image inputs
    center_path = Path(center_image_path) if center_image_path else None
    left_path = Path(left_image_path) if left_image_path else None
    right_path = Path(right_image_path) if right_image_path else None

    # Fallback to center if left/right are missing
    if not left_path and center_path:
        left_path = center_path
    if not right_path and center_path:
        right_path = center_path
    if not center_path and left_path:
        center_path = left_path

    if not center_path or not center_path.exists():
        raise FileNotFoundError(f"Center/Main image path '{center_path}' does not exist.")

    # 1. Background Layer (Left ~45% + Right ~45%)
    # Left background
    if left_path and left_path.exists():
        with Image.open(left_path) as limg:
            l_rgb = limg.convert("RGB")
            # Enhance contrast and saturation
            l_rgb = ImageEnhance.Contrast(l_rgb).enhance(1.15)
            l_rgb = ImageEnhance.Color(l_rgb).enhance(1.10)
            # Cover left 50%
            lw = int(target_width * 0.52)
            lh = target_height
            # Resize preserving aspect
            l_scale = max(lw / l_rgb.width, lh / l_rgb.height)
            l_crop = l_rgb.resize((int(l_rgb.width * l_scale), int(l_rgb.height * l_scale)), Image.Resampling.LANCZOS)
            # Center crop
            l_cx = (l_crop.width - lw) // 2
            l_cy = (l_crop.height - lh) // 2
            l_final = l_crop.crop((l_cx, l_cy, l_cx + lw, l_cy + lh))
            canvas.paste(l_final, (0, 0))

    # Right background
    if right_path and right_path.exists():
        with Image.open(right_path) as rimg:
            r_rgb = rimg.convert("RGB")
            r_rgb = ImageEnhance.Contrast(r_rgb).enhance(1.15)
            r_rgb = ImageEnhance.Color(r_rgb).enhance(1.10)
            rw = int(target_width * 0.52)
            rh = target_height
            r_scale = max(rw / r_rgb.width, rh / r_rgb.height)
            r_crop = r_rgb.resize((int(r_rgb.width * r_scale), int(r_rgb.height * r_scale)), Image.Resampling.LANCZOS)
            r_cx = (r_crop.width - rw) // 2
            r_cy = (r_crop.height - rh) // 2
            r_final = r_crop.crop((r_cx, r_cy, r_cx + rw, r_cy + rh))
            canvas.paste(r_final, (target_width - rw, 0))

    # Apply soft dark gradient / blur between sides to unify
    dark_overlay = Image.new("RGBA", (target_width, target_height), (0, 0, 0, 0))
    d_draw = ImageDraw.Draw(dark_overlay)

    # Edge vignette and bottom gradient for typography
    for y in range(int(target_height * 0.55), target_height):
        alpha = int(210 * ((y - target_height * 0.55) / (target_height * 0.45)))
        d_draw.line([(0, y), (target_width, y)], fill=(5, 5, 10, alpha))

    # Vertical center shadow for divider depth
    cw = target_width // 2
    for x in range(cw - 120, cw + 120):
        dist = abs(x - cw) / 120.0
        alpha = int(90 * (1.0 - dist))
        d_draw.line([(x, 0), (x, target_height)], fill=(0, 0, 0, alpha))

    canvas = Image.alpha_composite(canvas.convert("RGBA"), dark_overlay).convert("RGB")

    # 2. Center Hero Layer
    if center_path and center_path.exists() and (center_path != left_path or right_path != left_path):
        with Image.open(center_path) as cimg:
            c_rgb = cimg.convert("RGB")
            c_rgb = ImageEnhance.Contrast(c_rgb).enhance(1.25)
            c_rgb = ImageEnhance.Color(c_rgb).enhance(1.20)
            c_rgb = ImageEnhance.Sharpness(c_rgb).enhance(1.30)

            # Fit hero centered with safe margin
            hero_h = int(target_height * 0.90)
            hero_w = int(c_rgb.width * (hero_h / float(c_rgb.height)))
            if hero_w > int(target_width * 0.55):
                hero_w = int(target_width * 0.55)
                hero_h = int(c_rgb.height * (hero_w / float(c_rgb.width)))

            c_hero = c_rgb.resize((hero_w, hero_h), Image.Resampling.LANCZOS)

            # Center position
            hx = (target_width - hero_w) // 2
            hy = int(target_height * 0.05)

            # Add rim-light / drop shadow around hero
            hero_mask = Image.new("L", (hero_w, hero_h), 255)
            # Paste hero
            canvas.paste(c_hero, (hx, hy))

    # 3. Typography Layer (If enabled)
    if has_text and (title or subtitle or badge):
        draw = ImageDraw.Draw(canvas)

        # Style resolution
        color_map = {
            "yellow_black": ("#FFE600", "#FF4141"),   # Line 1 Yellow, Line 2 Crimson
            "red_white": ("#FF2A2A", "#FFFFFF"),      # Line 1 Red, Line 2 White
            "cyan_black": ("#00F5FF", "#FFE600"),     # Line 1 Cyan, Line 2 Yellow
            "gold_luxury": ("#FFD700", "#FFF8DC"),    # Gold & Ivory
            "white_clean": ("#FFFFFF", "#FFE600"),    # White & Yellow
        }
        fill_1, fill_2 = color_map.get(color_preset, ("#FFE600", "#FF4141"))

        # Badge (top-left)
        if badge:
            badge_font = resolve_font(24, font_hint=font_hint)
            b_text = badge.strip().upper()
            bbox = badge_font.getbbox(b_text)
            bw = (bbox[2] - bbox[0]) + 30
            bh = (bbox[3] - bbox[1]) + 16
            bx = 36
            by = 32

            # Badge pill background
            draw.rounded_rectangle([bx, by, bx + bw, by + bh], radius=8, fill=(200, 20, 30))
            draw.text((bx + 15, by + 6), b_text, font=badge_font, fill="white")

        # Title & Subtitle Typography (Bottom Left / Center, avoiding bottom-right 25%x20%)
        # YouTube Duration Badge Safe Zone: width - 320 to width, height - 144 to height
        title_font = resolve_font(52, font_hint=font_hint)
        sub_font = resolve_font(38, font_hint=font_hint)

        curr_y = int(target_height * 0.68)
        left_margin = 45

        if title:
            draw_stroked_text(
                draw=draw,
                xy=(left_margin, curr_y),
                text=title.strip().upper(),
                font=title_font,
                fill=fill_1,
                stroke_fill="black",
                stroke_width=stroke_width,
                shadow_offset=(5, 5),
            )
            curr_y += 62

        if subtitle:
            draw_stroked_text(
                draw=draw,
                xy=(left_margin, curr_y),
                text=subtitle.strip().upper(),
                font=sub_font,
                fill=fill_2,
                stroke_fill="black",
                stroke_width=max(4, stroke_width - 1),
                shadow_offset=(4, 4),
            )

    # Save output if path specified
    if output_path:
        out_p = Path(output_path).resolve()
        out_p.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(out_p, format="JPEG", quality=95, optimize=True)
        logger.info(f"Thumbnail saved to: {out_p}")

    return canvas
