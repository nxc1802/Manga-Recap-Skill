import argparse
import asyncio
import os
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.core.logger import logger
from src.core.utils import sync_agent_script_to_storage, get_project_data_dir

async def main():
    parser = argparse.ArgumentParser(description="Import Agent's Custom Script into Local Storage & script.csv")
    parser.add_argument("--chapter", type=str, required=True, help="Chapter number (e.g., 1)")
    parser.add_argument("--script", type=str, default=None, help="Path to script.txt or script.json")
    parser.add_argument("--project", type=str, default="Mỗi lính đánh thuê trở về đều có một kế hoạch", help="Project title")
    args = parser.parse_args()

    project_dir = get_project_data_dir(args.project)
    script_path = args.script
    if not script_path:
        for candidate in [
            project_dir / "chapters" / str(args.chapter) / "script.json",
            project_dir / "chapters" / str(args.chapter) / "script.txt",
            project_dir / "image" / "recut" / f"chap_{args.chapter}" / "script.json",
            project_dir / "image" / "recut" / f"chap_{args.chapter}" / "script.txt"
        ]:
            if candidate.exists():
                script_path = str(candidate)
                break
        if not script_path:
            script_path = str(project_dir / "chapters" / str(args.chapter) / "script.json")

    if not os.path.exists(script_path):
        logger.error(f"Script file not found: {script_path}")
        logger.error("Agentic Hybrid Workflow yêu cầu Agent (AI) phải trực tiếp xem ảnh recut và viết kịch bản vào script.txt hoặc script.json trước.")
        sys.exit(1)

    logger.info(f"Importing Agent script from: {script_path} for Chapter {args.chapter}...")
    success = sync_agent_script_to_storage(args.project, args.chapter, script_path)
    if not success:
        logger.error("Import script failed. Please check script formatting.")
        sys.exit(1)

    logger.info("=========================================================")
    logger.info("SUCCESS: Agent script imported successfully into local storage!")
    logger.info("The chapter is now ready for audio and video generation.")
    logger.info("=========================================================")

if __name__ == "__main__":
    asyncio.run(main())
