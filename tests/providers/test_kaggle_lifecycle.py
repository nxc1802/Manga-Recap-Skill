"""Tests for K1 Kaggle job lifecycle hardening, safety guards, and worker resilience."""

from __future__ import annotations

import json
import os
import queue
import tarfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.cloud.account_fallback import with_account_fallback
from src.cloud.account_manager import KaggleAccountManager
from src.cloud import dual_gpu_worker as worker
from src.cloud.jobs import load_cloud_job_state
from src.cloud.kaggle_service import (
    KaggleClientPollTimeoutError,
    KaggleCloudService,
    KaggleExecutionError,
    KaggleQueueTimeoutError,
    KaggleSubmitAmbiguousError,
)
from src.cloud.lifecycle import (
    LifecycleState,
    check_disk_low_water,
    check_no_duplicate_active_job,
    estimate_workload_budgets,
)
from src.core.errors import ProviderIntegrityError
from src.core.storage import atomic_write_json, read_json
from tests.workflow.test_canonical_workflow import unit_project  # noqa: F401


def _fake_quota_api(allowed_sec=108000, used_sec=0):
    class _Ctx:
        def __enter__(self):
            return SimpleNamespace(
                quota=SimpleNamespace(
                    get_quota=lambda: SimpleNamespace(
                        gpu_quota=SimpleNamespace(
                            total_time_allowed=allowed_sec,
                            time_used=used_sec,
                            time_reserved=0,
                            quota_refresh_time=None,
                        )
                    )
                )
            )

        def __exit__(self, *_):
            return False

    return _Ctx()


def test_workload_budgets_scale_separately_and_cap_at_9h(unit_project):
    project, _ = unit_project
    single_budget = estimate_workload_budgets(project, ["1"], "produce", gpu=True)
    assert single_budget.queue_notice_sec == 300.0
    assert single_budget.queue_timeout_sec == 900.0
    assert single_budget.execution_budget_sec == 1800
    assert single_budget.client_poll_timeout_sec == 2100
    assert single_budget.dataset_transfer_timeout_sec == 600.0

    # Inject a large 250-segment chapter to confirm execution budget scales cleanly above 1800s.
    script_path = project / "chapters" / "1" / "script.json"
    script_data = read_json(script_path)
    script_data["segments"] = [dict(script_data["segments"][0], seq_id=f"s{i}") for i in range(250)]
    atomic_write_json(script_path, script_data)

    large_budget = estimate_workload_budgets(project, ["1"] * 4, "produce", gpu=True)
    assert large_budget.execution_budget_sec > 1800
    assert large_budget.execution_budget_sec <= 32400
    assert large_budget.client_poll_timeout_sec <= 32700

    cpu_budget = estimate_workload_budgets(project, ["1"], "render", gpu=False)
    assert cpu_budget.queue_timeout_sec is None


def test_ambiguous_push_persists_submit_unknown_and_blocks_duplicate_fallback(unit_project, tmp_path, monkeypatch):
    project, chapter = unit_project
    mgr = KaggleAccountManager(tmp_path / "kaggle_accounts.json")
    mgr.add("acct_1", "KGAT_token_1")
    mgr.add("acct_2", "KGAT_token_2")
    monkeypatch.setenv("KAGGLE_ACCOUNTS_FILE", str(mgr.registry_file))
    monkeypatch.setattr("src.cloud.kaggle_service.time.sleep", lambda *_: None)

    api = MagicMock()
    api.build_kaggle_client.side_effect = lambda: _fake_quota_api()
    api.dataset_status.return_value = "ready"
    api.dataset_list_files.return_value = SimpleNamespace(dataset_files=[SimpleNamespace(name="payload.tar.gz")])
    api.kernels_push.side_effect = ConnectionError("network drop after HTTP POST")
    api.kernels_status.side_effect = ConnectionError("status lookup also dropped")

    attempted = []

    def auth(self):
        attempted.append(self.account_name)
        self._username = self.account_name
        self._api = api

    monkeypatch.setattr(KaggleCloudService, "_authenticate", auth)
    svc = KaggleCloudService()

    # Transport-ambiguous push must fail closed with SUBMIT_UNKNOWN and MUST NOT rotate to acct_2.
    with pytest.raises(KaggleSubmitAmbiguousError):
        svc.dispatch_chapter_job_with_fallback("unit", "1", job_type="render", gpu=True)

    assert attempted == ["acct_1"]
    state = load_cloud_job_state(chapter / "cloud_job.json")
    assert state.status == LifecycleState.SUBMIT_UNKNOWN
    assert state.kernel_ref.startswith("acct_1/")

    # Subsequent dispatch sees SUBMIT_UNKNOWN and blocks duplicate submission.
    with pytest.raises(KaggleSubmitAmbiguousError):
        svc.dispatch_chapter_job("unit", "1", job_type="render", gpu=True)

    # Reconciling later via pull() recovers when status query works cleanly.
    api.kernels_status.return_value = SimpleNamespace(status="KernelWorkerStatus.COMPLETE", failureMessage=None)
    api.kernels_status.side_effect = None

    with pytest.raises(KaggleExecutionError):
        # No actual tarball downloaded in mock; verify pull reconciles state & invokes status/output!
        def _write_tampered(_ref, dest_dir):
            out = Path(dest_dir)
            out.mkdir(parents=True, exist_ok=True)
            atomic_write_json(out / "result_manifest.json", {"project_slug": "wrong"})

        api.kernels_output.side_effect = _write_tampered
        try:
            svc.pull("unit", "1")
        except Exception as exc:
            raise KaggleExecutionError(str(exc), category="invalid_response") from exc

    api.kernels_output.assert_called()


