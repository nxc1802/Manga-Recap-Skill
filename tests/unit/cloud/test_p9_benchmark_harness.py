"""
tests/unit/cloud/test_p9_benchmark_harness.py
Deterministic unit & integration tests for P9 Dual-T4 Benchmark Matrix Harness (`B0`..`B5`).
"""

from __future__ import annotations

import json
import shutil
import sys
import tarfile
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from scripts.benchmark.run_dual_t4_matrix import (
    _dry_run_smi_querier,
    _dry_run_tts_worker_factory,
    _prepare_clean_variant_workspace,
    main as matrix_main,
    run_benchmark_matrix,
)
from src.cloud.cuda_worker_pool import CudaWorkerPool, compute_usable_vram_budget
from src.cloud.dual_t4_orchestrator import DualT4Orchestrator
from src.cloud.elastic_scheduler import BenchmarkProfileId
from src.cloud.kaggle_service import KaggleCloudService
from src.cloud.series_payload import (
    generate_benchmark_worker_script,
    package_series_payload,
    verify_series_payload_manifest,
)
from src.render.artifact_validation import fingerprint
from tests.unit.cloud.test_p8_dual_t4_architecture import (
    _RealMediaFakeGpuWorkerHandle,
    _create_multi_chapter_series,
)


def _install_multi_chapter_p9_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    approve_ch1: bool = True,
) -> Path:
    return _create_multi_chapter_series(
        tmp_path,
        monkeypatch,
        chapters=["1", "2", "3"],
        approve_ch1=approve_ch1,
    )


def _gpu_smi_dual_t4() -> List[Dict[str, Any]]:
    return [
        {
            "gpu_id": 0,
            "gpu_uuid": "GPU-P9-T4-0000",
            "name": "Tesla T4",
            "vram_total_mb": 15360.0,
            "vram_used_mb": 1100.0,
            "vram_free_mb": 14260.0,
            "usable_vram_budget_mb": compute_usable_vram_budget(15360.0),
            "gpu_util_pct": 62.0,
            "encoder_util_pct": 15.0,
            "temperature_c": 48.0,
        },
        {
            "gpu_id": 1,
            "gpu_uuid": "GPU-P9-T4-0001",
            "name": "Tesla T4",
            "vram_total_mb": 15360.0,
            "vram_used_mb": 900.0,
            "vram_free_mb": 14460.0,
            "usable_vram_budget_mb": compute_usable_vram_budget(15360.0),
            "gpu_util_pct": 74.0,
            "encoder_util_pct": 35.0,
            "temperature_c": 51.0,
        },
    ]


def test_p9_benchmark_variants_b0_to_b5_isolation_and_metrics(tmp_path, monkeypatch):
    """
    Verifies B0..B5 run back-to-back on identical fresh workspace clones,
    preserving strict B0 (GPU0-only sequential) and B1 (GPU0 TTS -> GPU1 Render)
    isolation while emitting all required primary, secondary, stage, and validity metrics.
    """
    proj_dir = _install_multi_chapter_p9_fixture(tmp_path, monkeypatch, approve_ch1=True)
    out_dir = tmp_path / "p9_out"

    results = run_benchmark_matrix(
        source_project_dir=proj_dir,
        chapters=["1", "2", "3"],
        output_dir=out_dir,
        variants=[
            BenchmarkProfileId.B0,
            BenchmarkProfileId.B1,
            BenchmarkProfileId.B2,
            BenchmarkProfileId.B3,
            BenchmarkProfileId.B4,
            BenchmarkProfileId.B5,
        ],
        repeats=1,
        warmup=True,
        encoder="libx264",
        compile_feature=False,
        dry_run=True,
        smi_querier=_gpu_smi_dual_t4,
    )

    assert results["all_valid"] is True
    assert results["dry_run"] is True
    assert results["mock_mode"] is True
    assert len(results["runs"]) == 6
    assert (out_dir / "benchmark_matrix_results.json").is_file()
    assert (out_dir / "benchmark_matrix_report.md").is_file()

    by_id = {r["profile_id"]: r for r in results["runs"]}
    assert set(by_id.keys()) == {"B0", "B1", "B2", "B3", "B4", "B5"}

    # B0 single-T4 isolation: GPU1 never used
    b0 = by_id["B0"]
    assert b0["gpu_ids"] == [0]
    assert b0["gpu1_util_avg"] == 0.0
    assert b0["gpu1_peak_vram_mb"] == 0.0
    assert b0["media_counts"]["tts_gpus_seen"] == [0]
    assert b0["media_counts"]["render_gpus_seen"] == [0]

    # B1 dedicated GPU0 TTS -> GPU1 Render
    b1 = by_id["B1"]
    assert b1["media_counts"]["tts_gpus_seen"] == [0]
    assert b1["media_counts"]["render_gpus_seen"] == [1]

    # Verify all required metrics & stage breakdowns exist on every variant
    expected_stage_keys = {
        "model_init_sec",
        "tts_infer_sec",
        "audio_norm_sec",
        "render_sec",
        "mux_sec",
        "validation_sec",
    }
    for pid, run in by_id.items():
        assert run["e2e_compute_wall_sec"] > 0.0, pid
        assert run["tts_rtf"] >= 0.0
        assert run["segments_per_sec"] > 0.0
        assert run["render_fps"] > 0.0
        assert run["video_minutes_per_hour"] > 0.0
        assert run["gpu0_util_avg"] > 0.0
        assert run["gpu0_peak_vram_mb"] > 0.0
        assert run["cpu_util_avg"] >= 0.0
        assert run["disk_throughput_mb_s"] > 0.0
        assert set(run["stage_breakdown"].keys()) == expected_stage_keys
        assert run["all_valid"] is True


def _delayed_init_tts_worker_factory(*, gpu_id: int = 0, **kwargs: Any) -> Any:
    """Top-level spawn-picklable TTS worker simulating cold model init + inference."""
    import time

    time.sleep(0.03)
    base = _dry_run_tts_worker_factory(gpu_id=gpu_id, **kwargs)
    orig_synth = base.synthesize_batch_tasks

    def _synth(tasks, **kw):
        time.sleep(0.02)
        return orig_synth(tasks, **kw)

    base.synthesize_batch_tasks = _synth
    return base


def test_p9_separates_lifecycle_timings_from_compute_and_aggregates_repeats(tmp_path, monkeypatch):
    """
    Verifies Kaggle upload/queue/boot/warmup/download times are tracked separately
    and NEVER inflate `e2e_compute_wall_sec`, while repeats aggregate mean/min/max/stdev
    and multi-replica TTS (`B4`) preserves non-zero `model_init_sec` and `tts_infer_sec`.
    """
    proj_dir = _install_multi_chapter_p9_fixture(tmp_path, monkeypatch, approve_ch1=True)
    out_dir = tmp_path / "p9_repeats"

    def _pool_factory(**kw):
        opts = dict(kw.pop("worker_options", {}) or {})
        opts["tts_worker_factory"] = _delayed_init_tts_worker_factory
        return CudaWorkerPool(**kw, worker_options=opts)

    results = run_benchmark_matrix(
        source_project_dir=proj_dir,
        chapters=["1", "2", "3"],
        output_dir=out_dir,
        variants=[BenchmarkProfileId.B0, BenchmarkProfileId.B4],
        repeats=2,
        warmup=True,
        encoder="libx264",
        dry_run=True,
        boot_sec=14.25,
        extra_phase_timings={
            "dataset_upload_sec": 5.5,
            "queue_sec": 9.75,
            "download_sec": 3.1,
        },
        smi_querier=_gpu_smi_dual_t4,
        worker_pool_factory=_pool_factory,
    )

    assert len(results["runs"]) == 4  # 2 variants * 2 repeats
    pt = results["phase_timings"]
    assert pt["dataset_upload_sec"] == 5.5
    assert pt["queue_sec"] == 9.75
    assert pt["kernel_boot_sec"] == 14.25
    assert pt["download_sec"] == 3.1
    assert pt["warmup_sec"] > 0.0

    expected_compute = round(sum(r["e2e_compute_wall_sec"] for r in results["runs"]), 4)
    assert pt["compute_sec"] == pytest.approx(expected_compute, abs=1e-3)
    # No variant compute wall can silently absorb the 14.25s boot or 5.5s upload!
    for r in results["runs"]:
        assert r["e2e_compute_wall_sec"] < 10.0

    summary = results["summary_by_variant"]
    assert summary["B0"]["repeats"] == 2
    assert summary["B4"]["repeats"] == 2
    assert summary["B4"]["stage_breakdown"]["model_init_sec"] > 0.0
    assert summary["B4"]["stage_breakdown"]["tts_infer_sec"] > 0.0
    assert summary["B0"]["e2e_compute_wall_min_sec"] <= summary["B0"]["e2e_compute_wall_sec"]
    assert summary["B0"]["e2e_compute_wall_sec"] <= summary["B0"]["e2e_compute_wall_max_sec"]


