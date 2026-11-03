"""
src/core/schemas.py
Enterprise Pydantic v2 schemas for Manga Recap Agentic Skill V3.
Governs: project.json, chapter.json, script.json, characters.json, manifest.json.
"""

from __future__ import annotations

import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


def utc_iso_now() -> str:
    """Returns current UTC timestamp in ISO 8601 format."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# ==============================================================================
# 1. Enums
# ==============================================================================

class StageType(str, Enum):
    """Pipeline execution stages for chapter workflow."""
    CRAWL = "crawl"
    SEGMENT = "segment"
    SCRIPT = "script"
    TTS = "tts"
    RENDER = "render"
    FINALIZE = "finalize"


class CheckpointStatus(str, Enum):
    """Execution status for stage checkpoints and overall chapters."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"


class CameraMotionType(str, Enum):
    """Cinematic camera motion modes supported by Video Engine V2."""
    STATIC = "static"
    PAN_LEFT = "pan_left"
    PAN_RIGHT = "pan_right"
    PAN_UP = "pan_up"
    PAN_DOWN = "pan_down"
    ZOOM_IN = "zoom_in"
    ZOOM_OUT = "zoom_out"
    FOCUS_CROP = "focus_crop"
    VERTICAL_SCROLL = "vertical_scroll"


class PanelStatus(str, Enum):
    """Classification of segmented panels in manifest."""
    ACTIVE = "active"                   # Standard panel rendered into video
    CONTEXT_ONLY = "context_only"       # Narrative context for Agent, not rendered
    SKIP = "skip"                       # Explicitly excluded (ads, duplicate, credits)
    NEEDS_REVIEW = "needs_review"       # Ambiguous cut or gutter anomaly


class CharacterRole(str, Enum):
    """Role classification of story characters."""
    PROTAGONIST = "protagonist"
    ANTAGONIST = "antagonist"
    SUPPORTING = "supporting"
    EXTRA = "extra"


class CharacterGender(str, Enum):
    """Gender classification for TTS voice selection."""
    MALE = "male"
    FEMALE = "female"
    NEUTRAL = "neutral"
    UNKNOWN = "unknown"


class VoiceStrategy(str, Enum):
    """Narration and voice assignment strategy."""
    SINGLE_NARRATOR = "single_narrator"               # Strategy A: One voice throughout
    NARRATOR_AND_CHARACTERS = "narrator_and_characters"# Strategy B: Narrator + Character voices
    EMOTION_AWARE = "emotion_aware"                   # Strategy C: Emotion-tuned narrator cues


# ==============================================================================
# 2. Geometric & Sub-models
# ==============================================================================

class FocusBox(BaseModel):
    """
    Normalized bounding box [x_min, y_min, x_max, y_max] in 0.0 - 1.0 range.
    Supports backward-compatibility coordinate conversions (focus_x, focus_y, crop_scale).
    """
    model_config = ConfigDict(extra="forbid", frozen=True)

    x_min: float = Field(default=0.0, ge=0.0, le=1.0, description="Normalized left coordinate (0.0 - 1.0)")
    y_min: float = Field(default=0.0, ge=0.0, le=1.0, description="Normalized top coordinate (0.0 - 1.0)")
    x_max: float = Field(default=1.0, ge=0.0, le=1.0, description="Normalized right coordinate (0.0 - 1.0)")
    y_max: float = Field(default=1.0, ge=0.0, le=1.0, description="Normalized bottom coordinate (0.0 - 1.0)")

    @model_validator(mode="after")
    def validate_box_order(self) -> FocusBox:
        if self.x_min > self.x_max:
            raise ValueError(f"x_min ({self.x_min}) must be <= x_max ({self.x_max})")
        if self.y_min > self.y_max:
            raise ValueError(f"y_min ({self.y_min}) must be <= y_max ({self.y_max})")
        return self

    @property
    def focus_x(self) -> float:
        """Center X in percentage (0.0 - 100.0)."""
        return round(((self.x_min + self.x_max) / 2.0) * 100.0, 4)

    @property
    def focus_y(self) -> float:
        """Center Y in percentage (0.0 - 100.0)."""
        return round(((self.y_min + self.y_max) / 2.0) * 100.0, 4)

    @property
    def crop_scale(self) -> float:
        """Scale factor percentage based on width ratio (0.0 - 100.0)."""
        return round(max(1.0, (self.x_max - self.x_min) * 100.0), 4)

    @classmethod
    def from_center_and_scale(
        cls,
        focus_x: float = 50.0,
        focus_y: float = 50.0,
        crop_scale: float = 100.0
    ) -> FocusBox:
        """Constructs a FocusBox from legacy center percentage and crop scale."""
        scale_ratio = max(0.05, min(1.0, crop_scale / 100.0))
        cx = max(0.0, min(1.0, focus_x / 100.0))
        cy = max(0.0, min(1.0, focus_y / 100.0))

        half_w = scale_ratio / 2.0
        half_h = scale_ratio / 2.0

        x_min = max(0.0, cx - half_w)
        x_max = min(1.0, cx + half_w)
        y_min = max(0.0, cy - half_h)
        y_max = min(1.0, cy + half_h)

        return cls(x_min=round(x_min, 4), y_min=round(y_min, 4), x_max=round(x_max, 4), y_max=round(y_max, 4))


class RenderSettings(BaseModel):
    """Project-level video rendering defaults."""
    model_config = ConfigDict(extra="ignore")

    target_width: int = Field(default=1280, ge=320)
    target_height: int = Field(default=720, ge=240)
    fps: int = Field(default=30, ge=15, le=60)
    video_codec: str = "libx264"
    audio_codec: str = "aac"
    crf: int = Field(default=20, ge=0, le=51)
    preset: str = "medium"
    burn_subtitles: bool = True
    video_mode: str = "auto"


class AudioSettings(BaseModel):
    """Project-level audio synthesis defaults."""
    model_config = ConfigDict(extra="ignore")

    default_voice: str = "Trúc Ly"
    default_speed: float = Field(default=1.0, gt=0.2, le=3.0)
    sample_rate: int = Field(default=48000, description="Master uncompressed audio sample rate (Hz)")
    bgm_path: Optional[str] = None
    bgm_volume: float = Field(default=0.15, ge=0.0, le=1.0)
    voice_volume: float = Field(default=1.0, ge=0.0, le=2.0)
    ducking_factor: float = Field(default=0.30, ge=0.0, le=1.0)


# ==============================================================================
# 3. Stage Checkpoint Model
# ==============================================================================

class StageCheckpoint(BaseModel):
    """State tracking for a single pipeline stage."""
    model_config = ConfigDict(extra="ignore")

    stage: StageType
    status: CheckpointStatus = CheckpointStatus.PENDING
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    error_message: Optional[str] = None
    retry_count: int = Field(default=0, ge=0)
    artifacts: List[str] = Field(default_factory=list, description="List of relative file paths created")
    checksums: Dict[str, str] = Field(default_factory=dict, description="Relative path -> sha256 hex digest")


class ChapterStats(BaseModel):
    """Aggregate numerical metrics for a chapter."""
    model_config = ConfigDict(extra="ignore")

    total_raw_images: int = Field(default=0, ge=0)
    total_panels: int = Field(default=0, ge=0)
    total_segments: int = Field(default=0, ge=0)
    total_audio_duration_seconds: float = Field(default=0.0, ge=0.0)
    final_video_duration_seconds: float = Field(default=0.0, ge=0.0)
    video_path: Optional[str] = None


def default_stages_factory() -> Dict[StageType, StageCheckpoint]:
    """Generates a pristine dictionary of initial stage checkpoints."""
    return {stage: StageCheckpoint(stage=stage, status=CheckpointStatus.PENDING) for stage in StageType}


# ==============================================================================
# 4. Root Document: project.json
# ==============================================================================

class ProjectConfig(BaseModel):
    """Schema for data/projects/<project_slug>/project.json."""
    model_config = ConfigDict(extra="ignore")

    project_id: str
    slug: str
    title: str
    description: Optional[str] = None
    source_url: Optional[str] = None
    language: str = "vi"
    created_at: str = Field(default_factory=utc_iso_now)
    updated_at: str = Field(default_factory=utc_iso_now)
    render_settings: RenderSettings = Field(default_factory=RenderSettings)
    audio_settings: AudioSettings = Field(default_factory=AudioSettings)
    chapters: List[str] = Field(default_factory=list, description="Registered chapter identifiers")
    schema_version: str = "3.0.0"


# ==============================================================================
# 5. Root Document: chapter.json
# ==============================================================================

