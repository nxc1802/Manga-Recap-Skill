"""
src/core/series_setup.py
Strict Form Runtime Validation & Setup Contract (Manga Recap Agentic Skill V4).

Mandates that all 10 series configuration questions (Q01-Q10) are collected explicitly
from the user via the `ask_question` tool with answer_source="user".
Provides cryptographic snapshot hashing (SHA-256), fail-closed pipeline gates,
and atomic synchronization with project.json.
"""

from __future__ import annotations

import datetime
from enum import Enum
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.cli.exit_codes import CliValidationError
from src.core.logger import setup_logger
from src.core.schemas import (
    AudioSettings,
    ProjectConfig,
    RenderSettings,
    utc_iso_now,
)
from src.core.storage import (
    atomic_write_json,
    get_project_dir,
    read_json,
)
from src.tts.catalog import is_valid_voice_id, resolve_voice_id

logger = setup_logger("series_setup")

SETUP_SCHEMA_VERSION = "4.0.0"


class QuestionId(str, Enum):
    """Canonical question identifiers for the 10-item series setup gate."""
    Q01_CHAPTER_SOURCE = "Q01"
    Q02_COMPUTE_ENV = "Q02"
    Q03_VOICE_PRESET = "Q03"
    Q04_VOICE_SPEED = "Q04"
    Q05_BURN_SUBTITLES = "Q05"
    Q06_ANIMATION_MOTION = "Q06"
    Q07_THUMBNAIL = "Q07"
    Q08_YOUTUBE_PUBLISH = "Q08"
    Q09_VIDEO_QUALITY = "Q09"
    Q10_BGM_SETTINGS = "Q10"


ALL_QUESTION_IDS: List[str] = [f"Q{i:02d}" for i in range(1, 11)]

QUESTION_METADATA: Dict[str, Dict[str, str]] = {
    "Q01": {
        "title": "Chương & Nguồn truyện",
        "description": "Phạm vi chương xử lý và nguồn ảnh (URL truyện hoặc đường dẫn thư mục cục bộ).",
    },
    "Q02": {
        "title": "Môi trường thực thi",
        "description": "Môi trường tính toán: 'kaggle' (Dual-T4 Cloud GPU) hoặc 'local' (Máy cục bộ).",
    },
    "Q03": {
        "title": "Giọng đọc",
        "description": "Giọng đọc VieNeu-TTS v3 Turbo chính thức từ catalog (ví dụ: 'Ngọc Huyền', 'Trúc Ly', 'Hải Đăng').",
    },
    "Q04": {
        "title": "Tốc độ giọng đọc",
        "description": "Tốc độ đọc tường thuật (float từ 0.5 đến 2.0, ví dụ: 1.0, 1.1x).",
    },
    "Q05": {
        "title": "Gắn phụ đề",
        "description": "Có gắn cứng phụ đề (burn subtitles) vào video hay không (True / False).",
    },
    "Q06": {
        "title": "Animation / Motion",
        "description": "Hiệu ứng chuyển động máy quay: Animation (cuộn chậm từ trên xuống) hoặc Static (cánh mờ).",
    },
    "Q07": {
        "title": "Thiết kế Thumbnail",
        "description": "Tự động thiết kế ảnh thumbnail 16:9 thu hút cho YouTube (True / False).",
    },
    "Q08": {
        "title": "Kênh YouTube & Hiển thị",
        "description": "Kênh YouTube đích và quyền riêng tư (private/unlisted/public), hoặc 'Không upload'.",
    },
    "Q09": {
        "title": "Chất lượng Video",
        "description": "Cấu hình xuất video: 'preview' (720p kiểm tra) hoặc 'final' (1080p xuất bản).",
    },
    "Q10": {
        "title": "Nhạc nền BGM & Gains",
        "description": "Nhạc nền BGM (đường dẫn file, volume 0.0-1.0, voice volume 0.0-2.0) hoặc 'Không BGM'.",
    },
}


class SetupAnswerItem(BaseModel):
    """User-confirmed response item for a single setup question."""
    model_config = ConfigDict(extra="forbid")

    question_id: str
    question_title: str
    user_answer: Any
    answer_source: Literal["user"] = "user"
    raw_input: Optional[Any] = None
    confirmed_at: str
    normalized: Optional[Dict[str, Any]] = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_incoming_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            # Support field aliases
            if "raw_input" not in data and "raw_answer" in data:
                data["raw_input"] = data["raw_answer"]
            if "user_answer" not in data and "raw_answer" in data:
                data["user_answer"] = data["raw_answer"]
            if "confirmed_at" not in data and "user_confirmed_at" in data:
                data["confirmed_at"] = data["user_confirmed_at"]
            if "confirmed_at" not in data:
                data["confirmed_at"] = utc_iso_now()

            # Ensure answer_source is strictly checked
            src = data.get("answer_source", "user")
            if src != "user":
                raise ValueError(f"Strict Form violation: answer_source must be 'user', got '{src}'")
        return data

    @field_validator("answer_source")
    @classmethod
    def _validate_source(cls, v: str) -> str:
        if v != "user":
            raise ValueError(f"Strict Form violation: answer_source must be 'user', got '{v}'")
        return v


class SeriesSetupAnswers(BaseModel):
    """Persistent cryptographic record of the 10-question series setup."""
    model_config = ConfigDict(extra="forbid")

    schema_version: str = SETUP_SCHEMA_VERSION
    project_slug: str
    setup_revision: int = Field(default=1, ge=1)
    created_at: str
    updated_at: str
    user_confirmed_at: str
    reviewer: str = Field(default="user", min_length=1)
    answers: Dict[str, SetupAnswerItem]
    snapshot_hash: str
    legacy_migrated: bool = False
    migration_notes: Optional[str] = None


