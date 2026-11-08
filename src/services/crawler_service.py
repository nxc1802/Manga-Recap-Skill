import asyncio
import os
import re
from pathlib import Path
from typing import Optional, Union
from urllib.parse import urlparse

from bs4 import BeautifulSoup
import httpx

from src.core.config import config
from src.core.logger import logger
from src.core.schemas import utc_iso_now
from src.core.storage import atomic_write_json, calculate_sha256, read_json
from src.core.utils import get_project_data_dir, slugify_project_title
from src.crawler.errors import CrawlerError, CrawlerErrorCode
from src.crawler.extractor import ChapterExtractor
from src.crawler.registry import SiteAdapterRegistry
from src.crawler.resolver import SeriesResolver
from src.crawler.schemas import URLClassification
from src.crawler.verifier import ImageVerifier


class CrawlerService:
    def __init__(self):
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://www.webtoons.com/",
        }
        self.resolver = SeriesResolver()
        self.registry = SiteAdapterRegistry()
        self.extractor = ChapterExtractor()
        self.verifier = ImageVerifier()

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
                "updated_at": utc_iso_now(),
            }
            atomic_write_json(project_json, init_project_data)
            logger.info(f"Project created with slug: {proj_slug}")
            return proj_slug
        except Exception as e:
            logger.exception(f"Error in get_or_create_project: {e}")
            raise

    async def download_image(
        self,
        client: httpx.AsyncClient,
        img_url: str,
        save_path: Path,
        chapter_base_url: str = None,
    ) -> bool:
        """
        Downloads a single image with referrer headers, saving it locally.
        Re-verifies existing file; re-downloads if corrupted or 0 bytes.
        """
        if save_path.exists():
            ok, _ = self.verifier.verify_image(save_path, strict=False)
            if ok:
                return True

        headers = self.headers.copy()

        # Resolve profile headers / referer
        ref_target = chapter_base_url or img_url
        profile = self.registry.get_profile(ref_target)
        if profile and profile.headers:
            headers.update(profile.headers)

        # Referer priority
        if "webtoons.com" in img_url.lower() or "pstatic.net" in img_url.lower():
            headers["Referer"] = "https://www.webtoons.com/"
        elif chapter_base_url:
            headers["Referer"] = chapter_base_url
        elif config.crawler.chapter_base_url:
            headers["Referer"] = config.crawler.chapter_base_url

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

    async def crawl_raw_pages(
        self,
        url: str,
        output_raw_dir: Path,
        max_panels: int = None,
        chapter_number: Optional[str] = None,
        project_slug: Optional[str] = None,
        strict_verify: bool = False,
    ) -> list[str]:
        """
        Crawls online chapter image URLs and downloads directly to output_raw_dir.
        Supports URL classification (direct image, series landing page, chapter reader).
        """
        output_raw_dir.mkdir(parents=True, exist_ok=True)

        # 1. URL classification
        classification = self.resolver.classify_url(url)

        if classification == URLClassification.DIRECT_IMAGE:
            # Single image download
            dest_name = f"page_001{Path(urlparse(url).path).suffix or '.jpg'}"
            dest_path = output_raw_dir / dest_name
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                success = await self.download_image(client, url, dest_path, url)
                if success:
                    return [dest_name]
                raise CrawlerError(
                    f"Failed to download direct image: {url}",
                    code=CrawlerErrorCode.PARTIAL_DOWNLOAD,
                    url=url,
                )

        target_reader_url = url
        profile = self.registry.get_profile(url, project_slug=project_slug)
        headers = dict(self.headers)
        if profile and profile.headers:
            headers.update(profile.headers)

        # If it's a series landing page, resolve target chapter reader URL
        if classification == URLClassification.SERIES:
            logger.info(f"Series URL detected: {url}. Resolving chapter reader URL...")
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code in (401, 403):
                    raise CrawlerError(
                        f"Access blocked (HTTP {resp.status_code}) at series page {url}",
                        code=CrawlerErrorCode.BLOCKED_OR_AUTH_REQUIRED,
                        domain=profile.domain,
                        url=url,
                    )
                if resp.status_code != 200:
                    raise CrawlerError(
                        f"Failed to fetch series page: HTTP {resp.status_code}",
                        code=CrawlerErrorCode.NO_CHAPTERS_FOUND,
                        url=url,
                    )
                chapters = self.resolver.resolve_chapters(resp.text, url, profile)
                target_ch = chapter_number or "1"
                matched_url = self.resolver.get_chapter_url(chapters, target_ch)
                if not matched_url:
                    raise CrawlerError(
                        f"Chapter '{target_ch}' not found on series page {url}",
                        code=CrawlerErrorCode.NO_CHAPTERS_FOUND,
                        domain=profile.domain,
                        url=url,
                    )
                target_reader_url = matched_url
                logger.info(f"Resolved Chapter {target_ch} reader URL: {target_reader_url}")

        # 2. Fetch chapter reader HTML
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            resp = await client.get(target_reader_url, headers=headers)
            if resp.status_code in (401, 403):
                raise CrawlerError(
                    f"Access blocked (HTTP {resp.status_code}) at {target_reader_url}",
                    code=CrawlerErrorCode.BLOCKED_OR_AUTH_REQUIRED,
                    domain=profile.domain,
                    url=target_reader_url,
                )
            if resp.status_code != 200:
                raise ValueError(f"Failed to fetch chapter page: HTTP {resp.status_code}")

            # 3. Extract ordered image URLs
            manga_imgs = self.extractor.extract_image_urls(resp.text, target_reader_url, profile)

            if max_panels and max_panels > 0:
                manga_imgs = manga_imgs[:max_panels]

            sem = asyncio.Semaphore(5)
            downloaded = []

            async def download_task(seq: int, img_url: str):
                async with sem:
                    ext = ".jpg"
                    low_url = img_url.lower()
                    if ".png" in low_url:
                        ext = ".png"
                    elif ".webp" in low_url:
                        ext = ".webp"
                    dest_name = f"page_{seq:03d}{ext}"
                    dest_path = output_raw_dir / dest_name
                    success = await self.download_image(client, img_url, dest_path, target_reader_url)
                    if success:
                        if strict_verify:
                            ok, err = self.verifier.verify_image(dest_path, strict=True)
                            if not ok:
                                logger.warning(f"Downloaded image failed strict verify: {err}")
                                return
                        downloaded.append(dest_name)

            tasks = [download_task(i + 1, img_url) for i, img_url in enumerate(manga_imgs)]
            await asyncio.gather(*tasks)

            if not downloaded:
                raise CrawlerError(
                    f"No images successfully downloaded for {target_reader_url}",
                    code=CrawlerErrorCode.PARTIAL_DOWNLOAD,
                    domain=profile.domain,
                    url=target_reader_url,
                )

            return sorted(downloaded)

    async def crawl_chapter(
        self,
        project_id: str,
        chapter_number: str,
        general_story_context: str,
        chapter_base_url: str = None,
        max_panels: int = None,
    ) -> tuple[str, str]:
        """
        Orchestrates crawling a chapter:
        1. Fetch chapter HTML / image list.
        2. Create/update chapter record in local chapter.json.
        3. Download images locally to data/{project}/chapters/{num}/raw/page_*.jpg
        4. Stitch & segment images.
        5. Emit manifest.json.
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
            url = re.sub(r'/episode-\d+/', f'/episode-{chapter_number}/', url_template)
            url = re.sub(r'([?&]episode_no=)\d+', r'\g<1>' + chapter_number, url)
        else:
            if url_template.startswith("http://") or url_template.startswith("https://"):
                url = url_template
            else:
                url = f"{url_template}{chapter_number}"

        logger.info(f"Crawling Chapter {chapter_number} from: {url}")
        summary_context = general_story_context

        # Get or create chapter record locally
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
                        "finalize": {"stage": "finalize", "status": "pending"},
                    },
                }
            atomic_write_json(chapter_json_path, chapter_data)
        except Exception as e:
            logger.exception(f"Failed to handle chapter record in local storage: {e}")
            raise

        raw_pages_dir = chap_dir / "raw"
        raw_pages_dir.mkdir(parents=True, exist_ok=True)

        limit_panels = max_panels
        if limit_panels is None:
            limit_panels = getattr(config.orchestrator, "max_panels", None)

        try:
            downloaded = await self.crawl_raw_pages(
                url=url,
                output_raw_dir=raw_pages_dir,
                max_panels=limit_panels,
                chapter_number=str(chapter_number),
                project_slug=slugify_project_title(project_id),
            )
            if not downloaded:
                raise ValueError("No panels found in chapter page HTML.")
        except Exception as e:
            chapter_data["status"] = "failed"
            chapter_data["stages"]["crawl"]["status"] = "failed"
            chapter_data["stages"]["crawl"]["error_message"] = str(e)
            atomic_write_json(chapter_json_path, chapter_data)
            raise

        # Preprocessing: Stitch & Segment Chapter Pages
        logger.info("--- Preprocessing: Stitch & Segment Chapter Pages ---")
        raw_images_dir = chap_dir / "panels"
        raw_images_dir.mkdir(parents=True, exist_ok=True)

        from src.core.stitch_segment import preprocess_and_segment_images
        segmented_panels = preprocess_and_segment_images(
            raw_pages_dir=raw_pages_dir,
            output_panels_dir=raw_images_dir,
        )

        if not segmented_panels:
            chapter_data["status"] = "failed"
            chapter_data["stages"]["segment"]["status"] = "failed"
            chapter_data["stages"]["segment"]["error_message"] = "Stitch & Segment failed to produce any valid panel images."
            atomic_write_json(chapter_json_path, chapter_data)
            raise ValueError("Stitch & Segment failed to produce any valid panel images.")

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
                "status": "active",
            })

        manifest_data = {
            "schema_version": "3.0.0",
            "project_slug": slugify_project_title(project_id),
            "chapter_number": str(chapter_number),
            "source_url": url,
            "created_at": utc_iso_now(),
            "total_panels": len(segmented_panels),
            "panels": panels_manifest,
        }
        atomic_write_json(chap_dir / "manifest.json", manifest_data)
        atomic_write_json(raw_images_dir / "manifest.json", manifest_data)

        # Update chapter status to crawled
        chapter_data["status"] = "crawled"
        chapter_data["stages"]["crawl"]["status"] = "completed"
        chapter_data["stages"]["crawl"]["completed_at"] = utc_iso_now()
        chapter_data["stages"]["segment"]["status"] = "completed"
        chapter_data["stages"]["segment"]["completed_at"] = utc_iso_now()
        chapter_data["stages"]["segment"]["artifacts"] = [
            str(Path(p).relative_to(project_dir)) for p in segmented_panels
        ]
        atomic_write_json(chapter_json_path, chapter_data)

        logger.info(f"Chapter {chapter_number} crawl and segmentation completed successfully.")
        return chapter_id, summary_context


crawler_service = CrawlerService()
