import os
import subprocess
from pathlib import Path
import textwrap
from enum import Enum
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import numpy as np
from src.core.logger import logger

class VideoMode(Enum):
    PORTRAIT_PADDED = "portrait_padded"
    LANDSCAPE_FIT = "landscape_fit"
    PORTRAIT_VERTICAL = "portrait_vertical"

class VideoEngine:
    def __init__(self):
        self.width = 1280
        self.height = 720
        self.fps = 30

    def _get_font(self, font_size: int = 26) -> ImageFont.FreeTypeFont:
        FONT_SEARCH_PATHS = [
            # macOS
            "/System/Library/Fonts/Supplemental/Arial Unicode MS.ttf",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/Library/Fonts/Arial.ttf",
            # Linux (Ubuntu/Debian)
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            # Windows
            "C:/Windows/Fonts/arial.ttf",
            "C:/Windows/Fonts/segoeui.ttf",
        ]
        for path in FONT_SEARCH_PATHS:
            if os.path.exists(path):
                try:
                    return ImageFont.truetype(path, font_size)
                except Exception:
                    pass
        logger.warning("No system font found. Using Pillow default font.")
        return ImageFont.load_default()

    def detect_video_mode(self, img_w: int, img_h: int, forced_mode: str = None) -> VideoMode:
        if forced_mode and forced_mode in [m.value for m in VideoMode]:
            return VideoMode(forced_mode)
        
        aspect = img_w / img_h
        if aspect >= 1.5:          # Ngang rõ ràng → Landscape 16:9
            return VideoMode.LANDSCAPE_FIT
        elif aspect <= 0.7:        # Dọc rõ ràng/dọc dài → Portrait Vertical
            return VideoMode.PORTRAIT_VERTICAL  
        else:                      # Vuông/gần vuông → Portrait Padded (default)
            return VideoMode.PORTRAIT_PADDED

    def find_optimal_crop_box(self, img: Image.Image, crop_w: int, crop_h: int) -> tuple[int, int, int, int]:
        """
        Finds the optimal crop bounding box of size crop_w x crop_h within img
        based on edge density / detail concentration (sliding window sum of variances).
        """
        img_w, img_h = img.size
        if img_w <= crop_w and img_h <= crop_h:
            return 0, 0, img_w, img_h
            
        # Convert to grayscale to compute gradients/variances
        gray = img.convert("L")
        arr = np.array(gray, dtype=np.float32)
        
        # We want to find the best top-left corner (left, top)
        # If we need to crop vertically (img_h > crop_h)
        if img_h > crop_h:
            # Compute row-wise variances to represent detail density per row
            row_vars = np.var(arr, axis=1)
            if len(row_vars) > crop_h:
                window_sums = np.convolve(row_vars, np.ones(crop_h), mode='valid')
                best_top = int(np.argmax(window_sums))
                top = min(max(0, best_top), img_h - crop_h)
            else:
                top = 0
        else:
            top = 0
            
        # If we need to crop horizontally (img_w > crop_w)
        if img_w > crop_w:
            # Compute column-wise variances
            col_vars = np.var(arr, axis=0)
            if len(col_vars) > crop_w:
                window_sums = np.convolve(col_vars, np.ones(crop_w), mode='valid')
                best_left = int(np.argmax(window_sums))
                left = min(max(0, best_left), img_w - crop_w)
            else:
                left = 0
        else:
            left = 0
            
        return left, top, left + crop_w, top + crop_h

    def create_panel_image_with_subs(self, raw_img_path: str, text: str, output_path: str, focus_x: int = 50, focus_y: int = 50, crop_scale: int = 100) -> str:
        """
        Creates a 1280x720 frame using Pillow:
        1. Crops a square 1:1 region centered around (focus_x, focus_y).
        2. Resizes and blurs the cropped region to 1280x720 (background).
        3. Resizes the cropped region to 720x720 and centers it on the background (foreground).
        4. Renders wrapped subtitle text at the bottom.
        """
        try:
            with Image.open(raw_img_path) as img:
                img_w, img_h = img.size
                
                # Base square size is the smaller dimension
                base_crop_size = min(img_w, img_h)
                # Apply scaling factor (zoom)
                crop_size = int(base_crop_size * (crop_scale / 100.0))
                # Make sure it doesn't exceed image dimensions
                crop_size = min(crop_size, img_w, img_h)
                
                center_x = int(img_w * (focus_x / 100.0))
                center_y = int(img_h * (focus_y / 100.0))
                
                left = center_x - crop_size // 2
                top = center_y - crop_size // 2
                
                # Clamp coordinates to stay within image boundaries
                if left < 0:
                    left = 0
                elif left + crop_size > img_w:
                    left = img_w - crop_size
                    
                if top < 0:
                    top = 0
                elif top + crop_size > img_h:
                    top = img_h - crop_size
                    
                cropped_sq = img.crop((left, top, left + crop_size, top + crop_size))
                
                # Create blurred background by stretching to 1280x720 and applying Gaussian Blur
                bg = cropped_sq.resize((self.width, self.height), Image.Resampling.LANCZOS)
                bg = bg.filter(ImageFilter.GaussianBlur(radius=30))
                
                # Create foreground centered square (720x720)
                fg = cropped_sq.resize((self.height, self.height), Image.Resampling.LANCZOS)
                
                # Paste foreground in the center of background
                paste_x = (self.width - self.height) // 2
                bg.paste(fg, (paste_x, 0))
                
                final_frame = bg
                
                # 3. Draw Subtitles
                if text:
                    overlay = Image.new("RGBA", final_frame.size, (0, 0, 0, 0))
                    draw = ImageDraw.Draw(overlay)
                    
                    font_size = 26
                    font = self._get_font(font_size)
                        
                    lines = textwrap.wrap(text, width=65)
                    
                    # Calculate text box size
                    line_height = font_size + 10
                    rect_height = len(lines) * line_height + 24
                    rect_top = self.height - rect_height - 30
                    
                    # Draw semi-transparent background block
                    draw.rounded_rectangle(
                        [50, rect_top, self.width - 50, self.height - 30],
                        radius=12,
                        fill=(0, 0, 0, 175)
                    )
                    
                    # Draw text line by line
                    y = rect_top + 12
                    for line in lines:
                        try:
                            left, top_b, right, bottom = font.getbbox(line)
                            text_width = right - left
                        except Exception:
                            text_width = len(line) * 13
                            
                        x = (self.width - text_width) // 2
                        draw.text((x, y), line, font=font, fill=(255, 255, 255, 255))
                        y += line_height
                        
                    final_frame = Image.alpha_composite(final_frame.convert("RGBA"), overlay)
                
                Path(output_path).parent.mkdir(parents=True, exist_ok=True)
                final_frame.convert("RGB").save(output_path, "WEBP", quality=90)
                return output_path
                
        except Exception as e:
            logger.error(f"Error creating subbed image for {raw_img_path}: {e}")
            raise

    def create_processed_panel_image(self, raw_img_path: str, output_path: str, video_mode = VideoMode.PORTRAIT_PADDED, focus_x: int = 50, focus_y: int = 50, crop_scale: int = 100) -> str:
        """
        Creates a processed frame using Pillow depending on the VideoMode.
        Always uses center-based crop.
        """
        try:
            if isinstance(video_mode, str):
                try:
                    video_mode = VideoMode(video_mode)
                except ValueError:
                    video_mode = VideoMode.PORTRAIT_PADDED

            with Image.open(raw_img_path) as img:
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGB")
                img_w, img_h = img.size
                
                if video_mode == VideoMode.LANDSCAPE_FIT:
                    # Landscape 16:9 layout
                    scale = min(self.width / img_w, self.height / img_h)
                    new_w = int(img_w * scale)
                    new_h = int(img_h * scale)
                    fg = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
                    
                    bg = img.resize((self.width, self.height), Image.Resampling.LANCZOS)
                    bg = bg.filter(ImageFilter.GaussianBlur(radius=30))
                    
                    paste_x = (self.width - new_w) // 2
                    paste_y = (self.height - new_h) // 2
                    bg.paste(fg, (paste_x, paste_y))
                    
                    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
                    bg.convert("RGB").save(output_path, "WEBP", quality=90)
                    
                elif video_mode == VideoMode.PORTRAIT_PADDED:
                    # Portrait padded layout: crop center region with target aspect ratio 720x828 (1.15 headroom)
                    target_aspect = 828 / 720
                    if img_w * target_aspect <= img_h:
                        crop_w = img_w
                        crop_h = int(img_w * target_aspect)
                    else:
                        crop_h = img_h
                        crop_w = int(img_h / target_aspect)
                        
                    # Use optimal crop box based on edge density (Option 2)
                    left, top, right, bottom = self.find_optimal_crop_box(img, crop_w, crop_h)
                    cropped = img.crop((left, top, right, bottom))
                    
                    fg = cropped.resize((720, 828), Image.Resampling.LANCZOS)
                    
                    bg = cropped.resize((1280, 828), Image.Resampling.LANCZOS)
                    bg = bg.filter(ImageFilter.GaussianBlur(radius=30))
                    
                    bg.paste(fg, ((1280 - 720) // 2, 0))
                    
                    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
                    bg.convert("RGB").save(output_path, "WEBP", quality=90)
                    
                elif video_mode == VideoMode.PORTRAIT_VERTICAL:
                    # Tall vertical webtoon panel
                    new_h = int(720 * (img_h / img_w))
                    if new_h < 720:
                        new_h = 720
                        
                    fg = img.resize((720, new_h), Image.Resampling.LANCZOS)
                    
                    bg = img.resize((1280, new_h), Image.Resampling.LANCZOS)
                    bg = bg.filter(ImageFilter.GaussianBlur(radius=30))
                    
                    bg.paste(fg, ((1280 - 720) // 2, 0))
                    
                    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
                    bg.convert("RGB").save(output_path, "WEBP", quality=90)
                    
            return output_path
        except Exception as e:
            logger.error(f"Error creating processed image for {raw_img_path}: {e}")
            raise

    def create_subtitle_overlay_png(self, text: str, output_path: str) -> str:
        """
        Creates a 1280x720 transparent PNG containing wrapped subtitles at the bottom.
        """
        try:
            overlay = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
            if not text:
                overlay.save(output_path, "PNG")
                return output_path
                
            draw = ImageDraw.Draw(overlay)
            
            font_size = 26
            font = self._get_font(font_size)
                
            lines = textwrap.wrap(text, width=65)
            
            # Calculate text box size
            line_height = font_size + 10
            rect_height = len(lines) * line_height + 24
            rect_top = self.height - rect_height - 30
            
            # Draw semi-transparent background block
            draw.rounded_rectangle(
                [50, rect_top, self.width - 50, self.height - 30],
                radius=12,
                fill=(0, 0, 0, 175)
            )
            
            # Draw text line by line
            y = rect_top + 12
            for line in lines:
                try:
                    left, top_b, right, bottom = font.getbbox(line)
                    text_width = right - left
                except Exception:
                    text_width = len(line) * 13
                    
                x = (self.width - text_width) // 2
                draw.text((x, y), line, font=font, fill=(255, 255, 255, 255))
                y += line_height
                
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            overlay.save(output_path, "PNG")
            return output_path
        except Exception as e:
            logger.error(f"Error creating subtitle overlay PNG: {e}")
            raise

    def split_text_into_chunks(self, text: str, max_chars: int = 50) -> list[str]:
        """
        Splits text into readable subtitle chunks of max_chars length.
        Prefers splitting on punctuation (., !, ?, ,, ;) or spaces.
        """
        import re
        if not text:
            return []
            
        # Standardize spaces
        text = re.sub(r'\s+', ' ', text).strip()
        
        # Split by sentences first
        sentences = re.split(r'(?<=[.!?])\s+', text)
        chunks = []
        
        for sent in sentences:
            if len(sent) <= max_chars:
                chunks.append(sent)
            else:
                # Split sentence further by words
                words = sent.split(' ')
                current_chunk = []
                current_len = 0
                for word in words:
                    # +1 is for the space
                    word_len = len(word) + (1 if current_chunk else 0)
                    if current_len + word_len <= max_chars:
                        current_chunk.append(word)
                        current_len += word_len
                    else:
                        if current_chunk:
                            chunks.append(" ".join(current_chunk))
                        current_chunk = [word]
                        current_len = len(word)
                if current_chunk:
                    chunks.append(" ".join(current_chunk))
        return [c.strip() for c in chunks if c.strip()]

    def generate_panel_clip(self, img_path: str, audio_path: str, duration_ms: int, output_path: str, video_mode = VideoMode.PORTRAIT_PADDED, seq: int = 1, panel_text: str = None, burn_subtitles: bool = True, pan_direction: str = None) -> bool:
        """
        Renders a single panel clip with uniform movement according to VideoMode and timed subtitles.
        """
        if isinstance(video_mode, str):
            try:
                video_mode = VideoMode(video_mode)
            except ValueError:
                video_mode = VideoMode.PORTRAIT_PADDED

        duration_sec = duration_ms / 1000.0
        frame_count = int(self.fps * duration_sec)
        
        # Get image height to check the scrolling offset
        try:
            with Image.open(img_path) as tmp_img:
                _, img_h = tmp_img.size
        except Exception as e:
            logger.error(f"Failed to open processed image to get size: {e}")
            img_h = 720
            
        # Calculate dynamic focal point using sliding window row-variance sum
        best_top = 0
        if img_h > 720:
            try:
                with Image.open(img_path) as tmp_img:
                    gray = tmp_img.convert("L")
                    arr = np.array(gray, dtype=np.float32)
                    row_vars = np.var(arr, axis=1)
                    if len(row_vars) > 720:
                        window_sums = np.convolve(row_vars, np.ones(720), mode='valid')
                        best_top = int(np.argmax(window_sums))
                        best_top = min(max(0, best_top), img_h - 720)
            except Exception as e:
                logger.error(f"Failed to compute dynamic focal point: {e}")
                best_top = (img_h - 720) // 2
                
        y_expr = str(best_top)
        filter_complex_base = f"[0:v]fps=fps={self.fps},crop=1280:720:0:'{y_expr}'[pan];"
            
        cmd = [
            "ffmpeg", "-y",
            "-framerate", str(self.fps),
            "-loop", "1", "-i", img_path,
            "-i", audio_path
        ]
        
        temp_pngs = []
        try:
            if burn_subtitles and panel_text:
                chunks = self.split_text_into_chunks(panel_text, max_chars=55)
                total_chars = sum(len(c) for c in chunks)
                
                if total_chars > 0:
                    overlay_inputs = []
                    filter_parts = []
                    prev_output = "[pan]"
                    current_time_ms = 0
                    
                    for idx, chunk in enumerate(chunks):
                        chunk_len = len(chunk)
                        chunk_duration_ms = int(duration_ms * (chunk_len / total_chars))
                        
                        start_sec = current_time_ms / 1000.0
                        end_sec = (current_time_ms + chunk_duration_ms) / 1000.0
                        current_time_ms += chunk_duration_ms
                        
                        if end_sec > duration_sec or idx == len(chunks) - 1:
                            end_sec = duration_sec
                            
                        overlay_path = Path(output_path).parent / f"{Path(output_path).stem}_sub_{idx}.png"
                        self.create_subtitle_overlay_png(chunk, str(overlay_path))
                        temp_pngs.append(overlay_path)
                        
                        inp_idx = 2 + idx
                        overlay_inputs.extend(["-loop", "1", "-i", str(overlay_path)])
                        
                        out_label = f"[tmp{idx}]" if idx < len(chunks) - 1 else "[outv]"
                        filter_parts.append(
                            f"{prev_output}[{inp_idx}:v]overlay=0:0:enable='between(t,{start_sec:.3f},{end_sec:.3f})'{out_label}"
                        )
                        prev_output = out_label
                        
                    cmd.extend(overlay_inputs)
                    filter_complex = filter_complex_base + ";".join(filter_parts)
                    
                    cmd.extend([
                        "-filter_complex", filter_complex,
                        "-map", "[outv]",
                        "-map", "1:a"
                    ])
                else:
                    cmd.extend([
                        "-filter_complex", filter_complex_base,
                        "-map", "[pan]",
                        "-map", "1:a"
                    ])
            else:
                cmd.extend([
                    "-filter_complex", filter_complex_base,
                    "-map", "[pan]",
                    "-map", "1:a"
                ])
                
            cmd.extend([
                "-c:v", "libx264",
                "-t", str(duration_sec),
                "-c:a", "aac",
                "-pix_fmt", "yuv420p",
                "-r", str(self.fps),
                output_path
            ])
            
            logger.info(f"Rendering panel clip {seq}: {output_path} (Mode: {video_mode.value if hasattr(video_mode, 'value') else video_mode}, Duration: {duration_sec:.2f}s)...")
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            return True
        except Exception as e:
            logger.error(f"FFmpeg error rendering panel clip {output_path}: {e}")
            if hasattr(e, 'stderr') and e.stderr:
                logger.error(f"FFmpeg stderr: {e.stderr.decode('utf-8', errors='ignore')}")
            return False
        finally:
            for p in temp_pngs:
                try:
                    if p.exists():
                        p.unlink()
                except Exception:
                    pass

    def concatenate_clips(self, clip_paths: list[str], output_path: str) -> bool:
        """
        Losslessly concatenates multiple MP4 clips using FFmpeg demuxer.
        """
        list_file_path = Path(output_path).parent / "video_concat_list.txt"
        
        with open(list_file_path, "w", encoding="utf-8") as f:
            for path in clip_paths:
                abs_path = os.path.abspath(path)
                f.write(f"file '{abs_path}'\n")
                
        cmd = [
            "ffmpeg", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(list_file_path),
            "-c", "copy",
            output_path
        ]
        
        try:
            logger.info(f"Concatenating {len(clip_paths)} clips into: {output_path}...")
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            if list_file_path.exists():
                list_file_path.unlink()
            return True
        except Exception as e:
            logger.error(f"Failed to concatenate video clips: {e}")
            return False

    def generate_srt_file(self, script_items: list[dict], srt_path: str):
        """
        Generates a standard SubRip (.srt) subtitle file from script segments and durations.
        """
        def format_srt_time(ms: int) -> str:
            hours = ms // 3600000
            ms %= 3600000
            minutes = ms // 60000
            ms %= 60000
            seconds = ms // 1000
            milliseconds = ms % 1000
            return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"

        current_time_ms = 0
        srt_index = 1
        with open(srt_path, "w", encoding="utf-8") as f:
            for item in script_items:
                duration_ms = item["duration_ms"]
                text = item["text"]
                if not text:
                    current_time_ms += duration_ms
                    continue
                    
                chunks = self.split_text_into_chunks(text, max_chars=55)
                total_chars = sum(len(c) for c in chunks)
                
                if total_chars > 0:
                    chunk_start_ms = current_time_ms
                    for idx, chunk in enumerate(chunks):
                        chunk_len = len(chunk)
                        chunk_duration_ms = int(duration_ms * (chunk_len / total_chars))
                        
                        start_time = format_srt_time(chunk_start_ms)
                        if idx == len(chunks) - 1:
                            chunk_end_ms = current_time_ms + duration_ms
                        else:
                            chunk_end_ms = chunk_start_ms + chunk_duration_ms
                            
                        end_time = format_srt_time(chunk_end_ms)
                        
                        f.write(f"{srt_index}\n")
                        f.write(f"{start_time} --> {end_time}\n")
                        f.write(f"{chunk}\n\n")
                        
                        srt_index += 1
                        chunk_start_ms = chunk_end_ms
                else:
                    start_time = format_srt_time(current_time_ms)
                    end_time = format_srt_time(current_time_ms + duration_ms)
                    f.write(f"{srt_index}\n")
                    f.write(f"{start_time} --> {end_time}\n")
                    f.write(f"{text}\n\n")
                    srt_index += 1
                    
                current_time_ms += duration_ms
        logger.info(f"SRT subtitle file written to {srt_path}")

    def mix_bgm_and_burn_subs(self, video_path: str, srt_path: str, bgm_path: str | None, output_path: str) -> bool:
        """
        Mixes BGM loop audio with the video voiceover.
        Since subtitles are already drawn on the frames by Pillow, we do NOT use subtitles filter.
        If BGM is not provided, this simply copies the video (lossless) or re-encodes if necessary.
        """
        # If no BGM, we can copy the video directly!
        if not bgm_path or not os.path.exists(bgm_path):
            logger.info("No BGM provided. Performing fast copy to final output.")
            cmd = [
                "ffmpeg", "-y",
                "-i", video_path,
                "-c", "copy",
                output_path
            ]
        else:
            # Mix BGM
            logger.info(f"Mixing background music {bgm_path} (volume 12%)")
            cmd = [
                "ffmpeg", "-y",
                "-i", video_path,
                "-i", bgm_path,
                "-filter_complex", "[1:a]volume=0.12[bgm];[0:a][bgm]amix=inputs=2:duration=first:dropout_transition=2[a]",
                "-map", "0:v",
                "-map", "[a]",
                "-c:v", "copy", # video stream can be copied losslessly!
                "-c:a", "aac",
                output_path
            ]
            
        try:
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            return True
        except Exception as e:
            logger.error(f"Failed to mix BGM: {e}")
            if hasattr(e, 'stderr') and e.stderr:
                logger.error(f"FFmpeg stderr: {e.stderr.decode('utf-8', errors='ignore')}")
            return False

video_engine = VideoEngine()
