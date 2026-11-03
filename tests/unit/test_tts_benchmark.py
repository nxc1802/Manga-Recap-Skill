"""
tests/unit/test_tts_benchmark.py
Unit tests for TTS Voice & Emotion Benchmark Runner and artifacts.
"""

import json
from pathlib import Path
import wave
import pytest

from src.cli.main import main
from src.tts.benchmark import (
    TTSBenchmarkRunner,
    compute_waveform_peaks,
    generate_mock_wav,
)


def test_generate_mock_wav_and_peaks(tmp_path):
    wav_path = tmp_path / "sample.wav"
    dur, peaks = generate_mock_wav(wav_path, duration=2.5, sample_rate=48000, freq=300.0)

    assert wav_path.exists()
    assert dur == 2.5
    assert len(peaks) == 64
    assert all(0.0 <= p <= 1.0 for p in peaks)

    with wave.open(str(wav_path), "rb") as wf:
        assert wf.getframerate() == 48000
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2


def test_compute_waveform_peaks(tmp_path):
    wav_path = tmp_path / "peaks_test.wav"
    generate_mock_wav(wav_path, duration=2.0)

    extracted_peaks = compute_waveform_peaks(wav_path, num_peaks=64)
    assert len(extracted_peaks) == 64
    assert max(extracted_peaks) > 0.0


def test_benchmark_runner_mock_execution(tmp_path):
    out_dir = tmp_path / "bench_out"
    runner = TTSBenchmarkRunner(
        output_dir=out_dir,
        voices=["Trúc Ly", "Hải Đăng"],
        emotions=["neutral", "excited"],
        use_mock=True,
        copy_to_lab=False,
    )

    res = runner.run()

    assert res["status"] == "completed"
    assert res["voices_screened_count"] == 2
    assert res["emotions_tested_count"] == 2
    assert Path(res["report_json"]).exists()
    assert Path(res["summary_md"]).exists()

    # Verify report JSON content
    with open(res["report_json"], "r", encoding="utf-8") as f:
        data = json.load(f)
        assert data["engine"] == "VieNeu-TTS v3 Turbo"
        assert len(data["samples"]) > 0
        sample = data["samples"][0]
        assert sample["sample_rate"] == 48000
        assert "peaks" in sample
        assert len(sample["peaks"]) == 64


def test_tts_benchmark_cli_with_mock(tmp_path, capsys):
    out_dir = tmp_path / "cli_bench"
    exit_code = main([
        "tts_benchmark",
        "--voices", "Trúc Ly,Adam bựa",
        "--emotions", "neutral",
        "--out-dir", str(out_dir),
        "--mock",
        "--no-copy-to-lab",
        "--json",
    ])
    assert exit_code == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["status"] == "success"
    assert payload["command"] == "tts_benchmark"
    assert payload["data"]["total_voices_tested"] == 2
    assert payload["data"]["voices"][0] == "Trúc Ly"
    assert Path(payload["data"]["report_json"]).exists()
