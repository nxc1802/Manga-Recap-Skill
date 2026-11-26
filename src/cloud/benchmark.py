"""Offline comparative benchmark harness for Dual-T4 execution policies A-D (mocked GPUs, overlap-aware telemetry)."""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from src.cloud.scheduler import ExecutionPolicy
from src.cloud.telemetry import TelemetryRecorder


COMPARATIVE_POLICIES: Tuple[str, ...] = (
    ExecutionPolicy.BASELINE_SPLIT,
    ExecutionPolicy.DUAL_TTS_SHARED_ENCODE,
    ExecutionPolicy.DUAL_TTS_THEN_DUAL_ENCODE,
    ExecutionPolicy.ONE_TTS_DUAL_ENCODE,
)


@dataclass(frozen=True)
class WorkloadProfile:
    """Synthetic dual-T4 comparative benchmark workload specification."""

    name: str
    chapters: int
    segments_per_chapter: int
    shots_per_segment: int
    tts_sec_per_segment: float
    encode_sec_per_shot: float
    unload_sec: float = 0.015
    finalize_sec_per_chapter: float = 0.02
    shared_encode_contention: float = 1.15  # slowdown when CUDA TTS & NVENC share same GPU simultaneously

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "chapters": self.chapters,
            "segments_per_chapter": self.segments_per_chapter,
            "shots_per_segment": self.shots_per_segment,
            "tts_sec_per_segment": self.tts_sec_per_segment,
            "encode_sec_per_shot": self.encode_sec_per_shot,
            "unload_sec": self.unload_sec,
            "finalize_sec_per_chapter": self.finalize_sec_per_chapter,
            "shared_encode_contention": self.shared_encode_contention,
        }


DEFAULT_WORKLOAD_PROFILES: Dict[str, WorkloadProfile] = {
    "short_mixed": WorkloadProfile(
        name="short_mixed",
        chapters=3,
        segments_per_chapter=4,
        shots_per_segment=1,
        tts_sec_per_segment=0.025,
        encode_sec_per_shot=0.025,
    ),
    "tts_heavy": WorkloadProfile(
        name="tts_heavy",
        chapters=3,
        segments_per_chapter=6,
        shots_per_segment=1,
        tts_sec_per_segment=0.04,
        encode_sec_per_shot=0.015,
    ),
    "render_heavy": WorkloadProfile(
        name="render_heavy",
        chapters=3,
        segments_per_chapter=3,
        shots_per_segment=3,
        tts_sec_per_segment=0.015,
        encode_sec_per_shot=0.035,
    ),
}


def simulate_policy_timeline(
    profile: WorkloadProfile,
    policy: str,
    *,
    num_gpus: int = 2,
) -> Dict[str, Any]:
    """
    Deterministic event-driven discrete simulator producing overlap-aware spans
    matching Dual-T4 Policies A-D execution contracts.
    """
    n_gpu = max(1, min(2, int(num_gpus)))
    rec = TelemetryRecorder(job_id=f"bench-{profile.name}-{policy}", project_slug=profile.name, policy=policy)
    rec.started_mono = 0.0
    ch_names = [str(i + 1) for i in range(profile.chapters)]
    rec.chapters = ch_names

    tts_units = [
        (c_idx, ch_names[c_idx], s_idx)
        for c_idx in range(profile.chapters)
        for s_idx in range(profile.segments_per_chapter)
    ]

    # Track virtual clock for GPU0, GPU1, CPU finalizer, and unit completion times.
    gpu_t = [0.0] * n_gpu
    cpu_finalize_t = 0.0
    shot_ready_times: List[Tuple[str, float]] = []

    if policy == ExecutionPolicy.BASELINE_SPLIT or n_gpu == 1:
        # Policy A: GPU0 processes TTS chapter-by-chapter -> unloads -> all GPUs encode ready shots
        for ch_idx in range(profile.chapters):
            ch_id = ch_names[ch_idx]
            for s_idx in range(profile.segments_per_chapter):
                t0 = gpu_t[0]
                t1 = t0 + profile.tts_sec_per_segment
                gpu_t[0] = t1
                rec.record_stage("tts", t0, t1, chapter_id=ch_id, device_id="gpu:0", metadata={"segments": 1})
                for _ in range(profile.shots_per_segment):
                    shot_ready_times.append((ch_id, t1))
        gpu_t[0] += profile.unload_sec
        # Secondary GPU could start earlier on baseline stream once shots arrive
        enc_workers = [gpu_t[0]] + ([0.0] if n_gpu == 2 else [])
        ch_last_shot: Dict[str, float] = {}
        for ch_id, ready_at in shot_ready_times:
            # Prefer secondary GPU while GPU0 is still running TTS
            best_dev = min(range(n_gpu), key=lambda d: max(enc_workers[d], ready_at))
            t0 = max(enc_workers[best_dev], ready_at)
            t1 = t0 + profile.encode_sec_per_shot
            enc_workers[best_dev] = t1
            rec.record_stage("encode", t0, t1, chapter_id=ch_id, device_id=f"gpu:{best_dev}")
            ch_last_shot[ch_id] = max(ch_last_shot.get(ch_id, 0.0), t1)
        for ch_id in ch_names:
            t0 = max(cpu_finalize_t, ch_last_shot.get(ch_id, 0.0))
            t1 = t0 + profile.finalize_sec_per_chapter
            cpu_finalize_t = t1
            rec.record_stage("finalize", t0, t1, chapter_id=ch_id, device_id="cpu")

    elif policy == ExecutionPolicy.DUAL_TTS_THEN_DUAL_ENCODE:
        # Policy C: Both GPUs run cost-aware TTS -> explicit unload barrier -> both GPUs encode cleanly
        ch_last_shot = {}
        for _, ch_id, _ in tts_units:
            d = 0 if gpu_t[0] <= gpu_t[1] else 1
            t0 = gpu_t[d]
            t1 = t0 + profile.tts_sec_per_segment
            gpu_t[d] = t1
            rec.record_stage("tts", t0, t1, chapter_id=ch_id, device_id=f"gpu:{d}", metadata={"segments": 1})
            for _ in range(profile.shots_per_segment):
                shot_ready_times.append((ch_id, t1))
        barrier_t = max(gpu_t) + profile.unload_sec
        enc_t = [barrier_t, barrier_t]
        for ch_id, ready_at in shot_ready_times:
            d = 0 if enc_t[0] <= enc_t[1] else 1
            t0 = max(enc_t[d], ready_at)
            t1 = t0 + profile.encode_sec_per_shot
            enc_t[d] = t1
            rec.record_stage("encode", t0, t1, chapter_id=ch_id, device_id=f"gpu:{d}")
            ch_last_shot[ch_id] = max(ch_last_shot.get(ch_id, 0.0), t1)
        for ch_id in ch_names:
            t0 = max(cpu_finalize_t, ch_last_shot.get(ch_id, 0.0))
            t1 = t0 + profile.finalize_sec_per_chapter
            cpu_finalize_t = t1
            rec.record_stage("finalize", t0, t1, chapter_id=ch_id, device_id="cpu")

    elif policy == ExecutionPolicy.DUAL_TTS_SHARED_ENCODE:
        # Policy B: Both GPUs run TTS + concurrent NVENC slots (slight contention factor) -> unload -> finish tail
        tts_end = [0.0, 0.0]
        for _, ch_id, _ in tts_units:
            d = 0 if tts_end[0] <= tts_end[1] else 1
            t0 = tts_end[d]
            t1 = t0 + profile.tts_sec_per_segment
            tts_end[d] = t1
            rec.record_stage("tts", t0, t1, chapter_id=ch_id, device_id=f"gpu:{d}", metadata={"segments": 1})
            for _ in range(profile.shots_per_segment):
                shot_ready_times.append((ch_id, t1))
        enc_t = [0.0, 0.0]
        ch_last_shot = {}
        for ch_id, ready_at in shot_ready_times:
            d = 0 if enc_t[0] <= enc_t[1] else 1
            t0 = max(enc_t[d], ready_at)
            in_tts = t0 < tts_end[d]
            dur = profile.encode_sec_per_shot * (profile.shared_encode_contention if in_tts else 1.0)
            t1 = t0 + dur
            enc_t[d] = t1
            rec.record_stage("encode", t0, t1, chapter_id=ch_id, device_id=f"gpu:{d}")
            ch_last_shot[ch_id] = max(ch_last_shot.get(ch_id, 0.0), t1)
        max_end = max(max(tts_end) + profile.unload_sec, max(enc_t))
        for ch_id in ch_names:
            t0 = max(cpu_finalize_t, ch_last_shot.get(ch_id, 0.0))
            t1 = t0 + profile.finalize_sec_per_chapter
            cpu_finalize_t = t1
            rec.record_stage("finalize", t0, t1, chapter_id=ch_id, device_id="cpu")
        if cpu_finalize_t < max_end:
            cpu_finalize_t = max_end

    elif policy == ExecutionPolicy.ONE_TTS_DUAL_ENCODE:
        # Policy D: GPU0 TTS only, GPU1 encodes immediately while GPU0 joins encode after unload
        for _, ch_id, _ in tts_units:
            t0 = gpu_t[0]
            t1 = t0 + profile.tts_sec_per_segment
            gpu_t[0] = t1
            rec.record_stage("tts", t0, t1, chapter_id=ch_id, device_id="gpu:0", metadata={"segments": 1})
            for _ in range(profile.shots_per_segment):
                shot_ready_times.append((ch_id, t1))
        enc_t = [gpu_t[0] + profile.unload_sec, 0.0]
        ch_last_shot = {}
        for ch_id, ready_at in shot_ready_times:
            d = 0 if max(enc_t[0], ready_at) <= max(enc_t[1], ready_at) else 1
            t0 = max(enc_t[d], ready_at)
            t1 = t0 + profile.encode_sec_per_shot
            enc_t[d] = t1
            rec.record_stage("encode", t0, t1, chapter_id=ch_id, device_id=f"gpu:{d}")
            ch_last_shot[ch_id] = max(ch_last_shot.get(ch_id, 0.0), t1)
        for ch_id in ch_names:
            t0 = max(cpu_finalize_t, ch_last_shot.get(ch_id, 0.0))
            t1 = t0 + profile.finalize_sec_per_chapter
            cpu_finalize_t = t1
            rec.record_stage("finalize", t0, t1, chapter_id=ch_id, device_id="cpu")
    else:
        raise ValueError(f"Unknown policy for simulation: {policy}")

    # Normalize total simulated wall makespan across all recorded spans cleanly.
    total_makespan = max(cpu_finalize_t, max((s["end_offset_sec"] for s in rec._spans), default=0.0))
    summary = (rec.build_report().get("timing") or {}).get("stages") or {}
    tts_union = float((summary.get("tts") or {}).get("wall_union_sec", 0.0))
    enc_union = float((summary.get("encode") or {}).get("wall_union_sec", 0.0))
    total_segs = profile.chapters * profile.segments_per_chapter
    total_shots = total_segs * profile.shots_per_segment
    chapters_per_hr = (profile.chapters / total_makespan) * 3600.0 if total_makespan > 0 else 0.0
    return {
        "policy": policy,
        "makespan_sec": round(total_makespan, 6),
        "tts_union_sec": round(tts_union, 6),
        "encode_union_sec": round(enc_union, 6),
        "tts_segs_per_sec": round(total_segs / tts_union, 4) if tts_union > 0 else 0.0,
        "encode_shots_per_sec": round(total_shots / enc_union, 4) if enc_union > 0 else 0.0,
        "useful_chapters_per_session_hour": round(chapters_per_hr, 2),
    }


