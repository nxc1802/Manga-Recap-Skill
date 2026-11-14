"""
tests/unit/test_safe_cleanup.py
Unit and L2 tests for Two-Receipt Safe Cleanup Protocol, ReleaseState.CLEANUP_ALLOWED,
real ffprobe/validate_video + validate_subtitles verification, feature cleanup guards,
bypass removal, and Persistent TombstoneRecord (Manga Recap V4).
"""

from v4_fixtures import init_confirmed_project

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from unit.test_series_setup import _make_valid_answers_dict

from src.cli.batch_cmd import handle_batch
from src.cli.clean_cmd import handle_clean
from src.cli.exit_codes import CliValidationError
from src.cli.main import main
from src.core.contracts import ReleaseState, TombstoneRecord
from src.core.series_setup import record_series_setup
from src.core.state_machine import load_release_record
from src.core.storage import (
    atomic_write_json,
    init_chapter_layout,
)
from src.pipeline import pipeline

SHA40_CLEAN = "a" * 40


def _write_real_mp4(path: Path, duration: float = 0.3, width: int = 160, height: int = 120, fps: int = 30) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=duration={duration}:size={width}x{height}:rate={fps}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={duration}",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ],
        check=True,
    )


@pytest.fixture
def cleanup_project(tmp_path, monkeypatch):
    """Sets up a realistic V4 project layout with real FFmpeg MP4 + SRT for safe cleanup testing."""
    data_dir = tmp_path / "data"
    output_dir = tmp_path / "output"
    monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(data_dir))
    monkeypatch.setenv("MANGA_RECAP_OUTPUT_DIR", str(output_dir))

    slug = "safe_clean_test_series"
    proj_dir = init_confirmed_project(slug, "Safe Clean Test")
    chap_dir = init_chapter_layout(slug, 1)

    # Real verified final video + SRT in output/
    out_proj_dir = output_dir / slug
    out_proj_dir.mkdir(parents=True, exist_ok=True)
    verified_vid = out_proj_dir / "chapter_001.mp4"
    _write_real_mp4(verified_vid)

    verified_srt = out_proj_dir / "chapter_001.srt"
    verified_srt.write_text("1\n00:00:00,000 --> 00:00:00,250\nLời dẫn mở đầu.\n", encoding="utf-8")

    # Intermediates to be cleaned
    a_clip = chap_dir / "audio" / "clips" / "audio_1_001.wav"
    a_clip.parent.mkdir(parents=True, exist_ok=True)
    a_clip.write_bytes(b"RIFF" + b"\x00" * 5000)

    v_clip = chap_dir / "video" / "clips" / "clip_000.mp4"
    v_clip.parent.mkdir(parents=True, exist_ok=True)
    v_clip.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 8000)

    speech_master = chap_dir / "audio" / "speech_master_1.wav"
    speech_master.write_bytes(b"RIFF" + b"\x00" * 9000)

    raw_page = chap_dir / "raw" / "page_001.jpg"
    raw_page.parent.mkdir(parents=True, exist_ok=True)
    raw_page.write_bytes(b"\xff\xd8" + b"\x00" * 1000)

    answers = _make_valid_answers_dict()
    answers["Q08"]["user_answer"] = {"account": "review_truyen", "privacy": "private"}
    record_series_setup(slug, answers, project_dir=proj_dir)

    return {
        "slug": slug,
        "proj_dir": proj_dir,
        "chap_dir": chap_dir,
        "out_proj_dir": out_proj_dir,
        "verified_vid": verified_vid,
        "verified_srt": verified_srt,
        "a_clip": a_clip,
        "v_clip": v_clip,
        "speech_master": speech_master,
        "raw_page": raw_page,
    }


def add_hf_receipt(
    proj_dir: Path,
    slug: str,
    status: str = "verified",
    commit: str = SHA40_CLEAN,
    *,
    restore_verified: bool = True,
    manifest_files: list | None = None,
):
    """Helper to write a canonical archive_manifest.json + archive_receipt.json."""
    manifest_path = proj_dir / "archive_manifest.json"
    atomic_write_json(manifest_path, {
        "schema_version": "4.0.0",
        "project_slug": slug,
        "files": manifest_files or [],
    })
    digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    receipt = {
        "schema_version": "4.0.0",
        "receipt_type": "huggingface_archive",
        "project_slug": slug,
        "repo_id": "Cuong2004/manga-recap-archive",
        "commit_sha": commit,
        "remote_url": f"https://huggingface.co/datasets/Cuong2004/manga-recap-archive/tree/{commit}/series/{slug}",
        "created_at": "2026-11-10T20:00:00Z",
        "verified_file_count": max(1, len(manifest_files or [])),
        "total_bytes": 0,
        "status": status,
        "manifest_sha256": digest,
        "verification_revision": commit,
        "last_verified_at": "2026-11-10T20:01:00Z",
        "restore_verified_at": "2026-11-10T20:02:00Z" if restore_verified else None,
        "restored_revision": commit if restore_verified else None,
        "restored_verified_files": max(1, len(manifest_files or [])) if restore_verified else None,
    }
    atomic_write_json(proj_dir / "archive_receipt.json", receipt)


