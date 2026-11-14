"""Frozen lifecycle receipt/state shapes; transitions require separate evidence validators."""
from typing import Literal
from enum import Enum
from pydantic import BaseModel, ConfigDict, Field

SHA256 = r"^[a-f0-9]{64}$"


class ArchiveReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["4.0.0"] = "4.0.0"
    receipt_type: Literal["huggingface_archive"] = "huggingface_archive"
    project_slug: str
    repo_id: str
    commit_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    remote_url: str
    created_at: str
    verified_file_count: int = Field(ge=0)
    total_bytes: int = Field(ge=0)
    manifest_sha256: str = Field(pattern=SHA256)
    status: Literal["uploaded", "verified"]
    verification_revision: str | None = None
    last_verified_at: str | None = None
    restore_verified_at: str | None = None
    restored_revision: str | None = None
    restored_verified_files: int | None = Field(default=None, ge=0)


class YouTubeReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["4.0.0"] = "4.0.0"
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


class ReleaseState(str, Enum):
    DRAFT = "DRAFT"
    SETUP_VALID = "SETUP_VALID"
    SOURCE_READY = "SOURCE_READY"
    SCRIPT_READY = "SCRIPT_READY"
    AUDIO_VALID = "AUDIO_VALID"
    VISUAL_PLAN_VALID = "VISUAL_PLAN_VALID"
    RENDER_VALID = "RENDER_VALID"
    TECHNICALLY_VALID = "TECHNICALLY_VALID"
    HUMAN_APPROVED = "HUMAN_APPROVED"
    PRODUCTION_READY = "PRODUCTION_READY"
    UPLOADED = "UPLOADED"
    YOUTUBE_VERIFIED = "YOUTUBE_VERIFIED"
    ARCHIVED = "ARCHIVED"
    ARCHIVE_VERIFIED = "ARCHIVE_VERIFIED"
    RESTORE_VERIFIED = "RESTORE_VERIFIED"
    CLEANUP_ALLOWED = "CLEANUP_ALLOWED"


class ReleaseRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["4.1.0"] = "4.1.0"
    project_slug: str
    chapter_number: str
    state: ReleaseState
    evidence_hashes: dict[str, str]


class TombstoneRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["4.0.0"] = "4.0.0"
    tombstone_version: Literal["4.0.0"] = "4.0.0"
    project_slug: str = Field(min_length=1)
    archive_timestamp: str | None = None
    cleanup_timestamp: str = Field(min_length=1)
    hf_repo_id: str = Field(min_length=1)
    hf_commit_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    hf_remote_url: str = Field(min_length=1)
    manifest_sha256: str = Field(pattern=SHA256)
    youtube_videos: dict[str, str] = Field(default_factory=dict)
    youtube_videos_total: int | None = Field(default=None, ge=0)
    youtube_videos_truncated: bool = False
    reclaimed_bytes: int = Field(ge=0)
    cleaned_files_count: int = Field(ge=0)
