"""
src/tts/engine.py
Offline Python SDK wrapper around VieNeu-TTS v3 Turbo with deterministic mock fallback.
All output audio is guaranteed 48 kHz uncompressed 16-bit PCM mono WAV.
"""

from __future__ import annotations

import hashlib
import math
import os
import shutil
import struct
import subprocess
import threading
import time
import uuid
import wave
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from src.core.storage import calculate_sha256
from src.core.logger import logger
from src.media.ffmpeg import get_wav_duration, run_ffmpeg
from src.tts.catalog import CATALOG_VOICES, resolve_voice_id


# Thread-safe in-memory cache for audio file metrics: (path_str, mtime_ns, size_bytes) -> (duration_sec, sha256_checksum)
_AUDIO_METRIC_CACHE: Dict[Tuple[str, int, int], Tuple[float, str]] = {}
_CACHE_LOCK = threading.Lock()


def _cache_audio_metrics(path: Path, duration: float, checksum: str) -> None:
    """Stores verified duration and sha256 checksum in cache keyed by resolved path, mtime, and file size."""
    try:
        st = path.stat()
        key = (str(path.resolve()), st.st_mtime_ns, st.st_size)
        with _CACHE_LOCK:
            if len(_AUDIO_METRIC_CACHE) > 8192:
                _AUDIO_METRIC_CACHE.clear()
            _AUDIO_METRIC_CACHE[key] = (duration, checksum)
    except Exception:
        pass


def _get_cached_audio_metrics(path: Path) -> Optional[Tuple[float, str]]:
    """Retrieves cached duration and sha256 if file has not been modified."""
    try:
        st = path.stat()
        key = (str(path.resolve()), st.st_mtime_ns, st.st_size)
        with _CACHE_LOCK:
            return _AUDIO_METRIC_CACHE.get(key)
    except Exception:
        return None


def _encode_wav_16bit_pcm(
    audio_data: Any, sample_rate: int = 48000
) -> Tuple[bytes, float, str]:
    """
    Direct in-memory encoding of audio waveform to standard 48 kHz
    16-bit uncompressed PCM mono WAV binary.
    Returns: (wav_bytes, duration_seconds, sha256_checksum)
    Sub-millisecond execution, zero disk I/O, zero subprocess overhead.
    """
    import numpy as np

    if not isinstance(audio_data, np.ndarray):
        audio_data = np.asarray(audio_data, dtype=np.float32)
    if audio_data.ndim > 1:
        audio_data = audio_data.mean(axis=1 if audio_data.shape[1] < audio_data.shape[0] else 0)

    audio_clipped = np.clip(audio_data, -1.0, 1.0)
    pcm_bytes = (audio_clipped * 32767.0).astype(np.int16).tobytes()
    data_bytes = len(pcm_bytes)

    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        data_bytes + 36,
        b"WAVE",
        b"fmt ",
        16,
        1,
        1,
        sample_rate,
        sample_rate * 2,
        2,
        16,
        b"data",
        data_bytes,
    )
    wav_bytes = header + pcm_bytes
    checksum = hashlib.sha256(wav_bytes).hexdigest()
    duration = round(len(audio_data) / float(sample_rate), 4)
    return wav_bytes, duration, checksum