def test_p9_refuses_invalid_artifacts_and_missing_dual_gpus_fail_closed(tmp_path, monkeypatch):
    """
    Zero fake benchmark numbers: fails immediately if `<2` distinct GPUs in real mode,
    if `validate_final_artifact` fails on any chapter, or if G6 approval is missing on dispatch.
    """
    proj_dir = _install_multi_chapter_p9_fixture(tmp_path, monkeypatch, approve_ch1=True)

    # 1. Real non-dry-run mode must fail if nvidia-smi reports only 1 GPU
    with pytest.raises(RuntimeError, match=">= 2 distinct physical GPUs"):
        run_benchmark_matrix(
            source_project_dir=proj_dir,
            chapters=["1", "2", "3"],
            output_dir=tmp_path / "fail_1gpu",
            variants=[BenchmarkProfileId.B3],
            dry_run=False,
            smi_querier=lambda: [_gpu_smi_dual_t4()[0]],
        )

    # 2. Failed artifact validation must fail closed and refuse summary completion
    monkeypatch.setattr(
        "src.cloud.dual_t4_orchestrator.validate_final_artifact",
        lambda *args, **kwargs: {
            "valid": False,
            "error": "corrupt stream",
            "video": {"frames": 0, "duration": 0.0},
        },
    )
    with pytest.raises(RuntimeError, match="failed validate_final_artifact|all_valid"):
        run_benchmark_matrix(
            source_project_dir=proj_dir,
            chapters=["1", "2", "3"],
            output_dir=tmp_path / "fail_art",
            variants=[BenchmarkProfileId.B0],
            dry_run=True,
            smi_querier=_gpu_smi_dual_t4,
        )

    # 3. Dispatch must fail closed when Chapter 1 G6 preview approval is absent
    proj_unapproved = _create_multi_chapter_series(tmp_path / "unapp", monkeypatch, approve_ch1=False)
    svc = KaggleCloudService()
    svc._api = object()
    svc._username = "tester"
    with pytest.raises(ValueError, match="preview|G6|Human preview"):
        svc.dispatch_benchmark_matrix_job(
            "p8_series",
            ["1", "2", "3"],
        )