def benchmark_policies_simulated(
    profiles: Optional[Sequence[WorkloadProfile]] = None,
    *,
    policies: Sequence[str] = COMPARATIVE_POLICIES,
    repeats: int = 3,
    run_fn: Optional[Callable[[WorkloadProfile, str], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Runs comparative policy A-D evaluations across ``profiles`` (either synthetic event simulator
    or custom harness ``run_fn``) and reports medians, min/max spreads, and explicit offline provenance.
    """
    workloads = list(profiles or DEFAULT_WORKLOAD_PROFILES.values())
    n_repeats = max(1, int(repeats))
    evaluator = run_fn or (lambda p, pol: simulate_policy_timeline(p, pol, num_gpus=2))

    by_workload: Dict[str, Any] = {}
    for prof in workloads:
        policy_rows: Dict[str, Any] = {}
        for pol in policies:
            trials = [evaluator(prof, pol) for _ in range(n_repeats)]
            makespans = [float(t["makespan_sec"]) for t in trials]
            throughputs = [float(t["useful_chapters_per_session_hour"]) for t in trials]
            med = float(statistics.median(makespans))
            policy_rows[pol] = {
                "median_makespan_sec": round(med, 6),
                "min_makespan_sec": round(min(makespans), 6),
                "max_makespan_sec": round(max(makespans), 6),
                "spread_sec": round(max(makespans) - min(makespans), 6),
                "median_tts_union_sec": round(float(statistics.median(t["tts_union_sec"] for t in trials)), 6),
                "median_encode_union_sec": round(float(statistics.median(t["encode_union_sec"] for t in trials)), 6),
                "median_useful_chapters_per_session_hour": round(float(statistics.median(throughputs)), 2),
            }
        ranked = sorted(policy_rows.items(), key=lambda kv: kv[1]["median_makespan_sec"])
        baseline_med = policy_rows.get(ExecutionPolicy.BASELINE_SPLIT, {}).get("median_makespan_sec")
        best_policy, best_stats = ranked[0]
        speedup = round(baseline_med / best_stats["median_makespan_sec"], 3) if (baseline_med and best_stats["median_makespan_sec"] > 0) else 1.0
        by_workload[prof.name] = {
            "profile": prof.as_dict(),
            "results": policy_rows,
            "fastest_policy": best_policy,
            "speedup_vs_baseline_a": speedup,
            "ranking": [k for k, _ in ranked],
        }

    return {
        "schema_version": "1.0.0",
        "environment": "offline_mocked_dual_t4",
        "live_kaggle_verified": False,
        "recommended_safe_rollback_policy": ExecutionPolicy.BASELINE_SPLIT,
        "workloads": by_workload,
    }
