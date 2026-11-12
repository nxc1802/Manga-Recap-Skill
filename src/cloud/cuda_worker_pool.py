"""
src/cloud/cuda_worker_pool.py
Multiprocessing `spawn`-isolated GPU worker pool, `nvidia-smi` VRAM telemetry sampler,
JSONL worker lifecycle logger, and OOM/crash recovery for Dual-T4 execution.
"""

from __future__ import annotations

import csv
import datetime
import gc
import importlib
import inspect
import json
import multiprocessing
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from src.core.logger import logger


DEFAULT_VRAM_RESERVE_MB = 1536.0
DEFAULT_VRAM_RATIO = 0.90
DEFAULT_POLL_INTERVAL_SEC = 1.0
DEFAULT_CANVAS_CACHE_MAX_ENTRIES = 128

GPU_TRACE_COLUMNS: Tuple[str, ...] = (
    "timestamp_utc",
    "elapsed_sec",
    "gpu_id",
    "gpu_uuid",
    "name",
    "vram_total_mb",
    "vram_used_mb",
    "vram_free_mb",
    "usable_vram_budget_mb",
    "gpu_util_pct",
    "encoder_util_pct",
    "temperature_c",
    "active_workers",
    "scheduler_state",
)


def _utc_iso_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def compute_usable_vram_budget(total_mb: float) -> float:
    """
    Computes safe usable VRAM budget per GPU:
      usable_vram_budget = min(0.9 * total_mb, total_mb - 1536.0)
    For a 16 GB T4 (15360 MB), yields 13824.0 MB (~13.5 GiB / 13.8-14.3 GB).
    """
    total = float(total_mb or 0.0)
    if total <= 0.0:
        return 0.0
    return round(max(0.0, min(DEFAULT_VRAM_RATIO * total, total - DEFAULT_VRAM_RESERVE_MB)), 2)


def is_oom_error(err: Union[BaseException, str]) -> bool:
    """Detects CUDA / NVENC / PyTorch out-of-memory failures."""
    if isinstance(err, MemoryError):
        return True
    msg = str(err).lower()
    if any(
        token in msg
        for token in (
            "out of memory",
            "cuda oom",
            "cuda out of memory",
            "cublas_status_alloc_failed",
            "nvenc out of memory",
            "memory allocation",
            "oom",
        )
    ):
        return True
    cls_name = type(err).__name__.lower() if isinstance(err, BaseException) else ""
    return "outofmemory" in cls_name


def query_nvidia_smi() -> List[Dict[str, Any]]:
    """
    Queries live `nvidia-smi` telemetry for all visible physical GPUs.
    Returns empty list when `nvidia-smi` is absent (e.g. macOS / CPU-only hosts).
    """
    smi_bin = shutil.which("nvidia-smi") or (
        "/opt/bin/nvidia-smi" if Path("/opt/bin/nvidia-smi").exists() else None
    )
    if not smi_bin:
        return []

    queries = [
        (
            "index,uuid,name,memory.total,memory.used,memory.free,"
            "utilization.gpu,utilization.encoder,temperature.gpu",
            True,
        ),
        (
            "index,uuid,name,memory.total,memory.used,memory.free,"
            "utilization.gpu,temperature.gpu",
            False,
        ),
    ]
    for fields, has_enc in queries:
        try:
            res = subprocess.run(
                [smi_bin, f"--query-gpu={fields}", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=3.0,
                check=False,
            )
            if res.returncode != 0 or not res.stdout.strip():
                continue
            rows: List[Dict[str, Any]] = []
            for line in res.stdout.strip().splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) < (9 if has_enc else 8):
                    continue

                def _num(val: str, default: float = 0.0) -> float:
                    try:
                        if val.upper() in {"N/A", "[N/A]", "[NOT SUPPORTED]", ""}:
                            return default
                        return float(val)
                    except ValueError:
                        return default

                total_mb = _num(parts[3])
                used_mb = _num(parts[4])
                free_mb = _num(parts[5])
                gpu_util = _num(parts[6])
                enc_util = _num(parts[7]) if has_enc else 0.0
                temp_c = _num(parts[8] if has_enc else parts[7])
                rows.append(
                    {
                        "gpu_id": int(_num(parts[0], 0)),
                        "gpu_uuid": parts[1],
                        "name": parts[2],
                        "vram_total_mb": total_mb,
                        "vram_used_mb": used_mb,
                        "vram_free_mb": free_mb,
                        "usable_vram_budget_mb": compute_usable_vram_budget(total_mb),
                        "gpu_util_pct": gpu_util,
                        "encoder_util_pct": enc_util,
                        "temperature_c": temp_c,
                    }
                )
            return rows
        except Exception:
            continue
    return []


@dataclass
class GpuTelemetrySample:
    timestamp_utc: str
    elapsed_sec: float
    gpu_id: int
    gpu_uuid: str
    name: str
    vram_total_mb: float
    vram_used_mb: float
    vram_free_mb: float
    usable_vram_budget_mb: float
    gpu_util_pct: float = 0.0
    encoder_util_pct: float = 0.0
    temperature_c: float = 0.0
    active_workers: int = 0
    scheduler_state: str = "BALANCED"

    def to_csv_row(self) -> List[Any]:
        return [
            self.timestamp_utc,
            round(self.elapsed_sec, 3),
            self.gpu_id,
            self.gpu_uuid,
            self.name,
            round(self.vram_total_mb, 2),
            round(self.vram_used_mb, 2),
            round(self.vram_free_mb, 2),
            round(self.usable_vram_budget_mb, 2),
            round(self.gpu_util_pct, 1),
            round(self.encoder_util_pct, 1),
            round(self.temperature_c, 1),
            int(self.active_workers),
            self.scheduler_state,
        ]


