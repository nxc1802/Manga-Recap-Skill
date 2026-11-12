"""
tests/unit/cloud/test_p8_dual_t4_architecture.py
Deterministic unit & L2 architecture tests for P8 Dual-T4:
1. 4-state ElasticScheduler (`TTS_HEAVY`, `BALANCED`, `RENDER_HEAVY`, `DRAIN`)
2. Multiprocessing `spawn` + `CUDA_VISIBLE_DEVICES` isolation (real spawned children)
3. VRAM budget formula + `GpuTelemetrySampler` CSV/JSONL + OOM/crash recovery (zero placeholders)
4. 6 bounded streaming queues + `Ready Chapter Queue` overlap (real FFmpeg + `validate_final_artifact`)
5. B0-B5 profiles + `StartupAutotuner` stop criteria & freeze
6. G8 Dual-T4 series payload/bootstrap/evidence gate & `produce-series` fail-closed Ch.1 G6 gate
7. `SingleSessionProductionRunner` stale result keys fix
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import os
import shutil
import tarfile
import threading
import time
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import numpy as np
import pytest
from PIL import Image

from src.cli.main import main
from src.cloud.cuda_worker_pool import (
    CudaWorkerPool,
    GPU_TRACE_COLUMNS,
    GpuTelemetrySampler,
    WorkerExecutionError,
    compute_usable_vram_budget,
)
from src.cloud.dual_t4_orchestrator import (
    DualT4Orchestrator,
    MicroBatchPlanner,
)
from src.cloud.elastic_scheduler import (
    ARCHITECTURE_PROFILES,
    AutotuneSample,
    BenchmarkProfileId,
    ElasticPipelineQueues,
    ElasticScheduler,
    QueueSnapshot,
    SchedulerState,
    StartupAutotuner,
    benchmark_architecture_profiles,
)
from src.cloud.kaggle_service import KaggleCloudService
from src.cloud.series_payload import (
    G8_REQUIRED_RECEIPTS,
    package_series_payload,
    run_series_worker,
    sync_series_results,
    verify_g8_dual_t4_evidence,
    verify_series_payload_manifest,
)
from src.core.human_review import approve_preview, review_fingerprint
from src.core.schemas import (
    PanelItem,
    PanelManifest,
    RecapScript,
    ScriptSegment,
)
from src.core.series_setup import record_series_setup
from src.core.storage import (
    atomic_write_json,
    calculate_sha256,
    init_chapter_layout,
    init_project_layout,
    read_json,
)
from src.render.artifact_validation import fingerprint
from src.render.chapter_renderer import render_chapter
from unit.test_series_setup import _make_valid_answers_dict


def _write_signal_wav(path: Path, duration_sec: float = 0.5) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    rate = 48000
    n = max(2400, int(round(duration_sec * rate)))
    samples = (np.sin(np.arange(n) / 10.0) * 3000.0).astype("<i2")
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(samples.tobytes())
    return calculate_sha256(path)


def _create_multi_chapter_series(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    chapters: List[str] = ["1", "2", "3"],
    approve_ch1: bool = True,
) -> Path:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(data_dir))
    monkeypatch.setenv("MANGA_RECAP_OUTPUT_DIR", str(tmp_path / "exports"))
    slug = "p8_series"
    init_project_layout(slug, "P8 Series Fixture")
    proj_dir = data_dir / "projects" / slug

    answers = _make_valid_answers_dict()
    answers["Q01"]["user_answer"] = {"chapters": f"1-{chapters[-1]}", "source": "https://example.invalid/p8"}
    answers["Q02"]["user_answer"] = "kaggle"
    answers["Q05"]["user_answer"] = False
    answers["Q06"]["user_answer"] = "static"
    answers["Q07"]["user_answer"] = False
    answers["Q08"]["user_answer"] = {"no_upload": True}
    answers["Q09"]["user_answer"] = "preview"  # keeps fixture fast 720p for L2 ffmpeg tests
    answers["Q10"]["user_answer"] = {"none": True}
    record_series_setup(slug, answers, project_dir=proj_dir)
    atomic_write_json(
        proj_dir / "voice_selection.json",
        {
            "project_slug": slug,
            "primary_voice": "Ngọc Huyền",
            "reviewer": "L2 test",
            "status": "approved",
            "speed": 1.0,
        },
    )

    for ch in chapters:
        init_chapter_layout(slug, ch)
        c_dir = proj_dir / "chapters" / ch
        panel_path = c_dir / "panels" / f"p_{ch}_1.png"
        arr = np.tile(np.arange(200, dtype=np.uint8), (150, 1))
        Image.fromarray(arr).convert("RGB").save(panel_path)
        panel_item = PanelItem(
            panel_id=f"p_{ch}_1",
            sequence_number=1,
            filename=panel_path.name,
            relative_path=f"panels/{panel_path.name}",
            source_image=f"page_{ch}.png",
            width=200,
            height=150,
            aspect_ratio=200 / 150,
            crop_box=(0, 0, 200, 150),
            sha256=calculate_sha256(panel_path),
        )
        atomic_write_json(
            c_dir / "manifest.json",
            PanelManifest(project_slug=slug, chapter_number=ch, panels=[panel_item]),
        )
        if ch == "1":
            wav_p = c_dir / "audio" / "clips" / "audio_1_001.wav"
            sha = _write_signal_wav(wav_p, 0.5)
            segs = [
                ScriptSegment(
                    seq_id="001",
                    evidence_panel_ids=[f"p_{ch}_1"],
                    narrator_text=f"Chương {ch} đoạn mở đầu.",
                    duration=0.5,
                    audio_path="audio/clips/audio_1_001.wav",
                    checksum=sha,
                    status="synthesized",
                )
            ]
        else:
            segs = [
                ScriptSegment(
                    seq_id="001",
                    evidence_panel_ids=[f"p_{ch}_1"],
                    narrator_text=f"Chương {ch} đoạn một.",
                ),
                ScriptSegment(
                    seq_id="002",
                    evidence_panel_ids=[f"p_{ch}_1"],
                    narrator_text=f"Chương {ch} đoạn hai.",
                ),
            ]
        atomic_write_json(
            c_dir / "script.json",
            RecapScript(project_slug=slug, chapter_number=ch, segments=segs),
        )

    if approve_ch1:
        render_chapter(proj_dir, "1", quality="preview")
        approve_preview(
            proj_dir,
            "L2 human fixture",
            review_fingerprint(proj_dir),
            fingerprint(proj_dir / "chapters" / "1" / "video" / "preview.mp4"),
        )
    return proj_dir


# ==============================================================================
# 1. Elastic Scheduler 4-State Machine & Queues
# ==============================================================================


def test_elastic_scheduler_4_states_and_tts_unload_transition():
    queues = ElasticPipelineQueues(
        source_prefetch_maxsize=2,
        tts_maxsize=4,
        audio_ready_maxsize=2,
        chapter_ready_maxsize=2,
        render_maxsize=16,
        validation_maxsize=2,
    )
    assert queues.maxsizes()["source_prefetch_queue"] == 2
    assert set(queues.depths().keys()) == {
        "source_prefetch_queue",
        "tts_queue",
        "audio_ready_queue",
        "chapter_ready_queue",
        "render_queue",
        "validation_queue",
    }

    sched = ElasticScheduler(BenchmarkProfileId.B3, drain_shot_threshold=3, render_heavy_shot_threshold=4)

    # State 1: TTS_HEAVY (TTS active, Render empty) -> uses GPU0 + GPU1 for TTS
    d1 = sched.evaluate_state(QueueSnapshot(pending_tts_tasks=6, pending_render_shots=0))
    assert d1.state == SchedulerState.TTS_HEAVY
    assert d1.tts_gpu_ids == (0, 1)
    assert d1.unload_tts_on_gpus == ()

    # State 2: BALANCED (both TTS and Render active) -> GPU0 TTS, GPU1 Render
    d2 = sched.evaluate_state(QueueSnapshot(pending_tts_tasks=4, pending_render_shots=5))
    assert d2.state == SchedulerState.BALANCED
    assert d2.tts_gpu_ids == (0,)
    assert d2.render_gpu_ids == (1,)

    # State 3: RENDER_HEAVY (TTS done, heavy render backlog) -> unloads TTS & steals on BOTH GPUs
    d3 = sched.evaluate_state(
        QueueSnapshot(
            pending_tts_tasks=0,
            in_flight_tts=0,
            all_tts_submitted=True,
            pending_render_shots=10,
        )
    )
    assert d3.state == SchedulerState.RENDER_HEAVY
    assert set(d3.unload_tts_on_gpus) == {0, 1}
    assert d3.render_gpu_ids == (0, 1)
    assert d3.tts_gpu_ids == ()

    # State 4: DRAIN (tail shots <= threshold) -> both GPUs finish & TTS unload isn't duplicated
    d4 = sched.evaluate_state(
        QueueSnapshot(
            pending_tts_tasks=0,
            in_flight_tts=0,
            all_tts_submitted=True,
            pending_render_shots=2,
        )
    )
    assert d4.state == SchedulerState.DRAIN
    assert d4.render_gpu_ids == (0, 1)
    assert d4.unload_tts_on_gpus == ()
    assert [h["to_state"] for h in sched.history] == [
        "TTS_HEAVY",
        "BALANCED",
        "RENDER_HEAVY",
        "DRAIN",
    ]


# ==============================================================================
# 2. Real Multiprocessing `spawn` + `CUDA_VISIBLE_DEVICES` Isolation
# ==============================================================================


def test_real_spawn_cuda_visible_devices_isolation():
    """
    Launches genuine `multiprocessing.get_context('spawn')` child processes for
    physical GPU 0 and GPU 1 and verifies child-level `CUDA_VISIBLE_DEVICES`
    isolation without touching parent process environment variables.
    """
    parent_cvd_before = os.environ.get("CUDA_VISIBLE_DEVICES")
    pool = CudaWorkerPool(gpu_ids=(0, 1), tts_replicas_per_gpu=1, nvenc_workers_per_gpu=1)
    assert pool.mp_ctx.get_start_method() == "spawn"
    try:
        info0 = pool.probe_gpu(0)
        info1 = pool.probe_gpu(1)
        assert info0["env_cuda_visible_devices"] == "0"
        assert info1["env_cuda_visible_devices"] == "1"
        assert info0["pid"] != os.getpid()
        assert info1["pid"] != os.getpid()
        assert info0["pid"] != info1["pid"]
        assert os.environ.get("CUDA_VISIBLE_DEVICES") == parent_cvd_before
    finally:
        pool.shutdown()


# ==============================================================================
# 3. VRAM Budget + GpuTelemetrySampler + OOM & Crash Recovery
# ==============================================================================


def test_vram_budget_and_gpu_telemetry_sampler(tmp_path):
    assert compute_usable_vram_budget(15360.0) == 13824.0
    assert compute_usable_vram_budget(16384.0) == 14745.6

    fake_rows = [
        {
            "gpu_id": 0,
            "gpu_uuid": "GPU-0000",
            "name": "Tesla T4",
            "vram_total_mb": 15360.0,
            "vram_used_mb": 14000.0,  # exceeds 13824.0 budget
            "vram_free_mb": 1360.0,
            "gpu_util_pct": 85.0,
            "encoder_util_pct": 10.0,
            "temperature_c": 65.0,
        },
        {
            "gpu_id": 1,
            "gpu_uuid": "GPU-1111",
            "name": "Tesla T4",
            "vram_total_mb": 15360.0,
            "vram_used_mb": 2200.0,
            "vram_free_mb": 13160.0,
            "gpu_util_pct": 72.0,
            "encoder_util_pct": 64.0,
            "temperature_c": 61.0,
        },
    ]
    trace_csv = tmp_path / "dual_t4_gpu_trace.csv"
    log_jsonl = tmp_path / "dual_t4_worker_log.jsonl"
    sampler = GpuTelemetrySampler(
        trace_csv,
        log_jsonl,
        smi_querier=lambda: fake_rows,
        state_provider=lambda: ("BALANCED", {0: 1, 1: 2}),
    )
    samples = sampler.sample_once()
    assert len(samples) == 2
    assert sampler.is_over_vram_budget(0) is True
    assert sampler.is_over_vram_budget(1) is False
    assert sampler.get_peak_vram_mb(0) == 14000.0

    sampler.log_worker_event("tts_batch_completed", gpu_id=0, worker_id="w0", batch_size=8)
    with trace_csv.open("r", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    assert tuple(rows[0]) == GPU_TRACE_COLUMNS
    assert len(rows) == 3  # header + 2 GPUs
    events = [json.loads(line) for line in log_jsonl.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(events) == 1 and events[0]["event"] == "tts_batch_completed"


class _FakeOomCrashWorkerHandle:
    """Simulates isolated spawn worker with configurable OOM and crash faults."""

    _pid_counter = 50000

    def __init__(self, *, mp_ctx, physical_gpu_id, worker_id, role, options):
        self.physical_gpu_id = physical_gpu_id
        self.worker_id = worker_id
        self.role = role
        self.options = options
        type(self)._pid_counter += 1
        self.pid = type(self)._pid_counter
        self._alive = False
        self.tts_loaded = False

    def start(self):
        self._alive = True
        return {
            "type": "ready",
            "pid": self.pid,
            "env_cuda_visible_devices": str(self.physical_gpu_id),
        }

    def is_alive(self):
        return self._alive

    def execute(self, op, payload=None, timeout=600.0):
        payload = payload or {}
        if op == "unload_tts":
            self.tts_loaded = False
            return {"ok": True, "data": {"unloaded": True}}
        if op == "tts_batch":
            tasks = payload["tasks"]
            bs = int(payload.get("batch_size", len(tasks)))
            if bs > 2 or len(tasks) > 2:
                self._alive = False
                raise WorkerExecutionError("CUDA out of memory while allocating batch", is_oom=True)
            results = []
            for t in tasks:
                out_p = Path(t["output_path"])
                sha = _write_signal_wav(out_p, 0.4)
                results.append({"seq_id": t["seq_id"], "output_path": str(out_p), "sha256": sha, "duration": 0.4})
            return {"ok": True, "data": {"results": results, "vram_mb": 480.0, "env_cuda_visible_devices": str(self.physical_gpu_id)}}
        if op == "render_shot":
            crash_once = self.options.get("crash_once_state")
            if crash_once is not None and not crash_once["crashed"]:
                crash_once["crashed"] = True
                self._alive = False
                raise WorkerExecutionError("Simulated NVENC worker crash", crashed=True)
            task = payload["task"]
            out_p = Path(task["output_clip_path"])
            out_p.parent.mkdir(parents=True, exist_ok=True)
            out_p.write_bytes(b"\x00\x00\x00\x20ftypisom" + b"\x01" * 2048)
            return {
                "ok": True,
                "data": {
                    "output_clip_path": str(out_p),
                    "gpu_index_used": 0,
                    "env_cuda_visible_devices": str(self.physical_gpu_id),
                },
            }
        raise ValueError(f"Unsupported op {op}")

    def shutdown(self, timeout=10.0):
        self._alive = False

    def kill(self):
        self._alive = False


def test_oom_and_worker_crash_recovery_requeues_exact_tasks(tmp_path):
    crash_state = {"crashed": False}
    sampler = GpuTelemetrySampler(tmp_path / "trace.csv", tmp_path / "worker.jsonl", smi_querier=lambda: [])
    pool = CudaWorkerPool(
        gpu_ids=(0, 1),
        tts_replicas_per_gpu=2,
        nvenc_workers_per_gpu=2,
        initial_tts_batch_size=8,
        telemetry=sampler,
        worker_options={"crash_once_state": crash_state},
        worker_handle_factory=_FakeOomCrashWorkerHandle,
    )
    try:
        tasks = [
            {"seq_id": f"{i:03d}", "text": f"Line {i}", "voice": "Ngọc Huyền", "speed": 1.0, "output_path": str(tmp_path / f"a_{i}.wav")}
            for i in range(6)
        ]
        results = pool.submit_tts_batch(0, tasks, batch_size=8, chapter="2")
        assert [r["seq_id"] for r in results] == [t["seq_id"] for t in tasks]
        for r in results:
            assert Path(r["output_path"]).is_file()
        stats = pool.get_stats()[0]
        assert stats["oom_events"] >= 1
        assert stats["current_tts_batch_size"] <= 4
        assert stats["current_tts_replicas"] == 1

        # Crash recovery on GPU 1 render shot re-queues exact shot
        shot_out = tmp_path / "shot_001.mp4"
        clip = pool.submit_render_shot(1, {"shot_id": "shot_00001", "output_clip_path": str(shot_out)}, chapter="2")
        assert clip == shot_out and shot_out.is_file()
        assert pool.get_stats()[1]["crash_events"] == 1
        assert pool.get_stats()[1]["current_nvenc_workers"] == 1

        log_events = [json.loads(line)["event"] for line in (tmp_path / "worker.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        assert "oom_recovery" in log_events
        assert "worker_crash_recovery" in log_events
    finally:
        pool.shutdown()


# ==============================================================================
# 4. Streaming `Ready Chapter Queue` Overlap + Real FFmpeg + G8 Receipts
# ==============================================================================


class _RealMediaFakeGpuWorkerHandle:
    """
    Simulates spawn GPU workers with timestamps + real 48kHz audio / real FFmpeg
    shot rendering so `validate_final_artifact` & G8 receipts verify end-to-end.
    """

    _pid_counter = 60000

    def __init__(self, *, mp_ctx, physical_gpu_id, worker_id, role, options):
        self.physical_gpu_id = physical_gpu_id
        self.worker_id = worker_id
        self.role = role
        self.options = options
        type(self)._pid_counter += 1
        self.pid = type(self)._pid_counter
        self._alive = False
        self.canvas_cache: Dict[Any, Any] = {}

    def start(self):
        self._alive = True
        return {"type": "ready", "pid": self.pid, "env_cuda_visible_devices": str(self.physical_gpu_id)}

    def is_alive(self):
        return self._alive

    def execute(self, op, payload=None, timeout=600.0):
        payload = payload or {}
        timeline = self.options.get("timeline")
        if op == "unload_tts":
            if timeline is not None:
                timeline.append(("unload_tts", self.physical_gpu_id, time.monotonic()))
            return {"ok": True, "data": {"unloaded": True}}
        if op == "tts_batch":
            tasks = payload["tasks"]
            ch = str(tasks[0].get("chapter_number", ""))
            if timeline is not None:
                timeline.append(("tts_start", ch, self.physical_gpu_id, time.monotonic()))
            time.sleep(0.06)  # ensures observable overlap window
            results = []
            for t in tasks:
                out_p = Path(t["output_path"])
                sha = _write_signal_wav(out_p, 0.5)
                results.append({"seq_id": t["seq_id"], "output_path": str(out_p), "sha256": sha, "duration": 0.5})
            if timeline is not None:
                timeline.append(("tts_end", ch, self.physical_gpu_id, time.monotonic()))
            return {
                "ok": True,
                "data": {
                    "results": results,
                    "vram_mb": 475.1,
                    "env_cuda_visible_devices": str(self.physical_gpu_id),
                },
            }
        if op == "render_shot":
            from src.cloud.dual_t4_orchestrator import _resolve_fast_renderer_api

            task = payload["task"]
            ch = str(task.get("chapter_number", ""))
            if timeline is not None:
                timeline.append(("render_start", ch, self.physical_gpu_id, time.monotonic()))
            _, render_shot_fn, _ = _resolve_fast_renderer_api()
            clip_path = render_shot_fn(task, canvas_cache=self.canvas_cache, gpu_index=0)
            if timeline is not None:
                timeline.append(("render_end", ch, self.physical_gpu_id, time.monotonic()))
            return {
                "ok": True,
                "data": {
                    "output_clip_path": str(clip_path),
                    "gpu_index_used": 0,
                    "env_cuda_visible_devices": str(self.physical_gpu_id),
                },
            }
        raise ValueError(f"Unsupported op {op}")

    def shutdown(self, timeout=10.0):
        self._alive = False

    def kill(self):
        self._alive = False


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required for streaming overlap L2 fixture")
def test_streaming_ready_chapter_queue_overlap_and_g8_receipts(tmp_path, monkeypatch):
    proj_dir = _create_multi_chapter_series(tmp_path, monkeypatch, chapters=["1", "2", "3"], approve_ch1=True)
    timeline: List[Any] = []

    def _smi_fake():
        return [
            {
                "gpu_id": 0, "gpu_uuid": "GPU-T4-0", "name": "Tesla T4",
                "vram_total_mb": 15360.0, "vram_used_mb": 900.0, "vram_free_mb": 14460.0,
                "gpu_util_pct": 80.0, "encoder_util_pct": 25.0, "temperature_c": 60.0,
            },
            {
                "gpu_id": 1, "gpu_uuid": "GPU-T4-1", "name": "Tesla T4",
                "vram_total_mb": 15360.0, "vram_used_mb": 800.0, "vram_free_mb": 14560.0,
                "gpu_util_pct": 75.0, "encoder_util_pct": 70.0, "temperature_c": 58.0,
            },
        ]

    receipts_dir = tmp_path / "receipts"
    orch = DualT4Orchestrator(
        workspace_dir=proj_dir,
        chapters=["1", "2", "3"],
        profile=BenchmarkProfileId.B3,
        encoder="libx264",
        compile_feature=True,
        receipts_dir=receipts_dir,
        smi_querier=_smi_fake,
        worker_pool_factory=lambda **kw: CudaWorkerPool(
            **kw,
            worker_options={"timeline": timeline},
            worker_handle_factory=_RealMediaFakeGpuWorkerHandle,
        ),
    )
    tel = orch.run_pipeline()
    assert tel.chapters == ["1", "2", "3"]
    assert (proj_dir / "chapters" / "1" / "video" / "final.mp4").is_file()
    assert fingerprint(proj_dir / "chapters" / "1" / "video" / "final.mp4") == fingerprint(
        proj_dir / "chapters" / "1" / "video" / "preview.mp4"
    )
    assert (proj_dir / "chapters" / "2" / "video" / "final.mp4").is_file()
    assert (proj_dir / "chapters" / "3" / "video" / "final.mp4").is_file()
    assert (proj_dir / "outputs" / "full_recap_feature.mp4").is_file()

    # Verify streaming overlap: Chapter 2 starts rendering BEFORE Chapter 3 finishes TTS!
    ch2_render_starts = [item[3] for item in timeline if item[0] == "render_start" and item[1] == "2"]
    ch3_tts_ends = [item[3] for item in timeline if item[0] == "tts_end" and item[1] == "3"]
    assert ch2_render_starts and ch3_tts_ends
    assert min(ch2_render_starts) < max(ch3_tts_ends), (
        "Expected chapter-level streaming overlap (ch2 render before ch3 TTS ends)"
    )

    # Verify TTS unloaded cleanly on transition to drain/render-heavy
    assert any(item[0] == "unload_tts" for item in timeline)

    # Verify G8 receipts (except result manifest written by run_series_worker)
    for fname in (
        "dual_t4_environment.json",
        "dual_t4_gpu_trace.csv",
        "dual_t4_worker_log.jsonl",
        "dual_t4_artifact_report.json",
    ):
        assert (receipts_dir / fname).is_file()
    art = read_json(receipts_dir / "dual_t4_artifact_report.json")
    assert art["gpu_usage_evidence"]["0"]["tts_items"] + art["gpu_usage_evidence"]["0"]["render_shots"] > 0
    assert art["gpu_usage_evidence"]["1"]["tts_items"] + art["gpu_usage_evidence"]["1"]["render_shots"] > 0


# ==============================================================================
# 5. B0-B5 Architecture Profiles & StartupAutotuner Freeze
# ==============================================================================


def test_b0_b5_profiles_and_startup_autotuner_freeze():
    assert set(ARCHITECTURE_PROFILES.keys()) == {
        BenchmarkProfileId.B0,
        BenchmarkProfileId.B1,
        BenchmarkProfileId.B2,
        BenchmarkProfileId.B3,
        BenchmarkProfileId.B4,
        BenchmarkProfileId.B5,
    }
    bench = benchmark_architecture_profiles(
        lambda prof: {
            "wall_clock_seconds": {
                "B0": 120.0,
                "B1": 75.0,
                "B2": 62.0,
                "B3": 48.0,
                "B4": 44.0,
                "B5": 38.0,
            }[prof.profile_id.value],
            "peak_vram_mb": 4200.0,
            "errors": 0,
        }
    )
    assert bench["profiles_tested"] == ["B0", "B1", "B2", "B3", "B4", "B5"]
    assert bench["winner_profile_id"] == "B5"

    # StartupAutotuner stops on <5% gain, VRAM > budget, latency/errors degrade, and freezes
    def _probe(cand):
        if cand.tts_batch_size == 32:
            # Exceeds budget 13824 MB -> stops batch scaling
            return AutotuneSample(throughput=40.0, latency_p95_sec=0.5, peak_vram_mb=14200.0)
        if cand.tts_replicas_per_gpu == 3:
            # Gain < 5% -> stops replica scaling
            return AutotuneSample(throughput=22.2, latency_p95_sec=0.4, peak_vram_mb=3200.0)
        if cand.nvenc_workers_per_gpu == 3:
            # Latency degraded -> stops NVENC scaling
            return AutotuneSample(throughput=27.0, latency_p95_sec=0.9, peak_vram_mb=2400.0)
        if cand.coexistence:
            # Coexistence brings +15% gain within VRAM budget
            return AutotuneSample(throughput=30.0, latency_p95_sec=0.42, peak_vram_mb=5000.0)
        score = 10.0
        if cand.tts_batch_size == 8:
            score = 14.0
        elif cand.tts_batch_size == 16:
            score = 18.0
        if cand.tts_replicas_per_gpu == 2:
            score = 22.0
        if cand.nvenc_workers_per_gpu == 2:
            score = 26.0
        return AutotuneSample(throughput=score, latency_p95_sec=0.4, peak_vram_mb=2400.0)

    tuner = StartupAutotuner(total_vram_mb=15360.0)
    frozen = tuner.autotune(_probe)
    assert frozen.frozen is True
    assert frozen.tts_batch_size == 16
    assert frozen.tts_replicas_per_gpu == 2
    assert frozen.nvenc_workers_per_gpu == 2
    assert frozen.coexistence is True
    assert any("vram_exceeds_budget" in r for r in frozen.stop_reasons)
    assert any("gain_below_5pct" in r for r in frozen.stop_reasons)
    assert any("latency_degraded" in r for r in frozen.stop_reasons)
    with pytest.raises(dataclasses.FrozenInstanceError):
        frozen.tts_batch_size = 32  # type: ignore[misc]


# ==============================================================================
# 6. Series Payload, Kaggle Dispatch, G8 Dual-GPU Gate, & `produce-series` G6 Gate
# ==============================================================================


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required for series payload/sync L2")
def test_series_payload_worker_and_g8_sync_end_to_end(tmp_path, monkeypatch):
    proj_dir = _create_multi_chapter_series(tmp_path, monkeypatch, chapters=["1", "2", "3"], approve_ch1=True)

    # 1. Fail closed if Ch1 approval is removed, even with skip_preview=True
    approval_path = proj_dir / "preview_approval.json"
    saved_approval = approval_path.read_bytes()
    approval_path.unlink()
    with pytest.raises(Exception, match="Gate G6"):
        package_series_payload(proj_dir, ["1", "2", "3"], tmp_path / "fail.tar.gz", skip_preview=True)
    approval_path.write_bytes(saved_approval)

    # 2. Package & verify manifest (supports tar extraction & Kaggle auto-extracted manifest)
    archive_path, digest = package_series_payload(proj_dir, ["1", "2", "3"], tmp_path / "series_payload.tar.gz")
    unpack = tmp_path / "kaggle_runtime"
    unpack.mkdir()
    with tarfile.open(archive_path) as tar:
        tar.extractall(unpack, filter="data")
    manifest = verify_series_payload_manifest(unpack, digest)
    assert manifest["chapters"] == ["1", "2", "3"]

    service = object.__new__(KaggleCloudService)
    service._series_payload_manifest_hash = digest
    worker_script = service.generate_series_worker_script("p8_series", ["1", "2", "3"], encoder_preference="libx264")
    compile(worker_script, "series_worker.py", "exec")
    assert digest in worker_script
    assert "payload_manifest.json" in worker_script
    assert worker_script.index("Payload inventory mismatch") < worker_script.index("pip', 'install'")

    # 3. Run in-container series worker with mocked spawn handles producing real media
    results_dir = tmp_path / "kaggle_results"
    smi_both = lambda: [
        {"gpu_id": 0, "gpu_uuid": "GPU-0", "name": "Tesla T4", "vram_total_mb": 15360.0, "vram_used_mb": 600.0, "vram_free_mb": 14760.0, "gpu_util_pct": 80.0, "encoder_util_pct": 30.0, "temperature_c": 60.0},
        {"gpu_id": 1, "gpu_uuid": "GPU-1", "name": "Tesla T4", "vram_total_mb": 15360.0, "vram_used_mb": 700.0, "vram_free_mb": 14660.0, "gpu_util_pct": 75.0, "encoder_util_pct": 65.0, "temperature_c": 58.0},
    ]
    receipt = run_series_worker(
        unpack,
        digest,
        results_dir,
        encoder="libx264",
        orchestrator_factory=lambda **kw: DualT4Orchestrator(
            workspace_dir=kw["workspace_dir"],
            chapters=kw["chapters"],
            render_workers=kw["render_workers"],
            compile_feature=kw["compile_feature"],
            profile=kw["profile"],
            encoder="libx264",
            work_dir=kw["work_dir"],
            receipts_dir=kw["receipts_dir"],
            smi_querier=smi_both,
            worker_pool_factory=lambda **pkw: CudaWorkerPool(
                **pkw, worker_handle_factory=_RealMediaFakeGpuWorkerHandle
            ),
        ),
    )
    for fname in G8_REQUIRED_RECEIPTS:
        assert (results_dir / fname).is_file()
    assert receipt["payload_manifest_sha256"] == digest

    # 4. Sync results back to local project via trusted dispatch snapshot
    expected_inputs = {
        rel.removeprefix("project/"): h
        for rel, h in manifest["files"].items()
        if rel.startswith("project/")
    }
    sync_out = sync_series_results(proj_dir, ["1", "2", "3"], results_dir, digest, expected_inputs)
    assert sync_out["status"] == "success"
    assert sync_out["g8_evidence"]["valid"] is True
    assert (proj_dir / "chapters" / "2" / "video" / "final.mp4").is_file()
    assert (proj_dir / "chapters" / "3" / "video" / "final.mp4").is_file()
    assert (proj_dir / "cloud_receipts" / "dual_t4" / "dual_t4_result_manifest.json").is_file()

    # 5. Negative G8 check: if GPU1 did zero real work or mock_mode=True, verify_g8_dual_t4_evidence rejects
    tampered = tmp_path / "tampered_results"
    shutil.copytree(results_dir, tampered)
    art = read_json(tampered / "dual_t4_artifact_report.json")
    art["gpu_usage_evidence"]["1"]["tts_items"] = 0
    art["gpu_usage_evidence"]["1"]["render_shots"] = 0
    atomic_write_json(tampered / "dual_t4_artifact_report.json", art)
    with pytest.raises(ValueError, match="zero real work on GPU 1"):
        verify_g8_dual_t4_evidence(tampered, ["1", "2", "3"])

    env_tamper = tmp_path / "tampered_env"
    shutil.copytree(results_dir, env_tamper)
    env_doc = read_json(env_tamper / "dual_t4_environment.json")
    env_doc["mock_mode"] = True
    atomic_write_json(env_tamper / "dual_t4_environment.json", env_doc)
    with pytest.raises(ValueError, match="mock_mode"):
        verify_g8_dual_t4_evidence(env_tamper, ["1", "2", "3"])

    # 6. Tampered payload fails manifest verification fail-closed
    (unpack / "src" / "cloud" / "series_payload.py").write_text("# tampered\n", encoding="utf-8")
    with pytest.raises(ValueError, match="altered"):
        verify_series_payload_manifest(unpack, digest)


def test_produce_series_cli_and_dispatch_fail_closed_g6_and_metadata(tmp_path, monkeypatch, capsys):
    proj_dir = _create_multi_chapter_series(tmp_path, monkeypatch, chapters=["1", "2"], approve_ch1=False)
    monkeypatch.setattr("src.cloud.kaggle_service.KaggleCloudService.is_authenticated", True)

    # CLI produce-series with --skip-preview must still fail closed with exit code 2 (Gate G6)
    rc = main(["cloud", "produce-series", "-p", "p8_series", "-c", "1,2", "--skip-preview", "--json"])
    assert rc == 2
    out = capsys.readouterr().out
    resp = json.loads(out[out.find("{") :])
    assert "G6" in resp["error"]["message"]

    # Approve Ch.1 and dispatch non-waiting series job -> checks Kaggle kernel-metadata.json
    render_chapter(proj_dir, "1", quality="preview")
    approve_preview(
        proj_dir,
        "L2 human fixture",
        review_fingerprint(proj_dir),
        fingerprint(proj_dir / "chapters" / "1" / "video" / "preview.mp4"),
    )
    service = object.__new__(KaggleCloudService)
    service.api_token = "KGAT_secret_test_token_123456"
    service._username = "unit-user"
    service._api = MagicMock()
    service._api.dataset_status.return_value = SimpleNamespace(status="ready")
    service._api.dataset_list_files.return_value = SimpleNamespace(
        dataset_files=[SimpleNamespace(name="payload.tar.gz")]
    )
    service._api.kernels_push.return_value = SimpleNamespace(ref="unit-user/mr-p8-series")
    monkeypatch.setattr("src.cloud.kaggle_service.time.sleep", lambda *_: None)

    dispatched = service.dispatch_series_job("p8_series", ["1", "2"], wait=False, skip_preview=True)
    assert dispatched["status"] == "dispatched"
    staging = Path(dispatched["staging_dir"])
    kmeta = read_json(staging / "kernel-metadata.json")
    assert kmeta["is_private"] is True
    assert kmeta["enable_gpu"] is True
    assert kmeta["machine_shape"] == "NvidiaTeslaT4"
    dmeta = read_json(staging / "dataset" / "dataset-metadata.json")
    assert dmeta["licenses"] == [{"name": "other"}]
    for f in staging.rglob("*"):
        if f.is_file() and f.suffix in {".json", ".py"}:
            assert service.api_token not in f.read_text(encoding="utf-8")


# ==============================================================================
# 7. SingleSessionProductionRunner Stale Keys Fix
# ==============================================================================


def test_runner_maps_canonical_render_export_keys(tmp_path, monkeypatch):
    from src.production.runner import SingleSessionProductionRunner
    from src.production.schemas import ProductionJobConfig, ProductionStageStatus

    proj_dir = _create_multi_chapter_series(tmp_path, monkeypatch, chapters=["1"], approve_ch1=True)
    render_chapter(proj_dir, "1", quality="final")
    config = ProductionJobConfig(project_slug="p8_series", target_chapters=["1"], force=True, compile=False, export_bundle=False)
    runner = SingleSessionProductionRunner.load_or_create("p8_series", config=config)

    fake_render_res = {
        "video_path": str(proj_dir / "chapters" / "1" / "video" / "final.mp4"),
        "subtitle_path": str(proj_dir / "chapters" / "1" / "subtitles" / "final.srt"),
        "duration_seconds": 0.5,
        "validation": {"valid": True, "video": {"frames": 15}},
        "export_paths": {
            "video_padded": str(tmp_path / "exports" / "p8_series" / "chapter_001.mp4"),
            "subtitle_padded": str(tmp_path / "exports" / "p8_series" / "chapter_001.srt"),
        },
    }
    monkeypatch.setattr("src.production.runner.handle_tts", lambda *_: {"segments_synthesized": 0, "segments_cached": 1})
    monkeypatch.setattr("src.production.runner.handle_render", lambda *_: fake_render_res)
    monkeypatch.setattr("src.production.runner.resolve_video_encoder", lambda *_: ("libx264", [], "CPU"))

    manifest = runner.run()
    assert manifest.status == ProductionStageStatus.COMPLETED
    ch1 = manifest.chapters["1"]
    assert ch1.video_path == fake_render_res["video_path"]
    assert ch1.subtitle_path == fake_render_res["subtitle_path"]
    assert ch1.export_video_path == fake_render_res["export_paths"]["video_padded"]
    assert ch1.export_subtitle_path == fake_render_res["export_paths"]["subtitle_padded"]
    assert ch1.rendered_clips_count == 1


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required for per-shot subtitle burn L2")
def test_dual_t4_burn_subtitles_per_shot_and_font_size_propagation(tmp_path, monkeypatch):
    import functools
    import src.render.fast_renderer as fast_renderer
    from src.cloud.dual_t4_orchestrator import _build_chapter_shot_tasks, _concat_chapter_shot_clips

    proj_dir = _create_multi_chapter_series(tmp_path, monkeypatch, chapters=["1", "2"], approve_ch1=False)
    proj_cfg = read_json(proj_dir / "project.json")
    proj_cfg["render_settings"]["subtitle"]["font_size"] = 30
    atomic_write_json(proj_dir / "project.json", proj_cfg)

    answers = _make_valid_answers_dict()
    answers["Q01"]["user_answer"] = {"chapters": "1-2", "source": "https://example.invalid/p8"}
    answers["Q02"]["user_answer"] = "kaggle"
    answers["Q05"]["user_answer"] = True
    answers["Q06"]["user_answer"] = "static"
    answers["Q07"]["user_answer"] = False
    answers["Q08"]["user_answer"] = {"no_upload": True}
    answers["Q09"]["user_answer"] = "preview"
    answers["Q10"]["user_answer"] = {"none": True}
    record_series_setup("p8_series", answers, project_dir=proj_dir)

    render_chapter(proj_dir, "1", quality="preview")
    approve_preview(
        proj_dir,
        "L2 human fixture",
        review_fingerprint(proj_dir),
        fingerprint(proj_dir / "chapters" / "1" / "video" / "preview.mp4"),
    )

    recorded_build_calls: List[Dict[str, Any]] = []
    recorded_concat_calls: List[Dict[str, Any]] = []
    orig_build = fast_renderer.build_shot_render_tasks
    orig_concat = fast_renderer.concat_shot_clips

    @functools.wraps(orig_build)
    def _spy_build(*args, **kwargs):
        tasks = orig_build(*args, **kwargs)
        recorded_build_calls.append({"kwargs": dict(kwargs), "tasks": list(tasks)})
        return tasks

    @functools.wraps(orig_concat)
    def _spy_concat(*args, **kwargs):
        recorded_concat_calls.append(dict(kwargs))
        return orig_concat(*args, **kwargs)

    monkeypatch.setattr(fast_renderer, "build_shot_render_tasks", _spy_build)
    monkeypatch.setattr(fast_renderer, "concat_shot_clips", _spy_concat)

    orch = DualT4Orchestrator(
        workspace_dir=proj_dir,
        chapters=["1", "2"],
        profile=BenchmarkProfileId.B3,
        encoder="libx264",
        compile_feature=False,
        receipts_dir=tmp_path / "sub_receipts",
        smi_querier=lambda: [],
        worker_pool_factory=lambda **kw: CudaWorkerPool(
            **kw, worker_handle_factory=_RealMediaFakeGpuWorkerHandle
        ),
    )
    tel = orch.run_pipeline()
    assert tel.chapters == ["1", "2"]
    assert (proj_dir / "chapters" / "2" / "video" / "final.mp4").is_file()

    # Verify per-shot burn + font_size=30 + chapter_number propagation into fast_renderer
    assert len(recorded_build_calls) == 1
    b_call = recorded_build_calls[0]
    assert b_call["kwargs"]["burn_subtitles"] is True
    assert b_call["kwargs"]["subtitle_font_size"] == 30
    assert b_call["kwargs"]["subtitle_cues"]
    for t in b_call["tasks"]:
        assert t["subtitle_font_size"] == 30
        assert t["burn_subtitles"] is True
        assert t["local_subtitle_cues"]

    # Verify concat received zero-reencode burn_subtitles=False (already burned per-shot) + font_size=30 + gpu_index=0
    assert len(recorded_concat_calls) == 1
    c_call = recorded_concat_calls[0]
    assert c_call["burn_subtitles"] is False
    assert c_call["subtitle_path"] is None
    assert c_call["subtitle_font_size"] == 30
    assert c_call["gpu_index"] == 0

    # Also verify _build_chapter_shot_tasks stamps chapter_number explicitly
    assert _build_chapter_shot_tasks is not None and _concat_chapter_shot_clips is not None
    ch2_dir = proj_dir / "chapters" / "2"
    from src.core.storage import load_manifest, load_recap_script
    from src.core.visual_contract import VisualPlan
    stamped_tasks, per_shot = _build_chapter_shot_tasks(
        orig_build,
        ch2_dir,
        "2",
        VisualPlan.load(ch2_dir / "visual_plan.json"),
        load_manifest(ch2_dir),
        work_dir=tmp_path / "stamp_work",
        encoder="libx264",
        subtitles_path=ch2_dir / "subtitles" / "final.srt",
        burn_subtitles=True,
        subtitle_font_size=30,
        quality_mode="final",
    )
    assert per_shot is True
    assert stamped_tasks and all(t.get("chapter_number") == "2" for t in stamped_tasks)
    assert load_recap_script(ch2_dir).chapter_number == "2"
