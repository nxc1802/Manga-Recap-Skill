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


def test_vieneu_engine_force_neural_precedence_over_mock_env(monkeypatch):
    monkeypatch.setenv("VIENEU_MOCK", "1")
    # Even if VIENEU_MOCK=1 is in environment, force_mode=EngineMode.NEURAL MUST attempt neural
    # and fail loudly rather than silently returning MockTTSBackend.
    import src.tts.engine as eng

    def failing_neural_init(self):
        raise ImportError("Simulated missing vieneu package")

    monkeypatch.setattr(eng.NeuralTTSBackend, "__init__", failing_neural_init)

    with pytest.raises(RuntimeError, match="Neural TTS backend requested but failed to initialize"):
        get_tts_engine(force_mode=EngineMode.NEURAL)


def test_synthesize_utterance_atomic_write_and_sha256(tmp_path):
    from src.core.storage import calculate_sha256
    engine = VieNeuEngine(force_mode=EngineMode.MOCK)
    dest = tmp_path / "atomic_clip.wav"

    dur, checksum, was_cached = engine.synthesize_utterance(
        text="Kiểm tra ghi tệp an toàn và trả về mã băm SHA-256.",
        voice="Trúc Ly",
        output_path=dest,
    )

    assert dest.exists()
    assert dur > 0.5
    assert was_cached is False
    assert len(checksum) == 64
    assert checksum == calculate_sha256(dest)
    # Ensure no temporary files remained
    tmp_files = list(tmp_path.glob(".*.tmp.*"))
    assert len(tmp_files) == 0


def test_synthesize_utterance_atomic_failure_resilience(tmp_path, monkeypatch):
    engine = VieNeuEngine(force_mode=EngineMode.MOCK)
    dest = tmp_path / "original_clip.wav"

    # Create original valid file
    dur1, checksum1, _ = engine.synthesize_utterance(
        text="Nội dung ban đầu nguyên vẹn.",
        voice="Trúc Ly",
        output_path=dest,
    )
    original_bytes = dest.read_bytes()

    # Simulate crash/failure inside backend.synthesize
    def failing_synthesize(*args, **kwargs):
        raise IOError("Simulated sudden I/O or power failure during inference")

    monkeypatch.setattr(engine.backend, "synthesize", failing_synthesize)

    with pytest.raises(IOError, match="Simulated sudden I/O"):
        engine.synthesize_utterance(
            text="Nội dung mới bị lỗi giữa chừng.",
            voice="Trúc Ly",
            output_path=dest,
        )

    # Destination file must be completely untouched and uncorrupted
    assert dest.exists()
    assert dest.read_bytes() == original_bytes
    # No dangling temporary files
    tmp_files = list(tmp_path.glob(".*.tmp.*"))
    assert len(tmp_files) == 0


def test_vieneu_engine_synthesize_batch(tmp_path):
    """Verifies synthesize_batch processes multiple utterances with accurate durations and checksums."""
    engine = VieNeuEngine(force_mode=EngineMode.MOCK)
    tasks = [
        {
            "text": "Câu thứ nhất trong loạt xử lý hàng loạt.",
            "voice": "Trúc Ly",
            "output_path": tmp_path / "batch_001.wav",
            "speed": 1.0,
        },
        {
            "text": "Câu thứ hai với tốc độ nhanh hơn một chút.",
            "voice": "Hải Đăng",
            "output_path": tmp_path / "batch_002.wav",
            "speed": 1.15,
        },
        {
            "text": "Câu thứ ba với âm hưởng buồn và tiếng thở dài.",
            "voice": "Trúc Ly",
            "output_path": tmp_path / "batch_003.wav",
            "speed": 0.95,
            "emotion": "sad",
            "emotion_cue": "[thở dài]",
        },
    ]

    results = engine.synthesize_batch(tasks, workers=2)
    assert len(results) == 3

    for i, (dur, checksum, was_cached) in enumerate(results):
        out_file = tasks[i]["output_path"]
        assert out_file.exists()
        assert dur > 0.5
        assert len(checksum) == 64
        assert was_cached is False
        with wave.open(str(out_file), "rb") as wf:
            assert wf.getframerate() == 48000
            assert wf.getnchannels() == 1
            assert wf.getsampwidth() == 2


def test_in_memory_wav_encoding_and_caching(tmp_path):
    """Verifies direct in-memory 16-bit PCM WAV encoding and metric cache lookups."""
    from src.tts.engine import _encode_wav_16bit_pcm, _cache_audio_metrics, _get_cached_audio_metrics
    import numpy as np

    # Synthetic 1.0 second sine wave at 48 kHz
    t = np.linspace(0, 1.0, 48000, endpoint=False, dtype=np.float32)
    audio = 0.5 * np.sin(2 * np.pi * 440 * t)

    wav_bytes, dur, csum = _encode_wav_16bit_pcm(audio, sample_rate=48000)
    assert len(wav_bytes) == 44 + 48000 * 2
    assert dur == 1.0
    assert len(csum) == 64

    test_wav = tmp_path / "in_memory.wav"
    test_wav.write_bytes(wav_bytes)

    # Check that get_wav_duration_seconds correctly parses it
    assert get_wav_duration_seconds(test_wav) == 1.0

    # Cache metrics and verify cache retrieval
    _cache_audio_metrics(test_wav, dur, csum)
    cached = _get_cached_audio_metrics(test_wav)
    assert cached is not None
    assert cached[0] == 1.0
    assert cached[1] == csum




def test_neural_adapter_uses_native_sdk_sample_rate(tmp_path):
    """Unit SDK fixture only: verifies rate handling, not real neural synthesis."""
    import numpy as np
    from src.tts.engine import NeuralTTSBackend
    from src.render.artifact_validation import validate_audio
    backend = object.__new__(NeuralTTSBackend)
    backend._infer_lock = __import__("threading").Lock()
    backend.total_inference_seconds = 0.0
    backend.utterance_count = 0

    class FixtureSDK:
        def infer(self, **_):
            return {"native_signal": True}  # Deliberately not a numpy waveform.
        def save(self, result, path):
            assert result == {"native_signal": True}
            with wave.open(str(path), "wb") as stream:
                stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(24000)
                stream.writeframes((np.sin(np.arange(2400)/10)*2000).astype("<i2").tobytes())

    backend._sdk = FixtureSDK()
    path = tmp_path / "normalized.wav"
    duration = backend.synthesize("unit", "Ngọc Huyền", path)
    report = validate_audio(path)
    assert report["sample_rate"] == 48000
    assert duration == pytest.approx(0.1, abs=0.001)
    assert not list(tmp_path.glob("*.native.wav"))


def test_neural_zero_pcm_never_replaces_existing_output(tmp_path):
    """A failed neural backend cannot overwrite an existing valid utterance."""
    from src.tts.engine import VieNeuEngine
    from src.render.artifact_validation import ArtifactValidationError
    engine = object.__new__(VieNeuEngine)

    class InvalidBackend:
        def synthesize(self, output_path, **_):
            with wave.open(str(output_path), "wb") as stream:
                stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(48000)
                stream.writeframes(bytes(9600))
            return 0.1

    engine.backend = InvalidBackend()
    path = tmp_path / "preserved.wav"
    path.write_bytes(b"existing sentinel, never a production artifact")
    original = path.read_bytes()
    with pytest.raises(ArtifactValidationError):
        engine.synthesize_utterance("unit", "Ngọc Huyền", path)
    assert path.read_bytes() == original
