"""Typed CloudJob/CloudResult models and atomic cloud_job.json persistence."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from src.core.logger import logger
from src.core.storage import atomic_write_json, read_json


def _utc_iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CloudResult(BaseModel):
    """Validated summary of downloaded cloud chapter/series artifacts."""

    model_config = ConfigDict(extra="allow")

    status: str = "success"
    job_type: str
    project_slug: str
    chapter_number: Optional[str] = None
    chapters: List[str] = Field(default_factory=list)
    video_path: Optional[str] = None
    subtitle_path: Optional[str] = None
    final_video: Optional[str] = None
    compiled_video: Optional[str] = None
    export_paths: Dict[str, Optional[str]] = Field(default_factory=dict)


class CloudJob(BaseModel):
    """Persistent cloud job state stored in ``cloud_job.json``."""

    model_config = ConfigDict(extra="allow")

    project_slug: str = ""
    chapter_number: str = ""
    job_type: str = "produce"
    kernel_ref: str = ""
    dataset_ref: Optional[str] = None
    account_name: str = "unknown"
    username: str = "unknown"
    gpu: bool = True
    series: bool = False
    chapters: List[str] = Field(default_factory=list)
    payload_manifest_sha256: Optional[str] = None
    input_hashes: Dict[str, str] = Field(default_factory=dict)
    status: str = "QUEUED"
    queued_since: Optional[str] = None
    started_running_at: Optional[str] = None
    updated_at: str = Field(default_factory=_utc_iso_now)
    queue_elapsed_sec: Optional[float] = None
    total_elapsed_sec: Optional[float] = None
    queue_timeout_seconds: Optional[float] = None
    error: Optional[str] = None
    result: Optional[Dict[str, Any]] = None


def write_cloud_job_state(state_path: Optional[Path], payload: Dict[str, Any]) -> None:
    """Validates and atomically persists ``cloud_job.json`` if parent directory exists."""
    if state_path is None:
        return
    try:
        if state_path.parent.is_dir():
            model = CloudJob.model_validate(payload)
            atomic_write_json(state_path, model.model_dump(mode="json", exclude_none=True))
    except Exception as exc:
        logger.debug("Could not persist cloud_job.json (%s): %s", state_path, exc)


def load_cloud_job_state(state_path: Path) -> CloudJob:
    """Loads and validates ``cloud_job.json``."""
    return CloudJob.model_validate(read_json(state_path))
