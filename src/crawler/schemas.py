"""
src/crawler/schemas.py
Pydantic schemas and enums for Universal/Agentic Crawler.
"""

from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field
from src.core.schemas import utc_iso_now


class URLClassification(str, Enum):
    SERIES = "series"
    CHAPTER = "chapter"
    DIRECT_IMAGE = "direct_image"
    UNKNOWN = "unknown"


class SeriesResolverConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    chapter_list_selector: Optional[str] = None
    chapter_link_selector: str = "a"
    chapter_number_regex: str = r"(?:chap(?:ter)?|ep(?:isode)?)[_ -]*(\d+(?:\.\d+)?)"
    reverse_order: bool = False


class ChapterExtractorConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    container_selector: Optional[str] = None
    image_selectors: List[str] = Field(default_factory=lambda: ["img"])
    url_attributes: List[str] = Field(
        default_factory=lambda: ["data-url", "data-src", "data-original", "data-lazy-src", "src"]
    )
    script_regex: Optional[str] = None
    exclude_patterns: List[str] = Field(
        default_factory=lambda: [
            "banner",
            "logo",
            "icon",
            "thumb",
            "bg_",
            "transparency",
            "advert",
            "avatar",
            "discord",
        ]
    )


class SourceProfile(BaseModel):
    model_config = ConfigDict(extra="ignore")
    schema_version: str = "1.0.0"
    domain: str
    name: str
    url_patterns: List[str] = Field(default_factory=list)
    headers: Dict[str, str] = Field(default_factory=dict)
    series_resolver: SeriesResolverConfig = Field(default_factory=SeriesResolverConfig)
    chapter_extractor: ChapterExtractorConfig = Field(default_factory=ChapterExtractorConfig)
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    last_verified: str = Field(default_factory=utc_iso_now)
    requires_js: bool = False
    rate_limit_seconds: float = 0.5
