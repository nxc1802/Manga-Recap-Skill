import os
import httpx
from bs4 import BeautifulSoup
from pathlib import Path
import asyncio
from src.core.config import config
from src.core.logger import logger
from src.core.storage import read_json, atomic_write_json, calculate_sha256
from src.core.schemas import utc_iso_now
from src.core.utils import slugify_project_title, get_project_data_dir

class CrawlerService:
    def __init__(self):
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://www.webtoons.com/"
        }
        
    async def get_or_create_project(self, title: str) -> str:
        """
        Manages local project record in project.json.
        Creates it if it does not exist. Returns the project slug/identifier.
        """
        try:
            proj_slug = slugify_project_title(title)
            project_dir = get_project_data_dir(title)
            project_dir.mkdir(parents=True, exist_ok=True)
            project_json = project_dir / "project.json"

            if project_json.exists():
                logger.info(f"Found existing local project for '{title}': {proj_slug}")
                return proj_slug

            logger.info(f"Creating new local project for '{title}'...")
            init_project_data = {
                "schema_version": "3.0.0",
                "project_id": proj_slug,
                "slug": proj_slug,
                "title": title,
                "description": f"Recap project for {title}",
                "created_at": utc_iso_now(),
                "updated_at": utc_iso_now()
            }
            atomic_write_json(project_json, init_project_data)
            logger.info(f"Project created with slug: {proj_slug}")
            return proj_slug
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

    async def crawl_raw_pages(self, url: str, output_raw_dir: Path, max_panels: int = None) -> list[str]:
        """Crawls online chapter image URLs and downloads directly to output_raw_dir."""
        output_raw_dir.mkdir(parents=True, exist_ok=True)
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            resp = await client.get(url, headers=self.headers)
            if resp.status_code != 200:
                raise ValueError(f"Failed to fetch chapter page: HTTP {resp.status_code}")
            soup = BeautifulSoup(resp.text, "html.parser")
            manga_imgs = []

            # Check for Webtoons script-embedded image list (mobile & desktop)
            import re
            for s in soup.find_all("script"):
                txt = s.string or s.text or ""
                if "var imageList =" in txt or "imageList" in txt:
                    urls = re.findall(r'url:\s*\"([^\"]+)\"', txt)
                    if urls:
                        manga_imgs.extend(urls)
                        break

            if not manga_imgs:
                img_tags = soup.find_all("img")
                for img in img_tags:
                    src = img.get("data-url") or img.get("data-src") or img.get("src")
                    if src and any(d in src.lower() for d in ["webtoons.com", "pstatic.net", "comic", "manga", "chapter"]):
                        if not any(x in src.lower() for x in ["banner", "logo", "icon", "thumb", "bg_", "transparency"]):
                            manga_imgs.append(src)
                if not manga_imgs:
                    for img in img_tags:
                        src = img.get("data-url") or img.get("data-src") or img.get("src")
                        if src and any(ext in src.lower() for ext in [".jpg", ".jpeg", ".png", ".webp"]):
                            if not any(x in src.lower() for x in ["banner", "logo", "icon", "thumb", "bg_", "transparency"]):
                                manga_imgs.append(src)
            if max_panels and max_panels > 0:
                manga_imgs = manga_imgs[:max_panels]

            sem = asyncio.Semaphore(5)
            downloaded = []
            async def download_task(seq: int, img_url: str):
                async with sem:
                    ext = ".jpg"
                    if ".png" in img_url.lower():
                        ext = ".png"
                    elif ".webp" in img_url.lower():
                        ext = ".webp"
                    dest_name = f"page_{seq:03d}{ext}"
                    dest_path = output_raw_dir / dest_name
                    success = await self.download_image(client, img_url, dest_path, url)
                    if success:
                        downloaded.append(dest_name)
            tasks = [download_task(i + 1, img_url) for i, img_url in enumerate(manga_imgs)]
            await asyncio.gather(*tasks)
            return sorted(downloaded)

    async def crawl_chapter(self, project_id: str, chapter_number: str, general_story_context: str, chapter_base_url: str = None, max_panels: int = None) -> tuple[str, str]:
        """
        Orchestrates crawling a chapter:
        1. Fetch chapter HTML.
        2. Create/update chapter record in local chapter.json.
        3. Parse images.
        4. Download images locally to data/{project}/chapters/{num}/raw/page_*.jpg
        5. Stitch & segment images.
        6. Emit manifest.json.
        Returns (chapter_id, summary_context)
        """
        url_template = chapter_base_url or config.crawler.chapter_base_url
        if "[CHAPTER_NUMBER]" in url_template:
            url = url_template.replace("[CHAPTER_NUMBER]", chapter_number)
        elif "{chapter_number}" in url_template:
            url = url_template.replace("{chapter_number}", chapter_number)
        elif "{chapter}" in url_template:
            url = url_template.replace("{chapter}", chapter_number)
        elif "webtoons.com" in url_template.lower():
            # Dynamically replace episode number in webtoons URL
            import re
            url = re.sub(r'/episode-\d+/', f'/episode-{chapter_number}/', url_template)
            url = re.sub(r'([?&]episode_no=)\d+', r'\g<1>' + chapter_number, url)
        else:
            if url_template.startswith("http://") or url_template.startswith("https://"):
                url = url_template
            else:
                url = f"{url_template}{chapter_number}"
        logger.info(f"Crawling Chapter {chapter_number} from: {url}")
        
        # 1. Use manual story description from parameter instead of config
        summary_context = general_story_context
        
        # 2. Get or create chapter record locally
        project_dir = get_project_data_dir(project_id)
        chap_dir = project_dir / "chapters" / str(chapter_number)
        chap_dir.mkdir(parents=True, exist_ok=True)
        chapter_json_path = chap_dir / "chapter.json"
        chapter_id = f"{slugify_project_title(project_id)}_chap_{chapter_number}"

        try:
            if chapter_json_path.exists():
                chapter_data = read_json(chapter_json_path)
                chapter_data["summary_context"] = summary_context
                chapter_data["updated_at"] = utc_iso_now()
                logger.info(f"Chapter {chapter_number} already exists locally: {chapter_id}")
            else:
                chapter_data = {
                    "schema_version": "3.0.0",
                    "chapter_id": chapter_id,
                    "project_slug": slugify_project_title(project_id),
                    "chapter_number": str(chapter_number),
                    "title": f"Chapter {chapter_number}",
                    "summary_context": summary_context,
                    "status": "processing",
                    "created_at": utc_iso_now(),
                    "updated_at": utc_iso_now(),
                    "stages": {
                        "crawl": {"stage": "crawl", "status": "running", "started_at": utc_iso_now()},
                        "segment": {"stage": "segment", "status": "pending"},
                        "script": {"stage": "script", "status": "pending"},
                        "tts": {"stage": "tts", "status": "pending"},
                        "render": {"stage": "render", "status": "pending"},
                        "finalize": {"stage": "finalize", "status": "pending"}
                    }
                }
            atomic_write_json(chapter_json_path, chapter_data)
        except Exception as e:
            logger.exception(f"Failed to handle chapter record in local storage: {e}")
            raise

        # 3. Fetch chapter HTML
        async with httpx.AsyncClient(follow_redirects=True, headers=self.headers, timeout=20.0) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                chapter_data["status"] = "failed"
                chapter_data["stages"]["crawl"]["status"] = "failed"
                chapter_data["stages"]["crawl"]["error_message"] = f"Failed to fetch chapter page: Status {resp.status_code}"
                atomic_write_json(chapter_json_path, chapter_data)
                raise RuntimeError(f"Failed to fetch chapter page: Status {resp.status_code}")
                
            soup = BeautifulSoup(resp.text, "html.parser")
            
            # Check if this is a Webtoons.com URL
            if "webtoons.com" in url.lower():
                logger.info("Webtoons.com URL detected. Applying Webtoons parsing logic...")
                import re
                manga_imgs = []
                for s in soup.find_all("script"):
                    txt = s.string or s.text or ""
                    if "var imageList =" in txt or "imageList" in txt:
                        urls = re.findall(r'url:\s*\"([^\"]+)\"', txt)
                        if urls:
                            manga_imgs.extend(urls)
                            break
                if not manga_imgs:
                    img_list_div = soup.find("div", id="_imageList")
                    if img_list_div:
                        img_tags = img_list_div.find_all("img")
                        for img in img_tags:
                            src = img.get("data-url") or img.get("data-src") or img.get("src")
                            if src and not any(x in src.lower() for x in ["banner", "logo", "icon", "thumb", "bg_", "transparency"]):
                                manga_imgs.append(src)
                    else:
                        img_tags = soup.find_all("img", class_="_images")
                        manga_imgs = [
                            img.get("data-url") or img.get("data-src") or img.get("src")
                            for img in img_tags
                            if (img.get("data-url") or img.get("data-src") or img.get("src"))
                            and not any(x in (img.get("data-url") or img.get("data-src") or img.get("src")).lower() for x in ["banner", "logo", "icon", "thumb", "bg_", "transparency"])
                        ]
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
                chapter_data["status"] = "failed"
                chapter_data["stages"]["crawl"]["status"] = "failed"
                chapter_data["stages"]["crawl"]["error_message"] = "No panels found in chapter page HTML."
                atomic_write_json(chapter_json_path, chapter_data)
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
            
            # V3 layout: save directly to chapters/<num>/raw/page_*.jpg
            raw_pages_dir = chap_dir / "raw"
            raw_pages_dir.mkdir(parents=True, exist_ok=True)
            
            async def download_task(seq: int, img_url: str):
                async with sem:
                    ext = ".jpg" # Default extension
                    if ".png" in img_url.lower():
                        ext = ".png"
                    elif ".webp" in img_url.lower():
                        ext = ".webp"
                    
                    file_name = f"page_{seq:03d}{ext}"
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
            raw_images_dir = chap_dir / "panels"
            raw_images_dir.mkdir(parents=True, exist_ok=True)
            
            from src.core.stitch_segment import preprocess_and_segment_images
            segmented_panels = preprocess_and_segment_images(
                raw_pages_dir=raw_pages_dir,
                output_panels_dir=raw_images_dir
            )
            
            if not segmented_panels:
                chapter_data["status"] = "failed"
                chapter_data["stages"]["segment"]["status"] = "failed"
                chapter_data["stages"]["segment"]["error_message"] = "Stitch & Segment failed to produce any valid panel images."
                atomic_write_json(chapter_json_path, chapter_data)
                raise ValueError("Stitch & Segment failed to produce any valid panel images.")
                
            # 6. Save manifest.json locally
            logger.info(f"Registering {len(segmented_panels)} segmented panels in local manifest...")
            panels_manifest = []
            for idx, panel_path in enumerate(segmented_panels):
                seq = idx + 1
                p_path = Path(panel_path)
                panels_manifest.append({
                    "panel_id": f"panel_{seq:03d}",
                    "sequence_number": seq,
                    "filename": p_path.name,
                    "relative_path": str(p_path.relative_to(project_dir)),
                    "source_image": p_path.name,
                    "crop_box": [0, 0, 0, 0],
                    "width": 800,
                    "height": 1000,
                    "aspect_ratio": 0.8,
                    "sha256": calculate_sha256(p_path) if p_path.exists() else "",
                    "status": "active"
                })

            manifest_data = {
                "schema_version": "3.0.0",
                "project_slug": slugify_project_title(project_id),
                "chapter_number": str(chapter_number),
                "source_url": url,
                "created_at": utc_iso_now(),
                "total_panels": len(segmented_panels),
                "panels": panels_manifest
            }
            atomic_write_json(chap_dir / "manifest.json", manifest_data)
            # Backward-compatibility copy in recut directory
            atomic_write_json(raw_images_dir / "manifest.json", manifest_data)
            
            # Update chapter status to crawled
            chapter_data["status"] = "crawled"
            chapter_data["stages"]["crawl"]["status"] = "completed"
            chapter_data["stages"]["crawl"]["completed_at"] = utc_iso_now()
            chapter_data["stages"]["segment"]["status"] = "completed"
            chapter_data["stages"]["segment"]["completed_at"] = utc_iso_now()
            chapter_data["stages"]["segment"]["artifacts"] = [str(Path(p).relative_to(project_dir)) for p in segmented_panels]
            atomic_write_json(chapter_json_path, chapter_data)

            logger.info(f"Chapter {chapter_number} crawl and segmentation completed successfully.")
            return chapter_id, summary_context

crawler_service = CrawlerService()
