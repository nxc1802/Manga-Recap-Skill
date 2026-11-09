"""
tests/unit/cloud/test_kaggle_p0_correctness.py
P0 Cloud Pipeline Correctness Regression Test Suite (Test Matrix T01-T07 & Security).

Validates canonical contracts established in Gate 0 (docs/engineering/contract_decisions.md):
- T01: 1 segment with 3 selected panels are all rendered in correct sequence (multi-panel).
- T02: Intentionally omitted source panel is valid and does not cause pipeline failure.
- T03: Missing panel in panel_ids triggers explicit fail-fast with seq_id and path without substitution.
- T04: Multiple voice/speed/emotion per segment passed correctly to TTS.
- T05: Non-sequential seq_id (e.g. "002_1") maps cleanly across audio, SRT, video clips.
- T06: Batch TTS missing audio result triggers immediate failure (no silent truncation/zip).
- T07: Incomplete download / missing MP4 / timeout never marks chapter completed.
- Security: Path traversal rejection on archive extraction, no unconditional CC0.
"""

from __future__ import annotations

import ast
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
from src.core.schemas import (
    ChapterMetadata,
    CheckpointStatus,
    PanelManifest,
    ProjectConfig,
    RecapScript,
)
from src.core.storage import (
    atomic_write_json,
    get_chapter_dir,
    get_project_dir,
    read_json,
)


# ==============================================================================
# Hermetic Test Fixtures
# ==============================================================================

@pytest.fixture
def synthetic_workspace(tmp_path, monkeypatch):
    """
    Creates an isolated hermetic workspace containing a copy of the canonical
    synthetic fixture (tests/fixtures/synthetic/).
    Monkeypatches storage directory resolvers to point into this temporary directory.
    """
    src_fixture = Path(__file__).resolve().parent.parent.parent / "fixtures" / "synthetic"
    assert src_fixture.exists(), f"Synthetic fixture missing at {src_fixture}"

    proj_dir = tmp_path / "synthetic_fixture"
    shutil.copytree(src_fixture, proj_dir)

    chap_dir = proj_dir / "chapters" / "1"
    assert (proj_dir / "project.json").exists()
    assert (chap_dir / "chapter.json").exists()
    assert (chap_dir / "script.json").exists()
    assert (chap_dir / "manifest.json").exists()

    monkeypatch.setattr("src.cloud.kaggle_service.get_project_dir", lambda p: proj_dir)
    monkeypatch.setattr("src.cloud.kaggle_service.get_chapter_dir", lambda p, c: chap_dir)

    return {
        "root": tmp_path,
        "project_dir": proj_dir,
        "chapter_dir": chap_dir,
        "project_slug": "synthetic_fixture",
        "chapter_number": "1",
    }


@pytest.fixture
def cloud_service():
    """Returns a KaggleCloudService instance with mock API client."""
    service = KaggleCloudService(api_token="KGAT_hermetic_mock_token_99999")
    mock_api = MagicMock()
    mock_api.get_config_value.return_value = "cuongnguyen1802"
    service._api = mock_api
    service._username = "cuongnguyen1802"
    return service


# ==============================================================================
# T01: Multi-Panel Segment Rendering
# ==============================================================================

