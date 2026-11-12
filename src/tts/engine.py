"""
src/tts/engine.py
Offline Python SDK wrapper around VieNeu-TTS v3 Turbo with deterministic mock fallback.
All output audio is guaranteed 48 kHz uncompressed 16-bit PCM mono WAV.
"""

from __future__ import annotations

import functools
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
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from src.core.logger import logger
from src.tts.catalog import CATALOG_VOICES, resolve_voice_id


class EngineMode(str, Enum):
    NEURAL = "neural"
    MOCK = "mock"


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
    """
    Extracts duration in seconds directly from uncompressed WAV binary header.
    Sub-millisecond execution, zero subprocess spawn overhead.
    """
    path = Path(file_path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"WAV file not found: {path}")

    cached = _get_cached_audio_metrics(path)
    if cached is not None and cached[0] > 0.0:
        return cached[0]

    try:
        with wave.open(str(path), "rb") as wf:
            framerate = wf.getframerate()
            nframes = wf.getnframes()
            if framerate <= 0:
                raise ValueError(f"Invalid WAV framerate: {framerate}")
            dur = round(nframes / float(framerate), 4)
            _cache_audio_metrics(path, dur, "")
            return dur
    except Exception as e:
        logger.warning(f"Failed to read WAV header with wave.open ({e}), attempting fallback...")
        # Fallback to file size calculation for 48kHz 16-bit mono PCM:
        # data_size = file_size - 44 bytes header
        # bytes_per_sec = 48000 * 1 * 2 = 96000
        size = path.stat().st_size
        if size > 44:
            dur = round((size - 44) / 96000.0, 4)
            _cache_audio_metrics(path, dur, "")
            return dur
        raise


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