def add_youtube_receipt(
    chap_dir: Path,
    slug: str,
    chapter: str = "1",
    video_id: str = "yt_test_vid_123",
    *,
    upload_status: str = "processed",
    status: str = "verified",
):
    """Helper to write a canonical YouTubeReceipt-compliant youtube_receipt.json."""
    receipt = {
        "schema_version": "4.0.0",
        "receipt_type": "youtube_verification",
        "project_slug": slug,
        "chapter_number": str(chapter),
        "video_id": video_id,
        "video_url": f"https://www.youtube.com/watch?v={video_id}",
        "account": "review_truyen",
        "channel_title": "Review Truyen",
        "channel_id": "UC_TEST_123",
        "upload_status": upload_status,
        "has_captions": True,
        "has_custom_thumbnail": False,
        "verified_at": "2026-11-10T20:05:00Z" if status == "verified" else None,
        "privacy_status": "private",
        "captions_uploaded": True,
        "thumbnail_uploaded": False,
        "captions_required": True,
        "thumbnail_required": False,
        "uploaded_at": "2026-11-10T20:00:00Z",
        "status": status,
    }
    atomic_write_json(chap_dir / "youtube_receipt.json", receipt)


def add_release_state(proj_dir: Path, slug: str, state: str = "RESTORE_VERIFIED", chapter: str = "1") -> None:
    atomic_write_json(
        proj_dir / "release_state.json",
        {
            "schema_version": "4.1.0",
            "project_slug": slug,
            "chapter_number": chapter,
            "state": state,
            "evidence_hashes": {},
        },
    )


@pytest.mark.skipif(not shutil.which("ffprobe"), reason="FFprobe required for L2 cleanup verification")
class TestDefaultDryRunBehavior:
    """Tests that dry-run is the default safe mode."""

    def test_default_dry_run_in_cli(self, cleanup_project):
        slug = cleanup_project["slug"]
        a_clip = cleanup_project["a_clip"]

        code = main(["clean", "--project", slug, "--json"])
        assert code == 0
        assert a_clip.exists(), "Files must NOT be unlinked in default dry-run mode"


class TestConfirmProjectSafetyBarrier:
    """Tests that --confirm-project <slug> is strictly required for deletion."""

    def test_clean_blocked_when_confirm_project_missing(self, cleanup_project):
        slug = cleanup_project["slug"]
        proj_dir = cleanup_project["proj_dir"]
        chap_dir = cleanup_project["chap_dir"]

        add_hf_receipt(proj_dir, slug)
        add_youtube_receipt(chap_dir, slug)

        args = argparse.Namespace(project=slug, chapter=None, dry_run=False, confirm_project=None)
        with pytest.raises(CliValidationError, match="Safety barrier failed: --confirm-project"):
            handle_clean(args)

    def test_clean_blocked_when_confirm_project_mismatched(self, cleanup_project):
        slug = cleanup_project["slug"]
        proj_dir = cleanup_project["proj_dir"]
        chap_dir = cleanup_project["chap_dir"]

        add_hf_receipt(proj_dir, slug)
        add_youtube_receipt(chap_dir, slug)

        args = argparse.Namespace(project=slug, chapter=None, dry_run=False, confirm_project="wrong_series_slug")
        with pytest.raises(CliValidationError, match="Safety barrier failed"):
            handle_clean(args)


