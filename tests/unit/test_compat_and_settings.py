"""Unit tests for Phase 2 legacy migration, named series setup, runtime settings, and dependency sync."""

from __future__ import annotations

import csv
import subprocess
import sys
from pathlib import Path

import pytest

from src.compat.migrate import (
    migrate_project_artifacts,
    normalize_chapter_data,
    normalize_legacy_script_data,
    normalize_stage_type,
    parse_legacy_script_csv,
)
from src.core.errors import DomainValidationError, ProviderAuthError
from src.core.project import init_chapter_layout, init_project_layout
from src.core.schemas import StageType
from src.core.series_setup import (
    extract_named_settings,
    record_series_setup,
    require_series_setup,
)
from src.core.settings import (
    get_runtime_settings,
    resolve_gemini_api_key,
    resolve_hf_token,
    resolve_kaggle_legacy_token,
)
from src.core.storage import atomic_write_json, read_json


def test_named_setup_settings_equivalent_to_q_ids(tmp_path):
    named = {
        "source": {"user_answer": {"chapters": "1-3", "source": "https://example.com/series"}, "answer_source": "user"},
        "compute": {"user_answer": "local", "answer_source": "user"},
        "voice": {"user_answer": "Ngọc Huyền", "answer_source": "user"},
        "speed": {"user_answer": 1.1, "answer_source": "user"},
        "subtitles": {"user_answer": True, "answer_source": "user"},
        "publish": {"user_answer": {"no_upload": True}, "answer_source": "user"},
        "quality": {"user_answer": "final", "answer_source": "user"},
        "audio": {"user_answer": False, "answer_source": "user"},
    }
    setup = record_series_setup("named_series", named, project_dir=tmp_path)
    loaded = require_series_setup("named_series", project_dir=tmp_path)
    assert loaded == setup
    extracted = extract_named_settings(loaded)
    assert extracted["compute"] == "local"
    assert extracted["voice"] == "Ngọc Huyền"
    assert extracted["subtitles"] is True
    assert extracted["quality"] == "final"

    conflicting = dict(named)
    conflicting["Q02"] = {"user_answer": "kaggle", "answer_source": "user"}
    with pytest.raises(DomainValidationError, match="Conflicting"):
        record_series_setup("conflict_series", conflicting, project_dir=tmp_path / "conflict")


def test_runtime_settings_and_credential_resolution(tmp_path, monkeypatch):
    monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "custom_data"))
    monkeypatch.setenv("MANGA_RECAP_OUTPUT_DIR", str(tmp_path / "custom_out"))
    monkeypatch.setenv("YOUTUBE_ACCOUNTS_DIR", str(tmp_path / "yt"))
    monkeypatch.setenv("LOG_LEVEL", "debug")
    monkeypatch.setenv("VIENEU_DEVICE", "cpu")
    settings = get_runtime_settings()
    assert settings.projects_dir == tmp_path / "custom_data" / "projects"
    assert settings.output_dir == tmp_path / "custom_out"
    assert settings.youtube_accounts_dir == tmp_path / "yt"
    assert settings.log_level == "DEBUG"
    assert settings.vieneu_device == "cpu"

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(ProviderAuthError):
        resolve_gemini_api_key()
    assert resolve_gemini_api_key("explicit-key") == "explicit-key"

    monkeypatch.setenv("HF_TOKEN", "hf_test_123")
    assert resolve_hf_token() == "hf_test_123"
    monkeypatch.setenv("KAGGLE_API_TOKEN", "KGAT_abc")
    assert resolve_kaggle_legacy_token() == "KGAT_abc"


def test_legacy_normalization_and_idempotent_migration(tmp_path, monkeypatch):
    monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path))
    proj = init_project_layout("legacy_proj", "Legacy Project")
    chap = init_chapter_layout("legacy_proj", "1")

    assert normalize_stage_type("crawl") == StageType.INGEST
    assert normalize_stage_type("tts") == StageType.AUDIO

    norm_ch, changed = normalize_chapter_data({
        "chapter_id": "legacy_proj_chap_1",
        "chapter_number": "1",
        "project_slug": "legacy_proj",
        "title": "Chapter 1",
        "status": "completed",
        "custom_note": "keep_me",
        "stages": {
            "crawl": {"stage": "crawl", "status": "completed"},
        },
    })
    assert changed is True
    assert norm_ch["overall_status"] == "completed"
    assert norm_ch["custom_note"] == "keep_me"
    assert norm_ch["stages"]["ingest"]["status"] == "completed"

    # Legacy project voice alias + legacy memory/script.csv + memory/character_dictionary.json
    p_cfg = read_json(proj / "project.json")
    p_cfg["audio_settings"]["default_voice"] = "edge-tts-female"
    atomic_write_json(proj / "project.json", p_cfg)

    mem_dir = proj / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(mem_dir / "character_dictionary.json", {"Hero": {"core_features": "Dark hair"}})
    with open(mem_dir / "script.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["chapter", "sequence_id", "text", "focus_x", "focus_y", "pan_direction", "crop_scale"])
        w.writeheader()
        w.writerow({"chapter": "1", "sequence_id": "001", "text": "Lời kể mở đầu", "focus_x": "50", "focus_y": "50", "pan_direction": "static", "crop_scale": "100"})

    # Dry run must not mutate files
    dry = migrate_project_artifacts(proj, dry_run=True)
    assert dry["planned_count"] >= 3
    assert dry["applied_count"] == 0
    assert not (chap / "script.json").exists()
    assert not (proj / "characters.json").exists()

    # Apply with backups
    applied = migrate_project_artifacts(proj, dry_run=False, backup=True)
    assert applied["applied_count"] >= 3
    assert (proj / "project.json.bak").is_file()
    assert (proj / "characters.json").is_file()
    assert (chap / "script.json").is_file()
    assert read_json(proj / "project.json")["audio_settings"]["default_voice"] == "Trúc Ly"

    # Idempotent second run
    second = migrate_project_artifacts(proj, dry_run=False)
    assert second["planned_count"] == 0
    assert second["applied_count"] == 0

    # Preserves unknown fields during script normalization
    script_obj, was_migrated = normalize_legacy_script_data({
        "project_slug": "legacy_proj",
        "chapter_number": "1",
        "title": "Ep 1",
        "extra_author_tag": "preserved",
        "segments": [{"seq_id": "001", "panel_ids": ["p1.jpg"], "text": "Xin chào", "voice_id": "edge-tts-male"}],
    })
    assert was_migrated is True
    assert script_obj.segments[0].voice_id == "Hải Đăng"
    assert script_obj.metadata["preserved_legacy_fields"]["extra_author_tag"] == "preserved"

    csv_script = parse_legacy_script_csv(mem_dir / "script.csv", "legacy_proj", "1")
    assert len(csv_script.segments) == 1


def test_requirements_synced_with_pyproject():
    res = subprocess.run(
        [sys.executable, "scripts/sync_requirements.py", "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stderr
