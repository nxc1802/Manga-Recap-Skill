"""
src/crawler package.
Modular Universal / Agentic Manga Crawler components.
"""

from src.crawler.schemas import (
    URLClassification,
    SeriesResolverConfig,
    ChapterExtractorConfig,
    SourceProfile,
)
from src.crawler.errors import CrawlerErrorCode, CrawlerError
from src.crawler.resolver import SeriesResolver
from src.crawler.extractor import ChapterExtractor
from src.crawler.registry import SiteAdapterRegistry
from src.crawler.verifier import ImageVerifier, ImageVerificationReport

__all__ = [
    "URLClassification",
    "SeriesResolverConfig",
    "ChapterExtractorConfig",
    "SourceProfile",
    "CrawlerErrorCode",
    "CrawlerError",
    "SeriesResolver",
    "ChapterExtractor",
    "SiteAdapterRegistry",
    "ImageVerifier",
    "ImageVerificationReport",
]