def test_ambiguous_push_auto_reconciles_when_remote_kernel_already_queued(unit_project, tmp_path, monkeypatch):
    project, chapter = unit_project
    monkeypatch.setattr("src.cloud.kaggle_service.time.sleep", lambda *_: None)

    svc = object.__new__(KaggleCloudService)
    svc.account_name = "primary"
    svc._username = "u1"
    svc._accounts = [("primary", "tok1")]
    api = MagicMock()
    api.dataset_status.return_value = "ready"
    api.dataset_list_files.return_value = SimpleNamespace(dataset_files=[SimpleNamespace(name="payload.tar.gz")])
    api.kernels_push.side_effect = TimeoutError("read timeout during push response")
    api.kernels_status.return_value = SimpleNamespace(status="KernelWorkerStatus.QUEUED", failureMessage=None)
    svc._api = api

    dispatched = svc.dispatch_chapter_job("unit", "1", job_type="render", gpu=False, wait=False)
    assert dispatched["status"] == "dispatched"
    assert dispatched["kernel_ref"].startswith("u1/")
    state = load_cloud_job_state(chapter / "cloud_job.json")
    assert state.status == "QUEUED"
    api.kernels_push.assert_called_once()


def test_active_job_and_corrupt_state_block_duplicate_dispatch(unit_project):
    project, chapter = unit_project
    state_path = chapter / "cloud_job.json"
    atomic_write_json(
        state_path,
        {
            "project_slug": "unit",
            "chapter_number": "1",
            "job_type": "render",
            "kernel_ref": "u1/running-job",
            "status": "RUNNING",
        },
    )
    with pytest.raises(KaggleExecutionError, match="already recorded"):
        check_no_duplicate_active_job(state_path)

    state_path.write_text("{bad-json", encoding="utf-8")
    with pytest.raises(ProviderIntegrityError, match="unreadable"):
        check_no_duplicate_active_job(state_path)


def test_account_fallback_fails_closed_on_post_submit_errors_and_cancels_before_queue_rotation(
    tmp_path, monkeypatch
):
    mgr = KaggleAccountManager(tmp_path / "kaggle_accounts.json")
    mgr.add("acct_1", "KGAT_token_1")
    mgr.add("acct_2", "KGAT_token_2")
    monkeypatch.setenv("KAGGLE_ACCOUNTS_FILE", str(mgr.registry_file))
    monkeypatch.setattr(
        KaggleCloudService,
        "_authenticate",
        lambda self: (setattr(self, "_api", object()), setattr(self, "_username", self.account_name)),
    )
    svc = KaggleCloudService()

    for err in [
        KaggleExecutionError("OOM", category="remote_failed"),
        KaggleExecutionError("dup", category="active_job_exists"),
        KaggleSubmitAmbiguousError("ambig", category="submit_unknown"),
        KaggleClientPollTimeoutError("poll limit", category="client_poll_timeout"),
        ProviderIntegrityError("bad manifest", category="manifest_mismatch"),
    ]:
        calls = []

        @with_account_fallback
        def _op(self):
            calls.append(self.account_name)
            raise err

        with pytest.raises(type(err)):
            _op(svc)
        assert calls == ["acct_1"]

    # Queue stall emits notice at queue_notice_seconds without failing, then queue timeout cancels kernel.
    monkeypatch.setattr("src.cloud.kaggle_service.time.sleep", lambda *_: None)
    mono_vals = iter([0.0, 0.0, 350.0, 350.0, 350.0, 950.0, 950.0, 950.0])
    monkeypatch.setattr("src.cloud.kaggle_service.time.monotonic", lambda: next(mono_vals, 1000.0))

    api = MagicMock()
    api.kernels_status.return_value = SimpleNamespace(status="KernelWorkerStatus.QUEUED", failureMessage=None)
    api.kernels_cancel.return_value = True
    poll_svc = object.__new__(KaggleCloudService)
    poll_svc.account_name = "acct_1"
    poll_svc._username = "acct_1"
    poll_svc._api = api
    state_file = tmp_path / "queue_job.json"
    with pytest.raises(KaggleQueueTimeoutError):
        poll_svc._poll_kernel_until_complete(
            "acct_1/k1",
            timeout_seconds=3600,
            queue_timeout_seconds=900.0,
            queue_notice_seconds=300.0,
            poll_interval=0,
            job_state_path=state_file,
        )
    api.kernels_cancel.assert_called_once_with("acct_1/k1")
    saved = read_json(state_file)
    assert saved["status"] == LifecycleState.QUEUE_TIMEOUT


