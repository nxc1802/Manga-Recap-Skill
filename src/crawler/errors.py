"""
src/crawler/errors.py
Error codes and exception definitions for Universal / Agentic Manga Crawler.
"""

from enum import Enum
from typing import Any, Dict, Optional
from src.cli.exit_codes import CliValidationError


class CrawlerErrorCode(str, Enum):
    BLOCKED_OR_AUTH_REQUIRED = "blocked_or_auth_required"
    UNSUPPORTED_STRUCTURE = "unsupported_structure"
    NO_CHAPTERS_FOUND = "no_chapters_found"
    NO_IMAGES_FOUND = "no_images_found"
    PARTIAL_DOWNLOAD = "partial_download"
    CORRUPT_ASSET = "corrupt_asset"
    NETWORK_TIMEOUT = "network_timeout"


class CrawlerError(CliValidationError):
    """Structured exception for crawler operations with error code and context."""

    def __init__(
        self,
        message: str,
        code: CrawlerErrorCode,
        domain: Optional[str] = None,
        url: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.code = code
        self.domain = domain
        self.url = url
        self.details = details or {}
