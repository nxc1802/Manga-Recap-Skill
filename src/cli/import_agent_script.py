import argparse
import asyncio
import os
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.core.logger import logger
from src.core.utils import sync_agent_script_to_db, get_project_data_dir

async def main():
    parser = argparse.ArgumentParser(description="Import Agent's Custom Script into Supabase & script.csv")
    parser.add_argument("--chapter", type=str, required=True, help="Chapter number (e.g., 1)")
    parser.add_argument("--script", type=str, default=None, help="Path to script.txt (default: data/[project]/image/recut/chap_{N}/script.txt)")
    parser.add_argument("--project", type=str, default="Mỗi lính đánh thuê trở về đều có một kế hoạch", help="Project title in database")
    args = parser.parse_args()

    project_dir = get_project_data_dir(args.project)
    script_path = args.script
    if not script_path:
        json_path = project_dir / "image" / "recut" / f"chap_{args.chapter}" / "script.json"
        txt_path = project_dir / "image" / "recut" / f"chap_{args.chapter}" / "script.txt"
        if json_path.exists():
            script_path = str(json_path)
        else:
            script_path = str(txt_path)

    if not os.path.exists(script_path):
        logger.error(f"Script file not found: {script_path}")
        logger.error("Agentic Hybrid Workflow yêu cầu Agent (AI) phải trực tiếp xem ảnh recut và viết kịch bản vào script.txt trước.")
        sys.exit(1)

    logger.info(f"Importing Agent script from: {script_path} for Chapter {args.chapter}...")
    success = sync_agent_script_to_db(args.project, args.chapter, script_path)
    if not success:
        logger.error("Import script failed. Please check script formatting or database connection.")
        sys.exit(1)

    logger.info("=========================================================")
    logger.info("SUCCESS: Agent script imported successfully into database!")
    logger.info("The chapter is now ready for audio and video generation.")
    logger.info("=========================================================")

if __name__ == "__main__":
    asyncio.run(main())
