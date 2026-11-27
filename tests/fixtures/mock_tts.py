"""Deterministic mock TTS fixtures for offline tests and devtools only."""

from __future__ import annotations

import hashlib
import math
import struct
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from src.core.storage import calculate_sha256
from src.media.ffmpeg import get_wav_duration
from src.tts.catalog import CATALOG_VOICES, resolve_voice_id
from src.tts.engine import (
    BaseTTSBackend,
    _cache_audio_metrics,
    _validate_tts_speed,
    audio_file_metrics,
)


class MockTTSBackend(BaseTTSBackend):
    """
    Deterministic mock backend for offline testing and devtools benchmarking.
    Generates genuine 48 kHz 16-bit uncompressed PCM mono WAV files.
    """

    SAMPLE_RATE = 48000
    CHANNELS = 1
    BYTES_PER_SAMPLE = 2

    def __init__(self):
        self._infer_lock = threading.Lock()
        self.init_duration_seconds: float = 0.0
        self.total_inference_seconds: float = 0.0
        self.utterance_count: int = 0

    def estimate_duration(self, text: str, speed: float = 1.0) -> float:
        clean_text = text.strip()
        words = len(clean_text.split())
        base_dur = max(0.8, words / 3.2)
        adjusted_dur = base_dur / max(0.5, min(2.0, speed))
        return round(adjusted_dur, 3)

    def synthesize(
        self,
        text: str,
        voice: str,
        output_path: Path,
        speed: float = 1.0,
        emotion: Optional[str] = None,
        emotion_cue: Optional[str] = None,
        **kwargs: Any,
    ) -> float:
        output_path = Path(output_path).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        duration_sec = self.estimate_duration(text, speed=speed)
        total_frames = int(duration_sec * self.SAMPLE_RATE)

        freq = 220.0
        frames = bytearray()
        fade_frames = min(int(self.SAMPLE_RATE * 0.05), max(1, total_frames // 4))

        for i in range(total_frames):
            envelope = 1.0
            if i < fade_frames:
                envelope = i / float(fade_frames)
            elif i > total_frames - fade_frames:
                envelope = (total_frames - i) / float(fade_frames)

            sample_val = int(800 * envelope * math.sin(2 * math.pi * freq * (i / self.SAMPLE_RATE)))
            frames.extend(struct.pack("<h", max(-32768, min(32767, sample_val))))

        t_infer_0 = time.monotonic()
        data_bytes = len(frames)
        header = struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF",
            data_bytes + 36,
            b"WAVE",
            b"fmt ",
            16,
            1,
            self.CHANNELS,
            self.SAMPLE_RATE,
            self.SAMPLE_RATE * self.BYTES_PER_SAMPLE,
            self.BYTES_PER_SAMPLE,
            16,
            b"data",
            data_bytes,
        )
        wav_bytes = header + bytes(frames)
        with self._infer_lock:
            output_path.write_bytes(wav_bytes)
            checksum = hashlib.sha256(wav_bytes).hexdigest()
            self._last_checksum = checksum
            _cache_audio_metrics(output_path, duration_sec, checksum)
        self.total_inference_seconds += time.monotonic() - t_infer_0
        self.utterance_count += 1
        return duration_sec

    def list_voices(self) -> List[Tuple[str, str]]:
        return [(v.description, v.voice_id) for v in CATALOG_VOICES.values()]


class MockVieNeuEngine:
    """Offline test engine mirroring VieNeuEngine synthesis methods without neural weights."""

    def __init__(self):
        self.backend = MockTTSBackend()

    @property
    def mode(self) -> str:
        return "mock"

    @property
    def init_duration_seconds(self) -> float:
        return self.backend.init_duration_seconds

    @property
    def total_inference_seconds(self) -> float:
        return self.backend.total_inference_seconds

    @property
    def utterance_count(self) -> int:
        return self.backend.utterance_count

    def synthesize_utterance(
        self,
        text: str,
        voice: str,
        output_path: Union[str, Path],
        speed: float = 1.0,
        emotion: Optional[str] = None,
        emotion_cue: Optional[str] = None,
        use_cache: bool = False,
        force: bool = False,
        **kwargs: Any,
    ) -> Tuple[float, str, bool]:
        resolved_voice = resolve_voice_id(voice)
        output_path = Path(output_path).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = output_path.with_name(f".{output_path.stem}.tmp.{uuid.uuid4().hex[:8]}.wav")
        try:
            dur = self.backend.synthesize(
                text=text,
                voice=resolved_voice,
                output_path=tmp_path,
                speed=speed,
                emotion=emotion,
                emotion_cue=emotion_cue,
                **kwargs,
            )
            if not tmp_path.exists() or tmp_path.stat().st_size <= 44:
                raise RuntimeError(f"Mock TTS produced invalid file at {tmp_path}")
            dur = get_wav_duration(tmp_path)
            tmp_path.replace(output_path)
            checksum = calculate_sha256(output_path)
            _cache_audio_metrics(output_path, dur, checksum)
            return dur, checksum, False
        except Exception:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            raise

    def synthesize_batch(self, tasks: List[Dict[str, Any]], workers: int = 2) -> List[Tuple[float, str, bool]]:
        if not tasks:
            return []

        def _worker(t_dict: Dict[str, Any]) -> Tuple[float, str, bool]:
            return self.synthesize_utterance(
                text=t_dict["text"],
                voice=t_dict.get("voice") or "Ngọc Huyền",
                output_path=t_dict["output_path"],
                speed=t_dict.get("speed", 1.0) or 1.0,
                emotion=t_dict.get("emotion"),
                emotion_cue=t_dict.get("emotion_cue"),
            )

        if workers > 1 and len(tasks) > 1:
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=min(workers, len(tasks))) as pool:
                return list(pool.map(_worker, tasks))
        return [_worker(t) for t in tasks]

    def synthesize_batch_native(
        self,
        tasks: List[Dict[str, Any]],
        *,
        batch_size: int = 8,
        cpu_workers: int = 4,
    ) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        for idx, task in enumerate(tasks):
            seq_id = task.get("seq_id") if task.get("seq_id") is not None else f"{idx:03d}"
            raw_text = task.get("text")
            if not isinstance(raw_text, str) or not raw_text.strip():
                raise ValueError(f"Task {seq_id}: text must be a non-empty string")
            raw_out = task.get("output_path")
            if raw_out is None or not str(raw_out).strip():
                raise ValueError(f"Task {seq_id}: output_path is required")
            out_path = Path(raw_out).resolve()
            if out_path.exists() and out_path.is_dir():
                raise ValueError(f"Task {seq_id}: output_path points to a directory: {out_path}")
            target_speed = _validate_tts_speed(task.get("speed", 1.0))
            t0 = time.monotonic()
            self.synthesize_utterance(
                text=raw_text,
                voice=task.get("voice") or "Ngọc Huyền",
                output_path=out_path,
                speed=target_speed,
                emotion=task.get("emotion"),
                emotion_cue=task.get("emotion_cue"),
            )
            infer_sec = round(time.monotonic() - t0, 6)
            t1 = time.monotonic()
            report = audio_file_metrics(out_path)
            post_sec = round(time.monotonic() - t1, 6)
            results.append(
                {
                    "seq_id": seq_id,
                    "output_path": str(out_path),
                    "duration": report["duration"],
                    "sha256": report["sha256"],
                    "sample_rate": 48000,
                    "valid": True,
                    "inference_seconds": infer_sec,
                    "postprocess_seconds": post_sec,
                    "batch_used": False,
                    "effective_batch_size": 1,
                    "batching_mode": "mock_sequential",
                }
            )
        return results

    def synthesize_to_file(
        self,
        text: str,
        voice_id: str,
        output_path: Path,
        speed: float = 1.0,
        **kwargs: Any,
    ) -> float:
        dur, _, _ = self.synthesize_utterance(
            text=text,
            voice=voice_id,
            output_path=output_path,
            speed=speed,
            **kwargs,
        )
        return dur


def make_mock_tts_engine(*, force_reload: bool = False) -> MockVieNeuEngine:
    """Factory compatible with synthesize_chapter_tts(tts_engine_factory=...)."""
    return MockVieNeuEngine()
