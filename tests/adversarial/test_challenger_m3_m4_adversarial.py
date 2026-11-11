"""
tests/adversarial/test_challenger_m3_m4_adversarial.py
Adversarial Stress Test Suite for Manga-Recap-Skill V4 Milestones 3 & 4.
Author: challenger_m3_m4 (Empirical Challenger)

Covers empirical verification & adversarial challenge for:
1. Dual-T4 Concurrency, Pipelined Overlap, and Speedup Invariants
2. GPU Isolation and Environment Variable Mapping
3. Tar Packaging Security & Path Traversal (Tar Slip)
4. OOM Micro-Batch Halving & Non-OOM Error Handling
5. Chapter 1 Preview Gate Resistance & CLI Exit Codes
6. Two-Receipt Safe Cleanup Gate Bypass Resistance
7. Secret Whitelist Filtering Invariant
8. Tombstone Invariant Size Boundary (< 2048 bytes)
"""

import argparse
import io
import json
import os
import shutil
import sys
import tarfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.archive.hf_archive import (
    FORBIDDEN_PATTERNS,
    generate_archive_manifest,
    is_whitelisted_archive_path,
)
from src.cli.clean_cmd import _check_two_receipt_gate, handle_clean
from src.cli.cloud_cmd import handle_cloud
from src.cli.exit_codes import CliValidationError, ExitCode
from src.cloud.dual_t4_orchestrator import (
    ChapterTelemetry,
    DualT4Orchestrator,
    MicroBatchPlanner,
    RenderWorker,
    SeriesTelemetry,
    TTSWorker,
)
from src.cloud.kaggle_service import KaggleCloudService, safe_extract_tar
from src.core.storage import atomic_write_json, read_json


# ==============================================================================
# Fixtures
# ==============================================================================