@pytest.mark.skipif(not shutil.which("ffprobe"), reason="FFprobe required for L2 cleanup verification")
class TestTwoReceiptGateEnforcement:
    """Tests that deletion is blocked unless ArchiveReceipt, YouTubeReceipt, and ReleaseState gates pass."""

    def test_blocks_if_hf_receipt_missing(self, cleanup_project):
        slug = cleanup_project["slug"]
        chap_dir = cleanup_project["chap_dir"]
        add_youtube_receipt(chap_dir, slug)

        args = argparse.Namespace(project=slug, chapter=1, dry_run=False, confirm_project=slug)
        with pytest.raises(CliValidationError, match="Hugging Face archive receipt missing"):
            handle_clean(args)

    def test_blocks_if_hf_receipt_unverified(self, cleanup_project):
        slug = cleanup_project["slug"]
        proj_dir = cleanup_project["proj_dir"]
        chap_dir = cleanup_project["chap_dir"]

        add_hf_receipt(proj_dir, slug, status="pending", commit="")
        add_youtube_receipt(chap_dir, slug)

        args = argparse.Namespace(project=slug, chapter=1, dry_run=False, confirm_project=slug)
        with pytest.raises(CliValidationError, match="Hugging Face archive receipt is unverified"):
            handle_clean(args)

    def test_blocks_if_restore_not_verified(self, cleanup_project):
        slug = cleanup_project["slug"]
        proj_dir = cleanup_project["proj_dir"]
        chap_dir = cleanup_project["chap_dir"]

        add_hf_receipt(proj_dir, slug, restore_verified=False)
        add_youtube_receipt(chap_dir, slug)

        args = argparse.Namespace(project=slug, chapter=1, dry_run=False, confirm_project=slug)
        with pytest.raises(CliValidationError, match="restore test"):
            handle_clean(args)

    def test_blocks_if_project_state_drifted_after_archive(self, cleanup_project):
        slug = cleanup_project["slug"]
        proj_dir = cleanup_project["proj_dir"]
        chap_dir = cleanup_project["chap_dir"]

        proj_json = proj_dir / "project.json"
        orig_sha = hashlib.sha256(proj_json.read_bytes()).hexdigest()
        add_hf_receipt(
            proj_dir,
            slug,
            manifest_files=[
                {
                    "relative_path": "project.json",
                    "repo_path": f"series/{slug}/project.json",
                    "sha256": orig_sha,
                    "size_bytes": proj_json.stat().st_size,
                    "role": "metadata",
                }
            ],
        )
        add_youtube_receipt(chap_dir, slug)

        # Mutate project.json after archive verification
        proj_json.write_text(proj_json.read_text(encoding="utf-8") + "\n", encoding="utf-8")

        args = argparse.Namespace(project=slug, chapter=1, dry_run=False, confirm_project=slug)
        with pytest.raises(CliValidationError, match="drifted after archive"):
            handle_clean(args)

    def test_blocks_if_youtube_receipt_missing_or_unprocessed(self, cleanup_project):
        slug = cleanup_project["slug"]
        proj_dir = cleanup_project["proj_dir"]
        chap_dir = cleanup_project["chap_dir"]
        add_hf_receipt(proj_dir, slug)

        args = argparse.Namespace(project=slug, chapter=1, dry_run=False, confirm_project=slug)
        with pytest.raises(CliValidationError, match="Missing or unverified YouTube receipt"):
            handle_clean(args)

        add_youtube_receipt(chap_dir, slug, upload_status="uploaded")
        with pytest.raises(CliValidationError, match="Missing or unverified YouTube receipt"):
            handle_clean(args)

    def test_blocks_if_release_state_not_cleanup_allowed(self, cleanup_project):
        slug = cleanup_project["slug"]
        proj_dir = cleanup_project["proj_dir"]
        chap_dir = cleanup_project["chap_dir"]

        add_hf_receipt(proj_dir, slug)
        add_youtube_receipt(chap_dir, slug)
        add_release_state(proj_dir, slug, state="ARCHIVE_VERIFIED")

        args = argparse.Namespace(project=slug, chapter=1, dry_run=False, confirm_project=slug)
        with pytest.raises(CliValidationError, match="CLEANUP_ALLOWED"):
            handle_clean(args)

    def test_youtube_receipt_waived_when_no_upload_chosen(self, cleanup_project):
        slug = cleanup_project["slug"]
        proj_dir = cleanup_project["proj_dir"]
        a_clip = cleanup_project["a_clip"]

        answers = _make_valid_answers_dict()
        answers["Q08"]["user_answer"] = "Không upload"
        record_series_setup(slug, answers, project_dir=proj_dir)

        add_hf_receipt(proj_dir, slug)

        args = argparse.Namespace(project=slug, chapter=1, dry_run=False, confirm_project=slug)
        res = handle_clean(args)
        assert res["cleaned_files_count"] >= 3
        assert not a_clip.exists()
        assert res["tombstone_created"] is True


