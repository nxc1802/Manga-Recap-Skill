"""Tests for Phase K0 Kaggle hardware probe, resource sampling, and overlap telemetry."""

import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.cloud.telemetry import (
    ResourceSampler,
    TelemetryRecorder,
    merge_interval_duration,
    probe_worker_hardware,
    query_nvidia_smi,
    sanitize_telemetry_value,
)
from src.production.hardware import smoke_test_encoder


def test_merge_interval_duration_and_overlap_accounting():
    # Two parallel intervals [0, 10] and [4, 14] -> union is 14s, sum is 20s, overlap is 6s
    assert merge_interval_duration([(0.0, 10.0), (4.0, 14.0)]) == 14.0
    assert merge_interval_duration([(0.0, 3.0), (5.0, 9.0)]) == 7.0
    assert merge_interval_duration([]) == 0.0

    rec = TelemetryRecorder(job_id="job-test", project_slug="p1", chapters=["1", "2"])
    t0 = rec.started_mono
    rec.record_stage("tts", t0 + 0.0, t0 + 10.0, chapter_id="1", device_id="gpu:0")
    rec.record_stage("encode", t0 + 4.0, t0 + 14.0, chapter_id="1", device_id="gpu:1")
    rec.record_stage("finalize", t0 + 14.0, t0 + 16.0, chapter_id="1", device_id="cpu")
    summary = rec.compute_stage_summary()

    assert summary["active_union_sec"] == pytest.approx(16.0, abs=0.01)
    assert summary["sum_all_spans_sec"] == pytest.approx(22.0, abs=0.01)
    assert summary["overlap_saved_sec"] == pytest.approx(6.0, abs=0.01)
    assert summary["stages"]["tts"]["wall_union_sec"] == pytest.approx(10.0, abs=0.01)
    assert summary["stages"]["encode"]["wall_union_sec"] == pytest.approx(10.0, abs=0.01)


def test_telemetry_sanitizes_tokens_and_private_text():
    raw = {
        "api_key": "AIzaSecretLeak9999",
        "kaggle_token": "KGAT_secret123",
        "narrator_text": "Private story text",
        "nested": {"safe_note": "using key AIzaAnotherSecret111 and hf_SecretToken222"},
    }
    cleaned = sanitize_telemetry_value(raw)
    assert cleaned["api_key"] == "[REDACTED]"
    assert cleaned["kaggle_token"] == "[REDACTED]"
    assert cleaned["narrator_text"] == "[REDACTED]"
    assert "AIzaAnotherSecret111" not in cleaned["nested"]["safe_note"]
    assert "hf_SecretToken222" not in cleaned["nested"]["safe_note"]


def test_probe_worker_hardware_and_nvidia_smi(tmp_path):
    smi_out = (
        "0, Tesla T4, 15360, 1200, 42, 18\n"
        "1, Tesla T4, 15360, 800, 12, 85\n"
    )
    with patch("src.cloud.telemetry.shutil.which", return_value="/usr/bin/nvidia-smi"), patch(
        "src.cloud.telemetry.subprocess.run",
        return_value=SimpleNamespace(returncode=0, stdout=smi_out, stderr=""),
    ):
        gpus = query_nvidia_smi()
    assert len(gpus) == 2
    assert gpus[1]["nvenc_util_pct"] == 85.0

    # Per-GPU smoke_test_encoder passes -gpu <idx> cleanly
    seen_cmds = []

    def _fake_subproc(cmd, **kwargs):
        seen_cmds.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout="ffmpeg version 7.0", stderr="")

    with patch("src.production.hardware.shutil.which", return_value="/usr/bin/ffmpeg"), patch(
        "src.production.hardware.subprocess.run", side_effect=_fake_subproc
    ):
        assert smoke_test_encoder("h264_nvenc", gpu_index=1) is True
    assert "-gpu" in seen_cmds[0] and "1" in seen_cmds[0]

    with patch("src.cloud.telemetry.query_nvidia_smi", return_value=gpus), patch(
        "src.cloud.telemetry.probe_available_encoders", return_value=["libx264", "h264_nvenc"]
    ), patch("src.cloud.telemetry.smoke_test_encoder", side_effect=lambda enc, gpu_index=None: gpu_index in (0, 1)):
        hw = probe_worker_hardware(tmp_path, probe_nvenc_devices=True)
    assert hw["usable_nvenc_devices"] == [0, 1]
    assert hw["cpu_cores"] >= 1
    assert hw["disk_free_mb"] is not None


def test_resource_sampler_tracks_peaks(tmp_path):
    seq = iter(
        [
            [{"index": 0, "sm_util_pct": 30.0, "nvenc_util_pct": 10.0, "vram_used_mb": 1024.0}],
            [{"index": 0, "sm_util_pct": 88.0, "nvenc_util_pct": 75.0, "vram_used_mb": 4096.0}],
        ]
    )
    sampler = ResourceSampler(
        work_root=tmp_path,
        interval_sec=0.2,
        queue_depth_fn=lambda: {"jobs_qsize": 2},
        gpu_query_fn=lambda: next(seq),
    )
    sampler.sample_once()
    sampler.sample_once()
    summary = sampler.summarize()
    assert summary["sample_count"] == 2
    assert summary["gpu_peaks"]["0"]["peak_sm_util_pct"] == 88.0
    assert summary["gpu_peaks"]["0"]["peak_nvenc_util_pct"] == 75.0
    assert summary["gpu_peaks"]["0"]["peak_vram_used_mb"] == 4096.0