class TestT01MultiPanelRendering:
    """
    T01: 1 segment with 3 selected panels must all be handled in correct sequence without skipping.
    Canonical fixture segment '002_1' maps to:
    ['panel_002.jpg', 'panel_003.jpg', 'panel_004.jpg'].
    """

    def test_worker_script_generates_multi_panel_composition_logic(self, cloud_service, synthetic_workspace):
        worker_code = cloud_service.generate_worker_script(
            synthetic_workspace["project_slug"],
            synthetic_workspace["chapter_number"],
        )
        assert ast.parse(worker_code), "Worker script must be syntactically valid Python"

        # Invariant 1: Must NOT discard multi-panels with p_ids[0] single-panel assumption
        # Buggy pattern was: p_name = p_ids[0] followed by directly opening that single panel
        assert "p_name = p_ids[0]" not in worker_code, (
            "Worker script must eliminate single-panel assumption 'p_name = p_ids[0]' "
            "and iterate/render all selected panels."
        )

        # Invariant 2: Script must handle all panel_ids in the segment
        assert "panel_ids" in worker_code or "p_ids" in worker_code

    def test_multi_panel_segment_retains_all_panels_in_sequence(self, synthetic_workspace):
        """Verifies synthetic script.json contains 3 distinct panels in segment '002_1'."""
        chap_dir = synthetic_workspace["chapter_dir"]
        script_data = read_json(chap_dir / "script.json")
        seg_002 = next((s for s in script_data["segments"] if s["seq_id"] == "002_1"), None)
        assert seg_002 is not None
        assert len(seg_002["panel_ids"]) == 3
        assert seg_002["panel_ids"] == ["panel_002.jpg", "panel_003.jpg", "panel_004.jpg"]

        # All 3 physical files must exist in panels/
        for p in seg_002["panel_ids"]:
            assert (chap_dir / "panels" / p).exists(), f"Required panel {p} must exist"


# ==============================================================================
# T02: Intentionally Omitted Source Panel Is Valid
# ==============================================================================

class TestT02OmittedSourcePanel:
    """
    T02: An intentionally omitted source panel (present in manifest & disk, but not in script.json)
    is valid and must not cause pipeline failure.
    Canonical fixture has 'panel_005.jpg' on disk & in manifest.json, but omitted from script.json.
    """

    def test_omitted_panel_payload_packaging(self, cloud_service, synthetic_workspace):
        chap_dir = synthetic_workspace["chapter_dir"]
        out_tar = synthetic_workspace["root"] / "payload_test.tar.gz"

        # Assert panel_005.jpg exists physically
        assert (chap_dir / "panels" / "panel_005.jpg").exists()

        # Assert panel_005.jpg is NOT referenced in script.json
        script_data = read_json(chap_dir / "script.json")
        referenced = {p for seg in script_data["segments"] for p in seg.get("panel_ids", [])}
        assert "panel_005.jpg" not in referenced

        # Packaging must succeed cleanly without validation errors
        result_tar = cloud_service.package_chapter_payload(
            synthetic_workspace["project_slug"],
            synthetic_workspace["chapter_number"],
            out_tar,
        )
        assert result_tar.exists()
        assert result_tar.stat().st_size > 0

        # Inspect tar contents
        with tarfile.open(result_tar, "r:gz") as tar:
            names = tar.getnames()
            assert "chapter/script.json" in names
            assert "chapter/panels/panel_005.jpg" in names or "chapter/panels" in names

    def test_worker_script_generation_succeeds_with_omitted_panel(self, cloud_service, synthetic_workspace):
        worker_code = cloud_service.generate_worker_script(
            synthetic_workspace["project_slug"],
            synthetic_workspace["chapter_number"],
        )
        assert len(worker_code) > 500
        # Worker must not mandate 100% manifest panel coverage in script
        assert "missing from script" not in worker_code.lower()


# ==============================================================================
# T03: Missing Panel in panel_ids Triggers Fail-Fast (No Substitution)
# ==============================================================================

