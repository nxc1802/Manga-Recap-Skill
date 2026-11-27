"""Record and apply the eight user-confirmed series settings."""
from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.core.errors import DomainValidationError
from src.core.project import get_project_dir
from src.core.schemas import ProjectConfig, utc_iso_now
from src.core.storage import FileLock, atomic_write_json_pair, read_json
from src.core.version import SETUP_SCHEMA_VERSION
from src.tts.catalog import is_valid_voice_id, resolve_voice_id

QUESTION_METADATA = {
    "Q01": {"title": "Chương và nguồn truyện", "name": "source"},
    "Q02": {"title": "Môi trường thực thi", "name": "compute"},
    "Q03": {"title": "Giọng đọc", "name": "voice"},
    "Q04": {"title": "Tốc độ giọng đọc", "name": "speed"},
    "Q05": {"title": "Gắn phụ đề", "name": "subtitles"},
    "Q08": {"title": "Kênh YouTube và quyền riêng tư", "name": "publish"},
    "Q09": {"title": "Chất lượng video", "name": "quality"},
    "Q10": {"title": "Nhạc nền", "name": "audio"},
}
ALL_QUESTION_IDS = list(QUESTION_METADATA)
NAMED_TO_QID = {meta["name"]: qid for qid, meta in QUESTION_METADATA.items()}
QID_TO_NAMED = {qid: meta["name"] for qid, meta in QUESTION_METADATA.items()}


def normalize_answers_payload(answers_data: Any) -> dict[str, Any]:
    """
    Accepts either Q01-Q05/Q08-Q10 keys or named setting keys
    (source, compute, voice, speed, subtitles, publish, quality, audio).
    Rejects conflicting duplicates rather than silently dropping user input.
    """
    raw = answers_data.get("answers", answers_data) if isinstance(answers_data, dict) else answers_data.answers
    if not isinstance(raw, dict):
        raise DomainValidationError("Provide exactly Q01–Q05 and Q08–Q10")

    normalized: dict[str, Any] = {}
    for key, val in raw.items():
        canon_qid = NAMED_TO_QID.get(str(key).strip().lower(), key)
        if canon_qid in normalized and normalized[canon_qid] != val:
            raise DomainValidationError(f"Conflicting setup answers for '{key}' / '{canon_qid}'")
        normalized[canon_qid] = val
    return normalized


def extract_named_settings(setup: "SeriesSetupAnswers") -> dict[str, Any]:
    """Returns confirmed user answers keyed by semantic setting names."""
    return {
        QID_TO_NAMED[qid]: item.user_answer
        for qid, item in setup.answers.items()
        if qid in QID_TO_NAMED
    }


class SetupAnswerItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    question_id: str
    question_title: str
    user_answer: Any
    answer_source: Literal["user"]
    confirmed_at: str


