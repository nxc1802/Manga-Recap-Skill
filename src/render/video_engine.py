import os
import subprocess
from pathlib import Path
import textwrap
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from src.core.logger import logger

class VideoEngine:
    def __init__(self):
        self.width = 1280
        self.height = 720
        self.fps = 25

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
                    # Initialize Draw context with support for alpha channel (transparency)
                    overlay = Image.new("RGBA", final_frame.size, (0, 0, 0, 0))
                    draw = ImageDraw.Draw(overlay)
                    
                    # Search for a clean Vietnamese-compatible font
                    font = None
                    font_size = 26
                    font_paths = [
                        "/System/Library/Fonts/Supplemental/Arial.ttf",
                        "/Library/Fonts/Arial.ttf",
                        "/System/Library/Fonts/Helvetica.ttc",
                        "/System/Library/Fonts/Times New Roman.ttf"
                    ]
                    for path in font_paths:
                        if os.path.exists(path):
                            try:
                                font = ImageFont.truetype(path, font_size)
                                break
                            except Exception:
                                pass
                    if not font:
                        font = ImageFont.load_default()
                        
                    # Wrap text to fit inside 1100px width (approx 65 characters)
                    lines = textwrap.wrap(text, width=65)
                    
                    # Calculate text box size
                    line_height = font_size + 10
                    rect_height = len(lines) * line_height + 24
                    rect_top = self.height - rect_height - 30
                    
                    # Draw semi-transparent background block
                    draw.rounded_rectangle(
                        [50, rect_top, self.width - 50, self.height - 30],
                        radius=12,
                        fill=(0, 0, 0, 175) # 175/255 transparency
                    )
                    
                    # Draw text line by line
                    y = rect_top + 12
                    for line in lines:
                        try:
                            # Try modern Pillow text size method
                            left, top_b, right, bottom = font.getbbox(line)
                            text_width = right - left
                        except Exception:
                            text_width = len(line) * 13 # rough estimate fallback
                            
                        x = (self.width - text_width) // 2
                        draw.text((x, y), line, font=font, fill=(255, 255, 255, 255))
                        y += line_height
                        
                    # Composite overlay onto background image
                    final_frame = Image.alpha_composite(final_frame.convert("RGBA"), overlay)
                
                # Ensure output folder exists
                Path(output_path).parent.mkdir(parents=True, exist_ok=True)
                final_frame.convert("RGB").save(output_path, "WEBP", quality=90)
                return output_path
                
        except Exception as e:
            logger.error(f"Error creating subbed image for {raw_img_path}: {e}")
            raise

    def create_processed_panel_image(self, raw_img_path: str, output_path: str, focus_x: int = 50, focus_y: int = 50, crop_scale: int = 100) -> str:
        """
        Creates a 1280x720 frame using Pillow:
        1. Crops a square 1:1 region centered around (focus_x, focus_y).
        2. Resizes and blurs the cropped region to 1280x720 (background).
        3. Resizes the cropped region to 720x720 and centers it on the background (foreground).
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
                
                # Ensure output folder exists
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
            
            # Search for a clean Vietnamese-compatible font
            font = None
            font_size = 26
            font_paths = [
                "/System/Library/Fonts/Supplemental/Arial.ttf",
                "/Library/Fonts/Arial.ttf",
                "/System/Library/Fonts/Helvetica.ttc",
                "/System/Library/Fonts/Times New Roman.ttf"
            ]
            for path in font_paths:
                if os.path.exists(path):
                    try:
                        font = ImageFont.truetype(path, font_size)
                        break
                    except Exception:
                        pass
            if not font:
                font = ImageFont.load_default()
                
            # Wrap text to fit inside 1100px width (approx 65 characters)
            lines = textwrap.wrap(text, width=65)
            
            # Calculate text box size
            line_height = font_size + 10
            rect_height = len(lines) * line_height + 24
            rect_top = self.height - rect_height - 30
            
            # Draw semi-transparent background block
            draw.rounded_rectangle(
                [50, rect_top, self.width - 50, self.height - 30],
                radius=12,
                fill=(0, 0, 0, 175) # 175/255 transparency
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

    def generate_panel_clip(self, img_path: str, audio_path: str, duration_ms: int, output_path: str, pan_direction: str = "horizontal", seq: int = 1, sub_overlay_path: str = None) -> bool:
        """
        Renders a single panel clip with professional pan movements and static subtitle overlay.
        """
        duration_sec = duration_ms / 1000.0
        frame_count = int(self.fps * duration_sec)
        
        # Fixed zoom factor to provide padding for panning
        zoom = 1.15
        
        # Calculate horizontal and vertical panning expressions based on pan_direction
        if pan_direction == "horizontal":
            # Alternates left-to-right and right-to-left based on sequence number
            if seq % 2 == 0:
                x_expr = f"(zoom-1)*iw*(on/{frame_count})"
            else:
                x_expr = f"(zoom-1)*iw*(1-on/{frame_count})"
            y_expr = f"(zoom-1)*ih/2"
        elif pan_direction == "vertical":
            # Alternates top-to-bottom and bottom-to-top based on sequence number
            x_expr = f"(zoom-1)*iw/2"
            if seq % 2 == 0:
                y_expr = f"(zoom-1)*ih*(on/{frame_count})"
            else:
                y_expr = f"(zoom-1)*ih*(1-on/{frame_count})"
        else: # "zoom" or fallback
            x_expr = f"(zoom-1)*iw/2"
            y_expr = f"(zoom-1)*ih/2"
            
        cmd = [
            "ffmpeg", "-y",
            "-loop", "1", "-i", img_path,
            "-i", audio_path
        ]
        
        if sub_overlay_path and os.path.exists(sub_overlay_path):
            cmd.extend([
                "-loop", "1", "-i", sub_overlay_path,
                "-filter_complex", f"[0:v]zoompan=z={zoom}:d={frame_count}:x='{x_expr}':y='{y_expr}':s={self.width}x{self.height}[pan];[pan][2:v]overlay=0:0:shortest=1[outv]",
                "-map", "[outv]",
                "-map", "1:a"
            ])
        else:
            vf_filter = f"zoompan=z={zoom}:d={frame_count}:x='{x_expr}':y='{y_expr}':s={self.width}x{self.height}"
            cmd.extend([
                "-vf", vf_filter
            ])
            
        cmd.extend([
            "-c:v", "libx264",
            "-t", str(duration_sec),
            "-c:a", "aac",
            "-pix_fmt", "yuv420p",
            "-r", str(self.fps),
            output_path
        ])
        
        try:
            logger.info(f"Rendering panel clip {seq}: {output_path} (Pan: {pan_direction}, Duration: {duration_sec:.2f}s)...")
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            return True
        except Exception as e:
            logger.error(f"FFmpeg error rendering panel clip {output_path}: {e}")
            if hasattr(e, 'stderr') and e.stderr:
                logger.error(f"FFmpeg stderr: {e.stderr.decode('utf-8', errors='ignore')}")
            return False

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
        with open(srt_path, "w", encoding="utf-8") as f:
            for i, item in enumerate(script_items):
                duration_ms = item["duration_ms"]
                text = item["text"]
                
                start_time = format_srt_time(current_time_ms)
                end_time = format_srt_time(current_time_ms + duration_ms)
                
                f.write(f"{i + 1}\n")
                f.write(f"{start_time} --> {end_time}\n")
                f.write(f"{text}\n\n")
                
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
