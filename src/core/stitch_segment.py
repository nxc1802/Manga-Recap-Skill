import os
import re
from pathlib import Path
from PIL import Image
import numpy as np
from src.core.logger import logger

def preprocess_and_segment_images(
    raw_pages_dir: Path,
    output_panels_dir: Path,
    threshold_variance: float = 8.0,
    min_gutter_height: int = 12,
    min_panel_height: int = 40
) -> list[Path]:
    """
    Stitches all raw pages in raw_pages_dir vertically, segments them into individual panels
    by identifying horizontal gutters (margins), discards solid color spacer panels, and
    saves the final panels to output_panels_dir.

    Returns:
        List of Path objects pointing to the saved panel images.
    """
    logger.info(f"Stitch & Segment: Scanning directory {raw_pages_dir}...")
    
    # 1. Read and sort page images numerically (e.g. image_1.jpg, image_2.jpg... image_10.jpg)
    try:
        # Match image_*.jpg, image_*.png, image_*.webp, etc.
        all_files = list(raw_pages_dir.glob("image_*"))
        if not all_files:
            logger.warning(f"Stitch & Segment: No raw pages found in {raw_pages_dir}")
            return []
            
        def get_numeric_key(path: Path):
            # Extract numbers from filename for natural sorting
            parts = re.split(r'(\d+)', path.name)
            return [int(p) if p.isdigit() else p for p in parts]
            
        sorted_files = sorted(all_files, key=get_numeric_key)
        logger.info(f"Stitch & Segment: Found {len(sorted_files)} page images to process.")
        
        images = []
        for f in sorted_files:
            try:
                img = Image.open(f)
                images.append(img.convert("RGB"))
            except Exception as e:
                logger.error(f"Stitch & Segment: Failed to load image {f}: {e}")
    except Exception as e:
        logger.error(f"Stitch & Segment: Error reading page files: {e}")
        return []

    if not images:
        logger.warning("Stitch & Segment: No valid images loaded.")
        return []

    # 2. Stitch images vertically
    # Determine the maximum width among all images
    max_width = max(img.width for img in images)
    logger.info(f"Stitch & Segment: Normalizing page widths to max_width={max_width}px...")

    resized_images = []
    total_height = 0
    for img in images:
        if img.width != max_width:
            # Scale height proportionally to maintain aspect ratio
            h = int(img.height * (max_width / img.width))
            img_resized = img.resize((max_width, h), Image.Resampling.LANCZOS)
        else:
            img_resized = img
        resized_images.append(img_resized)
        total_height += img_resized.height

    logger.info(f"Stitch & Segment: Creating stitched canvas of size {max_width}x{total_height}...")
    stitched_img = Image.new("RGB", (max_width, total_height), (255, 255, 255))
    
    y_offset = 0
    for img in resized_images:
        stitched_img.paste(img, (0, y_offset))
        y_offset += img.height

    # Save temporary stitched image for debugging (optional, can be deleted later)
    temp_dir = raw_pages_dir.parent / "temp_diagnostics"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_stitched_path = temp_dir / "full_stitched.jpg"
    try:
        stitched_img.save(temp_stitched_path, "JPEG", quality=75)
        logger.info(f"Stitch & Segment: Diagnostics stitched image saved to {temp_stitched_path}")
    except Exception as e:
        logger.warning(f"Stitch & Segment: Failed to save diagnostic stitched image: {e}")

    # 3. Analyze row variance to detect horizontal gutters (blank margins)
    logger.info("Stitch & Segment: Analysing horizontal lines for gutter detection...")
    gray_img = stitched_img.convert("L")
    img_data = np.array(gray_img)
    height, width = img_data.shape

    # Calculate variance and mean value for each row
    row_variances = np.var(img_data, axis=1)
    row_means = np.mean(img_data, axis=1)

    # A row belongs to a gutter if its pixel variance is very low (constant color)
    # and the mean color is close to white (>=240) or close to black (<=15)
    is_gutter_row = (row_variances < threshold_variance) & ((row_means >= 240) | (row_means <= 15))

    # Identify contiguous blocks of gutter rows and extract cut lines
    cut_lines = []
    in_gutter = False
    gutter_start = 0

    for y in range(height):
        if is_gutter_row[y]:
            if not in_gutter:
                in_gutter = True
                gutter_start = y
        else:
            if in_gutter:
                in_gutter = False
                gutter_height = y - gutter_start
                if gutter_height >= min_gutter_height:
                    # Cut at the midpoint of the gutter region
                    mid_cut = gutter_start + (gutter_height // 2)
                    cut_lines.append(mid_cut)

    # Compile the final crop boundaries
    cuts = [0] + cut_lines + [height]
    logger.info(f"Stitch & Segment: Identified {len(cut_lines)} cut lines. Slicing into {len(cuts) - 1} potential panels...")

    # 4. Crop and save panels, filtering out solid color spacer pages
    output_panels_dir.mkdir(parents=True, exist_ok=True)
    saved_panel_paths = []
    panel_idx = 1

    for idx in range(len(cuts) - 1):
        top = cuts[idx]
        bottom = cuts[idx+1]
        h = bottom - top
        
        if h < min_panel_height:
            logger.info(f"Stitch & Segment: Skipping slice {top}-{bottom} due to small height ({h}px)")
            continue

        panel_crop = stitched_img.crop((0, top, max_width, bottom))

        # Check if this panel is a solid background spacer (e.g. pure white or black margin)
        # Convert crop to grayscale and calculate its variance
        panel_gray = panel_crop.convert("L")
        panel_arr = np.array(panel_gray)
        panel_var = np.var(panel_arr)
        panel_mean = np.mean(panel_arr)

        # Discard if variance is extremely low and average color is near white or black
        if panel_var < 5.0 and (panel_mean >= 245 or panel_mean <= 10):
            logger.info(f"Stitch & Segment: Discarded blank/gutter panel at slice {top}-{bottom} (variance: {panel_var:.2f}, mean: {panel_mean:.2f})")
            continue

        out_path = output_panels_dir / f"panel_{panel_idx:03d}.jpg"
        try:
            panel_crop.save(out_path, "JPEG", quality=90)
            saved_panel_paths.append(out_path)
            logger.info(f"Stitch & Segment: Saved Panel {panel_idx:03d} -> {out_path.name} ({width}x{h}px)")
            panel_idx += 1
        except Exception as e:
            logger.error(f"Stitch & Segment: Failed to save panel {out_path}: {e}")

    logger.info(f"Stitch & Segment: Completed! Successfully generated {len(saved_panel_paths)} panels.")
    return saved_panel_paths