def test_p9_dispatch_packages_one_session_verifies_sha256_and_separates_queue_upload_download(
    tmp_path, monkeypatch
):
    """
    Verifies single-session packaging/verification (`scripts/benchmark/run_dual_t4_matrix.py`
    included & SHA-256 pinned), stdlib-only bootstrap ordering, and mocked Kaggle dispatch
    recording `dataset_upload_sec`, `queue_sec`, and `download_sec` separately.
    """
    proj_dir = _install_multi_chapter_p9_fixture(tmp_path, monkeypatch, approve_ch1=True)

    tar_path = tmp_path / "bench_payload.tar.gz"
    out_tar, digest = package_series_payload(
        proj_dir,
        ["1", "2", "3"],
        tar_path,
        job_type="benchmark_matrix",
        include_benchmark_harness=True,
    )
    assert out_tar.is_file()
    extracted = tmp_path / "extracted_rt"
    with tarfile.open(out_tar) as tf:
        tf.extractall(extracted, filter="data")
    manifest = verify_series_payload_manifest(extracted, digest)
    assert manifest["job_type"] == "benchmark_matrix"
    assert "scripts/benchmark/run_dual_t4_matrix.py" in manifest["files"]

    worker_code = generate_benchmark_worker_script(
        "p8_series",
        ["1", "2", "3"],
        payload_manifest_hash=digest,
        project_dir=proj_dir,
        variants=("B0", "B3", "B5"),
        repeats=2,
        warmup=True,
    )
    assert worker_code.index("Payload hash mismatch") < worker_code.index("pip")
    assert "'--in-container'" in worker_code
    assert "'--warmup'" in worker_code
    assert "'B0,B3,B5'" in worker_code
    assert "'--boot-sec'" in worker_code

    # End-to-end mocked dispatch
    created_datasets: List[str] = []
    pushed_kernels: List[Dict[str, Any]] = []
    status_calls = {"count": 0}

    class _FakeKaggleApi:
        def dataset_create_new(self, folder, public=False, quiet=True):
            created_datasets.append(folder)

        def dataset_status(self, ds_ref):
            return SimpleNamespace(status="ready")

        def dataset_list_files(self, ds_ref):
            return SimpleNamespace(dataset_files=[SimpleNamespace(name="payload.tar.gz")])

        def kernels_push(self, folder):
            meta = json.loads((Path(folder) / "kernel-metadata.json").read_text(encoding="utf-8"))
            pushed_kernels.append(meta)
            return SimpleNamespace(ref="/code/tester/p9-bench-kernel")

        def kernels_status(self, ref):
            status_calls["count"] += 1
            if status_calls["count"] == 1:
                return SimpleNamespace(status="QUEUED")
            if status_calls["count"] == 2:
                return SimpleNamespace(status="RUNNING")
            return SimpleNamespace(status="COMPLETE")

        def kernels_output(self, ref, target_dir):
            out_res = Path(target_dir) / "results"
            out_res.mkdir(parents=True, exist_ok=True)
            # Extract real manifest hash pushed to staging kernel
            payload_sha = svc._series_payload_manifest_hash
            bench_payload = {
                "schema_version": "4.2.0",
                "project_slug": "p8_series",
                "chapters": ["1", "2", "3"],
                "all_valid": True,
                "winner_profile_id": "B5",
                "phase_timings": {
                    "kernel_boot_sec": 21.4,
                    "model_load_sec": 18.2,
                    "compute_sec": 100.0,
                },
                "runs": [
                    {
                        "profile_id": "B5",
                        "e2e_compute_wall_sec": 100.0,
                        "all_valid": True,
                    }
                ],
                "summary_by_variant": {},
            }
            bj = out_res / "benchmark_matrix_results.json"
            bj.write_text(json.dumps(bench_payload), encoding="utf-8")
            md = out_res / "benchmark_matrix_report.md"
            md.write_text("# Report\n", encoding="utf-8")
            rm = out_res / "dual_t4_result_manifest.json"
            rm.write_text(
                json.dumps(
                    {
                        "schema_version": "4.2.0",
                        "project_slug": "p8_series",
                        "chapters": ["1", "2", "3"],
                        "job_type": "benchmark_matrix",
                        "payload_manifest_sha256": payload_sha,
                        "files": {
                            "benchmark_matrix_results.json": fingerprint(bj),
                            "benchmark_matrix_report.md": fingerprint(md),
                        },
                    }
                ),
                encoding="utf-8",
            )

    svc = KaggleCloudService()
    svc._api = _FakeKaggleApi()
    svc._username = "tester"

    dispatch_out = tmp_path / "dispatch_dl"
    res = svc.dispatch_benchmark_matrix_job(
        "p8_series",
        ["1", "2", "3"],
        variants=("B0", "B3", "B5"),
        repeats=2,
        warmup=True,
        output_dir=dispatch_out,
        poll_interval=0,
        dataset_poll_interval=0.0,
    )
    assert len(created_datasets) == 1
    assert len(pushed_kernels) == 1
    assert pushed_kernels[0]["machine_shape"] == "NvidiaTeslaT4"
    assert res["all_valid"] is True
    assert res["phase_timings"]["kernel_boot_sec"] == 21.4
    assert res["phase_timings"]["dataset_upload_sec"] >= 0.0
    assert res["phase_timings"]["queue_sec"] >= 0.0
    assert res["phase_timings"]["download_sec"] >= 0.0


