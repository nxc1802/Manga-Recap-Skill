"""
tests/unit/test_kaggle_dual_t4.py
Comprehensive unit tests for Kaggle Dual-T4 Single-Session Pipeline Engine (PR-03).

Test Coverage:
1. Kernel metadata "machine_shape": "NvidiaTeslaT4" with "enable_gpu": True.
2. Multi-chapter payload packaging (package_series_payload) and single-session unpack (sync_series_results).
3. Chapter 1 preview gate enforcement (unreviewed rejected, approved accepted, --skip-preview bypass).
4. Process 0 / Process 1 environment variable mapping (GPU 0 TTS vs GPU 1 NVENC) and device isolation.
5. Pipelined audio-ready event trigger (ch_{N}_audio_ready.json) and overlap simulation.
6. OOM backoff logic (batch halving on OutOfMemoryError) and telemetry metric aggregation.
7. CLI command parsing and execution for 'cloud produce-series'.
"""

import argparse
import json
import shutil
import tarfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.cli.cloud_cmd import handle_cloud
from src.cli.exit_codes import ExitCode
from src.cloud.dual_t4_orchestrator import (
    DualT4Orchestrator,
    GpuDeviceTelemetry,
    MicroBatchPlanner,
    RenderWorker,
    SeriesTelemetry,
    TTSWorker,
)
from src.cloud.kaggle_service import KaggleCloudService
from src.core.schemas import HumanReviewStatus


@pytest.fixture
def dummy_project(tmp_path):
    """Sets up a realistic multi-chapter project directory."""
    proj_dir = tmp_path / "test_series"
    proj_dir.mkdir(parents=True)

    proj_cfg = {
        "project_slug": "test_series",
        "title": "Test Series V4",
        "chapters": ["1", "2", "3"],
        "setup_revision": 1,
        "render_settings": {
            "target_width": 1920,
            "target_height": 1080,
            "fps": 30,
            "burn_subtitles": False,
        },
        "audio_settings": {
            "default_voice": "Ngọc Huyền",
            "voice_volume": 1.0,
        },
    }
    (proj_dir / "project.json").write_text(json.dumps(proj_cfg), encoding="utf-8")
    (proj_dir / "characters.json").write_text('{"characters": []}', encoding="utf-8")
    (proj_dir / "story_summary.md").write_text("# Story Summary\nLore.", encoding="utf-8")

    # Setup chapters 1, 2, 3
    for ch_num in ["1", "2", "3"]:
        chap_dir = proj_dir / "chapters" / ch_num
        chap_dir.mkdir(parents=True)

        chap_meta = {
            "chapter_id": f"test_series_ch{ch_num}",
            "chapter_number": ch_num,
            "project_slug": "test_series",
            "title": f"Chapter {ch_num}",
            "technical_status": "in_progress",
            "human_review_status": "unreviewed" if ch_num == "1" else "unreviewed",
            "stages": {},
        }
        (chap_dir / "chapter.json").write_text(json.dumps(chap_meta), encoding="utf-8")

        script_data = {
            "chapter_number": ch_num,
            "segments": [
                {
                    "seq_id": "001",
                    "panel_id": "panel_001",
                    "voice_id": "Ngọc Huyền",
                    "text": f"Đây là phân đoạn mở đầu chương {ch_num}.",
                },
                {
                    "seq_id": "002",
                    "panel_id": "panel_002",
                    "voice_id": "Ngọc Huyền",
                    "text": f"Nhân vật chính xuất hiện trong chương {ch_num}.",
                },
            ],
            "total_duration_seconds": 6.0,
        }
        (chap_dir / "script.json").write_text(json.dumps(script_data), encoding="utf-8")
        (chap_dir / "manifest.json").write_text('{"panels": []}', encoding="utf-8")

        panels_dir = chap_dir / "panels"
        panels_dir.mkdir()
        (panels_dir / "panel_001.jpg").write_bytes(b"\xff\xd8\xff\xe0dummy_jpg")
        (panels_dir / "panel_002.jpg").write_bytes(b"\xff\xd8\xff\xe0dummy_jpg")

    return proj_dir