def _apply_speed_to_file(
    pcm_bytes: bytes,
    speed: float,
    output_path: Path,
    sample_rate: int = 48000,
) -> Tuple[float, str]:
    """
    Streams raw PCM bytes directly into ffmpeg atempo filter and writes to seekable output_path.
    Calculates duration and SHA-256 in one pass without intermediate temp files.
    """
    cmd = [
        "ffmpeg", "-y",
        "-f", "s16le",
        "-ar", str(sample_rate),
        "-ac", "1",
        "-i", "pipe:0",
        "-filter:a", f"atempo={speed}",
        "-ar", str(sample_rate),
        "-ac", "1",
        "-c:a", "pcm_s16le",
        str(output_path),
    ]
    try:
        subprocess.run(
            cmd,
            input=pcm_bytes,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
    except Exception as e:
        logger.warning(f"Streaming atempo speed filter failed: {e}")
        raise

    wav_bytes = output_path.read_bytes()
    checksum = hashlib.sha256(wav_bytes).hexdigest()
    if len(wav_bytes) >= 44:
        frames = (len(wav_bytes) - 44) // 2
        dur = round(frames / float(sample_rate), 4)
    else:
        dur = 0.0
    return dur, checksum


def get_wav_duration_seconds(file_path: Union[str, Path]) -> float:
    return get_wav_duration(Path(file_path))


class BaseTTSBackend(ABC):
    """Abstract base class for TTS execution backends."""

    @abstractmethod
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
        """Synthesizes text to WAV file and returns duration in seconds."""
        pass

    @abstractmethod
    def list_voices(self) -> List[Tuple[str, str]]:
        """Returns list of (description, voice_id)."""
        pass


_VALID_EMOTION_CUES = frozenset({"[cười]", "[thở dài]", "[hắng giọng]"})


def _prepare_tts_text(text: str, emotion_cue: Optional[str] = None) -> str:
    """Normalizes input text and prepends supported VieNeu emotion cue once."""
    input_text = text.strip()
    if emotion_cue and emotion_cue in _VALID_EMOTION_CUES:
        if emotion_cue not in input_text:
            input_text = f"{emotion_cue} {input_text}"
    return input_text


def _validate_tts_speed(speed: Any) -> float:
    """Validates explicit speech speed within [0.5, 2.0]."""
    if speed is None:
        raise ValueError("Neural TTS speed must be explicit and in 0.5..2.0")
    target_speed = float(speed)
    if not math.isfinite(target_speed) or not 0.5 <= target_speed <= 2.0:
        raise ValueError("Neural TTS speed must be explicit and in 0.5..2.0")
    return target_speed


def audio_file_metrics(path: Path) -> dict:
    # The checksum identifies cached syntheses. Duration only reads the WAV header.
    return {"duration": get_wav_duration(path), "sha256": calculate_sha256(path)}


def _normalize_native_wav_file(
    raw_path: Path,
    output_path: Path,
    target_speed: float,
    *,
    consume_raw: bool = False,
) -> Dict[str, Any]:
    """
    Validates native WAV output, applies FFmpeg atempo/resample ONLY when needed,
    atomically writes 48 kHz 16-bit mono PCM WAV to output_path, and returns audio report.
    """
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    get_wav_duration(raw_path)

    with wave.open(str(raw_path), "rb") as source:
        native_format = (source.getframerate(), source.getnchannels(), source.getsampwidth())

    if native_format == (48000, 1, 2) and abs(target_speed - 1.0) <= 1e-8:
        if consume_raw and raw_path.parent == output_path.parent:
            os.replace(raw_path, output_path)
            report = audio_file_metrics(output_path)
        else:
            tmp_out = output_path.with_name(f".{output_path.stem}.tmp.{uuid.uuid4().hex[:8]}.wav")
            try:
                shutil.copyfile(raw_path, tmp_out)
                report = audio_file_metrics(tmp_out)
                os.replace(tmp_out, output_path)
            finally:
                tmp_out.unlink(missing_ok=True)
        _cache_audio_metrics(output_path, report["duration"], report["sha256"])
        return report

    tmp_out = output_path.with_name(f".{output_path.stem}.tmp.{uuid.uuid4().hex[:8]}.wav")
    try:
        cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(raw_path)]
        if abs(target_speed - 1.0) > 1e-8:
            cmd += ["-af", f"atempo={target_speed}"]
        cmd += ["-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", str(tmp_out)]
        run_ffmpeg(cmd, description="Native TTS audio normalization", timeout=120)
        report = audio_file_metrics(tmp_out)
        os.replace(tmp_out, output_path)
        _cache_audio_metrics(output_path, report["duration"], report["sha256"])
        return report
    finally:
        tmp_out.unlink(missing_ok=True)