class ChapterMetadata(BaseModel):
    """Schema for data/projects/<project_slug>/chapters/<chapter_num>/chapter.json."""
    model_config = ConfigDict(extra="ignore")

    chapter_id: str
    chapter_number: str
    project_slug: str
    title: str
    source_url: Optional[str] = None
    created_at: str = Field(default_factory=utc_iso_now)
    updated_at: str = Field(default_factory=utc_iso_now)
    current_stage: StageType = StageType.CRAWL
    overall_status: CheckpointStatus = CheckpointStatus.PENDING
    stages: Dict[StageType, StageCheckpoint] = Field(default_factory=default_stages_factory)
    stats: ChapterStats = Field(default_factory=ChapterStats)
    schema_version: str = "3.0.0"

    @property
    def checkpoints(self) -> Dict[StageType, StageCheckpoint]:
        """Convenience alias for stages mapping."""
        return self.stages


# ==============================================================================
# 6. Root Document: script.json
# ==============================================================================

class ScriptSegment(BaseModel):
    """An individual narration utterance mapped to visual panels."""
    model_config = ConfigDict(extra="ignore")

    seq_id: str = Field(..., description="Unique segment sequence string, e.g. '001', '002_1'")
    panel_ids: List[str] = Field(default_factory=list, description="Referenced panel filenames or IDs")
    narrator_text: str = Field(..., min_length=1, description="Vietnamese narration text")
    character: Optional[str] = Field(default=None, description="Character speaker name or None for narrator")
    emotion: Optional[str] = Field(default="neutral", description="Emotion intent: neutral, dramatic, sad, excited, etc.")
    emotion_cue: Optional[str] = Field(default=None, description="Audible cue: [cười], [thở dài], [hắng giọng], or None")
    voice_id: str = Field(default="Trúc Ly", description="VieNeu preset voice ID")
    camera_motion: CameraMotionType = Field(default=CameraMotionType.STATIC)
    focus_box: Optional[FocusBox] = None
    duration: Optional[float] = Field(default=None, ge=0.0, description="Measured waveform duration in seconds")
    audio_path: Optional[str] = Field(default=None, description="Relative path to synthesized utterance WAV")
    checksum: Optional[str] = Field(default=None, description="SHA-256 fingerprint of utterance audio/text")
    status: str = Field(default="pending", description="Segment status: pending, synthesized, verified, needs_review")

    @field_validator("seq_id")
    @classmethod
    def clean_seq_id(cls, v: str) -> str:
        return str(v).strip()


class RecapScript(BaseModel):
    """Schema for data/projects/<project_slug>/chapters/<chapter_num>/script.json."""
    model_config = ConfigDict(extra="ignore")

    project_slug: str
    chapter_number: str
    title: str
    voice_strategy: VoiceStrategy = VoiceStrategy.SINGLE_NARRATOR
    default_voice: str = "Trúc Ly"
    segments: List[ScriptSegment] = Field(default_factory=list)
    total_duration_seconds: float = Field(default=0.0, ge=0.0)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    schema_version: str = "3.0.0"

    @classmethod
    def from_legacy_dict(cls, project_slug: str, chapter_number: str, title: str, legacy_data: Dict[str, Any]) -> RecapScript:
        """Upgrades legacy dictionary format (from script.csv or old json) to RecapScript."""
        segments: List[ScriptSegment] = []
        for seq_str, content in legacy_data.items():
            if isinstance(content, str):
                segments.append(ScriptSegment(
                    seq_id=str(seq_str),
                    panel_ids=[f"panel_{seq_str}.jpg"],
                    narrator_text=content,
                    camera_motion=CameraMotionType.STATIC
                ))
            elif isinstance(content, dict):
                fx = float(content.get("focus_x", 50))
                fy = float(content.get("focus_y", 50))
                cs = float(content.get("crop_scale", 100))
                box = FocusBox.from_center_and_scale(focus_x=fx, focus_y=fy, crop_scale=cs)

                motion_str = str(content.get("pan_direction", "static")).lower()
                motion_map = {
                    "horizontal": CameraMotionType.PAN_LEFT,
                    "vertical": CameraMotionType.PAN_DOWN,
                    "zoom": CameraMotionType.ZOOM_IN,
                    "static": CameraMotionType.STATIC
                }
                motion = motion_map.get(motion_str, CameraMotionType.STATIC)

                segments.append(ScriptSegment(
                    seq_id=str(seq_str),
                    panel_ids=content.get("panel_ids", [f"panel_{seq_str}.jpg"]),
                    narrator_text=content.get("text", ""),
                    character=content.get("character"),
                    emotion=content.get("emotion", "neutral"),
                    camera_motion=motion,
                    focus_box=box,
                    status=content.get("status", "pending")
                ))

        return cls(
            project_slug=project_slug,
            chapter_number=str(chapter_number),
            title=title,
            segments=segments
        )


