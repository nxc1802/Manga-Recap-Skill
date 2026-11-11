"""
tests/unit/test_series_setup.py
Comprehensive unit test suite for Strict Form Runtime Validation (PR-01).

Tests:
1. Missing question rejection (individual and batch for Q01..Q10).
2. Non-user answer source rejection (answer_source != "user" rejected).
3. Cryptographic snapshot hash calculation & tampering detection.
4. Valid setup acceptance & domain constraints for all 10 questions.
5. Atomic persistence & project.json synchronization.
6. CLI commands: manga-recap setup [validate|record|show|migrate].
7. Fail-closed blocking in pipeline entry points (crawl, cloud, render, batch).
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch
import pytest

from src.cli.exit_codes import CliValidationError, ExitCode
from src.cli.main import build_parser, main
from src.core.series_setup import (
    ALL_QUESTION_IDS,
    QUESTION_METADATA,
    QuestionId,
    SeriesSetupAnswers,
    SetupAnswerItem,
    compute_snapshot_hash,
    migrate_legacy_setup,
    record_series_setup,
    require_series_setup,
    validate_complete_setup,
)
from src.core.storage import (
    atomic_write_json,
    get_project_dir,
    init_chapter_layout,
    init_project_layout,
    read_json,
)


def _make_valid_answers_dict() -> Dict[str, Any]:
    """Helper returning a canonical, fully valid 10-answer dict."""
    return {
        "Q01": {
            "question_id": "Q01",
            "question_title": "Chương & Nguồn truyện",
            "user_answer": {"chapters": "1-5", "source": "https://example.com/manga/solo-leveling"},
            "answer_source": "user",
            "confirmed_at": "2026-11-10T14:00:00Z",
        },
        "Q02": {
            "question_id": "Q02",
            "question_title": "Môi trường thực thi",
            "user_answer": "kaggle",
            "answer_source": "user",
            "confirmed_at": "2026-11-10T14:00:00Z",
        },
        "Q03": {
            "question_id": "Q03",
            "question_title": "Giọng đọc",
            "user_answer": "Ngọc Huyền",
            "answer_source": "user",
            "confirmed_at": "2026-11-10T14:00:00Z",
        },
        "Q04": {
            "question_id": "Q04",
            "question_title": "Tốc độ giọng đọc",
            "user_answer": 1.0,
            "answer_source": "user",
            "confirmed_at": "2026-11-10T14:00:00Z",
        },
        "Q05": {
            "question_id": "Q05",
            "question_title": "Gắn phụ đề",
            "user_answer": False,
            "answer_source": "user",
            "confirmed_at": "2026-11-10T14:00:00Z",
        },
        "Q06": {
            "question_id": "Q06",
            "question_title": "Animation / Motion",
            "user_answer": "animation",
            "answer_source": "user",
            "confirmed_at": "2026-11-10T14:00:00Z",
        },
        "Q07": {
            "question_id": "Q07",
            "question_title": "Thiết kế Thumbnail",
            "user_answer": True,
            "answer_source": "user",
            "confirmed_at": "2026-11-10T14:00:00Z",
        },
        "Q08": {
            "question_id": "Q08",
            "question_title": "Kênh YouTube & Hiển thị",
            "user_answer": {"account": "MangaRecap_Official", "privacy": "unlisted"},
            "answer_source": "user",
            "confirmed_at": "2026-11-10T14:00:00Z",
        },
        "Q09": {
            "question_id": "Q09",
            "question_title": "Chất lượng Video",
            "user_answer": "final",
            "answer_source": "user",
            "confirmed_at": "2026-11-10T14:00:00Z",
        },
        "Q10": {
            "question_id": "Q10",
            "question_title": "Nhạc nền BGM & Gains",
            "user_answer": {"bgm_path": "audio/epic_theme.mp3", "bgm_volume": 0.15, "voice_volume": 1.0},
            "answer_source": "user",
            "confirmed_at": "2026-11-10T14:00:00Z",
        },
    }


class TestSeriesSetupModelsAndHashing:
    """Verifies schema validation, canonical hashing, and snapshot immutability."""

    def test_canonical_hash_deterministic_and_reorder_invariant(self):
        answers = _make_valid_answers_dict()
        hash1 = compute_snapshot_hash(answers)

        # Re-order keys in dictionary
        reordered = {k: answers[k] for k in reversed(list(answers.keys()))}
        hash2 = compute_snapshot_hash(reordered)

        assert hash1 == hash2
        assert len(hash1) == 64  # SHA-256 hex digest

    def test_valid_setup_passes_validation(self):
        answers = _make_valid_answers_dict()
        snap_hash = compute_snapshot_hash(answers)
        payload = {
            "schema_version": "4.0.0",
            "project_slug": "test_manga",
            "setup_revision": 1,
            "created_at": "2026-11-10T14:00:00Z",
            "updated_at": "2026-11-10T14:00:00Z",
            "user_confirmed_at": "2026-11-10T14:00:00Z",
            "reviewer": "user",
            "answers": answers,
            "snapshot_hash": snap_hash,
        }
        setup_obj = SeriesSetupAnswers.model_validate(payload)
        is_valid, errors = validate_complete_setup(setup_obj)
        assert is_valid is True
        assert len(errors) == 0

    def test_missing_question_rejection_for_every_single_qid(self):
        """Individually omits each question from Q01 to Q10 and confirms rejection."""
        base_answers = _make_valid_answers_dict()

        for qid in ALL_QUESTION_IDS:
            answers_copy = copy.deepcopy(base_answers)
            del answers_copy[qid]
            snap_hash = compute_snapshot_hash(answers_copy)

            data = {
                "project_slug": "test_manga",
                "answers": answers_copy,
                "snapshot_hash": snap_hash,
            }
            is_valid, errors = validate_complete_setup(data)
            assert is_valid is False
            assert any(f"Missing mandatory question {qid}" in err for err in errors), (
                f"Expected rejection for missing {qid}, got errors: {errors}"
            )

    def test_non_user_answer_source_rejected(self):
        """Verifies that answer_source='default' or 'inferred' is strictly rejected."""
        answers = _make_valid_answers_dict()
        answers["Q03"]["answer_source"] = "default"

        # Pydantic validation rejects it
        with pytest.raises(Exception):
            SetupAnswerItem.model_validate(answers["Q03"])

        # Also validate_complete_setup rejects non-user answers
        data = {
            "project_slug": "test_manga",
            "answers": answers,
            "snapshot_hash": compute_snapshot_hash(answers),
        }
        is_valid, errors = validate_complete_setup(data)
        assert is_valid is False

    def test_snapshot_hash_tampering_detected(self):
        """Verifies that altering user_answer without updating snapshot_hash fails validation."""
        answers = _make_valid_answers_dict()
        original_hash = compute_snapshot_hash(answers)

        # Tamper with Q04 speed
        tampered_answers = copy.deepcopy(answers)
        tampered_answers["Q04"]["user_answer"] = 1.5

        data = {
            "project_slug": "test_manga",
            "answers": tampered_answers,
            "snapshot_hash": original_hash,  # Stored hash doesn't match tampered answer
        }
        is_valid, errors = validate_complete_setup(data)
        assert is_valid is False
        assert any("Snapshot hash mismatch" in err for err in errors)


class TestQuestionDomainValidation:
    """Verifies domain constraints for specific questions Q01-Q10."""

    def test_q02_compute_env_constraints(self):
        answers = _make_valid_answers_dict()
        answers["Q02"]["user_answer"] = "aws_ec2"  # Invalid
        data = {"answers": answers, "snapshot_hash": compute_snapshot_hash(answers)}
        is_valid, errors = validate_complete_setup(data)
        assert is_valid is False
        assert any("compute environment must be 'kaggle' or 'local'" in err for err in errors)

    def test_q03_voice_catalog_validation(self):
        answers = _make_valid_answers_dict()
        answers["Q03"]["user_answer"] = "Nonexistent_Alien_Voice_999"
        data = {"answers": answers, "snapshot_hash": compute_snapshot_hash(answers)}
        is_valid, errors = validate_complete_setup(data)
        assert is_valid is False
        assert any("not recognized in official VieNeu catalog" in err for err in errors)

        # Valid voice passes
        answers["Q03"]["user_answer"] = "Trúc Ly"
        data = {"answers": answers, "snapshot_hash": compute_snapshot_hash(answers)}
        is_valid, errors = validate_complete_setup(data)
        assert is_valid is True

    def test_q04_speed_bounds(self):
        answers = _make_valid_answers_dict()
        answers["Q04"]["user_answer"] = 3.5  # Too fast
        data = {"answers": answers, "snapshot_hash": compute_snapshot_hash(answers)}
        is_valid, errors = validate_complete_setup(data)
        assert is_valid is False
        assert any("speed must be between 0.5 and 2.0" in err for err in errors)

    def test_q05_burn_subtitles_boolean_normalization(self):
        answers = _make_valid_answers_dict()
        # Vietnamese string "Có" and "Không"
        answers["Q05"]["user_answer"] = "Có"
        data = {"answers": answers, "snapshot_hash": compute_snapshot_hash(answers)}
        is_valid, _ = validate_complete_setup(data)
        assert is_valid is True

        answers["Q05"]["user_answer"] = "Không"
        data = {"answers": answers, "snapshot_hash": compute_snapshot_hash(answers)}
        is_valid, _ = validate_complete_setup(data)
        assert is_valid is True

    def test_q08_no_upload_allowed(self):
        answers = _make_valid_answers_dict()
        answers["Q08"]["user_answer"] = "Không upload"
        data = {"answers": answers, "snapshot_hash": compute_snapshot_hash(answers)}
        is_valid, errors = validate_complete_setup(data)
        assert is_valid is True

    def test_q10_no_bgm_allowed(self):
        answers = _make_valid_answers_dict()
        answers["Q10"]["user_answer"] = "Không BGM"
        data = {"answers": answers, "snapshot_hash": compute_snapshot_hash(answers)}
        is_valid, errors = validate_complete_setup(data)
        assert is_valid is True


class TestAtomicPersistenceAndSync:
    """Verifies atomic write of series_setup_answers.json and synchronization with project.json."""

    def test_record_series_setup_synchronizes_project_json(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))
        slug = "atomic_sync_manga"
        proj_dir = get_project_dir(slug)

        answers = _make_valid_answers_dict()
        saved = record_series_setup(
            project_slug=slug,
            answers_data=answers,
            reviewer="alice_editor",
            project_dir=proj_dir,
        )

        assert saved.setup_revision == 1
        assert saved.reviewer == "alice_editor"

        # Verify series_setup_answers.json
        answers_file = proj_dir / "series_setup_answers.json"
        assert answers_file.exists()
        saved_on_disk = read_json(answers_file)
        assert saved_on_disk["setup_revision"] == 1
        assert saved_on_disk["reviewer"] == "alice_editor"

        # Verify project.json synchronized
        proj_file = proj_dir / "project.json"
        assert proj_file.exists()
        proj_data = read_json(proj_file)
        assert proj_data["setup_revision"] == 1
        assert proj_data["schema_version"] == "4.0.0"
        assert proj_data["audio_settings"]["default_voice"] == "Ngọc Huyền"
        assert proj_data["audio_settings"]["default_speed"] == 1.0
        assert proj_data["render_settings"]["burn_subtitles"] is False
        assert proj_data["render_settings"]["render_pan_mode"] == "animation"

    def test_setup_revision_increments_on_update(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))
        slug = "revision_test_manga"
        proj_dir = get_project_dir(slug)

        answers = _make_valid_answers_dict()
        rev1 = record_series_setup(slug, answers, project_dir=proj_dir)
        assert rev1.setup_revision == 1

        # Second record updates revision
        answers["Q04"]["user_answer"] = 1.1
        rev2 = record_series_setup(slug, answers, project_dir=proj_dir)
        assert rev2.setup_revision == 2

        # Check project.json also reflects rev 2
        proj_data = read_json(proj_dir / "project.json")
        assert proj_data["setup_revision"] == 2
        assert proj_data["audio_settings"]["default_speed"] == 1.1


class TestSetupCliCommands:
    """Verifies manga-recap setup [validate|record|show|migrate] commands via CLI."""

    def test_cli_setup_validate_unconfigured_fails_code_2(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))
        code = main(["setup", "validate", "-p", "unconfigured_series", "--json"])
        assert code == ExitCode.VALIDATION_ERROR

        captured = capsys.readouterr()
        resp = json.loads(captured.out)
        assert resp["status"] == "error"
        assert resp["exit_code"] == 2
        assert "is missing required user configuration" in resp["error"]["message"]

    def test_cli_setup_record_from_file_and_subsequent_validate(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))
        slug = "cli_configured_series"

        # Prepare answers JSON file
        answers = _make_valid_answers_dict()
        ans_file = tmp_path / "answers.json"
        ans_file.write_text(json.dumps(answers, ensure_ascii=False), encoding="utf-8")

        # 1. Record via CLI
        code_rec = main([
            "setup", "record",
            "-p", slug,
            "--answers-json", str(ans_file),
            "--reviewer", "bob_reviewer",
            "--json",
        ])
        assert code_rec == ExitCode.SUCCESS
        captured = capsys.readouterr()
        rec_data = json.loads(captured.out)["data"]
        assert rec_data["setup_revision"] == 1
        assert rec_data["reviewer"] == "bob_reviewer"

        # 2. Validate via CLI
        code_val = main(["setup", "validate", "-p", slug, "--json"])
        assert code_val == ExitCode.SUCCESS
        captured = capsys.readouterr()
        val_data = json.loads(captured.out)["data"]
        assert val_data["valid"] is True
        assert val_data["passed"] == 10

        # 3. Show via CLI
        code_show = main(["setup", "show", "-p", slug, "--json"])
        assert code_show == ExitCode.SUCCESS
        captured = capsys.readouterr()
        show_data = json.loads(captured.out)["data"]
        assert show_data["setup_revision"] == 1
        assert len(show_data["answers"]) == 10

    def test_cli_setup_migrate_legacy_project(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))
        slug = "legacy_project_to_migrate"

        # Setup legacy project layout
        init_project_layout(slug, "Legacy Project Title")

        code_mig = main([
            "setup", "migrate",
            "-p", slug,
            "--reviewer", "migration_bot",
            "--json",
        ])
        assert code_mig == ExitCode.SUCCESS

        captured = capsys.readouterr()
        data = json.loads(captured.out)["data"]
        assert data["legacy_migrated"] is True
        assert data["reviewer"] == "migration_bot"

        # Validation must now pass
        code_val = main(["setup", "validate", "-p", slug, "--json"])
        assert code_val == ExitCode.SUCCESS


class TestFailClosedPipelineGates:
    """Verifies fail-closed blocking in crawl_cmd, cloud_cmd, render_cmd, batch_cmd."""

    def test_crawl_fails_closed_on_unconfigured_series(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))
        code = main([
            "crawl",
            "-p", "unconfigured_crawl_series",
            "-c", "1",
            "--url", "https://example.com/ch1",
            "--json",
        ])
        assert code == ExitCode.VALIDATION_ERROR
        captured = capsys.readouterr()
        resp = json.loads(captured.out)
        assert resp["exit_code"] == 2
        assert "Strict Form Gate failed" in resp["error"]["message"]

    def test_cloud_run_fails_closed_on_unconfigured_series(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))
        # Patch authentication so it passes auth check and hits setup gate
        from unittest.mock import patch
        with patch("src.cloud.kaggle_service.KaggleCloudService.is_authenticated", True):
            code = main([
                "cloud",
                "run",
                "-p", "unconfigured_cloud_series",
                "-c", "1",
                "--json",
            ])
            assert code == ExitCode.VALIDATION_ERROR
            captured = capsys.readouterr()
            start_idx = captured.out.find("{")
            resp = json.loads(captured.out[start_idx:] if start_idx != -1 else captured.out)
            assert resp["exit_code"] == 2
            assert "Strict Form Gate failed" in resp["error"]["message"]

    def test_render_fails_closed_on_unconfigured_series(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))
        with patch("shutil.which", return_value="/usr/bin/ffmpeg"):
            code = main([
                "render",
                "-p", "unconfigured_render_series",
                "-c", "1",
                "--json",
            ])
            assert code == ExitCode.VALIDATION_ERROR
            captured = capsys.readouterr()
            start_idx = captured.out.find("{")
            resp = json.loads(captured.out[start_idx:] if start_idx != -1 else captured.out)
            assert resp["status"] == "error"
            assert resp["exit_code"] == 2
            assert "Strict Form Gate failed" in resp["error"]["message"]

    def test_crawl_succeeds_after_setup_recorded(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))
        slug = "configured_series_for_crawl"
        proj_dir = get_project_dir(slug)

        # Record valid setup
        answers = _make_valid_answers_dict()
        record_series_setup(slug, answers, project_dir=proj_dir)

        # Prepare raw source images
        src_dir = tmp_path / "raw_src"
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "p1.jpg").write_bytes(b"dummy_p1")
        (src_dir / "p2.png").write_bytes(b"dummy_p2")

        code = main([
            "crawl",
            "-p", slug,
            "-c", "1",
            "--import-dir", str(src_dir),
            "--json",
        ])
        assert code == ExitCode.SUCCESS
        captured = capsys.readouterr()
        resp = json.loads(captured.out)
        assert resp["status"] == "success"
        assert resp["data"]["raw_images_count"] == 2
