import os
import httpx
from bs4 import BeautifulSoup
from pathlib import Path
import asyncio
from src.core.config import config
from src.core.logger import logger
from src.core.supabase_client import supabase
from src.core.openrouter_client import openrouter_client

class CrawlerService:
    def __init__(self):
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://truyenqqko.com/"
        }
        
    async def get_or_create_project(self, title: str) -> str:
        """
        Queries Supabase for a project with the given title.
        Creates it if it does not exist. Returns the project UUID.
        """
        try:
            # Query existing project
            res = supabase.table("projects").select("id").eq("title", title).execute()
            if res.data:
                proj_id = res.data[0]["id"]
                logger.info(f"Found existing project for '{title}': {proj_id}")
                return proj_id
                
            # Create a new project
            logger.info(f"Creating new project for '{title}'...")
            insert_res = supabase.table("projects").insert({
                "title": title,
                "description": f"Recap project for {title}"
            }).execute()
            
            proj_id = insert_res.data[0]["id"]
            logger.info(f"Project created with ID: {proj_id}")
            return proj_id
        except Exception as e:
            logger.exception(f"Error in get_or_create_project: {e}")
            raise
    async def download_image(self, client: httpx.AsyncClient, img_url: str, save_path: Path, chapter_base_url: str = None) -> bool:
        """
        Downloads a single image with referrer headers, saving it locally.
        """
        if save_path.exists():
            return True
        headers = self.headers.copy()
        
        # Referer must match the page or base domain.
        # Webtoons requires Referer: https://www.webtoons.com/
        if "webtoons.com" in img_url.lower() or "pstatic.net" in img_url.lower():
            headers["Referer"] = "https://www.webtoons.com/"
        else:
            headers["Referer"] = chapter_base_url or config.crawler.chapter_base_url
            
        try:
            resp = await client.get(img_url, headers=headers, timeout=30.0)
            if resp.status_code == 200:
                save_path.parent.mkdir(parents=True, exist_ok=True)
                with open(save_path, "wb") as f:
                    f.write(resp.content)
                return True
            else:
                logger.error(f"Failed to download image {img_url}: Status {resp.status_code}")
                return False
        except Exception as e:
            logger.error(f"Error downloading image {img_url}: {e}")
            return False

    async def crawl_chapter(self, project_id: str, chapter_number: str, general_story_context: str, chapter_base_url: str = None, max_panels: int = None) -> tuple[str, str]:
        """
        Orchestrates crawling a chapter:
        1. Fetch chapter HTML.
        2. Create chapter record in Supabase (or update if exists).
        3. Parse images.
        4. Download images locally to data/raw_images/chap_{chapter_number}/
        5. Insert panel records into Supabase panels table.
        Returns (chapter_uuid, summary_context)
        """
        url = f"{chapter_base_url or config.crawler.chapter_base_url}{chapter_number}"
        logger.info(f"Crawling Chapter {chapter_number} from: {url}")
        
        # 1. Use manual story description from parameter instead of config
        summary_context = general_story_context
        
        # 2. Get or create chapter record
        try:
            res = supabase.table("chapters").select("id").eq("project_id", project_id).eq("chapter_number", chapter_number).execute()
            if res.data:
                chapter_id = res.data[0]["id"]
                logger.info(f"Chapter {chapter_number} already exists in database: {chapter_id}")
                # We update summary_context
                supabase.table("chapters").update({"summary_context": summary_context}).eq("id", chapter_id).execute()
            else:
                insert_res = supabase.table("chapters").insert({
                    "project_id": project_id,
                    "chapter_number": chapter_number,
                    "summary_context": summary_context,
                    "status": "processing"
                }).execute()
                chapter_id = insert_res.data[0]["id"]
                logger.info(f"Inserted new chapter: {chapter_id}")
        except Exception as e:
            logger.exception(f"Failed to handle chapter record in Supabase: {e}")
            raise

        # 3. Fetch chapter HTML
        async with httpx.AsyncClient(follow_redirects=True, headers=self.headers, timeout=20.0) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                supabase.table("chapters").update({"status": "failed"}).eq("id", chapter_id).execute()
                raise RuntimeError(f"Failed to fetch chapter page: Status {resp.status_code}")
                
            soup = BeautifulSoup(resp.text, "html.parser")
            
            # Check if this is a Webtoons.com URL
            if "webtoons.com" in url.lower():
                logger.info("Webtoons.com URL detected. Applying Webtoons parsing logic...")
                img_list_div = soup.find("div", id="_imageList")
                if img_list_div:
                    img_tags = img_list_div.find_all("img")
                    manga_imgs = []
                    for img in img_tags:
                        src = img.get("data-url") or img.get("src")
                        if src:
                            manga_imgs.append(src)
                else:
                    img_tags = soup.find_all("img", class_="_images")
                    manga_imgs = [img.get("data-url") or img.get("src") for img in img_tags if img.get("data-url") or img.get("src")]
            else:
                # Find all image tags in div.page-chapter
                page_divs = soup.find_all("div", class_="page-chapter")
                if not page_divs:
                    logger.error("No page-chapter divs found on page. Checking alternative structures...")
                    # Try finding any img tag that contains chap_ or cdn
                    img_tags = soup.find_all("img")
                    manga_imgs = []
                    for img in img_tags:
                        src = img.get("src") or img.get("data-src") or img.get("data-original")
                        if src and ("chap" in src or "truyenvua" in src):
                            manga_imgs.append(src)
                else:
                    manga_imgs = []
                    for div in page_divs:
                        img = div.find("img")
                        if img:
                            src = img.get("src") or img.get("data-src") or img.get("data-original")
                            if src:
                                manga_imgs.append(src)
                            
            if not manga_imgs:
                logger.error("No manga panels found to download.")
                supabase.table("chapters").update({"status": "failed"}).eq("id", chapter_id).execute()
                raise ValueError("No panels found in chapter page HTML.")
                
            # Apply limit if max_panels parameter or configuration is set
            limit_panels = max_panels
            if limit_panels is None:
                limit_panels = getattr(config.orchestrator, "max_panels", None)
                
            if limit_panels is not None and limit_panels > 0:
                logger.info(f"Limiting raw pages to download to {limit_panels} as per settings.")
                manga_imgs = manga_imgs[:limit_panels]
                
            logger.info(f"Found {len(manga_imgs)} raw page images to download.")
            
            # 4. Download images sequentially or concurrently to raw_pages/
            sem = asyncio.Semaphore(5) # limit concurrency to 5 requests
            
            from src.core.utils import get_project_title_by_id, get_project_data_dir
            project_title = get_project_title_by_id(project_id)
            project_dir = get_project_data_dir(project_title)
            
            # Temporary directory for raw unsliced pages
            raw_pages_dir = project_dir / "raw_pages" / f"chap_{chapter_number}"
            raw_pages_dir.mkdir(parents=True, exist_ok=True)
            
            async def download_task(seq: int, img_url: str):
                async with sem:
                    ext = ".jpg" # Default extension
                    if ".png" in img_url.lower():
                        ext = ".png"
                    elif ".webp" in img_url.lower():
                        ext = ".webp"
                    
                    file_name = f"image_{chapter_number}_{seq}{ext}"
                    save_path = raw_pages_dir / file_name
                    
                    success = await self.download_image(client, img_url, save_path, url)
                    if success:
                        logger.info(f"Downloaded raw page {seq} -> {save_path}")
                    else:
                        logger.error(f"Failed downloading raw page {seq}")

            tasks = [download_task(i + 1, img_url) for i, img_url in enumerate(manga_imgs)]
            await asyncio.gather(*tasks)
            
            # 5. Run Stitch & Segment on the downloaded pages
            logger.info("--- Preprocessing: Stitch & Segment Chapter Pages ---")
            raw_images_dir = project_dir / "raw_images" / f"chap_{chapter_number}"
            raw_images_dir.mkdir(parents=True, exist_ok=True)
            
            from src.core.stitch_segment import preprocess_and_segment_images
            segmented_panels = preprocess_and_segment_images(
                raw_pages_dir=raw_pages_dir,
                output_panels_dir=raw_images_dir
            )
            
            if not segmented_panels:
                raise ValueError("Stitch & Segment failed to produce any valid panel images.")
                
            # 6. Insert segmented panels into Supabase
            logger.info(f"Registering {len(segmented_panels)} segmented panels in database...")
            
            # First, clean existing database panel records to prevent duplicates on re-run
            supabase.table("panels").delete().eq("chapter_id", chapter_id).execute()
            
            for idx, panel_path in enumerate(segmented_panels):
                seq = idx + 1
                try:
                    supabase.table("panels").insert({
                        "chapter_id": chapter_id,
                        "sequence_number": seq,
                        "raw_image_url": str(panel_path),
                        "status": "pending"
                    }).execute()
                except Exception as e:
                    logger.error(f"Failed to record panel {seq} in Supabase: {e}")
            
            # Update chapter status to crawled (processing in DB)
            supabase.table("chapters").update({"status": "processing"}).eq("id", chapter_id).execute()
            logger.info(f"Chapter {chapter_number} crawl and segmentation completed successfully.")
            return chapter_id, summary_context

crawler_service = CrawlerService()
