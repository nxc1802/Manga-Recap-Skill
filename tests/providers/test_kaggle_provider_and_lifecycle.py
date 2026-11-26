"""Fake-SDK tests for Kaggle provider transport, bootstrap verification, and lifecycle orchestration."""

import json
import os
import shutil
import sys
import tarfile
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.cloud.bootstrap import bootstrap_runtime, verify_runtime_files
from src.cloud.jobs import load_cloud_job_state
from src.cloud.kaggle_service import (
    KaggleCloudService,
    KaggleExecutionError,
    KaggleQueueTimeoutError,
    KaggleQuotaExhaustedError,
)
from src.core.storage import atomic_write_json, read_json
from src.providers.kaggle import KaggleClient, authenticate_kaggle_token
from tests.workflow.test_canonical_workflow import unit_project  # noqa: F401


def _install_fake_kaggle_module(monkeypatch, *, token_seen: list, fail_token: bool = False, username: str = "u_fake"):
    pkg = ModuleType("kaggle")
    api_pkg = ModuleType("kaggle.api")
    ext_mod = ModuleType("kaggle.api.kaggle_api_extended")

    class FakeKaggleApi:
        CONFIG_NAME_TOKEN = "token"
        CONFIG_NAME_USER = "username"

        def __init__(self):
            self.config_values = {}

        def authenticate(self):
            tok = os.environ.get("KAGGLE_API_TOKEN", "")
            token_seen.append(tok)
            self.config_values["token"] = "WRONG" if fail_token else tok
            self.config_values["username"] = username

    ext_mod.KaggleApi = FakeKaggleApi
    for name, mod in {
        "kaggle": pkg,
        "kaggle.api": api_pkg,
        "kaggle.api.kaggle_api_extended": ext_mod,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)


def test_kaggle_provider_auth_isolates_token_and_fails_on_mismatch(monkeypatch):
    seen = []
    monkeypatch.setenv("KAGGLE_API_TOKEN", "KGAT_env_orig")
    _install_fake_kaggle_module(monkeypatch, token_seen=seen, fail_token=False, username="kaggle_user")

    api, user = authenticate_kaggle_token("KGAT_override", account_name="work")
    assert api is not None
    assert user == "kaggle_user"
    assert seen == ["KGAT_override"]
    assert os.environ.get("KAGGLE_API_TOKEN") == "KGAT_env_orig"

    # Mismatched token / missing username fails cleanly without leaving dirty env var
    _install_fake_kaggle_module(monkeypatch, token_seen=seen, fail_token=True, username="")
    client = KaggleClient.from_token("KGAT_bad", account_name="bad")
    assert not client.is_authenticated
    assert os.environ.get("KAGGLE_API_TOKEN") == "KGAT_env_orig"


