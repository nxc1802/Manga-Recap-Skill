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

from src.cloud.account_manager import KaggleAccountManager
from src.cloud import dual_gpu_worker as worker
from src.cloud.jobs import load_cloud_job_state
from src.cloud.kaggle_service import (
    KaggleAuthError,
    KaggleClientPollTimeoutError,
    KaggleCloudService,
    KaggleExecutionError,
    KaggleQueueTimeoutError,
    KaggleQuotaExhaustedError,
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

    # Transport-ambiguous push must fail closed with SUBMIT_UNKNOWN on pinned acct_1.
    with pytest.raises(KaggleSubmitAmbiguousError):
        svc.dispatch_chapter_job("unit", "1", job_type="render", gpu=True)

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

    dispatched = svc.dispatch_chapter_job("unit", "1", job_type="render", gpu=True, wait=False)
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


def test_queue_timeout_preserves_lock_without_auto_cancel_and_ambiguous_outcomes_keep_intent(
    unit_project, tmp_path, monkeypatch
):
    project, chapter = unit_project
    monkeypatch.setattr("src.cloud.kaggle_service.time.sleep", lambda *_: None)
    mono_vals = iter([0.0, 0.0, 350.0, 350.0, 350.0, 950.0, 950.0, 950.0])
    monkeypatch.setattr("src.cloud.kaggle_service.time.monotonic", lambda: next(mono_vals, 1000.0))

    api = MagicMock()
    api.kernels_status.return_value = SimpleNamespace(status="KernelWorkerStatus.QUEUED", failureMessage=None)
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
    api.kernels_cancel.assert_not_called()
    saved = read_json(state_file)
    assert saved["status"] == LifecycleState.QUEUE_TIMEOUT
    assert "cancel_attempted" not in saved

    # Verify push HTTP 429/403/422 roll back reservation, whereas empty {} / 500 persist SUBMIT_UNKNOWN lock (Fix #4).
    class _HttpErr(Exception):
        def __init__(self, code: int, text: str):
            super().__init__(f"HTTP {code}: {text}")
            self.response = SimpleNamespace(status_code=code, text=text)

    svc = object.__new__(KaggleCloudService)
    svc.account_name = "primary"
    svc._username = "u1"
    svc._accounts = [("primary", "tok1")]
    push_api = MagicMock()
    push_api.dataset_status.return_value = "ready"
    push_api.dataset_list_files.return_value = SimpleNamespace(dataset_files=[SimpleNamespace(name="payload.tar.gz")])
    svc._api = push_api
    ch_state = chapter / "cloud_job.json"

    for exc, expected_cls in [
        (_HttpErr(429, "Too many requests"), KaggleQuotaExhaustedError),
        (_HttpErr(403, "Forbidden token"), KaggleAuthError),
        (_HttpErr(422, "Invalid kernel slug"), KaggleExecutionError),
    ]:
        push_api.kernels_push.side_effect = exc
        with pytest.raises(expected_cls):
            svc.dispatch_chapter_job("unit", "1", job_type="render", gpu=True, wait=False)
        assert not ch_state.exists()

    push_api.kernels_push.side_effect = None
    push_api.kernels_push.return_value = {}
    push_api.kernels_status.side_effect = ConnectionError("status lookup down")
    with pytest.raises(KaggleSubmitAmbiguousError):
        svc.dispatch_chapter_job("unit", "1", job_type="render", gpu=True, wait=False)
    assert load_cloud_job_state(ch_state).status == LifecycleState.SUBMIT_UNKNOWN


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

    clock = [0.0]

    class _FakeQueue:
        def get(self, timeout=None):
            clock[0] += 10.0
            raise queue.Empty()

        def put_nowait(self, item):
            pass

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
    monkeypatch.setattr(worker.time, "monotonic", lambda: clock[0])

    with pytest.raises(RuntimeError, match="heartbeat stalled"):
        worker.run_pipeline(
            project,
            ["1"],
            "render",
            encoder="h264_nvenc",
            gpu_ids=(0, 1),
            render_workers=1,
            worker_heartbeat_timeout_sec=5.0,
        )
    assert fake_child.terminated


def test_timeout_unknown_and_original_account_reconcile(unit_project, tmp_path, monkeypatch):
    """P0/Fix #3: QUEUE_TIMEOUT / CLIENT_TIMEOUT / REMOTE_COMPLETE block duplicates; reconcile uses original account."""
    project, chapter = unit_project
    mgr = KaggleAccountManager(tmp_path / "kaggle_accounts.json")
    mgr.add("acct_1", "KGAT_token_1")
    mgr.add("acct_2", "KGAT_token_2")
    monkeypatch.setenv("KAGGLE_ACCOUNTS_FILE", str(mgr.registry_file))
    monkeypatch.setattr("src.cloud.kaggle_service.time.sleep", lambda *_: None)

    api_by_acct = {"acct_1": MagicMock(), "acct_2": MagicMock()}
    api_by_acct["acct_1"].kernels_status.return_value = SimpleNamespace(status="KernelWorkerStatus.QUEUED", failureMessage=None)

    def auth(self):
        self._username = self.account_name
        self._api = api_by_acct[self.account_name]

    monkeypatch.setattr(KaggleCloudService, "_authenticate", auth)
    svc = KaggleCloudService()
    svc._select_account(1)  # currently switched to acct_2!

    state_path = chapter / "cloud_job.json"
    atomic_write_json(
        state_path,
        {
            "project_slug": "unit",
            "chapter_number": "1",
            "job_type": "render",
            "kernel_ref": "acct_1/queued-job",
            "account_name": "acct_1",
            "username": "acct_1",
            "status": LifecycleState.QUEUE_TIMEOUT,
        },
    )

    # Active account switched to acct_2 -> reconcile_job STILL queries original acct_1!
    reconcile_res = svc.reconcile_job("unit", "1")
    assert reconcile_res["status"] == LifecycleState.QUEUED
    assert svc.account_name == "acct_2"  # restored cleanly after reconcile
    api_by_acct["acct_1"].kernels_status.assert_called_once_with("acct_1/queued-job")
    api_by_acct["acct_2"].kernels_status.assert_not_called()

    # QUEUE_TIMEOUT while remote still QUEUED blocks duplicate dispatch even while active account is acct_2!
    api_by_acct["acct_1"].kernels_status.reset_mock()
    with pytest.raises(KaggleExecutionError, match="already recorded"):
        svc.dispatch_chapter_job("unit", "1", job_type="render", gpu=True, wait=False)
    api_by_acct["acct_1"].kernels_status.assert_called_once_with("acct_1/queued-job")
    api_by_acct["acct_2"].kernels_status.assert_not_called()

    # Unknown status probe or CLIENT_TIMEOUT -> COMPLETE stays blocked until pull/sync completes!
    atomic_write_json(
        state_path,
        {
            "project_slug": "unit",
            "chapter_number": "1",
            "job_type": "render",
            "kernel_ref": "acct_1/running-timeout",
            "account_name": "acct_1",
            "username": "acct_1",
            "status": LifecycleState.CLIENT_TIMEOUT,
        },
    )
    api_by_acct["acct_1"].kernels_status.return_value = SimpleNamespace(status="UNKNOWN_STATUS", failureMessage=None)
    with pytest.raises(KaggleExecutionError, match="already recorded"):
        check_no_duplicate_active_job(state_path, service=svc)
    assert load_cloud_job_state(state_path).status == LifecycleState.CLIENT_TIMEOUT

    api_by_acct["acct_1"].kernels_status.return_value = SimpleNamespace(status="KernelWorkerStatus.COMPLETE", failureMessage=None)
    with pytest.raises(KaggleExecutionError, match="already completed"):
        check_no_duplicate_active_job(state_path, service=svc)
    assert load_cloud_job_state(state_path).status == LifecycleState.REMOTE_COMPLETE

    # Manual UI cancellation (CANCEL_ACKNOWLEDGED) or ERROR reconciles to FAILED and unblocks cleanly.
    api_by_acct["acct_1"].kernels_status.return_value = SimpleNamespace(status="KernelWorkerStatus.CANCEL_ACKNOWLEDGED", failureMessage=None)
    reconciled_cancel = check_no_duplicate_active_job(state_path, service=svc)
    assert reconciled_cancel is not None and reconciled_cancel.status == LifecycleState.FAILED

    api_by_acct["acct_1"].kernels_status.return_value = SimpleNamespace(status="KernelWorkerStatus.ERROR", failureMessage="OOM")
    reconciled = check_no_duplicate_active_job(state_path, service=svc)
    assert reconciled is not None and reconciled.status == LifecycleState.FAILED


def test_project_lock_blocks_overlapping_chapter_series_chunk_and_concurrent_dispatch(unit_project, monkeypatch):
    """P0 S1: project lock serializes overlapping reservations, blocks chapter vs series overlap, and blocks sync failures."""
    import threading
    from src.cloud.lifecycle import reserve_cloud_job_slot
    from src.core.schemas import ChapterMetadata
    from src.core.project import get_chapter_dir

    project, ch1 = unit_project
    ch2 = project / "chapters" / "2"
    ch2.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        ch2 / "chapter.json",
        ChapterMetadata(project_slug="unit", chapter_number="2", title="Chapter 2", chapter_id="ch2"),
    )
    (ch2 / "script.json").write_text((ch1 / "script.json").read_text(encoding="utf-8"), encoding="utf-8")

    svc = object.__new__(KaggleCloudService)
    svc.account_name = "primary"
    svc._username = "u1"
    svc._accounts = [("primary", "tok1")]
    mock_api = MagicMock()
    mock_api.dataset_status.return_value = "ready"
    mock_api.dataset_list_files.return_value = SimpleNamespace(dataset_files=[SimpleNamespace(name="payload.tar.gz")])
    mock_api.kernels_status.return_value = SimpleNamespace(status="KernelWorkerStatus.RUNNING", failureMessage=None)
    svc._api = mock_api
    monkeypatch.setattr("src.cloud.kaggle_service.time.sleep", lambda *_: None)

    # 1. Chapter 1 active blocks overlapping series job [1, 2]
    atomic_write_json(
        ch1 / "cloud_job.json",
        {"project_slug": "unit", "chapter_number": "1", "chapters": ["1"], "kernel_ref": "u1/ch1", "status": "RUNNING"},
    )
    with pytest.raises(KaggleExecutionError, match="already recorded"):
        svc.dispatch_series_job("unit", ["1", "2"], job_type="render", gpu=True, wait=False)

    # 2. Project-level series job [1] in REMOTE_COMPLETE (after sync failure) blocks chapter 1, while chapter 2 proceeds!
    (ch1 / "cloud_job.json").unlink()
    # Force explicit REMOTE_COMPLETE check without re-polling to RUNNING first in step 2!
    mock_api.kernels_status.return_value = SimpleNamespace(status="KernelWorkerStatus.COMPLETE", failureMessage=None)
    atomic_write_json(
        project / "cloud_job.json",
        {
            "project_slug": "unit",
            "chapter_number": "1",
            "chapters": ["1"],
            "series": True,
            "kernel_ref": "u1/ser1",
            "status": LifecycleState.REMOTE_COMPLETE,
        },
    )
    with pytest.raises(KaggleExecutionError, match="already completed"):
        svc.dispatch_chapter_job("unit", "1", job_type="render", gpu=True, wait=False)

    mock_api.kernels_status.return_value = SimpleNamespace(status="KernelWorkerStatus.RUNNING", failureMessage=None)

    # 3. Concurrent slot reservation under FileLock: exactly 1 caller reserves ch2, the second blocks.
    barrier = threading.Barrier(2)
    results = []
    errors = []

    def _race(tag):
        barrier.wait(timeout=5)
        try:
            res_id, _ = reserve_cloud_job_slot(
                project,
                ch2 / "cloud_job.json",
                ["2"],
                {"project_slug": "unit", "chapter_number": "2", "chapters": ["2"], "kernel_ref": f"u1/{tag}"},
            )
            results.append(res_id)
        except Exception as exc:
            errors.append(exc)

    t1 = threading.Thread(target=_race, args=("k1",))
    t2 = threading.Thread(target=_race, args=("k2",))
    t1.start(); t2.start()
    t1.join(timeout=5); t2.join(timeout=5)
    assert len(results) == 1
    assert len(errors) == 1 and isinstance(errors[0], KaggleExecutionError)
