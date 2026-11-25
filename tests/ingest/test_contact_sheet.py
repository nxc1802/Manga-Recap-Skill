"""
tests/unit/test_contact_sheet.py
Unit tests for ContactSheetGenerator (1920x1920 4x4 grid sheets and crimson badges).
"""

from pathlib import Path
from PIL import Image
import pytest

from src.core.contact_sheet import ContactSheetGenerator
from src.core.schemas import PanelItem, PanelStatus


def test_contact_sheet_empty_panels(tmp_path):
    gen = ContactSheetGenerator(cell_size=480, columns=4, rows=4)
    sheets = gen.generate_sheets([], tmp_path / "panels", tmp_path / "contact_sheets")
    assert sheets == []


def test_contact_sheet_generation_and_dimensions(tmp_path):
    panels_dir = tmp_path / "panels"
    sheets_dir = tmp_path / "contact_sheets"
    panels_dir.mkdir()
    sheets_dir.mkdir()

    # Create 20 mock panel images (exceeds 1 sheet of 16 -> should create 2 sheets)
    panel_items = []
    for i in range(1, 21):
        filename = f"panel_{i:03d}.jpg"
        im = Image.new("RGB", (1080, 1350), (i * 10 % 255, 100, 150))
        im.save(panels_dir / filename)

        item = PanelItem(
            panel_id=f"panel_{i:03d}",
            sequence_number=i,
            filename=filename,
            relative_path=f"panels/{filename}",
            source_image="page_001.jpg",
            crop_box=(0, 0, 1080, 1350),
            width=1080,
            height=1350,
            aspect_ratio=0.8,
            sha256="0" * 64,
            status=PanelStatus.ACTIVE,
        )
        panel_items.append(item)

    gen = ContactSheetGenerator(cell_size=480, columns=4, rows=4)
    sheets = gen.generate_sheets(panel_items, panels_dir, sheets_dir)

    assert len(sheets) == 2
    assert sheets[0].sheet_id == "sheet_001"
    assert len(sheets[0].panel_ids) == 16
    assert sheets[1].sheet_id == "sheet_002"
    assert len(sheets[1].panel_ids) == 4

    sheet_1_path = sheets_dir / "sheet_001.jpg"
    assert sheet_1_path.exists()
    with Image.open(sheet_1_path) as s_img:
        assert s_img.size == (1920, 1920)

    sheet_2_path = sheets_dir / "sheet_002.jpg"
    assert sheet_2_path.exists()
    with Image.open(sheet_2_path) as s_img:
        assert s_img.size == (1920, 1920)
