"""
tests/unit/test_clean_cmd.py
Unit tests for manga-recap clean subcommand and intermediate storage reclamation.
"""

import argparse
from pathlib import Path
import pytest

from src.cli.clean_cmd import handle_clean
from src.cli.main import main
from src.core.storage import (
    init_chapter_layout,
    init_project_layout,
    safe_cleanup_chapter_intermediates,
)


def test_clean_cmd_skips_unverified_chapters(tmp_path, monkeypatch):
    """Verifies that clean_cmd does NOT delete assets for chapters without verified output videos."""
    monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MANGA_RECAP_OUTPUT_DIR", str(tmp_path / "output"))

    slug = "clean_test_unverified"
    init_project_layout(slug, "Unverified Project")
    chap_dir = init_chapter_layout(slug, 1)

    # Create dummy audio clip
    clip = chap_dir / "audio" / "clips" / "audio_1_001.wav"
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b"RIFF" + b"\x00" * 100)

    args = argparse.Namespace(project=slug, chapter=None, dry_run=False, confirm_project=slug, bypass_receipts=True)
    res = handle_clean(args)

    assert res["cleaned_files_count"] == 0
    assert len(res["skipped_chapters"]) == 1
    assert clip.exists(), "Audio clip must NOT be deleted without verified final video"


def test_clean_cmd_reclaims_intermediates_when_output_verified(tmp_path, monkeypatch):
    """Verifies that clean_cmd safely unlinks intermediate audio/video clips once final video exists."""
    monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))
    out_dir = tmp_path / "output"
    monkeypatch.setenv("MANGA_RECAP_OUTPUT_DIR", str(out_dir))

    slug = "clean_test_verified"
    proj_dir = init_project_layout(slug, "Verified Project")
    chap_dir = init_chapter_layout(slug, 1)

    # Create verified final video in output
    out_proj_dir = out_dir / slug
    out_proj_dir.mkdir(parents=True, exist_ok=True)
    verified_vid = out_proj_dir / "chapter_001.mp4"
    verified_vid.write_bytes(b"V" * 150000)

    # Create intermediate audio and video clips
    a_clip = chap_dir / "audio" / "clips" / "audio_1_001.wav"
    a_clip.parent.mkdir(parents=True, exist_ok=True)
    a_clip.write_bytes(b"RIFF" + b"\x00" * 5000)

    speech_master = chap_dir / "audio" / "speech_master_1.wav"
    speech_master.write_bytes(b"RIFF" + b"\x00" * 8000)

    v_clip = chap_dir / "video" / "clips" / "clip_000.mp4"
    v_clip.parent.mkdir(parents=True, exist_ok=True)
    v_clip.write_bytes(b"VID" * 1000)

    raw_vid = chap_dir / "video" / "raw_video_1.mp4"
    raw_vid.write_bytes(b"RAW" * 1000)

    # Dry-run first
    args_dry = argparse.Namespace(project=slug, chapter=1, dry_run=True)
    res_dry = handle_clean(args_dry)
    assert res_dry["dry_run"] is True
    assert res_dry["cleaned_files_count"] >= 4
    assert a_clip.exists()

    # Actual cleanup
    args_real = argparse.Namespace(project=slug, chapter=1, dry_run=False, confirm_project=slug, bypass_receipts=True)
    res_real = handle_clean(args_real)
    assert res_real["dry_run"] is False
    assert res_real["cleaned_files_count"] >= 4
    assert not a_clip.exists()
    assert not speech_master.exists()
    assert not v_clip.exists()
    assert not raw_vid.exists()
    assert verified_vid.exists()


def test_clean_cmd_cli_invocation(tmp_path, monkeypatch):
    """Verifies that manga-recap clean --project <slug> runs cleanly from main CLI."""
    monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MANGA_RECAP_OUTPUT_DIR", str(tmp_path / "output"))

    slug = "clean_test_cli"
    init_project_layout(slug, "CLI Clean")
    init_chapter_layout(slug, 1)

    code = main(["clean", "--project", slug, "--dry-run", "--json"])
    assert code == 0