# ==============================================================================
# 7. Root Document: characters.json
# ==============================================================================

class CharacterProfile(BaseModel):
    """Profile of an identified story character."""
    model_config = ConfigDict(extra="ignore")

    canonical_name: str
    aliases: List[str] = Field(default_factory=list)
    role: CharacterRole = CharacterRole.SUPPORTING
    gender: CharacterGender = CharacterGender.UNKNOWN
    description: Optional[str] = None
    core_features: str = Field(default="", description="Key facial, hair, and physiological traits")
    current_outfit: str = Field(default="", description="Last observed clothing description")
    assigned_voice: Optional[str] = Field(default=None, description="VieNeu voice preset identifier")
    voice_settings: Optional[Dict[str, Any]] = Field(default_factory=dict)
    first_appearance: Optional[str] = None
    image_crops: List[str] = Field(default_factory=list, description="Relative paths to visual reference crops")
    notes: Optional[str] = None


class CharacterDictionary(BaseModel):
    """Schema for data/projects/<project_slug>/characters.json."""
    model_config = ConfigDict(extra="ignore")

    project_slug: str
    characters: Dict[str, CharacterProfile] = Field(default_factory=dict)
    updated_at: str = Field(default_factory=utc_iso_now)
    schema_version: str = "3.0.0"


# ==============================================================================
# 8. Root Document: manifest.json
# ==============================================================================

class RawImageInfo(BaseModel):
    """Scraped / imported raw page image."""
    model_config = ConfigDict(extra="ignore")

    filename: str
    relative_path: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    sha256: str


class PanelItem(BaseModel):
    """Segmented panel crop information."""
    model_config = ConfigDict(extra="ignore")

    panel_id: str = Field(..., description="e.g. 'panel_001'")
    sequence_number: int = Field(ge=1)
    filename: str = Field(..., description="e.g. 'panel_001.jpg'")
    relative_path: str = Field(..., description="panels/panel_001.jpg")
    source_image: str = Field(..., description="Original raw image filename")
    crop_box: Tuple[int, int, int, int] = Field(..., description="(left, top, right, bottom) pixel coordinates")
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    aspect_ratio: float = Field(gt=0.0)
    sha256: str
    status: PanelStatus = PanelStatus.ACTIVE
    ocr_text_en: Optional[str] = None
    notes: Optional[str] = None


class ContactSheetInfo(BaseModel):
    """Contact sheet grid image for Agent multi-panel visual review."""
    model_config = ConfigDict(extra="ignore")

    sheet_id: str
    relative_path: str
    panel_ids: List[str]
    columns: int = 4
    rows: int = 4


class PanelManifest(BaseModel):
    """Schema for data/projects/<project_slug>/chapters/<chapter_num>/manifest.json."""
    model_config = ConfigDict(extra="ignore")

    project_slug: str
    chapter_number: str
    source_url: Optional[str] = None
    created_at: str = Field(default_factory=utc_iso_now)
    total_raw_images: int = Field(default=0, ge=0)
    total_panels: int = Field(default=0, ge=0)
    raw_images: List[RawImageInfo] = Field(default_factory=list)
    panels: List[PanelItem] = Field(default_factory=list)
    contact_sheets: List[ContactSheetInfo] = Field(default_factory=list)
    schema_version: str = "3.0.0"


# ==============================================================================
# 9. Human Review Gate: voice_selection.json
# ==============================================================================

class VoiceSelectionApproval(BaseModel):
    """Schema for Review Gate 1 voice selection approval."""
    model_config = ConfigDict(extra="ignore")

    approved_strategy: str = Field(default="single_narrator", description="Approved strategy (single_narrator, dual_voice, multi_voice)")
    primary_voice: str = Field(..., description="Approved primary narrator voice name")
    speed: float = Field(default=1.0, ge=0.5, le=2.0, description="Approved narration speed")
    reviewer: Optional[str] = Field(default=None, description="Human reviewer identifier")
    approved_by: Optional[str] = Field(default=None, description="Alias for reviewer")
    approved_at: Optional[str] = Field(default=None, description="ISO timestamp of approval")
    notes: Optional[str] = Field(default=None, description="Human reviewer notes")

    def get_reviewer(self) -> str:
        return (self.reviewer or self.approved_by or "").strip()

    def is_approved(self) -> bool:
        return bool(self.get_reviewer() and self.primary_voice.strip())
