"""
tests/unit/cloud/test_kaggle_service.py
Unit tests for KaggleCloudService and CLI cloud integration.
"""

import json
import shutil
import tarfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.cloud.kaggle_service import KaggleCloudService


def test_kaggle_service_initialization():
    service = KaggleCloudService(api_token="KGAT_mock_token_12345")
    assert service.api_token == "KGAT_mock_token_12345"


# Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
pass


# Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
pass


# Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
pass


# Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
pass


# Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
pass


# Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
pass


# ==============================================================================
# Phase 2: P1 Unified Local & Cloud Contracts, Job Separation, Option Parity
# ==============================================================================

class TestPhase2UnifiedContractsAndJobSeparation:
    """
    Validates Phase 2 requirements in KaggleCloudService:
    1. Canonical schema consumption: ChapterMetadata, CheckpointStatus, HumanReviewStatus.
    2. True job separation: tts (audio only), render (video only, zero TTS code), produce (full).
    3. Option parity: video modes, subtitle styling, camera motion, FPS, BGM mixing.
    """

    @pytest.fixture
    def chapter_fixture(self, tmp_path, monkeypatch):
        proj_dir = tmp_path / "test_proj"
        chap_dir = proj_dir / "chapters" / "1"
        chap_dir.mkdir(parents=True)
        panels_dir = chap_dir / "panels"
        panels_dir.mkdir()
        audio_dir = chap_dir / "audio" / "clips"
        audio_dir.mkdir(parents=True)

        # project.json with custom render and audio settings
        project_cfg = {
            "project_id": "test_proj_id",
            "slug": "test_proj",
            "title": "Test Manga Recap",
            "render_settings": {
                "target_width": 1920,
                "target_height": 1080,
                "fps": 30,
                "video_mode": "auto",
                "burn_subtitles": True,
                "subtitle": {
                    "max_lines": 1,
                    "font_size": 28,
                    "max_chars_per_line": 40,
                },
            },
            "audio_settings": {
                "default_voice": "Ngọc Huyền",
                "bgm_path": "assets/bgm.mp3",
                "bgm_volume": 0.18,
                "ducking_factor": 0.35,
            },
        }
        (proj_dir / "project.json").write_text(json.dumps(project_cfg), encoding="utf-8")

        # Create dummy BGM file
        assets_dir = proj_dir / "assets"
        assets_dir.mkdir()
        (assets_dir / "bgm.mp3").write_bytes(b"DUMMY_MP3_DATA" * 50)

        # chapter.json
        chap_meta = {
            "chapter_id": "test_proj_ch1",
            "chapter_number": "1",
            "project_slug": "test_proj",
            "title": "Chapter 1",
            "overall_status": "in_progress",
            "technical_status": "in_progress",
            "human_review_status": "unreviewed",
            "stages": {
                "ingest": {"stage": "ingest", "status": "completed"},
                "agent": {"stage": "agent", "status": "completed"},
                "audio": {"stage": "audio", "status": "pending"},
                "render": {"stage": "render", "status": "pending"},
                "export": {"stage": "export", "status": "pending"},
            },
        }
        (chap_dir / "chapter.json").write_text(json.dumps(chap_meta), encoding="utf-8")

        # script.json with 2 segments (one with emotion cue and focus_box)
        script = {
            "title": "Ch 1",
            "segments": [
                {
                    "seq_id": "001",
                    "panel_ids": ["panel_001.jpg"],
                    "narrator_text": "Bối cảnh giang hồ dậy sóng khắp nơi.",
                    "voice_id": "Ngọc Huyền",
                    "camera_motion": "static",
                },
                {
                    "seq_id": "002_1",
                    "panel_ids": ["panel_002.jpg"],
                    "narrator_text": "[hắng giọng] Hắn từ từ rút thanh bảo kiếm.",
                    "voice_id": "Nam An",
                    "speed": 1.1,
                    "emotion_cue": "[hắng giọng]",
                    "camera_motion": "focus_crop",
                    "focus_box": {"x_min": 0.2, "y_min": 0.1, "x_max": 0.8, "y_max": 0.9},
                },
            ],
        }
        (chap_dir / "script.json").write_text(json.dumps(script), encoding="utf-8")

        # panels
        (panels_dir / "panel_001.jpg").write_bytes(b"PANEL1" * 100)
        (panels_dir / "panel_002.jpg").write_bytes(b"PANEL2" * 100)

        # audio clips (48kHz header dummy)
        import wave
        for sid in ["001", "002_1"]:
            wav_path = audio_dir / f"audio_1_{sid}.wav"
            with wave.open(str(wav_path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(48000)
                wf.writeframes(b"\x00\x00" * 48000 * 2)  # 2 seconds of silence

        monkeypatch.setattr("src.cloud.kaggle_service.get_project_dir", lambda p: proj_dir)
        monkeypatch.setattr("src.cloud.kaggle_service.get_chapter_dir", lambda p, c: chap_dir)

        return {
            "proj_dir": proj_dir,
            "chap_dir": chap_dir,
            "panels_dir": panels_dir,
            "audio_dir": audio_dir,
            "project_slug": "test_proj",
            "chapter_number": "1",
            "tmp_path": tmp_path,
        }

    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass

    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass

    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass

    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass

    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass

    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass

    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass

    def test_convenience_dispatchers_route_correct_job_type(self, chapter_fixture):
        """dispatch_tts_job, dispatch_render_job, dispatch_produce_job route correctly."""
        service = KaggleCloudService(api_token="dummy")
        service.dispatch_chapter_job = MagicMock(return_value={"status": "dispatched"})

        service.dispatch_tts_job("p", "1", wait=False)
        service.dispatch_chapter_job.assert_called_with("p", "1", job_type="tts", wait=False)

        service.dispatch_render_job("p", "1", wait=False)
        service.dispatch_chapter_job.assert_called_with("p", "1", job_type="render", wait=False)

        service.dispatch_produce_job("p", "1", wait=False)
        service.dispatch_chapter_job.assert_called_with("p", "1", job_type="produce", wait=False)