@pytest.mark.skipif(not shutil.which("ffprobe"), reason="FFprobe required for L2 media validation")
class TestMediaValidationAndFeatureGuards:
    """Tests ffprobe/validate_video + validate_subtitles enforcement and project-level feature guards."""

    def test_skips_chapter_with_corrupt_mp4_failing_ffprobe(self, cleanup_project):
        slug = cleanup_project["slug"]
        proj_dir = cleanup_project["proj_dir"]
        chap_dir = cleanup_project["chap_dir"]
        verified_vid = cleanup_project["verified_vid"]
        a_clip = cleanup_project["a_clip"]

        # Overwrite valid MP4 with fake bytes > 100KB (would pass old st_size check, must fail ffprobe)
        verified_vid.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 150000)

        add_hf_receipt(proj_dir, slug)
        add_youtube_receipt(chap_dir, slug)

        args = argparse.Namespace(project=slug, chapter=1, dry_run=False, confirm_project=slug)
        res = handle_clean(args)

        assert res["chapters_cleaned"] == []
        assert len(res["skipped_chapters"]) == 1
        assert "ffprobe" in res["skipped_chapters"][0]["reason"]
        assert a_clip.exists(), "Intermediates must NOT be deleted when output video fails ffprobe"

    def test_skips_chapter_with_invalid_srt(self, cleanup_project):
        slug = cleanup_project["slug"]
        proj_dir = cleanup_project["proj_dir"]
        chap_dir = cleanup_project["chap_dir"]
        verified_srt = cleanup_project["verified_srt"]
        a_clip = cleanup_project["a_clip"]

        verified_srt.write_text("CORRUPT SRT CONTENT WITHOUT TIMESTAMPS", encoding="utf-8")

        add_hf_receipt(proj_dir, slug)
        add_youtube_receipt(chap_dir, slug)

        args = argparse.Namespace(project=slug, chapter=1, dry_run=False, confirm_project=slug)
        res = handle_clean(args)

        assert res["chapters_cleaned"] == []
        assert "Subtitle validation failed" in res["skipped_chapters"][0]["reason"]
        assert a_clip.exists()

    def test_feature_artifacts_preserved_on_single_chapter_or_missing_feature_receipt(self, cleanup_project):
        slug = cleanup_project["slug"]
        proj_dir = cleanup_project["proj_dir"]
        chap_dir = cleanup_project["chap_dir"]
        out_proj_dir = cleanup_project["out_proj_dir"]

        proj_audio_final = proj_dir / "audio" / "final.wav"
        proj_audio_final.parent.mkdir(parents=True, exist_ok=True)
        proj_audio_final.write_bytes(b"RIFF" + b"\x00" * 1000)
        full_wav = out_proj_dir / f"{slug}_full_recap.wav"
        full_wav.write_bytes(b"RIFF" + b"\x00" * 1000)

        add_hf_receipt(proj_dir, slug)
        add_youtube_receipt(chap_dir, slug)

        # Case 1: Single-chapter -c 1 must NOT delete project-level feature artifacts
        handle_clean(argparse.Namespace(project=slug, chapter=1, dry_run=False, confirm_project=slug))
        assert proj_audio_final.exists()
        assert full_wav.exists()

        # Case 2: Full-project clean without feature.youtube_receipt.json must STILL preserve feature artifacts
        handle_clean(argparse.Namespace(project=slug, chapter=None, dry_run=False, confirm_project=slug))
        assert proj_audio_final.exists()
        assert full_wav.exists()

        # Case 3: With verified feature.youtube_receipt.json, full-project clean reclaims them
        add_youtube_receipt(out_proj_dir, slug, chapter="feature", video_id="yt_feature_1")
        (out_proj_dir / "youtube_receipt.json").rename(out_proj_dir / "feature.youtube_receipt.json")
        handle_clean(argparse.Namespace(project=slug, chapter=None, dry_run=False, confirm_project=slug))
        assert not proj_audio_final.exists()
        assert not full_wav.exists()