# ==============================================================================
# 1. Kernel Metadata: machine_shape="NvidiaTeslaT4"
# ==============================================================================


def test_kernel_metadata_machine_shape_dispatch_chapter(tmp_path, monkeypatch, dummy_project):
    """Verifies dispatch_chapter_job writes machine_shape='NvidiaTeslaT4' when gpu=True."""
    monkeypatch.setattr("src.cloud.kaggle_service.get_project_dir", lambda p: dummy_project)
    monkeypatch.setattr("src.cloud.kaggle_service.get_chapter_dir", lambda p, c: dummy_project / "chapters" / c)
    monkeypatch.setattr("src.cloud.kaggle_service.time.sleep", lambda s: None)

    service = KaggleCloudService(api_token="dummy")
    mock_api = MagicMock()
    mock_api.kernels_push.return_value = MagicMock(ref="cuongnguyen1802/test-kernel")

    ds_mock = MagicMock()
    ds_mock.ref = "cuongnguyen1802/manga-recap-payload"
    ds_mock.current_version_number = 10
    mock_api.dataset_list.return_value = [ds_mock]
    mock_api.dataset_status.return_value = "ready"

    f1 = MagicMock()
    f1.name = "script.json"
    mock_api.dataset_list_files.return_value.dataset_files = [f1]

    service._api = mock_api
    service._username = "cuongnguyen1802"

    res = service.dispatch_chapter_job(
        project_slug="test_series",
        chapter_number="1",
        gpu=True,
        wait=False,
    )
    assert res["status"] == "dispatched"

    staging_dir = Path(res["staging_dir"])
    metadata_file = staging_dir / "kernel-metadata.json"
    assert metadata_file.exists()

    meta = json.loads(metadata_file.read_text(encoding="utf-8"))
    assert meta["enable_gpu"] is True
    assert meta["machine_shape"] == "NvidiaTeslaT4"


def test_kernel_metadata_machine_shape_dispatch_series(tmp_path, monkeypatch, dummy_project):
    """Verifies dispatch_series_job writes machine_shape='NvidiaTeslaT4' when gpu=True."""
    monkeypatch.setattr("src.cloud.kaggle_service.get_project_dir", lambda p: dummy_project)
    monkeypatch.setattr("src.cloud.kaggle_service.get_chapter_dir", lambda p, c: dummy_project / "chapters" / c)
    monkeypatch.setattr("src.cloud.kaggle_service.time.sleep", lambda s: None)

    service = KaggleCloudService(api_token="dummy")
    mock_api = MagicMock()
    mock_api.kernels_push.return_value = MagicMock(ref="cuongnguyen1802/series-kernel")

    ds_mock = MagicMock()
    ds_mock.ref = "cuongnguyen1802/mr-test-series-payload"
    ds_mock.current_version_number = 10
    mock_api.dataset_list.return_value = [ds_mock]
    mock_api.dataset_status.return_value = "ready"

    f1 = MagicMock()
    f1.name = "script.json"
    mock_api.dataset_list_files.return_value.dataset_files = [f1]

    service._api = mock_api
    service._username = "cuongnguyen1802"

    res = service.dispatch_series_job(
        project_slug="test_series",
        chapters=["1", "2"],
        gpu=True,
        wait=False,
        skip_preview=True,
    )
    assert res["status"] == "dispatched"

    staging_dir = Path(res["staging_dir"])
    metadata_file = staging_dir / "kernel-metadata.json"
    assert metadata_file.exists()

    meta = json.loads(metadata_file.read_text(encoding="utf-8"))
    assert meta["enable_gpu"] is True
    assert meta["machine_shape"] == "NvidiaTeslaT4"


# ==============================================================================
# 2. Multi-Chapter Payload Packaging & Single-Session Unpack
# ==============================================================================


