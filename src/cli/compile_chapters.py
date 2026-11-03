import argparse
import os
import re
import sys
import subprocess
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.core.logger import logger
from src.core.utils import get_project_data_dir, run_ffmpeg

def main():
    parser = argparse.ArgumentParser(description="Compile and Concat Chapter Videos into a Final Recap Video")
    parser.add_argument("--project", type=str, default="Mỗi lính đánh thuê trở về đều có một kế hoạch", help="Project title")
    args = parser.parse_args()

    try:
        # 1. Verify project directory exists locally
        project_dir = get_project_data_dir(args.project)
        if not project_dir.exists():
            logger.error(f"Project directory '{project_dir}' not found for project '{args.project}'.")
            sys.exit(1)

        project_json = project_dir / "project.json"
        if project_json.exists():
            from src.core.storage import read_json
            try:
                proj_data = read_json(project_json)
                logger.info(f"Compiling chapters for project: {proj_data.get('title', args.project)}")
            except Exception:
                pass

        video_dir = project_dir / "video" / "chapters"
        
        if not video_dir.exists():
            logger.error(f"Video folder does not exist: {video_dir}")
            sys.exit(1)
            
        # 2. Scan video directory for files like chapter_1_1.mp4, chapter_1_final_v1.mp4
        files = os.listdir(video_dir)
        chapter_videos = {} # chapter_num (int) -> (version_num (int), file_path)
        
        for f in files:
            # Match chapter_[CHAPTER]...[VERSION].mp4 but NOT raw temp clips
            if "_raw_temp" in f or not f.endswith(".mp4"):
                continue
            match = re.match(r"^chapter_(\d+)(?:_.*?)?_(?:v)?(\d+)\.mp4$", f)
            if match:
                chap_num = int(match.group(1))
                version = int(match.group(2))
                file_path = video_dir / f
                
                # If we haven't seen this chapter, or if this version is higher, update it
                if chap_num not in chapter_videos or version > chapter_videos[chap_num][0]:
                    chapter_videos[chap_num] = (version, file_path)
                continue
            
            simple_match = re.match(r"^chapter_(\d+)(?:_(.+))?\.mp4$", f)
            if simple_match:
                chap_num = int(simple_match.group(1))
                file_path = video_dir / f
                if chap_num not in chapter_videos:
                    chapter_videos[chap_num] = (1, file_path)

                    
        if not chapter_videos:
            logger.error(f"No chapter videos found in {video_dir}")
            sys.exit(1)
            
        # 3. Sort chapters numerically
        sorted_chaps = sorted(chapter_videos.keys())
        compile_list = [chapter_videos[c][1] for c in sorted_chaps]
        
        logger.info(f"Found {len(compile_list)} chapters to combine:")
        for c in sorted_chaps:
            ver, path = chapter_videos[c]
            logger.info(f" - Chapter {c} (Version {ver}): {path.name}")
            
        # 4. Generate temporary concatenation list file
        concat_list_path = project_dir / "compile_concat_list.txt"
        with open(concat_list_path, "w", encoding="utf-8") as f:
            for path in compile_list:
                abs_path = os.path.abspath(path)
                f.write(f"file '{abs_path}'\n")
                
        final_video_dir = project_dir / "video"
        final_video_dir.mkdir(parents=True, exist_ok=True)
        final_video_path = final_video_dir / "final.mp4"
        
        # 5. Run FFmpeg demuxer to concatenate
        cmd = [
            "ffmpeg", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_list_path),
            "-c", "copy",
            str(final_video_path)
        ]
        
        logger.info(f"Combining chapters into final recap video: {final_video_path}...")
        try:
            run_ffmpeg(cmd, description="FFmpeg video concatenation")
            if not final_video_path.exists() or final_video_path.stat().st_size == 0:
                raise RuntimeError(f"Concatenated final video is empty or missing: {final_video_path}")
        finally:
            # Clean up temp file
            if concat_list_path.exists():
                concat_list_path.unlink()
            
        # 6. Extract final audio to audio/final.wav
        final_audio_path = project_dir / "audio" / "final.wav"
        final_audio_path.parent.mkdir(parents=True, exist_ok=True)
        
        logger.info(f"Extracting final audio to {final_audio_path}...")
        cmd_audio = [
            "ffmpeg", "-y",
            "-i", str(final_video_path),
            "-vn", "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2",
            str(final_audio_path)
        ]
        run_ffmpeg(cmd_audio, description="FFmpeg final audio extraction")
        if not final_audio_path.exists() or final_audio_path.stat().st_size == 0:
            raise RuntimeError(f"Extracted final audio is empty or missing: {final_audio_path}")

        
        logger.info("=========================================================")
        logger.info("SUCCESS: Final recap video & audio combined successfully!")
        logger.info(f"Compiled Video File: {final_video_path.resolve()}")
        logger.info(f"Compiled Audio File: {final_audio_path.resolve()}")
        logger.info("=========================================================")
        
    except Exception as e:
        logger.exception(f"Compilation CLI failed: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