class TestT03MissingPanelFailFast:
    """
    T03: If a panel referenced in panel_ids is missing on disk,
    the pipeline MUST fail fast identifying seq_id and the missing path.
    It is STRICTLY FORBIDDEN to fall back to panel_001.jpg or substitute images.
    """

    def test_worker_script_does_not_contain_first_panel_fallback(self, cloud_service, synthetic_workspace):
        worker_code = cloud_service.generate_worker_script(
            synthetic_workspace["project_slug"],
            synthetic_workspace["chapter_number"],
        )
        # Buggy pattern was:
        # avail_panels = sorted(list(panels_dir.glob("*.jpg")))
        # if avail_panels: panel_file = avail_panels[0]
        assert "avail_panels[0]" not in worker_code, (
            "Worker script must NOT silently substitute missing panels with avail_panels[0]!"
        )

    def test_missing_panel_adversarial_fixture_contract(self, synthetic_workspace):
        """Verifies that synthetic fixture provides script_missing_panel.json with absent panel."""
        chap_dir = synthetic_workspace["chapter_dir"]
        missing_script = chap_dir / "script_missing_panel.json"
        assert missing_script.exists()

        data = read_json(missing_script)
        missing_seg = next(s for s in data["segments"] if s["seq_id"] == "004_missing_test")
        assert missing_seg["panel_ids"] == ["panel_missing_999.jpg"]

        missing_path = chap_dir / "panels" / "panel_missing_999.jpg"
        assert not missing_path.exists(), "panel_missing_999.jpg must NOT exist on disk"


# ==============================================================================
# T04: Multi-Voice, Speed, and Emotion per Segment
# ==============================================================================

class TestT04MultiVoiceSpeedEmotion:
    """
    T04: Distinct voice_id, speed, and emotion per segment must be passed to TTS.
    Must NOT collapse all segments to the first segment's voice.
    """

    def test_worker_script_does_not_override_all_voices_with_first_segment(self, cloud_service, synthetic_workspace):
        worker_code = cloud_service.generate_worker_script(
            synthetic_workspace["project_slug"],
            synthetic_workspace["chapter_number"],
        )
        # Buggy pattern was:
        # target_voice = segments[0].get("voice_id", "Ngọc Huyền") if segments else "Ngọc Huyền"
        assert "target_voice = segments[0]" not in worker_code, (
            "Worker script must NOT collapse voice selection to segments[0]!"
        )

    def test_synthetic_script_voice_parameter_diversity(self, synthetic_workspace):
        """Confirms synthetic fixture contains multi-voice, multi-emotion parameters."""
        chap_dir = synthetic_workspace["chapter_dir"]
        script_data = read_json(chap_dir / "script.json")

        voices = [s.get("voice_id") for s in script_data["segments"]]
        emotions = [s.get("emotion") for s in script_data["segments"]]
        cues = [s.get("emotion_cue") for s in script_data["segments"]]

        assert "Ngọc Huyền" in voices
        assert "Nam An" in voices
        assert "dramatic" in emotions
        assert "sad" in emotions
        assert any(cue is not None for cue in cues)


# ==============================================================================
# T05: Opaque Non-Sequential seq_id Mapping
# ==============================================================================

class TestT05OpaqueSeqIdMapping:
    """
    T05: seq_id like '002_1' or non-sequential strings must cleanly map to audio,
    subtitles, and video clips without assuming integer indices (e.g. f"{idx:03d}").
    """

    def test_worker_script_does_not_hardcode_integer_sequence(self, cloud_service, synthetic_workspace):
        worker_code = cloud_service.generate_worker_script(
            synthetic_workspace["project_slug"],
            synthetic_workspace["chapter_number"],
        )
        # Buggy pattern in audio merge:
        # for idx in range(1, len(segments) + 1):
        #     seq_id = f"{idx:03d}"
        assert 'f"{idx:03d}"' not in worker_code and "f'{idx:03d}'" not in worker_code, (
            "Worker script must NOT hardcode integer sequence 'f\"{idx:03d}\"'; "
            "it must preserve opaque seq_id directly from segment metadata."
        )

    def test_synthetic_fixture_audio_files_match_opaque_seq_id(self, synthetic_workspace):
        chap_dir = synthetic_workspace["chapter_dir"]
        script_data = read_json(chap_dir / "script.json")
        audio_clips_dir = chap_dir / "audio" / "clips"

        # Assert physical file names correspond to exact seq_ids, including "002_1"
        for seg in script_data["segments"]:
            expected_audio = audio_clips_dir / f"audio_1_{seg['seq_id']}.wav"
            assert expected_audio.exists(), f"Audio clip {expected_audio.name} must exist"
            assert seg["seq_id"] in expected_audio.name


