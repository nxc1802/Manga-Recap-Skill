"""
tests/unit/test_tts_cache.py
Unit tests for SHA-256 utterance cache key calculation and pool resolution.
"""

from pathlib import Path
import pytest

from src.tts.cache import compute_utterance_cache_key, get_cached_audio_path, save_to_cache
from src.tts.engine import EngineMode, VieNeuEngine


def test_cache_key_determinism():
    k1 = compute_utterance_cache_key(
        text="Nhân vật bước đi trong đêm tối.",
        voice_id="Trúc Ly",
        speed=1.0,
        emotion="neutral",
    )
    k2 = compute_utterance_cache_key(
        text="   Nhân vật bước đi trong đêm tối.  ",
        voice_id="Trúc Ly",
        speed=1.0,
        emotion="neutral",
    )
    assert k1 == k2, "Leading/trailing whitespace should be trimmed"

    # Sensitivity tests: changing any parameter alters hash
    k_text = compute_utterance_cache_key(text="Nhân vật dừng lại.", voice_id="Trúc Ly")
    assert k1 != k_text

    k_voice = compute_utterance_cache_key(text="Nhân vật bước đi trong đêm tối.", voice_id="Hải Đăng")
    assert k1 != k_voice

    k_speed = compute_utterance_cache_key(text="Nhân vật bước đi trong đêm tối.", voice_id="Trúc Ly", speed=1.1)
    assert k1 != k_speed

    k_cue = compute_utterance_cache_key(
        text="Nhân vật bước đi trong đêm tối.",
        voice_id="Trúc Ly",
        emotion_cue="[thở dài]",
    )
    assert k1 != k_cue


def test_cache_pool_storage_and_hit(tmp_path, monkeypatch):
    cache_dir = tmp_path / "global_tts_cache"
    monkeypatch.setenv("MANGA_RECAP_TTS_CACHE_DIR", str(cache_dir))

    cache_key = "test_key_12345678"
    assert get_cached_audio_path(cache_key) is None

    # Create dummy source WAV
    source_wav = tmp_path / "source.wav"
    source_wav.write_bytes(b"RIFF\x2c\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00data\x08\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00")

    saved_path = save_to_cache(cache_key, source_wav)
    assert saved_path.exists()
    assert saved_path.name == f"{cache_key}.wav"

    cached_hit = get_cached_audio_path(cache_key)
    assert cached_hit is not None
    assert cached_hit == saved_path


def test_engine_synthesize_utterance_caching(tmp_path, monkeypatch):
    cache_dir = tmp_path / "tts_cache"
    monkeypatch.setenv("MANGA_RECAP_TTS_CACHE_DIR", str(cache_dir))

    engine = VieNeuEngine(force_mode=EngineMode.MOCK)
    out_file = tmp_path / "clip_001.wav"

    # Run 1: Cold synthesis
    dur1, key1, cached1 = engine.synthesize_utterance(
        text="Chào mừng các bạn đến với tập tiếp theo.",
        voice="Trúc Ly",
        output_path=out_file,
        use_cache=True,
    )
    assert out_file.exists()
    assert cached1 is False
    assert len(key1) == 64

    # Run 2: Cached hit (file exists)
    dur2, key2, cached2 = engine.synthesize_utterance(
        text="Chào mừng các bạn đến với tập tiếp theo.",
        voice="Trúc Ly",
        output_path=out_file,
        use_cache=True,
    )
    assert dur2 == dur1
    assert key2 == key1
    assert cached2 is True

    # Run 3: Forced re-synthesis
    dur3, key3, cached3 = engine.synthesize_utterance(
        text="Chào mừng các bạn đến với tập tiếp theo.",
        voice="Trúc Ly",
        output_path=out_file,
        use_cache=True,
        force=True,
    )
    assert cached3 is False