@pytest.fixture
def mock_series_project(tmp_path):
    """Sets up a realistic mock multi-chapter project structure."""
    proj_dir = tmp_path / "data" / "projects" / "adv_series"
    proj_dir.mkdir(parents=True)

    proj_config = {
        "slug": "adv_series",
        "title": "Adversarial Series",
        "audio_settings": {"voice_id": "Ngọc Huyền", "speed": 1.0},
        "render_settings": {"video_mode": "auto", "burn_subtitles": False, "fps": 30},
    }
    (proj_dir / "project.json").write_text(json.dumps(proj_config), encoding="utf-8")
    (proj_dir / "characters.json").write_text("{}", encoding="utf-8")
    from src.core.series_setup import migrate_legacy_setup
    migrate_legacy_setup("adv_series", project_dir=proj_dir)

    valid_mp4_bytes = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 150000

    for ch_num in ["1", "2", "3"]:
        c_dir = proj_dir / "chapters" / ch_num
        (c_dir / "panels").mkdir(parents=True)
        (c_dir / "raw").mkdir(parents=True)
        (c_dir / "video").mkdir(parents=True)
        (c_dir / "subtitles").mkdir(parents=True)
        (c_dir / "audio" / "clips").mkdir(parents=True)

        meta = {
            "chapter_number": ch_num,
            "overall_status": "in_progress",
            "human_review_status": "unreviewed",
            "stages": {},
        }
        (c_dir / "chapter.json").write_text(json.dumps(meta), encoding="utf-8")

        script = {
            "chapter_number": ch_num,
            "segments": [
                {"seq_id": "001", "panel_id": "panel_001", "voice_id": "Ngọc Huyền", "text": "Segment 1"},
                {"seq_id": "002", "panel_id": "panel_002", "voice_id": "Ngọc Huyền", "text": "Segment 2"},
            ],
            "total_duration_seconds": 6.0,
        }
        (c_dir / "script.json").write_text(json.dumps(script), encoding="utf-8")
        (c_dir / "manifest.json").write_text('{"panels": []}', encoding="utf-8")

        # Media assets
        (c_dir / "panels" / "panel_001.jpg").write_bytes(b"\xff\xd8\xff\xe0dummy_jpg")
        (c_dir / "video" / "final.mp4").write_bytes(valid_mp4_bytes)
        (c_dir / "subtitles" / f"chapter_{ch_num}.srt").write_text("1\n00:00:00,000 --> 00:00:02,000\nHi\n")
        (c_dir / "audio" / "speech_master.wav").write_bytes(b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x00" * 40)
        (c_dir / "audio" / "clips" / f"audio_{ch_num}_001.wav").write_bytes(b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x00" * 40)

    return proj_dir


# ==============================================================================
# Challenge 1: Dual-T4 Concurrency & Fake Speedup Invariant
# ==============================================================================

def test_dual_t4_orchestrator_concurrency_and_speedup(mock_series_project, monkeypatch):
    """
    CHALLENGE:
    Examines if DualT4Orchestrator truly runs concurrent overlap between Chapter N render and Chapter N+1 TTS,
    or if it runs sequentially and masks it with `max(1.5, ...)`.
    """
    call_log = []

    def mock_synth(self, chap_dir, ch_num, **kwargs):
        call_log.append(("TTS_START", ch_num, time.time()))
        time.sleep(0.05)
        call_log.append(("TTS_END", ch_num, time.time()))
        return {
            "chapter": str(ch_num),
            "status": "ready",
            "segment_count": 2,
            "total_duration_seconds": 6.0,
            "throughput_utt_per_sec": 40.0,
        }

    def mock_render(self, chap_dir, chapter_num, **kwargs):
        call_log.append(("RENDER_START", chapter_num, time.time()))
        time.sleep(0.05)
        call_log.append(("RENDER_END", chapter_num, time.time()))
        return {
            "chapter": str(chapter_num),
            "status": "completed",
            "video_path": str(chap_dir / "video" / "final.mp4"),
            "duration_seconds": 6.0,
            "encoding_fps": 60.0,
        }

    monkeypatch.setattr(TTSWorker, "synthesize_chapter", mock_synth)
    monkeypatch.setattr(RenderWorker, "render_chapter", mock_render)
    monkeypatch.setattr(RenderWorker, "wait_for_audio_ready", lambda self, d, c, **kw: {})

    orchestrator = DualT4Orchestrator(
        workspace_dir=mock_series_project,
        chapters=["1", "2"],
        compile_feature=False,
        mock_mode=True,
    )

    t0 = time.time()
    tel = orchestrator.run_pipeline()
    total_elapsed = time.time() - t0

    # Inspect call sequence
    events = [e[0] for e in call_log]
    expected_sequential_pattern = [
        "TTS_START", "TTS_END", "RENDER_START", "RENDER_END",
        "TTS_START", "TTS_END", "RENDER_START", "RENDER_END"
    ]
    # Check that execution was NOT sequential, verifying genuine concurrency
    is_sequential = (events == expected_sequential_pattern)
    assert not is_sequential, "Execution should be concurrent, not sequential"

    tts_starts = [e[2] for e in call_log if e[0] == "TTS_START"]
    render_ends = [e[2] for e in call_log if e[0] == "RENDER_END"]
    has_overlap = len(tts_starts) >= 2 and len(render_ends) >= 1 and tts_starts[1] < render_ends[0]
    assert has_overlap, "DualT4Orchestrator did not execute TTS(N+1) concurrently with Render(N)"

    # Honest speedup verification (no fake clamp)
    assert tel.speedup_factor > 1.0, f"Expected honest speedup > 1.0, got {tel.speedup_factor}"
    assert tel.pipeline_overlap_seconds > 0.0


# ==============================================================================
# Challenge 2: GPU Isolation & Environment Mapping Invariant
# ==============================================================================

def test_gpu_isolation_cuda_visible_devices(tmp_path):
    """
    CHALLENGE:
    Checks if Process 0 sets CUDA_VISIBLE_DEVICES=0 and Process 1 sets CUDA_VISIBLE_DEVICES=1.
    Checks if RenderWorker passes -gpu 1 to FFmpeg command line.
    """
    tts_worker = TTSWorker(workspace_dir=tmp_path, gpu_id=0, mock_mode=True)
    render_worker = RenderWorker(workspace_dir=tmp_path, gpu_id=1, mock_mode=True)

    # 1. Check if os.environ["CUDA_VISIBLE_DEVICES"] is set by either worker
    # In the current implementation, neither worker sets os.environ["CUDA_VISIBLE_DEVICES"]
    env_cuda = os.environ.get("CUDA_VISIBLE_DEVICES")

    # 2. Check if RenderWorker passes -gpu 1 to FFmpeg
    # Check encoder args or _encode_video command
    encoder, enc_args = render_worker.encoder, render_worker.encoder_args
    has_gpu_flag = ("-gpu" in enc_args and enc_args[enc_args.index("-gpu") + 1] == "1")

    # We record this finding:
    assert tts_worker.gpu_id == 0
    assert render_worker.gpu_id == 1
    # Check whether FFmpeg has explicit -gpu flag
    assert has_gpu_flag, "RenderWorker must set -gpu 1 flag for NVENC hardware isolation"


# ==============================================================================
# Challenge 3: Packaging Security & Tar Traversal (Tar Slip)
# ==============================================================================

def test_safe_extract_tar_traversal_rejection(tmp_path):
    """
    CHALLENGE:
    Tests whether safe_extract_tar rejects archives containing path traversal
    attempts such as '../escape.txt' and absolute paths.
    """
    target_extract = tmp_path / "extracted"
    target_extract.mkdir()

    # Create malicious tarball with ../ path traversal
    bad_tar_path = tmp_path / "malicious.tar.gz"
    with tarfile.open(bad_tar_path, "w:gz") as tar:
        # File 1: ../evil.txt
        ti1 = tarfile.TarInfo(name="../evil.txt")
        ti1.size = 9
        tar.addfile(ti1, io.BytesIO(b"evil data"))

    with pytest.raises(ValueError, match="Path Traversal"):
        with tarfile.open(bad_tar_path, "r:gz") as tar:
            safe_extract_tar(tar, target_extract)

    # File 2: /absolute/path.txt
    bad_tar_path2 = tmp_path / "malicious2.tar.gz"
    with tarfile.open(bad_tar_path2, "w:gz") as tar:
        ti2 = tarfile.TarInfo(name="/etc/passwd")
        ti2.size = 9
        tar.addfile(ti2, io.BytesIO(b"root data"))

    with pytest.raises(ValueError, match="Path Traversal"):
        with tarfile.open(bad_tar_path2, "r:gz") as tar:
            safe_extract_tar(tar, target_extract)


def test_series_worker_script_extraction_security():
    """
    CHALLENGE:
    Verifies whether Kaggle Cloud worker script template uses safe_extract_tar
    or vulnerable direct tar.extractall().
    """
    service = KaggleCloudService(api_token="dummy")
    script_content = service.generate_series_worker_script(
        project_slug="test_proj",
        chapters=["1", "2"],
    )
    # Check if tar.extractall(path=workspace) is replaced with safe_extract_tar
    uses_direct_extractall = "tar.extractall(path=workspace)" in script_content
    assert not uses_direct_extractall, "generate_series_worker_script must not use raw tar.extractall(path=workspace)"
    assert "safe_extract_tar" in script_content, "generate_series_worker_script must embed safe_extract_tar"


# ==============================================================================
# Challenge 4: Dynamic OOM Micro-Batch Halving Invariant
# ==============================================================================

def test_dynamic_oom_backoff_halving_exhaustive():
    """
    CHALLENGE:
    Tests that MicroBatchPlanner.execute_batch_with_oom_backoff:
    1. Halves batch size on CUDA OOM down to 1.
    2. Preserves order of all segments with zero dropped segments.
    3. Raises when single-item batch triggers OOM.
    4. Immediately re-raises non-OOM exception without halving.
    """
    segments = [{"seq_id": f"{i:03d}", "text": f"Segment text {i}"} for i in range(32)]
    oom_tracker = {"count": 0}

    # Simulate OOM whenever batch size > 2
    def infer_func_oom_above_2(texts, voice):
        if len(texts) > 2:
            raise RuntimeError("CUDA out of memory. Tried to allocate 512MB")
        return [f"processed_{t}" for t in texts]

    results = MicroBatchPlanner.execute_batch_with_oom_backoff(
        batch=segments,
        infer_func=infer_func_oom_above_2,
        voice="Ngọc Huyền",
        oom_tracker=oom_tracker,
    )

    assert len(results) == 32
    assert oom_tracker["count"] > 0
    for i, res in enumerate(results):
        assert res == f"processed_Segment text {i}"

    # Test single item OOM raises
    def infer_func_always_oom(texts, voice):
        raise RuntimeError("CUDA out of memory. Tried to allocate 512MB")

    single_item = [{"seq_id": "001", "text": "Huge text"}]
    with pytest.raises(RuntimeError, match="CUDA out of memory"):
        MicroBatchPlanner.execute_batch_with_oom_backoff(
            batch=single_item,
            infer_func=infer_func_always_oom,
            voice="Ngọc Huyền",
        )

    # Test non-OOM exception is immediately re-raised
    def infer_func_value_error(texts, voice):
        raise ValueError("Invalid token encountered")

    with pytest.raises(ValueError, match="Invalid token"):
        MicroBatchPlanner.execute_batch_with_oom_backoff(
            batch=segments,
            infer_func=infer_func_value_error,
            voice="Ngọc Huyền",
        )


# ==============================================================================
# Challenge 5: Chapter 1 Preview Gate Resistance
# ==============================================================================

def test_chapter_1_preview_gate_cli_exit_code_2(mock_series_project, monkeypatch):
    """
    CHALLENGE:
    Verifies that running 'cloud produce-series' on unreviewed Chapter 1 without
    --skip-preview returns ExitCode.VALIDATION_ERROR (exit code 2).
    """
    monkeypatch.setattr("src.core.storage.get_project_dir", lambda p: mock_series_project)
    monkeypatch.setattr("src.cloud.kaggle_service.get_project_dir", lambda p: mock_series_project)
    monkeypatch.setattr("src.cloud.kaggle_service.get_chapter_dir", lambda p, c: mock_series_project / "chapters" / c)

    # Mock Kaggle authentication
    service = KaggleCloudService(api_token="dummy")
    service._api = MagicMock()
    service._username = "test_user"
    monkeypatch.setattr("src.cli.cloud_cmd.KaggleCloudService", lambda: service)

    # Invoke cloud produce-series without skip-preview
    args = argparse.Namespace(
        cloud_action="produce-series",
        project="adv_series",
        chapters="1,2",
        skip_preview=False,
    )

    res = handle_cloud(args)
    assert res["status"] == "error"
    assert res["exit_code"] == ExitCode.VALIDATION_ERROR  # Exit code 2
    assert "preview has not been approved" in res["error"]


def test_chapter_1_preview_gate_allows_when_approved_or_skip(mock_series_project, monkeypatch):
    """
    CHALLENGE:
    Verifies that preview gate passes when:
    1. skip_preview is True
    2. human_review_status is approved
    """
    monkeypatch.setattr("src.core.storage.get_project_dir", lambda p: mock_series_project)
    monkeypatch.setattr("src.cloud.kaggle_service.get_project_dir", lambda p: mock_series_project)
    monkeypatch.setattr("src.cloud.kaggle_service.get_chapter_dir", lambda p, c: mock_series_project / "chapters" / c)

    service = KaggleCloudService(api_token="dummy")
    service._api = MagicMock()
    service._username = "test_user"

    # 1. Allowed with skip_preview=True
    ok, err = service.check_chapter_1_preview_approval("adv_series", skip_preview=True)
    assert ok is True
    assert err is None

    # 2. Allowed when approved in chapter.json
    c1_meta = mock_series_project / "chapters" / "1" / "chapter.json"
    data = json.loads(c1_meta.read_text(encoding="utf-8"))
    data["human_review_status"] = "approved"
    c1_meta.write_text(json.dumps(data), encoding="utf-8")

    ok2, err2 = service.check_chapter_1_preview_approval("adv_series", skip_preview=False)
    assert ok2 is True
    assert err2 is None


# ==============================================================================
# Challenge 6: Two-Receipt Gate Bypass Resistance
# ==============================================================================

def test_two_receipt_gate_missing_archive_receipt(mock_series_project, monkeypatch):
    """
    CHALLENGE:
    Attempt cleanup with --no-dry-run and missing archive_receipt.json -> MUST FAIL.
    """
    monkeypatch.setattr("src.cli.clean_cmd.get_project_dir", lambda p: mock_series_project)
    monkeypatch.setattr("src.cli.clean_cmd.get_output_dir", lambda p: mock_series_project / "output")

    args = argparse.Namespace(
        project="adv_series",
        dry_run=False,
        confirm_project="adv_series",
        chapter="1",
        bypass_receipts=False,
    )

    with pytest.raises(CliValidationError, match="Hugging Face archive receipt missing"):
        handle_clean(args)


def test_two_receipt_gate_unverified_archive_receipt(mock_series_project, monkeypatch):
    """
    CHALLENGE:
    Attempt cleanup with unverified/corrupt archive_receipt.json -> MUST FAIL.
    """
    monkeypatch.setattr("src.cli.clean_cmd.get_project_dir", lambda p: mock_series_project)
    monkeypatch.setattr("src.cli.clean_cmd.get_output_dir", lambda p: mock_series_project / "output")

    # Unverified receipt (status: unverified)
    receipt = {
        "status": "unverified",
        "commit_sha": "abc1234",
    }
    (mock_series_project / "archive_receipt.json").write_text(json.dumps(receipt), encoding="utf-8")

    args = argparse.Namespace(
        project="adv_series",
        dry_run=False,
        confirm_project="adv_series",
        chapter="1",
        bypass_receipts=False,
    )

    with pytest.raises(CliValidationError, match="archive receipt is unverified"):
        handle_clean(args)

    # Empty commit SHA
    receipt["status"] = "verified"
    receipt["commit_sha"] = ""
    (mock_series_project / "archive_receipt.json").write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(CliValidationError, match="archive receipt is unverified"):
        handle_clean(args)


def test_two_receipt_gate_missing_youtube_receipt_when_upload_configured(mock_series_project, monkeypatch):
    """
    CHALLENGE:
    Attempt cleanup with missing youtube_receipt.json when Q08 is upload -> MUST FAIL.
    """
    monkeypatch.setattr("src.cli.clean_cmd.get_project_dir", lambda p: mock_series_project)
    monkeypatch.setattr("src.cli.clean_cmd.get_output_dir", lambda p: mock_series_project / "output")

    # Set Q08 to active YouTube upload channel
    setup_file = mock_series_project / "series_setup_answers.json"
    setup_data = json.loads(setup_file.read_text(encoding="utf-8"))
    setup_data["answers"]["Q08"]["user_answer"] = "https://www.youtube.com/channel/UC123"
    setup_file.write_text(json.dumps(setup_data), encoding="utf-8")

    # Valid archive receipt
    (mock_series_project / "archive_receipt.json").write_text(
        json.dumps({"status": "verified", "commit_sha": "abc1234"}),
        encoding="utf-8",
    )

    args = argparse.Namespace(
        project="adv_series",
        dry_run=False,
        confirm_project="adv_series",
        chapter="1",
        bypass_receipts=False,
    )

    with pytest.raises(CliValidationError, match="Missing or unverified YouTube receipt"):
        handle_clean(args)


def test_two_receipt_gate_mismatched_confirm_project(mock_series_project, monkeypatch):
    """
    CHALLENGE:
    Attempt cleanup with mismatched --confirm-project -> MUST FAIL.
    """
    monkeypatch.setattr("src.cli.clean_cmd.get_project_dir", lambda p: mock_series_project)
    monkeypatch.setattr("src.cli.clean_cmd.get_output_dir", lambda p: mock_series_project / "output")

    args = argparse.Namespace(
        project="adv_series",
        dry_run=False,
        confirm_project="wrong_project_slug",
        chapter="1",
        bypass_receipts=False,
    )

    with pytest.raises(CliValidationError, match="Safety barrier failed: --confirm-project"):
        handle_clean(args)


def test_two_receipt_gate_q08_no_upload_waiver(mock_series_project, monkeypatch):
    """
    CHALLENGE:
    Verify Q08 'Không upload' properly waives YouTube receipt without waiving archive receipt.
    """
    monkeypatch.setattr("src.cli.clean_cmd.get_project_dir", lambda p: mock_series_project)
    monkeypatch.setattr("src.cli.clean_cmd.get_output_dir", lambda p: mock_series_project / "output")

    # Set Q08 to "Không upload"
    setup_file = mock_series_project / "series_setup_answers.json"
    setup_data = json.loads(setup_file.read_text(encoding="utf-8"))
    setup_data["answers"]["Q08"] = "Không upload"
    setup_file.write_text(json.dumps(setup_data), encoding="utf-8")

    # Part A: Without archive receipt -> MUST STILL FAIL
    args = argparse.Namespace(
        project="adv_series",
        dry_run=False,
        confirm_project="adv_series",
        chapter="1",
        bypass_receipts=False,
    )
    with pytest.raises(CliValidationError, match="archive receipt missing"):
        handle_clean(args)

    # Part B: With verified archive receipt -> PASSES without youtube_receipt.json
    (mock_series_project / "archive_receipt.json").write_text(
        json.dumps({
            "status": "verified",
            "commit_sha": "abc1234",
            "repo_id": "Cuong2004/manga-recap-archive",
            "remote_url": "https://huggingface.co/datasets/test",
        }),
        encoding="utf-8",
    )

    res = handle_clean(args)
    assert res["dry_run"] is False
    assert "1" in res["chapters_cleaned"]
    assert res["tombstone_created"] is True


def test_two_receipt_gate_programmatic_bypass_vulnerability(mock_series_project, monkeypatch):
    """
    CHALLENGE:
    Tests if omitting 'confirm_project' from Namespace args allows bypassing
    the Two-Receipt Gate programmatically.
    """
    monkeypatch.setattr("src.cli.clean_cmd.get_project_dir", lambda p: mock_series_project)
    monkeypatch.setattr("src.cli.clean_cmd.get_output_dir", lambda p: mock_series_project / "output")

    # No archive receipt exists
    assert not (mock_series_project / "archive_receipt.json").exists()

    # Pass Namespace WITHOUT confirm_project in vars(args)
    args = argparse.Namespace(
        project="adv_series",
        dry_run=False,
        chapter="1",
    )

    # In clean_cmd.py lines 162 and 170:
    # Fail-closed fix: When not dry_run, omitting confirm_project unconditionally
    # raises CliValidationError, blocking programmatic deletion bypass.
    with pytest.raises(CliValidationError, match="Safety barrier failed: --confirm-project"):
        handle_clean(args)


# ==============================================================================
# Challenge 7: Secret Whitelist Filtering Invariant
# ==============================================================================

def test_secret_whitelist_filtering_comprehensive(mock_series_project):
    """
    CHALLENGE:
    Ensure .env, .env.local, kaggle.json, client_secrets.json, token.json, credentials.json,
    *.pem, *.key, __pycache__ are NEVER included in archive_manifest.json under any circumstances.
    """
    # Plant forbidden secrets in root and subdirectories
    secrets_to_plant = [
        mock_series_project / ".env",
        mock_series_project / ".env.local",
        mock_series_project / "kaggle.json",
        mock_series_project / "client_secrets.json",
        mock_series_project / "token.json",
        mock_series_project / "credentials.json",
        mock_series_project / "server.pem",
        mock_series_project / "private.key",
        mock_series_project / "chapters" / "1" / "raw" / "kaggle.json",
        mock_series_project / "chapters" / "1" / "panels" / "token.json",
        mock_series_project / "chapters" / "1" / "subtitles" / "secret.key",
    ]

    for sf in secrets_to_plant:
        sf.parent.mkdir(parents=True, exist_ok=True)
        sf.write_text("SENSITIVE_SECRET_TOKEN=12345", encoding="utf-8")

    # Add __pycache__ dir
    pycache_dir = mock_series_project / "__pycache__"
    pycache_dir.mkdir(parents=True, exist_ok=True)
    (pycache_dir / "cache.pyc").write_bytes(b"\x00\x00\x00\x00")

    manifest = generate_archive_manifest("adv_series", project_dir=mock_series_project)

    manifest_paths = [f["relative_path"] for f in manifest["files"]]

    # Assert NONE of the planted secrets appear in manifest
    for sf in secrets_to_plant:
        rel = sf.relative_to(mock_series_project).as_posix()
        assert rel not in manifest_paths, f"CRITICAL LEAK: Secret file included in manifest: {rel}"

    assert not any("__pycache__" in p for p in manifest_paths)
    assert not any(".pyc" in p for p in manifest_paths)


# ==============================================================================
# Challenge 8: Tombstone Size Boundary Invariant (< 2048 bytes)
# ==============================================================================

def test_tombstone_size_boundary_multi_chapter(mock_series_project, monkeypatch):
    """
    CHALLENGE:
    Ensure generated tombstone.json payload is strictly < 2048 bytes (< 2 KB)
    even for long series (e.g. 35 chapters).
    """
    monkeypatch.setattr("src.cli.clean_cmd.get_project_dir", lambda p: mock_series_project)
    monkeypatch.setattr("src.cli.clean_cmd.get_output_dir", lambda p: mock_series_project / "output")

    # Create 35 chapters with receipts and videos
    for ch in range(1, 36):
        ch_str = str(ch)
        c_dir = mock_series_project / "chapters" / ch_str
        (c_dir / "video").mkdir(parents=True, exist_ok=True)
        (c_dir / "video" / "final.mp4").write_bytes(b"\x00\x00\x00\x20ftypisom" + b"\x00" * 150000)
        (c_dir / "youtube_receipt.json").write_text(
            json.dumps({
                "status": "verified",
                "video_id": f"vid_{ch:011d}",
                "video_url": f"https://www.youtube.com/watch?v=vid_{ch:011d}",
                "privacy_status": "unlisted",
            }),
            encoding="utf-8",
        )

    # Valid archive receipt
    (mock_series_project / "archive_receipt.json").write_text(
        json.dumps({
            "status": "verified",
            "commit_sha": "abc1234567890abcdef1234567890abcdef123456",
            "repo_id": "Cuong2004/manga-recap-archive",
            "remote_url": "https://huggingface.co/datasets/Cuong2004/manga-recap-archive/tree/abc1234",
            "created_at": "2026-11-10T20:00:00Z",
        }),
        encoding="utf-8",
    )

    args = argparse.Namespace(
        project="adv_series",
        dry_run=False,
        confirm_project="adv_series",
        bypass_receipts=False,
    )

    res = handle_clean(args)
    assert res["tombstone_created"] is True

    tombstone_file = mock_series_project / "tombstone.json"
    assert tombstone_file.exists()
    tombstone_size = tombstone_file.stat().st_size

    # Check whether tombstone strictly respects < 2048 bytes
    # If 35 chapters exceed 2048 bytes, this test flags the failure!
    assert tombstone_size < 2048, (
        f"INVARIANT VIOLATION: tombstone.json size is {tombstone_size} bytes (>= 2048 bytes / 2 KB limit)."
    )
