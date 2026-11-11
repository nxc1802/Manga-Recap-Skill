"""Validate downloaded output and local/cloud semantic parity before publishing state."""
import shutil
from pathlib import Path
from src.core.storage import read_json, atomic_write_json, load_manifest, load_recap_script, load_chapter_metadata, acquire_lock
from src.core.schemas import RecapScript, CheckpointStatus, StageType
from src.core.visual_contract import VisualPlan
from src.render.visual_policy import resolve_visual_plan
from src.render.artifact_validation import fingerprint, validate_audio, validate_final_artifact
from src.render.chapter_renderer import resolve_audio


def sync_result(project, number, downloaded, job_type, expected_manifest_hash):
    if not expected_manifest_hash:
        raise ValueError("Missing trusted dispatch manifest fingerprint")
    project, downloaded = Path(project), Path(downloaded).resolve()
    receipt = read_json(downloaded / "result_manifest.json")
    config = read_json(project / "project.json")
    if receipt.get("payload_manifest_sha256") != expected_manifest_hash or receipt.get("project_slug") != config["slug"] or receipt.get("chapter_number") != str(number) or receipt.get("job_type") != job_type:
        raise ValueError("Cloud result does not match dispatched job")
    actual = set()
    for path in downloaded.rglob("*"):
        if path.is_symlink():
            raise ValueError("Symlink in cloud results")
        if path.is_file() and path != downloaded / "result_manifest.json":
            actual.add(path.relative_to(downloaded).as_posix())
    if actual != set(receipt.get("files", {})):
        raise ValueError("Cloud result inventory mismatch")
    for relative, digest in receipt["files"].items():
        path = (downloaded / relative).resolve()
        if not path.is_relative_to(downloaded) or fingerprint(path) != digest:
            raise ValueError("Cloud output hash mismatch")
    chapter = project / "chapters" / str(number)
    incoming = downloaded / "chapter"
    local_script = load_recap_script(chapter)
    script = load_recap_script(incoming)
    if not script.segments or len(script.segments) != len(local_script.segments):
        raise ValueError("Cloud script segment mismatch")
    for old, new in zip(local_script.segments, script.segments):
        for key in ("seq_id", "evidence_panel_ids", "narrator_text", "camera_motion", "focus_box", "emotion", "emotion_cue"):
            if getattr(old, key) != getattr(new, key):
                raise ValueError(f"Cloud changed source narration/planning: {key}")
    audio_paths = [resolve_audio(incoming, segment) for segment in script.segments]
    for segment, path in zip(script.segments, audio_paths):
        audio = validate_audio(path)
        if segment.checksum != audio["sha256"] or segment.duration is None or abs(segment.duration - audio["duration"]) > 0.001:
            raise ValueError("Cloud audio metadata mismatch")
    report = None
    quality = "preview" if str(number) == "1" else "final"
    if job_type in {"render", "produce"}:
        settings = config["render_settings"]
        manifest = load_manifest(chapter)
        expected = resolve_visual_plan(chapter, manifest, script,
            target_width=settings["target_width"], target_height=settings["target_height"], fps=settings["fps"],
            motion_enabled=settings["render_pan_mode"] == "animation")
        plan = VisualPlan.load(incoming / "visual_plan.json")
        if expected != plan:
            raise ValueError("Cloud VisualPlan semantic parity failed")
        report = validate_final_artifact(incoming / "video" / f"{quality}.mp4", incoming / "subtitles" / f"{quality}.srt",
            audio_paths, plan, script, manifest, number, chapter_dir=incoming)
    # No imported status strings are trusted; construct completion from local
    # chapter metadata only after semantic/audio validation above.
    with acquire_lock(chapter / "chapter.json"):
        metadata = load_chapter_metadata(chapter)
        for subdir in ("audio", "video", "subtitles"):
            if (incoming / subdir).exists():
                shutil.copytree(incoming / subdir, chapter / subdir, dirs_exist_ok=True)
        atomic_write_json(chapter / "script.json", script)
        if report is not None:
            atomic_write_json(chapter / "visual_plan.json", plan)
            atomic_write_json(chapter / "artifact_validation.json", report)
            metadata.stages[StageType.RENDER].status = CheckpointStatus.COMPLETED
        metadata.stages[StageType.AUDIO].status = CheckpointStatus.COMPLETED
        # Preview completion is technical evidence, never human approval.
        metadata.technical_status = CheckpointStatus.COMPLETED if report else CheckpointStatus.PENDING
        atomic_write_json(chapter / "chapter.json", metadata)
    return dict(status="success", job_type=job_type, project_slug=config["slug"], chapter_number=str(number),
                video_path=str(chapter / "video" / f"{quality}.mp4") if report else None,
                subtitle_path=str(chapter / "subtitles" / f"{quality}.srt") if report else None,
                final_video=str(chapter / "video" / f"{quality}.mp4") if report else None,
                subtitles=str(chapter / "subtitles" / f"{quality}.srt") if report else None,
                validation=report, semantic_parity=report is not None)