class GpuTelemetrySampler:
    """
    Background ~1s `nvidia-smi` telemetry sampler writing `dual_t4_gpu_trace.csv`
    and structured worker lifecycle events to `dual_t4_worker_log.jsonl`.
    """

    def __init__(
        self,
        trace_csv_path: Union[str, Path],
        worker_log_path: Optional[Union[str, Path]] = None,
        *,
        poll_interval_sec: float = DEFAULT_POLL_INTERVAL_SEC,
        gpu_ids: Sequence[int] = (0, 1),
        smi_querier: Optional[Callable[[], List[Dict[str, Any]]]] = None,
        state_provider: Optional[Callable[[], Tuple[str, Dict[int, int]]]] = None,
    ):
        self.trace_csv_path = Path(trace_csv_path).resolve()
        self.worker_log_path = (
            Path(worker_log_path).resolve()
            if worker_log_path is not None
            else (self.trace_csv_path.parent / "dual_t4_worker_log.jsonl")
        )
        self.poll_interval_sec = max(0.05, float(poll_interval_sec))
        self.gpu_ids = tuple(int(g) for g in gpu_ids)
        self._smi_querier = smi_querier or query_nvidia_smi
        self._state_provider = state_provider

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._t0 = time.monotonic()
        self._header_written = False
        self._samples_by_gpu: Dict[int, List[GpuTelemetrySample]] = {g: [] for g in self.gpu_ids}
        self._peak_vram_mb: Dict[int, float] = {g: 0.0 for g in self.gpu_ids}
        self._budget_mb: Dict[int, float] = {}
        self._worker_events: List[Dict[str, Any]] = []

        self.trace_csv_path.parent.mkdir(parents=True, exist_ok=True)
        self.worker_log_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_files_initialized()

    def _ensure_files_initialized(self) -> None:
        with self._lock:
            if not self._header_written:
                with self.trace_csv_path.open("w", newline="", encoding="utf-8") as fh:
                    writer = csv.writer(fh)
                    writer.writerow(GPU_TRACE_COLUMNS)
                self._header_written = True
            if not self.worker_log_path.exists():
                self.worker_log_path.write_text("", encoding="utf-8")

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._t0 = time.monotonic()
        self.sample_once()
        self._thread = threading.Thread(
            target=self._run_loop, name="GpuTelemetrySampler", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=max(2.0, self.poll_interval_sec * 2.0))
            self._thread = None
        self.sample_once()

    def _run_loop(self) -> None:
        while not self._stop_event.wait(self.poll_interval_sec):
            try:
                self.sample_once()
            except Exception as exc:
                logger.debug(f"[GpuTelemetrySampler] poll error: {exc}")

    def sample_once(self) -> List[GpuTelemetrySample]:
        raw_rows = self._smi_querier() or []
        state_name = "BALANCED"
        active_map: Dict[int, int] = {}
        if self._state_provider is not None:
            try:
                state_name, active_map = self._state_provider()
            except Exception:
                pass

        now_utc = _utc_iso_now()
        elapsed = max(0.0, time.monotonic() - self._t0)
        emitted: List[GpuTelemetrySample] = []

        with self._lock:
            if not self._header_written:
                with self.trace_csv_path.open("w", newline="", encoding="utf-8") as fh:
                    csv.writer(fh).writerow(GPU_TRACE_COLUMNS)
                self._header_written = True

            with self.trace_csv_path.open("a", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                for row in raw_rows:
                    gid = int(row.get("gpu_id", 0))
                    if self.gpu_ids and gid not in self.gpu_ids:
                        continue
                    total_mb = float(row.get("vram_total_mb", 0.0))
                    used_mb = float(row.get("vram_used_mb", 0.0))
                    free_mb = float(row.get("vram_free_mb", max(0.0, total_mb - used_mb)))
                    budget_mb = float(
                        row.get("usable_vram_budget_mb")
                        or compute_usable_vram_budget(total_mb)
                    )
                    sample = GpuTelemetrySample(
                        timestamp_utc=now_utc,
                        elapsed_sec=elapsed,
                        gpu_id=gid,
                        gpu_uuid=str(row.get("gpu_uuid", f"GPU-{gid}")),
                        name=str(row.get("name", f"GPU-{gid}")),
                        vram_total_mb=total_mb,
                        vram_used_mb=used_mb,
                        vram_free_mb=free_mb,
                        usable_vram_budget_mb=budget_mb,
                        gpu_util_pct=float(row.get("gpu_util_pct", 0.0)),
                        encoder_util_pct=float(row.get("encoder_util_pct", 0.0)),
                        temperature_c=float(row.get("temperature_c", 0.0)),
                        active_workers=int(active_map.get(gid, 0)),
                        scheduler_state=str(state_name),
                    )
                    writer.writerow(sample.to_csv_row())
                    emitted.append(sample)
                    self._samples_by_gpu.setdefault(gid, []).append(sample)
                    self._peak_vram_mb[gid] = max(self._peak_vram_mb.get(gid, 0.0), used_mb)
                    if budget_mb > 0.0:
                        self._budget_mb[gid] = budget_mb
                fh.flush()
        return emitted

    def log_worker_event(
        self,
        event: str,
        *,
        gpu_id: int,
        worker_id: str,
        pid: Optional[int] = None,
        task_id: Optional[str] = None,
        task_type: Optional[str] = None,
        chapter: Optional[str] = None,
        batch_size: Optional[int] = None,
        duration_sec: Optional[float] = None,
        vram_used_mb: Optional[float] = None,
        status: str = "ok",
        details: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        record: Dict[str, Any] = {
            "timestamp_utc": _utc_iso_now(),
            "event": event,
            "gpu_id": int(gpu_id),
            "worker_id": worker_id,
            "pid": pid,
            "task_id": task_id,
            "task_type": task_type,
            "chapter": chapter,
            "batch_size": batch_size,
            "duration_sec": round(float(duration_sec), 4) if duration_sec is not None else None,
            "vram_used_mb": round(float(vram_used_mb), 2) if vram_used_mb is not None else None,
            "status": status,
            "details": details or {},
        }
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            with self.worker_log_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
            self._worker_events.append(record)
        return record

    def get_latest_sample(self, gpu_id: int) -> Optional[GpuTelemetrySample]:
        with self._lock:
            rows = self._samples_by_gpu.get(int(gpu_id), [])
            return rows[-1] if rows else None

    def get_peak_vram_mb(self, gpu_id: int) -> float:
        with self._lock:
            return float(self._peak_vram_mb.get(int(gpu_id), 0.0))

    def get_vram_budget_mb(self, gpu_id: int, default_total_mb: float = 15360.0) -> float:
        with self._lock:
            if int(gpu_id) in self._budget_mb:
                return float(self._budget_mb[int(gpu_id)])
        return compute_usable_vram_budget(default_total_mb)

    def is_over_vram_budget(self, gpu_id: int) -> bool:
        latest = self.get_latest_sample(gpu_id)
        if latest is None or latest.usable_vram_budget_mb <= 0.0:
            return False
        return latest.vram_used_mb > latest.usable_vram_budget_mb

    def summarize(self) -> Dict[str, Any]:
        with self._lock:
            per_gpu: Dict[str, Any] = {}
            for gid in sorted(set(self.gpu_ids) | set(self._samples_by_gpu.keys())):
                samples = self._samples_by_gpu.get(gid, [])
                per_gpu[str(gid)] = {
                    "sample_count": len(samples),
                    "peak_vram_mb": round(self._peak_vram_mb.get(gid, 0.0), 2),
                    "usable_vram_budget_mb": round(
                        self._budget_mb.get(gid, compute_usable_vram_budget(15360.0)), 2
                    ),
                    "max_gpu_util_pct": round(
                        max((s.gpu_util_pct for s in samples), default=0.0), 1
                    ),
                    "avg_gpu_util_pct": round(
                        (sum(s.gpu_util_pct for s in samples) / len(samples)) if samples else 0.0,
                        2,
                    ),
                    "max_encoder_util_pct": round(
                        max((s.encoder_util_pct for s in samples), default=0.0), 1
                    ),
                    "avg_encoder_util_pct": round(
                        (sum(s.encoder_util_pct for s in samples) / len(samples)) if samples else 0.0,
                        2,
                    ),
                }
            return {
                "trace_csv": str(self.trace_csv_path),
                "worker_log_jsonl": str(self.worker_log_path),
                "total_events": len(self._worker_events),
                "per_gpu": per_gpu,
            }


# ==============================================================================
# Spawn-Isolated GPU Worker Entrypoint (MUST set CUDA_VISIBLE_DEVICES first)
# ==============================================================================


class _BoundedCanvasCache(OrderedDict):
    """LRU canvas cache bounded by entry count to keep worker RAM bounded."""

    def __init__(self, max_entries: int = DEFAULT_CANVAS_CACHE_MAX_ENTRIES):
        super().__init__()
        self.max_entries = max(1, int(max_entries))

    def get_or_evict(self, key: Any) -> Any:
        val = super().get(key)
        if val is not None:
            self.move_to_end(key)
        return val

    def __setitem__(self, key: Any, value: Any) -> None:
        if key in self:
            self.move_to_end(key)
        super().__setitem__(key, value)
        while len(self) > self.max_entries:
            self.popitem(last=False)


def _ensure_repo_on_syspath(repo_root: Optional[str]) -> None:
    if repo_root and repo_root not in sys.path:
        sys.path.insert(0, repo_root)


def _load_tts_worker_cls():
    """Imports frozen #67 VieNeuCUDAWorker contract when available."""
    mod = importlib.import_module("src.tts.cuda_worker")
    return mod.VieNeuCUDAWorker


def _resolve_render_shot_fn():
    """
    Imports frozen #68 `src.render.fast_renderer.render_shot_clip` when present.
    Provides a minimal canonical Pillow+FFmpeg fallback only when running inside
    an isolated pre-merge worktree without `fast_renderer.py`.
    """
    try:
        mod = importlib.import_module("src.render.fast_renderer")
        fn = getattr(mod, "render_shot_clip", None)
        if callable(fn):
            return fn
    except ImportError:
        pass

    def _fallback_render_shot_clip(
        task: Dict[str, Any], *, canvas_cache: Optional[Dict] = None, gpu_index: int = 0
    ) -> Path:
        from PIL import Image
        from src.core.visual_contract import ProductionMotion, Region, Shot, VisualPlan
        from src.render.visual_plan_renderer import render_frame

        out_path = Path(task["output_clip_path"]).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        fps = int(task["fps"])
        w = int(task["target_width"])
        h = int(task["target_height"])
        frame_count = int(task["frame_count"])
        encoder = str(task.get("encoder") or "libx264")
        box = task["source_box"]
        if isinstance(box, dict):
            box = Region.model_validate(box)
        motion = ProductionMotion(task["motion_type"])
        shot = Shot(
            shot_id=str(task["shot_id"]),
            segment_id=str(task["segment_id"]),
            source_panel_id=str(task.get("source_panel_id") or "p1"),
            source_box=box,
            motion_type=motion,
            duration_sec=float(task["duration_sec"]),
            scroll_fraction_per_second=float(task.get("scroll_fraction_per_second", 0.0)),
        )
        plan = VisualPlan(
            chapter_number=str(task.get("chapter_number", "1")),
            input_fingerprint=str(task.get("input_fingerprint", "0" * 64)),
            motion_enabled=(motion == ProductionMotion.SLOW_PAN_DOWN),
            shots=[shot],
            fps=fps,
            target_width=w,
            target_height=h,
            total_duration_sec=float(task["duration_sec"]),
        )
        codec_args = ["-c:v", encoder]
        if encoder == "h264_nvenc":
            codec_args.extend(["-gpu", str(int(gpu_index)), "-preset", "p4", "-cq", "23"])
        elif encoder == "libx264":
            codec_args.extend(["-preset", "fast", "-crf", "23"])
        partial = out_path.with_name(f".{out_path.stem}.partial.mp4")
        try:
            with Image.open(task["panel_path"]) as img:
                if motion != ProductionMotion.SLOW_PAN_DOWN and canvas_cache is not None:
                    ckey = task.get("canvas_key") or (
                        str(task["panel_path"]),
                        tuple(box.model_dump().values()),
                        w,
                        h,
                    )
                    canvas_path = canvas_cache.get(ckey)
                    if canvas_path is None or not Path(canvas_path).is_file():
                        canvas_img = render_frame(img, shot, plan, 0.0)
                        c_file = out_path.with_name(f".canvas_{uuid.uuid4().hex[:10]}.png")
                        canvas_img.save(c_file, format="PNG")
                        canvas_cache[ckey] = str(c_file)
                        canvas_path = str(c_file)
                    cmd = [
                        "ffmpeg",
                        "-v",
                        "error",
                        "-y",
                        "-loop",
                        "1",
                        "-framerate",
                        str(fps),
                        "-i",
                        str(canvas_path),
                        "-frames:v",
                        str(frame_count),
                        "-vf",
                        "setsar=1",
                        "-an",
                        *codec_args,
                        "-pix_fmt",
                        "yuv420p",
                        "-profile:v",
                        "high",
                        "-r",
                        str(fps),
                        "-fps_mode",
                        "cfr",
                        "-video_track_timescale",
                        str(fps * 1000),
                        str(partial),
                    ]
                    subprocess.run(cmd, check=True, capture_output=True, timeout=300)
                else:
                    cmd = [
                        "ffmpeg",
                        "-v",
                        "error",
                        "-y",
                        "-f",
                        "rawvideo",
                        "-pix_fmt",
                        "rgb24",
                        "-s",
                        f"{w}x{h}",
                        "-r",
                        str(fps),
                        "-i",
                        "pipe:0",
                        "-an",
                        *codec_args,
                        "-pix_fmt",
                        "yuv420p",
                        "-profile:v",
                        "high",
                        "-r",
                        str(fps),
                        "-fps_mode",
                        "cfr",
                        "-video_track_timescale",
                        str(fps * 1000),
                        "-frames:v",
                        str(frame_count),
                        str(partial),
                    ]
                    proc = subprocess.Popen(
                        cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE
                    )
                    assert proc.stdin is not None
                    for idx in range(frame_count):
                        frame = render_frame(
                            img, shot, plan, min(idx / float(fps), shot.duration_sec)
                        )
                        proc.stdin.write(frame.tobytes())
                    proc.stdin.close()
                    rc = proc.wait(timeout=300)
                    if rc != 0:
                        err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
                        raise RuntimeError(f"Shot ffmpeg fallback failed ({rc}): {err[-1000:]}")
            partial.replace(out_path)
            return out_path
        finally:
            partial.unlink(missing_ok=True)

    return _fallback_render_shot_clip


def _cuda_worker_process_main(
    physical_gpu_id: int,
    worker_id: str,
    role: str,
    cmd_queue: Any,
    res_queue: Any,
    options: Dict[str, Any],
) -> None:
    """
    Top-level `spawn`-picklable entrypoint for isolated GPU worker processes.
    CRITICAL: sets `os.environ['CUDA_VISIBLE_DEVICES']` BEFORE touching torch/onnx/vieneu/NVENC.
    Inside this process, CUDA device 0 and FFmpeg `-gpu 0` map exclusively to `physical_gpu_id`.
    """
    os.environ["CUDA_VISIBLE_DEVICES"] = str(int(physical_gpu_id))
    os.environ.setdefault("OMP_NUM_THREADS", str(int(options.get("omp_threads", 2))))
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

    _ensure_repo_on_syspath(options.get("repo_root"))

    tts_worker: Any = None
    canvas_cache = _BoundedCanvasCache(
        max_entries=int(options.get("canvas_cache_entries", DEFAULT_CANVAS_CACHE_MAX_ENTRIES))
    )
    render_shot_fn: Optional[Callable[..., Path]] = None

    def _current_vram_mb() -> float:
        try:
            if tts_worker is not None and callable(getattr(tts_worker, "vram_usage_mb", None)):
                info = tts_worker.vram_usage_mb()
                if isinstance(info, dict):
                    return float(info.get("allocated_mb") or info.get("reserved_mb") or 0.0)
                if isinstance(info, (int, float)):
                    return float(info)
        except Exception:
            pass
        return 0.0

    def _ensure_tts_loaded() -> Any:
        nonlocal tts_worker
        if tts_worker is None:
            custom_factory = options.get("tts_worker_factory")
            if callable(custom_factory):
                tts_worker = custom_factory(
                    gpu_id=0,
                    mode=options.get("tts_mode", "v3turbo"),
                    precision=options.get("precision", "auto"),
                    preload=True,
                )
            else:
                cls = _load_tts_worker_cls()
                tts_worker = cls(
                    gpu_id=0,
                    mode=options.get("tts_mode", "v3turbo"),
                    precision=options.get("precision", "auto"),
                    preload=True,
                )
        return tts_worker

    def _unload_tts() -> bool:
        nonlocal tts_worker
        if tts_worker is None:
            return False
        try:
            if callable(getattr(tts_worker, "unload", None)):
                tts_worker.unload()
        finally:
            tts_worker = None
            gc.collect()
            try:
                import torch  # noqa: PLC0415

                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                pass
        return True

    res_queue.put(
        {
            "type": "ready",
            "worker_id": worker_id,
            "physical_gpu_id": int(physical_gpu_id),
            "pid": os.getpid(),
            "role": role,
            "env_cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
        }
    )

    try:
        if options.get("preload_tts") and role in {"tts", "coexist", "elastic"}:
            _ensure_tts_loaded()

        while True:
            cmd = cmd_queue.get()
            if cmd is None or cmd.get("op") == "shutdown":
                _unload_tts()
                res_queue.put(
                    {
                        "type": "shutdown_ack",
                        "worker_id": worker_id,
                        "task_id": cmd.get("task_id") if isinstance(cmd, dict) else None,
                    }
                )
                break

            task_id = cmd.get("task_id")
            op = cmd.get("op")
            t0 = time.monotonic()
            try:
                if op == "ping":
                    res_queue.put(
                        {
                            "type": "result",
                            "task_id": task_id,
                            "worker_id": worker_id,
                            "ok": True,
                            "data": {
                                "pid": os.getpid(),
                                "physical_gpu_id": int(physical_gpu_id),
                                "env_cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
                                "tts_loaded": tts_worker is not None,
                                "vram_mb": _current_vram_mb(),
                            },
                            "duration_sec": time.monotonic() - t0,
                        }
                    )
                elif op == "probe":
                    torch_info: Dict[str, Any] = {"available": False, "device_count": 0}
                    try:
                        import torch  # noqa: PLC0415

                        torch_info = {
                            "available": bool(torch.cuda.is_available()),
                            "device_count": int(torch.cuda.device_count())
                            if torch.cuda.is_available()
                            else 0,
                            "device_0_name": (
                                torch.cuda.get_device_name(0)
                                if torch.cuda.is_available() and torch.cuda.device_count() > 0
                                else None
                            ),
                        }
                    except Exception as exc:
                        torch_info["error"] = str(exc)
                    res_queue.put(
                        {
                            "type": "result",
                            "task_id": task_id,
                            "worker_id": worker_id,
                            "ok": True,
                            "data": {
                                "pid": os.getpid(),
                                "physical_gpu_id": int(physical_gpu_id),
                                "env_cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
                                "torch": torch_info,
                            },
                            "duration_sec": time.monotonic() - t0,
                        }
                    )
                elif op == "load_tts":
                    _ensure_tts_loaded()
                    res_queue.put(
                        {
                            "type": "result",
                            "task_id": task_id,
                            "worker_id": worker_id,
                            "ok": True,
                            "data": {"tts_loaded": True, "vram_mb": _current_vram_mb()},
                            "duration_sec": time.monotonic() - t0,
                        }
                    )
                elif op == "unload_tts":
                    unloaded = _unload_tts()
                    res_queue.put(
                        {
                            "type": "result",
                            "task_id": task_id,
                            "worker_id": worker_id,
                            "ok": True,
                            "data": {"unloaded": unloaded, "vram_mb": _current_vram_mb()},
                            "duration_sec": time.monotonic() - t0,
                        }
                    )
                elif op == "tts_batch":
                    t_init0 = time.monotonic()
                    was_loaded = tts_worker is not None
                    worker_obj = _ensure_tts_loaded()
                    init_dur = 0.0 if was_loaded else max(0.0, time.monotonic() - t_init0)
                    t_inf0 = time.monotonic()
                    results = worker_obj.synthesize_batch_tasks(
                        cmd["tasks"],
                        batch_size=int(cmd.get("batch_size", 8)),
                        precision=str(cmd.get("precision", options.get("precision", "auto"))),
                        cpu_workers=int(cmd.get("cpu_workers", 4)),
                    )
                    infer_dur = max(0.0, time.monotonic() - t_inf0)
                    res_queue.put(
                        {
                            "type": "result",
                            "task_id": task_id,
                            "worker_id": worker_id,
                            "ok": True,
                            "data": {
                                "results": results,
                                "model_init_sec": round(init_dur, 4),
                                "tts_infer_sec": round(infer_dur, 4),
                                "vram_mb": _current_vram_mb(),
                                "env_cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
                            },
                            "duration_sec": time.monotonic() - t0,
                        }
                    )
                elif op == "render_shot":
                    if render_shot_fn is None:
                        custom_render = options.get("render_shot_fn")
                        render_shot_fn = (
                            custom_render if callable(custom_render) else _resolve_render_shot_fn()
                        )
                    # Inside isolated CUDA_VISIBLE_DEVICES subprocess, NVENC uses logical gpu_index=0.
                    sig = inspect.signature(render_shot_fn)
                    if "gpu_index" in sig.parameters and "canvas_cache" in sig.parameters:
                        clip_path = render_shot_fn(
                            cmd["task"], canvas_cache=canvas_cache, gpu_index=0
                        )
                    elif "canvas_cache" in sig.parameters:
                        clip_path = render_shot_fn(cmd["task"], canvas_cache=canvas_cache)
                    else:
                        clip_path = render_shot_fn(cmd["task"])
                    res_queue.put(
                        {
                            "type": "result",
                            "task_id": task_id,
                            "worker_id": worker_id,
                            "ok": True,
                            "data": {
                                "output_clip_path": str(clip_path),
                                "gpu_index_used": 0,
                                "physical_gpu_id": int(physical_gpu_id),
                                "env_cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
                            },
                            "duration_sec": time.monotonic() - t0,
                        }
                    )
                else:
                    raise ValueError(f"Unsupported worker op: {op}")
            except BaseException as exc:
                res_queue.put(
                    {
                        "type": "result",
                        "task_id": task_id,
                        "worker_id": worker_id,
                        "ok": False,
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                        "is_oom": is_oom_error(exc),
                        "duration_sec": time.monotonic() - t0,
                    }
                )
    finally:
        _unload_tts()


# ==============================================================================
# Worker Handle & Pool Manager (Spawn-only + OOM/Crash Recovery)
# ==============================================================================


class WorkerExecutionError(RuntimeError):
    """Raised when a worker task fails; carries OOM/crash classification."""

    def __init__(self, message: str, *, is_oom: bool = False, crashed: bool = False):
        super().__init__(message)
        self.is_oom = is_oom
        self.crashed = crashed


class _SpawnWorkerHandle:
    """Manages a single `multiprocessing.get_context('spawn')` GPU worker process."""

    def __init__(
        self,
        *,
        mp_ctx: multiprocessing.context.SpawnContext,
        physical_gpu_id: int,
        worker_id: str,
        role: str,
        options: Optional[Dict[str, Any]] = None,
    ):
        self.mp_ctx = mp_ctx
        self.physical_gpu_id = int(physical_gpu_id)
        self.worker_id = worker_id
        self.role = role
        self.options = dict(options or {})
        self.options.setdefault("repo_root", str(Path(__file__).resolve().parents[2]))

        self.cmd_queue = self.mp_ctx.Queue()
        self.res_queue = self.mp_ctx.Queue()
        self.process = self.mp_ctx.Process(
            target=_cuda_worker_process_main,
            args=(
                self.physical_gpu_id,
                self.worker_id,
                self.role,
                self.cmd_queue,
                self.res_queue,
                self.options,
            ),
            name=f"cuda-{self.role}-gpu{self.physical_gpu_id}-{self.worker_id}",
            daemon=True,
        )
        self._io_lock = threading.Lock()
        self.pid: Optional[int] = None
        self.tts_loaded: bool = False
        self.started: bool = False

    def start(self, startup_timeout: float = 120.0) -> Dict[str, Any]:
        self.process.start()
        self.pid = self.process.pid
        deadline = time.monotonic() + startup_timeout
        while time.monotonic() < deadline:
            if not self.process.is_alive():
                raise WorkerExecutionError(
                    f"Spawned worker {self.worker_id} on GPU {self.physical_gpu_id} "
                    f"exited during startup (exitcode={self.process.exitcode})",
                    crashed=True,
                )
            try:
                msg = self.res_queue.get(timeout=0.2)
                if msg.get("type") == "ready":
                    self.started = True
                    self.pid = msg.get("pid", self.pid)
                    return msg
            except queue.Empty:
                continue
        self.kill()
        raise WorkerExecutionError(
            f"Timed out waiting for spawned worker {self.worker_id} startup",
            crashed=True,
        )

    def is_alive(self) -> bool:
        return self.process.is_alive()

    def execute(self, op: str, payload: Optional[Dict[str, Any]] = None, timeout: float = 600.0) -> Dict[str, Any]:
        task_id = uuid.uuid4().hex[:12]
        cmd = {"op": op, "task_id": task_id}
        if payload:
            cmd.update(payload)

        with self._io_lock:
            if not self.process.is_alive():
                raise WorkerExecutionError(
                    f"Worker {self.worker_id} (GPU {self.physical_gpu_id}) died before {op} "
                    f"(exitcode={self.process.exitcode})",
                    crashed=True,
                )
            self.cmd_queue.put(cmd)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    msg = self.res_queue.get(timeout=0.2)
                except queue.Empty:
                    if not self.process.is_alive():
                        raise WorkerExecutionError(
                            f"Worker {self.worker_id} (GPU {self.physical_gpu_id}) crashed during {op} "
                            f"(exitcode={self.process.exitcode})",
                            crashed=True,
                        )
                    continue

                if msg.get("task_id") != task_id and msg.get("type") != "shutdown_ack":
                    continue
                if not msg.get("ok", True):
                    err = str(msg.get("error", "Unknown worker error"))
                    raise WorkerExecutionError(
                        err,
                        is_oom=bool(msg.get("is_oom") or is_oom_error(err)),
                        crashed=False,
                    )
                if op == "load_tts" or op == "tts_batch":
                    self.tts_loaded = True
                elif op == "unload_tts":
                    self.tts_loaded = False
                return msg

            raise WorkerExecutionError(
                f"Worker {self.worker_id} (GPU {self.physical_gpu_id}) timed out on {op} ({timeout}s)",
                crashed=True,
            )

    def shutdown(self, timeout: float = 10.0) -> None:
        try:
            if self.process.is_alive():
                self.cmd_queue.put({"op": "shutdown", "task_id": "shutdown"})
                self.process.join(timeout=timeout)
        except Exception:
            pass
        if self.process.is_alive():
            self.kill()

    def kill(self) -> None:
        try:
            if self.process.is_alive():
                self.process.terminate()
                self.process.join(timeout=2.0)
            if self.process.is_alive():
                self.process.kill()
                self.process.join(timeout=2.0)
        except Exception:
            pass


class CudaWorkerPool:
    """
    Spawn-based Dual-T4 worker pool supporting elastic TTS replicas and NVENC
    workers per physical GPU, VRAM telemetry integration, and OOM/crash recovery.

    NEVER uses `fork` or `ThreadPoolExecutor` for GPU execution.
    """

    def __init__(
        self,
        *,
        gpu_ids: Sequence[int] = (0, 1),
        tts_replicas_per_gpu: int = 1,
        nvenc_workers_per_gpu: int = 2,
        initial_tts_batch_size: int = 8,
        telemetry: Optional[GpuTelemetrySampler] = None,
        worker_options: Optional[Dict[str, Any]] = None,
        worker_handle_factory: Optional[Callable[..., Any]] = None,
        max_task_retries: int = 3,
    ):
        # Strictly enforce `spawn` multiprocessing context; never `fork`.
        self.mp_ctx = multiprocessing.get_context("spawn")
        self.gpu_ids: Tuple[int, ...] = tuple(int(g) for g in gpu_ids)
        self.tts_replicas_target: Dict[int, int] = {
            g: max(1, int(tts_replicas_per_gpu)) for g in self.gpu_ids
        }
        self.nvenc_workers_target: Dict[int, int] = {
            g: max(1, int(nvenc_workers_per_gpu)) for g in self.gpu_ids
        }
        self.tts_batch_size_by_gpu: Dict[int, int] = {
            g: max(1, int(initial_tts_batch_size)) for g in self.gpu_ids
        }
        self.telemetry = telemetry
        self.worker_options = dict(worker_options or {})
        self.worker_handle_factory = worker_handle_factory
        self.max_task_retries = max(1, int(max_task_retries))

        self._lock = threading.Lock()
        self._tts_workers: Dict[int, List[Any]] = {g: [] for g in self.gpu_ids}
        self._render_workers: Dict[int, List[Any]] = {g: [] for g in self.gpu_ids}
        self._rr_tts_idx: Dict[int, int] = {g: 0 for g in self.gpu_ids}
        self._rr_render_idx: Dict[int, int] = {g: 0 for g in self.gpu_ids}
        self._oom_events: Dict[int, int] = {g: 0 for g in self.gpu_ids}
        self._crash_events: Dict[int, int] = {g: 0 for g in self.gpu_ids}
        self._stats: Dict[int, Dict[str, Any]] = {
            g: {
                "tts_tasks": 0,
                "tts_items": 0,
                "render_shots": 0,
                "active_seconds": 0.0,
                "model_init_seconds": 0.0,
                "tts_infer_seconds": 0.0,
                "render_shot_seconds": 0.0,
            }
            for g in self.gpu_ids
        }
        self._tts_init_intervals: List[Tuple[float, float]] = []
        self._tts_infer_intervals: List[Tuple[float, float]] = []
        self._closed = False

    def _spawn_handle(self, physical_gpu_id: int, role: str) -> Any:
        worker_id = f"{role}_g{physical_gpu_id}_{uuid.uuid4().hex[:6]}"
        if self.worker_handle_factory is not None:
            handle = self.worker_handle_factory(
                mp_ctx=self.mp_ctx,
                physical_gpu_id=physical_gpu_id,
                worker_id=worker_id,
                role=role,
                options=dict(self.worker_options),
            )
        else:
            handle = _SpawnWorkerHandle(
                mp_ctx=self.mp_ctx,
                physical_gpu_id=physical_gpu_id,
                worker_id=worker_id,
                role=role,
                options=dict(self.worker_options),
            )
        info = handle.start()
        if self.telemetry is not None:
            self.telemetry.log_worker_event(
                "worker_spawned",
                gpu_id=physical_gpu_id,
                worker_id=worker_id,
                pid=info.get("pid") or getattr(handle, "pid", None),
                task_type=role,
                details={
                    "spawn_method": self.mp_ctx.get_start_method(),
                    "env_cuda_visible_devices": info.get("env_cuda_visible_devices"),
                },
            )
        return handle

    def _ensure_tts_workers(self, gpu_id: int) -> List[Any]:
        gid = int(gpu_id)
        with self._lock:
            alive = [w for w in self._tts_workers.get(gid, []) if w.is_alive()]
            self._tts_workers[gid] = alive
            target = max(1, self.tts_replicas_target.get(gid, 1))
            while len(alive) < target:
                w = self._spawn_handle(gid, "tts")
                alive.append(w)
            self._tts_workers[gid] = alive
            return list(alive)

    def _ensure_render_workers(self, gpu_id: int) -> List[Any]:
        gid = int(gpu_id)
        with self._lock:
            alive = [w for w in self._render_workers.get(gid, []) if w.is_alive()]
            self._render_workers[gid] = alive
            target = max(1, self.nvenc_workers_target.get(gid, 1))
            while len(alive) < target:
                w = self._spawn_handle(gid, "render")
                alive.append(w)
            self._render_workers[gid] = alive
            return list(alive)

    def _next_tts_worker(self, gpu_id: int) -> Any:
        workers = self._ensure_tts_workers(gpu_id)
        with self._lock:
            idx = self._rr_tts_idx.get(gpu_id, 0) % len(workers)
            self._rr_tts_idx[gpu_id] = idx + 1
            return workers[idx]

    def _next_render_worker(self, gpu_id: int) -> Any:
        workers = self._ensure_render_workers(gpu_id)
        with self._lock:
            idx = self._rr_render_idx.get(gpu_id, 0) % len(workers)
            self._rr_render_idx[gpu_id] = idx + 1
            return workers[idx]

    def _handle_worker_failure_and_recover(
        self,
        *,
        gpu_id: int,
        role: str,
        failed_worker: Any,
        exc: BaseException,
    ) -> int:
        """
        Handles OOM or unexpected process crash by:
        1. Killing/removing the dead worker.
        2. Halving batch size & replica count on that GPU.
        3. Logging recovery telemetry.
        4. Respawning a clean worker on `gpu_id`.
        Returns the new halved batch size for `gpu_id`.
        """
        gid = int(gpu_id)
        is_oom = getattr(exc, "is_oom", False) or is_oom_error(exc)

        with self._lock:
            if is_oom:
                self._oom_events[gid] = self._oom_events.get(gid, 0) + 1
            else:
                self._crash_events[gid] = self._crash_events.get(gid, 0) + 1

            pool_map = self._tts_workers if role == "tts" else self._render_workers
            workers = pool_map.get(gid, [])
            if failed_worker in workers:
                workers.remove(failed_worker)

            if role == "tts":
                old_batch = self.tts_batch_size_by_gpu.get(gid, 8)
                new_batch = max(1, old_batch // 2)
                self.tts_batch_size_by_gpu[gid] = new_batch

                old_rep = self.tts_replicas_target.get(gid, 1)
                new_rep = max(1, old_rep // 2)
                self.tts_replicas_target[gid] = new_rep
                extra_to_shed = workers[max(0, new_rep - 1) :]
                pool_map[gid] = workers[: max(0, new_rep - 1)]
            else:
                new_batch = self.tts_batch_size_by_gpu.get(gid, 8)
                old_rep = self.nvenc_workers_target.get(gid, 1)
                new_rep = max(1, old_rep // 2)
                self.nvenc_workers_target[gid] = new_rep
                extra_to_shed = workers[max(0, new_rep - 1) :]
                pool_map[gid] = workers[: max(0, new_rep - 1)]

        try:
            failed_worker.kill()
        except Exception:
            pass
        for extra in extra_to_shed:
            try:
                extra.shutdown(timeout=5.0)
            except Exception:
                try:
                    extra.kill()
                except Exception:
                    pass

        if self.telemetry is not None:
            self.telemetry.log_worker_event(
                "oom_recovery" if is_oom else "worker_crash_recovery",
                gpu_id=gid,
                worker_id=getattr(failed_worker, "worker_id", f"{role}_g{gid}"),
                pid=getattr(failed_worker, "pid", None),
                task_type=role,
                batch_size=new_batch,
                status="recovered",
                details={
                    "reason": str(exc),
                    "new_batch_size": new_batch,
                    "new_replica_target": new_rep,
                },
            )

        # Respawn fresh worker immediately
        if role == "tts":
            self._ensure_tts_workers(gid)
        else:
            self._ensure_render_workers(gid)
        return new_batch

    def active_worker_counts(self) -> Dict[int, int]:
        with self._lock:
            return {
                g: sum(1 for w in self._tts_workers.get(g, []) if w.is_alive())
                + sum(1 for w in self._render_workers.get(g, []) if w.is_alive())
                for g in self.gpu_ids
            }

    def probe_gpu(self, gpu_id: int) -> Dict[str, Any]:
        worker = self._next_render_worker(int(gpu_id))
        res = worker.execute("probe")
        return res.get("data", {})

    def unload_tts_workers(self, gpu_ids: Optional[Iterable[int]] = None) -> None:
        targets = tuple(int(g) for g in (gpu_ids if gpu_ids is not None else self.gpu_ids))
        for gid in targets:
            with self._lock:
                workers = list(self._tts_workers.get(gid, []))
                self._tts_workers[gid] = []
            for w in workers:
                if not w.is_alive():
                    continue
                try:
                    w.execute("unload_tts", timeout=30.0)
                    if self.telemetry is not None:
                        self.telemetry.log_worker_event(
                            "tts_unloaded",
                            gpu_id=gid,
                            worker_id=getattr(w, "worker_id", f"tts_g{gid}"),
                            pid=getattr(w, "pid", None),
                            task_type="tts",
                        )
                except Exception:
                    pass
                w.shutdown(timeout=5.0)

    def submit_tts_batch(
        self,
        gpu_id: int,
        tasks: List[Dict[str, Any]],
        *,
        batch_size: Optional[int] = None,
        precision: str = "auto",
        cpu_workers: int = 4,
        chapter: Optional[str] = None,
        _attempt: int = 0,
    ) -> List[Dict[str, Any]]:
        """
        Executes a TTS batch on isolated `gpu_id` (`CUDA_VISIBLE_DEVICES=gpu_id`).
        On OOM or worker crash: restarts worker, halves batch/replica count,
        and re-queues the exact failed tasks (zero placeholders!).
        """
        if not tasks:
            return []
        gid = int(gpu_id)
        eff_batch = max(1, int(batch_size or self.tts_batch_size_by_gpu.get(gid, 8)))
        worker = self._next_tts_worker(gid)

        t0 = time.monotonic()
        try:
            resp = worker.execute(
                "tts_batch",
                {
                    "tasks": tasks,
                    "batch_size": eff_batch,
                    "precision": precision,
                    "cpu_workers": cpu_workers,
                },
            )
            t1 = time.monotonic()
            dur = t1 - t0
            data = resp.get("data", {})
            results = data.get("results", [])
            if len(results) != len(tasks):
                raise WorkerExecutionError(
                    f"TTS result count mismatch on GPU {gid}: expected {len(tasks)}, got {len(results)}",
                    crashed=True,
                )
            init_sec = float(data.get("model_init_sec", 0.0) or 0.0)
            infer_sec = float(data.get("tts_infer_sec", max(0.0, dur - init_sec)) or 0.0)
            t_init_end = min(t1, t0 + init_sec)
            with self._lock:
                self._stats[gid]["tts_tasks"] += 1
                self._stats[gid]["tts_items"] += len(tasks)
                self._stats[gid]["active_seconds"] += dur
                self._stats[gid]["model_init_seconds"] += init_sec
                self._stats[gid]["tts_infer_seconds"] += infer_sec
                if init_sec > 0.0 and t_init_end > t0:
                    self._tts_init_intervals.append((t0, t_init_end))
                if t1 > t_init_end:
                    self._tts_infer_intervals.append((t_init_end, t1))
            if self.telemetry is not None:
                self.telemetry.log_worker_event(
                    "tts_batch_completed",
                    gpu_id=gid,
                    worker_id=getattr(worker, "worker_id", f"tts_g{gid}"),
                    pid=getattr(worker, "pid", None),
                    task_type="tts",
                    chapter=chapter,
                    batch_size=eff_batch,
                    duration_sec=dur,
                    vram_used_mb=data.get("vram_mb"),
                    details={
                        "items": len(tasks),
                        "model_init_sec": round(init_sec, 4),
                        "tts_infer_sec": round(infer_sec, 4),
                        "env_cuda_visible_devices": data.get("env_cuda_visible_devices"),
                    },
                )
            return results
        except Exception as exc:
            is_oom = getattr(exc, "is_oom", False) or is_oom_error(exc)
            is_crash = getattr(exc, "crashed", False) or (not worker.is_alive())
            if not (is_oom or is_crash):
                raise

            new_batch = self._handle_worker_failure_and_recover(
                gpu_id=gid, role="tts", failed_worker=worker, exc=exc
            )

            # Split across halved micro-batches when multiple tasks remain, or retry single item up to limit.
            if len(tasks) > 1:
                mid = max(1, len(tasks) // 2)
                left = self.submit_tts_batch(
                    gid,
                    tasks[:mid],
                    batch_size=min(new_batch, mid),
                    precision=precision,
                    cpu_workers=cpu_workers,
                    chapter=chapter,
                    _attempt=_attempt,
                )
                right = self.submit_tts_batch(
                    gid,
                    tasks[mid:],
                    batch_size=min(new_batch, len(tasks) - mid),
                    precision=precision,
                    cpu_workers=cpu_workers,
                    chapter=chapter,
                    _attempt=_attempt,
                )
                return left + right

            if _attempt + 1 >= self.max_task_retries:
                raise RuntimeError(
                    f"TTS task {tasks[0].get('seq_id')} failed after {self.max_task_retries} "
                    f"OOM/crash recoveries on GPU {gid}: {exc}"
                ) from exc

            return self.submit_tts_batch(
                gid,
                tasks,
                batch_size=1,
                precision=precision,
                cpu_workers=cpu_workers,
                chapter=chapter,
                _attempt=_attempt + 1,
            )

    def submit_render_shot(
        self,
        gpu_id: int,
        task: Dict[str, Any],
        *,
        chapter: Optional[str] = None,
        _attempt: int = 0,
    ) -> Path:
        """
        Executes a `ShotRenderTask` on isolated `gpu_id` (`CUDA_VISIBLE_DEVICES=gpu_id`, `-gpu 0`).
        On OOM or worker crash: restarts worker, halves NVENC workers on GPU, and retries exact shot.
        """
        gid = int(gpu_id)
        worker = self._next_render_worker(gid)
        t0 = time.monotonic()
        try:
            resp = worker.execute("render_shot", {"task": task})
            dur = time.monotonic() - t0
            data = resp.get("data", {})
            out_path = Path(data["output_clip_path"])
            if not out_path.is_file() or out_path.stat().st_size <= 0:
                raise WorkerExecutionError(
                    f"Shot output missing or empty: {out_path}", crashed=True
                )
            with self._lock:
                self._stats[gid]["render_shots"] += 1
                self._stats[gid]["active_seconds"] += dur
                self._stats[gid]["render_shot_seconds"] += dur
            if self.telemetry is not None:
                self.telemetry.log_worker_event(
                    "render_shot_completed",
                    gpu_id=gid,
                    worker_id=getattr(worker, "worker_id", f"render_g{gid}"),
                    pid=getattr(worker, "pid", None),
                    task_id=str(task.get("shot_id")),
                    task_type="render",
                    chapter=chapter or str(task.get("chapter_number", "")),
                    duration_sec=dur,
                    details={
                        "encoder": task.get("encoder"),
                        "gpu_index_used": data.get("gpu_index_used", 0),
                        "env_cuda_visible_devices": data.get("env_cuda_visible_devices"),
                    },
                )
            return out_path
        except Exception as exc:
            is_oom = getattr(exc, "is_oom", False) or is_oom_error(exc)
            is_crash = getattr(exc, "crashed", False) or (not worker.is_alive())
            if not (is_oom or is_crash):
                raise

            self._handle_worker_failure_and_recover(
                gpu_id=gid, role="render", failed_worker=worker, exc=exc
            )
            if _attempt + 1 >= self.max_task_retries:
                raise RuntimeError(
                    f"Render shot {task.get('shot_id')} failed after {self.max_task_retries} "
                    f"OOM/crash recoveries on GPU {gid}: {exc}"
                ) from exc
            return self.submit_render_shot(
                gid, task, chapter=chapter, _attempt=_attempt + 1
            )

    def get_stats(self) -> Dict[int, Dict[str, Any]]:
        with self._lock:
            out: Dict[int, Dict[str, Any]] = {}
            for gid in self.gpu_ids:
                s = dict(self._stats[gid])
                s["oom_events"] = self._oom_events.get(gid, 0)
                s["crash_events"] = self._crash_events.get(gid, 0)
                s["current_tts_batch_size"] = self.tts_batch_size_by_gpu.get(gid, 8)
                s["current_tts_replicas"] = self.tts_replicas_target.get(gid, 1)
                s["current_nvenc_workers"] = self.nvenc_workers_target.get(gid, 1)
                out[gid] = s
            return out

    def get_timing_intervals(self) -> Dict[str, List[Tuple[float, float]]]:
        with self._lock:
            return {
                "tts_init": list(self._tts_init_intervals),
                "tts_infer": list(self._tts_infer_intervals),
            }

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
            all_workers = [
                w
                for mapping in (self._tts_workers, self._render_workers)
                for lst in mapping.values()
                for w in lst
            ]
            for gid in self.gpu_ids:
                self._tts_workers[gid] = []
                self._render_workers[gid] = []
        for w in all_workers:
            w.shutdown(timeout=5.0)
