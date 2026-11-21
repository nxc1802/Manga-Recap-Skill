"""Production chapter orchestration using only the canonical frame renderer."""
from pathlib import Path
import shutil
import tempfile
from src.cli.exit_codes import CliValidationError
from src.core.schemas import CheckpointStatus, HumanReviewStatus, StageType
from src.core.storage import (load_recap_script, load_manifest, read_json, atomic_write_json,
                              acquire_lock, load_chapter_metadata, update_checkpoint)
from src.core.series_setup import require_series_setup
from src.core.human_review import require_preview_approval, require_voice_approval
from src.core.utils import assert_file_nonempty, get_wav_duration
from src.render.visual_policy import resolve_visual_plan
from src.render.fast_renderer import render_visual_plan_fast
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
    require_series_setup(slug, project_dir=root)
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
    if script is None or manifest is None:
        raise CliValidationError("Canonical script and manifest are required before rendering")
    metadata = load_chapter_metadata(chapter)
    if script.project_slug != slug or manifest.project_slug != slug or metadata.project_slug != slug:
        raise CliValidationError("Chapter source/project identity mismatch")
    if script.chapter_number != str(chapter_num) or manifest.chapter_number != str(chapter_num) or metadata.chapter_number != str(chapter_num):
        raise CliValidationError("Chapter number mismatch")
    config = read_json(root / "project.json")
    settings = config["render_settings"]
    if encoder == "auto":
        from src.production.hardware import resolve_video_encoder
        encoder = resolve_video_encoder("auto")[0]
    else:
        encoder = encoder or "libx264"
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
    promote = quality == "final" and str(chapter_num) == "1"
    plan_path = chapter / "visual_plan.json"
    video_dir = chapter / "video"
    output = Path(options.get("output_path") or video_dir / f"{quality}.mp4").resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    subtitles = chapter / "subtitles" / f"{quality}.srt"
    subtitle_engine = SubtitleGenerator(max_chars_per_line=sub_settings["max_chars_per_line"],
        max_lines=sub_settings["max_lines"], chunking_mode=sub_settings["chunking"])
    if promote:
        from src.core.visual_contract import VisualPlan

        plan = VisualPlan.load(plan_path)
    else:
        for segment in script.segments:
            duration = get_wav_duration(resolve_audio(chapter, segment))
            if segment.duration is None or abs(segment.duration - duration) > 0.001:
                raise CliValidationError(f"Audio duration changed for {segment.seq_id}")
        plan = resolve_visual_plan(chapter, manifest, script, target_width=settings["target_width"],
            target_height=settings["target_height"], fps=settings["fps"],
            motion_enabled=settings["render_pan_mode"] == "animation")
        if not plan_path.exists():
            plan.save(plan_path)
    update_checkpoint(chapter, StageType.RENDER, CheckpointStatus.RUNNING)
    try:
        if promote:
            copy_artifact(video_dir / "preview.mp4", output)
            copy_artifact(subtitles.with_name("preview.srt"), subtitles)
        else:
            subtitles.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="manga-subtitles-", dir=subtitles.parent) as work:
                pending_srt = Path(work) / "captions.srt"
                subtitle_engine.generate_srt_file(script, pending_srt)
                assert_file_nonempty(pending_srt)
                mixed = prepare_audio(root, chapter, script, audio_settings)
                render_visual_plan_fast(
                    plan, chapter, manifest, output, encoder=encoder, audio_path=mixed,
                    subtitle_path=pending_srt if settings["burn_subtitles"] else None,
                    burn_subtitles=bool(settings["burn_subtitles"]), subtitle_font_size=sub_settings["font_size"],
                    max_workers=int(options.get("render_workers", 4)), gpu_index=int(options.get("gpu_index", 0)),
                    quality_mode=quality,
                )
                pending_srt.replace(subtitles)
        assert_file_nonempty(output)
        assert_file_nonempty(subtitles)
        if quality == "preview" and str(chapter_num) == "1":
            final_artifact = video_dir / "final.mp4"
            if final_artifact.is_file():
                final_artifact.replace(video_dir / "final.previous.mp4")
        update_checkpoint(chapter, StageType.RENDER, CheckpointStatus.COMPLETED,
            artifacts=[str(output), str(subtitles.relative_to(chapter)), "visual_plan.json"])
        with acquire_lock(chapter / "chapter.json"):
            metadata = load_chapter_metadata(chapter)
            metadata.technical_status = CheckpointStatus.COMPLETED
            metadata.overall_status = CheckpointStatus.NEEDS_REVIEW if quality == "preview" else CheckpointStatus.COMPLETED
            metadata.human_review_status = HumanReviewStatus.APPROVED if promote else HumanReviewStatus.UNREVIEWED
            metadata.stats.video_path = str(output)
            metadata.stats.final_video_duration_seconds = plan.total_duration_sec
            atomic_write_json(chapter / "chapter.json", metadata)
    except Exception as exc:
        update_checkpoint(chapter, StageType.RENDER, CheckpointStatus.FAILED, error=str(exc))
        raise
    return dict(video_path=str(output), subtitle_path=str(subtitles), visual_plan=str(plan_path),
                duration_seconds=plan.total_duration_sec)


def copy_artifact(source, destination):
    """Replace a media file atomically on its destination volume."""
    assert_file_nonempty(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="manga-copy-", dir=destination.parent) as work:
        pending = Path(work) / destination.name
        shutil.copy2(source, pending)
        pending.replace(destination)


def validate_existing_chapter(project_dir, chapter_num, quality="final", video_path=None):
    chapter = Path(project_dir) / "chapters" / str(chapter_num)
    assert_file_nonempty(video_path or chapter / "video" / f"{quality}.mp4")
    assert_file_nonempty(chapter / "subtitles" / f"{quality}.srt")


def prepare_audio(root, chapter, script, settings):
    mixer = AudioMixer(silence_padding_sec=0, tail_padding_sec=0)
    speech, _ = mixer.concatenate_speech(script.segments, chapter, chapter / "audio" / "speech_master.wav")
    bgm = settings.get("bgm_path")
    bgm_path = root / bgm if bgm else None
    if bgm_path:
        assert_file_nonempty(bgm_path)
    return mixer.mix_audio_track(speech, chapter / "audio" / "mixed.m4a", bgm_path=bgm_path,
        bgm_volume=settings["bgm_volume"], voice_volume=settings["voice_volume"], fadeout_seconds=0)
