"""
src/core/contact_sheet.py
4x4 Contact Sheet Generator for Visual OCR and Panel Review.
Generates 1920x1920 JPEG review sheets with crimson badges (#001..).
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from PIL import Image, ImageDraw, ImageFont

from src.core.logger import logger
from src.core.schemas import ContactSheetInfo, PanelItem


class ContactSheetGenerator:
    """
    Renders 4x4 grid review sheets (1920x1920 px) of segmented manga panels.
    Each panel is thumbnail-fitted and badged with a semi-transparent crimson sequence ID.
    Enables coding agents to inspect 16 panels in a single visual context turn.
    """

    def __init__(
        self,
        cell_size: int = 480,
        columns: int = 4,
        rows: int = 4,
        quality: int = 88,
    ):
        self.cell_size = cell_size
        self.columns = columns
        self.rows = rows
        self.quality = quality
        self.panels_per_sheet = columns * rows
        self.canvas_width = cell_size * columns
        self.canvas_height = cell_size * rows

    def _get_font(self) -> ImageFont.ImageFont:
        font_candidates = [
            "/System/Library/Fonts/SFNSMono.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/Library/Fonts/Arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ]
        for f in font_candidates:
            if Path(f).exists():
                try:
                    return ImageFont.truetype(f, 15)
                except Exception:
                    pass
        return ImageFont.load_default()

    def generate_sheets(
        self,
        panels: List[PanelItem],
        panels_dir: Path,
        output_dir: Path,
    ) -> List[ContactSheetInfo]:
        """
        Generates 4x4 contact sheets from segmented panel files.
        Saves output as sheet_001.jpg, sheet_002.jpg, etc.
        Returns a list of ContactSheetInfo metadata objects.
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        if not panels:
            logger.warning("ContactSheetGenerator: No panels provided, skipping sheet generation.")
            return []

        sheets: List[ContactSheetInfo] = []
        font = self._get_font()

        # Chunk panels into groups of 16 (4x4)
        chunks = [
            panels[i:i + self.panels_per_sheet]
            for i in range(0, len(panels), self.panels_per_sheet)
        ]

        for sheet_idx, chunk in enumerate(chunks, start=1):
            sheet_id = f"sheet_{sheet_idx:03d}"
            filename = f"{sheet_id}.jpg"
            out_path = output_dir / filename

            # 1. Create 1920x1920 canvas with dark charcoal background (#181818)
            canvas = Image.new("RGBA", (self.canvas_width, self.canvas_height), (24, 24, 24, 255))
            draw = ImageDraw.Draw(canvas)

            # Draw subtle 1px grid lines (#323232)
            for c in range(1, self.columns):
                x = c * self.cell_size
                draw.line([(x, 0), (x, self.canvas_height)], fill=(50, 50, 50, 255), width=1)
            for r in range(1, self.rows):
                y = r * self.cell_size
                draw.line([(0, y), (self.canvas_width, y)], fill=(50, 50, 50, 255), width=1)

            sheet_panel_ids: List[str] = []

            # 2. Render each panel thumbnail into its respective grid cell
            for cell_idx, panel in enumerate(chunk):
                col = cell_idx % self.columns
                row = cell_idx // self.columns
                cell_x = col * self.cell_size
                cell_y = row * self.cell_size

                sheet_panel_ids.append(panel.panel_id)
                panel_file = panels_dir / panel.filename

                if panel_file.exists():
                    try:
                        with Image.open(panel_file) as p_img:
                            thumb = p_img.convert("RGBA")
                            # Fit thumbnail into cell with 10px margin (460x460 max)
                            max_thumb_size = self.cell_size - 20
                            thumb.thumbnail((max_thumb_size, max_thumb_size), Image.Resampling.LANCZOS)
                            thumb_w, thumb_h = thumb.size

                            # Center inside cell
                            offset_x = cell_x + (self.cell_size - thumb_w) // 2
                            offset_y = cell_y + (self.cell_size - thumb_h) // 2
                            canvas.paste(thumb, (offset_x, offset_y), thumb)
                            thumb.close()
                    except Exception as e:
                        logger.error(f"ContactSheetGenerator: Failed rendering panel {panel.filename}: {e}")

                # 3. Render semi-transparent crimson badge (#001..)
                badge_x = cell_x + 12
                badge_y = cell_y + 12
                badge_w = 68
                badge_h = 24

                # Create badge overlay with alpha
                badge_layer = Image.new("RGBA", (self.canvas_width, self.canvas_height), (0, 0, 0, 0))
                b_draw = ImageDraw.Draw(badge_layer)

                # Crimson red background (220, 20, 60, 204), white outline (255, 255, 255, 180)
                b_draw.rounded_rectangle(
                    [(badge_x, badge_y), (badge_x + badge_w, badge_y + badge_h)],
                    radius=4,
                    fill=(220, 20, 60, 204),
                    outline=(255, 255, 255, 180),
                    width=1,
                )

                # Badge text (e.g., #001)
                seq_text = f"#{panel.sequence_number:03d}"
                b_draw.text(
                    (badge_x + 8, badge_y + 4),
                    seq_text,
                    fill=(255, 255, 255, 255),
                    font=font,
                )

                canvas = Image.alpha_composite(canvas, badge_layer)

            # 4. Save progressive JPEG
            rgb_canvas = canvas.convert("RGB")
            rgb_canvas.save(out_path, "JPEG", quality=self.quality, progressive=True)
            rgb_canvas.close()
            canvas.close()

            sheets.append(
                ContactSheetInfo(
                    sheet_id=sheet_id,
                    relative_path=f"contact_sheets/{filename}",
                    panel_ids=sheet_panel_ids,
                    columns=self.columns,
                    rows=self.rows,
                )
            )

        logger.info(f"ContactSheetGenerator: Generated {len(sheets)} contact sheets in {output_dir}")
        return sheets
