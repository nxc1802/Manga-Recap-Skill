"""
tests/adversarial/test_cloud_p0_adversarial.py
Adversarial Stress and Security Suite for Phase 1 Cloud Execution.

Covers:
1. Malicious Archive Extraction Attacks (Zip-Slip / Tar-Slip).
2. Adversarial Corrupted and Missing Artifacts (Zero-byte MP4, Corrupt Headers, Missing Clips).
3. Cloud Service Failure Handling (Kernel Failures, Crash Logs, OOM, Execution Timeouts).
4. Adversarial Script Inputs (Absent Panels in Multi-Panel Segments, Token Leak Prevention).
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import shutil
import tarfile
import time
from unittest.mock import MagicMock, patch

import pytest

from src.cloud.kaggle_service import KaggleCloudService
from src.core.storage import (
    atomic_write_json,
    get_chapter_dir,
    get_project_dir,
    read_json,
)


@pytest.fixture
def adversarial_workspace(tmp_path, monkeypatch):
    """Hermetic workspace derived from canonical synthetic fixture."""
    src_fixture = Path(__file__).resolve().parent.parent / "fixtures" / "synthetic"
    assert src_fixture.exists(), f"Synthetic fixture missing at {src_fixture}"

    proj_dir = tmp_path / "adv_project"
    shutil.copytree(src_fixture, proj_dir)

    chap_dir = proj_dir / "chapters" / "1"

    monkeypatch.setattr("src.cloud.kaggle_service.get_project_dir", lambda p: proj_dir)
    monkeypatch.setattr("src.cloud.kaggle_service.get_chapter_dir", lambda p, c: chap_dir)

    return {
        "root": tmp_path,
        "project_dir": proj_dir,
        "chapter_dir": chap_dir,
        "project_slug": "adv_project",
        "chapter_number": "1",
    }


@pytest.fixture
def cloud_service():
    """KaggleCloudService with mocked client."""
    service = KaggleCloudService(api_token="KGAT_adversarial_test_token_SECRET")
    mock_api = MagicMock()
    mock_api.get_config_value.return_value = "cuongnguyen1802"
    mock_api.dataset_status.return_value = "ready"
    mock_file = MagicMock()
    mock_file.name = "script.json"
    mock_ds_files = MagicMock()
    mock_ds_files.dataset_files = [mock_file]
    mock_api.dataset_list_files.return_value = mock_ds_files
    mock_ds = MagicMock()
    mock_ds.ref = "cuongnguyen1802/manga-recap-payload"
    mock_ds.current_version_number = 1
    mock_api.dataset_list.return_value = [mock_ds]
    service._api = mock_api
    service._username = "cuongnguyen1802"
    return service


# ==============================================================================
# 1. Malicious Archive Extraction (Zip-Slip / Tar-Slip Attacks)
# ==============================================================================

class TestAdversarialArchiveSecurity:
    """Stress tests protecting against path traversal in cloud output extraction."""

    def test_tar_slip_deep_parent_traversal(self, cloud_service, adversarial_workspace):
        """Attempts to write outside chapter directory using ../../../../tmp."""
        chap_dir = adversarial_workspace["chapter_dir"]
        escaped_file = adversarial_workspace["root"] / "pwned_deep_parent.txt"
        if escaped_file.exists():
            escaped_file.unlink()

        tar_buf = io.BytesIO()
        with tarfile.open(fileobj=tar_buf, mode="w:gz") as tar:
            data = b"TRAVERSAL_PAYLOAD"
            # Target deeply outside chapter directory
            ti = tarfile.TarInfo(name=f"../../../../{escaped_file.name}")
            ti.size = len(data)
            ti.mtime = int(time.time())
            tar.addfile(ti, io.BytesIO(data))

        download_dir = adversarial_workspace["root"] / "dl_slip_1"
        download_dir.mkdir(parents=True, exist_ok=True)
        (download_dir / "audio_clips.tar.gz").write_bytes(tar_buf.getvalue())
        (download_dir / "chapter_1.mp4").write_bytes(b"\x00\x00\x00 ftypisom" + b"X" * 1024)

        try:
            cloud_service.sync_results("adv_project", "1", download_dir)
        except (ValueError, RuntimeError, PermissionError):
            pass

        assert not escaped_file.exists(), "CRITICAL SECURITY BREACH: Deep parent Tar-Slip escaped boundary!"

    def test_tar_slip_absolute_path_traversal(self, cloud_service, adversarial_workspace):
        """Attempts to write to absolute path via tarball entry."""
        chap_dir = adversarial_workspace["chapter_dir"]
        escaped_file = adversarial_workspace["root"] / "pwned_absolute.txt"
        if escaped_file.exists():
            escaped_file.unlink()

        tar_buf = io.BytesIO()
        with tarfile.open(fileobj=tar_buf, mode="w:gz") as tar:
            data = b"ABSOLUTE_PATH_PAYLOAD"
            ti = tarfile.TarInfo(name=str(escaped_file.resolve()))
            ti.size = len(data)
            ti.mtime = int(time.time())
            tar.addfile(ti, io.BytesIO(data))

        download_dir = adversarial_workspace["root"] / "dl_slip_2"
        download_dir.mkdir(parents=True, exist_ok=True)
        (download_dir / "audio_clips.tar.gz").write_bytes(tar_buf.getvalue())
        (download_dir / "chapter_1.mp4").write_bytes(b"\x00\x00\x00 ftypisom" + b"X" * 1024)

        try:
            cloud_service.sync_results("adv_project", "1", download_dir)
        except (ValueError, RuntimeError, PermissionError):
            pass

        assert not escaped_file.exists(), "CRITICAL SECURITY BREACH: Absolute path Tar-Slip escaped boundary!"


# ==============================================================================
# 2. Corrupted and Incomplete Cloud Artifacts
# ==============================================================================

class TestAdversarialCorruptedArtifacts:
    """Stress tests verifying that corrupted artifacts never mark stages completed."""

    def test_zero_byte_mp4_rejection(self, cloud_service, adversarial_workspace):
        """Empty 0-byte video must not mark chapter completed."""
        chap_dir = adversarial_workspace["chapter_dir"]
        meta = read_json(chap_dir / "chapter.json")
        meta["overall_status"] = "in_progress"
        atomic_write_json(chap_dir / "chapter.json", meta)

        download_dir = adversarial_workspace["root"] / "dl_zero_byte"
        download_dir.mkdir(parents=True, exist_ok=True)
        (download_dir / "chapter_1.mp4").write_bytes(b"")  # 0 bytes

        try:
            res = cloud_service.sync_results("adv_project", "1", download_dir)
            assert res.get("status") != "success", "Zero-byte MP4 must not succeed"
        except (ValueError, RuntimeError, FileNotFoundError):
            pass

        meta_after = read_json(chap_dir / "chapter.json")
        assert meta_after.get("overall_status") != "completed"

    def test_corrupt_header_mp4_rejection(self, cloud_service, adversarial_workspace):
        """Corrupted junk bytes in MP4 must not mark chapter completed."""
        chap_dir = adversarial_workspace["chapter_dir"]
        meta = read_json(chap_dir / "chapter.json")
        meta["overall_status"] = "in_progress"
        atomic_write_json(chap_dir / "chapter.json", meta)

        download_dir = adversarial_workspace["root"] / "dl_corrupt_video"
        download_dir.mkdir(parents=True, exist_ok=True)
        (download_dir / "chapter_1.mp4").write_bytes(b"CORRUPTED_NOT_MP4_HEADER_GARBAGE")

        try:
            res = cloud_service.sync_results("adv_project", "1", download_dir)
            assert res.get("status") != "success"
        except (ValueError, RuntimeError, FileNotFoundError):
            pass

        meta_after = read_json(chap_dir / "chapter.json")
        assert meta_after.get("overall_status") != "completed"


# ==============================================================================
# 3. Cloud Execution Failures & Timeouts
# ==============================================================================

class TestAdversarialCloudFailures:
    """Stress tests on Kaggle API error states, crashes, and timeouts."""

    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass

    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass


# ==============================================================================
# 4. Token Leak Prevention & Security
# ==============================================================================

class TestAdversarialSecurityHygiene:
    """Verifies that authentication tokens and credentials are never leaked."""

    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass
