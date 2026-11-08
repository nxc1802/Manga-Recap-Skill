"""
src/crawler/extractor.py
ChapterExtractor: extracts ordered image URLs from chapter reader HTML.
"""

from __future__ import annotations

import re
from typing import List, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from src.crawler.errors import CrawlerError, CrawlerErrorCode
from src.crawler.schemas import SourceProfile


class ChapterExtractor:
    """Extracts ordered panel image URLs from webtoon/manga chapter reader pages."""

    DEFAULT_EXCLUDES = [
        "banner",
        "logo",
        "icon",
        "thumb",
        "bg_",
        "transparency",
        "advert",
        "avatar",
        "discord",
        "facebook",
        "button",
        "loading",
    ]

    DEFAULT_ATTRS = ["data-url", "data-src", "data-original", "data-lazy-src", "src"]

    def extract_image_urls(
        self,
        html: str,
        base_url: str,
        profile: Optional[SourceProfile] = None,
    ) -> List[str]:
        """Extracts ordered image URLs from script regex or DOM elements."""
        soup = BeautifulSoup(html, "html.parser")
        cfg = profile.chapter_extractor if profile else None

        images: List[str] = []

        # 1. Script parsing (Webtoons and embedded JS arrays)
        script_regex_pattern = cfg.script_regex if cfg else None
        if not script_regex_pattern:
            # Check for common webtoons script pattern
            script_regex_pattern = r'url:\s*\"([^\"]+)\"'

        for s in soup.find_all("script"):
            txt = s.string or s.text or ""
            if "var imageList =" in txt or "imageList" in txt or (cfg and cfg.script_regex and re.search(cfg.script_regex, txt)):
                found = re.findall(script_regex_pattern, txt)
                if found:
                    for u in found:
                        u_clean = u.replace("\\/", "/").strip()
                        if u_clean and not self._is_excluded(u_clean, cfg):
                            full_url = urljoin(base_url, u_clean)
                            if full_url not in images:
                                images.append(full_url)
                    if images:
                        return images

        # 2. DOM parsing
        container = soup
        if cfg and cfg.container_selector:
            c = soup.select_one(cfg.container_selector)
            if c:
                container = c
        else:
            # Try known reader containers
            for sel in ["div#_imageList", "div.page-chapter", "div.viewer", "div.reading-content", "div.chapter-content"]:
                c = soup.select_one(sel)
                if c:
                    container = c
                    break

        img_selectors = cfg.image_selectors if cfg else ["img"]
        url_attrs = cfg.url_attributes if cfg else self.DEFAULT_ATTRS

        seen = set(images)
        for selector in img_selectors:
            for elem in container.select(selector):
                raw_src = None
                for attr in url_attrs:
                    val = elem.get(attr)
                    if val and isinstance(val, str) and val.strip():
                        raw_src = val.strip()
                        break

                if not raw_src:
                    continue

                if self._is_excluded(raw_src, cfg):
                    continue

                # Ensure it looks like an image URL or belongs to comic cdn
                lower_src = raw_src.lower()
                is_img_like = any(ext in lower_src for ext in [".jpg", ".jpeg", ".png", ".webp", ".gif"]) or \
                              any(d in lower_src for d in ["webtoons.com", "pstatic.net", "comic", "manga", "chap", "truyen"])

                if not is_img_like:
                    continue

                full_url = urljoin(base_url, raw_src)
                if full_url not in seen:
                    seen.add(full_url)
                    images.append(full_url)

        if not images:
            domain = urlparse(base_url).netloc
            raise CrawlerError(
                message=f"No manga panel images found at {base_url}. Layout may have changed or domain profile is missing.",
                code=CrawlerErrorCode.NO_IMAGES_FOUND,
                domain=domain,
                url=base_url,
            )

        return images

    def _is_excluded(self, url_or_src: str, cfg) -> bool:
        low = url_or_src.lower()
        excludes = cfg.exclude_patterns if (cfg and cfg.exclude_patterns) else self.DEFAULT_EXCLUDES
        return any(x in low for x in excludes)
