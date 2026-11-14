"""Versioned production visual contract. Legacy camera enums are not accepted."""
from __future__ import annotations
import json
import math
from enum import Enum
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Region(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    x_min: float = Field(default=0, ge=0, le=1)
    y_min: float = Field(default=0, ge=0, le=1)
    x_max: float = Field(default=1, ge=0, le=1)
    y_max: float = Field(default=1, ge=0, le=1)

    @model_validator(mode="after")
    def ordered(self):
        if self.x_min >= self.x_max or self.y_min >= self.y_max:
            raise ValueError("Region must have positive width and height")
        return self


class ProductionMotion(str, Enum):
    STATIC = "static"
    SLOW_PAN_DOWN = "slow_pan_down"
    STATIC_FOCUS = "static_focus"


class VisualRole(str, Enum):
    SHOW = "show"
    CONTEXT_ONLY = "context_only"
    OMIT_VISUAL = "omit_visual"


class Shot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    shot_id: str = Field(min_length=1)
    segment_id: str = Field(min_length=1)
    source_panel_id: str = Field(min_length=1)
    substitute_panel_id: str | None = None
    source_box: Region = Field(default_factory=Region)
    focus_regions: list[Region] = Field(default_factory=list)
    avoid_regions: list[Region] = Field(default_factory=list)
    visual_role: VisualRole = VisualRole.SHOW
    visual_omit_reason: str | None = None
    substitution_reason: str | None = None
    motion_type: ProductionMotion = ProductionMotion.STATIC
    scroll_fraction_per_second: float = Field(default=0, ge=0, le=0.12)
    duration_sec: float = Field(gt=0)

    @model_validator(mode="after")
    def coherent(self):
        if self.visual_role == VisualRole.CONTEXT_ONLY:
            raise ValueError("Context-only evidence must be substituted before rendering")
        if self.visual_role == VisualRole.OMIT_VISUAL:
            if not self.visual_omit_reason or not self.substitute_panel_id or not self.substitution_reason:
                raise ValueError("Omitted visuals require an evidenced substitute and reason")
        elif self.substitute_panel_id or self.substitution_reason:
            raise ValueError("Substitutes require an omitted source")
        if self.substitute_panel_id == self.source_panel_id:
            raise ValueError("A source cannot substitute itself")
        if self.motion_type != ProductionMotion.SLOW_PAN_DOWN and self.scroll_fraction_per_second:
            raise ValueError("Only downward motion may scroll")
        return self


class VisualPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    schema_version: Literal["4.1.0"] = "4.1.0"
    chapter_number: str = Field(min_length=1)
    input_fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    motion_enabled: bool | None = None
    shots: list[Shot] = Field(min_length=1)
    target_width: int = Field(default=1920, gt=0, multiple_of=2)
    target_height: int = Field(default=1080, gt=0, multiple_of=2)
    fps: int = Field(default=30, ge=1, le=60)
    total_duration_sec: float = Field(gt=0)

    @model_validator(mode="after")
    def timeline(self):
        if len({s.shot_id for s in self.shots}) != len(self.shots):
            raise ValueError("Duplicate shot IDs")
        if not math.isclose(sum(s.duration_sec for s in self.shots), self.total_duration_sec, abs_tol=1e-6):
            raise ValueError("Shot timeline differs from expected duration")
        closed, current = set(), None
        previous = None
        boundary, elapsed = 0, 0.0
        for shot in self.shots:
            elapsed += shot.duration_sec
            next_boundary = round(elapsed * self.fps)
            if next_boundary <= boundary:
                raise ValueError("Shot duration is shorter than one output frame")
            boundary = next_boundary
            if previous and previous.segment_id == shot.segment_id and previous.source_panel_id == shot.source_panel_id:
                if shot.source_box.y_min < previous.source_box.y_min - 1e-9:
                    raise ValueError("Panel decomposition reverses vertically")
            previous = shot
            if current != shot.segment_id:
                if shot.segment_id in closed:
                    raise ValueError("Segment shots must be contiguous")
                if current is not None:
                    closed.add(current)
                current = shot.segment_id
        return self

    def get_shots_for_segment(self, segment_id):
        return [s for s in self.shots if s.segment_id == str(segment_id)]

    def get_shots_for_panel(self, panel_id):
        return [s for s in self.shots if s.source_panel_id == str(panel_id)]

    def save(self, path):
        from src.core.storage import atomic_write_json
        atomic_write_json(Path(path), self)
        return Path(path)

    @classmethod
    def load(cls, path):
        return cls.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))