def compute_snapshot_hash(answers: Union[Dict[str, Any], Dict[str, SetupAnswerItem]]) -> str:
    """
    Computes a deterministic cryptographic SHA-256 fingerprint over canonical answers.
    Answers are canonicalized by sorting keys (Q01..Q10) and serializing without extra whitespace.
    """
    canonical_dict: Dict[str, Any] = {}
    for qid in sorted(ALL_QUESTION_IDS):
        if qid in answers:
            item = answers[qid]
            if isinstance(item, SetupAnswerItem):
                user_ans = item.user_answer
                src = item.answer_source
                c_at = item.confirmed_at
            elif isinstance(item, dict):
                user_ans = item.get("user_answer", item.get("raw_answer"))
                src = item.get("answer_source", "user")
                c_at = item.get("confirmed_at", item.get("user_confirmed_at", ""))
            else:
                user_ans = item
                src = "user"
                c_at = ""

            canonical_dict[qid] = {
                "question_id": qid,
                "answer_source": src,
                "user_answer": user_ans,
                "confirmed_at": c_at,
            }

    canonical_json = json.dumps(canonical_dict, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def _normalize_bool(val: Any) -> Optional[bool]:
    """Helper to normalize boolean-like user input."""
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        v = val.strip().lower()
        if v in ("có", "co", "true", "yes", "y", "1", "tạo", "tạo thumbnail", "bật"):
            return True
        if v in ("không", "khong", "false", "no", "n", "0", "tắt", "không tạo"):
            return False
    return None


def validate_complete_setup(
    data: Union[SeriesSetupAnswers, Dict[str, Any], Path]
) -> Tuple[bool, List[str]]:
    """
    Validates that:
    1. All 10 questions Q01..Q10 are present.
    2. All answers have answer_source == 'user' and non-empty user_answer.
    3. Domain validation for each question passes without implicit fallback.
    4. Cryptographic snapshot_hash matches computed hash.
    
    Returns (is_valid, list_of_errors).
    """
    errors: List[str] = []

    # Resolve input into SeriesSetupAnswers or raw dict
    if isinstance(data, Path):
        if not data.exists():
            return False, [f"Setup answers file does not exist: {data}"]
        try:
            raw = read_json(data)
            data = raw
        except Exception as e:
            return False, [f"Failed to read setup file {data}: {e}"]

    raw_answers: Dict[str, Any] = {}
    stored_hash: Optional[str] = None

    if isinstance(data, SeriesSetupAnswers):
        raw_answers = data.answers
        stored_hash = data.snapshot_hash
    elif isinstance(data, dict):
        raw_answers = data.get("answers", {})
        stored_hash = data.get("snapshot_hash")
    else:
        return False, ["Invalid data type provided for setup validation."]

    # 1. Check all 10 questions present
    for qid in ALL_QUESTION_IDS:
        if qid not in raw_answers:
            q_info = QUESTION_METADATA.get(qid, {})
            title = q_info.get("title", qid)
            errors.append(f"Missing mandatory question {qid} ({title}).")

    if errors:
        return False, errors

    # 2. Check each question's properties and domain constraints
    normalized_answers: Dict[str, SetupAnswerItem] = {}

    for qid in ALL_QUESTION_IDS:
        item_raw = raw_answers[qid]
        if isinstance(item_raw, SetupAnswerItem):
            item = item_raw
        elif isinstance(item_raw, dict):
            try:
                item = SetupAnswerItem.model_validate(item_raw)
            except Exception as e:
                errors.append(f"Question {qid} schema error: {e}")
                continue
        else:
            errors.append(f"Question {qid} format invalid: expected dict or SetupAnswerItem.")
            continue

        normalized_answers[qid] = item
        if item.question_id != qid:
            errors.append(
                f"Question ID mismatch: key {qid} contains answer for {item.question_id}."
            )

        # Verify answer_source
        if item.answer_source != "user":
            errors.append(
                f"Question {qid}: answer_source must be 'user', got '{item.answer_source}'."
            )

        u_ans = item.user_answer
        if u_ans is None or (isinstance(u_ans, str) and not u_ans.strip()):
            errors.append(f"Question {qid}: user_answer cannot be empty.")
            continue

        # Domain checks
        if qid == "Q01":
            # Chapter scope & source
            if isinstance(u_ans, dict):
                src = u_ans.get("source") or u_ans.get("url") or u_ans.get("dir")
                ch = u_ans.get("chapters") or u_ans.get("chapter")
                if not src:
                    errors.append("Question Q01: source (URL or local path) must be specified.")
                if not ch:
                    errors.append("Question Q01: chapter scope must be specified.")
            else:
                errors.append(
                    "Question Q01: provide an explicit object containing both 'source' and 'chapters'; "
                    "unstructured text cannot prove the chapter range/source were selected."
                )
        elif qid == "Q02":
            # Compute environment: 'kaggle' or 'local'
            c_env = str(u_ans).strip().lower()
            if c_env not in ("kaggle", "local"):
                errors.append(
                    f"Question Q02: compute environment must be 'kaggle' or 'local', got '{u_ans}'."
                )
        elif qid == "Q03":
            # Voice preset: must exist in catalog
            v_name = str(u_ans).strip()
            if not is_valid_voice_id(v_name):
                errors.append(
                    f"Question Q03: voice '{v_name}' is not recognized in official VieNeu catalog."
                )
        elif qid == "Q04":
            # Speed: float in [0.5, 2.0]
            try:
                s_val = float(str(u_ans).strip().rstrip("xX"))
                if not (0.5 <= s_val <= 2.0):
                    errors.append(
                        f"Question Q04: speed must be between 0.5 and 2.0, got {s_val}."
                    )
            except ValueError:
                errors.append(
                    f"Question Q04: speed '{u_ans}' cannot be parsed as a float."
                )
        elif qid == "Q05":
            # Burn subtitles: boolean
            b_val = _normalize_bool(u_ans)
            if b_val is None:
                errors.append(
                    f"Question Q05: burn subtitles must be boolean (Có/Không or True/False), got '{u_ans}'."
                )
        elif qid == "Q06":
            # Animation motion: boolean or 'animation'/'static'
            if isinstance(u_ans, str):
                s_ans = u_ans.strip().lower()
                if s_ans not in ("animation", "static", "có", "co", "không", "khong", "true", "false"):
                    errors.append(
                        f"Question Q06: motion must be 'animation', 'static', or boolean, got '{u_ans}'."
                    )
            elif not isinstance(u_ans, bool):
                errors.append(
                    f"Question Q06: motion must be boolean or 'animation'/'static', got '{u_ans}'."
                )
        elif qid == "Q07":
            # Thumbnail creation: boolean
            t_val = _normalize_bool(u_ans)
            if t_val is None:
                errors.append(
                    f"Question Q07: thumbnail selection must be boolean (Có/Không), got '{u_ans}'."
                )
        elif qid == "Q08":
            # YouTube: explicit opt-out or exact account + privacy.
            if u_ans is False or (
                isinstance(u_ans, str)
                and u_ans.strip().lower() in ("không upload", "khong upload", "none", "no")
            ):
                pass
            elif isinstance(u_ans, dict):
                if u_ans.get("no_upload") is True:
                    if u_ans.get("account"):
                        errors.append("Question Q08: cannot choose both no_upload and an account.")
                elif (
                    isinstance(u_ans.get("account"), str)
                    and u_ans["account"].strip()
                    and u_ans.get("privacy") in ("private", "unlisted", "public")
                ):
                    pass
                else:
                    errors.append(
                        "Question Q08: upload requires both a non-empty 'account' and "
                        "'privacy' in private/unlisted/public."
                    )
            else:
                errors.append("Question Q08: explicit no_upload or account+privacy required.")
        elif qid == "Q09":
            # Video quality: exact user choice, no fuzzy substring matching.
            q_str = str(u_ans).strip().lower()
            if q_str not in ("preview", "final", "720p", "1080p"):
                errors.append(
                    f"Question Q09: select preview/720p or final/1080p; got '{u_ans}'."
                )
        elif qid == "Q10":
            # BGM: explicit none or fully specified path AND both gain levels.
            if u_ans is False or (
                isinstance(u_ans, str)
                and u_ans.strip().lower() in ("không bgm", "khong bgm", "none", "no")
            ):
                pass
            elif isinstance(u_ans, dict):
                if u_ans.get("none") is True:
                    if u_ans.get("bgm_path"):
                        errors.append("Question Q10: cannot choose both no BGM and a BGM file.")
                elif (
                    isinstance(u_ans.get("bgm_path"), str)
                    and u_ans["bgm_path"].strip()
                    and "bgm_volume" in u_ans
                    and "voice_volume" in u_ans
                ):
                    try:
                        bv = float(u_ans["bgm_volume"])
                        vv = float(u_ans["voice_volume"])
                        if not (0.0 <= bv <= 1.0 and 0.0 <= vv <= 2.0):
                            errors.append(
                                "Question Q10: BGM volume must be 0..1 and voice volume 0..2."
                            )
                    except (TypeError, ValueError):
                        errors.append("Question Q10: both volume values must be numeric.")
                else:
                    errors.append(
                        "Question Q10: BGM requires a path, bgm_volume and voice_volume "
                        "explicitly selected by the user."
                    )
            else:
                errors.append("Question Q10: explicit no BGM or path + both volumes required.")

    # 3. Verify cryptographic snapshot hash
    computed_hash = compute_snapshot_hash(normalized_answers)
    if stored_hash:
        if stored_hash != computed_hash:
            errors.append(
                f"Snapshot hash mismatch: stored '{stored_hash}' != computed '{computed_hash}'. "
                "Answers have been modified or tampered with without updating snapshot_hash."
            )
    else:
        errors.append("Missing cryptographic snapshot_hash in setup record.")

    return len(errors) == 0, errors


def require_series_setup(
    project_slug: str,
    project_dir: Optional[Path] = None,
    allow_legacy: bool = False,
) -> SeriesSetupAnswers:
    """
    Fail-closed gate checking that the series has complete, user-confirmed 10-question setup.
    Raises CliValidationError if setup is missing, incomplete, or tampered.
    Legacy project.json defaults never substitute for explicit 10/10 user choices.
    """
    proj_dir = project_dir or get_project_dir(project_slug)
    answers_file = proj_dir / "series_setup_answers.json"

    if answers_file.exists():
        try:
            raw = read_json(answers_file)
            answers_obj = SeriesSetupAnswers.model_validate(raw)
        except Exception as e:
            raise CliValidationError(
                f"Strict Form Gate failed: Series '{project_slug}' setup file is corrupt: {e}",
                details=[str(e)],
                data={"project_slug": project_slug, "valid": False, "error": str(e)},
            ) from e

        is_valid, errors = validate_complete_setup(answers_obj)
        if not is_valid:
            raise CliValidationError(
                f"Strict Form Gate failed: Series '{project_slug}' setup validation failed.",
                details=errors,
                data={"project_slug": project_slug, "valid": False, "errors": errors},
            )
        return answers_obj

    if allow_legacy:
        logger.warning(
            "allow_legacy=True is deprecated and ignored: user-confirmed "
            "Q01-Q10 answers are mandatory for every series."
        )

    raise CliValidationError(
        f"Strict Form Gate failed: Series '{project_slug}' is missing required user configuration.",
        details=[
            "Missing 'series_setup_answers.json'. All 10 questions (Q01-Q10) must be collected "
            "directly via ask_question and recorded via 'manga-recap setup record'."
        ],
        data={
            "project_slug": project_slug,
            "valid": False,
            "errors": [
                f"Missing question {qid} ({QUESTION_METADATA[qid]['title']})"
                for qid in ALL_QUESTION_IDS
            ],
        },
    )


def record_series_setup(
    project_slug: str,
    answers_data: Union[Dict[str, Any], SeriesSetupAnswers],
    reviewer: str = "user",
    project_dir: Optional[Path] = None,
) -> SeriesSetupAnswers:
    """
    Atomically records user-confirmed setup answers and synchronizes project.json with common setup_revision.
    Computes immutable snapshot_hash and validates all 10 questions before writing.
    """
    proj_dir = project_dir or get_project_dir(project_slug)
    proj_dir.mkdir(parents=True, exist_ok=True)
    answers_file = proj_dir / "series_setup_answers.json"
    project_json_file = proj_dir / "project.json"

    # Determine revision
    setup_rev = 1
    if answers_file.exists():
        try:
            curr = read_json(answers_file)
            setup_rev = int(curr.get("setup_revision", 1)) + 1
        except Exception:
            setup_rev = 2

    # Parse and structure answers
    parsed_answers: Dict[str, SetupAnswerItem] = {}
    now_ts = utc_iso_now()

    if isinstance(answers_data, SeriesSetupAnswers):
        parsed_answers = answers_data.answers
    elif isinstance(answers_data, dict):
        raw_map = answers_data.get("answers", answers_data)
        for qid in ALL_QUESTION_IDS:
            if qid in raw_map:
                item_val = raw_map[qid]
                if isinstance(item_val, SetupAnswerItem):
                    parsed_answers[qid] = item_val
                elif isinstance(item_val, dict):
                    q_title = item_val.get("question_title", QUESTION_METADATA.get(qid, {}).get("title", qid))
                    u_ans = item_val.get("user_answer", item_val.get("raw_answer"))
                    raw_in = item_val.get("raw_input", item_val.get("raw_answer", u_ans))
                    src = item_val.get("answer_source", "missing_explicit_confirmation")
                    c_at = item_val.get("confirmed_at", item_val.get("user_confirmed_at", now_ts))
                    parsed_answers[qid] = SetupAnswerItem(
                        question_id=qid,
                        question_title=q_title,
                        user_answer=u_ans,
                        answer_source=src,
                        raw_input=raw_in,
                        confirmed_at=c_at,
                    )
                else:
                    raise CliValidationError(
                        f"Q{qid} requires a structured, explicitly confirmed answer "
                        "(user_answer + answer_source='user'); raw values are not provenance."
                    )

    # Compute snapshot hash
    snap_hash = compute_snapshot_hash(parsed_answers)

    setup_answers = SeriesSetupAnswers(
        schema_version=SETUP_SCHEMA_VERSION,
        project_slug=project_slug,
        setup_revision=setup_rev,
        created_at=now_ts,
        updated_at=now_ts,
        user_confirmed_at=now_ts,
        reviewer=reviewer,
        answers=parsed_answers,
        snapshot_hash=snap_hash,
    )

    # Validate complete setup before disk persistence
    is_valid, errors = validate_complete_setup(setup_answers)
    if not is_valid:
        raise CliValidationError(
            f"Cannot record invalid series setup for '{project_slug}': {'; '.join(errors)}",
            details=errors,
            data={"project_slug": project_slug, "valid": False, "errors": errors},
        )

    # Atomically write series_setup_answers.json
    atomic_write_json(answers_file, setup_answers)

    # Synchronize project.json atomically
    _sync_project_json(project_json_file, project_slug, setup_answers)

    logger.info(
        f"Successfully recorded series setup answers for '{project_slug}' (rev={setup_rev}, hash={snap_hash[:8]})."
    )
    return setup_answers


def _sync_project_json(
    project_json_file: Path,
    project_slug: str,
    setup_answers: SeriesSetupAnswers,
) -> None:
    """Updates or creates project.json synchronized with setup answers and revision."""
    cfg: Optional[ProjectConfig] = None
    if project_json_file.exists():
        try:
            cfg = ProjectConfig.model_validate(read_json(project_json_file))
        except Exception:
            cfg = None

    if cfg is None:
        cfg = ProjectConfig(
            project_id=f"proj_{project_slug}",
            slug=project_slug,
            title=project_slug,
        )

    answers = setup_answers.answers

    # Q01: Chapter & Source
    q01 = answers["Q01"].user_answer
    if isinstance(q01, dict):
        if "source" in q01 or "url" in q01:
            cfg.source_url = q01.get("source") or q01.get("url")
    elif isinstance(q01, str) and (q01.startswith("http://") or q01.startswith("https://")):
        cfg.source_url = q01

    # Q03: Voice preset
    q03_voice = str(answers["Q03"].user_answer).strip()
    if is_valid_voice_id(q03_voice):
        cfg.audio_settings.default_voice = resolve_voice_id(q03_voice)

    # Q04: Speed
    try:
        cfg.audio_settings.default_speed = float(str(answers["Q04"].user_answer).strip().rstrip("xX"))
    except ValueError:
        pass

    # Q05: Burn subtitles
    b_subs = _normalize_bool(answers["Q05"].user_answer)
    if b_subs is not None:
        cfg.render_settings.burn_subtitles = b_subs

    # Q06: Motion
    q06 = answers["Q06"].user_answer
    if isinstance(q06, str):
        if q06.strip().lower() in ("animation", "có", "co", "true"):
            cfg.render_settings.render_pan_mode = "animation"
        else:
            cfg.render_settings.render_pan_mode = "static"
    elif isinstance(q06, bool):
        cfg.render_settings.render_pan_mode = "animation" if q06 else "static"

    # Q09: Video Quality
    q09 = str(answers["Q09"].user_answer).strip().lower()
    if "final" in q09 or "1080" in q09:
        cfg.render_settings.target_width = 1920
        cfg.render_settings.target_height = 1080
    else:
        cfg.render_settings.target_width = 1280
        cfg.render_settings.target_height = 720

    # Q10: BGM Settings
    q10 = answers["Q10"].user_answer
    if isinstance(q10, dict):
        if q10.get("bgm_path"):
            cfg.audio_settings.bgm_path = str(q10["bgm_path"])
        if "bgm_volume" in q10:
            with contextlib_suppress():
                cfg.audio_settings.bgm_volume = float(q10["bgm_volume"])
        if "voice_volume" in q10:
            with contextlib_suppress():
                cfg.audio_settings.voice_volume = float(q10["voice_volume"])
    elif isinstance(q10, (bool, str)) and (_normalize_bool(q10) is False or str(q10).strip().lower() in ("không bgm", "none")):
        cfg.audio_settings.bgm_path = None
        cfg.audio_settings.bgm_volume = 0.0

    # Stamp schema version & setup revision into raw dictionary
    cfg_dict = cfg.model_dump(mode="json")
    cfg_dict["setup_revision"] = setup_answers.setup_revision
    cfg_dict["schema_version"] = SETUP_SCHEMA_VERSION

    atomic_write_json(project_json_file, cfg_dict)


def _synthesize_legacy_answers(project_slug: str, p_data: Dict[str, Any]) -> SeriesSetupAnswers:
    """Creates a temporary synthetic SeriesSetupAnswers object for pre-V4 projects."""
    r_set = p_data.get("render_settings", {})
    a_set = p_data.get("audio_settings", {})
    now_ts = utc_iso_now()

    answers = {
        "Q01": SetupAnswerItem(
            question_id="Q01",
            question_title=QUESTION_METADATA["Q01"]["title"],
            user_answer={"chapters": "1", "source": p_data.get("source_url") or "legacy_local"},
            answer_source="user",
            confirmed_at=now_ts,
        ),
        "Q02": SetupAnswerItem(
            question_id="Q02",
            question_title=QUESTION_METADATA["Q02"]["title"],
            user_answer="local",
            answer_source="user",
            confirmed_at=now_ts,
        ),
        "Q03": SetupAnswerItem(
            question_id="Q03",
            question_title=QUESTION_METADATA["Q03"]["title"],
            user_answer=a_set.get("default_voice", "Ngọc Huyền"),
            answer_source="user",
            confirmed_at=now_ts,
        ),
        "Q04": SetupAnswerItem(
            question_id="Q04",
            question_title=QUESTION_METADATA["Q04"]["title"],
            user_answer=a_set.get("default_speed", 1.0),
            answer_source="user",
            confirmed_at=now_ts,
        ),
        "Q05": SetupAnswerItem(
            question_id="Q05",
            question_title=QUESTION_METADATA["Q05"]["title"],
            user_answer=bool(r_set.get("burn_subtitles", False)),
            answer_source="user",
            confirmed_at=now_ts,
        ),
        "Q06": SetupAnswerItem(
            question_id="Q06",
            question_title=QUESTION_METADATA["Q06"]["title"],
            user_answer=r_set.get("render_pan_mode", "static"),
            answer_source="user",
            confirmed_at=now_ts,
        ),
        "Q07": SetupAnswerItem(
            question_id="Q07",
            question_title=QUESTION_METADATA["Q07"]["title"],
            user_answer=False,
            answer_source="user",
            confirmed_at=now_ts,
        ),
        "Q08": SetupAnswerItem(
            question_id="Q08",
            question_title=QUESTION_METADATA["Q08"]["title"],
            user_answer="Không upload",
            answer_source="user",
            confirmed_at=now_ts,
        ),
        "Q09": SetupAnswerItem(
            question_id="Q09",
            question_title=QUESTION_METADATA["Q09"]["title"],
            user_answer="final" if r_set.get("target_height", 720) == 1080 else "preview",
            answer_source="user",
            confirmed_at=now_ts,
        ),
        "Q10": SetupAnswerItem(
            question_id="Q10",
            question_title=QUESTION_METADATA["Q10"]["title"],
            user_answer={
                "bgm_path": a_set.get("bgm_path") or r_set.get("bgm_path") or "none",
                "bgm_volume": a_set.get("bgm_volume", 0.15),
                "voice_volume": a_set.get("voice_volume", 1.0),
            } if (a_set.get("bgm_path") or r_set.get("bgm_path")) else "Không BGM",
            answer_source="user",
            confirmed_at=now_ts,
        ),
    }

    snap_hash = compute_snapshot_hash(answers)
    return SeriesSetupAnswers(
        schema_version=SETUP_SCHEMA_VERSION,
        project_slug=project_slug,
        setup_revision=1,
        created_at=now_ts,
        updated_at=now_ts,
        user_confirmed_at=now_ts,
        reviewer="legacy_system",
        answers=answers,
        snapshot_hash=snap_hash,
        legacy_migrated=True,
        migration_notes="Synthesized for backward compatibility with pre-V4 projects.",
    )


def migrate_legacy_setup(
    project_slug: str,
    reviewer: str = "legacy_migrator",
    project_dir: Optional[Path] = None,
    overrides: Optional[Dict[str, Any]] = None,
) -> SeriesSetupAnswers:
    """Migrates a legacy pre-V4 project to persistent SeriesSetupAnswers with audited reviewer."""
    proj_dir = project_dir or get_project_dir(project_slug)
    proj_json = proj_dir / "project.json"
    if not proj_json.exists():
        raise CliValidationError(
            f"Cannot migrate project '{project_slug}': project.json not found in {proj_dir}."
        )

    p_data = read_json(proj_json)
    # Migration must be a new explicit user approval, not a synthetic
    # reconstruction of preferences from legacy project.json.
    if not overrides:
        raise CliValidationError(
            "Legacy migration requires all ten explicit user answers via --answers-json. "
            "Old project defaults are not user consent."
        )
    answers_dict: Dict[str, Any] = dict(overrides.get("answers", overrides))
    if set(answers_dict) != set(ALL_QUESTION_IDS):
        raise CliValidationError(
            "Legacy migration requires exactly Q01-Q10, answered directly by the user."
        )

    recorded = record_series_setup(
        project_slug=project_slug,
        answers_data=answers_dict,
        reviewer=reviewer,
        project_dir=proj_dir,
    )
    return recorded


class contextlib_suppress:
    """Lightweight exception suppressor."""
    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc_val, exc_tb):
        return True