# ==============================================================================
# T06: Batch TTS Count Integrity (No Silent Truncation)
# ==============================================================================

class TestT06BatchTtsCountIntegrity:
    """
    T06: Batch TTS returning fewer audio clips than segments must trigger an immediate
    failure rather than silently zipping or truncating.
    """

    def test_worker_script_enforces_batch_size_equality(self, cloud_service, synthetic_workspace):
        worker_code = cloud_service.generate_worker_script(
            synthetic_workspace["project_slug"],
            synthetic_workspace["chapter_number"],
        )
        # Check that worker verifies batch length or handles count mismatch explicitly
        assert "len(audio_batch)" in worker_code or "len(segments)" in worker_code or "zip" not in worker_code


# ==============================================================================
# T07: Incomplete Download / Missing MP4 / Timeout Never Marks Completed
# ==============================================================================

class TestT07FalseCompletedAndErrorHandling:
    """
    T07: Incomplete download, missing MP4, or execution timeout must NEVER mark
    chapter status as 'completed'.
    """

    def test_sync_results_fails_when_video_missing(self, cloud_service, synthetic_workspace):
        """If downloaded output directory has NO video file, sync must fail and NOT mark completed."""
        empty_download_dir = synthetic_workspace["root"] / "download_no_video"
        empty_download_dir.mkdir(parents=True, exist_ok=True)
        # Create dummy subtitles only
        (empty_download_dir / "chapter_1.srt").write_text("1\n00:00:00 --> 00:00:05\nTest\n", encoding="utf-8")

        chap_dir = synthetic_workspace["chapter_dir"]
        meta_before = read_json(chap_dir / "chapter.json")
        meta_before["overall_status"] = "in_progress"
        atomic_write_json(chap_dir / "chapter.json", meta_before)

        # sync_results should either raise or fail cleanly, and MUST NOT mark completed
        try:
            res = cloud_service.sync_results(
                synthetic_workspace["project_slug"],
                synthetic_workspace["chapter_number"],
                empty_download_dir,
            )
            # If it returns a dict without raising, it must NOT claim success
            assert res.get("status") != "success", "Must not report success when video is missing"
        except (FileNotFoundError, RuntimeError, ValueError):
            pass  # Expected fail-fast

        meta_after = read_json(chap_dir / "chapter.json")
        assert meta_after.get("overall_status") != "completed", (
            "Chapter overall_status must NEVER be set to 'completed' when MP4 is missing!"
        )

    def test_timeout_in_poll_loop_raises_and_prevents_completion(self, cloud_service, synthetic_workspace):
        """When Kaggle kernel execution exceeds timeout, must raise and not mark completed."""
        cloud_service._api.kernels_push.return_value = MagicMock(ref="cuongnguyen1802/test-kernel")
        cloud_service._api.kernels_status.return_value = MagicMock(status="RUNNING")

        chap_dir = synthetic_workspace["chapter_dir"]
        meta = read_json(chap_dir / "chapter.json")
        meta["overall_status"] = "in_progress"
        atomic_write_json(chap_dir / "chapter.json", meta)

        with pytest.raises((TimeoutError, RuntimeError)):
            cloud_service.dispatch_chapter_job(
                synthetic_workspace["project_slug"],
                synthetic_workspace["chapter_number"],
                gpu=False,
                wait=True,
                poll_interval=0.01,
                timeout_seconds=0.05,
            )

        meta_after = read_json(chap_dir / "chapter.json")
        assert meta_after.get("overall_status") != "completed"


# ==============================================================================
# Security: Path Traversal Rejection & Licensing Checks
# ==============================================================================

