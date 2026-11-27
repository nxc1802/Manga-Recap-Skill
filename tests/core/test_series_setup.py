from argparse import Namespace

import pytest

from src.cli.exit_codes import CliValidationError
from src.cli.main import execute_subcommand
from src.core.series_setup import ALL_QUESTION_IDS, record_series_setup, require_series_setup
from src.core.storage import atomic_write_json, read_json


def _make_valid_answers_dict():
    values = {
        "Q01": {"chapters": "1-5", "source": "https://example.com/manga/chapter-1"},
        "Q02": "kaggle",
        "Q03": "Ngọc Huyền",
        "Q04": 1.0,
        "Q05": False,
        "Q08": {"account": "MangaRecap_Official", "privacy": "unlisted"},
        "Q09": "final",
        "Q10": {"bgm_path": "audio/epic_theme.mp3", "bgm_volume": 0.15, "voice_volume": 1.0},
    }
    return {qid: {"user_answer": value, "answer_source": "user"} for qid, value in values.items()}


def test_setup_applies_eight_confirmed_answers(tmp_path):
    root = tmp_path / "series"
    answers = _make_valid_answers_dict()
    setup = record_series_setup("sample", answers, project_dir=root)
    assert list(setup.answers) == ALL_QUESTION_IDS
    assert require_series_setup("sample", project_dir=root) == setup
    config = read_json(root / "project.json")
    assert config["render_settings"]["render_pan_mode"] == "animation"
    assert config["render_settings"]["video_mode"] == "landscape_fit"
    assert config["render_settings"]["target_height"] == 1080
    assert config["audio_settings"]["bgm_path"] == "audio/epic_theme.mp3"
    assert "setup_snapshot_hash" not in config

    answers["Q10"]["user_answer"] = False
    answers["Q09"]["user_answer"] = "preview 720p"
    updated = record_series_setup("sample", answers, project_dir=root)
    assert updated.setup_revision == 2
    config = read_json(root / "project.json")
    assert config["audio_settings"]["bgm_path"] is None
    assert config["render_settings"]["target_height"] == 720
    config["render_settings"]["burn_subtitles"] = True
    atomic_write_json(root / "project.json", config)
    with pytest.raises(CliValidationError, match="diverge"):
        require_series_setup("sample", project_dir=root)


@pytest.mark.parametrize("qid,value", [
    ("Q01", {"chapters": "1"}), ("Q02", "auto"), ("Q03", "missing voice"),
    ("Q04", "3x"), ("Q05", "auto"), ("Q08", {"account": "channel"}),
    ("Q09", "4k"), ("Q10", {"bgm_path": "song.mp3"}),
])
def test_reject_invalid_user_answer(tmp_path, qid, value):
    answers = _make_valid_answers_dict()
    answers[qid]["user_answer"] = value
    with pytest.raises(CliValidationError):
        record_series_setup("sample", answers, project_dir=tmp_path)
    assert not (tmp_path / "project.json").exists()


def test_reject_incomplete_or_unconfirmed_answers(tmp_path):
    answers = _make_valid_answers_dict()
    answers.pop("Q05")
    with pytest.raises(CliValidationError, match="exactly"):
        record_series_setup("sample", answers, project_dir=tmp_path)
    answers["Q05"] = {"user_answer": True}
    with pytest.raises(CliValidationError, match="answer_source"):
        record_series_setup("sample", answers, project_dir=tmp_path)


@pytest.mark.parametrize("command", ["crawl", "segment", "decide", "tts", "render",
                                     "compile", "visual-plan", "approve"])
def test_pipeline_blocks_unconfigured_series(command):
    with pytest.raises(CliValidationError, match="eight"):
        execute_subcommand(command, Namespace(project="unconfigured", chapter="1"))


def test_legacy_setup_v4_rejected_at_runtime_with_migration_hint(tmp_path):
    answers = _make_valid_answers_dict()
    root = tmp_path / "legacy_series"
    record_series_setup("legacy_series", answers, project_dir=root)
    setup_path = root / "series_setup_answers.json"
    raw = read_json(setup_path)
    raw["schema_version"] = "4.0.0"
    raw["answers"]["Q06"] = {
        "question_id": "Q06",
        "question_title": "Motion",
        "user_answer": "static",
        "answer_source": "user",
        "confirmed_at": raw["updated_at"],
    }
    atomic_write_json(setup_path, raw)
    with pytest.raises(CliValidationError, match="migrate_legacy_project.py"):
        require_series_setup("legacy_series", project_dir=root)
