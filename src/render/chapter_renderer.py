"""Production chapter orchestration using only the canonical frame renderer."""
from pathlib import Path
import shutil
import subprocess
from src.cli.exit_codes import CliValidationError
from src.core.schemas import CheckpointStatus, StageType
from src.core.storage import (load_recap_script, load_manifest, read_json, atomic_write_json,
                              update_checkpoint)
from src.core.series_setup import require_series_setup
from src.core.human_review import require_preview_approval, require_voice_approval
from src.render.artifact_validation import validate_audio, validate_final_artifact
from src.render.visual_policy import generate_visual_plan, resolve_visual_plan
from src.render.visual_plan_renderer import render_visual_plan
from src.render.audio_mixer import AudioMixer
from src.render.subtitle import SubtitleGenerator


def resolve_audio(chapter, segment):
    if not segment.audio_path:
        raise CliValidationError(f"Missing audio path: {segment.seq_id}")
    path = (chapter / segment.audio_path).resolve()
    if not path.is_relative_to(chapter.resolve()):
        raise CliValidationError("Audio must reside inside chapter payload")
    return path


def render_chapter(project_dir, chapter_num, quality="final", encoder="libx264", **options):
    root = Path(project_dir).resolve()
    slug = read_json(root / "project.json")["slug"]
    setup = require_series_setup(slug, project_dir=root)
    require_voice_approval(root)
    if quality not in {"preview", "final"}:
        raise CliValidationError("Unknown render quality")
    if options.get("allow_placeholders"):
        raise CliValidationError("Placeholder artifacts are never production inputs")
    if quality == "final" or str(chapter_num) != "1":
        require_preview_approval(root)
    chapter = root / "chapters" / str(chapter_num)
    script = load_recap_script(chapter)
    manifest = load_manifest(chapter)
    from src.core.storage import load_chapter_metadata
    metadata = load_chapter_metadata(chapter)
    if script.project_slug != slug or manifest.project_slug != slug or metadata.project_slug != slug:
        raise CliValidationError("Chapter source/project identity mismatch")
    if script.chapter_number != str(chapter_num) or manifest.chapter_number != str(chapter_num) or metadata.chapter_number != str(chapter_num):
        raise CliValidationError("Chapter number mismatch")
    config = read_json(root / "project.json")
    settings = config["render_settings"]
    encoder = "libx264" if encoder in (None, "auto") else encoder
    if settings["video_mode"] not in {"auto", "landscape_fit"}:
        raise CliValidationError("Canonical production currently supports auto/landscape_fit framing")
    if options.get("video_mode") is not None and options["video_mode"] != settings["video_mode"]:
        raise CliValidationError("Video mode override conflicts with confirmed project config")
    sub_settings = settings["subtitle"]
    for key, config_key in (("subtitle_lines", "max_lines"), ("subtitle_font_size", "font_size"), ("subtitle_chunking", "chunking")):
        if options.get(key) is not None and options[key] != sub_settings[config_key]:
            raise CliValidationError(f"{key} override conflicts with project config")
    audio_settings = config["audio_settings"]
    # Runtime overrides cannot change explicitly confirmed settings.
    overrides = {"burn_subtitles": settings["burn_subtitles"],
                 "allow_animation": settings["render_pan_mode"] == "animation"}
    for key, expected in overrides.items():
        if options.get(key) is not None and options[key] != expected:
            raise CliValidationError(f"{key} conflicts with confirmed setup")
    if options.get("bgm_path") is not None and options["bgm_path"] != audio_settings.get("bgm_path"):
        raise CliValidationError("BGM override conflicts with confirmed setup")
    audio_paths = [resolve_audio(chapter, segment) for segment in script.segments]
    for segment, path in zip(script.segments, audio_paths):
        report = validate_audio(path)
        if segment.checksum != report["sha256"] or segment.duration is None or abs(segment.duration - report["duration"]) > 0.001:
            raise CliValidationError(f"Audio evidence mismatch: {segment.seq_id}")
    plan = resolve_visual_plan(chapter, manifest, script, target_width=settings["target_width"],
        target_height=settings["target_height"], fps=settings["fps"],
        motion_enabled=settings["render_pan_mode"] == "animation")
    plan_path = chapter / "visual_plan.json"
    # The final chapter-one promotion must use the exact reviewed plan.
    if quality == "final" and str(chapter_num) == "1":
        from src.core.visual_contract import VisualPlan
        if VisualPlan.load(plan_path) != plan:
            raise CliValidationError("Final plan differs from human-reviewed preview")
    plan.save(plan_path)
    video_dir = chapter / "video"
    video_dir.mkdir(parents=True, exist_ok=True)
    output = Path(options.get("output_path") or video_dir / f"{quality}.mp4")
    subtitles = chapter / "subtitles" / f"{quality}.srt"
    subtitle_engine = SubtitleGenerator(max_chars_per_line=sub_settings["max_chars_per_line"],
        max_lines=sub_settings["max_lines"], chunking_mode=sub_settings["chunking"])
    subtitle_engine.generate_srt_file(script, subtitles)
    update_checkpoint(chapter, StageType.RENDER, CheckpointStatus.RUNNING)
    try:
        if quality == "final" and str(chapter_num) == "1":
            # Promote exact reviewed artifact; never silently replace it with a
            # differently framed or encoded video after approval.
            shutil.copy2(video_dir / "preview.mp4", output)
        else:
            raw = render_visual_plan(plan, chapter, manifest, video_dir / "visual.mp4", encoder=encoder or "libx264")
            mixer = AudioMixer(silence_padding_sec=0, tail_padding_sec=0)
            speech, _ = mixer.concatenate_speech(script.segments, chapter, chapter / "audio" / "speech_master.wav")
            bgm = audio_settings.get("bgm_path")
            bgm_path = (root / bgm).resolve() if bgm and not Path(bgm).is_absolute() else bgm
            if bgm_path and not Path(bgm_path).is_file():
                raise CliValidationError("Configured BGM is missing")
            mixed = mixer.mix_audio_track(speech, chapter / "audio" / "mixed.m4a", bgm_path=bgm_path,
                bgm_volume=audio_settings["bgm_volume"], voice_volume=audio_settings["voice_volume"], fadeout_seconds=0)
            partial = output.with_name(f".{output.stem}.mux.mp4")
            cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(raw), "-i", str(mixed), "-map", "0:v:0", "-map", "1:a:0"]
            if settings["burn_subtitles"]:
                # A generated, fixed relative path avoids ffmpeg filter escaping
                # of user-controlled filesystem names.
                shutil.copy2(subtitles, video_dir / "captions.srt")
                cmd += ["-vf", f"subtitles=captions.srt:force_style='Fontsize={sub_settings['font_size']}'", "-c:v", encoder or "libx264"]
            else:
                cmd += ["-c:v", "copy"]
            cmd += ["-c:a", "copy", "-movflags", "+faststart", str(partial)]
            result = subprocess.run(cmd, cwd=video_dir, capture_output=True, timeout=3600)
            if result.returncode:
                raise RuntimeError(result.stderr.decode(errors="replace")[-2000:])
            partial.replace(output)
        report = validate_final_artifact(output, subtitles, audio_paths, plan, script, manifest, chapter_num, chapter_dir=chapter)
        atomic_write_json(chapter / "artifact_validation.json", report)
        update_checkpoint(chapter, StageType.RENDER, CheckpointStatus.COMPLETED,
            artifacts=[str(output.relative_to(chapter)), str(subtitles.relative_to(chapter)), "visual_plan.json", "artifact_validation.json"])
        from src.core.storage import acquire_lock, load_chapter_metadata
        from src.core.schemas import HumanReviewStatus
        with acquire_lock(chapter / "chapter.json"):
            metadata = load_chapter_metadata(chapter)
            metadata.technical_status = CheckpointStatus.COMPLETED
            metadata.overall_status = CheckpointStatus.NEEDS_REVIEW if quality == "preview" else CheckpointStatus.COMPLETED
            metadata.human_review_status = HumanReviewStatus.UNREVIEWED
            atomic_write_json(chapter / "chapter.json", metadata)
    except Exception as exc:
        update_checkpoint(chapter, StageType.RENDER, CheckpointStatus.FAILED, error=str(exc))
        raise
    return dict(video_path=str(output), subtitle_path=str(subtitles), visual_plan=str(plan_path), validation=report,
                duration_seconds=plan.total_duration_sec)


def validate_existing_chapter(project_dir, chapter_num, quality="final", video_path=None):
    """Revalidate cache/compile inputs; a completed status is not evidence."""
    root = Path(project_dir)
    chapter = root / "chapters" / str(chapter_num)
    script, manifest = load_recap_script(chapter), load_manifest(chapter)
    from src.core.visual_contract import VisualPlan
    plan = VisualPlan.load(chapter / "visual_plan.json")
    settings = read_json(root / "project.json")["render_settings"]
    expected = resolve_visual_plan(chapter, manifest, script, target_width=settings["target_width"],
        target_height=settings["target_height"], fps=settings["fps"], motion_enabled=settings["render_pan_mode"] == "animation")
    if expected != plan:
        raise CliValidationError("Cached VisualPlan is stale")
    return validate_final_artifact(video_path or chapter / "video" / f"{quality}.mp4",
        chapter / "subtitles" / f"{quality}.srt", [resolve_audio(chapter, seg) for seg in script.segments],
        plan, script, manifest, chapter_num, chapter_dir=chapter)
