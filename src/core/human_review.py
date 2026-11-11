"""Chapter-one approval bound to validated media and current content/config."""
from pathlib import Path
import hashlib
import datetime
import json
from pydantic import BaseModel, ConfigDict, Field, field_validator
from typing import Literal
from src.cli.exit_codes import CliValidationError
from src.core.storage import atomic_write_json, read_json
from src.render.artifact_validation import fingerprint
from src.core.visual_contract import VisualPlan


class HumanApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["4.1.0"] = "4.1.0"
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
        raise CliValidationError("Voice strategy requires explicit series-level human approval")
    voice = resolve_voice_id(str(setup.answers["Q03"].user_answer))
    speed = float(str(setup.answers["Q04"].user_answer).lower().rstrip("x"))
    if resolve_voice_id(approval.primary_voice) != voice or abs(approval.speed - speed) > 1e-6:
        raise CliValidationError("Voice approval differs from confirmed setup")
    return approval


def validate_preview_package(root):
    from src.core.storage import load_recap_script, load_manifest
    from src.render.chapter_renderer import resolve_audio
    from src.render.visual_policy import resolve_visual_plan
    from src.render.artifact_validation import validate_final_artifact
    root = Path(root)
    require_voice_approval(root)
    from src.core.series_setup import require_series_setup, _normalize_bool
    setup = require_series_setup(read_json(root / "project.json")["slug"], project_dir=root)
    if _normalize_bool(setup.answers["Q07"].user_answer) is True:
        from PIL import Image
        thumbnail = root / "chapters" / "1" / "thumbnail.jpg"
        selection = read_json(thumbnail.parent / "thumbnail_selection.json")
        if selection.get("selection_source") != "explicit_cli_arguments" or fingerprint(thumbnail) != selection.get("artifact_sha256"):
            raise CliValidationError("Thumbnail selection/evidence is missing or stale")
        if not selection.get("panels"):
            raise CliValidationError("Thumbnail source panels are required")
        for relative, digest in selection["panels"].items():
            panel = (thumbnail.parent / relative).resolve()
            if not panel.is_relative_to((thumbnail.parent / "panels").resolve()) or fingerprint(panel) != digest:
                raise CliValidationError("Thumbnail source panel changed")
        with Image.open(thumbnail) as image:
            image.verify()
            if abs(image.width / image.height - 16/9) > 0.01:
                raise CliValidationError("Thumbnail must be a valid 16:9 image")
    chapter = root / "chapters" / "1"
    settings = read_json(root / "project.json")["render_settings"]
    script, manifest = load_recap_script(chapter), load_manifest(chapter)
    plan = VisualPlan.load(chapter / "visual_plan.json")
    expected = resolve_visual_plan(chapter, manifest, script, target_width=settings["target_width"],
        target_height=settings["target_height"], fps=settings["fps"],
        motion_enabled=settings["render_pan_mode"] == "animation")
    if expected != plan:
        raise CliValidationError("Reviewed plan differs from canonical source/config")
    return validate_final_artifact(chapter / "video" / "preview.mp4", chapter / "subtitles" / "preview.srt",
        [resolve_audio(chapter, seg) for seg in script.segments], plan, script, manifest, "1", chapter_dir=chapter)


def review_fingerprint(project_dir):
    root = Path(project_dir)
    chapter = root / "chapters" / "1"
    required = [root / "project.json", root / "series_setup_answers.json", root / "voice_selection.json"]
    required += [chapter / name for name in ("manifest.json", "script.json", "visual_plan.json")]
    # Hash the actual source/audio assets as well as metadata referencing them.
    required += sorted((chapter / "panels").glob("*"))
    required += sorted((chapter / "raw").glob("*"))
    required += sorted((chapter / "audio" / "clips").glob("*.wav"))
    required += sorted(chapter.glob("thumbnail*"))
    project = read_json(root / "project.json")
    bgm = project.get("audio_settings", {}).get("bgm_path")
    external = []
    if bgm:
        path = Path(bgm)
        external.append(("bgm", fingerprint(path if path.is_absolute() else root / path)))
    # Chapter registration and bookkeeping timestamps do not change the
    # reviewed render. Bind the relevant project contract, not unrelated state.
    relevant = {key: project.get(key) for key in ("slug", "source_url", "language", "render_settings", "audio_settings", "script_pacing", "setup_revision", "setup_snapshot_hash")}
    project_hash = hashlib.sha256(json.dumps(relevant, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    records = [(str(path.relative_to(root)), project_hash if path == root / "project.json" else fingerprint(path))
               for path in required if path.is_file()]
    if any(not path.is_file() for path in required):
        raise CliValidationError("Human approval requires all source/config/review inputs")
    return hashlib.sha256(json.dumps(records + external, sort_keys=True).encode()).hexdigest()


def require_preview_approval(project_dir):
    root = Path(project_dir)
    try:
        approval = HumanApproval.model_validate(read_json(root / "preview_approval.json"))
        if approval.project_slug != read_json(root / "project.json")["slug"] or not approval.reviewer.strip():
            raise ValueError("Reviewer/project mismatch")
        if approval.config_fingerprint != review_fingerprint(root):
            raise ValueError("Reviewed configuration/source is stale")
        chapter = root / "chapters" / "1"
        artifact = chapter / approval.artifact_path
        if fingerprint(artifact) != approval.artifact_hash:
            raise ValueError("Reviewed artifact has changed")
        final_artifact = chapter / "video" / "final.mp4"
        if final_artifact.exists() and fingerprint(final_artifact) != approval.artifact_hash:
            raise ValueError("Chapter 1 final artifact differs from the human-reviewed preview")
        validate_preview_package(root)
        if approval.config_fingerprint != review_fingerprint(root) or fingerprint(artifact) != approval.artifact_hash:
            raise ValueError("Review inputs changed during validation")
        return approval
    except Exception as exc:
        raise CliValidationError(f"Human preview Gate G6 rejected: {exc}") from exc


def approve_preview(project_dir, reviewer, expected_config, expected_artifact):
    # Explicit command records the human's decision; flags on render never do.
    from src.core.schemas import utc_iso_now
    root = Path(project_dir)
    config = review_fingerprint(root)
    artifact = root / "chapters" / "1" / "video" / "preview.mp4"
    if config != expected_config or fingerprint(artifact) != expected_artifact:
        raise CliValidationError("Review inputs changed since review pack was generated")
    validate_preview_package(root)
    approval = HumanApproval(human_review_status="approved", project_slug=read_json(root / "project.json")["slug"],
        approved_at=utc_iso_now(), reviewer=reviewer.strip(), config_fingerprint=config, artifact_hash=expected_artifact)
    atomic_write_json(root / "preview_approval.json", approval)
    return approval
