"""
src/tts/cache.py
SHA-256 utterance cache implementation for zero duplicate synthesis.
Strictly isolates neural audio artifacts from mock test audio artifacts.
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


def get_cache_root(backend_mode: str = "neural") -> Path:
    """Returns path to global TTS utterance cache, isolated by backend mode."""
    override = os.getenv("MANGA_RECAP_TTS_CACHE_DIR")
    base = Path(override).resolve() if override else Path("data/cache/tts").resolve()
    clean_mode = "mock" if (backend_mode or "").lower().strip() == "mock" else "neural"
    return base / clean_mode


def compute_utterance_cache_key(
    text: str,
    voice_id: str,
    speed: float = 1.0,
    emotion: Optional[str] = None,
    emotion_cue: Optional[str] = None,
    sampling_config: Optional[Dict[str, Any]] = None,
    model_version: str = "v3turbo",
    backend_mode: str = "neural",
) -> str:
    """
    Computes a deterministic SHA-256 hexadecimal hash for an utterance.
    Guarantees that identical text, voice, pacing, cues, AND backend mode
    produce the exact same key, strictly isolating neural from mock artifacts.
    """
    canonical_payload = {
        "text": text.strip(),
        "voice_id": voice_id.strip(),
        "speed": round(float(speed), 2),
        "emotion": (emotion or "neutral").strip().lower(),
        "emotion_cue": (emotion_cue or "").strip(),
        "sampling_config": sampling_config or {},
        "model_version": model_version.strip().lower(),
        "backend_mode": (backend_mode or "neural").strip().lower(),
    }
    serialized = json.dumps(canonical_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def get_cached_audio_path(cache_key: str, backend_mode: Optional[str] = None) -> Optional[Path]:
    """Returns path to cached audio file in the specified backend pool if it exists."""
    if backend_mode:
        cache_file = get_cache_root(backend_mode=backend_mode) / f"{cache_key}.wav"
        if cache_file.exists() and cache_file.stat().st_size > 44:
            return cache_file
        return None

    # If backend_mode not explicitly provided, check neural first, then mock, then legacy root
    for mode in ("neural", "mock"):
        cache_file = get_cache_root(backend_mode=mode) / f"{cache_key}.wav"
        if cache_file.exists() and cache_file.stat().st_size > 44:
            return cache_file

    override = os.getenv("MANGA_RECAP_TTS_CACHE_DIR")
    flat_root = Path(override).resolve() if override else Path("data/cache/tts").resolve()
    flat_file = flat_root / f"{cache_key}.wav"
    if flat_file.exists() and flat_file.stat().st_size > 44:
        return flat_file

    return None


def save_to_cache(
    cache_key: str,
    source_wav_path: Union[str, Path],
    backend_mode: str = "neural",
    overwrite: bool = False,
) -> Path:
    """
    Saves a synthesized WAV file into the backend-specific global cache pool.

    Args:
        cache_key: Deterministic SHA-256 hash identifying the utterance.
        source_wav_path: Path to the generated 48 kHz WAV file.
        backend_mode: 'neural' or 'mock' to maintain strict pool isolation.
        overwrite: If True, replaces existing cache file.
                   If False, only writes if cache file is missing or invalid (<= 44 bytes).

    Returns:
        Path to the cached WAV file.
    """
    root = get_cache_root(backend_mode=backend_mode)
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
