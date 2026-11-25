"""Release, timeline, YouTube, and archive receipt Pydantic models."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field

from src.core.version import PROJECT_SCHEMA_VERSION, RECEIPT_SCHEMA_VERSION


def _utc_iso_now() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# ==============================================================================
# YouTube Packaging & Metadata Models
# ==============================================================================

class YouTubeMetadata(BaseModel):
    """Schema for YouTube publishing metadata (title, description, tags, timestamps, SEO)."""

    model_config = ConfigDict(extra="ignore")

    schema_version: str = PROJECT_SCHEMA_VERSION
    project_slug: str
    chapter_number: str
    title: str = Field(..., max_length=100, description="YouTube video title (recommended <= 65 chars)")
    description: str = Field(..., description="Full formatted YouTube description with SEO, timestamps, credits")
    tags: List[str] = Field(default_factory=list, description="YouTube search tags")
    category_id: str = Field(default="24", description="YouTube Category ID (24 = Entertainment, 1 = Film & Animation)")
    privacy_status: str = Field(default="private", description="public, unlisted, or private")
    made_for_kids: bool = Field(default=False, description="COPPA compliance flag")
    timestamps: List[Dict[str, str]] = Field(
        default_factory=list,
        description="Chapter marker timestamps [{'time': '00:00', 'label': '...'}]",
    )
    thumbnail_concept: Optional[str] = Field(default=None, description="Visual idea and overlay text for thumbnail")
    shorts_highlight: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Recommended 30-60s highlight snippet for YouTube Shorts",
    )


class YouTubeAccountInfo(BaseModel):
    """Configuration, channel metadata, and token path for an authorized YouTube channel account."""

    model_config = ConfigDict(extra="ignore")

    account_name: str = Field(..., description="Unique account alias or slug (e.g. 'main', 'ch2')")
    channel_title: str = Field(default="", description="YouTube Channel display name")
    channel_id: str = Field(default="", description="YouTube Channel ID (e.g. UCxxxx)")
    token_file: str = Field(..., description="Path to authorized user OAuth token JSON")
    client_secrets_file: str = Field(default="client_secrets.json", description="Path to OAuth client_secrets.json")
    created_at: str = Field(default_factory=_utc_iso_now)
    last_used_at: Optional[str] = None
    is_active: bool = True


class YouTubeAccountsRegistry(BaseModel):
    """Registry schema for multi-channel YouTube account management."""

    model_config = ConfigDict(extra="ignore")

    default_account: Optional[str] = None
    accounts: Dict[str, YouTubeAccountInfo] = Field(default_factory=dict)


# ==============================================================================
# Feature Timeline & YouTube Chapter Models
# ==============================================================================

class ChapterMarker(BaseModel):
    """Timestamp marker for YouTube Chapters or video milestones."""

    model_config = ConfigDict(extra="ignore")

    time: str = Field(..., description="Timestamp string (MM:SS or HH:MM:SS)")
    label: str = Field(..., description="Chapter marker label or scene title")
    seconds: float = Field(default=0.0, description="Exact offset in seconds from start")


class ChapterTimelineItem(BaseModel):
    """Segment boundary metadata for an individual chapter in a feature compilation."""

    model_config = ConfigDict(extra="ignore")

    chapter_number: str
    title: str
    start_seconds: float = Field(ge=0.0)
    end_seconds: float = Field(ge=0.0)
    duration_seconds: float = Field(ge=0.0)
    start_timestamp: str = Field(..., description="e.g. '00:00'")
    end_timestamp: str = Field(..., description="e.g. '05:12'")
    source_video_path: str
    sub_markers: List[ChapterMarker] = Field(default_factory=list)


class FeatureTimeline(BaseModel):
    """Schema for feature_timeline.json tracking multi-chapter cumulative compilation timeline."""

    model_config = ConfigDict(extra="ignore")

    schema_version: str = PROJECT_SCHEMA_VERSION
    project_slug: str
    compiled_video_path: str
    total_duration_seconds: float = Field(ge=0.0)
    chapters_count: int = Field(ge=1)
    chapters: List[ChapterTimelineItem] = Field(default_factory=list)
    youtube_chapter_markers: List[ChapterMarker] = Field(default_factory=list)
    created_at: str = Field(default_factory=_utc_iso_now)


# ==============================================================================
# Provider Receipts
# ==============================================================================

class ArchiveReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["4.0.0"] = RECEIPT_SCHEMA_VERSION
    receipt_type: Literal["huggingface_archive"] = "huggingface_archive"
    project_slug: str
    repo_id: str
    commit_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    remote_url: str
    created_at: str
    verified_file_count: int = Field(ge=0)
    total_bytes: int = Field(ge=0)
    manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: Literal["uploaded", "verified"]
    verification_revision: str | None = None
    last_verified_at: str | None = None
    restore_verified_at: str | None = None
    restored_revision: str | None = None
    restored_verified_files: int | None = Field(default=None, ge=0)


class YouTubeReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["4.0.0"] = RECEIPT_SCHEMA_VERSION
    receipt_type: Literal["youtube_upload", "youtube_verification"] = "youtube_upload"
    project_slug: str
    chapter_number: str
    video_id: str
    video_url: str
    account: str
    channel_title: str
    channel_id: str
    title: str | None = None
    upload_status: str | None = None
    has_captions: bool | None = None
    has_custom_thumbnail: bool | None = None
    verified_at: str | None = None
    privacy_status: Literal["private", "unlisted", "public"]
    captions_uploaded: bool
    thumbnail_uploaded: bool
    captions_required: bool
    thumbnail_required: bool
    uploaded_at: str
    status: Literal["uploaded", "verified"]