def test_dispatch_fails_fast_on_exhausted_quota_and_remote_failure(unit_project, tmp_path, monkeypatch):
    project, chapter = unit_project
    monkeypatch.setattr("src.cloud.kaggle_service.time.sleep", lambda *_: None)

    svc = object.__new__(KaggleCloudService)
    svc.account_name = "acct_quota"
    svc._username = "u1"
    svc._accounts = [("acct_quota", "tok")]

    class QuotaMockApi:
        def build_kaggle_client(self):
            class _Ctx:
                def __enter__(self_inner):
                    return SimpleNamespace(
                        kernels=SimpleNamespace(
                            kernels_api_client=SimpleNamespace(
                                get_accelerator_quota_statistics=lambda: {
                                    "gpu_quota": {"total_time_allowed": "100s", "time_used": "95s", "time_reserved": "0s"}
                                }
                            )
                        )
                    )

                def __exit__(self_inner, *_):
                    return False

            return _Ctx()

        dataset_create_new = MagicMock()
        kernels_push = MagicMock()

    svc._api = QuotaMockApi()
    with pytest.raises(KaggleQuotaExhaustedError, match="exhausted its weekly GPU quota"):
        svc.dispatch_chapter_job("unit", "1", job_type="render", gpu=True)
    svc._api.dataset_create_new.assert_not_called()
    svc._api.kernels_push.assert_not_called()

    # Remote FAILED downloads error logs, updates cloud_job.json, and raises KaggleExecutionError
    svc_fail = object.__new__(KaggleCloudService)
    svc_fail.account_name = "acct_ok"
    svc_fail._username = "u1"
    svc_fail._accounts = [("acct_ok", "tok")]
    api_fail = MagicMock()
    api_fail.dataset_status.return_value = "ready"
    api_fail.dataset_list_files.return_value = SimpleNamespace(dataset_files=[SimpleNamespace(name="payload.tar.gz")])
    api_fail.kernels_push.return_value = SimpleNamespace(ref="/code/u1/fail-kernel", error=None)
    api_fail.kernels_status.return_value = SimpleNamespace(status="KernelWorkerStatus.ERROR", failureMessage="OOM on T4")

    def _write_err_log(_ref, out_dir):
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        (Path(out_dir) / "worker.log").write_text("CUDA out of memory\n", encoding="utf-8")

    api_fail.kernels_output.side_effect = _write_err_log
    svc_fail._api = api_fail

    with pytest.raises(KaggleExecutionError, match="OOM on T4"):
        svc_fail.dispatch_chapter_job("unit", "1", job_type="render", gpu=False, poll_interval=0)

    state = load_cloud_job_state(chapter / "cloud_job.json")
    assert state.status == "FAILED"
    assert "OOM on T4" in (state.error or "")


def test_cancel_reconcile_pull_resume_stale_and_result_mismatch(unit_project, tmp_path, monkeypatch):
    project, chapter = unit_project
    monkeypatch.setattr("src.cloud.kaggle_service.time.sleep", lambda *_: None)

    svc = object.__new__(KaggleCloudService)
    svc.account_name = "primary"
    svc._username = "u1"
    svc._accounts = [("primary", "tok1")]
    mock_api = MagicMock()
    mock_api.dataset_status.return_value = "ready"
    mock_api.dataset_list_files.return_value = SimpleNamespace(dataset_files=[SimpleNamespace(name="payload.tar.gz")])
    mock_api.kernels_push.return_value = SimpleNamespace(ref="/code/u1/run-job", error=None)
    svc._api = mock_api

    dispatched = svc.dispatch_chapter_job("unit", "1", job_type="render", gpu=False, wait=False)
    assert dispatched["status"] == "dispatched"
    assert dispatched["kernel_ref"] == "u1/run-job"

    # Reconciliation updates local cloud_job.json from remote RUNNING state
    mock_api.kernels_status.return_value = SimpleNamespace(status="KernelWorkerStatus.RUNNING", failureMessage=None)
    reconciled = svc.reconcile_job("unit", "1")
    assert reconciled["status"] == "RUNNING"

    # Cancellation updates cloud_job.json
    cancelled = svc.cancel_job("unit", "1")
    mock_api.kernels_cancel.assert_called_once_with("u1/run-job")
    assert cancelled["status"] == "CANCELLED"

    # Reset dispatched state and verify result mismatch & stale input fail closed on pull()
    state_path = chapter / "cloud_job.json"
    saved_state = read_json(state_path)
    saved_state["status"] = "dispatched"
    saved_state["result"] = None
    atomic_write_json(state_path, saved_state)

    mock_api.kernels_status.return_value = SimpleNamespace(status="KernelWorkerStatus.COMPLETE", failureMessage=None)

    def _download_tampered_receipt(_ref, dest_dir):
        out = Path(dest_dir)
        out.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            out / "result_manifest.json",
            {
                "project_slug": "unit",
                "chapters": ["1"],
                "job_type": "render",
                "series": False,
                "payload_manifest_sha256": "deadbeef" * 8,
            },
        )

    mock_api.kernels_output.side_effect = _download_tampered_receipt
    with pytest.raises(ValueError, match="Cloud receipt does not match"):
        svc.pull("unit", "1")

    # Mutate local script after dispatch -> stale input detection rejects sync cleanly
    script_file = chapter / "script.json"
    script_data = read_json(script_file)
    script_data["segments"][0]["narrator_text"] = "edited after cloud launch"
    atomic_write_json(script_file, script_data)

    def _download_valid_receipt(_ref, dest_dir):
        out = Path(dest_dir)
        out.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            out / "result_manifest.json",
            {
                "project_slug": "unit",
                "chapters": ["1"],
                "job_type": "render",
                "series": False,
                "payload_manifest_sha256": saved_state["payload_manifest_sha256"],
            },
        )

    mock_api.kernels_output.side_effect = _download_valid_receipt
    with pytest.raises(ValueError, match="Local input changed during the cloud job"):
        svc.pull("unit", "1")

    # Cached synced result returns directly without re-polling API
    atomic_write_json(state_path, {**saved_state, "status": "synced", "result": {"status": "success", "cached": True}})
    mock_api.kernels_status.reset_mock()
    assert svc.pull("unit", "1") == {"status": "success", "cached": True}
    mock_api.kernels_status.assert_not_called()