def test_package_series_payload_and_sync_results(tmp_path, monkeypatch, dummy_project):
    """
    Tests that package_series_payload creates a single archive containing all target chapters,
    and sync_series_results extracts and validates them into respective chapter directories.
    """
    monkeypatch.setattr("src.cloud.kaggle_service.get_project_dir", lambda p: dummy_project)
    monkeypatch.setattr("src.cloud.kaggle_service.get_chapter_dir", lambda p, c: dummy_project / "chapters" / c)
    monkeypatch.setattr("src.core.storage.get_project_dir", lambda p: dummy_project)
    monkeypatch.setattr("src.core.storage.get_chapter_dir", lambda p, c: dummy_project / "chapters" / c)
    monkeypatch.setattr("src.core.storage.get_output_dir", lambda p: tmp_path / "output")

    service = KaggleCloudService(api_token="dummy")
    payload_tar = tmp_path / "series_payload.tar.gz"

    # 1. Package
    res_path = service.package_series_payload("test_series", ["1", "2", "3"], payload_tar)
    assert res_path.exists()
    assert res_path.stat().st_size > 0

    with tarfile.open(res_path, "r:gz") as tar:
        names = tar.getnames()
        assert "project.json" in names
        assert "chapters/1/chapter.json" in names
        assert "chapters/2/chapter.json" in names
        assert "chapters/3/chapter.json" in names
        assert "chapters/1/panels/panel_001.jpg" in names

    # 2. Mock Kaggle results bundle
    download_dir = tmp_path / "mock_kaggle_download"
    download_dir.mkdir()
    results_tar = download_dir / "series_results.tar.gz"

    valid_mp4_bytes = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 100
    bundle_staging = tmp_path / "bundle_staging"
    bundle_staging.mkdir()

    for ch in ["1", "2", "3"]:
        c_dir = bundle_staging / "chapters" / ch
        (c_dir / "video").mkdir(parents=True)
        (c_dir / "subtitles").mkdir(parents=True)
        (c_dir / "audio" / "clips").mkdir(parents=True)

        (c_dir / "video" / "final.mp4").write_bytes(valid_mp4_bytes)
        (c_dir / "subtitles" / f"chapter_{ch}.srt").write_text("1\n00:00:00,000 --> 00:00:02,000\nTest\n", encoding="utf-8")
        (c_dir / "audio" / "speech_master.wav").write_bytes(b"RIFF" + b"\x00" * 44)
        (c_dir / "audio" / "clips" / f"audio_{ch}_001.wav").write_bytes(b"RIFF" + b"\x00" * 44)

    # Feature video
    (bundle_staging / "outputs").mkdir(parents=True)
    (bundle_staging / "outputs" / "full_recap_feature.mp4").write_bytes(valid_mp4_bytes)

    # Telemetry
    telemetry_data = {
        "pipeline": {"speedup_factor": 2.53, "total_wall_clock_seconds": 45.2},
        "gpu_0": {"peak_vram_mb": 4120.0},
        "gpu_1": {"peak_vram_mb": 1850.0},
    }
    (bundle_staging / "series_telemetry.json").write_text(json.dumps(telemetry_data), encoding="utf-8")

    with tarfile.open(results_tar, "w:gz") as tar:
        tar.add(bundle_staging, arcname="")

    # 3. Synchronize
    sync_res = service.sync_series_results("test_series", ["1", "2", "3"], download_dir)

    assert sync_res["status"] == "success"
    assert sync_res["chapters"] == ["1", "2", "3"]
    assert "1" in sync_res["chapter_results"]
    assert "2" in sync_res["chapter_results"]
    assert "3" in sync_res["chapter_results"]

    # Verify local disk artifacts
    for ch in ["1", "2", "3"]:
        assert (dummy_project / "chapters" / ch / "video" / "final.mp4").exists()
        assert (dummy_project / "chapters" / ch / "subtitles" / f"chapter_{ch}.srt").exists()
        meta = json.loads((dummy_project / "chapters" / ch / "chapter.json").read_text(encoding="utf-8"))
        assert meta["overall_status"] == "completed"


# ==============================================================================
# 3. Chapter 1 Preview Gate Enforcement
# ==============================================================================


