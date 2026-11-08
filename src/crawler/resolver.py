"""
src/crawler/resolver.py
SeriesResolver: URL classification and chapter listing discovery.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Union
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from src.crawler.errors import CrawlerError, CrawlerErrorCode
from src.crawler.schemas import SourceProfile, URLClassification


class SeriesResolver:
    """Classifies URLs and extracts chapter listings from series landing pages."""

    # Common patterns for chapter detection
    CHAPTER_PATTERNS = [
        re.compile(r"[-_/](?:chapter|chap|ep|episode|tap)[-_/]?(\d+(?:\.\d+)?)", re.IGNORECASE),
        re.compile(r"[?&](?:chapter|episode|ep|chap|episode_no)=(\d+(?:\.\d+)?)", re.IGNORECASE),
    ]


    # Common patterns for series landing page detection
    SERIES_PATTERNS = [
        re.compile(r"/(?:truyen-tranh|title|series|comic|manga)/[^/]+/?$", re.IGNORECASE),
        re.compile(r"/truyen/[^/]+/?$", re.IGNORECASE),
    ]

    IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}

    def classify_url(self, url: str, profile: Optional[SourceProfile] = None) -> URLClassification:
        """Determines whether a URL is a direct image, a chapter reader page, or a series landing page."""
        parsed = urlparse(url)
        path = parsed.path.lower()

        # 1. Direct image check
        if any(path.endswith(ext) for ext in self.IMAGE_EXTENSIONS):
            return URLClassification.DIRECT_IMAGE

        # 2. Check profile-specific patterns if present
        if profile and profile.url_patterns:
            for pattern in profile.url_patterns:
                if re.search(pattern, url, re.IGNORECASE):
                    if "chapter" in pattern.lower() or "viewer" in pattern.lower():
                        return URLClassification.CHAPTER
                    return URLClassification.SERIES

        # 3. Chapter URL heuristics
        for cp in self.CHAPTER_PATTERNS:
            if cp.search(url):
                return URLClassification.CHAPTER

        # 4. Series URL heuristics
        for sp in self.SERIES_PATTERNS:
            if sp.search(url):
                return URLClassification.SERIES

        # Fallback unknown
        return URLClassification.UNKNOWN

    def resolve_chapters(
        self,
        html: str,
        series_url: str,
        profile: Optional[SourceProfile] = None,
    ) -> List[Dict[str, Any]]:
        """
        Parses chapter listings from HTML.
        Returns a list of dicts: [{"chapter_number": "1", "title": "...", "url": "..."}]
        sorted ascending by chapter number.
        """
        soup = BeautifulSoup(html, "html.parser")
        cfg = profile.series_resolver if profile else None

        container = soup
        if cfg and cfg.chapter_list_selector:
            c = soup.select_one(cfg.chapter_list_selector)
            if c:
                container = c

        link_sel = cfg.chapter_link_selector if cfg else "a"
        num_regex_str = cfg.chapter_number_regex if cfg else r"(?:chap(?:ter)?|ep(?:isode)?|tập)?[_ -]*(\d+(?:\.\d+)?)"
        num_regex = re.compile(num_regex_str, re.IGNORECASE)

        raw_chapters: Dict[str, Dict[str, Any]] = {}

        for a in container.select(link_sel):
            href = a.get("href")
            if not href or href.startswith("#") or href.startswith("javascript:"):
                continue

            text = a.get_text(separator=" ", strip=True)
            combined_target = f"{text} {href}"

            # Search chapter number
            m = num_regex.search(combined_target)
            if not m:
                # Try generic number extraction
                m = re.search(r"\b(\d+(?:\.\d+)?)\b", text)

            if m:
                raw_num = m.group(1)
                try:
                    float_val = float(raw_num)
                    # Normalize string: "01" -> "1", "1.0" -> "1"
                    norm_num = str(int(float_val)) if float_val.is_integer() else str(float_val)
                except ValueError:
                    continue

                full_url = urljoin(series_url, href)
                title = text or f"Chapter {norm_num}"

                if norm_num not in raw_chapters:
                    raw_chapters[norm_num] = {
                        "chapter_number": norm_num,
                        "title": title,
                        "url": full_url,
                        "_order": float_val,
                    }

        if not raw_chapters:
            raise CrawlerError(
                message=f"No chapters found on series page: {series_url}",
                code=CrawlerErrorCode.NO_CHAPTERS_FOUND,
                url=series_url,
            )

        reverse = cfg.reverse_order if cfg else False
        sorted_list = sorted(raw_chapters.values(), key=lambda x: x["_order"], reverse=reverse)

        # Strip internal ordering key
        results = [
            {"chapter_number": item["chapter_number"], "title": item["title"], "url": item["url"]}
            for item in sorted_list
        ]
        return results

    def get_chapter_url(
        self,
        chapters: List[Dict[str, Any]],
        target_number: Union[str, int, float],
    ) -> Optional[str]:
        """Finds matching chapter URL by target number."""
        target_str = str(target_number).strip()
        try:
            target_float = float(target_str)
            target_norm = str(int(target_float)) if target_float.is_integer() else str(target_float)
        except ValueError:
            target_float = None
            target_norm = target_str

        for ch in chapters:
            c_num = str(ch.get("chapter_number", "")).strip()
            if c_num == target_norm or c_num == target_str:
                return ch.get("url")
            try:
                if target_float is not None and float(c_num) == target_float:
                    return ch.get("url")
            except ValueError:
                pass
        return None
