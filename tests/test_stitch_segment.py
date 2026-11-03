import pytest
from pathlib import Path
from PIL import Image
import os
from src.core.stitch_segment import preprocess_and_segment_images

def test_preprocess_empty_dir(temp_dir):
    raw_dir = temp_dir / "raw"
    out_dir = temp_dir / "panels"
    raw_dir.mkdir()
    out_dir.mkdir()
    
    # Run segmentation on empty directory
    panels = preprocess_and_segment_images(raw_dir, out_dir)
    assert panels == []

def test_preprocess_stitch_and_segment(temp_dir):
    raw_dir = temp_dir / "raw"
    out_dir = temp_dir / "panels"
    raw_dir.mkdir()
    out_dir.mkdir()
    
    # Create 2 simple page images with white background and a black box (which will act as a panel)
    # Separated by white margins (which will act as gutters)
    # Image 1
    img1 = Image.new("RGB", (800, 1000), "white")
    # Draw a 200px tall black box in the middle of page 1
    for y in range(300, 500):
        for x in range(200, 600):
            img1.putpixel((x, y), (0, 0, 0))
    img1.save(raw_dir / "image_1.jpg")
    
    # Image 2
    img2 = Image.new("RGB", (800, 1000), "white")
    # Draw a 200px tall black box in the middle of page 2
    for y in range(400, 600):
        for x in range(200, 600):
            img2.putpixel((x, y), (0, 0, 0))
    img2.save(raw_dir / "image_2.jpg")
    
    # Run segmentation
    panels = preprocess_and_segment_images(raw_dir, out_dir, min_gutter_height=5, min_panel_height=10)
    
    # Since we drew solid color regions separated by white gutters, it should detect panels
    assert len(panels) >= 1
    for p in panels:
        assert p.exists()
        assert p.suffix == ".jpg"
        # Verify it is in the output directory
        assert p.parent == out_dir