def test_bootstrap_runtime_verifies_payload_and_invokes_worker(unit_project, tmp_path, monkeypatch):
    from src.cloud.payload import package_payload

    project, _ = unit_project
    tar_path = tmp_path / "input" / "payload.tar.gz"
    tar_path.parent.mkdir(parents=True, exist_ok=True)
    _, manifest_hash = package_payload(project, "1", tar_path, "render")

    runtime_root = tmp_path / "runtime"
    called = {}

    def _fake_worker_run(root, expected_hash, out_dir, **kwargs):
        called["root"] = Path(root)
        called["expected_hash"] = expected_hash
        called["kwargs"] = kwargs
        assert (Path(root) / "project" / "project.json").is_file()

    monkeypatch.setattr("src.cloud.dual_gpu_worker.run", _fake_worker_run)

    bootstrap_runtime(
        manifest_hash,
        encoder="libx264",
        render_workers=1,
        compile_feature=False,
        input_dir=tar_path.parent,
        runtime_root=runtime_root,
        output_dir=tmp_path / "results",
        install_deps=False,
    )
    assert called["expected_hash"] == manifest_hash
    assert called["kwargs"]["encoder"] == "libx264"
    assert not runtime_root.exists()

    # Direct verify_runtime_files rejects extra tampered file
    with tarfile.open(tar_path) as tf:
        tf.extractall(runtime_root, filter="data")
    (runtime_root / "injected.py").write_text("print('bad')", encoding="utf-8")
    with pytest.raises(RuntimeError, match="inventory mismatch"):
        verify_runtime_files(runtime_root, manifest_hash)


def test_push_kernel_retries_transient_dataset_propagation_404(unit_project, monkeypatch):
    project, chapter = unit_project
    monkeypatch.setattr("src.cloud.kaggle_service.time.sleep", lambda *_: None)

    class _FakeHttpError(Exception):
        def __init__(self, status_code: int, text: str):
            super().__init__(f"HTTP {status_code}")
            self.response = SimpleNamespace(status_code=status_code, text=text)

    svc = object.__new__(KaggleCloudService)
    svc.account_name = "primary"
    svc._username = "u1"
    svc._accounts = [("primary", "tok1")]
    mock_api = MagicMock()
    mock_api.dataset_status.return_value = "ready"
    mock_api.dataset_list_files.return_value = SimpleNamespace(dataset_files=[SimpleNamespace(name="payload.tar.gz")])
    mock_api.kernels_push.side_effect = [
        _FakeHttpError(404, "Dataset not found yet"),
        SimpleNamespace(ref="/code/u1/propagated-ok", error=None, invalidDatasetSources=["u1/ds"]),
        SimpleNamespace(ref="/code/u1/propagated-ok", error=None, invalidDatasetSources=[]),
    ]
    svc._api = mock_api

    res = svc.dispatch_chapter_job("unit", "1", job_type="render", gpu=False, wait=False)
    assert res["status"] == "dispatched"
    assert res["kernel_ref"] == "u1/propagated-ok"
    assert mock_api.kernels_push.call_count == 3