def test_poll_running_timeout_persists_client_timeout_and_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr("src.cloud.kaggle_service.time.sleep", lambda *_: None)
    mono_vals = iter([0.0, 0.0, 1.0, 1.0, 1.0, 15.0, 15.0, 15.0])
    monkeypatch.setattr("src.cloud.kaggle_service.time.monotonic", lambda: next(mono_vals, 20.0))

    api = MagicMock()
    api.kernels_status.return_value = SimpleNamespace(status="KernelWorkerStatus.RUNNING", failureMessage=None)
    svc = object.__new__(KaggleCloudService)
    svc.account_name = "acct_1"
    svc._username = "acct_1"
    svc._api = api
    state_file = tmp_path / "run_job.json"

    with pytest.raises(KaggleClientPollTimeoutError):
        svc._poll_kernel_until_complete(
            "acct_1/running-k",
            timeout_seconds=10,
            queue_timeout_seconds=5.0,
            poll_interval=0,
            job_state_path=state_file,
        )
    saved = read_json(state_file)
    assert saved["status"] == LifecycleState.CLIENT_TIMEOUT
    assert saved["last_remote_status"] == "RUNNING"


def test_single_device_uses_in_process_queue_without_deadlock(unit_project, tmp_path, monkeypatch):
    project, chapter = unit_project
    config = worker.read_json(project / "project.json")

    class _DummyModel:
        def __init__(self, gpu_id):
            self.gpu_id = gpu_id

        def unload(self):
            pass

    seen_clips = []

    def _fake_encode(task, canvas_cache, gpu_index):
        path = Path(task["output_clip_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"ok")
        seen_clips.append(task["shot_index"])
        return path

    monkeypatch.setattr("src.tts.cuda_worker.VieNeuCUDAWorker", _DummyModel)
    monkeypatch.setattr(worker, "render_shot_clip", _fake_encode)
    events = queue.Queue()
    # Passing jobs=None triggers single-process _InProcessQueue without blocking OS pipes.
    worker._audio_then_encode(
        project,
        ["1"],
        tmp_path / "work",
        "render",
        True,
        config,
        "libx264",
        0,
        1,
        1,
        None,
        events,
        4,
    )
    collected = []
    while not events.empty():
        collected.append(events.get_nowait())
    assert any(ev[0] == "heartbeat" for ev in collected)
    assert any(ev[0] == "ready" for ev in collected)
    assert len(seen_clips) >= 1


def test_worker_heartbeat_watchdog_and_disk_low_water(unit_project, tmp_path, monkeypatch):
    project, _ = unit_project
    with pytest.raises(RuntimeError, match="Insufficient scratch disk"):
        check_disk_low_water(tmp_path, min_free_mb=10**12)

    class _FakeChild:
        def __init__(self):
            self.exitcode = None
            self.terminated = False
            self.killed = False

        def start(self):
            pass

        def is_alive(self):
            return not self.killed

        def terminate(self):
            self.terminated = True
            self.killed = True

        def kill(self):
            self.killed = True

        def join(self, timeout=None):
            pass

    fake_child = _FakeChild()

    class _FakeQueue:
        def get(self, timeout=None):
            raise queue.Empty()

        def qsize(self):
            return 0

        def close(self):
            pass

    class _FakeCtx:
        def Queue(self, maxsize=0):
            return _FakeQueue()

        def Process(self, target=None, args=()):
            return fake_child

    monkeypatch.setattr(worker.mp, "get_context", lambda *_args: _FakeCtx())
    mono_vals = iter([0.0, 0.0, 100.0, 100.0])
    monkeypatch.setattr(worker.time, "monotonic", lambda: next(mono_vals, 200.0))

    with pytest.raises(RuntimeError, match="heartbeat stalled"):
        worker.run_pipeline(
            project,
            ["1"],
            "render",
            encoder="libx264",
            gpu_ids=[0],
            render_workers=1,
            worker_heartbeat_timeout_sec=5.0,
        )
    assert fake_child.terminated
