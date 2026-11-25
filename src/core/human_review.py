"""Chapter-one approval bound to validated media and current content/config."""
from pathlib import Path
import hashlib
import datetime
import json
from pydantic import BaseModel, ConfigDict, Field, field_validator
from typing import Literal
from src.core.checkpoints import mark_chapter_human_approved
from src.core.errors import DomainValidationError
from src.core.storage import atomic_write_json, calculate_sha256, read_json
from src.core.utils import assert_file_nonempty


class HumanApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["4.1.0", "5.0.0"] = "5.0.0"
    human_review_status: Literal["approved"]
    project_slug: str
    chapter_number: Literal["1"] = "1"
    approved_at: datetime.datetime
    reviewer: str = Field(min_length=1)
    config_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    artifact_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    artifact_path: Literal["video/preview.mp4"] = "video/preview.mp4"

    @field_validator("approved_at")
    @classmethod
    def explicit_timestamp(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Approval timestamp must include a timezone")
        return value


def require_voice_approval(project_dir):
    from src.core.schemas import VoiceSelectionApproval
    from src.core.series_setup import require_series_setup
    from src.tts.catalog import resolve_voice_id
    root = Path(project_dir)
    slug = read_json(root / "project.json")["slug"]
    setup = require_series_setup(slug, project_dir=root)
    approval = VoiceSelectionApproval.model_validate(read_json(root / "voice_selection.json"))
    if not approval.is_approved() or approval.project_slug != slug or approval.voice_strategy != "single_narrator":
        raise DomainValidationError("Voice strategy requires explicit series-level human approval")
    voice = resolve_voice_id(str(setup.answers["Q03"].user_answer))
    speed = float(str(setup.answers["Q04"].user_answer).lower().rstrip("x"))
    if resolve_voice_id(approval.primary_voice) != voice or abs(approval.speed - speed) > 1e-6:
        raise DomainValidationError("Voice approval differs from confirmed setup")
    return approval


def validate_preview_package(root):
    require_voice_approval(root)
    chapter = Path(root) / "chapters" / "1"
    assert_file_nonempty(chapter / "video" / "preview.mp4")
    assert_file_nonempty(chapter / "subtitles" / "preview.srt")


def review_fingerprint(project_dir, *, legacy=False):
    root = Path(project_dir)
    chapter = root / "chapters" / "1"
    required = [root / "project.json", root / "series_setup_answers.json", root / "voice_selection.json"]
    required += [chapter / name for name in ("manifest.json", "script.json", "visual_plan.json")]
    if legacy:
        required += sorted((chapter / "panels").glob("*"))
        required += sorted((chapter / "raw").glob("*"))
        required += sorted((chapter / "audio" / "clips").glob("*.wav"))
        required += sorted(chapter.glob("thumbnail*"))
    project = read_json(root / "project.json")
    bgm = project.get("audio_settings", {}).get("bgm_path")
    external = []
    if bgm and legacy:
        path = Path(bgm)
        external.append(("bgm", calculate_sha256(path if path.is_absolute() else root / path)))
    # Chapter registration and bookkeeping timestamps do not change the
    # reviewed render. Bind the relevant project contract, not unrelated state.
    relevant = {key: project.get(key) for key in ("slug", "source_url", "language", "render_settings", "audio_settings", "script_pacing", "setup_revision", "setup_snapshot_hash")}
    project_hash = hashlib.sha256(json.dumps(relevant, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    records = [(str(path.relative_to(root)), project_hash if path == root / "project.json" else calculate_sha256(path))
               for path in required if path.is_file()]
    if any(not path.is_file() for path in required):
        raise DomainValidationError("Human approval requires all source/config/review inputs")
    return hashlib.sha256(json.dumps(records + external, sort_keys=True).encode()).hexdigest()


def require_preview_approval(project_dir):
    root = Path(project_dir)
    try:
        approval = HumanApproval.model_validate(read_json(root / "preview_approval.json"))
        if approval.project_slug != read_json(root / "project.json")["slug"] or not approval.reviewer.strip():
            raise ValueError("Reviewer/project mismatch")
        if approval.config_fingerprint != review_fingerprint(root, legacy=approval.schema_version == "4.1.0"):
            raise ValueError("Reviewed configuration/source is stale")
        chapter = root / "chapters" / "1"
        artifact = chapter / approval.artifact_path
        if calculate_sha256(artifact) != approval.artifact_hash:
            raise ValueError("Reviewed artifact has changed")
        validate_preview_package(root)
        return approval
    except Exception as exc:
        raise DomainValidationError(f"Human preview Gate G6 rejected: {exc}") from exc


def approve_preview(project_dir, reviewer, expected_config, expected_artifact):
    # Explicit command records the human's decision; flags on render never do.
    from src.core.schemas import utc_iso_now

    root = Path(project_dir)
    config = review_fingerprint(root)
    artifact = root / "chapters" / "1" / "video" / "preview.mp4"
    if config != expected_config or calculate_sha256(artifact) != expected_artifact:
        raise DomainValidationError("Review inputs changed since review pack was generated")
    validate_preview_package(root)
    approval = HumanApproval(human_review_status="approved", project_slug=read_json(root / "project.json")["slug"],
        approved_at=utc_iso_now(), reviewer=reviewer.strip(), config_fingerprint=config, artifact_hash=expected_artifact)
    atomic_write_json(root / "preview_approval.json", approval)
    mark_chapter_human_approved(root / "chapters" / "1")
    return approval
