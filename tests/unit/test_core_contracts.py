"""Tests for Phase 1 core contracts, error decoupling, project store, checkpoints, and cleanup."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.cli.exit_codes import CliValidationError
from src.core.checkpoints import (
    get_resumable_stage,
    record_chapter_render_result,
    update_checkpoint,
)
from src.core.cleanup import clean_project_chapters
from src.core.errors import DomainValidationError, StorageCorruptError
from src.core.project import (
    init_chapter_layout,
    init_project_layout,
    load_chapter_metadata,
    load_project_config,
)
from src.core.schemas import CheckpointStatus, HumanReviewStatus, StageType
from src.core.storage import atomic_write_json, read_json
from src.core.version import APP_VERSION, PRODUCTION_SCHEMA_VERSION, PROJECT_SCHEMA_VERSION, SETUP_SCHEMA_VERSION


def test_domain_validation_error_decoupled_from_cli():
    err = DomainValidationError("invalid gate", details=["missing Q01"])
    assert isinstance(err, CliValidationError)
    assert CliValidationError is DomainValidationError

    core_dir = Path("src/core")
    for py_file in core_dir.rglob("*.py"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(not alias.name.startswith("src.cli") for alias in node.names), str(py_file)
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("src.cli"), str(py_file)


def test_app_version_distinguished_from_schema_versions():
    assert APP_VERSION == "5.0.0"
    assert PROJECT_SCHEMA_VERSION == "3.0.0"
    assert PRODUCTION_SCHEMA_VERSION == "4.1.0"
    assert SETUP_SCHEMA_VERSION == "5.0.0"


def test_project_store_and_atomic_corrupt_preservation(tmp_path, monkeypatch):
    monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path))
    proj = init_project_layout("demo_proj", "Demo Project")
    chap = init_chapter_layout("demo_proj", "1")
    cfg = load_project_config(proj)
    assert cfg.slug == "demo_proj"
    assert cfg.schema_version == PROJECT_SCHEMA_VERSION

    meta_file = chap / "chapter.json"
    atomic_write_json(meta_file, load_chapter_metadata(chap))
    assert get_resumable_stage(chap) == StageType.CRAWL

    update_checkpoint(chap, StageType.RENDER, CheckpointStatus.COMPLETED)
    updated = record_chapter_render_result(
        chap,
        quality="preview",
        promote=False,
        video_path=chap / "video" / "preview.mp4",
        duration_seconds=12.5,
    )
    assert updated.overall_status == CheckpointStatus.NEEDS_REVIEW
    assert updated.human_review_status == HumanReviewStatus.UNREVIEWED
    assert updated.stats.final_video_duration_seconds == 12.5

    corrupt = tmp_path / "bad.json"
    corrupt.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(StorageCorruptError):
        read_json(corrupt)


def test_cleanup_refuses_bypass_receipts_and_running_stage(tmp_path, monkeypatch):
    monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path))
    init_project_layout("series", "Series")
    chap = init_chapter_layout("series", "1")
    (chap / "video" / "final.mp4").write_bytes(b"mp4")
    (chap / "subtitles" / "final.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n", encoding="utf-8")
    clip = chap / "audio" / "clips" / "c1.wav"
    clip.write_bytes(b"wav")

    with pytest.raises(DomainValidationError, match="bypass-receipts"):
        clean_project_chapters("series", dry_run=False, confirm_project="series", bypass_receipts=True)

    update_checkpoint(chap, StageType.AUDIO, CheckpointStatus.RUNNING)
    res = clean_project_chapters("series", chapter="1", dry_run=False, confirm_project="series")
    assert res["skipped_chapters"] and res["cleaned_files_count"] == 0
    assert clip.is_file()
