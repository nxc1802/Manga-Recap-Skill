#!/usr/bin/env python3
"""P8.0 Dual-T4 Hardware & SDK Capability Probe.

Standalone capability probe for verifying Kaggle Dual-T4 GPU topology, PyTorch /
ONNX Runtime execution providers, isolated per-GPU VieNeu-TTS v3 Turbo loading
and inference/batching, and isolated per-GPU FFmpeg NVENC + crop encoding.

Modes:
  --probe-local / --local   Run the hardware & SDK probe on the current machine.
  --dispatch                Push and monitor a lightweight probe-only Kaggle GPU
                            kernel (supports --dry-run for local validation).
  (no flags inside Kaggle)  Automatically runs --probe-local and writes
                            /kaggle/working/dual_t4_probe.json.
"""

from __future__ import annotations

import argparse
import datetime
import importlib.metadata
import inspect
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


SCHEMA_VERSION = "p8.0-probe-1.0"
DEFAULT_KAGGLE_OUTPUT = Path("/kaggle/working/dual_t4_probe.json")
SHORT_VI_UTTERANCE_1 = "Xin chào, đây là bài kiểm tra hiệu năng giọng đọc trên GPU."
SHORT_VI_UTTERANCE_2 = "Màn đêm buông xuống, trận chiến sinh tử chính thức bắt đầu."
BATCH_VI_UTTERANCES = [
    "Cánh cổng đá khổng lồ từ từ mở ra trong làn sương mù dày đặc.",
    "Hắn siết chặt thanh kiếm trong tay, ánh mắt lạnh băng nhìn thẳng về phía trước.",
    "Không thể nào! Sao ngươi lại có thể vượt qua kết giới của tông môn?",
    "Cuộc đối đầu định mệnh giữa hai cao thủ cuối cùng đã không thể tránh khỏi.",
]
PREFERRED_VOICE = "Ngọc Huyền"

_SECRET_ENV_KEYS = (
    "KAGGLE_API_TOKEN",
    "KAGGLE_KEY",
    "KAGGLE_USERNAME",
    "HF_TOKEN",
    "HUGGING_FACE_HUB_TOKEN",
    "HUGGINGFACE_TOKEN",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
)

_TOKEN_PATTERNS = (
    re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]{12,}", re.IGNORECASE),
    re.compile(r"\bhf_[A-Za-z0-9]{12,}\b"),
    re.compile(r"\bKGAT_[A-Za-z0-9_\-]{12,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"),
)


def _utc_iso_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _in_kaggle_session() -> bool:
    return bool(os.environ.get("KAGGLE_KERNEL_RUN_TYPE") or Path("/kaggle/working").is_dir())


def _redact_secrets(text: Optional[str]) -> str:
    if not text:
        return ""
    cleaned = str(text)
    for key in _SECRET_ENV_KEYS:
        if key == "KAGGLE_USERNAME":
            continue
        val = os.environ.get(key)
        if val and len(val) >= 6:
            cleaned = cleaned.replace(val, "[REDACTED]")
    for pat in _TOKEN_PATTERNS:
        cleaned = pat.sub(lambda m: f"{m.group(1)}[REDACTED]" if m.lastindex else "[REDACTED]", cleaned)
    return cleaned