def test_p9_cli_local_dry_run_end_to_end(tmp_path):
    """Exercises `run_dual_t4_matrix.py --local-dry-run` CLI directly."""
    out_dir = tmp_path / "cli_dry_out"
    rc = matrix_main(
        [
            "--local-dry-run",
            "--variants",
            "B0,B3,B5",
            "--repeats",
            "1",
            "--output-dir",
            str(out_dir),
        ]
    )
    assert rc == 0
    doc = json.loads((out_dir / "benchmark_matrix_results.json").read_text(encoding="utf-8"))
    assert doc["dry_run"] is True
    assert doc["all_valid"] is True
    assert set(doc["summary_by_variant"].keys()) == {"B0", "B3", "B5"}
    assert (out_dir / "benchmark_matrix_report.md").is_file()


def test_p9_cli_in_container_verifies_manifest_and_fail_closes(tmp_path, monkeypatch):
    """Verifies `--in-container` fails closed on tampered manifest hash & non-GPU host."""
    proj_dir = _install_multi_chapter_p9_fixture(tmp_path, monkeypatch, approve_ch1=True)
    tar_path = tmp_path / "bench_ic.tar.gz"
    out_tar, digest = package_series_payload(
        proj_dir,
        ["1", "2", "3"],
        tar_path,
        job_type="benchmark_matrix",
        include_benchmark_harness=True,
    )
    extracted = tmp_path / "ic_runtime"
    with tarfile.open(out_tar) as tf:
        tf.extractall(extracted, filter="data")

    # 1. Wrong manifest hash must fail before running benchmark
    with pytest.raises(ValueError, match="hash mismatch"):
        matrix_main(
            [
                "--in-container",
                "--runtime-root",
                str(extracted),
                "--manifest-hash",
                "0" * 64,
                "--output-dir",
                str(tmp_path / "ic_out_bad"),
            ]
        )

    # 2. Real manifest on CPU-only host / single GPU must refuse Dual-T4 execution
    monkeypatch.setattr("scripts.benchmark.run_dual_t4_matrix.query_nvidia_smi", lambda: [])
    with pytest.raises(RuntimeError, match="requires real NVIDIA GPU"):
        matrix_main(
            [
                "--in-container",
                "--runtime-root",
                str(extracted),
                "--manifest-hash",
                digest,
                "--output-dir",
                str(tmp_path / "ic_out_nogpu"),
            ]
        )


