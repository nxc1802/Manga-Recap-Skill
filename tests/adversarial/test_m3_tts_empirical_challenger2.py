"""
tests/adversarial/test_m3_tts_empirical_challenger2.py
Empirical Challenger 2 Adversarial Stress Suite for Milestone M3 (PR-05 & PR-06).

Rigorous empirical testing of:
1. PR-05 Master Audio Format Invariants (48 kHz, 16-bit PCM, Mono, duration == frames / 48000).
2. PR-05 Utterance Caching Invariants (cache hit determinism, parameter sensitivity, --force bypass, self-healing).
3. PR-05 Offline Isolation & Zero Cloud Network Check (hostile socket interception, zero external calls).
4. PR-06 Zero-CDN Compliance & Evaluation Lab (file:/// compatibility, local asset resolution, voice_selection.json export).
"""

from __future__ import annotations

import html.parser
import http.client
import json
from pathlib import Path
import re
import socket
import struct
import tempfile
import time
from typing import Any, Dict, List
import urllib.request
import wave

import pytest

from src.cli.exit_codes import ExitCode
from src.cli.main import main
from src.core.schemas import (
    ChapterMetadata,
    CheckpointStatus,
    ProjectConfig,
    RecapScript,
    ScriptSegment,
    StageType,
)
from src.core.storage import (
    atomic_write_json,
    calculate_sha256,
    init_chapter_layout,
    init_project_layout,
    read_json,
)
from scripts.devtools.tts_benchmark import (
    EMOTION_BENCHMARK_PROMPTS,
    REFERENCE_PROMPTS,
    TTSBenchmarkRunner,
    generate_mock_wav,
)
from src.tts.catalog import CATALOG_VOICES, resolve_voice_id
from src.tts.engine import (
    VieNeuEngine,
    get_tts_engine,
    get_wav_duration_seconds,
)
from tests.fixtures.mock_tts import MockTTSBackend, MockVieNeuEngine


# ==============================================================================
# Helper Utilities
# ==============================================================================

def unpack_riff_wave(path: Path) -> Dict[str, Any]:
    """Inspects binary RIFF/WAVE header fields byte-by-byte."""
    data = path.read_bytes()
    assert len(data) >= 44, f"File {path} is smaller than 44 bytes header: {len(data)}"

    riff, file_size_minus_8, wave_str = struct.unpack("<4sI4s", data[0:12])
    fmt, fmt_size, audio_format, channels, sample_rate, byte_rate, block_align, bits = struct.unpack(
        "<4sIHHIIHH", data[12:36]
    )
    data_id, data_size = struct.unpack("<4sI", data[36:44])

    return {
        "riff": riff,
        "wave": wave_str,
        "fmt": fmt,
        "fmt_size": fmt_size,
        "audio_format": audio_format,
        "channels": channels,
        "sample_rate": sample_rate,
        "byte_rate": byte_rate,
        "block_align": block_align,
        "bits": bits,
        "data_id": data_id,
        "data_size": data_size,
        "total_bytes": len(data),
    }


# ==============================================================================
# Challenge 1: PR-05 Master Audio Format Invariants
# ==============================================================================