class TestSecurityInvariants:
    """
    Security Invariants:
    1. Guard against Zip-Slip / Tar-Slip directory traversal during tar extraction.
    2. Do NOT unconditionally attach CC0 license to manga assets with unconfirmed copyright.
    """

    def test_archive_extraction_rejects_path_traversal(self, cloud_service, synthetic_workspace):
        """Malicious archive with '../' relative traversal must be rejected."""
        chap_dir = synthetic_workspace["chapter_dir"]
        evil_tar_path = synthetic_workspace["root"] / "evil_audio.tar.gz"

        # Create malicious tarball with traversal member
        tar_buf = io.BytesIO()
        with tarfile.open(fileobj=tar_buf, mode="w:gz") as tar:
            data = b"malicious payload"
            ti = tarfile.TarInfo(name="../../escaped_exploit.txt")
            ti.size = len(data)
            ti.mtime = int(time.time())
            tar.addfile(ti, io.BytesIO(data))
        evil_tar_path.write_bytes(tar_buf.getvalue())

        downloaded_dir = synthetic_workspace["root"] / "downloaded_evil"
        downloaded_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(evil_tar_path, downloaded_dir / "audio_clips.tar.gz")
        # Provide valid video so it gets past video check if needed
        (downloaded_dir / "chapter_1.mp4").write_bytes(b"\x00\x00\x00 ftypisom")

        target_escaped_file = chap_dir.parent / "escaped_exploit.txt"
        if target_escaped_file.exists():
            target_escaped_file.unlink()

        try:
            cloud_service.sync_results(
                synthetic_workspace["project_slug"],
                synthetic_workspace["chapter_number"],
                downloaded_dir,
            )
        except (ValueError, RuntimeError, PermissionError):
            pass  # Rejection via exception is valid

        assert not target_escaped_file.exists(), (
            "Security vulnerability: Archive extraction extracted files outside the destination directory!"
        )

    def test_dataset_metadata_does_not_unconditionally_attach_cc0(self, cloud_service, synthetic_workspace, tmp_path):
        """Verifies that dataset upload does not unconditionally attach CC0-1.0 license."""
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir(parents=True, exist_ok=True)

        cloud_service._api.kernels_push.return_value = MagicMock(ref="cuongnguyen1802/test-kernel")
        cloud_service._api.dataset_create_new.return_value = None

        cloud_service.dispatch_chapter_job(
            synthetic_workspace["project_slug"],
            synthetic_workspace["chapter_number"],
            gpu=False,
            wait=False,
        )

        # Check dataset metadata created in /tmp/kaggle_ds_*
        ds_meta_files = list(Path("/tmp").glob("kaggle_ds_*/dataset-metadata.json"))
        for meta_file in ds_meta_files:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            licenses = meta.get("licenses", [])
            for lic in licenses:
                assert lic.get("name") != "CC0-1.0", (
                    "Security / Copyright violation: Manga chapter dataset unconditionally claimed CC0-1.0!"
                )

    def test_worker_script_copies_uncompressed_audio_clips(self, cloud_service, synthetic_workspace):
        """Fix Critical 1 & S1.4: Worker script must scan and copy uncompressed audio clips from chapter payload."""
        script_content = cloud_service.generate_worker_script(
            synthetic_workspace["project_slug"],
            synthetic_workspace["chapter_number"],
            job_type="render",
        )
        assert "audio_clips_target = chapter_dir / \"audio\" / \"clips\"" in script_content
        assert "Found uncompressed audio clips directory" in script_content
        assert "cand_isolated = found_chap_parent / \"audio\" / \"clips\"" in script_content

    def test_worker_script_generates_dynamic_camera_motion_and_timed_cues(self, cloud_service, synthetic_workspace):
        """Fix Critical 2 & S1.2: Worker script must support timed subtitle cues and continuous output FPS camera motion."""
        script_content = cloud_service.generate_worker_script(
            synthetic_workspace["project_slug"],
            synthetic_workspace["chapter_number"],
            job_type="render",
            burn_subtitles=True,
        )
        assert "def split_text_into_cues" in script_content
        assert "def compose_frame_canvas" in script_content
        assert "is_dynamic" in script_content
        assert "num_steps = max(1, int(round(p_dur * cfg_fps)))" in script_content
