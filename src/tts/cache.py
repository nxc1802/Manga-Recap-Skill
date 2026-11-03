"""
src/tts/cache.py
SHA-256 utterance cache implementation for zero duplicate synthesis.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Union


def get_cache_root() -> Path:
    """Returns path to global TTS utterance cache."""
    override = os.getenv("MANGA_RECAP_TTS_CACHE_DIR")
    if override:
        return Path(override).resolve()
    return Path("data/cache/tts").resolve()


def compute_utterance_cache_key(
    text: str,
    voice_id: str,
    speed: float = 1.0,
    emotion: Optional[str] = None,
    emotion_cue: Optional[str] = None,
    sampling_config: Optional[Dict[str, Any]] = None,
    model_version: str = "v3turbo",
) -> str:
    """
    Computes a deterministic SHA-256 hexadecimal hash for an utterance.
    Guarantees that identical text, voice, pacing, and cues produce the exact same key.
    """
    canonical_payload = {
        "text": text.strip(),
        "voice_id": voice_id.strip(),
        "speed": round(float(speed), 2),
        "emotion": (emotion or "neutral").strip().lower(),
        "emotion_cue": (emotion_cue or "").strip(),
        "sampling_config": sampling_config or {},
        "model_version": model_version.strip().lower(),
    }
    serialized = json.dumps(canonical_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def get_cached_audio_path(cache_key: str) -> Optional[Path]:
    """Returns path to cached audio file if it exists."""
    cache_file = get_cache_root() / f"{cache_key}.wav"
    if cache_file.exists() and cache_file.stat().st_size > 44:
        return cache_file
    return None


def save_to_cache(
    cache_key: str,
    source_wav_path: Union[str, Path],
    overwrite: bool = False,
) -> Path:
    """
    Saves a synthesized WAV file into the global cache pool.

    Args:
        cache_key: Deterministic SHA-256 hash identifying the utterance.
        source_wav_path: Path to the generated 48 kHz WAV file.
        overwrite: If True, replaces existing cache file.
                   If False, only writes if cache file is missing or invalid (<= 44 bytes).

    Returns:
        Path to the cached WAV file.
    """
    root = get_cache_root()
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{cache_key}.wav"
    source_path = Path(source_wav_path).resolve()

    if overwrite or not target.exists() or target.stat().st_size <= 44:
        prefix = f".{cache_key}.tmp."
        fd, tmp_str = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=root)
        os.close(fd)
        tmp_path = Path(tmp_str)
        try:
            shutil.copyfile(source_path, tmp_path)
            os.replace(tmp_path, target)
        except Exception:
            if tmp_path.exists():
                with contextlib.suppress(Exception):
                    tmp_path.unlink()
            raise
    return target