def test_p9_cli_dispatch_syncs_nested_results_and_root_outputs(tmp_path, monkeypatch):
    """
    Regression test (#98-2): when Kaggle `kernels_output` downloads into
    `<out_dir>/results/`, `--dispatch` must populate refreshed
    `benchmark_matrix_results.json` and `benchmark_matrix_report.md` in BOTH
    `out_dir` and `out_dir/results`.
    """
    _install_multi_chapter_p9_fixture(tmp_path, monkeypatch, approve_ch1=True)

    class _FakeCliKaggleApi:
        def __init__(self, svc: KaggleCloudService):
            self._svc = svc
            self._status_calls = 0

        def dataset_create_new(self, folder, public=False, quiet=True):
            return None

        def dataset_status(self, ds_ref):
            return SimpleNamespace(status="ready")

        def dataset_list_files(self, ds_ref):
            return SimpleNamespace(dataset_files=[SimpleNamespace(name="payload.tar.gz")])

        def kernels_push(self, folder):
            return SimpleNamespace(ref="/code/tester/p9-cli-dispatch")

        def kernels_status(self, ref):
            self._status_calls += 1
            if self._status_calls == 1:
                return SimpleNamespace(status="QUEUED")
            return SimpleNamespace(status="COMPLETE")

        def kernels_output(self, ref, target_dir):
            out_res = Path(target_dir) / "results"
            out_res.mkdir(parents=True, exist_ok=True)
            payload_sha = self._svc._series_payload_manifest_hash
            bench_payload = {
                "schema_version": "4.2.0",
                "generated_at_utc": "2026-11-12T00:00:00Z",
                "dry_run": False,
                "mock_mode": False,
                "git_commit": "deadbeef",
                "project_slug": "p8_series",
                "chapters": ["1", "2", "3"],
                "repeats": 1,
                "warmup": False,
                "all_valid": True,
                "winner_profile_id": "B5",
                "phase_timings": {
                    "kernel_boot_sec": 12.3,
                    "model_load_sec": 8.4,
                    "warmup_sec": 0.0,
                    "compute_sec": 42.0,
                },
                "summary_by_variant": {
                    "B0": {
                        "profile_id": "B0",
                        "profile_name": "1-T4 Sequential Baseline",
                        "e2e_compute_wall_sec": 30.0,
                        "speedup_vs_b0": 1.0,
                        "tts_rtf": 0.4,
                        "segments_per_sec": 2.5,
                        "render_fps": 30.0,
                        "video_minutes_per_hour": 60.0,
                        "gpu0_util_avg": 75.0,
                        "gpu1_util_avg": 0.0,
                        "gpu0_peak_vram_mb": 4200.0,
                        "gpu1_peak_vram_mb": 0.0,
                        "cpu_util_avg": 25.0,
                        "disk_throughput_mb_s": 12.0,
                        "stage_breakdown": {},
                        "all_valid": True,
                    },
                    "B5": {
                        "profile_id": "B5",
                        "profile_name": "Elastic Dual-T4 + Coexistence",
                        "e2e_compute_wall_sec": 12.0,
                        "speedup_vs_b0": 2.5,
                        "tts_rtf": 0.2,
                        "segments_per_sec": 5.0,
                        "render_fps": 60.0,
                        "video_minutes_per_hour": 140.0,
                        "gpu0_util_avg": 82.0,
                        "gpu1_util_avg": 80.0,
                        "gpu0_peak_vram_mb": 5200.0,
                        "gpu1_peak_vram_mb": 3400.0,
                        "cpu_util_avg": 30.0,
                        "disk_throughput_mb_s": 18.0,
                        "stage_breakdown": {},
                        "all_valid": True,
                    },
                },
                "runs": [
                    {"profile_id": "B0", "e2e_compute_wall_sec": 30.0, "all_valid": True},
                    {"profile_id": "B5", "e2e_compute_wall_sec": 12.0, "all_valid": True},
                ],
            }
            bj = out_res / "benchmark_matrix_results.json"
            bj.write_text(json.dumps(bench_payload), encoding="utf-8")
            md = out_res / "benchmark_matrix_report.md"
            md.write_text("STALE REMOTE REPORT\n", encoding="utf-8")
            rm = out_res / "dual_t4_result_manifest.json"
            rm.write_text(
                json.dumps(
                    {
                        "schema_version": "4.2.0",
                        "project_slug": "p8_series",
                        "chapters": ["1", "2", "3"],
                        "job_type": "benchmark_matrix",
                        "payload_manifest_sha256": payload_sha,
                        "files": {
                            "benchmark_matrix_results.json": fingerprint(bj),
                            "benchmark_matrix_report.md": fingerprint(md),
                        },
                    }
                ),
                encoding="utf-8",
            )

    def _fake_auth(self):
        self._api = _FakeCliKaggleApi(self)
        self._username = "tester"

    monkeypatch.setattr(KaggleCloudService, "_authenticate", _fake_auth)

    out_dir = tmp_path / "cli_dispatch_out"
    rc = matrix_main(
        [
            "--dispatch",
            "--project",
            "p8_series",
            "--chapters",
            "1,2,3",
            "--variants",
            "B0,B5",
            "--repeats",
            "1",
            "--output-dir",
            str(out_dir),
            "--poll-interval",
            "0",
        ]
    )
    assert rc == 0

    root_json = out_dir / "benchmark_matrix_results.json"
    nested_json = out_dir / "results" / "benchmark_matrix_results.json"
    root_md = out_dir / "benchmark_matrix_report.md"
    nested_md = out_dir / "results" / "benchmark_matrix_report.md"

    assert root_json.is_file()
    assert nested_json.is_file()
    assert root_md.is_file()
    assert nested_md.is_file()

    root_doc = json.loads(root_json.read_text(encoding="utf-8"))
    nested_doc = json.loads(nested_json.read_text(encoding="utf-8"))
    assert root_doc == nested_doc
    assert "dataset_upload_sec" in root_doc["phase_timings"]
    assert "queue_sec" in root_doc["phase_timings"]
    assert "download_sec" in root_doc["phase_timings"]
    assert root_doc["phase_timings"]["kernel_boot_sec"] == 12.3

    root_md_txt = root_md.read_text(encoding="utf-8")
    nested_md_txt = nested_md.read_text(encoding="utf-8")
    assert root_md_txt == nested_md_txt
    assert "STALE REMOTE REPORT" not in nested_md_txt
    assert "`dataset_upload_sec`" in root_md_txt
    assert "`queue_sec`" in root_md_txt
    assert "`download_sec`" in root_md_txt


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required for render eligibility regression test")
def test_p9_orchestrator_defers_ineligible_render_stealer_without_reenqueue(tmp_path, monkeypatch):
    """
    Regression test (#98-1):
    1. In B3 (`BALANCED` with TTS active on GPU 0), GPU 0 render stealer evaluates
       eligibility BEFORE `render_queue.get()`, never popping and re-enqueuing items
       behind `None` sentinels.
    2. In B1 (dedicated GPU 1 render), `_render_dispatcher` emits exact sentinel count
       matching `len(render_stealer_gpus)` so zero unconsumed `None` sentinels remain.
    """
    proj_dir = _install_multi_chapter_p9_fixture(tmp_path, monkeypatch, approve_ch1=True)

    # 1. B3 race window: Chapter 3 TTS holds gate_event until Chapter 2 renders on GPU 1
    b3_proj = tmp_path / "b3_proj"
    _prepare_clean_variant_workspace(proj_dir, b3_proj, ["1", "2", "3"])
    timeline: List[Any] = []
    ch2_render_started_event = threading.Event()
    tts_ch3_gate_lock = threading.Lock()
    gate_state = {"ch3_waited": False}
    orch_b3 = DualT4Orchestrator(
        workspace_dir=b3_proj,
        chapters=["1", "2", "3"],
        profile=BenchmarkProfileId.B3,
        encoder="libx264",
        compile_feature=False,
        force_recompute=True,
        receipts_dir=tmp_path / "b3_receipts",
        work_dir=tmp_path / "b3_work",
        smi_querier=_gpu_smi_dual_t4,
        worker_pool_factory=lambda **kw: CudaWorkerPool(
            **kw,
            worker_options={
                "timeline": timeline,
                "ch2_render_started_event": ch2_render_started_event,
                "tts_ch3_gate_lock": tts_ch3_gate_lock,
                "gate_state": gate_state,
            },
            worker_handle_factory=_RealMediaFakeGpuWorkerHandle,
        ),
    )

    put_log_lock = threading.Lock()
    put_log_b3: List[tuple[str, Any]] = []
    orig_put_b3 = orch_b3.queues.render_queue.put

    def _spy_put_b3(item, *args, **kwargs):
        with put_log_lock:
            put_log_b3.append((threading.current_thread().name, item))
        return orig_put_b3(item, *args, **kwargs)

    monkeypatch.setattr(orch_b3.queues.render_queue, "put", _spy_put_b3)
    tel_b3 = orch_b3.run_pipeline()

    assert tel_b3.all_valid is True
    assert orch_b3.queues.render_queue.empty() is True
    assert orch_b3.queues.render_queue.unfinished_tasks == 0
    assert not any(tname.startswith("P8-Stealer") for tname, _ in put_log_b3)
    sentinel_indices_b3 = [idx for idx, (_, it) in enumerate(put_log_b3) if it is None]
    shot_indices_b3 = [idx for idx, (_, it) in enumerate(put_log_b3) if it is not None]
    assert len(sentinel_indices_b3) == 4  # 2 GPUs * 2 nvenc workers
    assert shot_indices_b3 and max(shot_indices_b3) < min(sentinel_indices_b3)
    ch3_tts_starts = [entry[3] for entry in timeline if entry[0] == "tts_start" and entry[1] == "3"]
    ch3_tts_ends = [entry[3] for entry in timeline if entry[0] == "tts_end" and entry[1] == "3"]
    assert ch3_tts_starts and ch3_tts_ends
    renders_during_ch3_tts = [
        entry
        for entry in timeline
        if entry[0] == "render_start" and min(ch3_tts_starts) <= entry[3] < min(ch3_tts_ends)
    ]
    assert renders_during_ch3_tts, "Expected streaming render overlap while Ch.3 TTS was active"
    assert {entry[2] for entry in renders_during_ch3_tts} == {1}

    # 2. B1 sentinel count matches len(render_stealer_gpus) == effective_nvenc (2, not 4)
    b1_proj = tmp_path / "b1_proj"
    _prepare_clean_variant_workspace(proj_dir, b1_proj, ["1", "2", "3"])
    orch_b1 = DualT4Orchestrator(
        workspace_dir=b1_proj,
        chapters=["1", "2", "3"],
        profile=BenchmarkProfileId.B1,
        encoder="libx264",
        compile_feature=False,
        force_recompute=True,
        receipts_dir=tmp_path / "b1_receipts",
        work_dir=tmp_path / "b1_work",
        smi_querier=_gpu_smi_dual_t4,
        worker_pool_factory=lambda **kw: CudaWorkerPool(
            **kw,
            worker_handle_factory=_RealMediaFakeGpuWorkerHandle,
        ),
    )

    put_log_b1: List[tuple[str, Any]] = []
    orig_put_b1 = orch_b1.queues.render_queue.put

    def _spy_put_b1(item, *args, **kwargs):
        with put_log_lock:
            put_log_b1.append((threading.current_thread().name, item))
        return orig_put_b1(item, *args, **kwargs)

    monkeypatch.setattr(orch_b1.queues.render_queue, "put", _spy_put_b1)
    tel_b1 = orch_b1.run_pipeline()

    assert tel_b1.all_valid is True
    assert orch_b1.queues.render_queue.empty() is True
    assert orch_b1.queues.render_queue.unfinished_tasks == 0
    assert not any(tname.startswith("P8-Stealer") for tname, _ in put_log_b1)
    sentinel_indices_b1 = [idx for idx, (_, it) in enumerate(put_log_b1) if it is None]
    assert len(sentinel_indices_b1) == 2
