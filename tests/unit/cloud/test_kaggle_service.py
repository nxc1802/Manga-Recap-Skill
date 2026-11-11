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


def test_package_chapter_payload(tmp_path, monkeypatch):
    proj_dir = tmp_path / "test_proj"
    chap_dir = proj_dir / "chapters" / "1"
    chap_dir.mkdir(parents=True)
    (proj_dir / "project.json").write_text("{}", encoding="utf-8")
    (chap_dir / "chapter.json").write_text("{}", encoding="utf-8")
    (chap_dir / "script.json").write_text('{"segments": []}', encoding="utf-8")
    (chap_dir / "panels").mkdir()
    (chap_dir / "panels" / "panel_001.jpg").write_bytes(b"dummy")

    monkeypatch.setattr("src.cloud.kaggle_service.get_project_dir", lambda p: proj_dir)
    monkeypatch.setattr("src.cloud.kaggle_service.get_chapter_dir", lambda p, c: chap_dir)

    service = KaggleCloudService(api_token="dummy")
    out_tar = tmp_path / "payload.tar.gz"
    res = service.package_chapter_payload("test_proj", "1", out_tar)

    assert res.exists()
    assert res.stat().st_size > 0


def test_generate_worker_script():
    service = KaggleCloudService(api_token="dummy")
    script_str = service.generate_worker_script("my_proj", "1", render_workers=4)
    assert "Manga Recap Extreme-Speed Cloud Production Worker" in script_str
    assert "my_proj" in script_str
    assert "Vieneu" in script_str
    assert "ThreadPoolExecutor" in script_str


def test_cli_tts_kaggle_dispatch(tmp_path, monkeypatch):
    import argparse
    from src.cli.tts_cmd import handle_tts

    proj_dir = tmp_path / "test_proj"
    chap_dir = proj_dir / "chapters" / "1"
    chap_dir.mkdir(parents=True)
    (proj_dir / "project.json").write_text("{}", encoding="utf-8")
    chap_meta = {
        "chapter_id": "test_proj_ch1",
        "chapter_number": "1",
        "project_slug": "test_proj",
        "title": "Chapter 1",
        "stages": {},
    }
    (chap_dir / "chapter.json").write_text(json.dumps(chap_meta), encoding="utf-8")
    (chap_dir / "script.json").write_text('{"segments": []}', encoding="utf-8")

    monkeypatch.setattr("src.cli.tts_cmd.get_project_dir", lambda p: proj_dir)
    monkeypatch.setattr("src.cli.tts_cmd.get_chapter_dir", lambda p, c: chap_dir)

    mock_service = MagicMock()
    mock_service.is_authenticated = True
    mock_service.dispatch_chapter_job.return_value = {
        "status": "success",
        "final_video": str(chap_dir / "video" / "final.mp4"),
        "subtitles": str(chap_dir / "subtitles" / "chapter_1.srt"),
        "telemetry": {"gpu": "Tesla T4", "tts_seconds": 20.63},
    }

    with patch("src.cloud.kaggle_service.KaggleCloudService", return_value=mock_service):
        args = argparse.Namespace(project="test_proj", chapter="1", kaggle=True)
        res = handle_tts(args)

    assert res["status"] == "success"
    assert res["provider"] == "kaggle"
    assert res["mode"] == "cloud_gpu"
    mock_service.dispatch_chapter_job.assert_called_once_with(
        project_slug="test_proj",
        chapter_number="1",
        gpu=True,
        wait=True,
    )


def test_cli_render_kaggle_dispatch(tmp_path, monkeypatch):
    import argparse
    from src.cli.render_cmd import handle_render

    proj_dir = tmp_path / "test_proj"
    chap_dir = proj_dir / "chapters" / "1"
    chap_dir.mkdir(parents=True)

    monkeypatch.setattr("src.cli.render_cmd.get_project_dir", lambda p: proj_dir)
    monkeypatch.setattr("src.cli.render_cmd.get_chapter_dir", lambda p, c: chap_dir)

    mock_service = MagicMock()
    mock_service.is_authenticated = True
    mock_service.dispatch_chapter_job.return_value = {
        "status": "success",
        "final_video": str(chap_dir / "video" / "final.mp4"),
        "subtitles": str(chap_dir / "subtitles" / "chapter_1.srt"),
        "telemetry": {"gpu": "Tesla T4", "render_seconds": 82.30},
    }

    with patch("src.cloud.kaggle_service.KaggleCloudService", return_value=mock_service):
        args = argparse.Namespace(project="test_proj", chapter="1", kaggle=True)
        res = handle_render(args)

    assert res["status"] == "success"
    assert res["provider"] == "kaggle"
    assert res["mode"] == "cloud_nvenc"
    mock_service.dispatch_chapter_job.assert_called_once_with(
        project_slug="test_proj",
        chapter_number="1",
        gpu=True,
        wait=True,
    )


