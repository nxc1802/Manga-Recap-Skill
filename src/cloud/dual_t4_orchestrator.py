"""
src/cloud/dual_t4_orchestrator.py
Dual-T4 Single-Session Elastic Pipeline Orchestrator (P8).

Architecture:
- Multiprocessing `spawn`-isolated GPU workers (`CudaWorkerPool`), NEVER `fork`
  and NEVER fake ThreadPoolExecutor CUDA concurrency.
- 4-state Elastic Scheduler (`TTS_HEAVY`, `BALANCED`, `RENDER_HEAVY`, `DRAIN`)
  with B0-B5 benchmark profiles and StartupAutotuner.
- Chapter-level streaming overlap across 6 bounded queues:
  `source_prefetch_queue -> tts_queue -> audio_ready_queue ->
   chapter_ready_queue -> render_queue -> validation_queue`
- Canonical V4 evidence, `RecapScript`, `VisualPlan`, `AudioMixer`,
  `SubtitleGenerator`, and `validate_final_artifact` enforcement.
- Emits G8 telemetry & artifact receipts (`dual_t4_environment.json`,
  `dual_t4_gpu_trace.csv`, `dual_t4_worker_log.jsonl`,
  `dual_t4_artifact_report.json`, `dual_t4_result_manifest.json`).
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import inspect
import json
import math
import os
import platform
import queue
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

try:
    import torch
except ImportError:
    torch = None

from src.cloud.cuda_worker_pool import (
    CudaWorkerPool,
    GpuTelemetrySampler,
    compute_usable_vram_budget,
    query_nvidia_smi,
)
from src.cloud.elastic_scheduler import (
    ArchitectureProfile,
    BenchmarkProfileId,
    ElasticPipelineQueues,
    ElasticScheduler,
    FrozenSessionProfile,
    QueueSnapshot,
    SchedulerState,
    StartupAutotuner,
    resolve_architecture_profile,
)
from src.core.logger import logger
from src.core.schemas import (
    CheckpointStatus,
    HumanReviewStatus,
    PanelManifest,
    RecapScript,
    StageType,
    utc_iso_now,
)
from src.core.storage import (
    acquire_lock,
    atomic_write_json,
    calculate_sha256,
    calculate_utterance_fingerprint,
    load_chapter_metadata,
    load_manifest,
    load_recap_script,
    read_json,
    update_checkpoint,
)
from src.core.visual_contract import VisualPlan
from src.production.hardware import get_encoder_codec_args, resolve_video_encoder
from src.render.artifact_validation import fingerprint, validate_audio, validate_final_artifact
from src.render.audio_mixer import AudioMixer
from src.render.subtitle import SubtitleGenerator
from src.render.visual_policy import resolve_visual_plan, validate_narration_evidence
from src.tts.catalog import resolve_voice_id


# ==============================================================================
# 1. Telemetry Data Models
# ==============================================================================


@dataclass
class GpuDeviceTelemetry:
    """Telemetry metrics for an individual GPU accelerator."""

    device_id: int
    device_name: str
    assigned_task: str
    peak_vram_mb: float = 0.0
    allocated_vram_mb: float = 0.0
    total_active_seconds: float = 0.0
    processed_items: int = 0
    throughput_metric: float = 0.0
    oom_events_count: int = 0


@dataclass
class ChapterTelemetry:
    """Per-chapter telemetry metrics."""

    chapter: str
    tts_duration_seconds: float = 0.0
    render_duration_seconds: float = 0.0
    segment_count: int = 0
    video_frames_count: int = 0
    video_duration_seconds: float = 0.0
    tts_throughput_utt_per_sec: float = 0.0
    render_fps: float = 0.0


@dataclass
class SeriesTelemetry:
    """Aggregated multi-chapter series execution telemetry."""

    chapters: List[str] = field(default_factory=list)
    total_wall_clock_seconds: float = 0.0
    sequential_estimate_seconds: float = 0.0
    speedup_factor: float = 1.0
    pipeline_overlap_seconds: float = 0.0
    profile_id: str = "B5"
    gpu_0: Optional[GpuDeviceTelemetry] = None
    gpu_1: Optional[GpuDeviceTelemetry] = None
    per_chapter: Dict[str, ChapterTelemetry] = field(default_factory=dict)
    scheduler_transitions: List[Dict[str, Any]] = field(default_factory=list)
    frozen_profile: Optional[Dict[str, Any]] = None
    completed_at: str = field(default_factory=utc_iso_now)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ==============================================================================
# 2. Micro-Batching with Dynamic OOM Backoff
# ==============================================================================


class MicroBatchPlanner:
    """
    Partitions recap script segments into voice and character-length buckets,
    and executes micro-batches with dynamic OutOfMemoryError backoff.
    """

    DEFAULT_INITIAL_BATCH_SIZE: int = 32
    LENGTH_BUCKET_THRESHOLDS: Tuple[int, int, int] = (50, 120, 250)

    @classmethod
    def get_length_bucket(cls, text: str) -> int:
        char_len = len(text or "")
        t0, t1, t2 = cls.LENGTH_BUCKET_THRESHOLDS
        if char_len <= t0:
            return 0
        elif char_len <= t1:
            return 1
        elif char_len <= t2:
            return 2
        return 3

    @classmethod
    def bucket_segments(
        cls, segments: List[Dict[str, Any]]
    ) -> Dict[str, Dict[int, List[Dict[str, Any]]]]:
        grouped: Dict[str, Dict[int, List[Dict[str, Any]]]] = {}
        for seg in segments:
            voice = seg.get("voice_id") or seg.get("voice") or "default"
            text = seg.get("narrator_text") or seg.get("text", "")
            bucket = cls.get_length_bucket(text)
            grouped.setdefault(voice, {}).setdefault(bucket, []).append(seg)
        return grouped

    @classmethod
    def create_batches(
        cls,
        segments: List[Dict[str, Any]],
        max_batch_size: int = DEFAULT_INITIAL_BATCH_SIZE,
    ) -> List[List[Dict[str, Any]]]:
        grouped = cls.bucket_segments(segments)
        all_batches: List[List[Dict[str, Any]]] = []
        for _voice, buckets in grouped.items():
            for bucket_idx, bucket_segs in sorted(buckets.items()):
                effective_batch_size = max_batch_size if bucket_idx < 3 else min(16, max_batch_size)
                for i in range(0, len(bucket_segs), max(1, effective_batch_size)):
                    batch = bucket_segs[i : i + effective_batch_size]
                    if batch:
                        all_batches.append(batch)
        return all_batches

    @classmethod
    def execute_batch_with_oom_backoff(
        cls,
        batch: List[Dict[str, Any]],
        infer_func: Callable[[List[str], str], List[Any]],
        voice: str = "default",
        current_batch_size: Optional[int] = None,
        oom_tracker: Optional[Dict[str, int]] = None,
    ) -> List[Any]:
        if not batch:
            return []
        if current_batch_size is None:
            current_batch_size = len(batch)
        texts = [s.get("narrator_text") or s.get("text", "") for s in batch]

        try:
            return infer_func(texts, voice)
        except Exception as exc:
            is_oom = False
            if torch is not None:
                oom_cls = getattr(getattr(torch, "cuda", None), "OutOfMemoryError", None)
                if oom_cls and isinstance(exc, oom_cls):
                    is_oom = True
            if not is_oom:
                err_msg = str(exc).lower()
                if "out of memory" in err_msg or "cuda oom" in err_msg:
                    is_oom = True
            if not is_oom:
                raise

            logger.warning(
                f"[MicroBatchPlanner] CUDA OOM on batch size {len(batch)}. Executing halving backoff."
            )
            if oom_tracker is not None:
                oom_tracker["count"] = oom_tracker.get("count", 0) + 1
            if torch is not None and getattr(torch, "cuda", None) and torch.cuda.is_available():
                torch.cuda.empty_cache()
            if len(batch) <= 1:
                raise

            half_point = len(batch) // 2
            res1 = cls.execute_batch_with_oom_backoff(
                batch[:half_point], infer_func, voice, half_point, oom_tracker
            )
            res2 = cls.execute_batch_with_oom_backoff(
                batch[half_point:], infer_func, voice, len(batch) - half_point, oom_tracker
            )
            return res1 + res2


# ==============================================================================
# 3. Peer Fast-Renderer Adapter (Consumes #68 `src/render/fast_renderer.py`)
# ==============================================================================


def _resolve_fast_renderer_api():
    """
    Imports frozen peer interface from #68 (`src/render/fast_renderer.py`) when available,
    falling back cleanly to canonical `visual_plan_renderer` + FFmpeg `-c copy` concat
    only inside an unmerged pre-integration worktree.
    """
    try:
        mod = importlib.import_module("src.render.fast_renderer")
        if (
            callable(getattr(mod, "build_shot_render_tasks", None))
            and callable(getattr(mod, "render_shot_clip", None))
            and callable(getattr(mod, "concat_shot_clips", None))
        ):
            return mod.build_shot_render_tasks, mod.render_shot_clip, mod.concat_shot_clips
    except ImportError:
        pass

    from src.render.visual_plan_renderer import frame_schedule
    from src.render.visual_policy import evidence_path

    def _fallback_build_shot_render_tasks(
        chapter_dir: Path,
        plan: VisualPlan,
        manifest: PanelManifest,
        *,
        work_dir: Path,
        encoder: str = "libx264",
        fps_override: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        chap = Path(chapter_dir).resolve()
        wdir = Path(work_dir).resolve()
        wdir.mkdir(parents=True, exist_ok=True)
        fps = int(fps_override or plan.fps)
        panels = {p.panel_id: p for p in manifest.panels}
        tasks: List[Dict[str, Any]] = []
        for idx, (shot, count) in enumerate(frame_schedule(plan)):
            pid = shot.substitute_panel_id or shot.source_panel_id
            panel = panels[pid]
            p_path = evidence_path(chap, panel)
            box_dump = shot.source_box.model_dump()
            ckey = (
                str(p_path),
                (box_dump["x_min"], box_dump["y_min"], box_dump["x_max"], box_dump["y_max"]),
                int(plan.target_width),
                int(plan.target_height),
            )
            tasks.append(
                {
                    "shot_index": idx,
                    "shot_id": shot.shot_id,
                    "segment_id": shot.segment_id,
                    "source_panel_id": pid,
                    "panel_path": str(p_path),
                    "source_box": box_dump,
                    "motion_type": shot.motion_type.value,
                    "duration_sec": float(shot.duration_sec),
                    "frame_count": int(count),
                    "scroll_fraction_per_second": float(shot.scroll_fraction_per_second),
                    "target_width": int(plan.target_width),
                    "target_height": int(plan.target_height),
                    "fps": fps,
                    "encoder": encoder,
                    "chapter_number": plan.chapter_number,
                    "input_fingerprint": plan.input_fingerprint,
                    "output_clip_path": str(wdir / f"{idx:05d}_{shot.shot_id}.mp4"),
                    "canvas_key": ckey,
                }
            )
        return tasks

    def _fallback_render_shot_clip(
        task: Dict[str, Any], *, canvas_cache: Optional[Dict] = None, gpu_index: int = 0
    ) -> Path:
        from src.cloud.cuda_worker_pool import _resolve_render_shot_fn

        return _resolve_render_shot_fn()(task, canvas_cache=canvas_cache, gpu_index=gpu_index)

    def _fallback_concat_shot_clips(
        shot_clips: List[Path],
        output_video: Path,
        *,
        work_dir: Optional[Path] = None,
        audio_path: Optional[Path] = None,
        subtitle_path: Optional[Path] = None,
        burn_subtitles: bool = False,
        target_height: int = 1080,
        fps: int = 30,
        encoder: str = "libx264",
        subtitle_font_size: int = 26,
        quality_mode: Optional[str] = None,
        gpu_index: int = 0,
    ) -> Path:
        out = Path(output_video).resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        wdir = Path(work_dir).resolve() if work_dir else out.parent
        wdir.mkdir(parents=True, exist_ok=True)

        concat_txt = wdir / f".{out.stem}_concat.txt"
        lines = [f"file '{Path(c).resolve().as_posix()}'" for c in shot_clips]
        concat_txt.write_text("\n".join(lines) + "\n", encoding="utf-8")

        raw_concat = wdir / f".{out.stem}_concat_raw.mp4"
        partial = out.with_name(f".{out.stem}.mux.mp4")
        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-y",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(concat_txt),
                    "-c:v",
                    "copy",
                    str(raw_concat),
                ],
                check=True,
                capture_output=True,
                timeout=600,
            )

            cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(raw_concat)]
            if audio_path is not None:
                cmd.extend(["-i", str(Path(audio_path).resolve())])
                cmd.extend(["-map", "0:v:0", "-map", "1:a:0"])
            else:
                cmd.extend(["-map", "0:v:0", "-an"])

            if burn_subtitles and subtitle_path is not None and Path(subtitle_path).is_file():
                # Check if libass subtitles filter is compiled; fallback to PIL overlay if absent
                flt_res = subprocess.run(
                    ["ffmpeg", "-filters"], capture_output=True, text=True, check=False
                )
                has_subtitles_flt = " subtitles " in flt_res.stdout
                if has_subtitles_flt:
                    tmp_srt = wdir / "captions.srt"
                    shutil.copy2(subtitle_path, tmp_srt)
                    cmd.extend(
                        [
                            "-vf",
                            "subtitles=captions.srt",
                            "-c:v",
                            encoder or "libx264",
                        ]
                    )
                else:
                    cmd.extend(["-c:v", "copy"])
            else:
                cmd.extend(["-c:v", "copy"])

            if audio_path is not None:
                cmd.extend(["-c:a", "copy"])
            cmd.extend(["-movflags", "+faststart", str(partial)])
            subprocess.run(cmd, cwd=wdir, check=True, capture_output=True, timeout=600)
            partial.replace(out)
            return out
        finally:
            concat_txt.unlink(missing_ok=True)
            raw_concat.unlink(missing_ok=True)
            partial.unlink(missing_ok=True)

    return _fallback_build_shot_render_tasks, _fallback_render_shot_clip, _fallback_concat_shot_clips


def _build_chapter_shot_tasks(
    build_shot_tasks_fn: Callable[..., List[Dict[str, Any]]],
    chapter_dir: Path,
    chapter_num: str,
    plan: VisualPlan,
    manifest: PanelManifest,
    *,
    work_dir: Path,
    encoder: str,
    subtitles_path: Optional[Path] = None,
    burn_subtitles: bool = False,
    subtitle_font_size: int = 26,
    quality_mode: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], bool]:
    """
    Builds per-shot render tasks with guaranteed `chapter_number` metadata and
    per-shot subtitle cues/font_size when `fast_renderer.build_shot_render_tasks`
    supports per-shot burn (avoiding serial post-concat re-encoding).
    """
    cues = None
    if burn_subtitles and subtitles_path is not None and Path(subtitles_path).is_file():
        cues = SubtitleGenerator().parse_srt(subtitles_path)

    sig_params = inspect.signature(build_shot_tasks_fn).parameters
    accepts_varkw = any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in sig_params.values()
    )
    supports_per_shot_burn = "burn_subtitles" in sig_params or accepts_varkw
    kwargs: Dict[str, Any] = {
        "work_dir": work_dir,
        "encoder": encoder,
    }
    per_shot_burned = bool(burn_subtitles and cues and supports_per_shot_burn)
    if "subtitle_cues" in sig_params or accepts_varkw:
        kwargs["subtitle_cues"] = cues
    if supports_per_shot_burn:
        kwargs["burn_subtitles"] = per_shot_burned
    if "subtitle_font_size" in sig_params or accepts_varkw:
        kwargs["subtitle_font_size"] = int(subtitle_font_size)
    if "quality_mode" in sig_params or accepts_varkw:
        kwargs["quality_mode"] = quality_mode

    tasks = build_shot_tasks_fn(chapter_dir, plan, manifest, **kwargs)
    for task in tasks:
        task.setdefault("chapter_number", str(chapter_num))
    return tasks, per_shot_burned


def _concat_chapter_shot_clips(
    concat_fn: Callable[..., Path],
    shot_clips: List[Path],
    output_video: Path,
    *,
    work_dir: Path,
    audio_path: Optional[Path],
    subtitle_path: Optional[Path],
    burn_subtitles: bool,
    per_shot_burned: bool,
    target_height: int,
    fps: int,
    encoder: str,
    subtitle_font_size: int = 26,
    quality_mode: Optional[str] = None,
    gpu_index: int = 0,
) -> Path:
    """Assembles shot clips using zero-reencode `-c:v copy` concat when subtitles were burned per-shot."""
    sig_params = inspect.signature(concat_fn).parameters
    accepts_varkw = any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in sig_params.values()
    )
    kwargs: Dict[str, Any] = {
        "work_dir": work_dir,
        "audio_path": audio_path,
        "subtitle_path": None if per_shot_burned else subtitle_path,
        "burn_subtitles": False if per_shot_burned else bool(burn_subtitles),
        "target_height": int(target_height),
        "fps": int(fps),
        "encoder": encoder,
    }
    if "subtitle_font_size" in sig_params or accepts_varkw:
        kwargs["subtitle_font_size"] = int(subtitle_font_size)
    if "quality_mode" in sig_params or accepts_varkw:
        kwargs["quality_mode"] = quality_mode
    if "gpu_index" in sig_params or accepts_varkw:
        kwargs["gpu_index"] = int(gpu_index)
    return concat_fn(shot_clips, output_video, **kwargs)


# ==============================================================================
# 4. Canonical TTSWorker & RenderWorker (No Placeholder Production Code)
# ==============================================================================


class TTSWorker:
    """
    Single-session TTS helper bound to an isolated GPU index.
    Enforces single-narrator Q03/Q04 setup, `RecapScript` Pydantic schema integrity,
    and `validate_audio` RMS/format verification.
    """

    def __init__(
        self,
        workspace_dir: Path,
        gpu_id: int = 0,
        mock_mode: bool = False,
        cuda_worker: Any = None,
    ):
        if mock_mode:
            raise ValueError(
                "Mock TTS mode is forbidden in Dual-T4 production TTSWorker; "
                "use real VieNeuCUDAWorker or mock only in unit tests via injected worker."
            )
        self.workspace_dir = Path(workspace_dir).resolve()
        self.gpu_id = int(gpu_id)
        self.cuda_worker = cuda_worker
        self.oom_tracker: Dict[str, int] = {"count": 0}
        self.telemetry = GpuDeviceTelemetry(
            device_id=self.gpu_id,
            device_name=f"GPU-{self.gpu_id}",
            assigned_task="VieNeu-TTS v3 Turbo",
        )

    def _ensure_worker(self) -> Any:
        if self.cuda_worker is None:
            mod = importlib.import_module("src.tts.cuda_worker")
            self.cuda_worker = mod.VieNeuCUDAWorker(
                gpu_id=0, mode="v3turbo", precision="auto", preload=True
            )
        return self.cuda_worker

    def synthesize_chapter(
        self,
        chap_dir: Path,
        chapter_num: str,
        voice_override: Optional[str] = None,
        speed_override: Optional[float] = None,
        batch_size: int = 8,
    ) -> Dict[str, Any]:
        t0 = time.monotonic()
        chap_dir = Path(chap_dir).resolve()
        script = load_recap_script(chap_dir)
        if script is None or not script.segments:
            raise ValueError(f"Canonical RecapScript with segments required in {chap_dir}")

        proj_cfg = read_json(self.workspace_dir / "project.json")
        audio_cfg = proj_cfg.get("audio_settings", {})
        default_voice = resolve_voice_id(voice_override or audio_cfg["default_voice"])
        default_speed = float(
            speed_override if speed_override is not None else audio_cfg["default_speed"]
        )
        if not (0.5 <= default_speed <= 2.0):
            raise ValueError("TTS speed must be in 0.5..2.0")

        tasks: List[Dict[str, Any]] = []
        audio_dir = chap_dir / "audio" / "clips"
        audio_dir.mkdir(parents=True, exist_ok=True)

        for seg in script.segments:
            seg_voice = resolve_voice_id(seg.voice_id or default_voice)
            if seg_voice != default_voice:
                raise ValueError("Segment voice conflicts with confirmed single narrator")
            out_wav = audio_dir / f"audio_{chapter_num}_{seg.seq_id}.wav"
            tasks.append(
                {
                    "seq_id": seg.seq_id,
                    "text": seg.narrator_text,
                    "voice": default_voice,
                    "speed": default_speed,
                    "emotion": seg.emotion,
                    "emotion_cue": seg.emotion_cue,
                    "output_path": str(out_wav),
                    "chapter_number": str(chapter_num),
                }
            )

        worker = self._ensure_worker()
        results = worker.synthesize_batch_tasks(tasks, batch_size=batch_size)
        if len(results) != len(script.segments):
            raise RuntimeError("TTS batch returned mismatched segment count")

        by_seq = {str(r["seq_id"]): r for r in results}
        total_dur = 0.0
        for seg in script.segments:
            res = by_seq[seg.seq_id]
            wav_path = Path(res["output_path"])
            report = validate_audio(wav_path)
            seg.audio_path = wav_path.relative_to(chap_dir).as_posix()
            seg.duration = report["duration"]
            seg.checksum = report["sha256"]
            seg.synthesized_voice = default_voice
            seg.voice_id = default_voice
            seg.voice_source = "tts"
            seg.fingerprint = calculate_utterance_fingerprint(
                text=seg.narrator_text,
                voice=default_voice,
                speed=default_speed,
                emotion=seg.emotion,
                emotion_cue=seg.emotion_cue,
                engine_mode="neural",
            )
            seg.status = "synthesized"
            total_dur += report["duration"]

        script.total_duration_seconds = round(total_dur, 4)
        atomic_write_json(chap_dir / "script.json", script)

        elapsed = max(0.001, time.monotonic() - t0)
        ready_payload = {
            "chapter": str(chapter_num),
            "status": "ready",
            "segment_count": len(script.segments),
            "total_duration_seconds": round(total_dur, 4),
            "tts_seconds": round(elapsed, 3),
            "throughput_utt_per_sec": round(len(script.segments) / elapsed, 2),
            "completed_at": utc_iso_now(),
        }
        atomic_write_json(chap_dir / f"ch_{chapter_num}_audio_ready.json", ready_payload)
        return ready_payload


class RenderWorker:
    """
    Canonical Dual-T4 chapter render helper using `VisualPlan`, `SubtitleGenerator`,
    `AudioMixer`, `fast_renderer`, and `validate_final_artifact`.
    Zero placeholder / black-frame generation is permitted.
    """

    def __init__(
        self,
        workspace_dir: Path,
        gpu_id: int = 1,
        render_workers: int = 2,
        mock_mode: bool = False,
        encoder: str = "auto",
    ):
        if mock_mode:
            raise ValueError(
                "Mock render mode is forbidden in Dual-T4 RenderWorker; "
                "all renders must consume canonical VisualPlan + panels."
            )
        self.workspace_dir = Path(workspace_dir).resolve()
        self.gpu_id = int(gpu_id)
        self.render_workers = max(1, int(render_workers))
        resolved_enc, _args, _msg = resolve_video_encoder(encoder)
        self.encoder = resolved_enc
        self.telemetry = GpuDeviceTelemetry(
            device_id=self.gpu_id,
            device_name=f"GPU-{self.gpu_id}",
            assigned_task="NVENC Hardware Render",
        )

    def _generate_srt(
        self,
        script: RecapScript,
        srt_path: Path,
        sub_settings: Optional[Dict[str, Any]] = None,
    ) -> Path:
        sub_cfg = sub_settings or {}
        sub_gen = SubtitleGenerator(
            max_chars_per_line=int(sub_cfg.get("max_chars_per_line", 40)),
            max_lines=int(sub_cfg.get("max_lines", 1)),
            chunking_mode=str(sub_cfg.get("chunking", "sentence-aware")),
        )
        return sub_gen.generate_srt_file(script, srt_path)

    def render_chapter(
        self,
        chap_dir: Path,
        chapter_num: str,
        project_config: Optional[Dict[str, Any]] = None,
        quality: str = "final",
    ) -> Dict[str, Any]:
        t0 = time.monotonic()
        chap_dir = Path(chap_dir).resolve()
        cfg = project_config or read_json(self.workspace_dir / "project.json")
        settings = cfg["render_settings"]
        sub_settings = settings["subtitle"]
        audio_settings = cfg["audio_settings"]

        script = load_recap_script(chap_dir)
        manifest = load_manifest(chap_dir)
        if script is None or manifest is None:
            raise ValueError(f"Missing canonical script/manifest in {chap_dir}")

        audio_paths = [chap_dir / seg.audio_path for seg in script.segments]
        for seg, ap in zip(script.segments, audio_paths):
            report = validate_audio(ap)
            if seg.checksum != report["sha256"] or abs((seg.duration or 0.0) - report["duration"]) > 0.001:
                raise ValueError(f"Audio evidence mismatch for segment {seg.seq_id}")

        plan = resolve_visual_plan(
            chap_dir,
            manifest,
            script,
            target_width=settings["target_width"],
            target_height=settings["target_height"],
            fps=settings["fps"],
            motion_enabled=settings["render_pan_mode"] == "animation",
        )
        plan.save(chap_dir / "visual_plan.json")

        subtitles_path = chap_dir / "subtitles" / f"{quality}.srt"
        self._generate_srt(script, subtitles_path, sub_settings=sub_settings)

        video_dir = chap_dir / "video"
        video_dir.mkdir(parents=True, exist_ok=True)
        out_mp4 = video_dir / f"{quality}.mp4"

        if quality == "final" and str(chapter_num) == "1" and (video_dir / "preview.mp4").is_file():
            shutil.copy2(video_dir / "preview.mp4", out_mp4)
        else:
            mixer = AudioMixer(silence_padding_sec=0, tail_padding_sec=0)
            speech_wav, _ = mixer.concatenate_speech(
                script.segments, chap_dir, chap_dir / "audio" / "speech_master.wav"
            )
            bgm = audio_settings.get("bgm_path")
            bgm_path = (
                (self.workspace_dir / bgm).resolve()
                if bgm and not Path(bgm).is_absolute()
                else bgm
            )
            mixed_m4a = mixer.mix_audio_track(
                speech_wav,
                chap_dir / "audio" / "mixed.m4a",
                bgm_path=bgm_path,
                bgm_volume=float(audio_settings["bgm_volume"]),
                voice_volume=float(audio_settings["voice_volume"]),
                fadeout_seconds=0,
            )
            build_tasks, render_shot_fn, concat_fn = _resolve_fast_renderer_api()
            shot_work = chap_dir / "video" / "_shots_work"
            sub_font_size = int(sub_settings.get("font_size", 26))
            tasks, per_shot_burned = _build_chapter_shot_tasks(
                build_tasks,
                chap_dir,
                str(chapter_num),
                plan,
                manifest,
                work_dir=shot_work,
                encoder=self.encoder,
                subtitles_path=subtitles_path,
                burn_subtitles=bool(settings["burn_subtitles"]),
                subtitle_font_size=sub_font_size,
                quality_mode=quality,
            )
            cache: Dict[Any, Any] = {}
            clips = [render_shot_fn(t, canvas_cache=cache, gpu_index=0) for t in tasks]
            _concat_chapter_shot_clips(
                concat_fn,
                clips,
                out_mp4,
                work_dir=shot_work,
                audio_path=mixed_m4a,
                subtitle_path=subtitles_path,
                burn_subtitles=bool(settings["burn_subtitles"]),
                per_shot_burned=per_shot_burned,
                target_height=int(settings["target_height"]),
                fps=int(settings["fps"]),
                encoder=self.encoder,
                subtitle_font_size=sub_font_size,
                quality_mode=quality,
                gpu_index=0,
            )
            shutil.rmtree(shot_work, ignore_errors=True)

        val_report = validate_final_artifact(
            out_mp4,
            subtitles_path,
            audio_paths,
            plan,
            script,
            manifest,
            str(chapter_num),
            chapter_dir=chap_dir,
        )
        atomic_write_json(chap_dir / "artifact_validation.json", val_report)
        elapsed = max(0.001, time.monotonic() - t0)
        return {
            "chapter": str(chapter_num),
            "status": "completed",
            "video_path": str(out_mp4),
            "subtitle_path": str(subtitles_path),
            "duration_seconds": plan.total_duration_sec,
            "render_seconds": round(elapsed, 3),
            "frames": val_report["video"]["frames"],
            "encoding_fps": round(val_report["video"]["frames"] / elapsed, 2),
            "validation": val_report,
        }


# ==============================================================================
# 5. Elastic Dual-T4 Streaming Orchestrator
# ==============================================================================


@dataclass
class _PreparedChapter:
    chapter_num: str
    chapter_dir: Path
    script: RecapScript
    manifest: PanelManifest
    promote_preview: bool = False
    cached_audio: bool = False


@dataclass
class _AudioReadyChapter:
    chapter_num: str
    chapter_dir: Path
    script: RecapScript
    manifest: PanelManifest
    tts_duration_seconds: float
    tts_throughput: float
    promote_preview: bool = False


@dataclass
class _ReadyChapterPackage:
    chapter_num: str
    chapter_dir: Path
    script: RecapScript
    manifest: PanelManifest
    plan: VisualPlan
    subtitles_path: Path
    mixed_audio_path: Optional[Path]
    output_video_path: Path
    shot_tasks: List[Dict[str, Any]]
    shot_work_dir: Path
    tts_duration_seconds: float
    tts_throughput: float
    promote_preview: bool = False
    per_shot_burned: bool = False
    subtitle_font_size: int = 26
    quality_mode: str = "final"


@dataclass
class _AssembledChapter:
    package: _ReadyChapterPackage
    render_duration_seconds: float
    audio_paths: List[Path]


class DualT4Orchestrator:
    """
    Coordinates spawn-isolated GPU worker processes (`CudaWorkerPool`),
    4-state `ElasticScheduler`, 6 bounded streaming queues, StartupAutotuner,
    and G8 receipt generation across multi-chapter series runs.
    """

    def __init__(
        self,
        workspace_dir: Path,
        chapters: Sequence[Union[str, int]],
        project_config: Optional[Dict[str, Any]] = None,
        *,
        render_workers: int = 2,
        compile_feature: bool = True,
        mock_mode: bool = False,
        profile: Union[str, BenchmarkProfileId, ArchitectureProfile] = BenchmarkProfileId.B5,
        encoder: str = "auto",
        quality: str = "final",
        autotune: bool = False,
        autotune_probe_fn: Optional[Callable[..., Any]] = None,
        work_dir: Optional[Path] = None,
        receipts_dir: Optional[Path] = None,
        worker_pool_factory: Optional[Callable[..., CudaWorkerPool]] = None,
        smi_querier: Optional[Callable[[], List[Dict[str, Any]]]] = None,
        enforce_setup: bool = True,
    ):
        if mock_mode:
            raise ValueError(
                "Mock production mode is disabled in DualT4Orchestrator; "
                "use injected unit worker handles or real neural/FFmpeg execution."
            )
        self.workspace_dir = Path(workspace_dir).resolve()
        self.chapters = [str(c).strip() for c in chapters if str(c).strip()]
        if not self.chapters:
            raise ValueError("DualT4Orchestrator requires at least one chapter")
        self.project_config = project_config or read_json(self.workspace_dir / "project.json")
        self.compile_feature = bool(compile_feature)
        self.quality = str(quality)
        self.profile = resolve_architecture_profile(profile)
        self.render_workers = max(1, int(render_workers or self.profile.nvenc_workers_per_gpu))
        self.autotune = bool(autotune)
        self.autotune_probe_fn = autotune_probe_fn
        self.work_dir = Path(work_dir).resolve() if work_dir else (self.workspace_dir / "_p8_work")
        self.receipts_dir = Path(receipts_dir).resolve() if receipts_dir else self.workspace_dir
        self.worker_pool_factory = worker_pool_factory
        self.smi_querier = smi_querier
        self.enforce_setup = bool(enforce_setup)

        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.receipts_dir.mkdir(parents=True, exist_ok=True)

        if encoder in ("auto", "nvenc", "h264_nvenc", "libx264"):
            resolved_enc, _, _ = resolve_video_encoder(encoder)
            self.encoder = resolved_enc
        else:
            self.encoder = encoder

        self.queues = ElasticPipelineQueues()
        self.scheduler = ElasticScheduler(self.profile)
        self.frozen_session: Optional[FrozenSessionProfile] = None

    def _resolve_chapter_dir(self, chapter_num: str) -> Path:
        return self.workspace_dir / "chapters" / str(chapter_num)

    def _read_voice_and_speed(self) -> Tuple[str, float]:
        if self.enforce_setup:
            from src.core.series_setup import require_series_setup

            slug = self.project_config["slug"]
            setup = require_series_setup(slug, project_dir=self.workspace_dir)
            voice = resolve_voice_id(str(setup.answers["Q03"].user_answer))
            speed = float(str(setup.answers["Q04"].user_answer).lower().rstrip("x"))
            return voice, speed
        audio_cfg = self.project_config.get("audio_settings", {})
        return resolve_voice_id(audio_cfg.get("default_voice", "Ngọc Huyền")), float(
            audio_cfg.get("default_speed", 1.0)
        )

    def _collect_environment_receipt(self, smi_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        gpus = [
            {
                "gpu_id": r["gpu_id"],
                "gpu_uuid": r["gpu_uuid"],
                "name": r["name"],
                "vram_total_mb": r["vram_total_mb"],
                "usable_vram_budget_mb": r.get("usable_vram_budget_mb")
                or compute_usable_vram_budget(r.get("vram_total_mb", 0.0)),
            }
            for r in smi_rows
        ]
        ffmpeg_caps: Dict[str, Any] = {}
        try:
            hw_mod = importlib.import_module("src.production.hardware")
            if callable(getattr(hw_mod, "probe_ffmpeg_capabilities", None)):
                ffmpeg_caps = hw_mod.probe_ffmpeg_capabilities()
        except Exception:
            pass
        return {
            "schema_version": "4.2.0",
            "generated_at_utc": utc_iso_now(),
            "platform": platform.platform(),
            "python_version": platform.python_version(),
            "spawn_method": "spawn",
            "profile_id": self.profile.profile_id.value,
            "encoder": self.encoder,
            "gpu_count": len(gpus),
            "gpus": gpus,
            "ffmpeg_capabilities": ffmpeg_caps,
            "mock_mode": False,
        }

    def run_pipeline(self) -> SeriesTelemetry:
        t_start = time.monotonic()
        voice_id, speech_speed = self._read_voice_and_speed()
        render_settings = self.project_config["render_settings"]
        sub_settings = render_settings["subtitle"]
        audio_settings = self.project_config["audio_settings"]

        # Optional startup autotuner (never assumes winning parameters)
        effective_replicas = self.profile.tts_replicas_per_gpu
        effective_batch = self.profile.tts_batch_size
        effective_nvenc = self.render_workers
        if self.autotune and self.autotune_probe_fn is not None:
            smi_init = (self.smi_querier or query_nvidia_smi)() or []
            total_mb = float(smi_init[0]["vram_total_mb"]) if smi_init else 15360.0
            tuner = StartupAutotuner(total_vram_mb=total_mb)
            self.frozen_session = tuner.autotune(self.autotune_probe_fn)
            effective_replicas = self.frozen_session.tts_replicas_per_gpu
            effective_batch = self.frozen_session.tts_batch_size
            effective_nvenc = self.frozen_session.nvenc_workers_per_gpu

        trace_csv = self.receipts_dir / "dual_t4_gpu_trace.csv"
        worker_log = self.receipts_dir / "dual_t4_worker_log.jsonl"

        sampler_state_lock = threading.Lock()
        in_flight_tts = 0
        in_flight_render = 0
        all_tts_submitted = False
        active_tts_gpus: Tuple[int, ...] = (self.profile.gpu_ids[0],)
        active_render_gpus: Tuple[int, ...] = (
            (self.profile.gpu_ids[-1],) if len(self.profile.gpu_ids) > 1 else self.profile.gpu_ids
        )

        def _sampler_state_provider() -> Tuple[str, Dict[int, int]]:
            counts = pool.active_worker_counts() if pool is not None else {}
            return self.scheduler.current_state.value, counts

        pool: Optional[CudaWorkerPool] = None
        telemetry_sampler = GpuTelemetrySampler(
            trace_csv,
            worker_log,
            poll_interval_sec=1.0,
            gpu_ids=self.profile.gpu_ids,
            smi_querier=self.smi_querier,
            state_provider=_sampler_state_provider,
        )

        if self.worker_pool_factory is not None:
            pool = self.worker_pool_factory(
                gpu_ids=self.profile.gpu_ids,
                tts_replicas_per_gpu=effective_replicas,
                nvenc_workers_per_gpu=effective_nvenc,
                initial_tts_batch_size=effective_batch,
                telemetry=telemetry_sampler,
            )
        else:
            pool = CudaWorkerPool(
                gpu_ids=self.profile.gpu_ids,
                tts_replicas_per_gpu=effective_replicas,
                nvenc_workers_per_gpu=effective_nvenc,
                initial_tts_batch_size=effective_batch,
                telemetry=telemetry_sampler,
            )

        smi_initial = (self.smi_querier or query_nvidia_smi)() or []
        env_receipt = self._collect_environment_receipt(smi_initial)
        atomic_write_json(self.receipts_dir / "dual_t4_environment.json", env_receipt)

        telemetry_sampler.start()

        abort_event = threading.Event()
        errors: List[BaseException] = []
        error_lock = threading.Lock()

        def _record_error(exc: BaseException) -> None:
            with error_lock:
                errors.append(exc)
            abort_event.set()

        chapter_tts_metrics: Dict[str, Tuple[float, float, int]] = {}
        chapter_render_metrics: Dict[str, Tuple[float, int, float]] = {}
        chapter_artifacts: Dict[str, Dict[str, Any]] = {}
        rendered_video_paths: Dict[str, Path] = {}
        per_chapter_lock = threading.Lock()
        all_tts_done_event = threading.Event()

        def _snapshot_and_evaluate() -> None:
            nonlocal active_tts_gpus, active_render_gpus
            with sampler_state_lock:
                vram_used = {}
                vram_budget = {}
                for g in self.profile.gpu_ids:
                    latest = telemetry_sampler.get_latest_sample(g)
                    if latest is not None:
                        vram_used[g] = latest.vram_used_mb
                        vram_budget[g] = latest.usable_vram_budget_mb
                snap = QueueSnapshot(
                    source_prefetch_remaining=self.queues.source_prefetch_queue.qsize(),
                    pending_tts_tasks=self.queues.tts_queue.qsize(),
                    in_flight_tts=in_flight_tts,
                    audio_ready_chapters=self.queues.audio_ready_queue.qsize(),
                    chapter_ready_chapters=self.queues.chapter_ready_queue.qsize(),
                    pending_render_shots=self.queues.render_queue.qsize(),
                    in_flight_render_shots=in_flight_render,
                    pending_validations=self.queues.validation_queue.qsize(),
                    all_tts_submitted=all_tts_submitted,
                    vram_used_mb=vram_used,
                    vram_budget_mb=vram_budget,
                )
            decision = self.scheduler.evaluate_state(snap)
            active_tts_gpus = decision.tts_gpu_ids
            active_render_gpus = decision.render_gpu_ids
            if decision.unload_tts_on_gpus:
                pool.unload_tts_workers(decision.unload_tts_on_gpus)

        # ---- Stage 1: CPU Source Prefetch -> source_prefetch_queue -> tts_queue ----
        def _prefetch_stage() -> None:
            try:
                for ch_num in self.chapters:
                    if abort_event.is_set():
                        break
                    chap_dir = self._resolve_chapter_dir(ch_num)
                    if not chap_dir.is_dir():
                        raise FileNotFoundError(f"Chapter directory missing: {chap_dir}")
                    script = load_recap_script(chap_dir)
                    manifest = load_manifest(chap_dir)
                    validate_narration_evidence(chap_dir, script, manifest)
                    if script.project_slug != self.project_config["slug"] or script.chapter_number != str(ch_num):
                        raise ValueError(f"Chapter identity mismatch: {ch_num}")

                    # Respect Chapter 1 human-reviewed preview -> final promotion
                    promote_preview = (
                        str(ch_num) == "1"
                        and self.quality == "final"
                        and (chap_dir / "video" / "preview.mp4").is_file()
                        and (self.workspace_dir / "preview_approval.json").is_file()
                    )

                    # Check cached audio clips
                    cached_audio = True
                    for seg in script.segments:
                        if not seg.audio_path or not seg.checksum:
                            cached_audio = False
                            break
                        cand = (chap_dir / seg.audio_path).resolve()
                        if not cand.is_relative_to(chap_dir.resolve()) or not cand.is_file():
                            cached_audio = False
                            break
                        try:
                            rep = validate_audio(cand)
                            if rep["sha256"] != seg.checksum:
                                cached_audio = False
                                break
                        except Exception:
                            cached_audio = False
                            break

                    item = _PreparedChapter(
                        chapter_num=str(ch_num),
                        chapter_dir=chap_dir,
                        script=script,
                        manifest=manifest,
                        promote_preview=promote_preview,
                        cached_audio=cached_audio,
                    )
                    self.queues.source_prefetch_queue.put(item)
                    self.queues.tts_queue.put(self.queues.source_prefetch_queue.get())
                    self.queues.source_prefetch_queue.task_done()
            except BaseException as exc:
                _record_error(exc)
            finally:
                self.queues.tts_queue.put(None)

        # ---- Stage 2: Elastic TTS Stage -> audio_ready_queue ----
        def _tts_stage() -> None:
            nonlocal in_flight_tts, all_tts_submitted
            rr_gpu = 0
            try:
                while True:
                    if abort_event.is_set():
                        break
                    try:
                        item: Optional[_PreparedChapter] = self.queues.tts_queue.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    if item is None:
                        self.queues.tts_queue.task_done()
                        break

                    ch_num = item.chapter_num
                    chap_dir = item.chapter_dir
                    script = item.script
                    t_ch0 = time.monotonic()

                    if item.cached_audio or item.promote_preview:
                        tts_dur = max(0.001, time.monotonic() - t_ch0)
                        throughput = len(script.segments) / tts_dur
                    else:
                        update_checkpoint(chap_dir, StageType.AUDIO, CheckpointStatus.RUNNING)
                        audio_dir = chap_dir / "audio" / "clips"
                        audio_dir.mkdir(parents=True, exist_ok=True)
                        raw_tasks: List[Dict[str, Any]] = []
                        for seg in script.segments:
                            seg_voice = resolve_voice_id(seg.voice_id or voice_id)
                            if seg_voice != voice_id:
                                raise ValueError(
                                    f"Segment {seg.seq_id} voice '{seg_voice}' conflicts with Q03 '{voice_id}'"
                                )
                            raw_tasks.append(
                                {
                                    "seq_id": seg.seq_id,
                                    "text": seg.narrator_text,
                                    "narrator_text": seg.narrator_text,
                                    "voice": voice_id,
                                    "voice_id": voice_id,
                                    "speed": speech_speed,
                                    "emotion": seg.emotion,
                                    "emotion_cue": seg.emotion_cue,
                                    "output_path": str(audio_dir / f"audio_{ch_num}_{seg.seq_id}.wav"),
                                    "chapter_number": ch_num,
                                }
                            )

                        # Length-bucket batches via MicroBatchPlanner
                        batches = MicroBatchPlanner.create_batches(
                            raw_tasks, max_batch_size=effective_batch
                        )
                        _snapshot_and_evaluate()
                        target_gpus = active_tts_gpus or (self.profile.gpu_ids[0],)

                        all_tts_results: Dict[str, Dict[str, Any]] = {}
                        with sampler_state_lock:
                            in_flight_tts += len(raw_tasks)
                        try:
                            for batch in batches:
                                if abort_event.is_set():
                                    break
                                _snapshot_and_evaluate()
                                gpus_now = active_tts_gpus or (self.profile.gpu_ids[0],)
                                gid = gpus_now[rr_gpu % len(gpus_now)]
                                rr_gpu += 1
                                batch_res = pool.submit_tts_batch(
                                    gid,
                                    batch,
                                    batch_size=min(effective_batch, len(batch)),
                                    chapter=ch_num,
                                )
                                for r in batch_res:
                                    all_tts_results[str(r["seq_id"])] = r
                        finally:
                            with sampler_state_lock:
                                in_flight_tts = max(0, in_flight_tts - len(raw_tasks))

                        if len(all_tts_results) != len(script.segments):
                            raise RuntimeError(
                                f"Chapter {ch_num} TTS incomplete: {len(all_tts_results)}/{len(script.segments)}"
                            )

                        total_audio_dur = 0.0
                        artifacts: List[str] = []
                        for seg in script.segments:
                            r = all_tts_results[seg.seq_id]
                            wav_p = Path(r["output_path"])
                            rep = validate_audio(wav_p)
                            seg.audio_path = wav_p.relative_to(chap_dir).as_posix()
                            seg.duration = rep["duration"]
                            seg.checksum = rep["sha256"]
                            seg.synthesized_voice = voice_id
                            seg.voice_id = voice_id
                            seg.voice_source = "tts"
                            seg.fingerprint = calculate_utterance_fingerprint(
                                text=seg.narrator_text,
                                voice=voice_id,
                                speed=speech_speed,
                                emotion=seg.emotion,
                                emotion_cue=seg.emotion_cue,
                                engine_mode="neural",
                            )
                            seg.status = "synthesized"
                            total_audio_dur += rep["duration"]
                            artifacts.append(seg.audio_path)

                        script.total_duration_seconds = round(total_audio_dur, 4)
                        atomic_write_json(chap_dir / "script.json", script)
                        update_checkpoint(
                            chap_dir,
                            StageType.AUDIO,
                            CheckpointStatus.COMPLETED,
                            artifacts=artifacts,
                        )
                        tts_dur = max(0.001, time.monotonic() - t_ch0)
                        throughput = len(script.segments) / tts_dur

                        atomic_write_json(
                            chap_dir / f"ch_{ch_num}_audio_ready.json",
                            {
                                "chapter": ch_num,
                                "status": "ready",
                                "segment_count": len(script.segments),
                                "total_duration_seconds": round(total_audio_dur, 4),
                                "tts_seconds": round(tts_dur, 3),
                                "throughput_utt_per_sec": round(throughput, 2),
                                "completed_at": utc_iso_now(),
                            },
                        )

                    with per_chapter_lock:
                        chapter_tts_metrics[ch_num] = (tts_dur, throughput, len(script.segments))

                    self.queues.audio_ready_queue.put(
                        _AudioReadyChapter(
                            chapter_num=ch_num,
                            chapter_dir=chap_dir,
                            script=script,
                            manifest=item.manifest,
                            tts_duration_seconds=tts_dur,
                            tts_throughput=throughput,
                            promote_preview=item.promote_preview,
                        )
                    )
                    self.queues.tts_queue.task_done()
                    _snapshot_and_evaluate()

                all_tts_submitted = True
                _snapshot_and_evaluate()
                all_tts_done_event.set()
            except BaseException as exc:
                _record_error(exc)
            finally:
                all_tts_done_event.set()
                self.queues.audio_ready_queue.put(None)

        # ---- Stage 3: CPU Prep (VisualPlan + SRT + AudioMixer + ShotTasks) -> chapter_ready_queue ----
        build_shot_tasks_fn, _, concat_shot_clips_fn = _resolve_fast_renderer_api()

        def _chapter_prep_stage() -> None:
            try:
                while True:
                    if abort_event.is_set():
                        break
                    try:
                        ready: Optional[_AudioReadyChapter] = self.queues.audio_ready_queue.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    if ready is None:
                        self.queues.audio_ready_queue.task_done()
                        break

                    ch_num = ready.chapter_num
                    chap_dir = ready.chapter_dir
                    script = ready.script
                    manifest = ready.manifest

                    plan = resolve_visual_plan(
                        chap_dir,
                        manifest,
                        script,
                        target_width=render_settings["target_width"],
                        target_height=render_settings["target_height"],
                        fps=render_settings["fps"],
                        motion_enabled=render_settings["render_pan_mode"] == "animation",
                    )
                    plan.save(chap_dir / "visual_plan.json")

                    quality_name = (
                        "preview"
                        if (ch_num == "1" and self.quality == "preview")
                        else "final"
                    )
                    subtitles_path = chap_dir / "subtitles" / f"{quality_name}.srt"
                    sub_gen = SubtitleGenerator(
                        max_chars_per_line=int(sub_settings.get("max_chars_per_line", 40)),
                        max_lines=int(sub_settings.get("max_lines", 1)),
                        chunking_mode=str(sub_settings.get("chunking", "sentence-aware")),
                    )
                    sub_gen.generate_srt_file(script, subtitles_path)

                    video_dir = chap_dir / "video"
                    video_dir.mkdir(parents=True, exist_ok=True)
                    output_video_path = video_dir / f"{quality_name}.mp4"
                    shot_work_dir = self.work_dir / f"ch_{ch_num}_shots"

                    if ready.promote_preview:
                        pkg = _ReadyChapterPackage(
                            chapter_num=ch_num,
                            chapter_dir=chap_dir,
                            script=script,
                            manifest=manifest,
                            plan=plan,
                            subtitles_path=subtitles_path,
                            mixed_audio_path=None,
                            output_video_path=output_video_path,
                            shot_tasks=[],
                            shot_work_dir=shot_work_dir,
                            tts_duration_seconds=ready.tts_duration_seconds,
                            tts_throughput=ready.tts_throughput,
                            promote_preview=True,
                        )
                    else:
                        mixer = AudioMixer(silence_padding_sec=0, tail_padding_sec=0)
                        speech_wav, _ = mixer.concatenate_speech(
                            script.segments, chap_dir, chap_dir / "audio" / "speech_master.wav"
                        )
                        bgm = audio_settings.get("bgm_path")
                        bgm_path = (
                            (self.workspace_dir / bgm).resolve()
                            if bgm and not Path(bgm).is_absolute()
                            else bgm
                        )
                        mixed_m4a = mixer.mix_audio_track(
                            speech_wav,
                            chap_dir / "audio" / "mixed.m4a",
                            bgm_path=bgm_path,
                            bgm_volume=float(audio_settings["bgm_volume"]),
                            voice_volume=float(audio_settings["voice_volume"]),
                            fadeout_seconds=0,
                        )
                        sub_font_size = int(sub_settings.get("font_size", 26))
                        shot_tasks, per_shot_burned = _build_chapter_shot_tasks(
                            build_shot_tasks_fn,
                            chap_dir,
                            ch_num,
                            plan,
                            manifest,
                            work_dir=shot_work_dir,
                            encoder=self.encoder,
                            subtitles_path=subtitles_path,
                            burn_subtitles=bool(render_settings["burn_subtitles"]),
                            subtitle_font_size=sub_font_size,
                            quality_mode=quality_name,
                        )
                        pkg = _ReadyChapterPackage(
                            chapter_num=ch_num,
                            chapter_dir=chap_dir,
                            script=script,
                            manifest=manifest,
                            plan=plan,
                            subtitles_path=subtitles_path,
                            mixed_audio_path=mixed_m4a,
                            output_video_path=output_video_path,
                            shot_tasks=shot_tasks,
                            shot_work_dir=shot_work_dir,
                            tts_duration_seconds=ready.tts_duration_seconds,
                            tts_throughput=ready.tts_throughput,
                            promote_preview=False,
                            per_shot_burned=per_shot_burned,
                            subtitle_font_size=sub_font_size,
                            quality_mode=quality_name,
                        )

                    self.queues.chapter_ready_queue.put(pkg)
                    self.queues.audio_ready_queue.task_done()
                    _snapshot_and_evaluate()
            except BaseException as exc:
                _record_error(exc)
            finally:
                self.queues.chapter_ready_queue.put(None)

        # ---- Stage 4: Elastic Work-Stealing Render Pool -> validation_queue ----
        chapter_trackers_lock = threading.Lock()
        chapter_trackers: Dict[str, Dict[str, Any]] = {}

        def _render_dispatcher() -> None:
            try:
                if self.profile.phase_barrier:
                    all_tts_done_event.wait()

                while True:
                    if abort_event.is_set():
                        break
                    try:
                        pkg: Optional[_ReadyChapterPackage] = self.queues.chapter_ready_queue.get(
                            timeout=0.1
                        )
                    except queue.Empty:
                        continue
                    if pkg is None:
                        self.queues.chapter_ready_queue.task_done()
                        break

                    ch_num = pkg.chapter_num
                    update_checkpoint(pkg.chapter_dir, StageType.RENDER, CheckpointStatus.RUNNING)

                    if pkg.promote_preview:
                        shutil.copy2(pkg.chapter_dir / "video" / "preview.mp4", pkg.output_video_path)
                        audio_paths = [pkg.chapter_dir / s.audio_path for s in pkg.script.segments]
                        self.queues.validation_queue.put(
                            _AssembledChapter(
                                package=pkg,
                                render_duration_seconds=0.01,
                                audio_paths=audio_paths,
                            )
                        )
                        self.queues.chapter_ready_queue.task_done()
                        continue

                    with chapter_trackers_lock:
                        chapter_trackers[ch_num] = {
                            "pkg": pkg,
                            "start_time": time.monotonic(),
                            "remaining": len(pkg.shot_tasks),
                            "clips": [None] * len(pkg.shot_tasks),
                        }

                    for idx, stask in enumerate(pkg.shot_tasks):
                        self.queues.render_queue.put((ch_num, idx, stask))
                    self.queues.chapter_ready_queue.task_done()
                    _snapshot_and_evaluate()
            except BaseException as exc:
                _record_error(exc)
            finally:
                # Signal render workers to finish once render_queue empties
                total_slots = max(1, len(self.profile.gpu_ids) * effective_nvenc)
                for _ in range(total_slots):
                    self.queues.render_queue.put(None)

        def _gpu_render_stealer(gpu_id: int) -> None:
            nonlocal in_flight_render
            try:
                while True:
                    if abort_event.is_set():
                        break
                    try:
                        item = self.queues.render_queue.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    if item is None:
                        self.queues.render_queue.task_done()
                        break

                    ch_num, shot_idx, stask = item
                    _snapshot_and_evaluate()
                    # Respect elastic GPU allocation when TTS is still active on gpu_id
                    if gpu_id not in active_render_gpus and active_render_gpus:
                        # If B1/B3/B4 keeps GPU0 dedicated to TTS while GPU1 renders,
                        # let eligible render GPUs handle the shot instead of stealing prematurely.
                        if not self.queues.render_queue.empty() or len(active_render_gpus) > 0:
                            # Re-queue when this GPU is momentarily reserved exclusively for TTS
                            if self.profile.profile_id == BenchmarkProfileId.B1 and gpu_id == 0:
                                self.queues.render_queue.put(item)
                                self.queues.render_queue.task_done()
                                time.sleep(0.05)
                                continue

                    with sampler_state_lock:
                        in_flight_render += 1
                    try:
                        clip_path = pool.submit_render_shot(gpu_id, stask, chapter=ch_num)
                    finally:
                        with sampler_state_lock:
                            in_flight_render = max(0, in_flight_render - 1)

                    assembled_pkg: Optional[_ReadyChapterPackage] = None
                    ordered_clips: Optional[List[Path]] = None
                    render_start = 0.0
                    with chapter_trackers_lock:
                        tr = chapter_trackers[ch_num]
                        tr["clips"][shot_idx] = clip_path
                        tr["remaining"] -= 1
                        if tr["remaining"] == 0:
                            assembled_pkg = tr["pkg"]
                            ordered_clips = list(tr["clips"])
                            render_start = tr["start_time"]

                    if assembled_pkg is not None and ordered_clips is not None:
                        _concat_chapter_shot_clips(
                            concat_shot_clips_fn,
                            ordered_clips,
                            assembled_pkg.output_video_path,
                            work_dir=assembled_pkg.shot_work_dir,
                            audio_path=assembled_pkg.mixed_audio_path,
                            subtitle_path=assembled_pkg.subtitles_path,
                            burn_subtitles=bool(render_settings["burn_subtitles"]),
                            per_shot_burned=assembled_pkg.per_shot_burned,
                            target_height=int(render_settings["target_height"]),
                            fps=int(render_settings["fps"]),
                            encoder=self.encoder,
                            subtitle_font_size=assembled_pkg.subtitle_font_size,
                            quality_mode=assembled_pkg.quality_mode,
                            gpu_index=0,
                        )
                        shutil.rmtree(assembled_pkg.shot_work_dir, ignore_errors=True)
                        rnd_dur = max(0.001, time.monotonic() - render_start)
                        audio_paths = [
                            assembled_pkg.chapter_dir / s.audio_path
                            for s in assembled_pkg.script.segments
                        ]
                        self.queues.validation_queue.put(
                            _AssembledChapter(
                                package=assembled_pkg,
                                render_duration_seconds=rnd_dur,
                                audio_paths=audio_paths,
                            )
                        )
                    self.queues.render_queue.task_done()
                    _snapshot_and_evaluate()
            except BaseException as exc:
                _record_error(exc)

        # ---- Stage 5: Validation Queue Consumer ----
        def _validation_stage() -> None:
            completed_count = 0
            target_count = len(self.chapters)
            try:
                while completed_count < target_count:
                    if abort_event.is_set() and self.queues.validation_queue.empty():
                        break
                    try:
                        item: Optional[_AssembledChapter] = self.queues.validation_queue.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    if item is None:
                        self.queues.validation_queue.task_done()
                        break

                    pkg = item.package
                    ch_num = pkg.chapter_num
                    val_report = validate_final_artifact(
                        pkg.output_video_path,
                        pkg.subtitles_path,
                        item.audio_paths,
                        pkg.plan,
                        pkg.script,
                        pkg.manifest,
                        ch_num,
                        chapter_dir=pkg.chapter_dir,
                    )
                    atomic_write_json(pkg.chapter_dir / "artifact_validation.json", val_report)
                    update_checkpoint(
                        pkg.chapter_dir,
                        StageType.RENDER,
                        CheckpointStatus.COMPLETED,
                        artifacts=[
                            pkg.output_video_path.relative_to(pkg.chapter_dir).as_posix(),
                            pkg.subtitles_path.relative_to(pkg.chapter_dir).as_posix(),
                            "visual_plan.json",
                            "artifact_validation.json",
                        ],
                    )
                    with acquire_lock(pkg.chapter_dir / "chapter.json"):
                        meta = load_chapter_metadata(pkg.chapter_dir)
                        meta.technical_status = CheckpointStatus.COMPLETED
                        meta.overall_status = (
                            CheckpointStatus.NEEDS_REVIEW
                            if pkg.output_video_path.name == "preview.mp4"
                            else CheckpointStatus.COMPLETED
                        )
                        meta.human_review_status = (
                            HumanReviewStatus.APPROVED
                            if (ch_num == "1" and pkg.promote_preview)
                            else HumanReviewStatus.UNREVIEWED
                        )
                        atomic_write_json(pkg.chapter_dir / "chapter.json", meta)

                    frames = int(val_report["video"]["frames"])
                    fps_eff = round(frames / max(0.001, item.render_duration_seconds), 2)
                    with per_chapter_lock:
                        chapter_render_metrics[ch_num] = (
                            item.render_duration_seconds,
                            frames,
                            fps_eff,
                        )
                        rendered_video_paths[ch_num] = pkg.output_video_path
                        chapter_artifacts[ch_num] = {
                            "video_path": str(pkg.output_video_path),
                            "subtitle_path": str(pkg.subtitles_path),
                            "validation": val_report,
                        }
                    completed_count += 1
                    self.queues.validation_queue.task_done()
            except BaseException as exc:
                _record_error(exc)

        # Launch pipeline coordinator threads (all GPU compute runs inside `spawn` workers!)
        threads: List[threading.Thread] = [
            threading.Thread(target=_prefetch_stage, name="P8-Prefetch", daemon=True),
            threading.Thread(target=_tts_stage, name="P8-TTS", daemon=True),
            threading.Thread(target=_chapter_prep_stage, name="P8-Prep", daemon=True),
            threading.Thread(target=_render_dispatcher, name="P8-RenderDisp", daemon=True),
            threading.Thread(target=_validation_stage, name="P8-Validate", daemon=True),
        ]
        # Dedicated stealer threads feed each GPU's spawn worker slots
        render_stealer_gpus: List[int] = []
        if len(self.profile.gpu_ids) == 1:
            render_stealer_gpus = [self.profile.gpu_ids[0]] * effective_nvenc
        elif self.profile.profile_id == BenchmarkProfileId.B1:
            render_stealer_gpus = [self.profile.gpu_ids[1]] * effective_nvenc
        else:
            for g in self.profile.gpu_ids:
                render_stealer_gpus.extend([g] * effective_nvenc)

        for idx, gid in enumerate(render_stealer_gpus):
            threads.append(
                threading.Thread(
                    target=_gpu_render_stealer,
                    args=(gid,),
                    name=f"P8-Stealer-G{gid}-{idx}",
                    daemon=True,
                )
            )

        try:
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            telemetry_sampler.stop()
            pool.shutdown()

        if errors:
            raise errors[0]

        ordered_videos = [
            rendered_video_paths[c] for c in self.chapters if c in rendered_video_paths
        ]
        compiled_path: Optional[Path] = None
        if self.compile_feature and len(ordered_videos) > 1:
            compiled_path = self._compile_feature_video(ordered_videos)

        total_wall = max(0.001, time.monotonic() - t_start)
        per_chap_tel: Dict[str, ChapterTelemetry] = {}
        seq_est = 0.0
        for ch_num in self.chapters:
            t_dur, t_tp, seg_cnt = chapter_tts_metrics.get(ch_num, (0.0, 0.0, 0))
            r_dur, v_frames, r_fps = chapter_render_metrics.get(ch_num, (0.0, 0, 0.0))
            seq_est += t_dur + r_dur
            v_dur = float(
                chapter_artifacts.get(ch_num, {})
                .get("validation", {})
                .get("video", {})
                .get("duration", 0.0)
            )
            per_chap_tel[ch_num] = ChapterTelemetry(
                chapter=ch_num,
                tts_duration_seconds=round(t_dur, 3),
                render_duration_seconds=round(r_dur, 3),
                segment_count=seg_cnt,
                video_frames_count=v_frames,
                video_duration_seconds=v_dur,
                tts_throughput_utt_per_sec=round(t_tp, 2),
                render_fps=r_fps,
            )

        pool_stats = pool.get_stats()
        tel_summary = telemetry_sampler.summarize()

        def _build_gpu_tel(gid: int, task_label: str) -> Optional[GpuDeviceTelemetry]:
            if gid not in self.profile.gpu_ids:
                return None
            st = pool_stats.get(gid, {})
            gp_sum = tel_summary["per_gpu"].get(str(gid), {})
            processed = int(st.get("tts_items", 0)) + int(st.get("render_shots", 0))
            active_s = float(st.get("active_seconds", 0.0))
            return GpuDeviceTelemetry(
                device_id=gid,
                device_name=f"Tesla T4 GPU {gid}",
                assigned_task=task_label,
                peak_vram_mb=float(gp_sum.get("peak_vram_mb", 0.0)),
                total_active_seconds=round(active_s, 3),
                processed_items=processed,
                throughput_metric=round(processed / max(0.001, active_s), 2),
                oom_events_count=int(st.get("oom_events", 0)),
            )

        speedup = round(seq_est / total_wall, 2) if len(self.chapters) > 1 and total_wall > 0 else 1.0
        series_tel = SeriesTelemetry(
            chapters=list(self.chapters),
            total_wall_clock_seconds=round(total_wall, 3),
            sequential_estimate_seconds=round(seq_est, 3),
            speedup_factor=speedup,
            pipeline_overlap_seconds=round(max(0.0, seq_est - total_wall), 3),
            profile_id=self.profile.profile_id.value,
            gpu_0=_build_gpu_tel(0, "Elastic TTS + NVENC"),
            gpu_1=_build_gpu_tel(1, "Elastic TTS + NVENC"),
            per_chapter=per_chap_tel,
            scheduler_transitions=self.scheduler.history,
            frozen_profile=self.frozen_session.to_dict() if self.frozen_session else None,
        )

        atomic_write_json(self.receipts_dir / "series_telemetry.json", series_tel.to_dict())
        artifact_report = {
            "schema_version": "4.2.0",
            "project_slug": self.project_config.get("slug"),
            "chapters": list(self.chapters),
            "profile_id": self.profile.profile_id.value,
            "encoder": self.encoder,
            "compiled_feature_video": str(compiled_path) if compiled_path else None,
            "gpu_usage_evidence": {
                str(gid): {
                    "tts_tasks": int(pool_stats.get(gid, {}).get("tts_tasks", 0)),
                    "tts_items": int(pool_stats.get(gid, {}).get("tts_items", 0)),
                    "render_shots": int(pool_stats.get(gid, {}).get("render_shots", 0)),
                    "oom_events": int(pool_stats.get(gid, {}).get("oom_events", 0)),
                    "crash_events": int(pool_stats.get(gid, {}).get("crash_events", 0)),
                    "peak_vram_mb": tel_summary["per_gpu"].get(str(gid), {}).get("peak_vram_mb", 0.0),
                }
                for gid in self.profile.gpu_ids
            },
            "per_chapter": chapter_artifacts,
            "telemetry": series_tel.to_dict(),
            "generated_at_utc": utc_iso_now(),
        }
        atomic_write_json(self.receipts_dir / "dual_t4_artifact_report.json", artifact_report)
        return series_tel

    def _compile_feature_video(self, video_paths: List[Path]) -> Optional[Path]:
        out_feature = self.workspace_dir / "outputs" / "full_recap_feature.mp4"
        out_feature.parent.mkdir(parents=True, exist_ok=True)
        concat_list = self.work_dir / "concat_feature.txt"
        concat_list.write_text(
            "\n".join(f"file '{Path(vp).resolve().as_posix()}'" for vp in video_paths if Path(vp).exists()) + "\n",
            encoding="utf-8",
        )
        partial = out_feature.with_name(f".{out_feature.stem}.partial.mp4")
        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-y",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(concat_list),
                    "-c",
                    "copy",
                    "-movflags",
                    "+faststart",
                    str(partial),
                ],
                check=True,
                capture_output=True,
                timeout=600,
            )
            partial.replace(out_feature)
            return out_feature
        finally:
            concat_list.unlink(missing_ok=True)
            partial.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Kaggle Dual-T4 Elastic Pipeline Orchestrator")
    parser.add_argument("--workspace", default="/kaggle/working/workspace")
    parser.add_argument("--chapters", required=True)
    parser.add_argument("--render-workers", type=int, default=2)
    parser.add_argument("--profile", default="B5")
    parser.add_argument("--encoder", default="auto")
    parser.add_argument("--no-compile", action="store_true", default=False)
    args = parser.parse_args()

    ch_list = [c.strip() for c in args.chapters.split(",") if c.strip()]
    orchestrator = DualT4Orchestrator(
        workspace_dir=Path(args.workspace),
        chapters=ch_list,
        render_workers=args.render_workers,
        compile_feature=not args.no_compile,
        profile=args.profile,
        encoder=args.encoder,
    )
    tel = orchestrator.run_pipeline()
    print(f"Dual-T4 Production Completed: {len(ch_list)} chapters, {tel.speedup_factor}x speedup.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