@pytest.mark.skipif(not shutil.which("ffprobe"), reason="FFprobe required for L2 cleanup verification")
class TestSuccessfulCleanupAndTombstone:
    """Tests complete successful cleanup execution, ReleaseState transition, and TombstoneRecord."""

    def test_successful_cleanup_with_both_receipts_and_tombstone_contract(self, cleanup_project):
        slug = cleanup_project["slug"]
        proj_dir = cleanup_project["proj_dir"]
        chap_dir = cleanup_project["chap_dir"]
        a_clip = cleanup_project["a_clip"]
        v_clip = cleanup_project["v_clip"]
        speech_master = cleanup_project["speech_master"]
        verified_vid = cleanup_project["verified_vid"]

        commit_hex = "c" * 40
        add_hf_receipt(proj_dir, slug, commit=commit_hex)
        add_youtube_receipt(chap_dir, slug, video_id="yt_vid_success_999")
        add_release_state(proj_dir, slug, state="RESTORE_VERIFIED")

        args = argparse.Namespace(project=slug, chapter=1, dry_run=False, confirm_project=slug)
        res = handle_clean(args)

        assert res["cleaned_files_count"] >= 3
        assert not a_clip.exists()
        assert not v_clip.exists()
        assert not speech_master.exists()
        assert verified_vid.exists(), "Verified final video must be preserved!"
        assert load_release_record(proj_dir).state == ReleaseState.CLEANUP_ALLOWED

        tombstone_file = proj_dir / "tombstone.json"
        assert tombstone_file.exists()
        assert tombstone_file.stat().st_size < 2048

        t_record = TombstoneRecord.model_validate(json.loads(tombstone_file.read_text(encoding="utf-8")))
        assert t_record.tombstone_version == "4.0.0"
        assert t_record.project_slug == slug
        assert t_record.hf_commit_sha == commit_hex
        assert t_record.manifest_sha256 == json.loads((proj_dir / "archive_receipt.json").read_text())["manifest_sha256"]
        assert t_record.youtube_videos["1"] == "yt_vid_success_999"
        assert t_record.reclaimed_bytes > 0

    def test_clean_with_purge_raw_flag(self, cleanup_project):
        slug = cleanup_project["slug"]
        proj_dir = cleanup_project["proj_dir"]
        chap_dir = cleanup_project["chap_dir"]
        raw_page = cleanup_project["raw_page"]

        add_hf_receipt(proj_dir, slug)
        add_youtube_receipt(chap_dir, slug)

        res = handle_clean(argparse.Namespace(project=slug, chapter=1, dry_run=False, confirm_project=slug, purge_raw=True))
        assert res["cleaned_files_count"] >= 4
        assert not raw_page.exists()
        assert (proj_dir / "archive_receipt.json").exists()
        assert (proj_dir / "tombstone.json").exists()

    def test_cli_end_to_end_invocation(self, cleanup_project):
        slug = cleanup_project["slug"]
        proj_dir = cleanup_project["proj_dir"]
        chap_dir = cleanup_project["chap_dir"]

        add_hf_receipt(proj_dir, slug)
        add_youtube_receipt(chap_dir, slug)

        code = main(["clean", "--project", slug, "--chapter", "1", "--no-dry-run", "--confirm-project", slug, "--json"])
        assert code == 0
        assert (proj_dir / "tombstone.json").exists()


class TestBypassPathsRemoved:
    """Tests that batch --cleanup and pipeline.cleanup_temp_files cannot bypass Two-Receipt Gate."""

    def test_batch_cleanup_flag_rejected(self, cleanup_project):
        slug = cleanup_project["slug"]
        proj_dir = cleanup_project["proj_dir"]
        with patch("src.core.human_review.require_preview_approval"):
            with pytest.raises(CliValidationError, match="Two-Receipt Safe Cleanup Gate"):
                handle_batch(argparse.Namespace(project=slug, chapters="1", cleanup=True))
        assert (cleanup_project["a_clip"]).exists()

    def test_pipeline_cleanup_temp_files_blocks_without_receipts(self, cleanup_project):
        slug = cleanup_project["slug"]
        chap_dir = cleanup_project["chap_dir"]
        temp_f = chap_dir / "chapter_1_raw_temp.mp4"
        temp_f.write_bytes(b"temp")

        with pytest.raises(CliValidationError, match="Two-Receipt Gate failed"):
            pipeline.cleanup_temp_files(chapter="1", project=slug, keep_temp=False)
        assert temp_f.exists()

    @pytest.mark.skipif(not shutil.which("ffprobe"), reason="FFprobe required")
    def test_pipeline_cleanup_temp_files_succeeds_only_after_two_receipt_gate(self, cleanup_project):
        slug = cleanup_project["slug"]
        proj_dir = cleanup_project["proj_dir"]
        chap_dir = cleanup_project["chap_dir"]
        temp_f = chap_dir / "chapter_1_raw_temp.mp4"
        temp_f.write_bytes(b"temp")

        add_hf_receipt(proj_dir, slug)
        add_youtube_receipt(chap_dir, slug)
        add_release_state(proj_dir, slug, state="RESTORE_VERIFIED")

        cleaned = pipeline.cleanup_temp_files(chapter="1", project=slug, keep_temp=False)
        assert "chapter_1_raw_temp.mp4" in cleaned
        assert not temp_f.exists()
        assert load_release_record(proj_dir).state == ReleaseState.CLEANUP_ALLOWED
