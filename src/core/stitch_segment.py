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
    min_panel_height: int = 40,
    sufficiency_variance: float = 100.0
) -> list[Path]:
    """
    Stitches all raw pages vertically, segments them based on horizontal black/white gutters,
    analyzes height and variance of each segment, performs Gutter Merging, Even Padding, and Skip-Marking,
    and saves the final panels.
    """
    logger.info(f"Stitch & Segment: Scanning directory {raw_pages_dir}...")
    
    # 1. Read and sort page images numerically
    try:
        all_files = list(raw_pages_dir.glob("image_*"))
        if not all_files:
            logger.warning(f"Stitch & Segment: No raw pages found in {raw_pages_dir}")
            return []
            
        def get_numeric_key(path: Path):
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
    max_width = max(img.width for img in images)
    logger.info(f"Stitch & Segment: Normalizing page widths to max_width={max_width}px...")

    resized_images = []
    total_height = 0
    for img in images:
        if img.width != max_width:
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

    # 3. Detect horizontal gutters (black/white blank lines)
    logger.info("Stitch & Segment: Analysing horizontal lines for gutter detection...")
    gray_img = stitched_img.convert("L")
    img_data = np.array(gray_img)
    height, width = img_data.shape

    row_variances = np.var(img_data, axis=1)
    row_means = np.mean(img_data, axis=1)

    # Gutters are rows with low variance and close to white or black
    is_gutter_row = (row_variances < threshold_variance) & ((row_means >= 240) | (row_means <= 15))

    gutter_intervals = []
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
                    gutter_intervals.append((gutter_start, y))
                    
    if in_gutter:
        gutter_height = height - gutter_start
        if gutter_height >= min_gutter_height:
            gutter_intervals.append((gutter_start, height))

    # Slice between gutters to get initial candidates
    raw_panels = []
    last_end = 0

    for g_start, g_end in gutter_intervals:
        if g_start > last_end:
            raw_panels.append((last_end, g_start))
        last_end = g_end

    if last_end < height:
        raw_panels.append((last_end, height))

    logger.info(f"Stitch & Segment: Slice into {len(raw_panels)} initial potential content panels...")

    # Build initial candidate list
    candidates = []
    for top, bottom in raw_panels:
        h = bottom - top
        if h < 5:  # skip extremely thin slices immediately
            continue
        candidates.append({
            "crop_coords": [top, bottom],
            "height": h
        })

    # Helper function to compute candidate variance
    def get_candidate_variance(cand):
        t, b = cand["crop_coords"]
        crop = stitched_img.crop((0, t, max_width, b))
        gray_crop = crop.convert("L")
        return float(np.var(np.array(gray_crop)))

    # 4. Gutter Merging (Iteratively merge candidates that are under 70% height)
    logger.info("Stitch & Segment: Commencing Gutter Merging for segments under 70% height...")
    
    force_merge_height = max_width * 0.7  # Merge very short strips (< 70% width) regardless of variance
    
    while True:
        target_idx = -1
        # Find the first candidate that is very short
        for idx, cand in enumerate(candidates):
            if cand["height"] < force_merge_height:
                target_idx = idx
                break
                
        if target_idx == -1:
            break
            
        # Compare gutter heights before and after target
        gutter_before = float('inf')
        if target_idx > 0:
            gutter_before = candidates[target_idx]["crop_coords"][0] - candidates[target_idx - 1]["crop_coords"][1]
            
        gutter_after = float('inf')
        if target_idx < len(candidates) - 1:
            gutter_after = candidates[target_idx + 1]["crop_coords"][0] - candidates[target_idx]["crop_coords"][1]
            
        if gutter_before == float('inf') and gutter_after == float('inf'):
            # Only one candidate left, cannot merge
            break
            
        # Merge with the nearest (shortest gutter)
        if gutter_before <= gutter_after:
            merge_idx = target_idx - 1
            logger.info(f"Gutter Merge: Merging panel {target_idx+1} at {candidates[target_idx]['crop_coords']} backward into panel {merge_idx+1} at {candidates[merge_idx]['crop_coords']} (gutter: {gutter_before}px)")
            new_coords = [candidates[merge_idx]["crop_coords"][0], candidates[target_idx]["crop_coords"][1]]
            candidates[merge_idx]["crop_coords"] = new_coords
            candidates[merge_idx]["height"] = new_coords[1] - new_coords[0]
            candidates.pop(target_idx)
        else:
            merge_idx = target_idx + 1
            logger.info(f"Gutter Merge: Merging panel {target_idx+1} at {candidates[target_idx]['crop_coords']} forward into panel {merge_idx+1} at {candidates[merge_idx]['crop_coords']} (gutter: {gutter_after}px)")
            new_coords = [candidates[target_idx]["crop_coords"][0], candidates[merge_idx]["crop_coords"][1]]
            candidates[target_idx]["crop_coords"] = new_coords
            candidates[target_idx]["height"] = new_coords[1] - new_coords[0]
            candidates.pop(merge_idx)

    # 4.5. Tall Panel Splitting
    # Splitting panels that are excessively tall (e.g. > max_width * 3.5) because gutter detection missed them
    logger.info("Stitch & Segment: Checking for extremely tall panels to split...")
    max_panel_height = int(max_width * 3.5)
    split_candidates = []
    
    for cand in candidates:
        top, bottom = cand["crop_coords"]
        h = cand["height"]
        if h > max_panel_height:
            current_top = top
            while bottom - current_top > max_panel_height:
                # Search for best split point in the middle
                search_start = current_top + max_width
                search_end = min(bottom - max_width, current_top + max_panel_height)
                
                if search_start >= search_end:
                    # Fallback to hard split
                    split_point = current_top + max_panel_height
                else:
                    crop = stitched_img.crop((0, search_start, max_width, search_end))
                    gray_crop = crop.convert("L")
                    arr = np.array(gray_crop)
                    row_vars = np.var(arr, axis=1)
                    best_local_y = int(np.argmin(row_vars))
                    split_point = search_start + best_local_y
                    
                split_candidates.append({
                    "crop_coords": [current_top, split_point],
                    "height": split_point - current_top
                })
                current_top = split_point
                
            # Add remaining part
            if bottom > current_top:
                split_candidates.append({
                    "crop_coords": [current_top, bottom],
                    "height": bottom - current_top
                })
        else:
            split_candidates.append(cand)
            
    candidates = split_candidates

    # 5. Process remaining candidates (Padding, Skip-Marking, Normal)
    output_panels_dir.mkdir(parents=True, exist_ok=True)
    saved_panel_paths = []
    
    for panel_idx, cand in enumerate(candidates, start=1):
        top, bottom = cand["crop_coords"]
        h = cand["height"]
        var = get_candidate_variance(cand)
        
        panel_crop = stitched_img.crop((0, top, max_width, bottom))
        
        is_under_height = h < max_width
        is_low_variance = var < sufficiency_variance
        
        if is_under_height:
            # 2.2: Under-height -> Padding
            # Căn lề padding đều trên dưới (Even Padding)
            pad_total = max_width - h
            pad_top = pad_total // 2
            pad_bottom = pad_total - pad_top
            
            # Detect padding background color from first and last row of the image
            gray_crop = panel_crop.convert("L")
            gray_arr = np.array(gray_crop)
            top_mean = np.mean(gray_arr[0, :])
            bottom_mean = np.mean(gray_arr[-1, :])
            border_mean = (top_mean + bottom_mean) / 2.0
            bg_color = (0, 0, 0) if border_mean < 128 else (255, 255, 255)
            
            padded_img = Image.new("RGB", (max_width, max_width), bg_color)
            padded_img.paste(panel_crop, (0, pad_top))
            
            out_path = output_panels_dir / f"panel_{panel_idx:03d}.jpg"
            padded_img.save(out_path, "JPEG", quality=90)
            saved_panel_paths.append(out_path)
            logger.info(f"Stitch & Segment: Saved Panel {panel_idx:03d} -> {out_path.name} (Padded to square, background: {bg_color}, original height: {h}px)")
            
        elif not is_under_height and is_low_variance:
            # 2.3: Sufficient height but low variance -> Skip Marking
            out_path = output_panels_dir / f"panel_{panel_idx:03d}_skip.jpg"
            panel_crop.save(out_path, "JPEG", quality=90)
            saved_panel_paths.append(out_path)
            logger.info(f"Stitch & Segment: Saved Panel {panel_idx:03d} -> {out_path.name} (MARKED SKIP, low variance: {var:.2f})")
            
        else:
            # 2.4: Normal image (Sufficient height and sufficient variance)
            out_path = output_panels_dir / f"panel_{panel_idx:03d}.jpg"
            panel_crop.save(out_path, "JPEG", quality=90)
            saved_panel_paths.append(out_path)
            
            logger.info(f"Stitch & Segment: Saved Panel {panel_idx:03d} -> {out_path.name} (Normal, height: {h}px, variance: {var:.2f})")
            
    logger.info(f"Stitch & Segment: Completed! Generated {len(saved_panel_paths)} panels.")
    return saved_panel_paths