def test_chapter_1_preview_gate_rejection_and_approval(monkeypatch, dummy_project):
    """
    Verifies Chapter 1 preview gate:
    - Rejects series job when Chapter 1 human_review_status != 'approved'
    - Allows series job when Chapter 1 is approved
    - Allows series job when skip_preview=True
    """
    monkeypatch.setattr("src.cloud.kaggle_service.get_project_dir", lambda p: dummy_project)
    monkeypatch.setattr("src.cloud.kaggle_service.get_chapter_dir", lambda p, c: dummy_project / "chapters" / c)

    service = KaggleCloudService(api_token="dummy")

    # 1. Unreviewed -> Rejected
    ok, err = service.check_chapter_1_preview_approval("test_series", skip_preview=False)
    assert ok is False
    assert "preview has not been approved" in err

    # 2. skip_preview=True -> Allowed
    ok_skip, err_skip = service.check_chapter_1_preview_approval("test_series", skip_preview=True)
    assert ok_skip is True
    assert err_skip is None

    # 3. Approve Chapter 1 -> Allowed
    c1_json = dummy_project / "chapters" / "1" / "chapter.json"
    meta = json.loads(c1_json.read_text(encoding="utf-8"))
    meta["human_review_status"] = "approved"
    c1_json.write_text(json.dumps(meta), encoding="utf-8")

    ok_appr, err_appr = service.check_chapter_1_preview_approval("test_series", skip_preview=False)
    assert ok_appr is True
    assert err_appr is None


def test_dispatch_series_job_preview_gate_raises_when_unreviewed(monkeypatch, dummy_project):
    """dispatch_series_job must raise ValueError if Chapter 1 is unreviewed and skip_preview is False."""
    monkeypatch.setattr("src.cloud.kaggle_service.get_project_dir", lambda p: dummy_project)
    monkeypatch.setattr("src.cloud.kaggle_service.get_chapter_dir", lambda p, c: dummy_project / "chapters" / c)

    service = KaggleCloudService(api_token="dummy")
    service._api = MagicMock()

    with pytest.raises(ValueError, match="preview has not been approved"):
        service.dispatch_series_job("test_series", ["1", "2"], skip_preview=False)


# ==============================================================================
# 4. Process 0 / Process 1 Environment Mapping & Device Isolation
# ==============================================================================


def test_tts_worker_and_render_worker_device_isolation(tmp_path):
    """
    Verifies that TTSWorker binds to GPU 0 and RenderWorker binds to GPU 1,
    with distinct telemetry structures and zero cross-device contamination.
    """
    ws = tmp_path / "test_workspace"
    ws.mkdir()

    tts_worker = TTSWorker(workspace_dir=ws, gpu_id=0, mock_mode=True)
    render_worker = RenderWorker(workspace_dir=ws, gpu_id=1, mock_mode=True)

    assert tts_worker.gpu_id == 0
    assert tts_worker.telemetry.device_id == 0
    assert "VieNeu" in tts_worker.telemetry.assigned_task

    assert render_worker.gpu_id == 1
    assert render_worker.telemetry.device_id == 1
    assert "NVENC" in render_worker.telemetry.assigned_task

    assert tts_worker.telemetry.device_id != render_worker.telemetry.device_id


# ==============================================================================
# 5. Pipelined Audio-Ready Sentinel Trigger & Overlap Simulation
# ==============================================================================


def test_pipelined_sentinel_and_overlap_orchestrator(tmp_path, dummy_project):
    """
    Tests that TTSWorker emits ch_{N}_audio_ready.json, RenderWorker polls and consumes it,
    and DualT4Orchestrator tracks pipelined overlap and speedup metrics.
    """
    orchestrator = DualT4Orchestrator(
        workspace_dir=dummy_project,
        chapters=["1", "2"],
        compile_feature=True,
        mock_mode=True,
    )

    series_tel = orchestrator.run_pipeline()

    assert isinstance(series_tel, SeriesTelemetry)
    assert series_tel.chapters == ["1", "2"]
    assert series_tel.speedup_factor > 1.0
    assert series_tel.pipeline_overlap_seconds > 0.0
    assert series_tel.total_wall_clock_seconds > 0.0
    assert series_tel.gpu_0 is not None
    assert series_tel.gpu_1 is not None

    # Sentinel files created
    assert (dummy_project / "chapters" / "1" / "ch_1_audio_ready.json").exists()
    assert (dummy_project / "chapters" / "2" / "ch_2_audio_ready.json").exists()
    assert (dummy_project / "chapters" / "1" / "ch_1_render_complete.json").exists()
    assert (dummy_project / "chapters" / "2" / "ch_2_render_complete.json").exists()

    # Telemetry JSON exists
    tel_file = dummy_project / "series_telemetry.json"
    assert tel_file.exists()
    tel_data = json.loads(tel_file.read_text(encoding="utf-8"))
    assert tel_data["speedup_factor"] > 1.0


