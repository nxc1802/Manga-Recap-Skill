import argparse
import os
import re
import sys
import subprocess
from pathlib import Path

# Add project root to path
sys.path.append(str(Path(__file__).resolve().parents[2]))

from src.core.logger import logger
from src.core.supabase_client import supabase
from src.core.utils import get_project_data_dir

def main():
    parser = argparse.ArgumentParser(description="Compile and Concat Chapter Videos into a Final Recap Video")
    parser.add_argument("--project", type=str, default="Mỗi lính đánh thuê trở về đều có một kế hoạch", help="Project title in database")
    args = parser.parse_args()

    try:
        # 1. Fetch project ID to verify it exists
        proj_res = supabase.table("projects").select("id").eq("title", args.project).execute()
        if not proj_res.data:
            logger.error(f"Project '{args.project}' not found in database.")
            sys.exit(1)
        
        project_dir = get_project_data_dir(args.project)
        video_dir = project_dir / "video"
        
        if not video_dir.exists():
            logger.error(f"Video folder does not exist: {video_dir}")
            sys.exit(1)
            
        # 2. Scan video directory for files like chapter_1_1.mp4, chapter_10_2.mp4
        files = os.listdir(video_dir)
        chapter_videos = {} # chapter_num (int) -> (version_num (int), file_path)
        
        for f in files:
            # Match chapter_[CHAPTER]_[VERSION].mp4 but NOT raw temp clips or other patterns
            match = re.match(r"^chapter_(\d+)_(\d+)\.mp4$", f)
            if match:
                chap_num = int(match.group(1))
                version = int(match.group(2))
                file_path = video_dir / f
                
                # If we haven't seen this chapter, or if this version is higher, update it
                if chap_num not in chapter_videos or version > chapter_videos[chap_num][0]:
                    chapter_videos[chap_num] = (version, file_path)
                    
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
                
        final_video_path = project_dir / "final_recap.mp4"
        
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
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        
        # Clean up temp file
        if concat_list_path.exists():
            concat_list_path.unlink()
            
        if result.returncode != 0:
            logger.error("Failed to combine videos using FFmpeg.")
            if result.stderr:
                logger.error(result.stderr.decode("utf-8", errors="ignore"))
            sys.exit(1)
            
        logger.info("=========================================================")
        logger.info("SUCCESS: Final recap video combined successfully!")
        logger.info(f"Compiled Video File: {final_video_path.resolve()}")
        logger.info("=========================================================")
        
    except Exception as e:
        logger.exception(f"Compilation CLI failed: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
