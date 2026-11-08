"""
src/production/__init__.py
Single-session cloud production package for Kaggle and Google Colab environments.
"""

from __future__ import annotations

from src.production.hardware import (
    SUPPORTED_ENCODERS,
    get_encoder_codec_args,
    probe_available_encoders,
    resolve_encoder,
    resolve_video_encoder,
    smoke_test_encoder,
)
from src.production.schemas import (
    ChapterProductionProgress,
    ProductionJobConfig,
    ProductionJobManifest,
    ProductionJobTelemetry,
    ProductionStageStatus,
)
from src.production.runner import SingleSessionProductionRunner

__all__ = [
    "SUPPORTED_ENCODERS",
    "get_encoder_codec_args",
    "probe_available_encoders",
    "resolve_encoder",
    "resolve_video_encoder",
    "smoke_test_encoder",
    "ProductionStageStatus",
    "ProductionJobConfig",
    "ChapterProductionProgress",
    "ProductionJobTelemetry",
    "ProductionJobManifest",
    "SingleSessionProductionRunner",
]
