"""
src/production/schemas.py
Pydantic v2 schemas for Single-Session Cloud Production Jobs and Telemetry.
Strictly decoupled, serializable to production_job.json, and audit-guaranteed zero secrets.
"""

from __future__ import annotations

import json
import os
import platform
import uuid
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

from src.core.schemas import utc_iso_now
from src.core.storage import atomic_write_json


class ProductionStageStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class ProductionJobConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    project_slug: str
    target_chapters: List[str] = Field(default_factory=list)
    encoder: str = "auto"
    tts_engine: str = "neural"
    tts_workers: int = 2
    render_workers: int = 2
    quality: str = "final"
    video_mode: str = "auto"
    burn_subtitles: bool = True
    animation: bool = True
    bgm_path: Optional[str] = None
    compile: bool = True
    export_bundle: bool = True
    cleanup_intermediates: bool = False
    force: bool = False
    voice_override: Optional[str] = None
    speed_override: Optional[float] = None
    subtitle_lines: Optional[int] = 1
    subtitle_font_size: Optional[int] = 26
    subtitle_chunking: Optional[str] = "sentence-aware"


class ChapterProductionProgress(BaseModel):
    model_config = ConfigDict(extra="ignore")

    chapter_number: str
    tts_status: ProductionStageStatus = ProductionStageStatus.PENDING
    tts_duration_seconds: float = 0.0
    tts_segments_synthesized: int = 0
    tts_segments_cached: int = 0
    render_status: ProductionStageStatus = ProductionStageStatus.PENDING
    render_duration_seconds: float = 0.0
    cached_clips_count: int = 0
    rendered_clips_count: int = 0
    video_path: Optional[str] = None
    subtitle_path: Optional[str] = None
    export_video_path: Optional[str] = None
    export_subtitle_path: Optional[str] = None
    error: Optional[str] = None


class ProductionJobTelemetry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    total_wall_time_seconds: float = 0.0
    tts_total_seconds: float = 0.0
    tts_model_init_seconds: float = 0.0
    tts_inference_seconds: float = 0.0
    render_total_seconds: float = 0.0
    compile_total_seconds: float = 0.0
    export_total_seconds: float = 0.0
    resolved_encoder: str = "libx264"
    encoder_status_message: str = ""
    hardware_platform: str = Field(default_factory=lambda: f"{platform.system()} {platform.machine()}")
    cpu_count: int = Field(default_factory=lambda: os.cpu_count() or 1)


class ProductionJobManifest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schema_version: str = "3.0.0"
    job_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    project_slug: str
    status: ProductionStageStatus = ProductionStageStatus.PENDING
    current_stage: str = "init"
    config: ProductionJobConfig
    chapters: Dict[str, ChapterProductionProgress] = Field(default_factory=dict)
    compiled_video_path: Optional[str] = None
    feature_timeline_path: Optional[str] = None
    master_audio_path: Optional[str] = None
    export_bundle_dir: Optional[str] = None
    telemetry: ProductionJobTelemetry = Field(default_factory=ProductionJobTelemetry)
    artifacts: List[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=utc_iso_now)
    updated_at: str = Field(default_factory=utc_iso_now)
    completed_at: Optional[str] = None
    error: Optional[str] = None

    def save(self, path: Union[str, Path]) -> None:
        """Atomically saves manifest to disk."""
        self.updated_at = utc_iso_now()
        atomic_write_json(Path(path), self.model_dump(mode="json"))

    @classmethod
    def load(cls, path: Union[str, Path]) -> ProductionJobManifest:
        """Loads and validates manifest from disk."""
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Production job manifest not found at: {p}")
        raw = json.loads(p.read_text(encoding="utf-8"))
        return cls.model_validate(raw)

    def audit_secrets(self) -> bool:
        """
        Validates the Zero Secrets guarantee: manifest contains no API tokens or passwords.
        Raises ValueError if potential credentials are detected.
        """
        forbidden_keywords = [
            "bearer ",
            "ghp_",
            "eyj",
            "openai_api_key",
            "gemini_api_key",
            "anthropic_api_key",
            "sk-",
        ]
        dump = json.dumps(self.model_dump(mode="json")).lower()
        for kw in forbidden_keywords:
            if kw in dump:
                raise ValueError(f"Secret leakage audit violation: detected pattern '{kw}' in manifest!")
        return True
