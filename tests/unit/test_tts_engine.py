"""
tests/unit/test_tts_engine.py
Unit tests for VieNeu-TTS engine, MockTTSBackend, and duration parsing.
"""

from pathlib import Path
import wave
import pytest

from src.tts.engine import (
    EngineMode,
    MockTTSBackend,
    VieNeuEngine,
    get_tts_engine,
    get_wav_duration_seconds,
)


def test_mock_backend_speech_cadence():
    backend = MockTTSBackend()
    # "Chào bạn" = 2 words -> approx 0.8s clamped
    dur1 = backend.estimate_duration("Chào bạn")
    assert dur1 >= 0.8

    # Longer sentence
    text = "Chào mừng các bạn đã quay trở lại với kênh tóm tắt truyện tranh hôm nay."
    dur2 = backend.estimate_duration(text, speed=1.0)
    assert dur2 > 2.0

    # Speed factor test: 2.0x speed should be approximately half duration
    dur_fast = backend.estimate_duration(text, speed=2.0)
    assert round(dur_fast * 2.0, 1) == pytest.approx(round(dur2, 1), abs=0.2)


def test_mock_backend_synthesizes_valid_48khz_wav(tmp_path):
    backend = MockTTSBackend()
    wav_file = tmp_path / "test_utterance.wav"

    dur = backend.synthesize(
        text="Xin chào mọi người đây là bản thử nghiệm âm thanh.",
        voice="Trúc Ly",
        output_path=wav_file,
        speed=1.0,
    )

    assert wav_file.exists()
    assert dur > 0.8

    # Validate binary WAV format
    with wave.open(str(wav_file), "rb") as wf:
        assert wf.getframerate() == 48000, "Audio must be 48 kHz"
        assert wf.getnchannels() == 1, "Audio must be Mono"
        assert wf.getsampwidth() == 2, "Audio must be 16-bit PCM"
        nframes = wf.getnframes()
        expected_dur = round(nframes / float(wf.getframerate()), 4)
        assert round(dur, 3) == pytest.approx(round(expected_dur, 3), abs=0.01)


def test_get_wav_duration_seconds(tmp_path):
    backend = MockTTSBackend()
    wav_file = tmp_path / "duration_test.wav"
    backend.synthesize(text="Kiểm tra thời lượng tệp tin âm thanh.", voice="Hải Đăng", output_path=wav_file)

    measured = get_wav_duration_seconds(wav_file)
    assert measured > 0.5

    with pytest.raises(FileNotFoundError):
        get_wav_duration_seconds(tmp_path / "non_existent.wav")


def test_vieneu_engine_singleton_and_mock_mode():
    engine = get_tts_engine(force_mode=EngineMode.MOCK)
    assert engine.mode == "mock"
    assert isinstance(engine.backend, MockTTSBackend)

    engine2 = get_tts_engine()
    assert engine is engine2
