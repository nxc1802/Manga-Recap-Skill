"""
tests/unit/cloud/test_kaggle_service.py
Unit tests for KaggleCloudService and CLI cloud integration.
"""

import json
from unittest.mock import MagicMock

import pytest

from src.cloud.kaggle_service import KaggleCloudService


def test_kaggle_service_initialization():
    service = KaggleCloudService(api_token="KGAT_mock_token_12345")
    assert service.api_token == "KGAT_mock_token_12345"


def test_convenience_dispatchers_route_correct_job_type():
    """dispatch_tts_job, dispatch_render_job, dispatch_produce_job route correctly."""
    service = KaggleCloudService(api_token="dummy")
    service.dispatch_chapter_job = MagicMock(return_value={"status": "dispatched"})

    service.dispatch_tts_job("p", "1", wait=False)
    service.dispatch_chapter_job.assert_called_with("p", "1", job_type="tts", wait=False)

    service.dispatch_render_job("p", "1", wait=False)
    service.dispatch_chapter_job.assert_called_with("p", "1", job_type="render", wait=False)

    service.dispatch_produce_job("p", "1", wait=False)
    service.dispatch_chapter_job.assert_called_with("p", "1", job_type="produce", wait=False)


def test_parse_kaggle_duration_and_gpu_quota_preflight():
    from contextlib import contextmanager
    from datetime import datetime, timedelta
    from types import SimpleNamespace
    from src.cloud.kaggle_service import _parse_kaggle_duration_seconds

    assert _parse_kaggle_duration_seconds("21600s") == 21600.0
    assert _parse_kaggle_duration_seconds("39098.757.0s") == 39098.757
    assert _parse_kaggle_duration_seconds(timedelta(days=1, seconds=21600)) == 108000.0
    assert _parse_kaggle_duration_seconds("0s") == 0.0

    service = object.__new__(KaggleCloudService)
    service.account_name = "existing"
    service._username = "cuongnguyen1802"

    class FakeApi:
        @contextmanager
        def build_kaggle_client(self):
            yield SimpleNamespace(
                kernels=SimpleNamespace(
                    kernels_api_client=SimpleNamespace(
                        get_accelerator_quota_statistics=lambda: SimpleNamespace(
                            quota_refresh_time=datetime(2026, 11, 21, 0, 0),
                            gpu_quota=SimpleNamespace(
                                time_used=timedelta(days=1, seconds=21600),
                                time_reserved=timedelta(0),
                                total_time_allowed=timedelta(days=1, seconds=21600),
                            ),
                        )
                    )
                )
            )

    service._api = FakeApi()
    quota = service.get_gpu_quota_status()
    assert quota is not None
    assert quota["exhausted"] is True
    assert quota["remaining_sec"] == 0.0
    assert quota["quota_refresh_time"] == "2026-11-21T00:00:00"


def test_poll_queue_timeout_cancel_and_account_fallback(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace
    from src.cloud.account_manager import KaggleAccountManager
    from src.cloud.kaggle_service import KaggleQueueTimeoutError, KaggleQuotaExhaustedError

    # 1. Queue timeout raises KaggleQueueTimeoutError and persists cloud_job.json
    chap_dir = tmp_path / "chap_1"
    chap_dir.mkdir(parents=True)
    state_file = chap_dir / "cloud_job.json"

    svc = object.__new__(KaggleCloudService)
    svc.account_name = "existing"
    svc._username = "u1"
    svc._api = MagicMock()
    svc._api.kernels_status.return_value = SimpleNamespace(status="KernelWorkerStatus.QUEUED")
    monkeypatch.setattr("src.cloud.kaggle_service.time.sleep", lambda *_: None)

    with pytest.raises(KaggleQueueTimeoutError, match="remained QUEUED"):
        svc._poll_kernel_until_complete(
            "u1/stuck-kernel",
            timeout_seconds=10,
            queue_timeout_seconds=0.01,
            poll_interval=0,
            job_state_path=state_file,
            job_metadata={"job_type": "render"},
        )
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved["status"] == "QUEUE_TIMEOUT"
    assert saved["kernel_ref"] == "u1/stuck-kernel"

    # 2. CANCEL_ACKNOWLEDGED fails immediately without waiting for timeout
    svc._api.kernels_status.return_value = SimpleNamespace(
        status="KernelWorkerStatus.CANCEL_ACKNOWLEDGED", failureMessage=None
    )
    with pytest.raises(RuntimeError, match="CANCEL_ACKNOWLEDGED"):
        svc._poll_kernel_until_complete(
            "u1/cancelled-kernel",
            timeout_seconds=10,
            queue_timeout_seconds=5.0,
            poll_interval=0,
        )

    # 3. dispatch_chapter_job_with_fallback skips exhausted account and rotates on QUEUED timeout
    mgr = KaggleAccountManager(tmp_path / "kaggle_accounts.json")
    mgr.add("acct_1", "KGAT_token_1")
    mgr.add("acct_2", "KGAT_token_2")
    mgr.add("acct_3", "KGAT_token_3")
    monkeypatch.setenv("KAGGLE_ACCOUNTS_FILE", str(mgr.registry_file))
    monkeypatch.setattr(
        KaggleCloudService,
        "_authenticate",
        lambda self: (setattr(self, "_api", object()), setattr(self, "_username", self.account_name)),
    )
    cloud_svc = KaggleCloudService()
    attempted = []

    def fake_dispatch(self, slug, ch, **kwargs):
        attempted.append(self.account_name)
        if self.account_name == "acct_1":
            raise KaggleQuotaExhaustedError("quota exhausted")
        if self.account_name == "acct_2":
            raise KaggleQueueTimeoutError("queued too long")
        return {"status": "success", "account": self.account_name}

    monkeypatch.setattr(KaggleCloudService, "dispatch_chapter_job", fake_dispatch)
    res = cloud_svc.dispatch_chapter_job_with_fallback("proj", "1", job_type="render")
    assert res == {"status": "success", "account": "acct_3"}
    assert attempted == ["acct_1", "acct_2", "acct_3"]


def test_render_dispatch_requires_voice_approval_before_kaggle_push(tmp_path, monkeypatch):
    from src.core.storage import StorageFileNotFoundError
    from src.cloud.payload import package_payload

    # Create minimal project missing voice_selection.json
    proj_dir = tmp_path / "missing_voice"
    chap_dir = proj_dir / "chapters" / "1"
    chap_dir.mkdir(parents=True)
    (proj_dir / "project.json").write_text(json.dumps({"slug": "missing_voice", "render_settings": {}}), encoding="utf-8")
    monkeypatch.setattr("src.cloud.kaggle_service.get_project_dir", lambda _: proj_dir)
    monkeypatch.setattr("src.core.series_setup.require_series_setup", lambda *a, **kw: None)

    svc = object.__new__(KaggleCloudService)
    svc._api = MagicMock()
    svc._username = "u1"

    with pytest.raises(StorageFileNotFoundError, match="voice_selection.json"):
        svc.dispatch_chapter_job("missing_voice", "1", job_type="render", wait=False)
    svc._api.dataset_create_new.assert_not_called()
    svc._api.kernels_push.assert_not_called()

    with pytest.raises(StorageFileNotFoundError, match="voice_selection.json"):
        package_payload(proj_dir, "1", tmp_path / "out.tar.gz", "render")
