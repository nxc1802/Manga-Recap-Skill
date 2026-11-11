"""
tests/unit/test_safe_cleanup.py
Unit tests for Two-Receipt Safe Cleanup Protocol & Persistent Tombstone (Manga Recap V4).
Tests:
- Two-receipt gate enforcement: blocks if HF receipt missing, blocks if YouTube receipt missing.
- Default dry-run behavior (no unlinking without explicit confirmation).
- `--confirm-project` safety barrier.
- YouTube waiver when user explicitly selected 'Không upload'.
- `tombstone.json` generation, contents, and <2 KB size guarantee.
- Scoped intermediate cleanup and optional raw purge.
"""

from v4_fixtures import init_confirmed_project

import argparse
import hashlib
import json
from pathlib import Path
import pytest

from src.cli.clean_cmd import handle_clean
from src.cli.exit_codes import CliValidationError
from src.cli.main import main
from src.core.storage import (
    atomic_write_json,
    init_chapter_layout,
    init_project_layout,
)


@pytest.fixture
def cleanup_project(tmp_path, monkeypatch):
    """Sets up a realistic project layout for safe cleanup testing."""
    data_dir = tmp_path / "data"
    output_dir = tmp_path / "output"
    monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(data_dir))
    monkeypatch.setenv("MANGA_RECAP_OUTPUT_DIR", str(output_dir))

    slug = "safe_clean_test_series"
    proj_dir = init_confirmed_project(slug, "Safe Clean Test")
    chap_dir = init_chapter_layout(slug, 1)

    # Output verified final video
    out_proj_dir = output_dir / slug
    out_proj_dir.mkdir(parents=True, exist_ok=True)
    verified_vid = out_proj_dir / "chapter_001.mp4"
    verified_vid.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 150000)

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

    # Full explicit unit setup; receipts are isolated protocol fixtures.
    from unit.test_series_setup import _make_valid_answers_dict
    from src.core.series_setup import record_series_setup
    answers = _make_valid_answers_dict()
    answers['Q08']['user_answer'] = {'account': 'review_truyen', 'privacy': 'private'}
    record_series_setup(slug, answers, project_dir=proj_dir)

    return {
        "slug": slug,
        "proj_dir": proj_dir,
        "chap_dir": chap_dir,
        "out_proj_dir": out_proj_dir,
        "verified_vid": verified_vid,
        "a_clip": a_clip,
        "v_clip": v_clip,
        "speech_master": speech_master,
        "raw_page": raw_page,
    }