# ==============================================================================
# 6. Dynamic OOM Backoff & Micro-Batching
# ==============================================================================


def test_micro_batch_bucketing():
    """Verifies segment bucketing by character length."""
    assert MicroBatchPlanner.get_length_bucket("Ngắn") == 0
    assert MicroBatchPlanner.get_length_bucket("A" * 70) == 1
    assert MicroBatchPlanner.get_length_bucket("B" * 150) == 2
    assert MicroBatchPlanner.get_length_bucket("C" * 300) == 3

    segments = [
        {"seq_id": "1", "voice_id": "v1", "text": "Short"},
        {"seq_id": "2", "voice_id": "v1", "text": "Medium " * 15},
        {"seq_id": "3", "voice_id": "v2", "text": "Voice 2 text"},
    ]
    grouped = MicroBatchPlanner.bucket_segments(segments)
    assert "v1" in grouped
    assert "v2" in grouped


def test_dynamic_oom_backoff_halving_and_completion():
    """
    Simulates an OutOfMemoryError when batch size > 2.
    Verifies that MicroBatchPlanner catches OOM, halves batch size, retries,
    completes all items without dropping any, and records OOM events.
    """
    call_batch_sizes = []
    oom_tracker = {"count": 0}

    def mock_infer_func(texts: list, voice: str):
        call_batch_sizes.append(len(texts))
        if len(texts) > 2:
            # Simulate CUDA OOM
            raise RuntimeError("CUDA out of memory. Tried to allocate 2.00 GiB")
        # Success for batch size <= 2
        return [f"audio_{t[:5]}" for t in texts]

    batch = [{"text": f"Segment text {i}"} for i in range(8)]
    results = MicroBatchPlanner.execute_batch_with_oom_backoff(
        batch=batch,
        infer_func=mock_infer_func,
        voice="test_voice",
        oom_tracker=oom_tracker,
    )

    # All 8 items must be successfully synthesized
    assert len(results) == 8
    # OOM was caught and triggered backoff
    assert oom_tracker["count"] > 0
    # At least one smaller batch size was executed
    assert any(bs <= 2 for bs in call_batch_sizes)


# ==============================================================================
# 7. CLI Command: cloud produce-series
# ==============================================================================


def test_cli_cloud_produce_series(dummy_project, monkeypatch):
    """Verifies CLI execution of 'manga-recap cloud produce-series'."""
    monkeypatch.setattr("src.core.storage.get_project_dir", lambda p: dummy_project)
    monkeypatch.setattr("src.core.utils.get_project_data_dir", lambda p: dummy_project)

    mock_service = MagicMock()
    mock_service.is_authenticated = True
    mock_service.dispatch_series_job.return_value = {
        "status": "success",
        "project_slug": "test_series",
        "chapters": ["1", "2"],
        "telemetry": {"speedup_factor": 2.53},
    }

    with patch("src.cli.cloud_cmd.KaggleCloudService", return_value=mock_service), \
         patch("src.core.series_setup.require_series_setup", return_value=MagicMock()):
        args = argparse.Namespace(
            cloud_action="produce-series",
            project="test_series",
            chapters="1,2",
            skip_preview=True,
            compile=True,
            no_gpu=False,
        )
        res = handle_cloud(args)

    assert res["status"] == "success"
    assert res["exit_code"] == ExitCode.SUCCESS
    mock_service.dispatch_series_job.assert_called_once()
