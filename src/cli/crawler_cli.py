import argparse
import asyncio
import sys
from pathlib import Path

# Add project root to path
sys.path.append(str(Path(__file__).resolve().parents[2]))

from src.core.logger import logger
from src.services.crawler_service import crawler_service
from src.core.config import config

async def main():
    parser = argparse.ArgumentParser(description="Crawler CLI for Manga Recap")
    parser.add_argument("--chapter", type=str, required=True, help="Chapter number (e.g., 1)")
    parser.add_argument("--url", type=str, default=None, help="Base chapter URL (without chapter number)")
    parser.add_argument("--project", type=str, default="Mỗi lính đánh thuê trở về đều có một kế hoạch", help="Project title in database")
    parser.add_argument("--max_panels", type=int, default=None, help="Max number of panels to crawl")
    args = parser.parse_args()

    try:
        logger.info(f"Initializing project '{args.project}' in database...")
        project_id = await crawler_service.get_or_create_project(args.project)
        
        logger.info(f"Starting crawl for Chapter {args.chapter}...")
        
        # We pass empty context for initial crawl since Agent will write custom scripts
        general_story_context = "Ghislain Perdium trùng sinh quay về quá khứ..."
        
        chapter_id, _ = await crawler_service.crawl_chapter(
            project_id=project_id,
            chapter_number=args.chapter,
            general_story_context=general_story_context,
            chapter_base_url=args.url,
            max_panels=args.max_panels
        )
        
        from src.core.utils import get_project_data_dir
        local_dir = get_project_data_dir(args.project) / "raw_images" / f"chap_{args.chapter}"
        logger.info("=========================================================")
        logger.info(f"SUCCESS: Chapter {args.chapter} crawled successfully!")
        logger.info(f"Chapter ID in DB: {chapter_id}")
        logger.info(f"Local Image Directory: {local_dir.resolve()}")
        logger.info("=========================================================")
        
    except Exception as e:
        logger.exception(f"Crawl CLI failed: {e}")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
