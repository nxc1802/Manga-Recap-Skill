"""Canonical ReleaseState transition engine with cryptographic evidence validation."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple, Union

from src.core.contracts import ArchiveReceipt, ReleaseRecord, ReleaseState, YouTubeReceipt
from src.core.schemas import PanelManifest, RecapScript
from src.core.storage import (
    acquire_lock,
    atomic_write_json,
    calculate_sha256,
    get_output_dir,
    read_json,
)
from src.core.visual_contract import VisualPlan

RELEASE_STATE_FILENAME = "release_state.json"
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_GIT_SHA40_RE = re.compile(r"^[a-f0-9]{40}$")


class StateTransitionError(ValueError):
    """Raised on illegal state transitions or missing/invalid cryptographic evidence."""


@dataclass(frozen=True)
class TransitionResult:
    """Result of a validated state transition."""

    previous_state: ReleaseState
    target_state: ReleaseState
    evidence_hashes: Dict[str, str]

    @property
    def state(self) -> ReleaseState:
        return self.target_state

    @property
    def current_state(self) -> ReleaseState:
        return self.target_state


STATE_ORDER: Tuple[ReleaseState, ...] = (
    ReleaseState.DRAFT,
    ReleaseState.SETUP_VALID,
    ReleaseState.SOURCE_READY,
    ReleaseState.SCRIPT_READY,
    ReleaseState.AUDIO_VALID,
    ReleaseState.VISUAL_PLAN_VALID,
    ReleaseState.RENDER_VALID,
    ReleaseState.TECHNICALLY_VALID,
    ReleaseState.HUMAN_APPROVED,
    ReleaseState.PRODUCTION_READY,
    ReleaseState.UPLOADED,
    ReleaseState.YOUTUBE_VERIFIED,
    ReleaseState.ARCHIVED,
    ReleaseState.ARCHIVE_VERIFIED,
    ReleaseState.RESTORE_VERIFIED,
    ReleaseState.CLEANUP_ALLOWED,
)

ALLOWED_TRANSITIONS: Dict[ReleaseState, Tuple[ReleaseState, ...]] = {
    STATE_ORDER[i]: (STATE_ORDER[i + 1],) if i + 1 < len(STATE_ORDER) else ()
    for i in range(len(STATE_ORDER))
}


def _coerce_state(value: Union[ReleaseState, str]) -> ReleaseState:
    if isinstance(value, ReleaseState):
        return value
    if isinstance(value, str):
        try:
            return ReleaseState(value)
        except ValueError as exc:
            raise StateTransitionError(f"Unknown release state: {value!r}") from exc
    raise StateTransitionError(f"Invalid release state type: {type(value).__name__}")


def _require_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise StateTransitionError(f"Missing or invalid SHA-256 for {label}")
    return value


def _sha256_json(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _require_evidence_dict(evidence: Optional[Dict[str, Any]], transition_name: str) -> Dict[str, Any]:
    if evidence is None or not isinstance(evidence, dict):
        raise StateTransitionError(f"Transition {transition_name} requires an evidence dict")
    return evidence


def _project_context(evidence: Dict[str, Any]) -> Tuple[Optional[Path], str, Optional[str]]:
    raw_dir = evidence.get("project_dir")
    project_dir = Path(raw_dir).resolve() if raw_dir is not None else None
    chapter_number = str(evidence.get("chapter_number") or "1")
    slug = evidence.get("project_slug")
    if project_dir is not None and not slug:
        proj_json = project_dir / "project.json"
        if not proj_json.exists():
            raise StateTransitionError(f"Missing project.json in {project_dir}")
        try:
            slug = str(read_json(proj_json)["slug"])
        except Exception as exc:
            raise StateTransitionError(f"Invalid project.json in {project_dir}: {exc}") from exc
    return project_dir, chapter_number, str(slug) if slug is not None else None


def _validate_setup_valid(evidence: Dict[str, Any]) -> Dict[str, str]:
    project_dir, _, slug = _project_context(evidence)
    if project_dir is not None:
        from src.core.series_setup import require_series_setup

        try:
            setup = require_series_setup(slug, project_dir=project_dir)
        except Exception as exc:
            raise StateTransitionError(f"Setup validation failed: {exc}") from exc
        snap_hash = _require_sha256(setup.snapshot_hash, "setup.snapshot_hash")
        provided = evidence.get("setup_snapshot_hash")
        if provided is not None and provided != snap_hash:
            raise StateTransitionError("Provided setup_snapshot_hash mismatches series_setup_answers.json")
        return {"setup_snapshot_hash": snap_hash}

    snap_hash = _require_sha256(evidence.get("setup_snapshot_hash"), "setup_snapshot_hash")
    if "series_setup_answers" in evidence:
        from src.core.series_setup import SeriesSetupAnswers, validate_complete_setup

        try:
            setup_obj = SeriesSetupAnswers.model_validate(evidence["series_setup_answers"])
            ok, errs = validate_complete_setup(setup_obj)
        except Exception as exc:
            raise StateTransitionError(f"Invalid series_setup_answers: {exc}") from exc
        if not ok:
            raise StateTransitionError(f"Incomplete series setup: {'; '.join(errs)}")
        if setup_obj.snapshot_hash != snap_hash:
            raise StateTransitionError("series_setup_answers.snapshot_hash mismatch")
    return {"setup_snapshot_hash": snap_hash}


def _validate_source_ready(evidence: Dict[str, Any]) -> Dict[str, str]:
    project_dir, chapter_number, slug = _project_context(evidence)
    if project_dir is not None:
        chap_dir = project_dir / "chapters" / chapter_number
        manifest_path = chap_dir / "manifest.json"
        if not manifest_path.is_file():
            raise StateTransitionError(f"Missing manifest.json at {manifest_path}")
        try:
            manifest = PanelManifest.model_validate(read_json(manifest_path))
        except Exception as exc:
            raise StateTransitionError(f"Invalid manifest.json: {exc}") from exc
        if slug and manifest.project_slug != slug:
            raise StateTransitionError("Manifest project_slug mismatch")
        if str(manifest.chapter_number) != chapter_number:
            raise StateTransitionError("Manifest chapter_number mismatch")
        if not manifest.panels:
            raise StateTransitionError("Manifest contains zero panels")

        panels_root = (chap_dir / "panels").resolve()
        panel_hashes: Dict[str, str] = {}
        for panel in manifest.panels:
            expected_sha = _require_sha256(panel.sha256, f"panel {panel.panel_id} sha256")
            panel_file = (chap_dir / panel.relative_path).resolve()
            if not panel_file.is_relative_to(panels_root) or not panel_file.is_file():
                raise StateTransitionError(f"Missing or unsafe panel file: {panel.relative_path}")
            if panel_file.stat().st_size <= 0:
                raise StateTransitionError(f"Empty panel file: {panel.relative_path}")
            actual_sha = calculate_sha256(panel_file)
            if actual_sha != expected_sha:
                raise StateTransitionError(f"Panel hash mismatch for {panel.panel_id}")
            panel_hashes[panel.panel_id] = expected_sha

        manifest_sha = calculate_sha256(manifest_path)
        if evidence.get("manifest_sha256") is not None and evidence["manifest_sha256"] != manifest_sha:
            raise StateTransitionError("Provided manifest_sha256 mismatches manifest.json")
        if evidence.get("panel_hashes") is not None and evidence["panel_hashes"] != panel_hashes:
            raise StateTransitionError("Provided panel_hashes mismatches manifest panels")
        return {
            "manifest_sha256": manifest_sha,
            "panel_hashes_digest": _sha256_json(panel_hashes),
        }

    manifest_sha = _require_sha256(evidence.get("manifest_sha256"), "manifest_sha256")
    panel_hashes = evidence.get("panel_hashes")
    if not isinstance(panel_hashes, dict) or not panel_hashes:
        raise StateTransitionError("SOURCE_READY requires non-empty panel_hashes mapping")
    normalized: Dict[str, str] = {}
    for pid, digest in panel_hashes.items():
        if not isinstance(pid, str) or not pid.strip():
            raise StateTransitionError("Panel ID in panel_hashes must be a non-empty string")
        normalized[pid] = _require_sha256(digest, f"panel_hashes[{pid}]")
    return {
        "manifest_sha256": manifest_sha,
        "panel_hashes_digest": _sha256_json(normalized),
    }


def _validate_script_ready(evidence: Dict[str, Any]) -> Dict[str, str]:
    project_dir, chapter_number, slug = _project_context(evidence)
    if project_dir is not None:
        from src.render.visual_policy import validate_narration_evidence

        chap_dir = project_dir / "chapters" / chapter_number
        script_path = chap_dir / "script.json"
        manifest_path = chap_dir / "manifest.json"
        if not script_path.is_file():
            raise StateTransitionError(f"Missing script.json at {script_path}")
        if not manifest_path.is_file():
            raise StateTransitionError(f"Missing manifest.json at {manifest_path}")
        try:
            script = RecapScript.model_validate(read_json(script_path))
            manifest = PanelManifest.model_validate(read_json(manifest_path))
            if slug and script.project_slug != slug:
                raise ValueError("Script project_slug mismatch")
            if str(script.chapter_number) != chapter_number:
                raise ValueError("Script chapter_number mismatch")
            validate_narration_evidence(chap_dir, script, manifest)
        except Exception as exc:
            raise StateTransitionError(f"Script evidence validation failed: {exc}") from exc
        script_sha = calculate_sha256(script_path)
        if evidence.get("script_sha256") is not None and evidence["script_sha256"] != script_sha:
            raise StateTransitionError("Provided script_sha256 mismatches script.json")
        return {"script_sha256": script_sha}

    script_sha = _require_sha256(evidence.get("script_sha256"), "script_sha256")
    raw_script = evidence.get("script")
    if raw_script is None:
        raise StateTransitionError("SCRIPT_READY requires script evidence")
    try:
        script = raw_script if isinstance(raw_script, RecapScript) else RecapScript.model_validate(raw_script)
    except Exception as exc:
        raise StateTransitionError(f"Invalid script schema: {exc}") from exc
    if not script.segments:
        raise StateTransitionError("Script must contain at least one segment")
    panel_hashes = evidence.get("panel_hashes")
    known_panels = set(panel_hashes.keys()) if isinstance(panel_hashes, dict) else None
    for seg in script.segments:
        if seg.status == "needs_review" or "[CAN_XAC_MINH" in seg.narrator_text:
            raise StateTransitionError(f"Script segment {seg.seq_id} requires human verification")
        if not seg.evidence_panel_ids:
            raise StateTransitionError(f"Script segment {seg.seq_id} lacks panel evidence")
        if known_panels is not None:
            for pid in seg.evidence_panel_ids:
                if pid not in known_panels and Path(pid).stem not in known_panels:
                    raise StateTransitionError(f"Script segment {seg.seq_id} references unknown panel {pid}")
    return {"script_sha256": script_sha}


def _validate_audio_valid(evidence: Dict[str, Any]) -> Dict[str, str]:
    project_dir, chapter_number, _ = _project_context(evidence)
    if project_dir is not None:
        from src.render.artifact_validation import validate_audio

        chap_dir = project_dir / "chapters" / chapter_number
        script_path = chap_dir / "script.json"
        if not script_path.is_file():
            raise StateTransitionError(f"Missing script.json at {script_path}")
        try:
            script = RecapScript.model_validate(read_json(script_path))
        except Exception as exc:
            raise StateTransitionError(f"Invalid script.json: {exc}") from exc
        if not script.segments:
            raise StateTransitionError("Script has no segments for audio validation")

        clip_records = []
        chap_resolved = chap_dir.resolve()
        for seg in script.segments:
            if not seg.audio_path:
                raise StateTransitionError(f"Missing audio_path for segment {seg.seq_id}")
            audio_file = (chap_dir / seg.audio_path).resolve()
            if not audio_file.is_relative_to(chap_resolved) or not audio_file.is_file():
                raise StateTransitionError(f"Missing or unsafe audio file for segment {seg.seq_id}")
            try:
                report = validate_audio(audio_file)
            except Exception as exc:
                raise StateTransitionError(f"Audio validation failed for {seg.seq_id}: {exc}") from exc
            if seg.checksum != report["sha256"]:
                raise StateTransitionError(f"Audio checksum mismatch for segment {seg.seq_id}")
            if seg.duration is None or abs(float(seg.duration) - float(report["duration"])) > 0.001:
                raise StateTransitionError(f"Audio duration mismatch for segment {seg.seq_id}")
            clip_records.append(
                {"seq_id": seg.seq_id, "sha256": report["sha256"], "duration": round(float(report["duration"]), 6)}
            )
        audio_manifest_sha = _sha256_json(clip_records)
        if evidence.get("audio_manifest_sha256") is not None and evidence["audio_manifest_sha256"] != audio_manifest_sha:
            raise StateTransitionError("Provided audio_manifest_sha256 mismatches validated WAV clips")
        return {"audio_manifest_sha256": audio_manifest_sha}

    clips = evidence.get("audio_clips")
    if not isinstance(clips, (list, dict)) or not clips:
        raise StateTransitionError("AUDIO_VALID requires non-empty audio_clips evidence")
    normalized_records = []
    items = (
        [{"seq_id": k, **(v if isinstance(v, dict) else {})} for k, v in sorted(clips.items())]
        if isinstance(clips, dict)
        else clips
    )
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            raise StateTransitionError(f"Invalid audio_clips entry at index {idx}")
        if item.get("valid") is False:
            raise StateTransitionError(f"Audio clip invalid at index {idx}")
        sha = _require_sha256(item.get("sha256"), f"audio_clips[{idx}].sha256")
        dur = item.get("duration")
        if dur is None or float(dur) <= 0.0:
            raise StateTransitionError(f"Audio clip duration must be > 0 at index {idx}")
        normalized_records.append(
            {"seq_id": str(item.get("seq_id", idx)), "sha256": sha, "duration": round(float(dur), 6)}
        )
    computed = _sha256_json(normalized_records)
    provided = evidence.get("audio_manifest_sha256")
    if provided is not None:
        provided = _require_sha256(provided, "audio_manifest_sha256")
        return {"audio_manifest_sha256": provided}
    return {"audio_manifest_sha256": computed}


def _validate_visual_plan_valid(evidence: Dict[str, Any]) -> Dict[str, str]:
    project_dir, chapter_number, _ = _project_context(evidence)
    if project_dir is not None:
        from src.render.visual_policy import resolve_visual_plan, validate_visual_plan

        chap_dir = project_dir / "chapters" / chapter_number
        plan_path = chap_dir / "visual_plan.json"
        if not plan_path.is_file():
            raise StateTransitionError(f"Missing visual_plan.json at {plan_path}")
        try:
            plan = VisualPlan.load(plan_path)
            input_fp = _require_sha256(plan.input_fingerprint, "visual_plan.input_fingerprint")
            settings = read_json(project_dir / "project.json")["render_settings"]
            manifest = PanelManifest.model_validate(read_json(chap_dir / "manifest.json"))
            script = RecapScript.model_validate(read_json(chap_dir / "script.json"))
            expected = resolve_visual_plan(
                chap_dir,
                manifest,
                script,
                target_width=settings["target_width"],
                target_height=settings["target_height"],
                fps=settings["fps"],
                motion_enabled=settings["render_pan_mode"] == "animation",
            )
            if expected != plan or expected.input_fingerprint != input_fp:
                raise ValueError("VisualPlan differs from canonical source/config")
            validate_visual_plan(
                plan,
                chap_dir,
                manifest,
                script,
                target_width=settings["target_width"],
                target_height=settings["target_height"],
                fps=settings["fps"],
                motion_enabled=settings["render_pan_mode"] == "animation",
            )
        except StateTransitionError:
            raise
        except Exception as exc:
            raise StateTransitionError(f"VisualPlan validation failed: {exc}") from exc
        if evidence.get("input_fingerprint") is not None and evidence["input_fingerprint"] != input_fp:
            raise StateTransitionError("Provided input_fingerprint mismatches visual_plan.json")
        return {
            "visual_plan_input_fingerprint": input_fp,
            "visual_plan_sha256": calculate_sha256(plan_path),
        }

    input_fp = _require_sha256(evidence.get("input_fingerprint"), "input_fingerprint")
    plan_sha = _require_sha256(evidence.get("visual_plan_sha256"), "visual_plan_sha256")
    if "visual_plan" in evidence:
        try:
            raw_plan = evidence["visual_plan"]
            plan = raw_plan if isinstance(raw_plan, VisualPlan) else VisualPlan.model_validate(raw_plan)
        except Exception as exc:
            raise StateTransitionError(f"Invalid visual_plan schema: {exc}") from exc
        if plan.input_fingerprint != input_fp:
            raise StateTransitionError("visual_plan.input_fingerprint mismatch")
    return {
        "visual_plan_input_fingerprint": input_fp,
        "visual_plan_sha256": plan_sha,
    }


def _validate_render_valid(evidence: Dict[str, Any]) -> Dict[str, str]:
    project_dir, chapter_number, _ = _project_context(evidence)
    if project_dir is not None:
        chap_dir = project_dir / "chapters" / chapter_number
        if evidence.get("video_path"):
            video_path = Path(evidence["video_path"])
        elif (chap_dir / "video" / "final.mp4").is_file():
            video_path = chap_dir / "video" / "final.mp4"
        else:
            video_path = chap_dir / "video" / "preview.mp4"

        if evidence.get("subtitle_path"):
            subtitle_path = Path(evidence["subtitle_path"])
        elif (chap_dir / "subtitles" / "final.srt").is_file():
            subtitle_path = chap_dir / "subtitles" / "final.srt"
        else:
            subtitle_path = chap_dir / "subtitles" / "preview.srt"

        if not video_path.is_file() or video_path.stat().st_size <= 0:
            raise StateTransitionError(f"Rendered video missing or empty: {video_path}")
        if not subtitle_path.is_file() or subtitle_path.stat().st_size <= 0:
            raise StateTransitionError(f"Rendered subtitles missing or empty: {subtitle_path}")
        video_sha = calculate_sha256(video_path)
        sub_sha = calculate_sha256(subtitle_path)
        if evidence.get("video_sha256") is not None and evidence["video_sha256"] != video_sha:
            raise StateTransitionError("Provided video_sha256 mismatches rendered video")
        if evidence.get("subtitle_sha256") is not None and evidence["subtitle_sha256"] != sub_sha:
            raise StateTransitionError("Provided subtitle_sha256 mismatches rendered subtitles")
        return {"video_sha256": video_sha, "subtitle_sha256": sub_sha}

    video_sha = _require_sha256(evidence.get("video_sha256"), "video_sha256")
    sub_sha = _require_sha256(evidence.get("subtitle_sha256"), "subtitle_sha256")
    return {"video_sha256": video_sha, "subtitle_sha256": sub_sha}


def _validate_technically_valid(evidence: Dict[str, Any]) -> Dict[str, str]:
    project_dir, chapter_number, _ = _project_context(evidence)
    if project_dir is not None:
        chap_dir = project_dir / "chapters" / chapter_number
        av_path = chap_dir / "artifact_validation.json"
        if not av_path.is_file():
            raise StateTransitionError(f"Missing artifact_validation.json at {av_path}")
        try:
            report = read_json(av_path)
        except Exception as exc:
            raise StateTransitionError(f"Invalid artifact_validation.json: {exc}") from exc
        if report.get("valid") is not True:
            raise StateTransitionError("artifact_validation.json valid flag is not True")
        if str(report.get("chapter_number")) != chapter_number:
            raise StateTransitionError("artifact_validation.json chapter_number mismatch")
        video_info = report.get("video") or {}
        sub_info = report.get("subtitles") or {}
        if video_info.get("valid") is not True or sub_info.get("valid") is not True:
            raise StateTransitionError("artifact_validation.json video/subtitles validation is not True")
        v_sha = _require_sha256(video_info.get("sha256"), "artifact_validation.video.sha256")
        s_sha = _require_sha256(sub_info.get("sha256"), "artifact_validation.subtitles.sha256")

        prev = evidence.get("previous_evidence_hashes") or {}
        if "video_sha256" in prev and prev["video_sha256"] != v_sha:
            raise StateTransitionError("artifact_validation.video.sha256 mismatches RENDER_VALID video_sha256")
        if "subtitle_sha256" in prev and prev["subtitle_sha256"] != s_sha:
            raise StateTransitionError("artifact_validation.subtitles.sha256 mismatches RENDER_VALID subtitle_sha256")

        try:
            plan = VisualPlan.load(chap_dir / "visual_plan.json")
            script = RecapScript.model_validate(read_json(chap_dir / "script.json"))
            manifest = PanelManifest.model_validate(read_json(chap_dir / "manifest.json"))
        except Exception as exc:
            raise StateTransitionError(f"Missing/invalid chapter inputs for artifact validation: {exc}") from exc
        if report.get("plan_fingerprint") != hashlib.sha256(plan.model_dump_json().encode()).hexdigest():
            raise StateTransitionError("artifact_validation.json plan_fingerprint is stale")
        if report.get("script_fingerprint") != hashlib.sha256(script.model_dump_json().encode()).hexdigest():
            raise StateTransitionError("artifact_validation.json script_fingerprint is stale")
        if report.get("evidence_fingerprint") != hashlib.sha256(manifest.model_dump_json().encode()).hexdigest():
            raise StateTransitionError("artifact_validation.json evidence_fingerprint is stale")
        return {"artifact_validation_sha256": calculate_sha256(av_path)}

    report = evidence.get("artifact_validation")
    if not isinstance(report, dict) or report.get("valid") is not True:
        raise StateTransitionError("TECHNICALLY_VALID requires artifact_validation with valid=True")
    video_info = report.get("video") or {}
    sub_info = report.get("subtitles") or {}
    if video_info.get("valid") is not True or sub_info.get("valid") is not True:
        raise StateTransitionError("artifact_validation requires valid video and subtitles sections")
    _require_sha256(video_info.get("sha256"), "artifact_validation.video.sha256")
    _require_sha256(sub_info.get("sha256"), "artifact_validation.subtitles.sha256")
    _require_sha256(report.get("plan_fingerprint"), "artifact_validation.plan_fingerprint")
    _require_sha256(report.get("script_fingerprint"), "artifact_validation.script_fingerprint")
    _require_sha256(report.get("evidence_fingerprint"), "artifact_validation.evidence_fingerprint")
    av_sha = evidence.get("artifact_validation_sha256")
    return {
        "artifact_validation_sha256": _require_sha256(av_sha, "artifact_validation_sha256")
        if av_sha is not None
        else _sha256_json(report)
    }


def _validate_human_approved(evidence: Dict[str, Any]) -> Dict[str, str]:
    from src.core.human_review import HumanApproval, require_preview_approval

    project_dir, _, slug = _project_context(evidence)
    if project_dir is not None:
        try:
            approval = require_preview_approval(project_dir)
        except Exception as exc:
            raise StateTransitionError(f"Human approval validation failed: {exc}") from exc
        if slug and approval.project_slug != slug:
            raise StateTransitionError("HumanApproval project_slug mismatch")
        pa_sha = calculate_sha256(project_dir / "preview_approval.json")
        return {
            "preview_approval_sha256": pa_sha,
            "approval_artifact_hash": approval.artifact_hash,
            "approval_config_fingerprint": approval.config_fingerprint,
        }

    raw_approval = evidence.get("preview_approval")
    if raw_approval is None:
        raise StateTransitionError("HUMAN_APPROVED requires preview_approval HumanApproval evidence")
    try:
        approval = raw_approval if isinstance(raw_approval, HumanApproval) else HumanApproval.model_validate(raw_approval)
    except Exception as exc:
        raise StateTransitionError(f"Invalid HumanApproval payload: {exc}") from exc
    if not approval.reviewer.strip():
        raise StateTransitionError("HumanApproval reviewer cannot be blank")
    if evidence.get("expected_config_fingerprint") and approval.config_fingerprint != evidence["expected_config_fingerprint"]:
        raise StateTransitionError("HumanApproval config_fingerprint mismatch")
    if evidence.get("expected_artifact_hash") and approval.artifact_hash != evidence["expected_artifact_hash"]:
        raise StateTransitionError("HumanApproval artifact_hash mismatch")
    pa_sha = evidence.get("preview_approval_sha256")
    return {
        "preview_approval_sha256": _require_sha256(pa_sha, "preview_approval_sha256")
        if pa_sha is not None
        else hashlib.sha256(approval.model_dump_json().encode("utf-8")).hexdigest(),
        "approval_artifact_hash": approval.artifact_hash,
        "approval_config_fingerprint": approval.config_fingerprint,
    }


def _validate_production_ready(evidence: Dict[str, Any]) -> Dict[str, str]:
    from src.core.human_review import require_preview_approval

    project_dir, chapter_number, _ = _project_context(evidence)
    if project_dir is not None:
        try:
            approval = require_preview_approval(project_dir)
        except Exception as exc:
            raise StateTransitionError(f"PRODUCTION_READY requires valid HumanApproval: {exc}") from exc
        chap_dir = project_dir / "chapters" / chapter_number
        final_video = chap_dir / "video" / "final.mp4"
        if not final_video.is_file() or final_video.stat().st_size <= 0:
            raise StateTransitionError(f"Missing or empty final video at {final_video}")
        final_sha = calculate_sha256(final_video)
        if chapter_number == "1" and final_sha != approval.artifact_hash:
            raise StateTransitionError("Chapter 1 final.mp4 hash differs from approved preview artifact_hash")
        av_path = chap_dir / "artifact_validation.json"
        if not av_path.is_file():
            raise StateTransitionError(f"Missing artifact_validation.json at {av_path}")
        av_report = read_json(av_path)
        if av_report.get("valid") is not True or (av_report.get("video") or {}).get("sha256") != final_sha:
            raise StateTransitionError("artifact_validation.json does not validate final.mp4")
        return {"final_video_sha256": final_sha}

    final_sha = _require_sha256(evidence.get("final_video_sha256"), "final_video_sha256")
    if evidence.get("artifact_validation_valid") is not True:
        av = evidence.get("artifact_validation")
        if not isinstance(av, dict) or av.get("valid") is not True:
            raise StateTransitionError("PRODUCTION_READY requires validated final artifact")
    return {"final_video_sha256": final_sha}


def _load_youtube_receipt(evidence: Dict[str, Any]) -> Tuple[YouTubeReceipt, str]:
    project_dir, chapter_number, slug = _project_context(evidence)
    if project_dir is not None:
        chap_dir = project_dir / "chapters" / chapter_number
        yt_path = Path(evidence["youtube_receipt_path"]) if evidence.get("youtube_receipt_path") else chap_dir / "youtube_receipt.json"
        if not yt_path.is_file() and slug and chapter_number.lower() in {"feature", "all", "compilation", "full"}:
            yt_path = get_output_dir(slug) / "feature.youtube_receipt.json"
        if not yt_path.is_file():
            raise StateTransitionError(f"Missing youtube_receipt.json at {yt_path}")
        try:
            receipt = YouTubeReceipt.model_validate(read_json(yt_path))
        except Exception as exc:
            raise StateTransitionError(f"Invalid youtube_receipt.json: {exc}") from exc
        if slug and receipt.project_slug != slug:
            raise StateTransitionError("YouTubeReceipt project_slug mismatch")
        if str(receipt.chapter_number) != chapter_number:
            raise StateTransitionError("YouTubeReceipt chapter_number mismatch")
        return receipt, calculate_sha256(yt_path)

    raw_receipt = evidence.get("youtube_receipt")
    if raw_receipt is None:
        raise StateTransitionError("Missing youtube_receipt evidence")
    try:
        receipt = raw_receipt if isinstance(raw_receipt, YouTubeReceipt) else YouTubeReceipt.model_validate(raw_receipt)
    except Exception as exc:
        raise StateTransitionError(f"Invalid YouTubeReceipt evidence: {exc}") from exc
    return receipt, hashlib.sha256(receipt.model_dump_json().encode("utf-8")).hexdigest()


def _validate_uploaded(evidence: Dict[str, Any]) -> Dict[str, str]:
    receipt, receipt_sha = _load_youtube_receipt(evidence)
    if receipt.status not in {"uploaded", "verified"}:
        raise StateTransitionError(f"Invalid YouTubeReceipt status for UPLOADED: {receipt.status!r}")
    if not receipt.video_id.strip() or not receipt.account.strip() or not receipt.channel_id.strip():
        raise StateTransitionError("YouTubeReceipt requires non-empty video_id, account, and channel_id")
    if receipt.captions_required and not receipt.captions_uploaded:
        raise StateTransitionError("YouTubeReceipt captions_required=True but captions_uploaded=False")
    if receipt.thumbnail_required and not receipt.thumbnail_uploaded:
        raise StateTransitionError("YouTubeReceipt thumbnail_required=True but thumbnail_uploaded=False")
    return {"youtube_receipt_sha256": receipt_sha, "youtube_video_id": receipt.video_id}


def _validate_youtube_verified(evidence: Dict[str, Any]) -> Dict[str, str]:
    receipt, receipt_sha = _load_youtube_receipt(evidence)
    if receipt.status != "verified":
        raise StateTransitionError(f"YOUTUBE_VERIFIED requires status='verified', got {receipt.status!r}")
    if receipt.upload_status != "processed":
        raise StateTransitionError(f"YOUTUBE_VERIFIED requires upload_status='processed', got {receipt.upload_status!r}")
    if not receipt.verified_at or not receipt.verified_at.strip():
        raise StateTransitionError("YOUTUBE_VERIFIED requires non-empty verified_at")
    if not receipt.video_id.strip():
        raise StateTransitionError("YOUTUBE_VERIFIED requires non-empty video_id")
    if receipt.captions_required and (not receipt.captions_uploaded or receipt.has_captions is False):
        raise StateTransitionError("YOUTUBE_VERIFIED requires verified captions when captions_required=True")
    if receipt.thumbnail_required and (not receipt.thumbnail_uploaded or receipt.has_custom_thumbnail is False):
        raise StateTransitionError("YOUTUBE_VERIFIED requires verified custom thumbnail when thumbnail_required=True")
    return {"youtube_verified_receipt_sha256": receipt_sha}


def _load_archive_receipt(evidence: Dict[str, Any]) -> Tuple[ArchiveReceipt, str]:
    project_dir, _, slug = _project_context(evidence)
    if project_dir is not None:
        receipt_path = Path(evidence["archive_receipt_path"]) if evidence.get("archive_receipt_path") else project_dir / "archive_receipt.json"
        manifest_path = project_dir / "archive_manifest.json"
        if not receipt_path.is_file():
            raise StateTransitionError(f"Missing archive_receipt.json at {receipt_path}")
        if not manifest_path.is_file():
            raise StateTransitionError(f"Missing archive_manifest.json at {manifest_path}")
        try:
            receipt = ArchiveReceipt.model_validate(read_json(receipt_path))
        except Exception as exc:
            raise StateTransitionError(f"Invalid archive_receipt.json: {exc}") from exc
        if slug and receipt.project_slug != slug:
            raise StateTransitionError("ArchiveReceipt project_slug mismatch")
        if calculate_sha256(manifest_path) != receipt.manifest_sha256:
            raise StateTransitionError("archive_manifest.json SHA-256 mismatches archive_receipt.manifest_sha256")
        return receipt, calculate_sha256(receipt_path)

    raw_receipt = evidence.get("archive_receipt")
    if raw_receipt is None:
        raise StateTransitionError("Missing archive_receipt evidence")
    try:
        receipt = raw_receipt if isinstance(raw_receipt, ArchiveReceipt) else ArchiveReceipt.model_validate(raw_receipt)
    except Exception as exc:
        raise StateTransitionError(f"Invalid ArchiveReceipt evidence: {exc}") from exc
    return receipt, hashlib.sha256(receipt.model_dump_json().encode("utf-8")).hexdigest()


def _validate_archived(evidence: Dict[str, Any]) -> Dict[str, str]:
    receipt, _ = _load_archive_receipt(evidence)
    if receipt.status not in {"uploaded", "verified"}:
        raise StateTransitionError(f"Invalid ArchiveReceipt status for ARCHIVED: {receipt.status!r}")
    if not _GIT_SHA40_RE.fullmatch(receipt.commit_sha):
        raise StateTransitionError("ArchiveReceipt commit_sha must be a 40-char hex commit SHA")
    _require_sha256(receipt.manifest_sha256, "archive_receipt.manifest_sha256")
    return {
        "archive_commit_sha": receipt.commit_sha,
        "archive_manifest_sha256": receipt.manifest_sha256,
    }


def _validate_archive_verified(evidence: Dict[str, Any]) -> Dict[str, str]:
    receipt, receipt_sha = _load_archive_receipt(evidence)
    if receipt.status != "verified":
        raise StateTransitionError(f"ARCHIVE_VERIFIED requires status='verified', got {receipt.status!r}")
    if receipt.verification_revision != receipt.commit_sha:
        raise StateTransitionError("ARCHIVE_VERIFIED requires verification_revision == commit_sha")
    if not receipt.last_verified_at or not receipt.last_verified_at.strip():
        raise StateTransitionError("ARCHIVE_VERIFIED requires non-empty last_verified_at")
    if receipt.verified_file_count <= 0:
        raise StateTransitionError("ARCHIVE_VERIFIED requires verified_file_count > 0")
    return {"archive_verified_sha256": receipt_sha}


def _validate_restore_verified(evidence: Dict[str, Any]) -> Dict[str, str]:
    receipt, receipt_sha = _load_archive_receipt(evidence)
    if receipt.status != "verified":
        raise StateTransitionError(f"RESTORE_VERIFIED requires status='verified', got {receipt.status!r}")
    if receipt.verification_revision != receipt.commit_sha:
        raise StateTransitionError("RESTORE_VERIFIED requires verification_revision == commit_sha")
    if not receipt.restore_verified_at or not receipt.restore_verified_at.strip():
        raise StateTransitionError("RESTORE_VERIFIED requires non-empty restore_verified_at")
    if receipt.restored_revision != receipt.commit_sha:
        raise StateTransitionError("RESTORE_VERIFIED requires restored_revision == commit_sha")
    if receipt.restored_verified_files is None or receipt.restored_verified_files <= 0:
        raise StateTransitionError("RESTORE_VERIFIED requires restored_verified_files > 0")
    return {"archive_restore_verified_receipt_sha256": receipt_sha}


def _validate_cleanup_allowed(evidence: Dict[str, Any]) -> Dict[str, str]:
    # Require both verified restore archive receipt and verified YouTube receipt.
    archive_receipt, _ = _load_archive_receipt(evidence)
    _validate_restore_verified(evidence)
    yt_receipt, _ = _load_youtube_receipt(evidence)
    _validate_youtube_verified(evidence)
    prev = evidence.get("previous_evidence_hashes") or {}
    if "archive_commit_sha" in prev and prev["archive_commit_sha"] != archive_receipt.commit_sha:
        raise StateTransitionError("Archive commit_sha drifted since ARCHIVED")
    if "archive_manifest_sha256" in prev and prev["archive_manifest_sha256"] != archive_receipt.manifest_sha256:
        raise StateTransitionError("Archive manifest_sha256 drifted since ARCHIVED")
    if "youtube_video_id" in prev and prev["youtube_video_id"] != yt_receipt.video_id:
        raise StateTransitionError("YouTube video_id drifted since UPLOADED")
    return {
        "cleanup_gate_digest": _sha256_json(
            {
                "archive_commit_sha": archive_receipt.commit_sha,
                "archive_manifest_sha256": archive_receipt.manifest_sha256,
                "youtube_video_id": yt_receipt.video_id,
            }
        )
    }


TransitionValidator = Callable[[Dict[str, Any]], Dict[str, str]]

TRANSITION_VALIDATORS: Dict[Tuple[ReleaseState, ReleaseState], TransitionValidator] = {
    (ReleaseState.DRAFT, ReleaseState.SETUP_VALID): _validate_setup_valid,
    (ReleaseState.SETUP_VALID, ReleaseState.SOURCE_READY): _validate_source_ready,
    (ReleaseState.SOURCE_READY, ReleaseState.SCRIPT_READY): _validate_script_ready,
    (ReleaseState.SCRIPT_READY, ReleaseState.AUDIO_VALID): _validate_audio_valid,
    (ReleaseState.AUDIO_VALID, ReleaseState.VISUAL_PLAN_VALID): _validate_visual_plan_valid,
    (ReleaseState.VISUAL_PLAN_VALID, ReleaseState.RENDER_VALID): _validate_render_valid,
    (ReleaseState.RENDER_VALID, ReleaseState.TECHNICALLY_VALID): _validate_technically_valid,
    (ReleaseState.TECHNICALLY_VALID, ReleaseState.HUMAN_APPROVED): _validate_human_approved,
    (ReleaseState.HUMAN_APPROVED, ReleaseState.PRODUCTION_READY): _validate_production_ready,
    (ReleaseState.PRODUCTION_READY, ReleaseState.UPLOADED): _validate_uploaded,
    (ReleaseState.UPLOADED, ReleaseState.YOUTUBE_VERIFIED): _validate_youtube_verified,
    (ReleaseState.YOUTUBE_VERIFIED, ReleaseState.ARCHIVED): _validate_archived,
    (ReleaseState.ARCHIVED, ReleaseState.ARCHIVE_VERIFIED): _validate_archive_verified,
    (ReleaseState.ARCHIVE_VERIFIED, ReleaseState.RESTORE_VERIFIED): _validate_restore_verified,
    (ReleaseState.RESTORE_VERIFIED, ReleaseState.CLEANUP_ALLOWED): _validate_cleanup_allowed,
}


def can_transition(
    current_state: Union[ReleaseState, str],
    target_state: Union[ReleaseState, str],
) -> bool:
    """Return True iff target_state is an allowed next step from current_state."""
    try:
        cur = _coerce_state(current_state)
        tgt = _coerce_state(target_state)
    except StateTransitionError:
        return False
    return tgt in ALLOWED_TRANSITIONS.get(cur, ())


def transition(
    current_state: Union[ReleaseState, str],
    target_state: Union[ReleaseState, str],
    evidence: Optional[Dict[str, Any]] = None,
) -> TransitionResult:
    """Validate transition order and cryptographic evidence, returning TransitionResult."""
    cur = _coerce_state(current_state)
    tgt = _coerce_state(target_state)
    if not can_transition(cur, tgt):
        raise StateTransitionError(f"Illegal state transition: {cur.value} -> {tgt.value}")
    validator = TRANSITION_VALIDATORS.get((cur, tgt))
    if validator is None:
        raise StateTransitionError(f"No validator registered for transition: {cur.value} -> {tgt.value}")
    ev = _require_evidence_dict(evidence, f"{cur.value} -> {tgt.value}")
    recorded_hashes = validator(ev)
    return TransitionResult(
        previous_state=cur,
        target_state=tgt,
        evidence_hashes=recorded_hashes,
    )


def load_release_record(
    project_dir: Union[str, Path],
    *,
    chapter_number: Optional[str] = None,
) -> ReleaseRecord:
    """Load release_state.json or return an initial DRAFT ReleaseRecord for an existing project."""
    root = Path(project_dir)
    state_file = root / RELEASE_STATE_FILENAME
    if state_file.exists():
        try:
            raw = read_json(state_file)
            record = ReleaseRecord.model_validate(raw)
        except Exception as exc:
            raise StateTransitionError(f"Invalid release_state.json in {root}: {exc}") from exc
        if (
            chapter_number is not None
            and record.state != ReleaseState.DRAFT
            and record.chapter_number != str(chapter_number)
        ):
            raise StateTransitionError(
                f"Release state chapter mismatch: expected {record.chapter_number}, got {chapter_number}"
            )
        return record

    proj_file = root / "project.json"
    if not proj_file.exists():
        raise StateTransitionError(f"Cannot load release record: missing project.json in {root}")
    try:
        proj = read_json(proj_file)
        slug = str(proj["slug"])
    except Exception as exc:
        raise StateTransitionError(f"Invalid project.json in {root}: {exc}") from exc

    return ReleaseRecord(
        schema_version="4.1.0",
        project_slug=slug,
        chapter_number=str(chapter_number or "1"),
        state=ReleaseState.DRAFT,
        evidence_hashes={},
    )


def advance_release_state(
    project_dir: Union[str, Path],
    target_state: Union[ReleaseState, str],
    *,
    chapter_number: Optional[str] = None,
    evidence: Optional[Dict[str, Any]] = None,
) -> ReleaseRecord:
    """Validate evidence in project_dir and atomically persist the next ReleaseRecord."""
    root = Path(project_dir)
    state_file = root / RELEASE_STATE_FILENAME
    tgt = _coerce_state(target_state)

    with acquire_lock(state_file):
        record = load_release_record(root, chapter_number=chapter_number)
        resolved_chapter = str(chapter_number) if chapter_number is not None else record.chapter_number
        merged_evidence: Dict[str, Any] = dict(evidence or {})
        merged_evidence["project_dir"] = root
        merged_evidence["project_slug"] = record.project_slug
        merged_evidence["chapter_number"] = resolved_chapter
        merged_evidence["previous_evidence_hashes"] = dict(record.evidence_hashes)

        result = transition(record.state, tgt, evidence=merged_evidence)
        updated_hashes = dict(record.evidence_hashes)
        updated_hashes.update(result.evidence_hashes)

        next_record = ReleaseRecord.model_validate(
            {
                "schema_version": "4.1.0",
                "project_slug": record.project_slug,
                "chapter_number": resolved_chapter,
                "state": result.target_state,
                "evidence_hashes": updated_hashes,
            }
        )
        atomic_write_json(state_file, next_record)
        return next_record
