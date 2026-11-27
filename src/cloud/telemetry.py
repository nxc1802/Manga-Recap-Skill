"""Lightweight structured telemetry and hardware probing for Kaggle Dual-T4 runs."""

from __future__ import annotations

import contextlib
import importlib.metadata
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from src.core.storage import atomic_write_json
from src.production.hardware import probe_available_encoders, smoke_test_encoder

_SENSITIVE_KEY_TOKENS = (
    "token",
    "secret",
    "password",
    "passwd",
    "api_key",
    "credentials",
    "narrator_text",
    "ocr_text",
    "prompt",
)
_SECRET_VALUE_RE = re.compile(r"(AIza[0-9A-Za-z_\-]{8,}|KGAT_[0-9A-Za-z_\-]{6,}|hf_[0-9A-Za-z]{6,})")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pkg_version(dist_name: str) -> Optional[str]:
    try:
        return importlib.metadata.version(dist_name)
    except Exception:
        return None


def sanitize_telemetry_value(value: Any, *, key_hint: str = "") -> Any:
    """Strips sensitive credentials, API tokens, and private narration text from telemetry."""
    if key_hint:
        low_key = key_hint.lower()
        if any(tok in low_key for tok in _SENSITIVE_KEY_TOKENS):
            return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): sanitize_telemetry_value(v, key_hint=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_telemetry_value(item) for item in value]
    if isinstance(value, str):
        return _SECRET_VALUE_RE.sub("[REDACTED]", value)
    return value


def _memory_info_mb() -> Dict[str, Optional[float]]:
    meminfo = Path("/proc/meminfo")
    if meminfo.is_file():
        total_kb = avail_kb = None
        for line in meminfo.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("MemTotal:"):
                with contextlib.suppress(ValueError):
                    total_kb = float(line.split()[1])
            elif line.startswith("MemAvailable:"):
                with contextlib.suppress(ValueError):
                    avail_kb = float(line.split()[1])
        return {
            "ram_total_mb": round(total_kb / 1024.0, 1) if total_kb is not None else None,
            "ram_available_mb": round(avail_kb / 1024.0, 1) if avail_kb is not None else None,
        }
    if hasattr(os, "sysconf"):
        with contextlib.suppress(Exception):
            pages = os.sysconf("SC_PHYS_PAGES")
            page_sz = os.sysconf("SC_PAGE_SIZE")
            if pages > 0 and page_sz > 0:
                return {"ram_total_mb": round((pages * page_sz) / (1024.0 * 1024.0), 1), "ram_available_mb": None}
    return {"ram_total_mb": None, "ram_available_mb": None}


