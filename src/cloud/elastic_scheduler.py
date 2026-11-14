"""
src/cloud/elastic_scheduler.py
4-State Elastic Dual-T4 Scheduler, Bounded Streaming Queues,
B0-B5 Benchmark Architecture Profiles, and Startup Autotuner.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from src.cloud.cuda_worker_pool import compute_usable_vram_budget


class SchedulerState(str, Enum):
    """4-state elastic pipeline scheduler states."""

    TTS_HEAVY = "TTS_HEAVY"
    BALANCED = "BALANCED"
    RENDER_HEAVY = "RENDER_HEAVY"
    DRAIN = "DRAIN"


class BenchmarkProfileId(str, Enum):
    """Explicit Dual-T4 benchmark architecture profiles (B0..B5)."""

    B0 = "B0"  # 1-T4 sequential baseline
    B1 = "B1"  # Dedicated GPU0 TTS -> GPU1 Render
    B2 = "B2"  # 2-GPU TTS -> 2-GPU Render (phase barrier)
    B3 = "B3"  # Elastic work-stealing across both GPUs
    B4 = "B4"  # Multi-replica TTS
    B5 = "B5"  # Elastic + fast FFmpeg/NVENC + TTS/NVENC coexistence


@dataclass(frozen=True)
class ArchitectureProfile:
    """Explicit configuration for each benchmark profile B0..B5."""

    profile_id: BenchmarkProfileId
    name: str
    description: str
    gpu_ids: Tuple[int, ...]
    tts_replicas_per_gpu: int
    tts_batch_size: int
    nvenc_workers_per_gpu: int
    coexistence: bool
    elastic_work_stealing: bool
    streaming_overlap: bool
    phase_barrier: bool = False

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["profile_id"] = self.profile_id.value
        return d


ARCHITECTURE_PROFILES: Dict[BenchmarkProfileId, ArchitectureProfile] = {
    BenchmarkProfileId.B0: ArchitectureProfile(
        profile_id=BenchmarkProfileId.B0,
        name="1_t4_sequential",
        description="Single GPU (GPU0 only) sequential TTS -> Render baseline",
        gpu_ids=(0,),
        tts_replicas_per_gpu=1,
        tts_batch_size=8,
        nvenc_workers_per_gpu=1,
        coexistence=False,
        elastic_work_stealing=False,
        streaming_overlap=False,
        phase_barrier=True,
    ),
    BenchmarkProfileId.B1: ArchitectureProfile(
        profile_id=BenchmarkProfileId.B1,
        name="dedicated_gpu0_tts_gpu1_render",
        description="Dedicated GPU0 for TTS and GPU1 for NVENC Render with chapter streaming",
        gpu_ids=(0, 1),
        tts_replicas_per_gpu=1,
        tts_batch_size=8,
        nvenc_workers_per_gpu=2,
        coexistence=False,
        elastic_work_stealing=False,
        streaming_overlap=True,
        phase_barrier=False,
    ),
    BenchmarkProfileId.B2: ArchitectureProfile(
        profile_id=BenchmarkProfileId.B2,
        name="two_gpu_tts_then_two_gpu_render",
        description="Phase-barrier 2-GPU TTS followed by TTS unload and 2-GPU Render",
        gpu_ids=(0, 1),
        tts_replicas_per_gpu=1,
        tts_batch_size=8,
        nvenc_workers_per_gpu=2,
        coexistence=False,
        elastic_work_stealing=True,
        streaming_overlap=False,
        phase_barrier=True,
    ),
    BenchmarkProfileId.B3: ArchitectureProfile(
        profile_id=BenchmarkProfileId.B3,
        name="elastic_work_stealing",
        description="4-state elastic scheduler with chapter streaming and dual-GPU work-stealing",
        gpu_ids=(0, 1),
        tts_replicas_per_gpu=1,
        tts_batch_size=8,
        nvenc_workers_per_gpu=2,
        coexistence=False,
        elastic_work_stealing=True,
        streaming_overlap=True,
        phase_barrier=False,
    ),
    BenchmarkProfileId.B4: ArchitectureProfile(
        profile_id=BenchmarkProfileId.B4,
        name="multi_replica_tts",
        description="Multi-replica TTS across GPUs with elastic render drain",
        gpu_ids=(0, 1),
        tts_replicas_per_gpu=2,
        tts_batch_size=16,
        nvenc_workers_per_gpu=2,
        coexistence=False,
        elastic_work_stealing=True,
        streaming_overlap=True,
        phase_barrier=False,
    ),
    BenchmarkProfileId.B5: ArchitectureProfile(
        profile_id=BenchmarkProfileId.B5,
        name="elastic_fast_nvenc_coexistence",
        description="Elastic 4-state work-stealing + fast FFmpeg/NVENC + bounded TTS/NVENC coexistence",
        gpu_ids=(0, 1),
        tts_replicas_per_gpu=1,
        tts_batch_size=8,
        nvenc_workers_per_gpu=2,
        coexistence=True,
        elastic_work_stealing=True,
        streaming_overlap=True,
        phase_barrier=False,
    ),
}


def resolve_architecture_profile(profile: Union[str, BenchmarkProfileId, ArchitectureProfile]) -> ArchitectureProfile:
    if isinstance(profile, ArchitectureProfile):
        return profile
    if isinstance(profile, BenchmarkProfileId):
        return ARCHITECTURE_PROFILES[profile]
    key = str(profile).strip().upper()
    return ARCHITECTURE_PROFILES[BenchmarkProfileId(key)]


class ElasticPipelineQueues:
    """
    Bounded CPU/GPU pipeline queues enabling streaming chapter overlap and backpressure:
      source_prefetch_queue -> tts_queue -> audio_ready_queue ->
      chapter_ready_queue -> render_queue -> validation_queue
    """

    def __init__(
        self,
        *,
        source_prefetch_maxsize: int = 4,
        tts_maxsize: int = 16,
        audio_ready_maxsize: int = 4,
        chapter_ready_maxsize: int = 4,
        render_maxsize: int = 128,
        validation_maxsize: int = 4,
    ):
        self.source_prefetch_queue: queue.Queue = queue.Queue(maxsize=max(1, source_prefetch_maxsize))
        self.tts_queue: queue.Queue = queue.Queue(maxsize=max(1, tts_maxsize))
        self.audio_ready_queue: queue.Queue = queue.Queue(maxsize=max(1, audio_ready_maxsize))
        self.chapter_ready_queue: queue.Queue = queue.Queue(maxsize=max(1, chapter_ready_maxsize))
        self.render_queue: queue.Queue = queue.Queue(maxsize=max(1, render_maxsize))
        self.validation_queue: queue.Queue = queue.Queue(maxsize=max(1, validation_maxsize))

    def depths(self) -> Dict[str, int]:
        return {
            "source_prefetch_queue": self.source_prefetch_queue.qsize(),
            "tts_queue": self.tts_queue.qsize(),
            "audio_ready_queue": self.audio_ready_queue.qsize(),
            "chapter_ready_queue": self.chapter_ready_queue.qsize(),
            "render_queue": self.render_queue.qsize(),
            "validation_queue": self.validation_queue.qsize(),
        }

    def maxsizes(self) -> Dict[str, int]:
        return {
            "source_prefetch_queue": self.source_prefetch_queue.maxsize,
            "tts_queue": self.tts_queue.maxsize,
            "audio_ready_queue": self.audio_ready_queue.maxsize,
            "chapter_ready_queue": self.chapter_ready_queue.maxsize,
            "render_queue": self.render_queue.maxsize,
            "validation_queue": self.validation_queue.maxsize,
        }


@dataclass(frozen=True)
class QueueSnapshot:
    """Point-in-time queue & GPU state snapshot consumed by ElasticScheduler."""

    source_prefetch_remaining: int = 0
    pending_tts_tasks: int = 0
    in_flight_tts: int = 0
    audio_ready_chapters: int = 0
    chapter_ready_chapters: int = 0
    pending_render_shots: int = 0
    in_flight_render_shots: int = 0
    pending_validations: int = 0
    all_tts_submitted: bool = False
    vram_used_mb: Dict[int, float] = field(default_factory=dict)
    vram_budget_mb: Dict[int, float] = field(default_factory=dict)


@dataclass(frozen=True)
class SchedulerDecision:
    """Actionable GPU assignment decision emitted by ElasticScheduler."""

    state: SchedulerState
    tts_gpu_ids: Tuple[int, ...]
    render_gpu_ids: Tuple[int, ...]
    unload_tts_on_gpus: Tuple[int, ...]
    reason: str


class ElasticScheduler:
    """
    4-state elastic scheduler (`TTS_HEAVY`, `BALANCED`, `RENDER_HEAVY`, `DRAIN`)
    governing dynamic GPU allocation across TTS and NVENC shot rendering.
    """

    def __init__(
        self,
        profile: Union[str, BenchmarkProfileId, ArchitectureProfile] = BenchmarkProfileId.B3,
        *,
        drain_shot_threshold: int = 4,
        render_heavy_shot_threshold: int = 4,
    ):
        self.profile = resolve_architecture_profile(profile)
        self.drain_shot_threshold = max(1, int(drain_shot_threshold))
        self.render_heavy_shot_threshold = max(1, int(render_heavy_shot_threshold))
        self._lock = threading.Lock()
        self._current_state: SchedulerState = SchedulerState.TTS_HEAVY
        self._history: List[Dict[str, Any]] = []
        self._tts_unloaded_gpus: set[int] = set()

    @property
    def current_state(self) -> SchedulerState:
        with self._lock:
            return self._current_state

    @property
    def history(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._history)

    def _within_coexistence_vram(self, gpu_id: int, snapshot: QueueSnapshot) -> bool:
        used = snapshot.vram_used_mb.get(gpu_id)
        budget = snapshot.vram_budget_mb.get(gpu_id)
        if used is None or budget is None or budget <= 0.0:
            return True
        # Reserve ~1536 MB headroom for NVENC contexts when coexisting with TTS.
        return used <= max(0.0, budget - 768.0)

    def evaluate_state(self, snapshot: QueueSnapshot) -> SchedulerDecision:
        """Evaluates queue depths and returns the deterministic 4-state decision."""
        gpu_ids = self.profile.gpu_ids
        tts_active = snapshot.pending_tts_tasks + snapshot.in_flight_tts
        render_active = snapshot.pending_render_shots + snapshot.in_flight_render_shots
        upstream_active = (
            snapshot.source_prefetch_remaining
            + tts_active
            + snapshot.audio_ready_chapters
            + snapshot.chapter_ready_chapters
        )
        tts_completely_done = (
            snapshot.all_tts_submitted
            and snapshot.source_prefetch_remaining == 0
            and tts_active == 0
        )

        # 1. Determine state transition
        if (
            tts_completely_done
            and snapshot.audio_ready_chapters == 0
            and snapshot.chapter_ready_chapters == 0
            and (
                render_active <= self.drain_shot_threshold
                or snapshot.pending_validations > 0
                and snapshot.pending_render_shots == 0
            )
        ):
            state = SchedulerState.DRAIN
            reason = "All TTS/upstream chapters complete; draining tail render/validation work"
        elif (
            tts_active == 0
            and (render_active > 0 or snapshot.audio_ready_chapters > 0 or snapshot.chapter_ready_chapters > 0)
        ) or (
            snapshot.pending_render_shots >= self.render_heavy_shot_threshold
            and snapshot.pending_tts_tasks == 0
            and snapshot.in_flight_tts == 0
        ):
            state = SchedulerState.RENDER_HEAVY
            reason = "TTS queue empty while render shots/chapters wait for GPU encoding"
        elif tts_active > 0 and render_active == 0 and snapshot.chapter_ready_chapters == 0:
            state = SchedulerState.TTS_HEAVY
            reason = "TTS backlog active with empty render queue"
        elif tts_active > 0 and (render_active > 0 or snapshot.chapter_ready_chapters > 0):
            state = SchedulerState.BALANCED
            reason = "Both TTS and Render pipelines concurrently active"
        elif upstream_active == 0 and render_active == 0:
            state = SchedulerState.DRAIN
            reason = "Pipeline idle/draining"
        else:
            state = SchedulerState.BALANCED
            reason = "Steady mixed pipeline load"

        # 2. Compute GPU allocation per profile + state
        if len(gpu_ids) == 1:
            g0 = gpu_ids[0]
            if state == SchedulerState.TTS_HEAVY:
                tts_gpus: Tuple[int, ...] = (g0,)
                render_gpus: Tuple[int, ...] = (g0,) if self.profile.coexistence else ()
            elif state in (SchedulerState.RENDER_HEAVY, SchedulerState.DRAIN):
                tts_gpus = () if tts_completely_done else (g0,)
                render_gpus = (g0,)
            else:
                tts_gpus = (g0,)
                render_gpus = (g0,)
        else:
            g0, g1 = gpu_ids[0], gpu_ids[1]
            if not self.profile.elastic_work_stealing:
                # B1: Dedicated GPU0 TTS -> GPU1 Render
                tts_gpus = () if tts_completely_done else (g0,)
                render_gpus = (g1,)
            elif state == SchedulerState.TTS_HEAVY:
                # Both GPUs can synthesize TTS when render queue is empty (if profile allows),
                # unless B5 coexistence keeps GPU1 ready or TTS is single-chapter small.
                tts_gpus = (g0, g1) if self.profile.profile_id in (
                    BenchmarkProfileId.B2,
                    BenchmarkProfileId.B3,
                    BenchmarkProfileId.B4,
                    BenchmarkProfileId.B5,
                ) else (g0,)
                render_gpus = (g1,) if self.profile.coexistence else ()
            elif state == SchedulerState.BALANCED:
                tts_gpus = (g0,)
                if self.profile.coexistence and self._within_coexistence_vram(g0, snapshot):
                    render_gpus = (g1, g0)
                else:
                    render_gpus = (g1,)
            else:
                # RENDER_HEAVY or DRAIN: unload TTS if drained and steal render shots across BOTH GPUs.
                tts_gpus = () if tts_completely_done else (g0,)
                render_gpus = (g0, g1)

        unload_gpus: List[int] = []
        if tts_completely_done:
            with self._lock:
                for gid in gpu_ids:
                    if gid not in self._tts_unloaded_gpus:
                        unload_gpus.append(gid)
                        self._tts_unloaded_gpus.add(gid)

        decision = SchedulerDecision(
            state=state,
            tts_gpu_ids=tts_gpus,
            render_gpu_ids=render_gpus,
            unload_tts_on_gpus=tuple(unload_gpus),
            reason=reason,
        )

        with self._lock:
            if state != self._current_state or not self._history:
                self._history.append(
                    {
                        "from_state": self._current_state.value if self._history else None,
                        "to_state": state.value,
                        "tts_gpu_ids": list(tts_gpus),
                        "render_gpu_ids": list(render_gpus),
                        "unload_tts_on_gpus": list(unload_gpus),
                        "reason": reason,
                        "pending_tts": snapshot.pending_tts_tasks,
                        "pending_render": snapshot.pending_render_shots,
                    }
                )
            self._current_state = state

        return decision


# ==============================================================================
# Startup Autotuner & B0..B5 Empirical Benchmark Runner
# ==============================================================================


@dataclass(frozen=True)
class AutotuneCandidate:
    tts_replicas_per_gpu: int
    tts_batch_size: int
    nvenc_workers_per_gpu: int
    coexistence: bool


@dataclass(frozen=True)
class AutotuneSample:
    throughput: float
    latency_p95_sec: float
    peak_vram_mb: float
    error_count: int = 0


@dataclass(frozen=True)
class AutotuneTrial:
    candidate: AutotuneCandidate
    sample: AutotuneSample
    accepted: bool
    relative_gain: float
    stop_reason: Optional[str] = None


@dataclass(frozen=True)
class FrozenSessionProfile:
    """Immutable session profile locked after StartupAutotuner converges."""

    tts_replicas_per_gpu: int
    tts_batch_size: int
    nvenc_workers_per_gpu: int
    coexistence: bool
    vram_budget_mb: float
    best_throughput: float
    best_latency_p95_sec: float
    best_peak_vram_mb: float
    trials: Tuple[AutotuneTrial, ...]
    stop_reasons: Tuple[str, ...]
    frozen: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tts_replicas_per_gpu": self.tts_replicas_per_gpu,
            "tts_batch_size": self.tts_batch_size,
            "nvenc_workers_per_gpu": self.nvenc_workers_per_gpu,
            "coexistence": self.coexistence,
            "vram_budget_mb": self.vram_budget_mb,
            "best_throughput": self.best_throughput,
            "best_latency_p95_sec": self.best_latency_p95_sec,
            "best_peak_vram_mb": self.best_peak_vram_mb,
            "stop_reasons": list(self.stop_reasons),
            "frozen": self.frozen,
            "trial_count": len(self.trials),
        }


class StartupAutotuner:
    """
    Explores TTS replicas/GPU (`1..3`), TTS batch size (`4/8/16/32`),
    NVENC workers/GPU (`1..4`), and TTS/NVENC coexistence at startup.
    Stops scaling an axis immediately when:
      - `peak_vram_mb > usable_vram_budget_mb`
      - `error_count > 0`
      - `latency_p95_sec` degrades beyond `latency_degrade_factor`
      - relative throughput gain `< min_gain_ratio` (default 5%)
    Freezes the winning `FrozenSessionProfile` for the rest of the session.
    """

    TTS_REPLICAS_RANGE: Tuple[int, ...] = (1, 2, 3)
    TTS_BATCH_SIZES: Tuple[int, ...] = (4, 8, 16, 32)
    NVENC_WORKERS_RANGE: Tuple[int, ...] = (1, 2, 3, 4)
    COEXISTENCE_OPTIONS: Tuple[bool, ...] = (False, True)

    def __init__(
        self,
        *,
        total_vram_mb: float = 15360.0,
        usable_vram_budget_mb: Optional[float] = None,
        min_gain_ratio: float = 0.05,
        latency_degrade_factor: float = 1.25,
    ):
        self.total_vram_mb = float(total_vram_mb)
        self.usable_vram_budget_mb = (
            float(usable_vram_budget_mb)
            if usable_vram_budget_mb is not None
            else compute_usable_vram_budget(self.total_vram_mb)
        )
        self.min_gain_ratio = float(min_gain_ratio)
        self.latency_degrade_factor = float(latency_degrade_factor)

    def _evaluate_candidate(
        self,
        candidate: AutotuneCandidate,
        probe_fn: Callable[[AutotuneCandidate], Union[AutotuneSample, Dict[str, Any]]],
        best_sample: Optional[AutotuneSample],
    ) -> Tuple[AutotuneSample, bool, float, Optional[str]]:
        raw = probe_fn(candidate)
        if isinstance(raw, AutotuneSample):
            sample = raw
        else:
            sample = AutotuneSample(
                throughput=float(raw.get("throughput", 0.0)),
                latency_p95_sec=float(raw.get("latency_p95_sec", 0.0)),
                peak_vram_mb=float(raw.get("peak_vram_mb", 0.0)),
                error_count=int(raw.get("error_count", 0)),
            )

        if sample.error_count > 0:
            return sample, False, 0.0, "errors_detected"
        if sample.peak_vram_mb > self.usable_vram_budget_mb:
            return sample, False, 0.0, "vram_exceeds_budget"
        if sample.throughput <= 0.0:
            return sample, False, 0.0, "zero_throughput"

        if best_sample is None:
            return sample, True, 1.0, None

        gain = (sample.throughput - best_sample.throughput) / max(1e-9, best_sample.throughput)
        if (
            best_sample.latency_p95_sec > 0.0
            and sample.latency_p95_sec > best_sample.latency_p95_sec * self.latency_degrade_factor
        ):
            return sample, False, gain, "latency_degraded"
        if gain < self.min_gain_ratio:
            return sample, False, gain, "gain_below_5pct"
        return sample, True, gain, None

    def autotune(
        self,
        probe_fn: Callable[[AutotuneCandidate], Union[AutotuneSample, Dict[str, Any]]],
    ) -> FrozenSessionProfile:
        trials: List[AutotuneTrial] = []
        stop_reasons: List[str] = []

        # Baseline anchor: (replicas=1, batch=4, nvenc=1, coexistence=False)
        best_cand = AutotuneCandidate(
            tts_replicas_per_gpu=self.TTS_REPLICAS_RANGE[0],
            tts_batch_size=self.TTS_BATCH_SIZES[0],
            nvenc_workers_per_gpu=self.NVENC_WORKERS_RANGE[0],
            coexistence=False,
        )
        best_sample, ok, gain, reason = self._evaluate_candidate(best_cand, probe_fn, None)
        trials.append(AutotuneTrial(best_cand, best_sample, ok, gain, reason))
        if not ok:
            if reason:
                stop_reasons.append(f"baseline:{reason}")
            return FrozenSessionProfile(
                tts_replicas_per_gpu=best_cand.tts_replicas_per_gpu,
                tts_batch_size=best_cand.tts_batch_size,
                nvenc_workers_per_gpu=best_cand.nvenc_workers_per_gpu,
                coexistence=best_cand.coexistence,
                vram_budget_mb=self.usable_vram_budget_mb,
                best_throughput=best_sample.throughput,
                best_latency_p95_sec=best_sample.latency_p95_sec,
                best_peak_vram_mb=best_sample.peak_vram_mb,
                trials=tuple(trials),
                stop_reasons=tuple(stop_reasons),
                frozen=True,
            )

        # Axis 1: TTS batch size (4 -> 8 -> 16 -> 32)
        for bs in self.TTS_BATCH_SIZES[1:]:
            cand = AutotuneCandidate(
                tts_replicas_per_gpu=best_cand.tts_replicas_per_gpu,
                tts_batch_size=bs,
                nvenc_workers_per_gpu=best_cand.nvenc_workers_per_gpu,
                coexistence=best_cand.coexistence,
            )
            sample, accepted, rel_gain, stop_reason = self._evaluate_candidate(cand, probe_fn, best_sample)
            trials.append(AutotuneTrial(cand, sample, accepted, rel_gain, stop_reason))
            if accepted:
                best_cand, best_sample = cand, sample
            else:
                if stop_reason:
                    stop_reasons.append(f"batch_size={bs}:{stop_reason}")
                break

        # Axis 2: TTS replicas per GPU (1 -> 2 -> 3)
        for rep in self.TTS_REPLICAS_RANGE[1:]:
            cand = AutotuneCandidate(
                tts_replicas_per_gpu=rep,
                tts_batch_size=best_cand.tts_batch_size,
                nvenc_workers_per_gpu=best_cand.nvenc_workers_per_gpu,
                coexistence=best_cand.coexistence,
            )
            sample, accepted, rel_gain, stop_reason = self._evaluate_candidate(cand, probe_fn, best_sample)
            trials.append(AutotuneTrial(cand, sample, accepted, rel_gain, stop_reason))
            if accepted:
                best_cand, best_sample = cand, sample
            else:
                if stop_reason:
                    stop_reasons.append(f"tts_replicas={rep}:{stop_reason}")
                break

        # Axis 3: NVENC workers per GPU (1 -> 2 -> 3 -> 4)
        for nw in self.NVENC_WORKERS_RANGE[1:]:
            cand = AutotuneCandidate(
                tts_replicas_per_gpu=best_cand.tts_replicas_per_gpu,
                tts_batch_size=best_cand.tts_batch_size,
                nvenc_workers_per_gpu=nw,
                coexistence=best_cand.coexistence,
            )
            sample, accepted, rel_gain, stop_reason = self._evaluate_candidate(cand, probe_fn, best_sample)
            trials.append(AutotuneTrial(cand, sample, accepted, rel_gain, stop_reason))
            if accepted:
                best_cand, best_sample = cand, sample
            else:
                if stop_reason:
                    stop_reasons.append(f"nvenc_workers={nw}:{stop_reason}")
                break

        # Axis 4: TTS/NVENC coexistence on same GPU
        coexist_cand = AutotuneCandidate(
            tts_replicas_per_gpu=best_cand.tts_replicas_per_gpu,
            tts_batch_size=best_cand.tts_batch_size,
            nvenc_workers_per_gpu=best_cand.nvenc_workers_per_gpu,
            coexistence=True,
        )
        sample, accepted, rel_gain, stop_reason = self._evaluate_candidate(coexist_cand, probe_fn, best_sample)
        trials.append(AutotuneTrial(coexist_cand, sample, accepted, rel_gain, stop_reason))
        if accepted:
            best_cand, best_sample = coexist_cand, sample
        elif stop_reason:
            stop_reasons.append(f"coexistence=True:{stop_reason}")

        return FrozenSessionProfile(
            tts_replicas_per_gpu=best_cand.tts_replicas_per_gpu,
            tts_batch_size=best_cand.tts_batch_size,
            nvenc_workers_per_gpu=best_cand.nvenc_workers_per_gpu,
            coexistence=best_cand.coexistence,
            vram_budget_mb=self.usable_vram_budget_mb,
            best_throughput=best_sample.throughput,
            best_latency_p95_sec=best_sample.latency_p95_sec,
            best_peak_vram_mb=best_sample.peak_vram_mb,
            trials=tuple(trials),
            stop_reasons=tuple(stop_reasons),
            frozen=True,
        )


def benchmark_architecture_profiles(
    runner_fn: Callable[[ArchitectureProfile], Dict[str, Any]],
    profiles: Optional[Sequence[Union[str, BenchmarkProfileId, ArchitectureProfile]]] = None,
) -> Dict[str, Any]:
    """
    Empirically benchmarks architecture profiles B0..B5 without hardcoding an assumed winner.
    Each `runner_fn(profile)` returns metrics including `wall_clock_seconds`, `peak_vram_mb`,
    and optional `errors`.
    """
    selected = [
        resolve_architecture_profile(p)
        for p in (profiles if profiles is not None else tuple(BenchmarkProfileId))
    ]
    results: List[Dict[str, Any]] = []
    for prof in selected:
        metrics = dict(runner_fn(prof) or {})
        wall = float(metrics.get("wall_clock_seconds", float("inf")))
        errors = int(metrics.get("errors", 0))
        results.append(
            {
                "profile_id": prof.profile_id.value,
                "name": prof.name,
                "profile": prof.to_dict(),
                "wall_clock_seconds": wall,
                "peak_vram_mb": float(metrics.get("peak_vram_mb", 0.0)),
                "errors": errors,
                "metrics": metrics,
            }
        )
    valid = [r for r in results if r["errors"] == 0 and r["wall_clock_seconds"] > 0]
    winner = min(valid, key=lambda r: r["wall_clock_seconds"]) if valid else None
    return {
        "profiles_tested": [r["profile_id"] for r in results],
        "results": results,
        "winner_profile_id": winner["profile_id"] if winner else None,
    }