def add_hf_receipt(proj_dir: Path, slug: str, status: str = "verified", commit: str = "sha_123456"):
    """Helper to write a mock archive_receipt.json."""
    # A complete mocked archive verification + restore proof is necessary
    # before cleanup. These are test receipts; they do not hit HF API.
    manifest_path = proj_dir / "archive_manifest.json"
    atomic_write_json(manifest_path, {
        "schema_version": "4.0.0",
        "project_slug": slug,
        "files": [],
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
        "status": status,
        "manifest_sha256": digest,
        "verification_revision": commit,
        "restored_revision": commit,
        "last_verified_at": "2026-11-10T20:01:00Z",
        "restore_verified_at": "2026-11-10T20:02:00Z",
        "restored_verified_files": 0,
    }
    atomic_write_json(proj_dir / "archive_receipt.json", receipt)


def add_youtube_receipt(chap_dir: Path, slug: str, chapter: str = "1", video_id: str = "yt_test_vid_123"):
    """Helper to write a mock youtube_receipt.json."""
    receipt = {
        "schema_version": "4.0.0",
        "receipt_type": "youtube_verification",
        "project_slug": slug,
        "chapter_number": chapter,
        "video_id": video_id,
        "video_url": f"https://www.youtube.com/watch?v={video_id}",
        "account": "review_truyen",
        "privacy_status": "private",
        "upload_status": "processed",
        "status": "verified",
        "verified_at": "2026-11-10T20:05:00Z",
    }
    atomic_write_json(chap_dir / "youtube_receipt.json", receipt)


class TestDefaultDryRunBehavior:
    """Tests that dry-run is the default safe mode."""

    def test_default_dry_run_in_cli(self, cleanup_project):
        slug = cleanup_project["slug"]
        a_clip = cleanup_project["a_clip"]

        # Run clean without --no-dry-run
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

        args = argparse.Namespace(
            project=slug,
            chapter=None,
            dry_run=False,
            confirm_project=None,
        )
        with pytest.raises(CliValidationError) as exc:
            handle_clean(args)
        assert "Safety barrier failed: --confirm-project" in str(exc.value)

    def test_clean_blocked_when_confirm_project_mismatched(self, cleanup_project):
        slug = cleanup_project["slug"]
        proj_dir = cleanup_project["proj_dir"]
        chap_dir = cleanup_project["chap_dir"]

        add_hf_receipt(proj_dir, slug)
        add_youtube_receipt(chap_dir, slug)

        args = argparse.Namespace(
            project=slug,
            chapter=None,
            dry_run=False,
            confirm_project="wrong_series_slug",
        )
        with pytest.raises(CliValidationError) as exc:
            handle_clean(args)
        assert "Safety barrier failed" in str(exc.value)


class TestTwoReceiptGateEnforcement:
    """Tests that deletion is blocked unless both HF and YouTube receipts are verified."""

    def test_blocks_if_hf_receipt_missing(self, cleanup_project):
        slug = cleanup_project["slug"]
        chap_dir = cleanup_project["chap_dir"]
        add_youtube_receipt(chap_dir, slug)

        args = argparse.Namespace(
            project=slug,
            chapter=1,
            dry_run=False,
            confirm_project=slug,
        )
        with pytest.raises(CliValidationError) as exc:
            handle_clean(args)
        assert "Hugging Face archive receipt missing" in str(exc.value)

    def test_blocks_if_hf_receipt_unverified(self, cleanup_project):
        slug = cleanup_project["slug"]
        proj_dir = cleanup_project["proj_dir"]
        chap_dir = cleanup_project["chap_dir"]

        add_hf_receipt(proj_dir, slug, status="pending", commit="")
        add_youtube_receipt(chap_dir, slug)

        args = argparse.Namespace(
            project=slug,
            chapter=1,
            dry_run=False,
            confirm_project=slug,
        )
        with pytest.raises(CliValidationError) as exc:
            handle_clean(args)
        assert "Hugging Face archive receipt is unverified" in str(exc.value)

    def test_blocks_if_youtube_receipt_missing_when_upload_configured(self, cleanup_project):
        slug = cleanup_project["slug"]
        proj_dir = cleanup_project["proj_dir"]
        add_hf_receipt(proj_dir, slug)

        args = argparse.Namespace(
            project=slug,
            chapter=1,
            dry_run=False,
            confirm_project=slug,
        )
        with pytest.raises(CliValidationError) as exc:
            handle_clean(args)
        assert "Missing or unverified YouTube receipt" in str(exc.value)

    def test_youtube_receipt_waived_when_no_upload_chosen(self, cleanup_project):
        slug = cleanup_project["slug"]
        proj_dir = cleanup_project["proj_dir"]
        a_clip = cleanup_project["a_clip"]

        # User explicitly configured 'Không upload'
        setup_answers = {
            "schema_version": "4.0.0",
            "project_slug": slug,
            "answers": {
                "Q08": {
                    "question_id": "Q08",
                    "question_title": "Kênh YouTube & Hiển thị",
                    "user_answer": "Không upload",
                    "answer_source": "user",
                    "confirmed_at": "2026-11-10T20:00:00Z",
                }
            },
        }
        atomic_write_json(proj_dir / "series_setup_answers.json", setup_answers)

        add_hf_receipt(proj_dir, slug)
        # Note: NO youtube_receipt added!

        args = argparse.Namespace(
            project=slug,
            chapter=1,
            dry_run=False,
            confirm_project=slug,
        )
        res = handle_clean(args)
        assert res["cleaned_files_count"] >= 3
        assert not a_clip.exists()
        assert res["tombstone_created"] is True


class TestSuccessfulCleanupAndTombstone:
    """Tests complete successful cleanup execution and persistent tombstone.json."""

    def test_successful_cleanup_with_both_receipts(self, cleanup_project):
        slug = cleanup_project["slug"]
        proj_dir = cleanup_project["proj_dir"]
        chap_dir = cleanup_project["chap_dir"]
        a_clip = cleanup_project["a_clip"]
        v_clip = cleanup_project["v_clip"]
        speech_master = cleanup_project["speech_master"]
        verified_vid = cleanup_project["verified_vid"]

        add_hf_receipt(proj_dir, slug, commit="git_commit_abcdef123")
        add_youtube_receipt(chap_dir, slug, video_id="yt_vid_success_999")

        args = argparse.Namespace(
            project=slug,
            chapter=1,
            dry_run=False,
            confirm_project=slug,
        )
        res = handle_clean(args)

        assert res["cleaned_files_count"] >= 3
        assert not a_clip.exists()
        assert not v_clip.exists()
        assert not speech_master.exists()
        assert verified_vid.exists(), "Verified final video must be preserved!"

        # Verify persistent tombstone.json
        tombstone_file = proj_dir / "tombstone.json"
        assert tombstone_file.exists()
        assert tombstone_file.stat().st_size < 2048, "Tombstone must be < 2 KB"

        t_data = json.loads(tombstone_file.read_text(encoding="utf-8"))
        assert t_data["tombstone_version"] == "4.0.0"
        assert t_data["project_slug"] == slug
        assert t_data["hf_commit_sha"] == "git_commit_abcdef123"
        assert "1" in t_data["youtube_videos"]
        yt_entry = t_data["youtube_videos"]["1"]
        assert (yt_entry["video_id"] if isinstance(yt_entry, dict) else yt_entry) == "yt_vid_success_999"
        assert t_data["reclaimed_bytes"] > 0

    def test_clean_with_purge_raw_flag(self, cleanup_project):
        slug = cleanup_project["slug"]
        proj_dir = cleanup_project["proj_dir"]
        chap_dir = cleanup_project["chap_dir"]
        raw_page = cleanup_project["raw_page"]

        add_hf_receipt(proj_dir, slug)
        add_youtube_receipt(chap_dir, slug)

        assert raw_page.exists()

        args = argparse.Namespace(
            project=slug,
            chapter=1,
            dry_run=False,
            confirm_project=slug,
            purge_raw=True,
        )
        res = handle_clean(args)

        assert not raw_page.exists(), "Raw page should be purged when --purge-raw is specified"
        assert (proj_dir / "archive_receipt.json").exists(), "Archive receipt must survive cleanup"
        assert (proj_dir / "tombstone.json").exists(), "Tombstone must survive cleanup"

    def test_cli_end_to_end_invocation(self, cleanup_project):
        slug = cleanup_project["slug"]
        proj_dir = cleanup_project["proj_dir"]
        chap_dir = cleanup_project["chap_dir"]

        add_hf_receipt(proj_dir, slug)
        add_youtube_receipt(chap_dir, slug)

        code = main([
            "clean",
            "--project", slug,
            "--no-dry-run",
            "--confirm-project", slug,
            "--json",
        ])
        assert code == 0
        assert (proj_dir / "tombstone.json").exists()