class MockTTSBackend(BaseTTSBackend):
    """
    Deterministic mock backend for offline testing, CI/CD, and environments
    lacking multi-gigabyte neural model weights.
    Generates genuine 48 kHz 16-bit uncompressed PCM mono WAV files.
    """

    SAMPLE_RATE = 48000
    CHANNELS = 1
    BYTES_PER_SAMPLE = 2  # 16-bit PCM

    def __init__(self):
        self._infer_lock = threading.Lock()
        self.init_duration_seconds: float = 0.0
        self.total_inference_seconds: float = 0.0
        self.utterance_count: int = 0

    def estimate_duration(self, text: str, speed: float = 1.0) -> float:
        """
        Estimates speech duration based on Vietnamese speech cadence:
        Average ~12.5 characters per second (approx 3.2 words/sec).
        Clamped to [0.8s, 15.0s].
        """
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

        # Generate realistic modulated audio waveform (sine wave with fade-in/fade-out)
        freq = 220.0  # A3 pitch
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
        self.total_inference_seconds += (time.monotonic() - t_infer_0)
        self.utterance_count += 1

        return duration_sec

    def list_voices(self) -> List[Tuple[str, str]]:
        return [(v.description, v.voice_id) for v in CATALOG_VOICES.values()]


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
    from src.render.artifact_validation import validate_audio

    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    validate_audio(raw_path)

    with wave.open(str(raw_path), "rb") as source:
        native_format = (source.getframerate(), source.getnchannels(), source.getsampwidth())

    if native_format == (48000, 1, 2) and abs(target_speed - 1.0) <= 1e-8:
        if consume_raw and raw_path.parent == output_path.parent:
            os.replace(raw_path, output_path)
            report = validate_audio(output_path)
        else:
            tmp_out = output_path.with_name(f".{output_path.stem}.tmp.{uuid.uuid4().hex[:8]}.wav")
            try:
                shutil.copyfile(raw_path, tmp_out)
                report = validate_audio(tmp_out)
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
        result = subprocess.run(cmd, capture_output=True, timeout=120)
        if result.returncode:
            raise RuntimeError(
                f"Native TTS audio normalization failed: {result.stderr.decode(errors='replace')[-2000:]}"
            )
        report = validate_audio(tmp_out)
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
        import subprocess
        tmp_out = wav_path.with_name(f".{wav_path.name}.speed.tmp.wav")
        cmd = [
            "ffmpeg", "-y", "-i", str(wav_path),
            "-filter:a", f"atempo={speed}",
            "-ar", "48000",
            str(tmp_out)
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True)
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
    """
    Unified, thread-safe, singleton TTS engine with automatic mock fallback.
    """

    _instance: Optional[VieNeuEngine] = None
    _lock = threading.Lock()

    def __init__(self, force_mode: Optional[Union[EngineMode, str]] = None):
        if isinstance(force_mode, str):
            try:
                self.force_mode: Optional[EngineMode] = EngineMode(force_mode)
            except ValueError:
                self.force_mode = None
        else:
            self.force_mode = force_mode
        self.backend: BaseTTSBackend = self._create_backend()

    @classmethod
    def get_instance(
        cls,
        force_mode: Optional[Union[EngineMode, str]] = None,
        force_reload: bool = False,
    ) -> VieNeuEngine:
        with cls._lock:
            target_mode_str: Optional[str] = None
            if force_mode is not None:
                if isinstance(force_mode, EngineMode):
                    target_mode_str = force_mode.value
                else:
                    target_mode_str = str(force_mode)

            needs_init = (
                cls._instance is None
                or force_reload
                or (target_mode_str is not None and cls._instance.mode != target_mode_str)
            )
            if needs_init:
                mode_param = force_mode
                if isinstance(force_mode, str):
                    try:
                        mode_param = EngineMode(force_mode)
                    except ValueError:
                        mode_param = None
                cls._instance = cls(force_mode=mode_param)
            return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Explicitly resets singleton instance for testing or resource reclamation."""
        with cls._lock:
            cls._instance = None

    @property
    def mode(self) -> str:
        return EngineMode.MOCK.value if isinstance(self.backend, MockTTSBackend) else EngineMode.NEURAL.value

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
        # 1. Explicit Neural enforcement takes absolute precedence over VIENEU_MOCK
        if self.force_mode == EngineMode.NEURAL or os.getenv("VIENEU_FORCE_NEURAL") == "1":
            try:
                return NeuralTTSBackend()
            except Exception as e:
                raise RuntimeError(
                    f"VieNeuEngine: Neural TTS backend requested but failed to initialize: {e}. "
                    "Ensure 'vieneu' is installed and weights are downloaded."
                ) from e

        # 2. Explicit Mock enforcement (only if Neural is not strictly forced)
        if self.force_mode == EngineMode.MOCK or os.getenv("VIENEU_MOCK") == "1":
            logger.info("VieNeuEngine: Using MockTTSBackend (configured explicitly).")
            return MockTTSBackend()

        # 3. Auto mode: attempt neural first
        try:
            import vieneu
            return NeuralTTSBackend()
        except ImportError:
            logger.warning("VieNeuEngine: 'vieneu' package not available. Falling back to MockTTSBackend.")
            return MockTTSBackend()
        except Exception as e:
            logger.warning(f"VieNeuEngine: Neural initialization failed ({e}). Falling back to MockTTSBackend.")
            return MockTTSBackend()

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

            if self.mode == EngineMode.NEURAL.value:
                from src.render.artifact_validation import validate_audio
                dur = validate_audio(tmp_path)["duration"]
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

        if not isinstance(self.backend, MockTTSBackend):
            raise RuntimeError("TTS backend does not support synthesize_batch_native")

        from src.render.artifact_validation import validate_audio

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
            dur, _, _ = self.synthesize_utterance(
                text=raw_text,
                voice=task.get("voice") or "Ngọc Huyền",
                output_path=out_path,
                speed=target_speed,
                emotion=task.get("emotion"),
                emotion_cue=task.get("emotion_cue"),
            )
            infer_sec = round(time.monotonic() - t0, 6)
            t1 = time.monotonic()
            report = validate_audio(out_path)
            post_sec = round(time.monotonic() - t1, 6)
            results.append(
                {
                    "seq_id": seq_id,
                    "output_path": str(out_path),
                    "duration": report["duration"],
                    "sha256": report["sha256"],
                    "sample_rate": report["sample_rate"],
                    "rms": report["rms"],
                    "valid": bool(report["valid"]),
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
        """Convenience method for direct synthesis to file."""
        dur, _, _ = self.synthesize_utterance(
            text=text,
            voice=voice_id,
            output_path=output_path,
            speed=speed,
            **kwargs,
        )
        return dur


def get_tts_engine(
    force_mode: Optional[Union[EngineMode, str]] = None,
    force_reload: bool = False,
) -> VieNeuEngine:
    """Returns singleton VieNeuEngine instance."""
    return VieNeuEngine.get_instance(force_mode=force_mode, force_reload=force_reload)