def test_save_to_cache_overwrite_flag(tmp_path, monkeypatch):
    """Verifies that save_to_cache with overwrite=True replaces cached audio files in the global pool."""
    cache_dir = tmp_path / "global_tts_cache"
    monkeypatch.setenv("MANGA_RECAP_TTS_CACHE_DIR", str(cache_dir))

    cache_key = "test_key_overwrite_verification_123"

    # Step 1: Write initial version (v1)
    wav_v1 = tmp_path / "v1.wav"
    wav_v1.write_bytes(b"RIFF\x2c\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00data\x08\x00\x00\x00AUDIO_V1")

    saved_v1 = save_to_cache(cache_key, wav_v1, overwrite=False)
    assert saved_v1.exists()
    assert b"AUDIO_V1" in saved_v1.read_bytes()

    # Step 2: Attempt save without overwrite (overwrite=False) -> Must remain v1
    wav_v2 = tmp_path / "v2.wav"
    wav_v2.write_bytes(b"RIFF\x2c\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00data\x08\x00\x00\x00AUDIO_V2")

    saved_v2_ignored = save_to_cache(cache_key, wav_v2, overwrite=False)
    assert b"AUDIO_V1" in saved_v2_ignored.read_bytes(), "When overwrite=False, existing cache must remain unchanged"

    # Step 3: Save with overwrite=True -> Must update to v2
    saved_v2_forced = save_to_cache(cache_key, wav_v2, overwrite=True)
    assert b"AUDIO_V2" in saved_v2_forced.read_bytes(), "When overwrite=True, cached file must be replaced"
    assert b"AUDIO_V1" not in saved_v2_forced.read_bytes()


def test_save_to_cache_corrupt_file_auto_healing(tmp_path, monkeypatch):
    """Verifies that a truncated cache file (size <= 44 bytes) is replaced even when overwrite=False."""
    cache_dir = tmp_path / "global_tts_cache"
    monkeypatch.setenv("MANGA_RECAP_TTS_CACHE_DIR", str(cache_dir))

    cache_key = "corrupt_key_xyz"
    corrupt_file = cache_dir / f"{cache_key}.wav"
    cache_dir.mkdir(parents=True, exist_ok=True)
    corrupt_file.write_bytes(b"RIFFtruncated")  # 13 bytes, <= 44 bytes header

    valid_wav = tmp_path / "valid.wav"
    valid_wav.write_bytes(b"RIFF\x2c\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00data\x08\x00\x00\x00HEALTHY_")

    repaired = save_to_cache(cache_key, valid_wav, overwrite=False)
    assert b"HEALTHY_" in repaired.read_bytes()


def test_engine_synthesize_utterance_force_updates_global_cache(tmp_path, monkeypatch):
    """Verifies that VieNeuEngine with force=True updates the cached file in the global cache pool."""
    cache_dir = tmp_path / "tts_cache"
    monkeypatch.setenv("MANGA_RECAP_TTS_CACHE_DIR", str(cache_dir))

    engine = VieNeuEngine(force_mode=EngineMode.MOCK)
    out_file = tmp_path / "clip_forced.wav"

    # Initial synthesis
    dur1, key1, cached1 = engine.synthesize_utterance(
        text="Kiểm tra ghi đè bộ nhớ đệm.",
        voice="Trúc Ly",
        output_path=out_file,
        use_cache=True,
    )
    assert cached1 is False
    cached_path = get_cached_audio_path(key1)
    assert cached_path is not None

    # Tamper with the cached file in global pool to simulate stale/defective audio
    tampered_bytes = b"RIFF\x2c\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00data\x08\x00\x00\x00OLDSTALE"
    cached_path.write_bytes(tampered_bytes)

    # Re-synthesize with force=True
    dur2, key2, cached2 = engine.synthesize_utterance(
        text="Kiểm tra ghi đè bộ nhớ đệm.",
        voice="Trúc Ly",
        output_path=out_file,
        use_cache=True,
        force=True,
    )
    assert cached2 is False
    assert key2 == key1

    # Verify that the global cache pool was updated and no longer contains OLDSTALE
    refreshed_cache_bytes = cached_path.read_bytes()
    assert b"OLDSTALE" not in refreshed_cache_bytes, (
        "Global cache pool was not updated when force=True!"
    )


def test_cache_mock_neural_isolation(tmp_path, monkeypatch):
    """Verifies that Mock and Neural cache keys are cryptographically distinct."""
    key_neural = compute_utterance_cache_key(
        text="Đối chiếu phân tách cache.",
        voice_id="Trúc Ly",
        speed=1.0,
        backend_mode="neural",
    )
    key_mock = compute_utterance_cache_key(
        text="Đối chiếu phân tách cache.",
        voice_id="Trúc Ly",
        speed=1.0,
        backend_mode="mock",
    )
    assert key_neural != key_mock, "Neural and Mock keys must never collide!"