def query_nvidia_smi() -> List[Dict[str, Any]]:
    """Queries nvidia-smi for GPU model, memory, SM utilization, and NVENC utilization."""
    smi = shutil.which("nvidia-smi")
    if not smi:
        return []
    try:
        res = subprocess.run(
            [
                smi,
                "--query-gpu=index,name,memory.total,memory.used,utilization.gpu,utilization.encoder",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if res.returncode != 0 or not res.stdout.strip():
            return []
        gpus: List[Dict[str, Any]] = []
        for raw_line in res.stdout.strip().splitlines():
            parts = [p.strip() for p in raw_line.split(",")]
            if len(parts) < 6:
                continue

            def _num(val: str) -> Optional[float]:
                try:
                    return float(val)
                except ValueError:
                    return None

            gpus.append({
                "index": int(_num(parts[0]) or 0),
                "name": parts[1],
                "vram_total_mb": _num(parts[2]),
                "vram_used_mb": _num(parts[3]),
                "sm_util_pct": _num(parts[4]),
                "nvenc_util_pct": _num(parts[5]),
            })
        return gpus
    except Exception:
        return []


def probe_worker_hardware(
    work_root: Optional[Path] = None,
    *,
    probe_nvenc_devices: bool = True,
) -> Dict[str, Any]:
    """Captures host/worker hardware, CUDA device inventory, per-GPU NVENC status, and versions."""
    root = Path(work_root or ".").resolve()
    disk_target = root if root.exists() else root.parent
    disk_free_mb = disk_total_mb = None
    with contextlib.suppress(Exception):
        usage = shutil.disk_usage(disk_target)
        disk_total_mb = round(usage.total / (1024.0 * 1024.0), 1)
        disk_free_mb = round(usage.free / (1024.0 * 1024.0), 1)

    cuda_available = False
    cuda_version: Optional[str] = None
    devices: List[Dict[str, Any]] = []
    try:
        import torch  # noqa: PLC0415

        cuda_mod = getattr(torch, "cuda", None)
        cuda_version = getattr(getattr(torch, "version", None), "cuda", None)
        if cuda_mod is not None and cuda_mod.is_available():
            cuda_available = True
            count = int(cuda_mod.device_count())
            for idx in range(count):
                name = None
                total_mb = None
                with contextlib.suppress(Exception):
                    name = str(cuda_mod.get_device_name(idx))
                with contextlib.suppress(Exception):
                    props = cuda_mod.get_device_properties(idx)
                    total_mb = round(getattr(props, "total_memory", 0) / (1024.0 * 1024.0), 1)
                devices.append({"index": idx, "name": name, "vram_total_mb": total_mb})
    except Exception:
        pass

    smi_gpus = query_nvidia_smi()
    if smi_gpus and not devices:
        for g in smi_gpus:
            devices.append({
                "index": g["index"],
                "name": g["name"],
                "vram_total_mb": g.get("vram_total_mb"),
            })
    elif smi_gpus:
        by_idx = {g["index"]: g for g in smi_gpus}
        for dev in devices:
            match = by_idx.get(dev["index"])
            if match:
                dev.setdefault("name", match.get("name"))
                if dev.get("vram_total_mb") is None:
                    dev["vram_total_mb"] = match.get("vram_total_mb")

    encoders = probe_available_encoders()
    has_nvenc_codec = "h264_nvenc" in encoders
    usable_nvenc_devices: List[int] = []
    if probe_nvenc_devices and has_nvenc_codec:
        if devices:
            for dev in devices:
                idx = int(dev["index"])
                ok = smoke_test_encoder("h264_nvenc", gpu_index=idx)
                dev["nvenc_usable"] = ok
                if ok:
                    usable_nvenc_devices.append(idx)
        elif smoke_test_encoder("h264_nvenc"):
            usable_nvenc_devices.append(0)
    else:
        for dev in devices:
            dev["nvenc_usable"] = False

    ffmpeg_version: Optional[str] = None
    ffmpeg_bin = shutil.which("ffmpeg")
    if ffmpeg_bin:
        with contextlib.suppress(Exception):
            ver_res = subprocess.run([ffmpeg_bin, "-version"], capture_output=True, text=True, timeout=3, check=False)
            if ver_res.stdout:
                ffmpeg_version = ver_res.stdout.splitlines()[0].strip()

    hf_cache = Path(os.getenv("HF_HOME", Path.home() / ".cache" / "huggingface" / "hub"))
    has_cached_weights = hf_cache.exists() and any("vieneu" in p.name.lower() for p in hf_cache.iterdir())

    mem = _memory_info_mb()
    return {
        "captured_at": _utc_now_iso(),
        "python_version": sys.version.split()[0],
        "cpu_cores": os.cpu_count() or 1,
        "ram_total_mb": mem["ram_total_mb"],
        "ram_available_mb": mem["ram_available_mb"],
        "disk_total_mb": disk_total_mb,
        "disk_free_mb": disk_free_mb,
        "cuda_available": cuda_available,
        "cuda_version": cuda_version,
        "cuda_device_count": len(devices),
        "devices": devices,
        "usable_nvenc_devices": usable_nvenc_devices,
        "ffmpeg_encoders": encoders,
        "ffmpeg_version": ffmpeg_version,
        "model_cache_warm": has_cached_weights,
        "versions": {
            "torch": _pkg_version("torch"),
            "vieneu": _pkg_version("vieneu"),
            "pydantic": _pkg_version("pydantic"),
            "pillow": _pkg_version("pillow"),
            "kaggle": _pkg_version("kaggle"),
        },
    }


def merge_interval_duration(intervals: List[Tuple[float, float]]) -> float:
    """Computes total non-overlapping union duration covered by monotonic intervals."""
    valid = sorted((min(a, b), max(a, b)) for a, b in intervals if b > a)
    if not valid:
        return 0.0
    total = 0.0
    cur_start, cur_end = valid[0]
    for start, end in valid[1:]:
        if start <= cur_end:
            cur_end = max(cur_end, end)
        else:
            total += cur_end - cur_start
            cur_start, cur_end = start, end
    total += cur_end - cur_start
    return round(total, 4)


class TelemetryRecorder:
    """Thread-safe structured telemetry collector with overlap-aware stage summaries."""

    def __init__(
        self,
        *,
        job_id: Optional[str] = None,
        project_slug: str = "",
        policy: str = "dual_tts_shared_encode",
        chapters: Optional[List[str]] = None,
    ) -> None:
        self.job_id = job_id or f"job-{uuid.uuid4().hex[:12]}"
        self.project_slug = project_slug
        self.policy = policy
        self.chapters = list(chapters or [])
        self.started_mono = time.monotonic()
        self.started_at = _utc_now_iso()
        self._lock = threading.Lock()
        self._spans: List[Dict[str, Any]] = []
        self._counters: Dict[str, float] = {}
        self._hardware: Optional[Dict[str, Any]] = None

    def set_hardware(self, hardware: Dict[str, Any]) -> None:
        with self._lock:
            self._hardware = sanitize_telemetry_value(hardware)

    def increment(self, metric: str, value: float = 1.0) -> None:
        with self._lock:
            self._counters[metric] = self._counters.get(metric, 0.0) + float(value)

    def record_stage(
        self,
        stage: str,
        start_mono: float,
        end_mono: float,
        *,
        chapter_id: Optional[str] = None,
        segment_id: Optional[str] = None,
        shot_id: Optional[str] = None,
        device_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        duration = max(0.0, float(end_mono) - float(start_mono))
        record = {
            "job_id": self.job_id,
            "stage": stage,
            "chapter_id": chapter_id,
            "segment_id": segment_id,
            "shot_id": shot_id,
            "device_id": device_id,
            "start_offset_sec": round(max(0.0, float(start_mono) - self.started_mono), 4),
            "end_offset_sec": round(max(0.0, float(end_mono) - self.started_mono), 4),
            "duration_sec": round(duration, 4),
            "ts": _utc_now_iso(),
        }
        if metadata:
            record["metadata"] = sanitize_telemetry_value(metadata)
        with self._lock:
            self._spans.append(record)

    @contextlib.contextmanager
    def span(
        self,
        stage: str,
        *,
        chapter_id: Optional[str] = None,
        segment_id: Optional[str] = None,
        shot_id: Optional[str] = None,
        device_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Iterator[Dict[str, Any]]:
        meta = dict(metadata or {})
        t0 = time.monotonic()
        try:
            yield meta
        finally:
            self.record_stage(
                stage,
                t0,
                time.monotonic(),
                chapter_id=chapter_id,
                segment_id=segment_id,
                shot_id=shot_id,
                device_id=device_id,
                metadata=meta or None,
            )

    def compute_stage_summary(self) -> Dict[str, Any]:
        """Aggregates per-stage intervals, true union wall time, and overlap saved time."""
        with self._lock:
            spans = list(self._spans)

        by_stage_intervals: Dict[str, List[Tuple[float, float]]] = {}
        all_intervals: List[Tuple[float, float]] = []
        raw_sums: Dict[str, float] = {}
        for sp in spans:
            st = str(sp["stage"])
            a = float(sp["start_offset_sec"])
            b = float(sp["end_offset_sec"])
            by_stage_intervals.setdefault(st, []).append((a, b))
            all_intervals.append((a, b))
            raw_sums[st] = raw_sums.get(st, 0.0) + float(sp["duration_sec"])

        stages: Dict[str, Dict[str, Any]] = {}
        for st, ivs in sorted(by_stage_intervals.items()):
            wall_union = merge_interval_duration(ivs)
            raw_total = round(raw_sums[st], 4)
            stages[st] = {
                "count": len(ivs),
                "wall_union_sec": wall_union,
                "sum_worker_sec": raw_total,
                "intra_stage_overlap_sec": round(max(0.0, raw_total - wall_union), 4),
            }

        global_union = merge_interval_duration(all_intervals)
        total_raw = round(sum(raw_sums.values()), 4)
        return {
            "wall_makespan_sec": round(max(0.0, time.monotonic() - self.started_mono), 4),
            "active_union_sec": global_union,
            "sum_all_spans_sec": total_raw,
            "overlap_saved_sec": round(max(0.0, total_raw - global_union), 4),
            "stages": stages,
        }

    def build_report(self, *, include_events: bool = True) -> Dict[str, Any]:
        with self._lock:
            counters = {k: round(v, 4) for k, v in self._counters.items()}
            events = list(self._spans) if include_events else []
            hardware = self._hardware

        summary = self.compute_stage_summary()
        return {
            "schema_version": "5.0.0",
            "job_id": self.job_id,
            "project_slug": self.project_slug,
            "policy": self.policy,
            "chapters": self.chapters,
            "started_at": self.started_at,
            "finished_at": _utc_now_iso(),
            "hardware": hardware,
            "counters": counters,
            "timing": summary,
            "events": events,
        }


def save_telemetry_report(path: Path, report: Dict[str, Any]) -> None:
    """Atomically persists a sanitized telemetry report JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, sanitize_telemetry_value(report))
