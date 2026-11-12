"""
scripts/benchmark/run_dual_t4_matrix.py
P9 Dual-T4 Benchmark Matrix Harness (`B0`..`B5`).

Supports:
  - `--local-dry-run` / `--local`: Self-contained local smoke verification using
    a tiny synthetic fixture or cloned project workspace with real `CudaWorkerPool`
    (`spawn`), real FFmpeg rendering, real `validate_audio` + `validate_final_artifact`,
    and explicitly tagged `dry_run=True` / `mock_mode=True` outputs.
  - `--dispatch`: Packages ONE pinned SHA-256 series+benchmark dataset and pushes
    ONE Kaggle Dual-T4 kernel (`B0`..`B5` back-to-back on identical hardware/corpus),
    recording `dataset_upload_sec`, `queue_sec`, and `download_sec` separately.
  - `--in-container`: Runs inside Kaggle Dual-T4 (`mock_mode=False`), enforces >= 2
    physical T4 GPUs via `nvidia-smi`, optional `--warmup`, executes `--variants` x
    `--repeats` on identical fresh workspace clones, refuses any invalid artifact,
    and emits JSON + Markdown reports with SHA-256 result manifest.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import numpy as np
from PIL import Image

from src.cloud.cuda_worker_pool import (
    CudaWorkerPool,
    compute_usable_vram_budget,
    query_nvidia_smi,
)
from src.cloud.dual_t4_orchestrator import DualT4Orchestrator
from src.cloud.elastic_scheduler import (
    ARCHITECTURE_PROFILES,
    BenchmarkProfileId,
    resolve_architecture_profile,
)
from src.cloud.series_payload import (
    SERIES_PAYLOAD_SCHEMA_VERSION,
    _normalize_chapters,
    verify_series_payload_manifest,
)
from src.core.human_review import approve_preview, require_preview_approval, require_voice_approval, review_fingerprint
from src.core.logger import logger
from src.core.schemas import (
    PanelItem,
    PanelManifest,
    RecapScript,
    ScriptSegment,
    utc_iso_now,
)
from src.core.series_setup import record_series_setup, require_series_setup
from src.core.storage import (
    atomic_write_json,
    calculate_sha256,
    get_project_dir,
    init_chapter_layout,
    init_project_layout,
    load_recap_script,
    read_json,
)
from src.render.artifact_validation import fingerprint, validate_audio
from src.render.chapter_renderer import render_chapter, resolve_audio

DEFAULT_VARIANTS: Tuple[str, ...] = ("B0", "B1", "B2", "B3", "B4", "B5")


def _parse_csv_list(raw: str) -> List[str]:
    items: List[str] = []
    seen = set()
    for part in str(raw or "").split(","):
        cleaned = part.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            items.append(cleaned)
    return items


def _parse_variants(raw: str) -> List[BenchmarkProfileId]:
    tokens = _parse_csv_list(raw)
    if not tokens:
        tokens = list(DEFAULT_VARIANTS)
    parsed: List[BenchmarkProfileId] = []
    for tok in tokens:
        parsed.append(resolve_architecture_profile(tok).profile_id)
    return parsed


def _discover_project_chapters(proj_dir: Path) -> List[str]:
    """Discovers chapters from `chapters/*/script.json` first, falling back to `project.json`."""
    chapters_root = proj_dir / "chapters"
    discovered: List[str] = []
    if chapters_root.is_dir():
        for entry in chapters_root.iterdir():
            if entry.is_dir() and (entry / "script.json").is_file():
                discovered.append(entry.name)
    if discovered:
        discovered.sort(key=lambda x: (0, int(x)) if x.isdigit() else (1, x))
        return discovered
    cfg = read_json(proj_dir / "project.json") if (proj_dir / "project.json").is_file() else {}
    return [str(c) for c in (cfg.get("chapters") or []) if str(c).strip()]


def _get_git_commit() -> str:
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=3.0,
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except Exception:
        pass
    return "unknown"


# ==============================================================================
# Deterministic Local Dry-Run Fixture & Worker Handle
# ==============================================================================


def _write_signal_wav(path: Path, duration_sec: float = 0.5) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    rate = 48000
    n = max(2400, int(round(duration_sec * rate)))
    samples = (np.sin(np.arange(n) / 10.0) * 3500.0).astype("<i2")
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(samples.tobytes())
    return calculate_sha256(path)


def _dry_run_tts_worker_factory(*, gpu_id: int = 0, **_: Any) -> Any:
    """In-subprocess fast deterministic neural-fixture TTS worker for `--local-dry-run`."""

    class _DryRunTTSWorker:
        def __init__(self, gid: int) -> None:
            self.gid = gid
            self._loaded = True

        def vram_usage_mb(self) -> Dict[str, float]:
            return {"allocated_mb": 420.0, "reserved_mb": 512.0}

        def synthesize_batch_tasks(
            self,
            tasks: List[Dict[str, Any]],
            *,
            batch_size: int = 8,
            precision: str = "auto",
            cpu_workers: int = 4,
        ) -> List[Dict[str, Any]]:
            _ = (batch_size, precision, cpu_workers)
            time.sleep(0.01 * max(1, len(tasks)))
            out: List[Dict[str, Any]] = []
            for t in tasks:
                out_p = Path(t["output_path"])
                sha = _write_signal_wav(out_p, 0.5)
                out.append(
                    {
                        "seq_id": t["seq_id"],
                        "output_path": str(out_p),
                        "sha256": sha,
                        "duration": 0.5,
                    }
                )
            return out

        def unload(self) -> None:
            self._loaded = False

    return _DryRunTTSWorker(gpu_id)


def _dry_run_smi_querier() -> List[Dict[str, Any]]:
    return [
        {
            "gpu_id": 0,
            "gpu_uuid": "GPU-DRYRUN-T4-0000",
            "name": "Tesla T4 (DRY-RUN SIMULATED)",
            "vram_total_mb": 15360.0,
            "vram_used_mb": 640.0,
            "vram_free_mb": 14720.0,
            "usable_vram_budget_mb": compute_usable_vram_budget(15360.0),
            "gpu_util_pct": 45.0,
            "encoder_util_pct": 12.0,
            "temperature_c": 42.0,
        },
        {
            "gpu_id": 1,
            "gpu_uuid": "GPU-DRYRUN-T4-0001",
            "name": "Tesla T4 (DRY-RUN SIMULATED)",
            "vram_total_mb": 15360.0,
            "vram_used_mb": 512.0,
            "vram_free_mb": 14848.0,
            "usable_vram_budget_mb": compute_usable_vram_budget(15360.0),
            "gpu_util_pct": 40.0,
            "encoder_util_pct": 20.0,
            "temperature_c": 41.0,
        },
    ]


def build_tiny_matrix_fixture(base_dir: Path, *, slug: str = "p9_dryrun_series") -> Path:
    """Builds a self-contained tiny 3-chapter canonical project for `--local-dry-run`."""
    data_dir = base_dir / "data"
    os.environ["MANGA_RECAP_DATA_DIR"] = str(data_dir)
    os.environ["MANGA_RECAP_OUTPUT_DIR"] = str(base_dir / "exports")

    init_project_layout(slug, "P9 Dry-Run Benchmark Fixture")
    proj_dir = data_dir / "projects" / slug

    raw_values = {
        "Q01": {"chapters": "1-3", "source": "https://example.invalid/p9"},
        "Q02": "kaggle",
        "Q03": "Ngọc Huyền",
        "Q04": 1.0,
        "Q05": False,
        "Q06": "static",
        "Q07": False,
        "Q08": {"no_upload": True},
        "Q09": "preview",
        "Q10": {"none": True},
    }
    confirmed_ts = utc_iso_now()
    answers = {
        qid: {
            "question_id": qid,
            "question_title": qid,
            "user_answer": val,
            "answer_source": "user",
            "confirmed_at": confirmed_ts,
        }
        for qid, val in raw_values.items()
    }
    record_series_setup(slug, answers, project_dir=proj_dir)
    atomic_write_json(
        proj_dir / "voice_selection.json",
        {
            "project_slug": slug,
            "primary_voice": "Ngọc Huyền",
            "reviewer": "P9 dry-run fixture",
            "status": "approved",
            "speed": 1.0,
        },
    )

    chapters = ["1", "2", "3"]
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
                    narrator_text="Chương 1 đoạn mở đầu.",
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
                    narrator_text=f"Chương {ch} đoạn thứ nhất.",
                ),
                ScriptSegment(
                    seq_id="002",
                    evidence_panel_ids=[f"p_{ch}_1"],
                    narrator_text=f"Chương {ch} đoạn thứ hai.",
                ),
            ]
        atomic_write_json(
            c_dir / "script.json",
            RecapScript(project_slug=slug, chapter_number=ch, segments=segs),
        )

    render_chapter(proj_dir, "1", quality="preview")
    approve_preview(
        proj_dir,
        "P9 dry-run fixture",
        review_fingerprint(proj_dir),
        fingerprint(proj_dir / "chapters" / "1" / "video" / "preview.mp4"),
    )
    proj_cfg = read_json(proj_dir / "project.json")
    proj_cfg["chapters"] = list(chapters)
    atomic_write_json(proj_dir / "project.json", proj_cfg)
    return proj_dir


# ==============================================================================
# Warmup & Workspace Isolation Helpers
# ==============================================================================


def _run_warmup_session(
    *,
    dry_run: bool,
    scratch_dir: Path,
    smi_querier: Optional[Callable[[], List[Dict[str, Any]]]] = None,
    worker_pool_factory: Optional[Callable[..., CudaWorkerPool]] = None,
) -> float:
    """
    Pre-warms TTS weights/cache into `HF_HOME` inside an isolated `spawn` worker
    before timed variants run (so B0 does not pay one-off HF download latency).
    Excluded from per-variant `e2e_compute_wall_sec`; recorded in `model_load_sec`.
    """
    t0 = time.monotonic()
    warm_wav = scratch_dir / "warmup" / "warm.wav"
    warm_wav.parent.mkdir(parents=True, exist_ok=True)
    worker_opts: Dict[str, Any] = {}
    if dry_run:
        worker_opts["tts_worker_factory"] = _dry_run_tts_worker_factory
    try:
        if worker_pool_factory is not None:
            pool = worker_pool_factory(
                gpu_ids=(0,),
                tts_replicas_per_gpu=1,
                nvenc_workers_per_gpu=1,
                initial_tts_batch_size=1,
                worker_options=worker_opts,
            )
        else:
            pool = CudaWorkerPool(
                gpu_ids=(0,),
                tts_replicas_per_gpu=1,
                nvenc_workers_per_gpu=1,
                initial_tts_batch_size=1,
                worker_options=worker_opts,
            )
        try:
            pool.submit_tts_batch(
                0,
                [
                    {
                        "seq_id": "warmup_001",
                        "text": "Khởi động bộ tổng hợp giọng nói.",
                        "narrator_text": "Khởi động bộ tổng hợp giọng nói.",
                        "voice": "Ngọc Huyền",
                        "voice_id": "Ngọc Huyền",
                        "speed": 1.0,
                        "emotion": "neutral",
                        "emotion_cue": None,
                        "output_path": str(warm_wav),
                        "chapter_number": "warmup",
                    }
                ],
                batch_size=1,
                chapter="warmup",
            )
            pool.unload_tts_workers((0,))
        finally:
            pool.shutdown()
    finally:
        shutil.rmtree(warm_wav.parent, ignore_errors=True)
    _ = smi_querier
    return round(max(0.0, time.monotonic() - t0), 4)


def _prepare_clean_variant_workspace(
    source_project_dir: Path,
    dest_project_dir: Path,
    chapters: Sequence[str],
) -> None:
    """
    Clones pinned source project cleanly for one variant run and strips derived
    audio/render artifacts so every variant computes identical work from scratch.
    """
    if dest_project_dir.exists():
        shutil.rmtree(dest_project_dir)
    shutil.copytree(source_project_dir, dest_project_dir)
    for ch in chapters:
        chap_dir = dest_project_dir / "chapters" / ch
        for sub in ("audio", "video", "subtitles"):
            shutil.rmtree(chap_dir / sub, ignore_errors=True)
        for fname in ("visual_plan.json", "artifact_validation.json", f"ch_{ch}_audio_ready.json"):
            (chap_dir / fname).unlink(missing_ok=True)
    shutil.rmtree(dest_project_dir / "outputs", ignore_errors=True)


# ==============================================================================
# Variant Validation & Metrics Extraction
# ==============================================================================


def _verify_variant_outputs_and_isolation(
    *,
    profile_id: BenchmarkProfileId,
    workspace_dir: Path,
    receipts_dir: Path,
    chapters: Sequence[str],
    dry_run: bool,
) -> Dict[str, Any]:
    """
    Fail-closed verification after a variant run:
    - Verifies `validate_audio` & checksum match for EVERY utterance of EVERY chapter.
    - Verifies `dual_t4_artifact_report.json` shows `all_valid=True` & every chapter `valid=True`.
    - Verifies architectural GPU isolation (`B0` never uses GPU1; `B1` GPU0 TTS / GPU1 render;
      non-dry-run Dual-T4 variants perform real work on GPU0 & GPU1).
    """
    report_path = receipts_dir / "dual_t4_artifact_report.json"
    worker_log_path = receipts_dir / "dual_t4_worker_log.jsonl"
    trace_csv_path = receipts_dir / "dual_t4_gpu_trace.csv"
    env_path = receipts_dir / "dual_t4_environment.json"

    for p in (report_path, worker_log_path, trace_csv_path, env_path):
        if not p.is_file() or p.stat().st_size == 0:
            raise RuntimeError(f"{profile_id.value} missing required telemetry/receipt file: {p.name}")

    art_doc = read_json(report_path)
    per_chap = art_doc.get("per_chapter") or {}
    if not art_doc.get("all_valid"):
        raise RuntimeError(f"{profile_id.value} artifact report did not pass all_valid")

    total_audio_duration_sec = 0.0
    total_segments = 0
    total_video_frames = 0
    total_video_duration_sec = 0.0

    for ch in chapters:
        chap_dir = workspace_dir / "chapters" / ch
        ch_info = per_chap.get(ch) or {}
        val = ch_info.get("validation") or {}
        if val.get("valid") is not True:
            raise RuntimeError(f"{profile_id.value} chapter {ch} failed validate_final_artifact")

        script = load_recap_script(chap_dir)
        if not script.segments:
            raise RuntimeError(f"{profile_id.value} chapter {ch} has zero segments")
        for seg in script.segments:
            audio_p = resolve_audio(chap_dir, seg)
            a_rep = validate_audio(audio_p)
            if (
                seg.checksum != a_rep["sha256"]
                or seg.duration is None
                or abs(seg.duration - a_rep["duration"]) > 0.001
            ):
                raise RuntimeError(
                    f"{profile_id.value} chapter {ch} segment {seg.seq_id} audio validation mismatch"
                )
            total_audio_duration_sec += float(a_rep["duration"])
            total_segments += 1

        v_meta = val.get("video") or {}
        total_video_frames += int(v_meta.get("frames", 0))
        total_video_duration_sec += float(v_meta.get("duration", 0.0))

    # Inspect worker log for GPU isolation invariants
    tts_gpus_seen = set()
    render_gpus_seen = set()
    all_gpus_seen = set()
    for line in worker_log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        evt = json.loads(line)
        gid = int(evt.get("gpu_id", -1))
        if gid >= 0:
            all_gpus_seen.add(gid)
        ev_name = evt.get("event")
        if ev_name == "tts_batch_completed":
            tts_gpus_seen.add(gid)
        elif ev_name == "render_shot_completed":
            render_gpus_seen.add(gid)

    if profile_id == BenchmarkProfileId.B0:
        if 1 in all_gpus_seen:
            raise RuntimeError(f"B0 isolation violated: GPU 1 events observed ({all_gpus_seen})")
    elif profile_id == BenchmarkProfileId.B1:
        if tts_gpus_seen != {0} or 0 in render_gpus_seen or 1 not in render_gpus_seen:
            raise RuntimeError(
                f"B1 isolation violated: tts_gpus={tts_gpus_seen}, render_gpus={render_gpus_seen}"
            )
    elif not dry_run:
        usage = art_doc.get("gpu_usage_evidence") or {}
        for gid_key in ("0", "1"):
            g_info = usage.get(gid_key) or {}
            if int(g_info.get("tts_items", 0)) + int(g_info.get("render_shots", 0)) <= 0:
                raise RuntimeError(
                    f"{profile_id.value} Dual-T4 non-dry-run failed GPU {gid_key} real work check"
                )

    return {
        "total_audio_duration_sec": round(total_audio_duration_sec, 4),
        "total_segments": total_segments,
        "total_video_frames": total_video_frames,
        "total_video_duration_sec": round(total_video_duration_sec, 4),
        "tts_gpus_seen": sorted(tts_gpus_seen),
        "render_gpus_seen": sorted(render_gpus_seen),
    }


# ==============================================================================
# Core Matrix Runner
# ==============================================================================


def run_benchmark_matrix(
    *,
    source_project_dir: Path,
    chapters: Sequence[str],
    output_dir: Path,
    variants: Sequence[BenchmarkProfileId] = tuple(BenchmarkProfileId(v) for v in DEFAULT_VARIANTS),
    repeats: int = 1,
    warmup: bool = False,
    encoder: str = "auto",
    compile_feature: bool = False,
    dry_run: bool = False,
    boot_sec: float = 0.0,
    smi_querier: Optional[Callable[[], List[Dict[str, Any]]]] = None,
    worker_pool_factory: Optional[Callable[..., CudaWorkerPool]] = None,
    orchestrator_factory: Optional[Callable[..., DualT4Orchestrator]] = None,
    keep_artifacts: bool = False,
    extra_phase_timings: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """
    Executes `B0..B5` back-to-back across identical cloned workspaces, collecting
    primary/secondary/stage/lifecycle/validity metrics with fail-closed artifact checks.
    """
    src_proj = Path(source_project_dir).resolve()
    out_dir = Path(output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    target_chapters = _normalize_chapters(chapters)
    repeat_count = max(1, int(repeats))

    proj_cfg = read_json(src_proj / "project.json")
    slug = str(proj_cfg["slug"])
    require_series_setup(slug, project_dir=src_proj)
    require_voice_approval(src_proj)
    require_preview_approval(src_proj)

    active_smi = smi_querier or (_dry_run_smi_querier if dry_run else query_nvidia_smi)
    smi_rows = active_smi() or []
    distinct_uuids = {str(r.get("gpu_uuid") or r.get("gpu_id")) for r in smi_rows}
    needs_dual_gpu = any(len(ARCHITECTURE_PROFILES[v].gpu_ids) >= 2 for v in variants)
    if not dry_run:
        if len(smi_rows) < 1:
            raise RuntimeError("Non-dry-run benchmark matrix requires real NVIDIA GPU (`nvidia-smi` empty)")
        if needs_dual_gpu and (len(smi_rows) < 2 or len(distinct_uuids) < 2):
            raise RuntimeError(
                f"Dual-T4 benchmark requires >= 2 distinct physical GPUs, found {len(smi_rows)} ({sorted(distinct_uuids)})"
            )

    scratch_root = Path(tempfile.mkdtemp(prefix="p9-bench-matrix-"))
    prev_data_dir = os.environ.get("MANGA_RECAP_DATA_DIR")
    warmup_sec = 0.0
    runs: List[Dict[str, Any]] = []

    try:
        if warmup:
            warmup_sec = _run_warmup_session(
                dry_run=dry_run,
                scratch_dir=scratch_root,
                smi_querier=active_smi,
                worker_pool_factory=worker_pool_factory,
            )

        for rep_idx in range(1, repeat_count + 1):
            for order_idx, profile_id in enumerate(variants, start=1):
                prof = ARCHITECTURE_PROFILES[profile_id]
                run_tag = f"{profile_id.value}_r{rep_idx}"
                run_root = scratch_root / "runs" / run_tag
                run_data_dir = run_root / "data"
                run_proj_dir = run_data_dir / "projects" / slug
                run_work_dir = run_root / "work"
                run_receipts_dir = out_dir / "receipts" / run_tag
                run_receipts_dir.mkdir(parents=True, exist_ok=True)

                _prepare_clean_variant_workspace(src_proj, run_proj_dir, target_chapters)
                os.environ["MANGA_RECAP_DATA_DIR"] = str(run_data_dir)

                worker_opts: Dict[str, Any] = {}
                if dry_run:
                    worker_opts["tts_worker_factory"] = _dry_run_tts_worker_factory

                if orchestrator_factory is not None:
                    orch = orchestrator_factory(
                        workspace_dir=run_proj_dir,
                        chapters=target_chapters,
                        render_workers=prof.nvenc_workers_per_gpu,
                        compile_feature=compile_feature,
                        profile=prof,
                        encoder="libx264" if (dry_run and encoder == "auto") else encoder,
                        autotune=False,
                        work_dir=run_work_dir,
                        receipts_dir=run_receipts_dir,
                        worker_pool_factory=worker_pool_factory,
                        worker_options=worker_opts,
                        smi_querier=active_smi,
                        enforce_setup=True,
                        force_recompute=True,
                    )
                else:
                    orch = DualT4Orchestrator(
                        workspace_dir=run_proj_dir,
                        chapters=target_chapters,
                        render_workers=prof.nvenc_workers_per_gpu,
                        compile_feature=compile_feature,
                        profile=prof,
                        encoder="libx264" if (dry_run and encoder == "auto") else encoder,
                        autotune=False,
                        work_dir=run_work_dir,
                        receipts_dir=run_receipts_dir,
                        worker_pool_factory=worker_pool_factory,
                        worker_options=worker_opts,
                        smi_querier=active_smi,
                        enforce_setup=True,
                        force_recompute=True,
                    )

                t_compute0 = time.monotonic()
                tel = orch.run_pipeline()
                e2e_wall = round(max(0.001, time.monotonic() - t_compute0), 4)

                media_counts = _verify_variant_outputs_and_isolation(
                    profile_id=profile_id,
                    workspace_dir=run_proj_dir,
                    receipts_dir=run_receipts_dir,
                    chapters=target_chapters,
                    dry_run=dry_run,
                )

                tel_dict = tel.to_dict() if hasattr(tel, "to_dict") else dataclasses.asdict(tel)
                stage = dict(tel_dict.get("stage_breakdown") or {})
                model_init_sec = float(stage.get("model_init_sec", 0.0))
                tts_infer_sec = float(stage.get("tts_infer_sec", 0.0))
                audio_norm_sec = float(stage.get("audio_norm_sec", 0.0))
                render_sec = float(stage.get("render_sec", 0.0))
                mux_sec = float(stage.get("mux_sec", 0.0))
                validation_sec = float(stage.get("validation_sec", 0.0))

                total_audio_dur = float(media_counts["total_audio_duration_sec"])
                total_segs = int(media_counts["total_segments"])
                total_frames = int(media_counts["total_video_frames"])
                total_video_dur = float(media_counts["total_video_duration_sec"])

                tts_active = tts_infer_sec + model_init_sec
                tts_rtf = round(tts_infer_sec / total_audio_dur, 4) if (total_audio_dur > 0 and tts_infer_sec > 0) else 0.0
                segments_per_sec = round(total_segs / max(0.001, tts_active), 3) if tts_active > 0 else 0.0
                render_fps = round(total_frames / max(0.001, render_sec), 2) if render_sec > 0 else 0.0
                video_min_per_hr = round(
                    (total_video_dur / 60.0) / (e2e_wall / 3600.0),
                    2,
                )

                gpu_util_avg = dict(tel_dict.get("gpu_util_avg") or {})
                gpu0_info = tel_dict.get("gpu_0") or {}
                gpu1_info = tel_dict.get("gpu_1") or {}

                run_entry = {
                    "profile_id": profile_id.value,
                    "profile_name": prof.name,
                    "description": prof.description,
                    "repeat": rep_idx,
                    "order_in_repeat": order_idx,
                    "chapters": target_chapters,
                    "encoder": orch.encoder,
                    "gpu_ids": list(prof.gpu_ids),
                    "e2e_compute_wall_sec": e2e_wall,
                    "tts_rtf": tts_rtf,
                    "segments_per_sec": segments_per_sec,
                    "render_fps": render_fps,
                    "video_minutes_per_hour": video_min_per_hr,
                    "gpu0_util_avg": float(gpu_util_avg.get("0", 0.0)),
                    "gpu1_util_avg": float(gpu_util_avg.get("1", 0.0)),
                    "gpu0_peak_vram_mb": float(gpu0_info.get("peak_vram_mb", 0.0)),
                    "gpu1_peak_vram_mb": float(gpu1_info.get("peak_vram_mb", 0.0)),
                    "cpu_util_avg": float(tel_dict.get("cpu_util_avg", 0.0)),
                    "disk_throughput_mb_s": float(tel_dict.get("disk_throughput_mb_s", 0.0)),
                    "stage_breakdown": {
                        "model_init_sec": round(model_init_sec, 4),
                        "tts_infer_sec": round(tts_infer_sec, 4),
                        "audio_norm_sec": round(audio_norm_sec, 4),
                        "render_sec": round(render_sec, 4),
                        "mux_sec": round(mux_sec, 4),
                        "validation_sec": round(validation_sec, 4),
                    },
                    "all_valid": bool(tel_dict.get("all_valid", False)),
                    "speedup_vs_single_gpu_baseline": 1.0,
                    "media_counts": media_counts,
                    "scheduler_transitions": tel_dict.get("scheduler_transitions", []),
                    "receipts_dir": str(run_receipts_dir.relative_to(out_dir)),
                }
                runs.append(run_entry)

                if not keep_artifacts:
                    shutil.rmtree(run_root, ignore_errors=True)
    finally:
        if prev_data_dir is None:
            os.environ.pop("MANGA_RECAP_DATA_DIR", None)
        else:
            os.environ["MANGA_RECAP_DATA_DIR"] = prev_data_dir
        shutil.rmtree(scratch_root, ignore_errors=True)

    # Aggregate per-variant summary across repeats
    summary_by_variant: Dict[str, Dict[str, Any]] = {}
    b0_mean_wall: Optional[float] = None
    for pid in variants:
        v_runs = [r for r in runs if r["profile_id"] == pid.value]
        walls = [float(r["e2e_compute_wall_sec"]) for r in v_runs]
        mean_wall = round(statistics.mean(walls), 4) if walls else 0.0
        stdev_wall = round(statistics.stdev(walls), 4) if len(walls) > 1 else 0.0
        if pid == BenchmarkProfileId.B0 and mean_wall > 0.0:
            b0_mean_wall = mean_wall

        summary_by_variant[pid.value] = {
            "profile_id": pid.value,
            "profile_name": ARCHITECTURE_PROFILES[pid].name,
            "repeats": len(v_runs),
            "e2e_compute_wall_sec": mean_wall,
            "e2e_compute_wall_min_sec": round(min(walls), 4) if walls else 0.0,
            "e2e_compute_wall_max_sec": round(max(walls), 4) if walls else 0.0,
            "e2e_compute_wall_stdev_sec": stdev_wall,
            "tts_rtf": round(statistics.mean(r["tts_rtf"] for r in v_runs), 4),
            "segments_per_sec": round(statistics.mean(r["segments_per_sec"] for r in v_runs), 3),
            "render_fps": round(statistics.mean(r["render_fps"] for r in v_runs), 2),
            "video_minutes_per_hour": round(statistics.mean(r["video_minutes_per_hour"] for r in v_runs), 2),
            "gpu0_util_avg": round(statistics.mean(r["gpu0_util_avg"] for r in v_runs), 2),
            "gpu1_util_avg": round(statistics.mean(r["gpu1_util_avg"] for r in v_runs), 2),
            "gpu0_peak_vram_mb": round(max(r["gpu0_peak_vram_mb"] for r in v_runs), 2),
            "gpu1_peak_vram_mb": round(max(r["gpu1_peak_vram_mb"] for r in v_runs), 2),
            "cpu_util_avg": round(statistics.mean(r["cpu_util_avg"] for r in v_runs), 2),
            "disk_throughput_mb_s": round(statistics.mean(r["disk_throughput_mb_s"] for r in v_runs), 3),
            "stage_breakdown": {
                k: round(statistics.mean(r["stage_breakdown"][k] for r in v_runs), 4)
                for k in (
                    "model_init_sec",
                    "tts_infer_sec",
                    "audio_norm_sec",
                    "render_sec",
                    "mux_sec",
                    "validation_sec",
                )
            },
            "all_valid": all(bool(r["all_valid"]) for r in v_runs),
        }

    for pid_val, entry in summary_by_variant.items():
        if b0_mean_wall and entry["e2e_compute_wall_sec"] > 0:
            entry["speedup_vs_b0"] = round(b0_mean_wall / entry["e2e_compute_wall_sec"], 3)
        else:
            entry["speedup_vs_b0"] = 1.0
    for r in runs:
        if b0_mean_wall and r["e2e_compute_wall_sec"] > 0:
            r["speedup_vs_single_gpu_baseline"] = round(b0_mean_wall / r["e2e_compute_wall_sec"], 3)

    compute_sec = round(sum(float(r["e2e_compute_wall_sec"]) for r in runs), 4)
    per_run_model_init = round(sum(float(r["stage_breakdown"]["model_init_sec"]) for r in runs), 4)
    phase_timings: Dict[str, float] = {
        "dataset_upload_sec": float((extra_phase_timings or {}).get("dataset_upload_sec", 0.0)),
        "queue_sec": float((extra_phase_timings or {}).get("queue_sec", 0.0)),
        "kernel_boot_sec": round(float(boot_sec), 4),
        "model_load_sec": round(warmup_sec + per_run_model_init, 4),
        "warmup_sec": round(warmup_sec, 4),
        "compute_sec": compute_sec,
        "download_sec": float((extra_phase_timings or {}).get("download_sec", 0.0)),
    }

    winner_candidates = [v for v in summary_by_variant.values() if v["all_valid"] and v["e2e_compute_wall_sec"] > 0]
    winner = min(winner_candidates, key=lambda v: v["e2e_compute_wall_sec"]) if winner_candidates else None

    render_settings = proj_cfg.get("render_settings", {})
    audio_settings = proj_cfg.get("audio_settings", {})

    results: Dict[str, Any] = {
        "schema_version": SERIES_PAYLOAD_SCHEMA_VERSION,
        "generated_at_utc": utc_iso_now(),
        "execution_mode": "local_dry_run" if dry_run else "kaggle_dual_t4",
        "dry_run": bool(dry_run),
        "mock_mode": bool(dry_run),
        "git_commit": _get_git_commit(),
        "project_slug": slug,
        "chapters": target_chapters,
        "variants_requested": [v.value for v in variants],
        "repeats": repeat_count,
        "warmup": bool(warmup),
        "pinned_parameters": {
            "resolution": f"{render_settings.get('target_width')}x{render_settings.get('target_height')}",
            "fps": render_settings.get("fps"),
            "burn_subtitles": render_settings.get("burn_subtitles"),
            "render_pan_mode": render_settings.get("render_pan_mode"),
            "voice": audio_settings.get("default_voice"),
            "compile_feature": bool(compile_feature),
        },
        "hardware": {
            "gpu_count": len(smi_rows),
            "gpus": smi_rows,
        },
        "phase_timings": phase_timings,
        "all_valid": all(bool(r["all_valid"]) for r in runs) and bool(runs),
        "winner_profile_id": winner["profile_id"] if winner else None,
        "summary_by_variant": summary_by_variant,
        "runs": runs,
    }

    atomic_write_json(out_dir / "benchmark_matrix_results.json", results)
    md_text = render_matrix_markdown(results)
    (out_dir / "benchmark_matrix_report.md").write_text(md_text, encoding="utf-8")
    return results


def render_matrix_markdown(results: Dict[str, Any]) -> str:
    """Renders a transparent Markdown summary strictly from verified run measurements."""
    mode_banner = (
        "DRY-RUN / SIMULATED GPU FIXTURE — NOT EMPIRICAL DUAL-T4 HARDWARE BENCHMARK"
        if results.get("dry_run")
        else "VERIFIED HARDWARE BENCHMARK RUN"
    )
    pt = results.get("phase_timings") or {}
    lines = [
        f"# Dual-T4 Benchmark Matrix Report ({mode_banner})",
        "",
        f"- **Generated UTC:** `{results.get('generated_at_utc')}`",
        f"- **Project:** `{results.get('project_slug')}` (chapters `{','.join(results.get('chapters', []))}`)",
        f"- **Commit:** `{results.get('git_commit')}`",
        f"- **Repeats:** `{results.get('repeats')}` | **Warmup:** `{results.get('warmup')}` | **All Valid:** `{results.get('all_valid')}`",
        f"- **Fastest Profile:** `{results.get('winner_profile_id')}`",
        "",
        "## Lifecycle Timings (Excluded from `e2e_compute_wall_sec`)",
        "| Phase | Seconds |",
        "|---|---|",
        f"| `dataset_upload_sec` | {pt.get('dataset_upload_sec', 0.0):.3f} |",
        f"| `queue_sec` | {pt.get('queue_sec', 0.0):.3f} |",
        f"| `kernel_boot_sec` | {pt.get('kernel_boot_sec', 0.0):.3f} |",
        f"| `model_load_sec` (warmup + init) | {pt.get('model_load_sec', 0.0):.3f} |",
        f"| `compute_sec` (all variants x repeats) | {pt.get('compute_sec', 0.0):.3f} |",
        f"| `download_sec` | {pt.get('download_sec', 0.0):.3f} |",
        "",
        "## Variant Comparison (`B0`..`B5`)",
        "| Profile | Wall (s) | Speedup vs B0 | TTS RTF | Segs/s | Render FPS | Vid Min/Hr | GPU0/1 Util (%) | Peak VRAM (MB) | CPU (%) | Disk (MB/s) | Valid |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for pid, row in (results.get("summary_by_variant") or {}).items():
        lines.append(
            f"| `{pid}` ({row.get('profile_name')}) | {row['e2e_compute_wall_sec']:.3f} "
            f"| {row['speedup_vs_b0']:.2f}x | {row['tts_rtf']:.3f} | {row['segments_per_sec']:.2f} "
            f"| {row['render_fps']:.1f} | {row['video_minutes_per_hour']:.1f} "
            f"| {row['gpu0_util_avg']:.1f}% / {row['gpu1_util_avg']:.1f}% "
            f"| {row['gpu0_peak_vram_mb']:.0f} / {row['gpu1_peak_vram_mb']:.0f} "
            f"| {row['cpu_util_avg']:.1f}% | {row['disk_throughput_mb_s']:.2f} "
            f"| `{row['all_valid']}` |"
        )
    lines.append("")
    lines.append("## Stage Breakdown Averages (Seconds)")
    lines.append("| Profile | model_init | tts_infer | audio_norm | render | mux | validation |")
    lines.append("|---|---|---|---|---|---|---|")
    for pid, row in (results.get("summary_by_variant") or {}).items():
        sb = row.get("stage_breakdown") or {}
        lines.append(
            f"| `{pid}` | {sb.get('model_init_sec', 0.0):.3f} | {sb.get('tts_infer_sec', 0.0):.3f} "
            f"| {sb.get('audio_norm_sec', 0.0):.3f} | {sb.get('render_sec', 0.0):.3f} "
            f"| {sb.get('mux_sec', 0.0):.3f} | {sb.get('validation_sec', 0.0):.3f} |"
        )
    lines.append("")
    return "\n".join(lines)


def _write_result_manifest(
    output_dir: Path,
    *,
    project_slug: str,
    chapters: Sequence[str],
    manifest_hash: str,
    results: Dict[str, Any],
) -> Path:
    manifest_path = output_dir / "dual_t4_result_manifest.json"
    files_inventory = {
        p.relative_to(output_dir).as_posix(): fingerprint(p)
        for p in sorted(output_dir.rglob("*"))
        if p.is_file() and p != manifest_path
    }
    receipt = {
        "schema_version": SERIES_PAYLOAD_SCHEMA_VERSION,
        "project_slug": project_slug,
        "chapters": list(chapters),
        "job_type": "benchmark_matrix",
        "payload_manifest_sha256": manifest_hash,
        "all_valid": bool(results.get("all_valid")),
        "winner_profile_id": results.get("winner_profile_id"),
        "files": files_inventory,
    }
    atomic_write_json(manifest_path, receipt)
    return manifest_path


# ==============================================================================
# CLI Entrypoint
# ==============================================================================


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="P9 Dual-T4 Benchmark Matrix Harness (B0..B5)"
    )
    mode_grp = parser.add_mutually_exclusive_group(required=True)
    mode_grp.add_argument(
        "--local-dry-run",
        "--local",
        dest="local_dry_run",
        action="store_true",
        help="Run local smoke benchmark matrix on synthetic tiny fixture or cloned project",
    )
    mode_grp.add_argument(
        "--dispatch",
        action="store_true",
        help="Package pinned series+benchmark dataset and push ONE Kaggle Dual-T4 benchmark kernel",
    )
    mode_grp.add_argument(
        "--in-container",
        action="store_true",
        help="Execute verified benchmark matrix inside Kaggle Dual-T4 container (mock_mode=False)",
    )

    parser.add_argument("--project", default=None, help="Project slug (required for --dispatch)")
    parser.add_argument("--chapters", default=None, help="Comma-separated chapters (e.g. 1,2,3)")
    parser.add_argument("--variants", default=",".join(DEFAULT_VARIANTS), help="Comma-separated profiles (B0..B5)")
    parser.add_argument("--repeats", type=int, default=1, help="Number of benchmark repeats (>=1)")
    parser.add_argument("--warmup", action="store_true", default=False, help="Pre-warm TTS model before timing")
    parser.add_argument("--encoder", default="auto", help="Encoder preference (auto, h264_nvenc, libx264)")
    parser.add_argument("--compile", dest="compile_feature", action="store_true", default=False)
    parser.add_argument("--no-compile", dest="compile_feature", action="store_false")
    parser.add_argument("--output-dir", default="tmp/p9_benchmark_matrix", help="Directory for JSON/Markdown reports")
    parser.add_argument("--keep-artifacts", action="store_true", default=False)

    # Dispatch options
    parser.add_argument("--kernel-slug", default=None)
    parser.add_argument("--no-wait", action="store_true", default=False)
    parser.add_argument("--poll-interval", type=int, default=6)
    parser.add_argument("--timeout-seconds", type=int, default=5400)

    # In-container bootstrap options
    parser.add_argument("--runtime-root", default=None)
    parser.add_argument("--manifest-hash", default=None)
    parser.add_argument("--boot-sec", type=float, default=0.0)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    variants = _parse_variants(args.variants)
    out_dir = Path(args.output_dir).resolve()

    if args.local_dry_run:
        with tempfile.TemporaryDirectory(prefix="p9-dryrun-") as tmp:
            tmp_p = Path(tmp)
            prev_data = os.environ.get("MANGA_RECAP_DATA_DIR")
            try:
                if args.project:
                    src_proj = get_project_dir(args.project)
                    if not src_proj.is_dir():
                        raise FileNotFoundError(f"Project not found: {src_proj}")
                else:
                    src_proj = build_tiny_matrix_fixture(tmp_p)
                chapters = _parse_csv_list(args.chapters) if args.chapters else _discover_project_chapters(src_proj)
                if not chapters:
                    chapters = ["1"]
                encoder = "libx264" if args.encoder == "auto" else args.encoder
                res = run_benchmark_matrix(
                    source_project_dir=src_proj,
                    chapters=chapters,
                    output_dir=out_dir,
                    variants=variants,
                    repeats=args.repeats,
                    warmup=args.warmup,
                    encoder=encoder,
                    compile_feature=args.compile_feature,
                    dry_run=True,
                    keep_artifacts=args.keep_artifacts,
                )
                print(
                    f"[P9 DRY-RUN] Completed {len(res['runs'])} run(s), "
                    f"winner={res['winner_profile_id']}, report={out_dir / 'benchmark_matrix_report.md'}"
                )
                return 0
            finally:
                if prev_data is None:
                    os.environ.pop("MANGA_RECAP_DATA_DIR", None)
                else:
                    os.environ["MANGA_RECAP_DATA_DIR"] = prev_data

    if args.dispatch:
        if not args.project:
            parser.error("--dispatch requires --project <slug>")
        from src.cloud.kaggle_service import KaggleCloudService

        proj_dir = get_project_dir(args.project)
        if not proj_dir.is_dir():
            raise FileNotFoundError(f"Project not found: {proj_dir}")
        chapters = _parse_csv_list(args.chapters) if args.chapters else _discover_project_chapters(proj_dir)
        if not chapters:
            raise ValueError("No chapters available for benchmark dispatch")

        service = KaggleCloudService()
        res = service.dispatch_benchmark_matrix_job(
            args.project,
            chapters,
            variants=tuple(v.value for v in variants),
            repeats=args.repeats,
            warmup=args.warmup,
            kernel_slug=args.kernel_slug,
            output_dir=out_dir,
            wait=not args.no_wait,
            poll_interval=args.poll_interval,
            timeout_seconds=args.timeout_seconds,
            encoder_preference=args.encoder,
            compile_feature=args.compile_feature,
        )
        if args.no_wait:
            atomic_write_json(out_dir / "benchmark_dispatch_receipt.json", res)
            print(f"[P9 DISPATCH] Pushed Kaggle Dual-T4 kernel: {res['kernel_ref']}")
            return 0
        # Refresh both JSON & Markdown reports at out_dir (and nested results/ when present)
        md_text = render_matrix_markdown(res)
        report_targets = [out_dir]
        nested_results = out_dir / "results"
        if nested_results.is_dir():
            report_targets.append(nested_results)
        for target_dir in report_targets:
            atomic_write_json(target_dir / "benchmark_matrix_results.json", res)
            (target_dir / "benchmark_matrix_report.md").write_text(md_text, encoding="utf-8")
        print(
            f"[P9 DISPATCH COMPLETE] kernel={res.get('kernel_ref')}, "
            f"winner={res.get('winner_profile_id')}, all_valid={res.get('all_valid')}"
        )
        return 0

    # --in-container
    if not args.runtime_root or not args.manifest_hash:
        parser.error("--in-container requires --runtime-root and --manifest-hash")
    rt_root = Path(args.runtime_root).resolve()
    manifest = verify_series_payload_manifest(rt_root, args.manifest_hash)
    slug = str(manifest["project_slug"])
    chapters = _parse_csv_list(args.chapters) if args.chapters else list(manifest["chapters"])
    proj_dir = rt_root / "project"
    res = run_benchmark_matrix(
        source_project_dir=proj_dir,
        chapters=chapters,
        output_dir=out_dir,
        variants=variants,
        repeats=args.repeats,
        warmup=args.warmup,
        encoder=args.encoder,
        compile_feature=args.compile_feature,
        dry_run=False,
        boot_sec=float(args.boot_sec),
        keep_artifacts=args.keep_artifacts,
    )
    _write_result_manifest(
        out_dir,
        project_slug=slug,
        chapters=chapters,
        manifest_hash=args.manifest_hash,
        results=res,
    )
    logger.info(
        f"[P9 IN-CONTAINER COMPLETE] project={slug}, winner={res.get('winner_profile_id')}, all_valid={res.get('all_valid')}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