def test_cli_cloud_subcommands():
    import argparse
    from src.cli.cloud_cmd import handle_cloud

    mock_service = MagicMock()
    mock_service.is_authenticated = True
    mock_service.get_username.return_value = "cuongnguyen1802"
    mock_service.dispatch_chapter_job.return_value = {"status": "success"}

    with patch("src.cli.cloud_cmd.KaggleCloudService", return_value=mock_service), patch("src.core.series_setup.require_series_setup"):
        # 1. info
        info_args = argparse.Namespace(cloud_action="info")
        info_res = handle_cloud(info_args)
        assert info_res["status"] == "success"
        assert info_res["data"]["username"] == "cuongnguyen1802"

        # 2. run
        run_args = argparse.Namespace(
            cloud_action="run",
            project="warrior-grandpa",
            chapter="11",
            no_gpu=False,
        )
        run_res = handle_cloud(run_args)
        assert run_res["status"] == "success"
        mock_service.dispatch_chapter_job.assert_called_once_with(
            project_slug="warrior-grandpa",
            chapter_number="11",
            gpu=True,
            wait=True,
        )


def test_cli_batch_kaggle_dispatch(tmp_path, monkeypatch):
    import argparse
    from src.cli.batch_cmd import handle_batch

    proj_dir = tmp_path / "test_proj"
    chap_dir = proj_dir / "chapters" / "1"
    chap_dir.mkdir(parents=True)
    (proj_dir / "project.json").write_text("{}", encoding="utf-8")
    chap_meta = {
        "chapter_id": "test_proj_ch1",
        "chapter_number": "1",
        "project_slug": "test_proj",
        "title": "Chapter 1",
        "stages": {},
    }
    (chap_dir / "chapter.json").write_text(json.dumps(chap_meta), encoding="utf-8")
    valid_script = {
        "title": "Ch 1",
        "summary": "Summary",
        "segments": [
            {
                "seq_id": "001",
                "panel_ids": ["panel_001.jpg"],
                "narrator_text": "Bối cảnh giang hồ dậy sóng khắp nơi.",
                "duration": 5.0,
            }
        ],
    }
    (chap_dir / "script.json").write_text(json.dumps(valid_script), encoding="utf-8")
    (chap_dir / "panels").mkdir()
    (chap_dir / "panels" / "panel_001.jpg").write_bytes(b"dummy")

    monkeypatch.setattr("src.cli.batch_cmd.get_project_dir", lambda p: proj_dir)
    monkeypatch.setattr("src.cli.batch_cmd.get_chapter_dir", lambda p, c: chap_dir)
    monkeypatch.setattr("src.cli.validate_cmd.handle_validate", lambda args: {"valid": True, "errors": []})

    mock_service = MagicMock()
    mock_service.is_authenticated = True
    mock_service.dispatch_chapter_job.return_value = {
        "status": "success",
        "final_video": str(chap_dir / "video" / "final.mp4"),
        "subtitles": str(chap_dir / "subtitles" / "chapter_1.srt"),
        "telemetry": {"gpu": "Tesla T4", "render_seconds": 82.30},
    }

    with patch("src.cloud.kaggle_service.KaggleCloudService", return_value=mock_service):
        args = argparse.Namespace(
            project="test_proj",
            chapters="1",
            retry_chapter=None,
            skip_preview=True,
            compile=False,
            force=False,
            kaggle=True,
        )
        res = handle_batch(args)

    assert res["success"] is True
    assert len(res["completed"]) == 1
    mock_service.dispatch_chapter_job.assert_called_once_with(
        project_slug="test_proj",
        chapter_number="1",
        gpu=True,
        wait=True,
    )


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

    def test_sync_results_sets_canonical_statuses(self, chapter_fixture):
        """sync_results for produce job must set technical_status=completed and human_review_status=unreviewed."""
        service = KaggleCloudService(api_token="dummy")
        download_dir = chapter_fixture["tmp_path"] / "download_produce"
        download_dir.mkdir()

        # Valid MP4 with ftyp header
        (download_dir / "chapter_1.mp4").write_bytes(b"\x00\x00\x00 ftypisom" + b"X" * 1024)
        (download_dir / "chapter_1.srt").write_text("1\n00:00:00,000 --> 00:00:02,000\nSub\n", encoding="utf-8")
        shutil.copy2(chapter_fixture["chap_dir"] / "script.json", download_dir / "script.json")

        report = service.sync_results(
            project_slug=chapter_fixture["project_slug"],
            chapter_number=chapter_fixture["chapter_number"],
            downloaded_dir=download_dir,
            job_type="produce",
        )
        assert report["status"] == "success"
        assert report["job_type"] == "produce"

        chap_meta = json.loads((chapter_fixture["chap_dir"] / "chapter.json").read_text(encoding="utf-8"))
        assert chap_meta["technical_status"] == "completed"
        assert chap_meta["human_review_status"] == "unreviewed"
        assert chap_meta["overall_status"] == "completed"
        assert chap_meta["stages"]["render"]["status"] == "completed"
        assert chap_meta["stages"]["export"]["status"] == "completed"

    def test_job_separation_tts_packaging_and_worker(self, chapter_fixture):
        """TTS job payload excludes panels and worker contains TTS inference but no video encoding."""
        service = KaggleCloudService(api_token="dummy")
        out_tar = chapter_fixture["tmp_path"] / "payload_tts.tar.gz"

        service.package_chapter_payload(
            chapter_fixture["project_slug"],
            chapter_fixture["chapter_number"],
            out_tar,
            job_type="tts",
        )
        assert out_tar.exists()

        # Verify panels are NOT in tar
        with tarfile.open(out_tar, "r:gz") as tar:
            names = tar.getnames()
            assert any("script.json" in n for n in names)
            assert not any("panels" in n for n in names)

        # Worker script must contain TTS logic but NOT video rendering logic
        worker_code = service.generate_worker_script(
            chapter_fixture["project_slug"],
            chapter_fixture["chapter_number"],
            job_type="tts",
        )
        assert "vieneu" in worker_code
        assert "Vieneu" in worker_code
        assert "infer_batch" in worker_code
        assert "ThreadPoolExecutor" not in worker_code
        assert "compose_single_frame" not in worker_code

    def test_job_separation_tts_sync_without_video(self, chapter_fixture):
        """TTS sync succeeds with audio clips and marks audio stage completed without requiring MP4."""
        service = KaggleCloudService(api_token="dummy")
        download_dir = chapter_fixture["tmp_path"] / "download_tts"
        download_dir.mkdir()

        shutil.copy2(chapter_fixture["chap_dir"] / "script.json", download_dir / "script.json")
        # Pack audio clips into archive
        clips_tar = download_dir / "audio_clips.tar.gz"
        with tarfile.open(clips_tar, "w:gz") as tar:
            tar.add(chapter_fixture["audio_dir"], arcname="clips")

        report = service.sync_results(
            project_slug=chapter_fixture["project_slug"],
            chapter_number=chapter_fixture["chapter_number"],
            downloaded_dir=download_dir,
            job_type="tts",
        )
        assert report["status"] == "success"
        assert report["job_type"] == "tts"

        chap_meta = json.loads((chapter_fixture["chap_dir"] / "chapter.json").read_text(encoding="utf-8"))
        assert chap_meta["stages"]["audio"]["status"] == "completed"
        assert chap_meta["current_stage"] == "audio"
        # Export and overall should not be prematurely completed
        assert chap_meta.get("overall_status") != "completed"

    def test_job_separation_render_has_zero_tts_code(self, chapter_fixture):
        """Render-only worker script contains zero TTS inference code and reuses audio cache."""
        service = KaggleCloudService(api_token="dummy")
        worker_code = service.generate_worker_script(
            chapter_fixture["project_slug"],
            chapter_fixture["chapter_number"],
            job_type="render",
        )
        # CRITICAL CONTRACT: No vieneu in render-only mode
        assert "import vieneu" not in worker_code
        assert "Vieneu" not in worker_code
        assert "pip install vieneu" not in worker_code
        assert "VERIFYING PRE-EXISTING AUDIO CACHE" in worker_code
        assert "ThreadPoolExecutor" in worker_code
        assert "compose_single_frame" in worker_code

    def test_job_separation_render_preflight_fails_when_audio_missing(self, chapter_fixture):
        """Render-only job dispatch fails immediately if audio clips are missing locally."""
        service = KaggleCloudService(api_token="dummy")
        mock_api = MagicMock()
        service._api = mock_api
        service._username = "test_user"

        # Remove one required audio clip
        (chapter_fixture["audio_dir"] / "audio_1_002_1.wav").unlink()

        with pytest.raises(FileNotFoundError) as exc_info:
            service.dispatch_chapter_job(
                project_slug=chapter_fixture["project_slug"],
                chapter_number=chapter_fixture["chapter_number"],
                job_type="render",
            )
        assert "Cannot dispatch render-only job" in str(exc_info.value)
        assert "audio clip(s) missing" in str(exc_info.value)

    def test_option_parity_video_modes_and_subtitles(self, chapter_fixture):
        """Worker script reflects project settings: video_mode, subtitle wrapping, cue sanitization, BGM ducking."""
        service = KaggleCloudService(api_token="dummy")

        worker_code = service.generate_worker_script(
            chapter_fixture["project_slug"],
            chapter_fixture["chapter_number"],
            job_type="render",
            video_mode="portrait_padded",
            burn_subtitles=True,
            fps=24,
            target_width=1280,
            target_height=720,
        )

        assert "1280" in worker_code
        assert "720" in worker_code
        assert "24" in worker_code
        assert "portrait_padded" in worker_code
        assert "detect_video_mode" in worker_code
        assert "landscape_fit" in worker_code
        assert "portrait_vertical" in worker_code
        assert "sanitize_sub_text" in worker_code
        assert "wrap_sub_lines" in worker_code
        assert "sidechaincompress" in worker_code

    def test_option_parity_burn_subtitles_toggle(self, chapter_fixture):
        """When burn_subtitles=False, draw text onto video frame is bypassed."""
        service = KaggleCloudService(api_token="dummy")

        worker_code = service.generate_worker_script(
            chapter_fixture["project_slug"],
            chapter_fixture["chapter_number"],
            job_type="render",
            burn_subtitles=False,
        )
        assert "cfg_burn_subs = bool(render_cfg.get(\"burn_subtitles\", False))" in worker_code

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

