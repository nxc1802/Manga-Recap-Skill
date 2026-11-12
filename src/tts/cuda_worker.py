"""
src/tts/cuda_worker.py
Isolated CUDA worker, runtime probing, length-bucketed native batch synthesis,
dynamic OOM backoff, and decoupled CPU post-processing for VieNeu-TTS v3 Turbo.

CRITICAL: Never import `torch`, `vieneu`, `onnxruntime`, `numpy`, or
`src.render.*` at module import time so `multiprocessing.get_context("spawn")`
can set `CUDA_VISIBLE_DEVICES` inside `tts_worker_loop(...)` before CUDA init.
"""

from __future__ import annotations

import gc
import importlib.metadata
import inspect
import os
import queue as stdlib_queue
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from src.core.logger import logger
from src.tts.catalog import resolve_voice_id
from src.tts.engine import (
    _cache_audio_metrics,
    _encode_wav_16bit_pcm,
    _normalize_native_wav_file,
    _prepare_tts_text,
    _validate_tts_speed,
)

LENGTH_BUCKET_THRESHOLDS: Tuple[int, int, int] = (50, 120, 250)
MAX_LONG_TEXT_BATCH_SIZE: int = 16


def _pkg_version(name: str) -> Optional[str]:
    try:
        return importlib.metadata.version(name)
    except Exception:
        return None


def _get_session_providers(sess: Any) -> Optional[List[str]]:
    if sess is None:
        return None
    if hasattr(sess, "get_providers") and callable(sess.get_providers):
        return list(sess.get_providers())
    if hasattr(sess, "session") and hasattr(sess.session, "get_providers"):
        return list(sess.session.get_providers())
    if hasattr(sess, "sess") and hasattr(sess.sess, "get_providers"):
        return list(sess.sess.get_providers())
    return None


def _collect_vram_metrics(device_index: int = 0) -> Dict[str, float]:
    """Reads CUDA VRAM stats (MB) for `device_index`, returning zeros when unavailable."""
    zeros = {
        "allocated_mb": 0.0,
        "peak_allocated_mb": 0.0,
        "reserved_mb": 0.0,
        "free_mb": 0.0,
        "total_mb": 0.0,
    }
    try:
        import torch  # noqa: PLC0415
    except Exception:
        return zeros

    try:
        if not getattr(torch, "cuda", None) or not torch.cuda.is_available():
            return zeros
        count = int(torch.cuda.device_count())
        idx = device_index if 0 <= device_index < count else 0
        free_b, total_b = torch.cuda.mem_get_info(idx)
        return {
            "allocated_mb": round(float(torch.cuda.memory_allocated(idx)) / (1024.0 * 1024.0), 1),
            "peak_allocated_mb": round(float(torch.cuda.max_memory_allocated(idx)) / (1024.0 * 1024.0), 1),
            "reserved_mb": round(float(torch.cuda.memory_reserved(idx)) / (1024.0 * 1024.0), 1),
            "free_mb": round(float(free_b) / (1024.0 * 1024.0), 1),
            "total_mb": round(float(total_b) / (1024.0 * 1024.0), 1),
        }
    except Exception:
        return zeros


def _clear_cuda_cache() -> None:
    """Runs Python GC and releases PyTorch CUDA caching allocator memory if active."""
    gc.collect()
    if "torch" not in sys.modules:
        return
    try:
        import torch  # noqa: PLC0415

        cuda_mod = getattr(torch, "cuda", None)
        if cuda_mod is not None and cuda_mod.is_available():
            cuda_mod.empty_cache()
            if hasattr(cuda_mod, "ipc_collect"):
                cuda_mod.ipc_collect()
    except Exception:
        pass


def _is_cuda_oom(exc: BaseException) -> bool:
    """Detects PyTorch CUDA OOM errors without importing torch unconditionally."""
    if "torch" in sys.modules:
        try:
            import torch  # noqa: PLC0415

            oom_cls = getattr(getattr(torch, "cuda", None), "OutOfMemoryError", None)
            if oom_cls is not None and isinstance(exc, oom_cls):
                return True
        except Exception:
            pass
    msg = str(exc).lower()
    return (
        "out of memory" in msg
        or "cuda oom" in msg
        or "cublas_status_alloc_failed" in msg
        or "cuda error: out of memory" in msg
    )


def _normalize_precision_kwargs(precision: str) -> Tuple[str, str]:
    """
    Maps unified precision setting into (pytorch_dtype, onnx_precision).
    On Dual-T4 CUDA, `auto` selects `bfloat16` via VieNeu's `_resolve_dtype`.
    """
    norm = str(precision or "auto").strip().lower()
    dtype_map = {
        "auto": "auto",
        "bf16": "bfloat16",
        "bfloat16": "bfloat16",
        "fp16": "float16",
        "float16": "float16",
        "fp32": "float32",
        "float32": "float32",
        "int8": "auto",
    }
    onnx_map = {
        "int8": "int8",
    }
    return dtype_map.get(norm, "auto"), onnx_map.get(norm, "fp32")