class SeriesSetupAnswers(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schema_version: str = SETUP_SCHEMA_VERSION
    project_slug: str
    setup_revision: int = Field(default=1, ge=1)
    created_at: str
    updated_at: str
    user_confirmed_at: str
    reviewer: str = Field(min_length=1)
    answers: dict[str, SetupAnswerItem]


def _normalize_bool(value):
    if isinstance(value, bool):
        return value
    text = str(value).strip().casefold()
    if text in {"có", "co", "true", "yes", "1"}:
        return True
    if text in {"không", "khong", "false", "no", "0"}:
        return False
    return None


def _choice(value, options):
    return str(value).strip().casefold() in options


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _gain(value, low, high):
    try:
        return not isinstance(value, bool) and low <= float(value) <= high
    except (ValueError, TypeError):
        return False


def _source(value):
    if not isinstance(value, dict):
        return None
    return value.get("source") or value.get("url") or value.get("dir")


def _no_upload(value):
    return value is False or _choice(value, {"không upload", "khong upload", "none", "no"})


def _no_bgm(value):
    return value is False or _choice(value, {"không bgm", "khong bgm", "none", "no"})


def _valid_answer(qid, value):
    if qid == "Q01":
        return _text(_source(value)) and bool(value.get("chapters") or value.get("chapter"))
    if qid == "Q02":
        return _choice(value, {"local", "kaggle"})
    if qid == "Q03":
        return _text(value) and is_valid_voice_id(value)
    if qid == "Q04":
        return _gain(str(value).strip().rstrip("xX"), 0.5, 2)
    if qid == "Q05":
        return _normalize_bool(value) is not None
    if qid == "Q08":
        if _no_upload(value):
            return True
        if not isinstance(value, dict):
            return False
        if value.get("no_upload") is True:
            return not value.get("account")
        return _text(value.get("account")) and value.get("privacy") in ("private", "unlisted", "public")
    if qid == "Q09":
        return _choice(value, {"preview", "720p", "preview 720p", "final", "1080p", "final 1080p"})
    if _no_bgm(value):
        return True
    if not isinstance(value, dict):
        return False
    if value.get("none") is True:
        return not value.get("bgm_path")
    return _text(value.get("bgm_path")) and _gain(value.get("bgm_volume"), 0, 1) and _gain(value.get("voice_volume"), 0, 2)


def validate_complete_setup(data):
    try:
        if isinstance(data, Path):
            data = read_json(data)
        setup = SeriesSetupAnswers.model_validate(data)
    except Exception as exc:
        return False, [str(exc)]
    errors = []
    if setup.schema_version != SETUP_SCHEMA_VERSION:
        errors.append(
            f"Unsupported series setup schema_version '{setup.schema_version}' (expected {SETUP_SCHEMA_VERSION}); "
            "run 'python scripts/migrate_legacy_project.py --project-dir <dir> --apply'."
        )
    for qid, question in QUESTION_METADATA.items():
        item = setup.answers.get(qid)
        if item is None:
            errors.append(f"Missing question {qid} ({question['title']})")
        elif item.question_id != qid or not _valid_answer(qid, item.user_answer):
            errors.append(f"Invalid answer for {qid} ({question['title']})")
    if set(setup.answers) - set(ALL_QUESTION_IDS):
        errors.append("Setup accepts Q01–Q05 and Q08–Q10 only; migrate legacy Q06/Q07 first")
    return not errors, errors


def _apply_answers(config, setup):
    config = config.model_copy(deep=True)
    values = {key: item.user_answer for key, item in setup.answers.items()}
    source = _source(values["Q01"])
    config.source_url = source
    audio = config.audio_settings
    audio.default_voice = resolve_voice_id(values["Q03"])
    audio.default_speed = float(str(values["Q04"]).strip().rstrip("xX"))
    render = config.render_settings
    render.burn_subtitles = _normalize_bool(values["Q05"])
    render.video_mode = "landscape_fit"
    render.render_pan_mode = "animation"
    if _choice(values["Q09"], {"final", "1080p", "final 1080p"}):
        render.target_width, render.target_height = 1920, 1080
    else:
        render.target_width, render.target_height = 1280, 720
    bgm = values["Q10"]
    if _no_bgm(bgm) or bgm.get("none") is True:
        audio.bgm_path, audio.bgm_volume, audio.voice_volume = None, 0.0, 1.0
    else:
        audio.bgm_path = bgm["bgm_path"]
        audio.bgm_volume = float(bgm["bgm_volume"])
        audio.voice_volume = float(bgm["voice_volume"])
    return config


def _check_setup(setup):
    valid, errors = validate_complete_setup(setup)
    if not valid:
        detail_msg = "; ".join(errors[:3])
        raise DomainValidationError(f"Series setup invalid: {detail_msg}", details=errors)


def require_series_setup(project_slug, project_dir=None):
    root = Path(project_dir or get_project_dir(project_slug))
    path = root / "series_setup_answers.json"
    if not path.is_file():
        raise DomainValidationError("Strict Form Gate failed: record eight user-confirmed answers with manga-recap setup record")
    with FileLock(path):
        try:
            setup = SeriesSetupAnswers.model_validate(read_json(path))
            config = ProjectConfig.model_validate(read_json(root / "project.json"))
            _check_setup(setup)
            if setup.project_slug != project_slug or config.slug != project_slug:
                raise ValueError("Setup/project identity mismatch")
            if config.setup_revision != setup.setup_revision:
                raise ValueError("Setup revision mismatch")
            if _apply_answers(config, setup) != config:
                raise ValueError("Project settings diverge from confirmed answers")
            return setup
        except DomainValidationError:
            raise
        except Exception as exc:
            raise DomainValidationError(f"Cannot use series setup for {project_slug}: {exc}") from exc


def record_series_setup(project_slug, answers_data, reviewer="user", project_dir=None):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", project_slug):
        raise DomainValidationError("Use letters, numbers, hyphens, or underscores in the project slug")
    root = Path(project_dir or get_project_dir(project_slug))
    if not reviewer.strip():
        raise DomainValidationError("Setup requires a reviewer")
    raw = normalize_answers_payload(answers_data)
    if set(raw) != set(ALL_QUESTION_IDS):
        raise DomainValidationError("Provide exactly Q01–Q05 and Q08–Q10")
    now = utc_iso_now()
    answers = {}
    for qid in ALL_QUESTION_IDS:
        data = raw[qid]
        if isinstance(data, SetupAnswerItem):
            data = data.model_dump()
        if not isinstance(data, dict) or data.get("answer_source") != "user":
            raise DomainValidationError(f"{qid} requires explicit answer_source='user'")
        answers[qid] = SetupAnswerItem.model_validate({
            "question_id": qid, "question_title": QUESTION_METADATA[qid]["title"], "confirmed_at": now, **data,
        })
    root.mkdir(parents=True, exist_ok=True)
    path = root / "series_setup_answers.json"
    project_path = root / "project.json"
    with FileLock(path):
        previous = read_json(path) if path.is_file() else {}
        setup = SeriesSetupAnswers(project_slug=project_slug, setup_revision=previous.get("setup_revision", 0) + 1,
            created_at=previous.get("created_at", now), updated_at=now, user_confirmed_at=now,
            reviewer=reviewer.strip(), answers=answers)
        _check_setup(setup)
        config = (ProjectConfig.model_validate(read_json(project_path)) if project_path.is_file() else
                  ProjectConfig(project_id=f"proj_{project_slug}", slug=project_slug, title=project_slug))
        if config.slug != project_slug:
            raise DomainValidationError("Setup/project identity mismatch")
        config = _apply_answers(config, setup)
        config.setup_revision = setup.setup_revision
        config.schema_version = setup.schema_version
        config.updated_at = now
        project_data = config.model_dump(mode="json", exclude={"setup_snapshot_hash"})
        atomic_write_json_pair(path, setup, project_path, project_data)
    return setup
