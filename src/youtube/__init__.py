"""
src/youtube
YouTube publishing, multi-account credential management, and SEO packaging.
"""

from src.youtube.account_manager import (
    YOUTUBE_SCOPES,
    YouTubeAccountManager,
    account_manager,
)

__all__ = [
    "YOUTUBE_SCOPES",
    "YouTubeAccountManager",
    "account_manager",
]