class NeuralTTSBackend(BaseTTSBackend):
    """
    Offline neural inference backend wrapping `vieneu.Vieneu(mode="v3turbo")`.
    Runs locally on Apple Silicon (CPU/MPS) via ONNX Runtime or CUDA PyTorch without network calls.
    """

    def __init__(self, mode: str = "v3turbo", precision: str = "fp32"):
        self.mode = mode
        self.precision = precision
        self._sdk = None
        self._infer_lock = threading.Lock()
        self.init_duration_seconds: float = 0.0
        self.total_inference_seconds: float = 0.0
        self.utterance_count: int = 0
        t0 = time.monotonic()
        self._init_sdk()
        self.init_duration_seconds = round(time.monotonic() - t0, 3)

    def _init_sdk(self):
        # Specifically verify that VieNeu weights exist in HF cache before enabling offline mode
        hf_cache = Path(os.getenv("HF_HOME", Path.home() / ".cache" / "huggingface" / "hub"))
        has_vieneu_weights = hf_cache.exists() and any(
            "vieneu" in p.name.lower() for p in hf_cache.iterdir()
        )
        if has_vieneu_weights:
            os.environ["HF_HUB_OFFLINE"] = "1"
            os.environ["TRANSFORMERS_OFFLINE"] = "1"

        try:
            from vieneu import Vieneu
            optimal_threads = min(max((os.cpu_count() or 8) // 2, 2), 6)
            self._sdk = Vieneu(mode=self.mode, threads=optimal_threads)
            logger.info(f"Initialized local VieNeu-TTS engine (mode={self.mode}, threads={optimal_threads})")
        except Exception as e:
            logger.error(f"Failed to initialize VieNeu-TTS neural backend: {e}")
            raise

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
        resolved_voice = resolve_voice_id(voice)
        target_speed = _validate_tts_speed(speed)
        input_text = _prepare_tts_text(text, emotion_cue=emotion_cue)

        t_infer_0 = time.monotonic()
        with self._infer_lock:
            audio_data = self._sdk.infer(
                text=input_text,
                voice=resolved_voice,
                **kwargs,
            )
        self.total_inference_seconds += (time.monotonic() - t_infer_0)
        self.utterance_count += 1

        # The SDK owns the native waveform/container contract. Never stamp an
        # assumed sample rate onto an opaque inference result.
        raw_path = output_path.with_name(f".{output_path.stem}.native.{uuid.uuid4().hex[:8]}.wav")
        try:
            with self._infer_lock:
                self._sdk.save(audio_data, str(raw_path))
            report = _normalize_native_wav_file(
                raw_path,
                output_path,
                target_speed,
                consume_raw=True,
            )
            return report["duration"]
        finally:
            raw_path.unlink(missing_ok=True)

    def synthesize_batch_native(
        self,
        tasks: List[Dict[str, Any]],
        *,
        batch_size: int = 8,
        cpu_workers: int = 4,
    ) -> List[Dict[str, Any]]:
        """
        Executes native batch synthesis (when supported by SDK) with length bucketing,
        OOM halving backoff, and offloaded CPU post-processing.
        """
        if not tasks:
            return []
        from src.tts.cuda_worker import run_sdk_batch_tasks

        lock = getattr(self, "_infer_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._infer_lock = lock
        mode = getattr(self, "mode", "v3turbo")

        normalized_inputs: List[Dict[str, Any]] = []
        for idx, task in enumerate(tasks):
            if task.get("seq_id") is None:
                normalized_inputs.append({**task, "seq_id": f"{idx:03d}"})
            else:
                normalized_inputs.append(task)

        results = run_sdk_batch_tasks(
            self._sdk,
            normalized_inputs,
            mode=mode,
            batch_size=batch_size,
            cpu_workers=cpu_workers,
            sdk_lock=lock,
        )
        self.total_inference_seconds = getattr(self, "total_inference_seconds", 0.0) + sum(
            float(r.get("inference_seconds", 0.0)) for r in results
        )
        self.utterance_count = getattr(self, "utterance_count", 0) + len(results)
        return results

    def _apply_speed(self, wav_path: Path, speed: float) -> None:
        """Applies pitch-preserving time stretching via ffmpeg atempo filter."""
        tmp_out = wav_path.with_name(f".{wav_path.name}.speed.tmp.wav")
        cmd = [
            "ffmpeg", "-y", "-i", str(wav_path),
            "-filter:a", f"atempo={speed}",
            "-ar", "48000",
            str(tmp_out)
        ]
        try:
            run_ffmpeg(cmd, description="TTS atempo time stretch")
            tmp_out.replace(wav_path)
        except Exception as e:
            logger.warning(f"Failed to apply atempo speed filter via ffmpeg: {e}")
            if tmp_out.exists():
                tmp_out.unlink()

    def list_voices(self) -> List[Tuple[str, str]]:
        if self._sdk and hasattr(self._sdk, "list_preset_voices"):
            return self._sdk.list_preset_voices()
        return [(v.description, v.voice_id) for v in CATALOG_VOICES.values()]


class VieNeuEngine:
    """Thread-safe singleton VieNeu-TTS neural engine without mock fallbacks."""

    _instance: Optional[VieNeuEngine] = None
    _lock = threading.Lock()

    def __init__(self):
        self.backend: BaseTTSBackend = self._create_backend()

    @classmethod
    def get_instance(cls, force_reload: bool = False) -> VieNeuEngine:
        with cls._lock:
            if cls._instance is None or force_reload:
                cls._instance = cls()
            return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Explicitly resets singleton instance for testing or resource reclamation."""
        with cls._lock:
            cls._instance = None

    @property
    def mode(self) -> str:
        return "neural"

    @property
    def init_duration_seconds(self) -> float:
        return getattr(self.backend, "init_duration_seconds", 0.0)

    @property
    def total_inference_seconds(self) -> float:
        return getattr(self.backend, "total_inference_seconds", 0.0)

    @property
    def utterance_count(self) -> int:
        return getattr(self.backend, "utterance_count", 0)

    def _create_backend(self) -> BaseTTSBackend:
        try:
            return NeuralTTSBackend()
        except Exception as e:
            raise RuntimeError(
                f"VieNeuEngine: Neural TTS backend failed to initialize: {e}. "
                "Ensure 'vieneu' is installed and weights are downloaded."
            ) from e

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
        """
        Synthesizes an utterance directly to output_path using atomic write safety.
        Returns: (duration_seconds, sha256_checksum, was_cached)
        """
        from src.core.storage import calculate_sha256

        resolved_voice = resolve_voice_id(voice)
        output_path = Path(output_path).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)

        tmp_id = uuid.uuid4().hex[:8]
        tmp_path = output_path.with_name(f".{output_path.stem}.tmp.{tmp_id}.wav")

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
                raise RuntimeError(f"TTS synthesis produced invalid or empty file at {tmp_path}")

            dur = get_wav_duration(tmp_path)
            tmp_path.replace(output_path)
            checksum = calculate_sha256(output_path)
            _cache_audio_metrics(output_path, dur, checksum)
            return dur, checksum, False
        except Exception:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            raise

    def synthesize_batch(
        self,
        tasks: List[Dict[str, Any]],
        workers: int = 2,
    ) -> List[Tuple[float, str, bool]]:
        """
        Batch synthesis for multiple utterances.
        Optimized to overlap neural inference and file I/O pipelines
        via ThreadPoolExecutor, and minimize disk round-trips.
        Each task dict contains: text, voice, output_path, speed (optional),
        emotion (optional), emotion_cue (optional).
        """
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
        else:
            return [_worker(t) for t in tasks]

    def synthesize_batch_native(
        self,
        tasks: List[Dict[str, Any]],
        *,
        batch_size: int = 8,
        cpu_workers: int = 4,
    ) -> List[Dict[str, Any]]:
        """
        Native batch synthesis wrapper returning per-utterance metadata dicts in
        original input order. Uses NeuralTTSBackend.synthesize_batch_native when
        running on neural backend (no silent mock fallback).
        """
        if not tasks:
            return []

        if hasattr(self.backend, "synthesize_batch_native"):
            return self.backend.synthesize_batch_native(
                tasks,
                batch_size=batch_size,
                cpu_workers=cpu_workers,
            )

        raise RuntimeError("TTS backend does not support synthesize_batch_native")

    def synthesize_to_file(
        self,
        text: str,
        voice_id: str,
        output_path: Path,
        speed: float = 1.0,
        **kwargs: Any,
    ) -> float:
        """Convenience method for direct synthesis to file."""
        dur, _, _ = self.synthesize_utterance(
            text=text,
            voice=voice_id,
            output_path=output_path,
            speed=speed,
            **kwargs,
        )
        return dur


def get_tts_engine(*, force_reload: bool = False) -> VieNeuEngine:
    """Returns singleton VieNeuEngine instance."""
    return VieNeuEngine.get_instance(force_reload=force_reload)