def _sanitize_payload(obj: Any) -> Any:
    if isinstance(obj, str):
        return _redact_secrets(obj)
    if isinstance(obj, dict):
        return {str(k): _sanitize_payload(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_payload(v) for v in obj]
    return obj


def _atomic_write_json(path: Path, data: Any) -> Path:
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    sanitized = _sanitize_payload(data)
    tmp.write_text(json.dumps(sanitized, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(target)
    return target


def _pkg_version(name: str) -> Optional[str]:
    try:
        return importlib.metadata.version(name)
    except Exception:
        return None


def _run_cmd(cmd: List[str], env: Optional[Dict[str, str]] = None, timeout: int = 30) -> Dict[str, Any]:
    t0 = time.perf_counter()
    try:
        res = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return {
            "ok": res.returncode == 0,
            "returncode": res.returncode,
            "stdout": _redact_secrets(res.stdout),
            "stderr": _redact_secrets(res.stderr),
            "duration_seconds": round(time.perf_counter() - t0, 3),
        }
    except FileNotFoundError as exc:
        return {
            "ok": False,
            "returncode": None,
            "stdout": "",
            "stderr": _redact_secrets(str(exc)),
            "duration_seconds": round(time.perf_counter() - t0, 3),
            "missing_binary": True,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "returncode": None,
            "stdout": _redact_secrets(exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")),
            "stderr": f"Command timed out after {timeout}s",
            "duration_seconds": round(time.perf_counter() - t0, 3),
            "timed_out": True,
        }


def _hf_cache_has_vieneu() -> bool:
    hf_home = Path(os.environ.get("HF_HOME", str(Path.home() / ".cache" / "huggingface")))
    hub_dir = hf_home / "hub" if (hf_home / "hub").is_dir() else hf_home
    if not hub_dir.is_dir():
        return False
    try:
        return any("vieneu" in p.name.lower() for p in hub_dir.iterdir())
    except Exception:
        return False


def _collect_host_info() -> Dict[str, Any]:
    return {
        "timestamp_utc": _utc_iso_now(),
        "platform": platform.platform(),
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
        "python_version": platform.python_version(),
        "python_executable": sys.executable,
        "in_kaggle": _in_kaggle_session(),
        "kaggle_kernel_run_type": os.environ.get("KAGGLE_KERNEL_RUN_TYPE"),
        "initial_cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
        "hf_cache_preexisting": _hf_cache_has_vieneu(),
        "packages": {
            "vieneu": _pkg_version("vieneu"),
            "torch": _pkg_version("torch"),
            "transformers": _pkg_version("transformers"),
            "safetensors": _pkg_version("safetensors"),
            "onnxruntime": _pkg_version("onnxruntime"),
            "onnxruntime_gpu": _pkg_version("onnxruntime-gpu"),
            "huggingface_hub": _pkg_version("huggingface_hub"),
            "numpy": _pkg_version("numpy"),
            "kaggle": _pkg_version("kaggle"),
        },
    }


def _collect_nvidia_smi() -> Dict[str, Any]:
    binary = shutil.which("nvidia-smi")
    if not binary:
        return {
            "available": False,
            "binary": None,
            "raw_list": [],
            "gpus": [],
            "gpu_count": 0,
            "driver_version": None,
            "cuda_driver_version": None,
            "nvcc_version": None,
        }

    list_res = _run_cmd([binary, "-L"], timeout=15)
    raw_list = [line.strip() for line in list_res["stdout"].splitlines() if line.strip()]

    query_res = _run_cmd(
        [
            binary,
            "--query-gpu=index,name,uuid,memory.total,memory.free,memory.used,driver_version,pci.bus_id,compute_cap",
            "--format=csv,noheader,nounits",
        ],
        timeout=15,
    )
    gpus: List[Dict[str, Any]] = []
    driver_version: Optional[str] = None
    if query_res["ok"]:
        for line in query_res["stdout"].splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 9:
                continue
            driver_version = driver_version or parts[6]
            gpus.append(
                {
                    "index": int(parts[0]) if parts[0].isdigit() else parts[0],
                    "name": parts[1],
                    "uuid": parts[2],
                    "vram_total_mb": _to_float_or_none(parts[3]),
                    "vram_free_mb": _to_float_or_none(parts[4]),
                    "vram_used_mb": _to_float_or_none(parts[5]),
                    "driver_version": parts[6],
                    "pci_bus_id": parts[7],
                    "compute_cap": parts[8],
                }
            )

    summary_res = _run_cmd([binary], timeout=15)
    cuda_driver_version = None
    if summary_res["ok"]:
        m = re.search(r"CUDA Version:\s*([0-9.]+)", summary_res["stdout"])
        if m:
            cuda_driver_version = m.group(1)
        if not driver_version:
            dm = re.search(r"Driver Version:\s*([0-9.]+)", summary_res["stdout"])
            if dm:
                driver_version = dm.group(1)

    nvcc_version = None
    nvcc_bin = shutil.which("nvcc")
    if nvcc_bin:
        nvcc_res = _run_cmd([nvcc_bin, "--version"], timeout=10)
        if nvcc_res["ok"]:
            m = re.search(r"release\s+([0-9.]+)", nvcc_res["stdout"])
            nvcc_version = m.group(1) if m else nvcc_res["stdout"].strip()

    return {
        "available": list_res["ok"] or query_res["ok"],
        "binary": binary,
        "raw_list": raw_list,
        "list_error": None if list_res["ok"] else list_res["stderr"],
        "gpus": gpus,
        "gpu_count": len(gpus) if gpus else len(raw_list),
        "driver_version": driver_version,
        "cuda_driver_version": cuda_driver_version,
        "nvcc_version": nvcc_version,
    }


def _to_float_or_none(val: str) -> Optional[float]:
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _collect_torch_summary() -> Dict[str, Any]:
    try:
        import torch  # type: ignore
    except Exception as exc:
        return {
            "installed": False,
            "error": _redact_secrets(str(exc)),
            "cuda_available": False,
            "device_count": 0,
            "devices": [],
        }

    cuda_avail = bool(torch.cuda.is_available())
    count = int(torch.cuda.device_count()) if cuda_avail else 0
    devices: List[Dict[str, Any]] = []
    for idx in range(count):
        try:
            props = torch.cuda.get_device_properties(idx)
            free_b, total_b = torch.cuda.mem_get_info(idx)
            devices.append(
                {
                    "index": idx,
                    "name": props.name,
                    "uuid": str(getattr(props, "uuid", "")) or None,
                    "compute_capability": f"{props.major}.{props.minor}",
                    "multi_processor_count": int(props.multi_processor_count),
                    "total_vram_mb": round(total_b / (1024.0 * 1024.0), 1),
                    "free_vram_mb": round(free_b / (1024.0 * 1024.0), 1),
                }
            )
        except Exception as exc:
            devices.append({"index": idx, "error": _redact_secrets(str(exc))})

    bf16_supported = None
    if cuda_avail and hasattr(torch.cuda, "is_bf16_supported"):
        try:
            bf16_supported = bool(torch.cuda.is_bf16_supported())
        except Exception:
            bf16_supported = None

    cudnn_ver = None
    try:
        if hasattr(torch.backends, "cudnn") and torch.backends.cudnn.is_available():
            cudnn_ver = torch.backends.cudnn.version()
    except Exception:
        cudnn_ver = None

    return {
        "installed": True,
        "version": getattr(torch, "__version__", None),
        "cuda_runtime_version": getattr(getattr(torch, "version", None), "cuda", None),
        "cudnn_version": cudnn_ver,
        "cuda_available": cuda_avail,
        "device_count": count,
        "bf16_supported": bf16_supported,
        "devices": devices,
    }


def _collect_ort_summary() -> Dict[str, Any]:
    try:
        import onnxruntime as ort  # type: ignore
    except Exception as exc:
        return {
            "installed": False,
            "error": _redact_secrets(str(exc)),
            "available_providers": [],
        }

    providers: List[str] = []
    try:
        providers = list(ort.get_available_providers())
    except Exception as exc:
        return {
            "installed": True,
            "version": getattr(ort, "__version__", None),
            "onnxruntime_gpu_version": _pkg_version("onnxruntime-gpu"),
            "available_providers": [],
            "error": _redact_secrets(str(exc)),
        }

    return {
        "installed": True,
        "version": getattr(ort, "__version__", None),
        "onnxruntime_gpu_version": _pkg_version("onnxruntime-gpu"),
        "available_providers": providers,
        "cuda_provider_available": "CUDAExecutionProvider" in providers,
        "tensorrt_provider_available": "TensorrtExecutionProvider" in providers,
    }


def _parse_ffmpeg_encoders(stdout: str) -> List[str]:
    encoders: List[str] = []
    past_sep = False
    for line in stdout.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("------"):
            past_sep = True
            continue
        if not past_sep:
            continue
        parts = s.split()
        if len(parts) >= 2 and parts[0].startswith("V"):
            encoders.append(parts[1])
    return encoders


def _parse_ffmpeg_filters(stdout: str) -> List[str]:
    filters: List[str] = []
    for line in stdout.splitlines():
        s = line.strip()
        if not s or s.startswith("Filters:") or s.startswith("T.. =") or s.startswith("..C =") or s.startswith("..."):
            continue
        parts = s.split()
        if len(parts) >= 3 and "->" in parts[2]:
            filters.append(parts[1])
    return filters


def _collect_ffmpeg_inventory() -> Dict[str, Any]:
    ffmpeg_bin = shutil.which("ffmpeg")
    ffprobe_bin = shutil.which("ffprobe")
    if not ffmpeg_bin:
        return {
            "ffmpeg_available": False,
            "ffprobe_available": bool(ffprobe_bin),
            "version": None,
            "encoders_checked": {},
            "filters_checked": {},
            "hwaccels": [],
        }

    ver_res = _run_cmd([ffmpeg_bin, "-hide_banner", "-version"], timeout=10)
    version_line = ver_res["stdout"].splitlines()[0].strip() if ver_res["stdout"] else None

    enc_res = _run_cmd([ffmpeg_bin, "-hide_banner", "-encoders"], timeout=10)
    all_encoders = set(_parse_ffmpeg_encoders(enc_res["stdout"])) if enc_res["ok"] else set()
    target_encoders = ["h264_nvenc", "hevc_nvenc", "libx264", "h264_videotoolbox"]

    flt_res = _run_cmd([ffmpeg_bin, "-hide_banner", "-filters"], timeout=10)
    all_filters = set(_parse_ffmpeg_filters(flt_res["stdout"])) if flt_res["ok"] else set()
    target_filters = [
        "scale_cuda",
        "scale_npp",
        "crop",
        "zoompan",
        "scale",
        "boxblur",
        "gblur",
        "overlay",
        "overlay_cuda",
        "hwupload_cuda",
        "hwdownload",
        "subtitles",
        "ass",
    ]

    hwa_res = _run_cmd([ffmpeg_bin, "-hide_banner", "-hwaccels"], timeout=10)
    hwaccels: List[str] = []
    if hwa_res["ok"]:
        for line in hwa_res["stdout"].splitlines():
            item = line.strip()
            if item and not item.lower().startswith("hardware acceleration"):
                hwaccels.append(item)

    return {
        "ffmpeg_available": True,
        "ffmpeg_binary": ffmpeg_bin,
        "ffprobe_available": bool(ffprobe_bin),
        "ffprobe_binary": ffprobe_bin,
        "version": version_line,
        "hwaccels": hwaccels,
        "encoders_checked": {name: (name in all_encoders) for name in target_encoders},
        "filters_checked": {name: (name in all_filters) for name in target_filters},
    }


def _ensure_vieneu_installed(auto_install: bool) -> Dict[str, Any]:
    try:
        import vieneu  # noqa: F401
        return {
            "already_installed": True,
            "install_attempted": False,
            "installed": True,
            "version": _pkg_version("vieneu"),
        }
    except ImportError as exc:
        if not auto_install:
            return {
                "already_installed": False,
                "install_attempted": False,
                "installed": False,
                "error": _redact_secrets(str(exc)),
            }

    res = _run_cmd([sys.executable, "-m", "pip", "install", "-q", "vieneu>=3.8.3"], timeout=300)
    return {
        "already_installed": False,
        "install_attempted": True,
        "installed": res["ok"],
        "duration_seconds": res["duration_seconds"],
        "version": _pkg_version("vieneu") if res["ok"] else None,
        "error": None if res["ok"] else res["stderr"][-1500:],
    }


def _get_session_providers(sess: Any) -> Optional[List[str]]:
    if sess is None:
        return None
    if hasattr(sess, "get_providers"):
        return list(sess.get_providers())
    if hasattr(sess, "session") and hasattr(sess.session, "get_providers"):
        return list(sess.session.get_providers())
    if hasattr(sess, "sess") and hasattr(sess.sess, "get_providers"):
        return list(sess.sess.get_providers())
    return None


def _waveform_stats(wav: Any, sample_rate: int) -> Dict[str, Any]:
    import numpy as np  # type: ignore

    arr = np.asarray(wav, dtype=np.float32).reshape(-1)
    n = int(arr.shape[0])
    dur = round(n / float(sample_rate), 4) if sample_rate > 0 else 0.0
    rms = round(float(np.sqrt(np.mean(arr * arr))), 6) if n > 0 else 0.0
    peak = round(float(np.max(np.abs(arr))), 6) if n > 0 else 0.0
    return {
        "sample_count": n,
        "sample_rate": sample_rate,
        "duration_seconds": dur,
        "rms": rms,
        "peak_abs": peak,
        "non_silent": bool(n > 0 and rms > 1e-4),
    }


def _worker_vieneu(gpu_id: int) -> Dict[str, Any]:
    report: Dict[str, Any] = {
        "requested_gpu_id": gpu_id,
        "env_cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
        "torch": _collect_torch_summary(),
        "onnxruntime": _collect_ort_summary(),
        "sdk_inspection": {},
        "initialization": {},
        "inference_single_cold": {},
        "inference_single_warm": {},
        "inference_batch_4": {},
        "vram_after": {},
    }

    torch_mod = None
    if report["torch"].get("installed"):
        import torch as torch_mod  # type: ignore

    if torch_mod is not None and report["torch"].get("cuda_available"):
        try:
            torch_mod.cuda.reset_peak_memory_stats(0)
        except Exception:
            pass

    try:
        from vieneu import Vieneu  # type: ignore
        from vieneu.v3turbo import V3TurboVieNeuTTS  # type: ignore
    except Exception as exc:
        report["initialization"] = {"ok": False, "error": f"Failed to import vieneu: {_redact_secrets(str(exc))}"}
        return report

    report["sdk_inspection"] = {
        "vieneu_version": _pkg_version("vieneu"),
        "factory_signature": str(inspect.signature(Vieneu)),
        "v3turbo_init_signature": str(inspect.signature(V3TurboVieNeuTTS.__init__)),
        "has_infer": hasattr(V3TurboVieNeuTTS, "infer"),
        "has_infer_batch": hasattr(V3TurboVieNeuTTS, "infer_batch"),
        "has_infer_stream": hasattr(V3TurboVieNeuTTS, "infer_stream"),
    }

    optimal_threads = min(max((os.cpu_count() or 8) // 2, 2), 6)
    tts = None
    t_init_0 = time.perf_counter()
    try:
        tts = Vieneu(mode="v3turbo", threads=optimal_threads)
        init_sec = round(time.perf_counter() - t_init_0, 3)
    except Exception as exc:
        report["initialization"] = {
            "ok": False,
            "init_seconds": round(time.perf_counter() - t_init_0, 3),
            "error": _redact_secrets(str(exc)),
        }
        return report

    try:
        engine = getattr(tts, "engine", None)
        backend_name = getattr(tts, "backend", None)
        engine_cls = type(engine).__name__ if engine is not None else None
        raw_engine_dev = getattr(engine, "device", None) if engine is not None else None
        if raw_engine_dev is None:
            engine_dev = None
        elif type(raw_engine_dev).__name__ == "_Dev":
            engine_dev = getattr(raw_engine_dev, "type", "cpu")
        else:
            engine_dev = str(raw_engine_dev)
        raw_engine_dtype = getattr(engine, "dtype", None) if engine is not None else None
        engine_dtype = str(raw_engine_dtype) if raw_engine_dtype is not None else None

        model_param_device = None
        model_param_dtype = None
        codec_param_device = None
        codec_param_dtype = None
        if engine is not None and hasattr(engine, "model"):
            try:
                p = next(engine.model.parameters())
                model_param_device = str(p.device)
                model_param_dtype = str(p.dtype)
            except Exception:
                pass
        if engine is not None and hasattr(engine, "audio_tokenizer"):
            try:
                cp = next(engine.audio_tokenizer.parameters())
                codec_param_device = str(cp.device)
                codec_param_dtype = str(cp.dtype)
            except Exception:
                pass

        sub_providers: Dict[str, Any] = {}
        if engine is not None:
            for attr in ("sess_pre", "sess_dec", "sess_ac", "sess_codec_dec", "speaker_encoder", "denoiser"):
                val = getattr(engine, attr, None)
                if val is not None:
                    sub_providers[attr] = _get_session_providers(val)

        cuda_expected = bool(report["torch"].get("cuda_available"))
        cuda_actually_used = bool(
            backend_name == "pytorch" and model_param_device and "cuda" in model_param_device.lower()
        )
        if cuda_actually_used:
            actual_provider = "pytorch_cuda"
        elif backend_name == "onnx":
            actual_provider = "onnx_cpu_fallback" if cuda_expected else "onnx_cpu"
        else:
            actual_provider = f"{backend_name}_{model_param_device or engine_dev or 'unknown'}"

        report["initialization"] = {
            "ok": True,
            "init_seconds": init_sec,
            "tts_backend": backend_name,
            "engine_class": engine_cls,
            "engine_device": engine_dev,
            "engine_dtype": engine_dtype,
            "model_param_device": model_param_device,
            "model_param_dtype": model_param_dtype,
            "codec_param_device": codec_param_device,
            "codec_param_dtype": codec_param_dtype,
            "sub_session_providers": sub_providers,
            "actual_execution_provider": actual_provider,
            "cuda_expected": cuda_expected,
            "cuda_actually_used": cuda_actually_used,
            "fell_back_to_cpu": bool(cuda_expected and not cuda_actually_used),
            "max_batch_size": getattr(tts, "max_batch_size", None),
            "max_streams": getattr(tts, "max_streams", None),
        }

        voice_used = PREFERRED_VOICE
        try:
            presets = tts.list_preset_voices()
            preset_ids = [item[1] for item in presets]
            if PREFERRED_VOICE not in preset_ids and preset_ids:
                voice_used = preset_ids[0]
        except Exception:
            pass

        sr = int(getattr(tts, "sample_rate", 48000))

        # 1. Cold single utterance inference
        try:
            if torch_mod is not None and cuda_actually_used:
                torch_mod.cuda.synchronize()
            t0 = time.perf_counter()
            wav1 = tts.infer(text=SHORT_VI_UTTERANCE_1, voice=voice_used)
            if torch_mod is not None and cuda_actually_used:
                torch_mod.cuda.synchronize()
            elapsed1 = round(time.perf_counter() - t0, 4)
            stats1 = _waveform_stats(wav1, sr)
            rtf1 = round(elapsed1 / stats1["duration_seconds"], 4) if stats1["duration_seconds"] > 0 else None
            report["inference_single_cold"] = {
                "ok": stats1["non_silent"],
                "voice": voice_used,
                "infer_seconds": elapsed1,
                "rtf": rtf1,
                **stats1,
            }
        except Exception as exc:
            report["inference_single_cold"] = {"ok": False, "voice": voice_used, "error": _redact_secrets(str(exc))}

        # 2. Warm single utterance inference
        try:
            if torch_mod is not None and cuda_actually_used:
                torch_mod.cuda.synchronize()
            t0 = time.perf_counter()
            wav2 = tts.infer(text=SHORT_VI_UTTERANCE_2, voice=voice_used)
            if torch_mod is not None and cuda_actually_used:
                torch_mod.cuda.synchronize()
            elapsed2 = round(time.perf_counter() - t0, 4)
            stats2 = _waveform_stats(wav2, sr)
            rtf2 = round(elapsed2 / stats2["duration_seconds"], 4) if stats2["duration_seconds"] > 0 else None
            report["inference_single_warm"] = {
                "ok": stats2["non_silent"],
                "voice": voice_used,
                "infer_seconds": elapsed2,
                "rtf": rtf2,
                **stats2,
            }
        except Exception as exc:
            report["inference_single_warm"] = {"ok": False, "voice": voice_used, "error": _redact_secrets(str(exc))}

        # 3. Batched inference probe (infer_batch with 4 short Vietnamese utterances)
        if hasattr(tts, "infer_batch"):
            try:
                fused_avail = bool(tts._fused_available()) if hasattr(tts, "_fused_available") else None
                if torch_mod is not None and cuda_actually_used:
                    torch_mod.cuda.synchronize()
                t0 = time.perf_counter()
                batch_wavs = tts.infer_batch(BATCH_VI_UTTERANCES, voice=voice_used, batch_size=len(BATCH_VI_UTTERANCES))
                if torch_mod is not None and cuda_actually_used:
                    torch_mod.cuda.synchronize()
                batch_elapsed = round(time.perf_counter() - t0, 4)
                item_stats = [_waveform_stats(w, sr) for w in batch_wavs]
                total_audio_sec = round(sum(s["duration_seconds"] for s in item_stats), 4)
                all_non_silent = len(item_stats) == len(BATCH_VI_UTTERANCES) and all(s["non_silent"] for s in item_stats)
                batch_engine = getattr(tts, "_batch_engine", None)
                fused_graph_count = len(getattr(batch_engine, "_fused", {}) or {}) if batch_engine is not None else 0
                report["inference_batch_4"] = {
                    "ok": all_non_silent,
                    "voice": voice_used,
                    "item_count": len(item_stats),
                    "infer_seconds": batch_elapsed,
                    "total_audio_seconds": total_audio_sec,
                    "batch_rtf": round(batch_elapsed / total_audio_sec, 4) if total_audio_sec > 0 else None,
                    "all_non_silent": all_non_silent,
                    "batch_engine_class": type(batch_engine).__name__ if batch_engine is not None else None,
                    "batch_engine_use_fused": getattr(batch_engine, "use_fused", None) if batch_engine is not None else None,
                    "fused_cuda_graph_available": fused_avail,
                    "fused_graphs_captured": fused_graph_count,
                    "per_item_durations": [s["duration_seconds"] for s in item_stats],
                }
            except Exception as exc:
                report["inference_batch_4"] = {
                    "ok": False,
                    "voice": voice_used,
                    "error": _redact_secrets(str(exc)),
                }

        if torch_mod is not None and cuda_actually_used:
            try:
                free_b, total_b = torch_mod.cuda.mem_get_info(0)
                report["vram_after"] = {
                    "allocated_mb": round(torch_mod.cuda.memory_allocated(0) / (1024.0 * 1024.0), 1),
                    "peak_allocated_mb": round(torch_mod.cuda.max_memory_allocated(0) / (1024.0 * 1024.0), 1),
                    "reserved_mb": round(torch_mod.cuda.memory_reserved(0) / (1024.0 * 1024.0), 1),
                    "free_mb": round(free_b / (1024.0 * 1024.0), 1),
                    "total_mb": round(total_b / (1024.0 * 1024.0), 1),
                }
            except Exception as exc:
                report["vram_after"] = {"error": _redact_secrets(str(exc))}
    finally:
        if tts is not None and hasattr(tts, "close"):
            try:
                tts.close()
            except Exception:
                pass

    return report


def _probe_mp4_with_ffprobe(ffprobe_bin: Optional[str], mp4_path: Path) -> Dict[str, Any]:
    if not ffprobe_bin:
        return {"ok": False, "error": "ffprobe not found"}
    res = _run_cmd(
        [
            ffprobe_bin,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,width,height,nb_frames,pix_fmt,avg_frame_rate",
            "-show_entries",
            "format=duration,size",
            "-of",
            "json",
            str(mp4_path),
        ],
        timeout=15,
    )
    if not res["ok"]:
        return {"ok": False, "error": res["stderr"]}
    try:
        parsed = json.loads(res["stdout"])
        stream = (parsed.get("streams") or [{}])[0]
        fmt = parsed.get("format") or {}
        return {
            "ok": True,
            "codec_name": stream.get("codec_name"),
            "width": stream.get("width"),
            "height": stream.get("height"),
            "nb_frames": stream.get("nb_frames"),
            "pix_fmt": stream.get("pix_fmt"),
            "avg_frame_rate": stream.get("avg_frame_rate"),
            "duration": fmt.get("duration"),
            "size_bytes": int(fmt["size"]) if str(fmt.get("size", "")).isdigit() else mp4_path.stat().st_size,
        }
    except Exception as exc:
        return {"ok": False, "error": _redact_secrets(str(exc))}


def _extract_nvenc_log_lines(stderr: str) -> List[str]:
    interesting: List[str] = []
    for line in stderr.splitlines():
        low = line.lower()
        if any(tok in low for tok in ("nvenc", "cuda", "gpu", "session", "scale_cuda", "error", "cannot", "failed")):
            interesting.append(line.strip())
    return interesting[-20:]


def _worker_nvenc(gpu_id: int) -> Dict[str, Any]:
    inv = _collect_ffmpeg_inventory()
    torch_vis = _collect_torch_summary()
    report: Dict[str, Any] = {
        "requested_gpu_id": gpu_id,
        "env_cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
        "visible_cuda_devices": torch_vis.get("devices", []),
        "ffmpeg_available": inv["ffmpeg_available"],
        "h264_nvenc_listed": bool(inv["encoders_checked"].get("h264_nvenc")),
        "crop_listed": bool(inv["filters_checked"].get("crop")),
        "scale_cuda_listed": bool(inv["filters_checked"].get("scale_cuda")),
        "nvenc_crop_smoke": {},
        "scale_cuda_smoke": {},
    }

    if not inv["ffmpeg_available"]:
        report["nvenc_crop_smoke"] = {"ok": False, "reason": "ffmpeg binary missing"}
        return report

    ffmpeg_bin = inv["ffmpeg_binary"]
    ffprobe_bin = inv.get("ffprobe_binary")
    if not report["h264_nvenc_listed"]:
        report["nvenc_crop_smoke"] = {
            "ok": False,
            "available": False,
            "reason": "h264_nvenc encoder not listed in ffmpeg -encoders",
        }
        return report

    with tempfile.TemporaryDirectory(prefix=f"p8_nvenc_gpu{gpu_id}_") as tmp_dir:
        out_mp4 = Path(tmp_dir) / f"smoke_gpu{gpu_id}.mp4"
        cmd = [
            ffmpeg_bin,
            "-v",
            "verbose",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=1920x2560:rate=30:duration=2.0",
            "-vf",
            "crop=1920:1080:0:400,format=yuv420p",
            "-c:v",
            "h264_nvenc",
            "-gpu",
            "0",
            "-preset",
            "p4",
            "-cq",
            "23",
            "-movflags",
            "+faststart",
            str(out_mp4),
        ]
        res = _run_cmd(cmd, timeout=60)
        probe = _probe_mp4_with_ffprobe(ffprobe_bin, out_mp4) if (res["ok"] and out_mp4.is_file()) else {}
        smoke_ok = bool(
            res["ok"]
            and probe.get("ok")
            and probe.get("codec_name") == "h264"
            and probe.get("width") == 1920
            and probe.get("height") == 1080
        )
        encode_fps = round(60.0 / res["duration_seconds"], 1) if (smoke_ok and res["duration_seconds"] > 0) else None
        report["nvenc_crop_smoke"] = {
            "ok": smoke_ok,
            "available": True,
            "gpu_flag_used": 0,
            "returncode": res["returncode"],
            "encode_seconds": res["duration_seconds"],
            "effective_fps": encode_fps,
            "ffprobe": probe,
            "nvenc_log_lines": _extract_nvenc_log_lines(res["stderr"]),
        }
        if not smoke_ok:
            report["nvenc_crop_smoke"]["stderr_tail"] = res["stderr"][-1500:]
            # Diagnostic: test whether FFmpeg expected physical gpu_id instead of CUDA_VISIBLE_DEVICES-remapped 0
            if gpu_id != 0:
                diag_mp4 = Path(tmp_dir) / f"smoke_explicit_gpu{gpu_id}.mp4"
                diag_cmd = [item if idx != cmd.index("-gpu") + 1 else str(gpu_id) for idx, item in enumerate(cmd[:-1])] + [str(diag_mp4)]
                diag_res = _run_cmd(diag_cmd, timeout=60)
                report["nvenc_crop_smoke"]["diagnostic_explicit_gpu_flag"] = {
                    "gpu_flag": gpu_id,
                    "ok": diag_res["ok"],
                    "returncode": diag_res["returncode"],
                    "stderr_tail": diag_res["stderr"][-1000:] if not diag_res["ok"] else "",
                }

        if report["scale_cuda_listed"] and inv["filters_checked"].get("hwupload_cuda"):
            sc_mp4 = Path(tmp_dir) / f"scale_cuda_gpu{gpu_id}.mp4"
            sc_cmd = [
                ffmpeg_bin,
                "-v",
                "verbose",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=1920x2560:rate=30:duration=1.0",
                "-vf",
                "crop=1920:1080:0:400,format=nv12,hwupload_cuda,scale_cuda=1280:720,hwdownload,format=nv12",
                "-c:v",
                "h264_nvenc",
                "-gpu",
                "0",
                "-preset",
                "p4",
                str(sc_mp4),
            ]
            sc_res = _run_cmd(sc_cmd, timeout=60)
            sc_probe = _probe_mp4_with_ffprobe(ffprobe_bin, sc_mp4) if (sc_res["ok"] and sc_mp4.is_file()) else {}
            report["scale_cuda_smoke"] = {
                "ok": bool(sc_res["ok"] and sc_probe.get("ok") and sc_probe.get("width") == 1280 and sc_probe.get("height") == 720),
                "returncode": sc_res["returncode"],
                "encode_seconds": sc_res["duration_seconds"],
                "ffprobe": sc_probe,
                "stderr_tail": "" if sc_res["ok"] else sc_res["stderr"][-1000:],
            }
        else:
            report["scale_cuda_smoke"] = {
                "ok": False,
                "available": False,
                "reason": "scale_cuda or hwupload_cuda filter not listed in ffmpeg -filters",
            }

    return report


def _run_isolated_worker(worker_flag: str, gpu_id: int, timeout: int = 360) -> Dict[str, Any]:
    script_path = Path(__file__).resolve()
    child_env = os.environ.copy()
    child_env["CUDA_VISIBLE_DEVICES"] = str(gpu_id)
    child_env.setdefault("PYTHONUNBUFFERED", "1")
    cmd = [sys.executable, str(script_path), worker_flag, "--gpu", str(gpu_id)]
    res = _run_cmd(cmd, env=child_env, timeout=timeout)
    if not res["ok"]:
        return {
            "requested_gpu_id": gpu_id,
            "env_cuda_visible_devices": str(gpu_id),
            "worker_failed": True,
            "returncode": res["returncode"],
            "stderr_tail": res["stderr"][-1500:],
            "stdout_tail": res["stdout"][-500:],
        }
    try:
        return json.loads(res["stdout"])
    except Exception as exc:
        return {
            "requested_gpu_id": gpu_id,
            "env_cuda_visible_devices": str(gpu_id),
            "worker_failed": True,
            "error": f"Invalid JSON from worker: {_redact_secrets(str(exc))}",
            "stdout_tail": res["stdout"][-1000:],
            "stderr_tail": res["stderr"][-1000:],
        }


def _build_probe_summary(report: Dict[str, Any]) -> Dict[str, Any]:
    nvsmi = report.get("nvidia_smi", {})
    gpus = nvsmi.get("gpus", [])
    gpu_count = int(nvsmi.get("gpu_count", 0))
    gpu_names = [g.get("name", "") for g in gpus]
    dual_t4_detected = gpu_count >= 2 and all("t4" in name.lower() for name in gpu_names[:2])
    nvsmi_uuids = [g.get("uuid") for g in gpus if g.get("uuid")]

    vieneu_by_gpu = report.get("isolated_vieneu_probes", {})
    nvenc_by_gpu = report.get("isolated_nvenc_probes", {})
    g0_tts = vieneu_by_gpu.get("0", {})
    g1_tts = vieneu_by_gpu.get("1", {})
    g0_nv = nvenc_by_gpu.get("0", {})
    g1_nv = nvenc_by_gpu.get("1", {})

    g0_uuid = ((g0_tts.get("torch") or {}).get("devices") or [{}])[0].get("uuid")
    g1_uuid = ((g1_tts.get("torch") or {}).get("devices") or [{}])[0].get("uuid")
    distinct_torch_uuids = bool(g0_uuid and g1_uuid and g0_uuid != g1_uuid)

    g0_cuda_active = bool((g0_tts.get("initialization") or {}).get("cuda_actually_used"))
    g1_cuda_active = bool((g1_tts.get("initialization") or {}).get("cuda_actually_used"))
    g0_batch_ok = bool((g0_tts.get("inference_batch_4") or {}).get("ok"))
    g1_batch_ok = bool((g1_tts.get("inference_batch_4") or {}).get("ok"))
    g0_nvenc_ok = bool((g0_nv.get("nvenc_crop_smoke") or {}).get("ok"))
    g1_nvenc_ok = bool((g1_nv.get("nvenc_crop_smoke") or {}).get("ok"))

    filters = (report.get("ffmpeg") or {}).get("filters_checked", {})
    encoders = (report.get("ffmpeg") or {}).get("encoders_checked", {})

    return {
        "gpu_count": gpu_count,
        "dual_t4_detected": dual_t4_detected,
        "distinct_nvsmi_uuids_count": len(set(nvsmi_uuids)),
        "distinct_isolated_torch_uuids_verified": distinct_torch_uuids,
        "vieneu_gpu0_cuda_active": g0_cuda_active,
        "vieneu_gpu1_cuda_active": g1_cuda_active,
        "vieneu_gpu0_provider": (g0_tts.get("initialization") or {}).get("actual_execution_provider"),
        "vieneu_gpu1_provider": (g1_tts.get("initialization") or {}).get("actual_execution_provider"),
        "vieneu_infer_batch_gpu0_ok": g0_batch_ok,
        "vieneu_infer_batch_gpu1_ok": g1_batch_ok,
        "h264_nvenc_available": bool(encoders.get("h264_nvenc")),
        "nvenc_gpu0_crop_smoke_ok": g0_nvenc_ok,
        "nvenc_gpu1_crop_smoke_ok": g1_nvenc_ok,
        "scale_cuda_available": bool(filters.get("scale_cuda")),
        "scale_cuda_smoke_gpu0_ok": bool((g0_nv.get("scale_cuda_smoke") or {}).get("ok")),
        "scale_cuda_smoke_gpu1_ok": bool((g1_nv.get("scale_cuda_smoke") or {}).get("ok")),
        "scale_npp_available": bool(filters.get("scale_npp")),
        "crop_available": bool(filters.get("crop")),
        "zoompan_available": bool(filters.get("zoompan")),
        "ready_for_p8_dual_t4": bool(
            dual_t4_detected
            and g0_cuda_active
            and g1_cuda_active
            and g0_nvenc_ok
            and g1_nvenc_ok
            and (distinct_torch_uuids or len(set(nvsmi_uuids)) >= 2)
        ),
    }


def run_local_probe(
    output_path: Path,
    gpu_ids: List[int],
    skip_tts: bool = False,
    skip_ffmpeg: bool = False,
    auto_install_vieneu: bool = False,
) -> Dict[str, Any]:
    install_info = _ensure_vieneu_installed(auto_install=auto_install_vieneu) if not skip_tts else {"skipped": True}
    host_info = _collect_host_info()
    nvidia_smi = _collect_nvidia_smi()
    torch_summary = _collect_torch_summary()
    ort_summary = _collect_ort_summary()
    ffmpeg_inv = _collect_ffmpeg_inventory()

    vieneu_probes: Dict[str, Any] = {}
    nvenc_probes: Dict[str, Any] = {}

    for gid in gpu_ids:
        key = str(gid)
        if not skip_tts:
            vieneu_probes[key] = _run_isolated_worker("--worker-vieneu", gid, timeout=600)
        if not skip_ffmpeg:
            nvenc_probes[key] = _run_isolated_worker("--worker-nvenc", gid, timeout=120)

    report: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": _utc_iso_now(),
        "host": host_info,
        "vieneu_bootstrap": install_info,
        "nvidia_smi": nvidia_smi,
        "torch": torch_summary,
        "onnxruntime": ort_summary,
        "ffmpeg": ffmpeg_inv,
        "isolated_vieneu_probes": vieneu_probes,
        "isolated_nvenc_probes": nvenc_probes,
    }
    report["summary"] = _build_probe_summary(report)
    written = _atomic_write_json(output_path, report)
    report["_written_path"] = str(written)
    return report


def _authenticate_kaggle() -> Tuple[Any, str]:
    try:
        from kaggle.api.kaggle_api_extended import KaggleApi  # type: ignore
    except ImportError as exc:
        raise RuntimeError("The 'kaggle' Python package is required for --dispatch.") from exc

    api = KaggleApi()
    try:
        api.authenticate()
    except SystemExit as exc:
        raise RuntimeError(
            "Kaggle API authentication failed. Configure KAGGLE_API_TOKEN or ~/.kaggle/access_token."
        ) from exc
    except Exception as exc:
        raise RuntimeError(f"Kaggle API authentication failed: {_redact_secrets(str(exc))}") from exc

    username = api.config_values.get(api.CONFIG_NAME_USER)
    if not username:
        raise RuntimeError("Authenticated Kaggle API did not resolve a username.")
    return api, str(username)


def _normalize_kernel_slug(raw: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", raw.lower()).strip("-")
    return slug[:45].rstrip("-") or "mr-p8-dual-t4-probe"


def _resolve_kernel_ref(push_res: Any, default_ref: str) -> str:
    assigned = getattr(push_res, "ref", None)
    if not assigned:
        return default_ref
    text = str(assigned).strip()
    if "/code/" in text:
        return text.split("/code/", 1)[1].strip("/")
    return text.strip("/")


def dispatch_kaggle_probe(
    output_path: Path,
    staging_dir: Path,
    kernel_slug: Optional[str] = None,
    machine_shape: str = "NvidiaTeslaT4",
    enable_gpu: bool = True,
    wait: bool = True,
    poll_interval: int = 10,
    timeout_seconds: int = 900,
    dry_run: bool = False,
) -> Dict[str, Any]:
    slug = _normalize_kernel_slug(kernel_slug or f"mr-p8-dual-t4-probe-{int(time.time()) % 100000}")
    stage = Path(staging_dir).resolve()
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True, exist_ok=True)

    probe_code_name = "probe_dual_t4_kaggle.py"
    code_target = stage / probe_code_name
    code_target.write_text(Path(__file__).read_text(encoding="utf-8"), encoding="utf-8")

    username = "kaggle_dry_run_user"
    api = None
    if not dry_run:
        api, username = _authenticate_kaggle()

    kernel_ref = f"{username}/{slug}"
    metadata = {
        "id": kernel_ref,
        "title": slug,
        "code_file": probe_code_name,
        "language": "python",
        "kernel_type": "script",
        "is_private": True,
        "enable_gpu": bool(enable_gpu),
        "enable_tpu": False,
        "enable_internet": True,
        "machine_shape": machine_shape if enable_gpu else "None",
        "dataset_sources": [],
        "competition_sources": [],
        "kernel_sources": [],
        "model_sources": [],
    }
    meta_path = stage / "kernel-metadata.json"
    _atomic_write_json(meta_path, metadata)

    if dry_run:
        return {
            "status": "dry_run",
            "staging_dir": str(stage),
            "kernel_metadata": metadata,
            "code_file": str(code_target),
        }

    assert api is not None
    push_kwargs: Dict[str, Any] = {}
    if enable_gpu and machine_shape:
        push_kwargs["acc"] = machine_shape
    push_res = api.kernels_push(str(stage), **push_kwargs)
    target_ref = _resolve_kernel_ref(push_res, kernel_ref)

    if not wait:
        return {
            "status": "dispatched",
            "kernel_ref": target_ref,
            "staging_dir": str(stage),
            "machine_shape": metadata["machine_shape"],
        }

    t0 = time.monotonic()
    last_status = None
    completed = False
    while time.monotonic() - t0 < timeout_seconds:
        time.sleep(poll_interval)
        status_obj = api.kernels_status(target_ref)
        raw_status = getattr(status_obj, "status", str(status_obj))
        if isinstance(raw_status, dict):
            raw_status = raw_status.get("status", "UNKNOWN")
        norm = str(raw_status).strip().upper().split(".")[-1]
        if norm != last_status:
            print(f"[{int(time.monotonic() - t0)}s] Kaggle kernel '{target_ref}' status: {raw_status}", file=sys.stderr)
            last_status = norm
        if norm in {"COMPLETE", "COMPLETED"}:
            completed = True
            break
        if any(err in norm for err in ("FAIL", "ERROR", "CANCEL")):
            err_dir = stage / "error_output"
            err_dir.mkdir(parents=True, exist_ok=True)
            log_tail = ""
            try:
                api.kernels_output(target_ref, str(err_dir))
                logs = sorted(err_dir.glob("*.log"))
                if logs:
                    log_tail = _redact_secrets(logs[0].read_text(errors="ignore")[-2500:])
            except Exception as dl_exc:
                log_tail = _redact_secrets(str(dl_exc))
            fail_msg = _redact_secrets(str(getattr(status_obj, "failureMessage", raw_status)))
            raise RuntimeError(f"Kaggle probe kernel '{target_ref}' failed ({fail_msg}). Log tail:\n{log_tail}")

    if not completed:
        raise TimeoutError(f"Kaggle probe kernel '{target_ref}' timed out after {timeout_seconds}s (last={last_status}).")

    out_dir = stage / "kernel_output"
    out_dir.mkdir(parents=True, exist_ok=True)
    api.kernels_output(target_ref, str(out_dir))

    candidates = list(out_dir.rglob("dual_t4_probe.json"))
    if not candidates:
        available_files = [p.relative_to(out_dir).as_posix() for p in out_dir.rglob("*") if p.is_file()]
        logs = sorted(out_dir.rglob("*.log"))
        log_tail = _redact_secrets(logs[0].read_text(errors="ignore")[-2500:]) if logs else ""
        raise FileNotFoundError(
            f"Kaggle kernel '{target_ref}' completed but dual_t4_probe.json was not found. "
            f"Files: {available_files}. Log tail:\n{log_tail}"
        )

    probe_data = json.loads(candidates[0].read_text(encoding="utf-8"))
    probe_data["kaggle_dispatch"] = {
        "kernel_ref": target_ref,
        "requested_machine_shape": metadata["machine_shape"],
        "enable_gpu": bool(enable_gpu),
        "elapsed_seconds": round(time.monotonic() - t0, 2),
    }
    written = _atomic_write_json(output_path, probe_data)
    return {
        "status": "completed",
        "kernel_ref": target_ref,
        "output_path": str(written),
        "summary": probe_data.get("summary", {}),
    }


def _parse_gpu_ids(raw: str) -> List[int]:
    ids: List[int] = []
    for part in raw.split(","):
        s = part.strip()
        if not s:
            continue
        ids.append(int(s))
    if not ids:
        raise ValueError("At least one GPU index must be provided via --gpus.")
    return ids


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="P8.0 Dual-T4 Hardware & SDK Capability Probe")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--probe-local",
        "--local",
        dest="probe_local",
        action="store_true",
        help="Run the hardware & SDK probe directly on this host/session.",
    )
    mode.add_argument(
        "--dispatch",
        action="store_true",
        help="Dispatch a lightweight probe-only GPU kernel to Kaggle and collect dual_t4_probe.json.",
    )
    mode.add_argument("--worker-vieneu", action="store_true", help=argparse.SUPPRESS)
    mode.add_argument("--worker-nvenc", action="store_true", help=argparse.SUPPRESS)

    parser.add_argument("--gpu", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output JSON report path (default: /kaggle/working/dual_t4_probe.json in Kaggle, else dual_t4_probe.json).",
    )
    parser.add_argument("--gpus", default="0,1", help="Comma-separated GPU indices to probe in isolated subprocesses.")
    parser.add_argument("--skip-tts", action="store_true", help="Skip VieNeu initialization and synthesis checks.")
    parser.add_argument("--skip-ffmpeg", action="store_true", help="Skip FFmpeg NVENC + crop smoke encodes.")
    parser.add_argument(
        "--auto-install-vieneu",
        action="store_true",
        help="Install vieneu>=3.8.3 via pip if missing (enabled automatically inside Kaggle).",
    )
    parser.add_argument("--kernel-slug", default=None, help="Optional Kaggle kernel slug for --dispatch.")
    parser.add_argument(
        "--machine-shape",
        default="NvidiaTeslaT4",
        help="Kaggle machine_shape accelerator string for --dispatch (default: NvidiaTeslaT4).",
    )
    parser.add_argument("--no-gpu", action="store_true", help="Disable GPU when dispatching (for negative tests).")
    parser.add_argument("--timeout", type=int, default=900, help="Dispatch poll timeout in seconds.")
    parser.add_argument("--poll-interval", type=int, default=10, help="Dispatch poll interval in seconds.")
    parser.add_argument("--no-wait", action="store_true", help="Push Kaggle kernel and return immediately.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="For --dispatch, stage and validate kernel metadata without pushing to Kaggle.",
    )
    parser.add_argument(
        "--staging-dir",
        type=Path,
        default=Path("/tmp/kaggle_p8_dual_t4_probe"),
        help="Staging directory for --dispatch.",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    if args.worker_vieneu:
        print(json.dumps(_worker_vieneu(args.gpu), ensure_ascii=False))
        return 0
    if args.worker_nvenc:
        print(json.dumps(_worker_nvenc(args.gpu), ensure_ascii=False))
        return 0

    in_kaggle = _in_kaggle_session()
    if not args.probe_local and not args.dispatch:
        if in_kaggle:
            args.probe_local = True
        else:
            parser.print_help()
            return 2

    default_out = DEFAULT_KAGGLE_OUTPUT if in_kaggle else Path("dual_t4_probe.json")
    output_path = args.output or default_out

    if args.probe_local:
        gpu_ids = _parse_gpu_ids(args.gpus)
        report = run_local_probe(
            output_path=output_path,
            gpu_ids=gpu_ids,
            skip_tts=args.skip_tts,
            skip_ffmpeg=args.skip_ffmpeg,
            auto_install_vieneu=bool(args.auto_install_vieneu or in_kaggle),
        )
        summary_view = {
            "status": "success",
            "mode": "probe_local",
            "output_path": report.pop("_written_path", str(output_path)),
            "summary": report["summary"],
        }
        print(json.dumps(summary_view, indent=2, ensure_ascii=False))
        return 0

    result = dispatch_kaggle_probe(
        output_path=output_path,
        staging_dir=args.staging_dir,
        kernel_slug=args.kernel_slug,
        machine_shape=args.machine_shape,
        enable_gpu=not args.no_gpu,
        wait=not args.no_wait,
        poll_interval=args.poll_interval,
        timeout_seconds=args.timeout,
        dry_run=args.dry_run,
    )
    print(json.dumps(_sanitize_payload(result), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