def _call_with_accepted_kwargs(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Calls `fn(*args, ...)` passing only kwargs accepted by its signature."""
    try:
        sig = inspect.signature(fn)
        accepts_varkw = any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )
        if accepts_varkw:
            return fn(*args, **kwargs)
        filtered = {k: v for k, v in kwargs.items() if k in sig.parameters}
        return fn(*args, **filtered)
    except (TypeError, ValueError):
        return fn(*args, **kwargs)


def _inspect_loaded_sdk(sdk: Any) -> Dict[str, Any]:
    backend_name = getattr(sdk, "backend", None)
    engine = getattr(sdk, "engine", None)
    engine_class = type(engine).__name__ if engine is not None else None

    raw_dev = getattr(engine, "device", None) if engine is not None else None
    if raw_dev is None:
        engine_dev = None
    elif type(raw_dev).__name__ == "_Dev":
        engine_dev = getattr(raw_dev, "type", "cpu")
    else:
        engine_dev = str(raw_dev)

    raw_dtype = getattr(engine, "dtype", None) if engine is not None else None
    engine_dtype = str(raw_dtype) if raw_dtype is not None else None

    model_param_device: Optional[str] = None
    model_param_dtype: Optional[str] = None
    if engine is not None and hasattr(engine, "model"):
        try:
            param = next(engine.model.parameters())
            model_param_device = str(param.device)
            model_param_dtype = str(param.dtype)
        except Exception:
            pass

    sub_providers: Dict[str, Any] = {}
    if engine is not None:
        for attr in ("sess_pre", "sess_dec", "sess_ac", "sess_codec_dec", "speaker_encoder", "denoiser"):
            sess = getattr(engine, attr, None)
            if sess is not None:
                sub_providers[attr] = _get_session_providers(sess)

    has_infer = callable(getattr(sdk, "infer", None))
    has_infer_batch = callable(getattr(sdk, "infer_batch", None))
    is_onnx_engine = bool(
        backend_name == "onnx"
        or (engine_class is not None and "onnx" in engine_class.lower())
    )

    return {
        "tts_backend": backend_name,
        "engine_class": engine_class,
        "engine_device": engine_dev,
        "dtype": model_param_dtype or engine_dtype,
        "model_param_device": model_param_device,
        "sub_session_providers": sub_providers,
        "has_infer": has_infer,
        "has_infer_batch": has_infer_batch,
        "is_onnx_engine": is_onnx_engine,
    }


def probe_vieneu_runtime(
    mode: str = "v3turbo",
    *,
    sdk: Optional[Any] = None,
    load_sdk: bool = False,
) -> Dict[str, Any]:
    """
    Inspects VieNeu runtime, PyTorch CUDA availability, ONNX Runtime providers,
    true `infer_batch` support, dtype, and CUDA VRAM usage.

    Never classifies queue grouping or ONNX sequential loops as neural batching.
    """
    torch_installed = False
    torch_version: Optional[str] = None
    cuda_available = False
    device_count = 0
    bf16_supported: Optional[bool] = None
    device_name: Optional[str] = None
    vram = _collect_vram_metrics(0)

    try:
        import torch  # noqa: PLC0415

        torch_installed = True
        torch_version = getattr(torch, "__version__", None)
        cuda_mod = getattr(torch, "cuda", None)
        if cuda_mod is not None:
            cuda_available = bool(cuda_mod.is_available())
            if cuda_available:
                device_count = int(cuda_mod.device_count())
                if hasattr(cuda_mod, "is_bf16_supported"):
                    try:
                        bf16_supported = bool(cuda_mod.is_bf16_supported())
                    except Exception:
                        bf16_supported = None
                try:
                    cur_idx = int(cuda_mod.current_device()) if hasattr(cuda_mod, "current_device") else 0
                except Exception:
                    cur_idx = 0
                try:
                    device_name = str(cuda_mod.get_device_name(cur_idx))
                except Exception:
                    device_name = None
                vram = _collect_vram_metrics(cur_idx)
    except Exception:
        pass

    ort_installed = False
    ort_providers: List[str] = []
    try:
        import onnxruntime as ort  # noqa: PLC0415

        ort_installed = True
        ort_providers = list(ort.get_available_providers())
    except Exception:
        pass

    vieneu_installed = False
    vieneu_version = _pkg_version("vieneu")
    cls_has_infer = False
    cls_has_infer_batch = False
    ephemeral_sdk = None

    try:
        import vieneu  # noqa: PLC0415

        vieneu_installed = True
        if mode == "v3turbo":
            try:
                from vieneu.v3turbo import V3TurboVieNeuTTS  # noqa: PLC0415

                cls_has_infer = callable(getattr(V3TurboVieNeuTTS, "infer", None))
                cls_has_infer_batch = callable(getattr(V3TurboVieNeuTTS, "infer_batch", None))
            except Exception:
                v3_cls = getattr(vieneu, "V3TurboVieNeuTTS", None)
                if v3_cls is not None:
                    cls_has_infer = callable(getattr(v3_cls, "infer", None))
                    cls_has_infer_batch = callable(getattr(v3_cls, "infer_batch", None))
        if sdk is None and not load_sdk:
            from src.tts.engine import VieNeuEngine  # noqa: PLC0415

            inst = getattr(VieNeuEngine, "_instance", None)
            inst_backend = getattr(inst, "backend", None) if inst is not None else None
            inst_sdk = getattr(inst_backend, "_sdk", None) if inst_backend is not None else None
            if inst_sdk is not None and getattr(inst_backend, "mode", mode) == mode:
                sdk = inst_sdk
        elif sdk is None and load_sdk:
            ephemeral_sdk = vieneu.Vieneu(mode=mode)
            sdk = ephemeral_sdk
    except Exception:
        pass

    def _build_runtime_report(
        *,
        sdk_loaded: bool,
        provider: str,
        tts_backend: Optional[str],
        engine_class: Optional[str],
        dtype: Optional[str],
        supports_infer: bool,
        supports_infer_batch: bool,
        batching_mode: str,
        sub_session_providers: Dict[str, Any],
    ) -> Dict[str, Any]:
        return {
            "mode": mode,
            "sdk_loaded": sdk_loaded,
            "vieneu_installed": vieneu_installed,
            "vieneu_version": vieneu_version,
            "torch_installed": torch_installed,
            "torch_version": torch_version,
            "cuda_available": cuda_available,
            "device_count": device_count,
            "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
            "device_name": device_name,
            "bf16_supported": bf16_supported,
            "ort_installed": ort_installed,
            "ort_providers": ort_providers,
            "provider": provider,
            "tts_backend": tts_backend,
            "engine_class": engine_class,
            "dtype": dtype,
            "supports_infer": supports_infer,
            "supports_infer_batch": supports_infer_batch,
            "neural_batching": supports_infer_batch,
            "batching_mode": batching_mode,
            "sub_session_providers": sub_session_providers,
            "vram_free_mb": vram["free_mb"],
            "vram_allocated_mb": vram["allocated_mb"],
            "vram_peak_allocated_mb": vram["peak_allocated_mb"],
            "vram_reserved_mb": vram["reserved_mb"],
            "vram_total_mb": vram["total_mb"],
        }

    try:
        if sdk is not None:
            info = _inspect_loaded_sdk(sdk)
            tts_backend = info["tts_backend"]
            engine_class = info["engine_class"]
            dtype = info["dtype"]
            sub_providers = info["sub_session_providers"]
            has_infer = info["has_infer"]
            has_infer_batch = info["has_infer_batch"]
            is_onnx = info["is_onnx_engine"]
            model_dev = info["model_param_device"] or info["engine_device"] or ""

            if tts_backend == "pytorch":
                is_cuda = cuda_available or ("cuda" in model_dev.lower())
                provider = "pytorch_cuda" if is_cuda else "pytorch_cpu"
                supports_infer_batch = bool(has_infer_batch)
            elif is_onnx:
                provider = "onnx_cpu"
                # V3TurboVieNeuTTS.infer_batch runs sequentially when backend == 'onnx'.
                supports_infer_batch = False
            elif cuda_available:
                provider = "pytorch_cuda"
                supports_infer_batch = bool(has_infer_batch)
            elif has_infer_batch:
                provider = "custom_sdk"
                supports_infer_batch = True
            elif ort_installed or vieneu_installed:
                provider = "onnx_cpu"
                supports_infer_batch = False
            else:
                provider = "custom_sdk"
                supports_infer_batch = False

            if dtype is None:
                if provider == "pytorch_cuda":
                    dtype = "torch.bfloat16" if bf16_supported else "torch.float16"
                elif provider == "onnx_cpu":
                    dtype = "float32"

            if supports_infer_batch:
                batching_mode = "native_infer_batch"
            elif is_onnx:
                batching_mode = "onnx_cpu_sequential"
            elif has_infer:
                batching_mode = "queue_grouped_single"
            else:
                batching_mode = "unavailable"

            return _build_runtime_report(
                sdk_loaded=True,
                provider=provider,
                tts_backend=tts_backend,
                engine_class=engine_class,
                dtype=dtype,
                supports_infer=has_infer,
                supports_infer_batch=supports_infer_batch,
                batching_mode=batching_mode,
                sub_session_providers=sub_providers,
            )

        # Unloaded static capability probe
        if cuda_available and vieneu_installed:
            provider = "pytorch_cuda"
            dtype = "torch.bfloat16" if bf16_supported else "torch.float16"
            supports_infer_batch = bool(cls_has_infer_batch)
            batching_mode = "native_infer_batch" if supports_infer_batch else "queue_grouped_single"
        elif vieneu_installed or ort_installed:
            provider = "onnx_cpu"
            dtype = "float32"
            supports_infer_batch = False
            batching_mode = "onnx_cpu_sequential" if vieneu_installed else "unavailable"
        else:
            provider = "unavailable"
            dtype = None
            supports_infer_batch = False
            batching_mode = "unavailable"

        return _build_runtime_report(
            sdk_loaded=False,
            provider=provider,
            tts_backend=None,
            engine_class=None,
            dtype=dtype,
            supports_infer=cls_has_infer,
            supports_infer_batch=supports_infer_batch,
            batching_mode=batching_mode,
            sub_session_providers={},
        )
    finally:
        if ephemeral_sdk is not None and hasattr(ephemeral_sdk, "close"):
            try:
                ephemeral_sdk.close()
            except Exception:
                pass


def get_length_bucket(text: str) -> int:
    """Categorizes normalized text length into bucket indices 0..3."""
    char_len = len(text or "")
    t0, t1, t2 = LENGTH_BUCKET_THRESHOLDS
    if char_len <= t0:
        return 0
    if char_len <= t1:
        return 1
    if char_len <= t2:
        return 2
    return 3


def _validate_and_normalize_tasks(tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Validates all incoming task dicts up front before running inference."""
    normalized: List[Dict[str, Any]] = []
    for idx, task in enumerate(tasks):
        if not isinstance(task, dict):
            raise ValueError(f"Task at index {idx} must be a dict")
        if "seq_id" not in task or task["seq_id"] is None:
            raise ValueError(f"Task at index {idx}: seq_id is required")
        seq_id = task["seq_id"]

        raw_text = task.get("text")
        if not isinstance(raw_text, str) or not raw_text.strip():
            raise ValueError(f"Task {seq_id}: text must be a non-empty string")

        raw_out = task.get("output_path")
        if raw_out is None or not str(raw_out).strip():
            raise ValueError(f"Task {seq_id}: output_path is required")
        output_path = Path(raw_out).resolve()
        if output_path.exists() and output_path.is_dir():
            raise ValueError(f"Task {seq_id}: output_path points to a directory: {output_path}")

        if "speed" in task:
            target_speed = _validate_tts_speed(task["speed"])
        else:
            target_speed = 1.0

        resolved_voice = resolve_voice_id(task.get("voice") or "Ngọc Huyền")
        prepared_text = _prepare_tts_text(raw_text, emotion_cue=task.get("emotion_cue"))

        normalized.append(
            {
                "orig_idx": idx,
                "seq_id": seq_id,
                "text": prepared_text,
                "voice": resolved_voice,
                "speed": target_speed,
                "speed_key": round(target_speed, 6),
                "bucket": get_length_bucket(prepared_text),
                "emotion": task.get("emotion"),
                "emotion_cue": task.get("emotion_cue"),
                "output_path": output_path,
                "chapter_number": task.get("chapter_number"),
            }
        )
    return normalized


def group_and_bucket_tasks(
    normalized_tasks: List[Dict[str, Any]],
    max_batch_size: int,
) -> List[List[Dict[str, Any]]]:
    """
    Groups normalized tasks by `(voice, speed_key)` and length bucket, sorting
    by text length within each bucket to minimize padding waste.
    """
    grouped: Dict[Tuple[str, float], Dict[int, List[Dict[str, Any]]]] = {}
    for item in normalized_tasks:
        group_key = (item["voice"], item["speed_key"])
        bucket_idx = int(item["bucket"])
        grouped.setdefault(group_key, {}).setdefault(bucket_idx, []).append(item)

    micro_batches: List[List[Dict[str, Any]]] = []
    for _, buckets in grouped.items():
        for bucket_idx in sorted(buckets.keys()):
            bucket_items = sorted(buckets[bucket_idx], key=lambda x: len(x["text"]))
            cap = max_batch_size if bucket_idx < 3 else min(MAX_LONG_TEXT_BATCH_SIZE, max_batch_size)
            cap = max(1, cap)
            for start in range(0, len(bucket_items), cap):
                micro_batches.append(bucket_items[start : start + cap])
    return micro_batches


def _uses_builtin_waveform_save(sdk: Any) -> bool:
    """
    True if `sdk.save` is missing or unmodified `BaseVieneuTTS.save` (which only
    calls soundfile.write(path, wav, sdk.sample_rate)), allowing direct in-memory
    WAV encoding inside CPU worker threads without an intermediate disk write.
    """
    if "save" in getattr(sdk, "__dict__", {}):
        return False
    save_fn = getattr(sdk, "save", None)
    if save_fn is None:
        return True
    try:
        from vieneu.base import BaseVieneuTTS  # noqa: PLC0415

        return getattr(type(sdk), "save", None) is BaseVieneuTTS.save
    except Exception:
        return False


def _postprocess_cpu_item(
    *,
    item: Dict[str, Any],
    waveform: Any,
    native_sr: int,
    raw_path: Optional[Path],
    inference_seconds: float,
    batch_used: bool,
    effective_batch_size: int,
    batching_mode: str,
) -> Dict[str, Any]:
    """Runs CPU normalization/FFmpeg skip, validate_audio, SHA-256, and atomic write."""
    t_post_0 = time.perf_counter()
    out_path: Path = item["output_path"]
    target_speed: float = float(item["speed"])
    out_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        if raw_path is not None:
            report = _normalize_native_wav_file(
                raw_path,
                out_path,
                target_speed,
                consume_raw=True,
            )
        else:
            import numpy as np  # noqa: PLC0415
            from src.render.artifact_validation import ArtifactValidationError, validate_audio  # noqa: PLC0415

            arr = np.asarray(waveform, dtype=np.float32)
            if arr.ndim > 1:
                arr = arr.reshape(-1)
            if arr.size == 0 or not np.isfinite(arr).all():
                raise ArtifactValidationError(
                    f"Task {item['seq_id']}: empty or non-finite neural waveform"
                )

            if native_sr == 48000 and abs(target_speed - 1.0) <= 1e-8:
                wav_bytes, _, _ = _encode_wav_16bit_pcm(arr, sample_rate=48000)
                tmp_out = out_path.with_name(f".{out_path.stem}.tmp.{uuid.uuid4().hex[:8]}.wav")
                try:
                    tmp_out.write_bytes(wav_bytes)
                    report = validate_audio(tmp_out)
                    os.replace(tmp_out, out_path)
                    _cache_audio_metrics(out_path, report["duration"], report["sha256"])
                finally:
                    tmp_out.unlink(missing_ok=True)
            else:
                wav_bytes, _, _ = _encode_wav_16bit_pcm(arr, sample_rate=native_sr)
                native_tmp = out_path.with_name(f".{out_path.stem}.native.{uuid.uuid4().hex[:8]}.wav")
                try:
                    native_tmp.write_bytes(wav_bytes)
                    report = _normalize_native_wav_file(
                        native_tmp,
                        out_path,
                        target_speed,
                        consume_raw=True,
                    )
                finally:
                    native_tmp.unlink(missing_ok=True)
    finally:
        if raw_path is not None:
            raw_path.unlink(missing_ok=True)

    post_seconds = round(time.perf_counter() - t_post_0, 6)
    result: Dict[str, Any] = {
        "seq_id": item["seq_id"],
        "output_path": str(out_path),
        "duration": report["duration"],
        "sha256": report["sha256"],
        "sample_rate": report["sample_rate"],
        "rms": report["rms"],
        "valid": bool(report["valid"]),
        "inference_seconds": round(float(inference_seconds), 6),
        "postprocess_seconds": post_seconds,
        "batch_used": bool(batch_used),
        "effective_batch_size": int(effective_batch_size),
        "batching_mode": batching_mode,
    }
    if item.get("chapter_number") is not None:
        result["chapter_number"] = item["chapter_number"]
    return result


def _run_native_batch_with_oom_backoff(
    sdk: Any,
    batch_items: List[Dict[str, Any]],
    voice: str,
    sdk_lock: Optional[threading.Lock],
    oom_counter: Optional[List[int]],
) -> List[Tuple[Dict[str, Any], Any, float, int]]:
    """
    Executes `_sdk.infer_batch(...)` on `batch_items`, halving batch size on CUDA
    OOM (`batch_size = max(1, len // 2)`), clearing CUDA cache, and retrying the
    exact items. Never substitutes silence or placeholder audio.

    Returns list of `(item, audio_obj, per_item_infer_sec, effective_batch_size)`.
    """
    n = len(batch_items)
    if n == 0:
        return []

    texts = [item["text"] for item in batch_items]
    t0 = time.perf_counter()
    try:
        if sdk_lock is not None:
            with sdk_lock:
                wavs = _call_with_accepted_kwargs(
                    sdk.infer_batch, texts, voice=voice, batch_size=n
                )
        else:
            wavs = _call_with_accepted_kwargs(
                sdk.infer_batch, texts, voice=voice, batch_size=n
            )
        elapsed = time.perf_counter() - t0
        if not isinstance(wavs, list) or len(wavs) != n:
            raise RuntimeError(
                f"infer_batch returned {len(wavs) if hasattr(wavs, '__len__') else type(wavs)} "
                f"items for batch of {n}"
            )
        per_item = elapsed / float(n)
        return [(item, wav, per_item, n) for item, wav in zip(batch_items, wavs)]
    except Exception as exc:
        if not _is_cuda_oom(exc):
            raise
        if oom_counter is not None:
            oom_counter[0] += 1
        _clear_cuda_cache()
        if n <= 1:
            logger.error("[VieNeuCUDAWorker] Unrecoverable CUDA OOM at batch_size=1; aborting without placeholder.")
            raise

        next_bs = max(1, n // 2)
        logger.warning(
            f"[VieNeuCUDAWorker] CUDA OOM at batch_size={n}; clearing cache and retrying with batch_size={next_bs}."
        )
        combined: List[Tuple[Dict[str, Any], Any, float, int]] = []
        for start in range(0, n, next_bs):
            sub = batch_items[start : start + next_bs]
            combined.extend(
                _run_native_batch_with_oom_backoff(sdk, sub, voice, sdk_lock, oom_counter)
            )
        return combined


def run_sdk_batch_tasks(
    sdk: Any,
    tasks: List[Dict[str, Any]],
    *,
    mode: str = "v3turbo",
    batch_size: int = 8,
    cpu_workers: int = 4,
    sdk_lock: Optional[threading.Lock] = None,
    oom_counter: Optional[List[int]] = None,
) -> List[Dict[str, Any]]:
    """
    Shared core execution pipeline for `VieNeuCUDAWorker` and `NeuralTTSBackend`.
    """
    if not tasks:
        return []
    if sdk is None:
        raise RuntimeError("TTS SDK is not loaded")
    if int(batch_size) < 1:
        raise ValueError("batch_size must be >= 1")
    if int(cpu_workers) < 1:
        raise ValueError("cpu_workers must be >= 1")

    normalized = _validate_and_normalize_tasks(tasks)
    runtime = probe_vieneu_runtime(mode=mode, sdk=sdk)
    supports_batch = bool(runtime["supports_infer_batch"])
    batching_mode = str(runtime["batching_mode"])
    native_sr = int(getattr(sdk, "sample_rate", 48000))
    direct_waveform_ok = _uses_builtin_waveform_save(sdk)

    micro_batches = group_and_bucket_tasks(normalized, max_batch_size=int(batch_size))
    pool_workers = min(max(1, int(cpu_workers)), len(normalized))
    futures: Dict[int, Any] = {}
    prev_ooms = oom_counter[0] if oom_counter is not None else 0
    active_bs = int(batch_size)

    with ThreadPoolExecutor(max_workers=pool_workers) as pool:
        for group in micro_batches:
            voice = group[0]["voice"]
            if supports_batch:
                # If an earlier group triggered OOM, cap subsequent groups dynamically.
                idx = 0
                while idx < len(group):
                    step_bs = min(active_bs, len(group) - idx)
                    sub_group = group[idx : idx + step_bs]
                    idx += step_bs
                    batch_outputs = _run_native_batch_with_oom_backoff(
                        sdk, sub_group, voice, sdk_lock, oom_counter
                    )
                    if oom_counter is not None and oom_counter[0] > prev_ooms:
                        prev_ooms = oom_counter[0]
                        min_eff = min(eff for _, _, _, eff in batch_outputs)
                        active_bs = max(1, min(active_bs, min_eff))

                    for item, audio_obj, infer_sec, eff_bs in batch_outputs:
                        raw_path = _materialize_non_direct_raw(
                            sdk, audio_obj, item["output_path"], item["seq_id"], direct_waveform_ok, sdk_lock
                        )
                        fut = pool.submit(
                            _postprocess_cpu_item,
                            item=item,
                            waveform=audio_obj if raw_path is None else None,
                            native_sr=native_sr,
                            raw_path=raw_path,
                            inference_seconds=infer_sec,
                            batch_used=True,
                            effective_batch_size=eff_bs,
                            batching_mode=batching_mode,
                        )
                        futures[item["orig_idx"]] = fut
            else:
                if not callable(getattr(sdk, "infer", None)):
                    raise RuntimeError("TTS SDK lacks both infer_batch and infer")
                for item in group:
                    t0 = time.perf_counter()
                    if sdk_lock is not None:
                        with sdk_lock:
                            audio_obj = _call_with_accepted_kwargs(
                                sdk.infer, text=item["text"], voice=voice
                            )
                    else:
                        audio_obj = _call_with_accepted_kwargs(
                            sdk.infer, text=item["text"], voice=voice
                        )
                    infer_sec = time.perf_counter() - t0
                    raw_path = _materialize_non_direct_raw(
                        sdk, audio_obj, item["output_path"], item["seq_id"], direct_waveform_ok, sdk_lock
                    )
                    fut = pool.submit(
                        _postprocess_cpu_item,
                        item=item,
                        waveform=audio_obj if raw_path is None else None,
                        native_sr=native_sr,
                        raw_path=raw_path,
                        inference_seconds=infer_sec,
                        batch_used=False,
                        effective_batch_size=1,
                        batching_mode=batching_mode,
                    )
                    futures[item["orig_idx"]] = fut

        # Collect in strict original input order
        ordered_results: List[Dict[str, Any]] = []
        for orig_idx in range(len(normalized)):
            ordered_results.append(futures[orig_idx].result())
        return ordered_results


def _materialize_non_direct_raw(
    sdk: Any,
    audio_obj: Any,
    output_path: Path,
    seq_id: Any,
    direct_waveform_ok: bool,
    sdk_lock: Optional[threading.Lock],
) -> Optional[Path]:
    """
    If `audio_obj` is a plain numpy waveform and `sdk.save` is standard, returns None
    so CPU threads encode directly in memory. Otherwise saves via `sdk.save` to a
    unique temp WAV path for CPU normalization/validation.
    """
    is_ndarray = False
    try:
        import numpy as np  # noqa: PLC0415

        is_ndarray = isinstance(audio_obj, np.ndarray)
    except Exception:
        is_ndarray = False

    if direct_waveform_ok and is_ndarray:
        return None

    save_fn = getattr(sdk, "save", None)
    if not callable(save_fn):
        if is_ndarray:
            return None
        raise RuntimeError(f"Task {seq_id}: SDK returned non-waveform audio without callable save()")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path = output_path.with_name(f".{output_path.stem}.native.{uuid.uuid4().hex[:8]}.wav")
    if sdk_lock is not None:
        with sdk_lock:
            save_fn(audio_obj, str(raw_path))
    else:
        save_fn(audio_obj, str(raw_path))
    return raw_path


class VieNeuCUDAWorker:
    """
    Resident GPU/CPU worker wrapping VieNeu-TTS v3 Turbo for isolated `spawn`
    subprocesses or direct batch execution.
    """

    def __init__(
        self,
        gpu_id: int = 0,
        mode: str = "v3turbo",
        precision: str = "auto",
        preload: bool = False,
    ):
        self.gpu_id = int(gpu_id)
        self.mode = mode
        self.precision = precision
        self._sdk: Optional[Any] = None
        self._loaded: bool = False
        self._logical_cuda_index: int = 0
        self._runtime_info: Optional[Dict[str, Any]] = None
        self._lock = threading.Lock()
        self.init_duration_seconds: float = 0.0
        self.total_inference_seconds: float = 0.0
        self.total_postprocess_seconds: float = 0.0
        self.utterance_count: int = 0
        self.oom_events_count: int = 0

        if preload:
            self.load()

    def load(self, precision: Optional[str] = None) -> Dict[str, Any]:
        """Loads VieNeu into VRAM (or CPU/ONNX fallback when CUDA unavailable)."""
        with self._lock:
            eff_precision = precision if precision is not None else self.precision
            if self._sdk is not None and (precision is None or eff_precision == self.precision):
                if self._runtime_info is None:
                    self._runtime_info = probe_vieneu_runtime(mode=self.mode, sdk=self._sdk)
                return dict(self._runtime_info)

            if self._sdk is not None:
                self._release_sdk_locked()

            self.precision = eff_precision
            hf_cache = Path(os.getenv("HF_HOME", Path.home() / ".cache" / "huggingface" / "hub"))
            has_vieneu_weights = hf_cache.exists() and any(
                "vieneu" in p.name.lower() for p in hf_cache.iterdir()
            )
            if has_vieneu_weights:
                os.environ["HF_HUB_OFFLINE"] = "1"
                os.environ["TRANSFORMERS_OFFLINE"] = "1"

            torch_device = "auto"
            try:
                import torch  # noqa: PLC0415

                if getattr(torch, "cuda", None) is not None and torch.cuda.is_available():
                    count = int(torch.cuda.device_count())
                    logical_idx = self.gpu_id if 0 <= self.gpu_id < count else 0
                    torch.cuda.set_device(logical_idx)
                    self._logical_cuda_index = logical_idx
                    torch_device = f"cuda:{logical_idx}" if logical_idx > 0 else "auto"
            except Exception:
                self._logical_cuda_index = 0

            dtype_arg, onnx_prec_arg = _normalize_precision_kwargs(self.precision)
            optimal_threads = min(max((os.cpu_count() or 8) // 2, 2), 6)

            t0 = time.monotonic()
            from vieneu import Vieneu  # noqa: PLC0415

            sdk_kwargs: Dict[str, Any] = {
                "threads": optimal_threads,
                "device": torch_device,
                "dtype": dtype_arg,
                "precision": onnx_prec_arg,
            }
            self._sdk = _call_with_accepted_kwargs(Vieneu, mode=self.mode, **sdk_kwargs)
            self.init_duration_seconds = round(time.monotonic() - t0, 3)
            self._loaded = True
            self._runtime_info = probe_vieneu_runtime(mode=self.mode, sdk=self._sdk)
            return dict(self._runtime_info)

    def _release_sdk_locked(self) -> None:
        sdk = self._sdk
        self._sdk = None
        self._loaded = False
        self._runtime_info = None
        if sdk is not None and hasattr(sdk, "close") and callable(sdk.close):
            try:
                sdk.close()
            except Exception as exc:
                logger.debug(f"[VieNeuCUDAWorker] SDK close notice: {exc}")

    def unload(self) -> None:
        """Releases model references, invokes gc.collect() and torch.cuda.empty_cache()."""
        with self._lock:
            had_sdk = self._sdk is not None
            self._release_sdk_locked()
        gc.collect()
        if had_sdk or "torch" in sys.modules:
            _clear_cuda_cache()

    def vram_usage_mb(self) -> Dict[str, float]:
        """Returns current CUDA VRAM usage in MB for this worker's device."""
        return _collect_vram_metrics(self._logical_cuda_index)

    def probe_runtime(self) -> Dict[str, Any]:
        """Inspects current runtime capability (using loaded SDK if resident)."""
        return probe_vieneu_runtime(mode=self.mode, sdk=self._sdk)

    def synthesize_batch_tasks(
        self,
        tasks: List[Dict[str, Any]],
        *,
        batch_size: int = 8,
        precision: str = "auto",
        cpu_workers: int = 4,
    ) -> List[Dict[str, Any]]:
        """
        Synthesizes tasks in `(voice, speed_key)` and length-bucketed micro-batches,
        offloading CPU post-processing and returning results strictly in original order.
        """
        if not tasks:
            return []

        if self._sdk is None:
            self.load(precision=precision)

        oom_counter = [self.oom_events_count]
        results = run_sdk_batch_tasks(
            self._sdk,
            tasks,
            mode=self.mode,
            batch_size=batch_size,
            cpu_workers=cpu_workers,
            sdk_lock=self._lock,
            oom_counter=oom_counter,
        )
        self.oom_events_count = oom_counter[0]
        self.total_inference_seconds += sum(float(r.get("inference_seconds", 0.0)) for r in results)
        self.total_postprocess_seconds += sum(float(r.get("postprocess_seconds", 0.0)) for r in results)
        self.utterance_count += len(results)
        return results


def _safe_queue_put(q: Any, payload: Dict[str, Any], lock: threading.Lock) -> None:
    if q is None:
        return
    with lock:
        q.put(payload)


def tts_worker_loop(
    physical_gpu_id: Union[int, str] = 0,
    task_queue: Any = None,
    result_queue: Any = None,
    *,
    mode: str = "v3turbo",
    precision: str = "auto",
    batch_size: int = 8,
    cpu_workers: int = 4,
    preload: bool = True,
    initial_tasks: Optional[List[Dict[str, Any]]] = None,
    heartbeat_interval_sec: float = 15.0,
    worker_factory: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """
    Top-level `spawn`-picklable worker entrypoint for `multiprocessing.get_context("spawn")`.

    Sets `os.environ["CUDA_VISIBLE_DEVICES"] = str(physical_gpu_id)` as its VERY
    FIRST action before importing `torch` or `vieneu`.
    """
    os.environ["CUDA_VISIBLE_DEVICES"] = str(physical_gpu_id)

    put_lock = threading.Lock()
    factory = worker_factory or VieNeuCUDAWorker
    worker = factory(gpu_id=0, mode=mode, precision=precision, preload=False)
    jobs_completed = 0

    def _emit_event(event_name: str, **extra: Any) -> None:
        payload = {
            "event": event_name,
            "worker": "tts",
            "physical_gpu_id": str(physical_gpu_id),
            "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
            "pid": os.getpid(),
            "vram": worker.vram_usage_mb(),
            "timestamp": time.time(),
            **extra,
        }
        _safe_queue_put(result_queue, payload, put_lock)

    try:
        runtime_info = (
            worker.load(precision=precision)
            if preload
            else worker.probe_runtime()
        )
        _emit_event("ready", logical_gpu_id=0, runtime=runtime_info)

        def _execute_job(job_id: Any, tasks: List[Dict[str, Any]], req_bs: int, req_prec: str, req_cpu: int) -> None:
            nonlocal jobs_completed
            stop_hb = threading.Event()

            def _emit_busy_hb() -> None:
                _emit_event(
                    "heartbeat",
                    status="busy",
                    job_id=job_id,
                    utterance_count=getattr(worker, "utterance_count", 0),
                )

            def _hb_runner() -> None:
                interval = max(0.05, float(heartbeat_interval_sec))
                while not stop_hb.wait(interval):
                    _emit_busy_hb()

            _emit_busy_hb()
            hb_thread = threading.Thread(target=_hb_runner, daemon=True)
            hb_thread.start()
            try:
                results = worker.synthesize_batch_tasks(
                    tasks,
                    batch_size=req_bs,
                    precision=req_prec,
                    cpu_workers=req_cpu,
                )
                jobs_completed += 1
                _emit_event(
                    "result",
                    job_id=job_id,
                    ok=True,
                    results=results,
                    oom_events_count=getattr(worker, "oom_events_count", 0),
                )
            except Exception as exc:
                _emit_event(
                    "error",
                    job_id=job_id,
                    ok=False,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
            finally:
                stop_hb.set()
                hb_thread.join(timeout=1.0)

        if initial_tasks:
            _execute_job("initial", initial_tasks, batch_size, precision, cpu_workers)

        if task_queue is not None:
            poll_timeout = max(0.05, float(heartbeat_interval_sec))
            while True:
                try:
                    item = task_queue.get(timeout=poll_timeout)
                except stdlib_queue.Empty:
                    _emit_event(
                        "heartbeat",
                        status="idle",
                        utterance_count=getattr(worker, "utterance_count", 0),
                    )
                    continue

                if item is None:
                    break
                if isinstance(item, dict):
                    cmd = str(item.get("cmd") or item.get("event") or "synthesize_batch").lower()
                    if cmd in {"stop", "shutdown", "exit"}:
                        break
                    if cmd == "unload":
                        worker.unload()
                        _emit_event("unloaded")
                        continue
                    if cmd == "probe":
                        _emit_event("probe", runtime=worker.probe_runtime())
                        continue

                    tasks = item.get("tasks") or []
                    _execute_job(
                        item.get("job_id"),
                        tasks,
                        int(item.get("batch_size", batch_size)),
                        str(item.get("precision", precision)),
                        int(item.get("cpu_workers", cpu_workers)),
                    )
                else:
                    raise ValueError(f"Unsupported task queue payload type: {type(item).__name__}")
    except Exception as exc:
        _emit_event("error", stage="loop", ok=False, error=str(exc), error_type=type(exc).__name__)
        raise
    finally:
        try:
            worker.unload()
        except Exception:
            pass
        _emit_event(
            "stopped",
            jobs_completed=jobs_completed,
            utterance_count=getattr(worker, "utterance_count", 0),
        )

    return {
        "physical_gpu_id": str(physical_gpu_id),
        "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
        "jobs_completed": jobs_completed,
        "utterance_count": getattr(worker, "utterance_count", 0),
    }