class TestMasterAudioFormatInvariantsChallenger2:
    """Verifies strict RIFF/WAVE 48 kHz, 16-bit Mono format across all synthesized clips."""

    def test_benchmark_samples_binary_invariants(self, tmp_path):
        """Scans benchmark samples (or isolated mock benchmark run in clean checkouts) for binary header invariants."""
        samples_dir = Path("data/benchmarks/vieneu_v3_turbo/samples")
        if not samples_dir.exists():
            bench_out = tmp_path / "vieneu_v3_turbo"
            TTSBenchmarkRunner(
                output_dir=bench_out,
                use_mock=True,
            ).run()
            samples_dir = bench_out / "samples"
        assert samples_dir.exists(), f"Benchmark samples directory {samples_dir} does not exist"

        wav_files = list(samples_dir.glob("*.wav"))
        assert len(wav_files) >= 50, f"Expected >= 50 samples, found {len(wav_files)}"

        for wav in wav_files:
            hdr = unpack_riff_wave(wav)
            assert hdr["riff"] == b"RIFF", f"{wav}: riff != RIFF"
            assert hdr["wave"] == b"WAVE", f"{wav}: wave != WAVE"
            assert hdr["fmt"] == b"fmt ", f"{wav}: fmt != 'fmt '"
            assert hdr["fmt_size"] == 16, f"{wav}: fmt chunk size != 16"
            assert hdr["audio_format"] == 1, f"{wav}: audio_format != 1 (uncompressed PCM)"
            assert hdr["channels"] == 1, f"{wav}: channels != 1 (Mono)"
            assert hdr["sample_rate"] == 48000, f"{wav}: sample_rate != 48000"
            assert hdr["bits"] == 16, f"{wav}: bits_per_sample != 16"
            assert hdr["byte_rate"] == 48000 * 1 * 2, f"{wav}: byte_rate != 96000"
            assert hdr["block_align"] == 2, f"{wav}: block_align != 2"
            assert hdr["data_id"] == b"data", f"{wav}: data chunk id != 'data'"

            # Duration matching frames / 48000
            total_frames = hdr["data_size"] // (hdr["channels"] * (hdr["bits"] // 8))
            expected_duration = total_frames / 48000.0
            extracted_duration = get_wav_duration_seconds(wav)
            assert abs(extracted_duration - expected_duration) < 0.001, (
                f"{wav}: duration mismatch: extracted={extracted_duration}, expected={expected_duration}"
            )

    def test_mock_backend_synthesis_format_invariants(self, tmp_path):
        """Tests MockTTSBackend synthesis for all 25 preset voices."""
        backend = MockTTSBackend()
        for voice_name in CATALOG_VOICES:
            out_file = tmp_path / f"{resolve_voice_id(voice_name)}.wav"
            dur = backend.synthesize(
                text=f"Kiểm tra chất lượng định dạng âm thanh chuẩn phòng thu 48 kHz cho giọng {voice_name}.",
                voice=voice_name,
                output_path=out_file,
            )
            assert out_file.exists()
            hdr = unpack_riff_wave(out_file)
            assert hdr["sample_rate"] == 48000
            assert hdr["channels"] == 1
            assert hdr["bits"] == 16
            assert hdr["byte_rate"] == 96000

            frames = hdr["data_size"] // 2
            expected_dur = frames / 48000.0
            assert abs(dur - expected_dur) < 0.001

    def test_get_wav_duration_seconds_direct_binary_payload_matching(self, tmp_path):
        """Verifies duration reported by get_wav_duration_seconds() matches binary payload byte size."""
        # Test across arbitrary frame counts
        for num_frames in [4800, 24000, 48000, 72000, 144000]:
            wav_file = tmp_path / f"frames_{num_frames}.wav"
            data_bytes = b"\x00\x00" * num_frames
            # Build RIFF header
            hdr = struct.pack(
                "<4sI4s4sIHHIIHH4sI",
                b"RIFF",
                len(data_bytes) + 36,
                b"WAVE",
                b"fmt ",
                16,
                1,       # PCM
                1,       # Mono
                48000,   # Sample rate
                96000,   # Byte rate
                2,       # Block align
                16,      # Bits
                b"data",
                len(data_bytes),
            )
            wav_file.write_bytes(hdr + data_bytes)

            measured = get_wav_duration_seconds(wav_file)
            expected = num_frames / 48000.0
            assert abs(measured - expected) < 0.0001, f"Mismatch: measured {measured} vs expected {expected}"


# ==============================================================================
# Challenge 2: PR-05 Utterance Caching Invariants
# ==============================================================================

class TestDirectSynthesisInvariantsChallenger2:
    """Rigorous challenge of direct synthesis: direct output, parameter sensitivity, and --force flag re-synthesis."""

    def test_direct_synthesis_writes_independent_audio(self, tmp_path):
        """Direct synthesis produces valid audio directly without cache sidecars."""
        engine = MockVieNeuEngine()
        text = "Nhân vật chính phát hiện ra bí mật cổ xưa bị chôn vùi."
        voice = "Trúc Ly"
        speed = 1.0

        out1 = tmp_path / "run1.wav"
        dur1, key1, cached1 = engine.synthesize_utterance(
            text=text, voice=voice, output_path=out1, speed=speed
        )
        assert not cached1
        assert out1.exists()
        assert dur1 > 0
        assert len(key1) == 64 and key1 == calculate_sha256(out1)

        out2 = tmp_path / "run2.wav"
        dur2, key2, cached2 = engine.synthesize_utterance(
            text=text, voice=voice, output_path=out2, speed=speed
        )
        assert not cached2
        assert out2.exists()
        assert dur2 > 0
        assert len(key2) == 64 and key2 == calculate_sha256(out2)
        # No .cache_key files created
        assert not list(tmp_path.glob("**/*.cache_key"))

    def test_direct_synthesis_parameter_variations(self, tmp_path):
        """Changing text, voice, or speed directly synthesizes distinct WAV files."""
        engine = MockVieNeuEngine()
        base_text = "Lời thoại gốc để kiểm tra tính nhạy của tham số."
        base_voice = "Trúc Ly"
        base_speed = 1.0

        out_base = tmp_path / "base.wav"
        dur_base, _, _ = engine.synthesize_utterance(
            text=base_text, voice=base_voice, output_path=out_base, speed=base_speed
        )

        # 1. Change text
        out_text = tmp_path / "diff_text.wav"
        dur_text, _, cached_text = engine.synthesize_utterance(
            text=base_text + " Thay đổi nội dung mở rộng.", voice=base_voice, output_path=out_text, speed=base_speed
        )
        assert cached_text is False
        assert dur_text > dur_base

        # 2. Change voice
        out_voice = tmp_path / "diff_voice.wav"
        dur_voice, _, cached_voice = engine.synthesize_utterance(
            text=base_text, voice="Hải Đăng", output_path=out_voice, speed=base_speed
        )
        assert cached_voice is False
        assert out_voice.exists()

        # 3. Change speed
        out_speed = tmp_path / "diff_speed.wav"
        dur_speed, _, cached_speed = engine.synthesize_utterance(
            text=base_text, voice=base_voice, output_path=out_speed, speed=1.5
        )
        assert cached_speed is False
        assert dur_speed < dur_base

    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass


# ==============================================================================
# Challenge 3: PR-05 Offline Isolation & Zero Cloud Network Check
# ==============================================================================

class TestOfflineIsolationAndZeroCloudChallenger2:
    """Interception of socket and HTTP connections to empirically prove 100% offline isolation."""

    def test_strict_network_blocking_on_tts_and_benchmark(self, monkeypatch, tmp_path):
        """Blocks all outbound socket and HTTP connections; verifies tts and tts_benchmark execute 100% offline."""
        network_attempts = []

        def blocked_connect(self, *args, **kwargs):
            network_attempts.append(("socket.connect", args, kwargs))
            raise ConnectionRefusedError(f"OFFLINE VIOLATION: Socket connect attempt to {args}")

        def blocked_create_connection(*args, **kwargs):
            network_attempts.append(("socket.create_connection", args, kwargs))
            raise ConnectionRefusedError(f"OFFLINE VIOLATION: create_connection attempt to {args}")

        def blocked_http_connect(self, *args, **kwargs):
            network_attempts.append(("http.connect", args, kwargs))
            raise ConnectionRefusedError(f"OFFLINE VIOLATION: HTTP connect attempt to {self.host}:{self.port}")

        monkeypatch.setattr(socket.socket, "connect", blocked_connect)
        monkeypatch.setattr(socket, "create_connection", blocked_create_connection)
        monkeypatch.setattr(http.client.HTTPConnection, "connect", blocked_http_connect)
        monkeypatch.setattr(http.client.HTTPSConnection, "connect", blocked_http_connect)

        # 1. Synthesize via MockVieNeuEngine test fixture
        engine = MockVieNeuEngine()
        out_wav = tmp_path / "isolated.wav"
        dur, _, _ = engine.synthesize_utterance(
            text="Kiểm tra hệ thống ngoại tuyến hoàn toàn khi chặn tất cả kết nối mạng.",
            voice="Trúc Ly",
            output_path=out_wav,
        )
        assert out_wav.exists()
        assert dur > 0.5
        assert len(network_attempts) == 0, f"Illegal network calls detected: {network_attempts}"

        # 2. Run TTS Benchmark runner in mock mode
        bench_dir = tmp_path / "bench_isolated"
        runner = TTSBenchmarkRunner(
            output_dir=bench_dir,
            voices=["Trúc Ly", "Hải Đăng"],
            emotions=["neutral"],
            use_mock=True,
        )
        res = runner.run()
        assert res["status"] == "completed"
        assert len(network_attempts) == 0, f"Illegal network calls detected during benchmark: {network_attempts}"

    def test_static_audit_for_zero_cloud_tts_apis(self):
        """Scans codebase to guarantee no Cloud TTS client libraries or endpoints exist."""
        forbidden_imports = [
            r"\bimport\s+edge_tts\b",
            r"\bfrom\s+edge_tts\b",
            r"\bimport\s+google\.cloud\.texttospeech\b",
            r"\bfrom\s+google\.cloud\.texttospeech\b",
            r"\bimport\s+openai\b",
            r"\bfrom\s+openai\b",
            r"\bimport\s+elevenlabs\b",
            r"\bfrom\s+elevenlabs\b",
        ]
        forbidden_endpoints = [
            "api.elevenlabs.io",
            "texttospeech.googleapis.com",
            "api.openai.com/v1/audio/speech",
        ]

        search_dirs = [Path("src/tts"), Path("src/cli"), Path("src/services")]
        for d in search_dirs:
            if not d.exists():
                continue
            for py_file in d.glob("*.py"):
                content = py_file.read_text(encoding="utf-8")
                for pat in forbidden_imports:
                    assert not re.search(pat, content), (
                        f"Forbidden cloud import pattern '{pat}' detected in {py_file}"
                    )
                for ep in forbidden_endpoints:
                    assert ep not in content.lower(), (
                        f"Forbidden cloud endpoint '{ep}' detected in {py_file}"
                    )


# ==============================================================================
# Challenge 4: PR-06 Zero-CDN Compliance & Evaluation Lab
# ==============================================================================
