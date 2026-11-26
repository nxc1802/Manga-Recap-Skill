"""Production chapter orchestration and feature compilation using the canonical frame renderer."""
import contextlib
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, Dict, List, Optional
from src.core.checkpoints import record_chapter_render_result, update_checkpoint
from src.core.errors import (
    DependencyNotFoundError,
    DomainValidationError,
    StorageFileNotFoundError,
)
from src.core.logger import logger
from src.core.project import (
    get_output_dir,
    load_chapter_metadata,
    load_manifest,
    load_recap_script,
    resolve_existing_project_dir,
)
from src.core.schemas import (
    ChapterMarker,
    ChapterTimelineItem,
    CheckpointStatus,
    FeatureTimeline,
    StageType,
)
from src.core.storage import atomic_write_json, read_json
from src.core.series_setup import require_series_setup
from src.core.human_review import require_preview_approval, require_voice_approval
from src.core.utils import assert_file_nonempty, get_wav_duration
from src.media.ffmpeg import probe_media_duration, run_ffmpeg
from src.render.visual_policy import resolve_visual_plan
from src.render.fast_renderer import render_visual_plan_fast
from src.render.audio_mixer import AudioMixer
from src.render.subtitle import SubtitleGenerator


def resolve_audio(chapter, segment):
    if not segment.audio_path:
        raise DomainValidationError(f"Missing audio path: {segment.seq_id}")
    path = (chapter / segment.audio_path).resolve()
    if not path.is_relative_to(chapter.resolve()):
        raise DomainValidationError("Audio must reside inside chapter payload")
    return path


def render_chapter(project_dir, chapter_num, quality="final", encoder="libx264", **options):
    root = Path(project_dir).resolve()
    slug = read_json(root / "project.json")["slug"]
    require_series_setup(slug, project_dir=root)
    require_voice_approval(root)
    if quality not in {"preview", "final"}:
        raise DomainValidationError("Unknown render quality")
    if options.get("allow_placeholders"):
        raise DomainValidationError("Placeholder artifacts are never production inputs")
    if quality == "final" or str(chapter_num) != "1":
        require_preview_approval(root)
    chapter = root / "chapters" / str(chapter_num)
    script = load_recap_script(chapter)
    manifest = load_manifest(chapter)
    if script is None or manifest is None:
        raise DomainValidationError("Canonical script and manifest are required before rendering")
    metadata = load_chapter_metadata(chapter)
    if script.project_slug != slug or manifest.project_slug != slug or metadata.project_slug != slug:
        raise DomainValidationError("Chapter source/project identity mismatch")
    if script.chapter_number != str(chapter_num) or manifest.chapter_number != str(chapter_num) or metadata.chapter_number != str(chapter_num):
        raise DomainValidationError("Chapter number mismatch")
    config = read_json(root / "project.json")
    settings = config["render_settings"]
    if encoder == "auto":
        from src.production.hardware import resolve_video_encoder
        encoder = resolve_video_encoder("auto")[0]
    else:
        encoder = encoder or "libx264"
    if settings["video_mode"] not in {"auto", "landscape_fit"}:
        raise DomainValidationError("Canonical production currently supports auto/landscape_fit framing")
    if options.get("video_mode") is not None and options["video_mode"] != settings["video_mode"]:
        raise DomainValidationError("Video mode override conflicts with confirmed project config")
    sub_settings = settings["subtitle"]
    for key, config_key in (("subtitle_lines", "max_lines"), ("subtitle_font_size", "font_size"), ("subtitle_chunking", "chunking")):
        if options.get(key) is not None and options[key] != sub_settings[config_key]:
            raise DomainValidationError(f"{key} override conflicts with project config")
    audio_settings = config["audio_settings"]
    # Runtime overrides cannot change explicitly confirmed settings.
    overrides = {"burn_subtitles": settings["burn_subtitles"],
                 "allow_animation": settings["render_pan_mode"] == "animation"}
    for key, expected in overrides.items():
        if options.get(key) is not None and options[key] != expected:
            raise DomainValidationError(f"{key} conflicts with confirmed setup")
    if options.get("bgm_path") is not None and options["bgm_path"] != audio_settings.get("bgm_path"):
        raise DomainValidationError("BGM override conflicts with confirmed setup")
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
                raise DomainValidationError(f"Audio duration changed for {segment.seq_id}")
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
        record_chapter_render_result(
            chapter,
            quality=quality,
            promote=promote,
            video_path=output,
            duration_seconds=plan.total_duration_sec,
        )
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


def compile_project_chapters(
    project_slug: str,
    *,
    chapters: Optional[str] = None,
    output: Optional[str] = None,
    extract_audio: bool = True,
    approved: bool = False,
) -> Dict[str, Any]:
    """Concatenates final chapter videos into a continuous feature video + timeline."""
    from src.youtube.metadata import format_seconds_to_timestamp

    if not shutil.which("ffmpeg"):
        raise DependencyNotFoundError("FFmpeg executable not found on system PATH. Please install FFmpeg.")
    if not project_slug:
        raise DomainValidationError("Project slug (--project) is required.")

    project_dir = resolve_existing_project_dir(project_slug)
    requested_chaps = [c.strip() for c in str(chapters).split(",") if c.strip()] if chapters else []

    chapter_videos: Dict[int, Path] = {}
    chapters_dir = project_dir / "chapters"
    if chapters_dir.exists():
        for c_entry in os.listdir(chapters_dir):
            if not c_entry.isdigit():
                continue
            c_num = int(c_entry)
            if requested_chaps and str(c_num) not in requested_chaps:
                continue
            c_video_dir = chapters_dir / c_entry / "video"
            if c_video_dir.exists():
                candidate_mp4s = [f for f in c_video_dir.glob("*.mp4") if not f.name.startswith(".")]
                finals = sorted(
                    f for f in candidate_mp4s
                    if "final" in f.name.lower() and not f.name.startswith(("final.previous.", "final.stale."))
                )
                non_previews = sorted(
                    f for f in candidate_mp4s
                    if "preview" not in f.name.lower() and not f.name.startswith(("final.previous.", "final.stale."))
                )
                if finals:
                    chapter_videos[c_num] = next((file for file in finals if file.name == "final.mp4"), finals[-1])
                elif non_previews:
                    chapter_videos[c_num] = non_previews[-1]
                elif candidate_mp4s:
                    raise StorageFileNotFoundError(
                        f"Chapter {c_num} only has preview video ({candidate_mp4s[0].name}). "
                        "Final rendered video is required for compilation; preview video cannot be used for final export."
                    )

    legacy_video_dir = project_dir / "video" / "chapters"
    if legacy_video_dir.exists():
        for f in os.listdir(legacy_video_dir):
            if "_raw_temp" in f or not f.endswith(".mp4"):
                continue
            m = re.match(r"^chapter_(\d+)(?:_.*?)?_(?:v)?(\d+)\.mp4$", f)
            if m:
                c_num = int(m.group(1))
                if requested_chaps and str(c_num) not in requested_chaps:
                    continue
                if "preview" in f.lower() and "final" not in f.lower():
                    continue
                if c_num not in chapter_videos:
                    chapter_videos[c_num] = legacy_video_dir / f

    if not chapter_videos:
        raise StorageFileNotFoundError(f"No rendered chapter videos found to compile in {project_dir}")

    if requested_chaps:
        missing_chaps = [c for c in requested_chaps if int(c) not in chapter_videos]
        if missing_chaps:
            raise StorageFileNotFoundError(
                f"Missing final rendered video for requested chapter(s): {', '.join(missing_chaps)} in {project_dir}"
            )

    for c_num, vid_path in chapter_videos.items():
        validate_existing_chapter(project_dir, c_num, video_path=vid_path)
        if "preview" in vid_path.name.lower() and "final" not in vid_path.name.lower():
            raise StorageFileNotFoundError(
                f"Chapter {c_num} video '{vid_path.name}' is a preview video. "
                "Final rendered video is required for compilation; preview video cannot be used for final export."
            )

    if not approved:
        require_voice_approval(project_dir)
        require_preview_approval(project_dir)
        for c_num in sorted(chapter_videos.keys()):
            chap_json = chapters_dir / str(c_num) / "chapter.json"
            if chap_json.exists():
                try:
                    c_meta = read_json(chap_json)
                except Exception as e:
                    raise DomainValidationError(f"Failed to read metadata for Chapter {c_num} from '{chap_json}': {e}") from e
                if not isinstance(c_meta, dict):
                    raise DomainValidationError(f"Invalid metadata format in '{chap_json}': expected JSON object.")
                if c_meta.get("overall_status") == CheckpointStatus.NEEDS_REVIEW.value:
                    raise DomainValidationError(
                        f"Chapter {c_num} requires human review before compilation. Supply --approved to bypass."
                    )

    sorted_chaps = sorted(chapter_videos.keys())
    compile_list = [chapter_videos[c] for c in sorted_chaps]
    out_dir = get_output_dir(project_slug)
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = Path(output).resolve() if output else (project_dir / "video" / "final.mp4")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    concat_list_path = project_dir / "compile_concat_list.txt"
    with open(concat_list_path, "w", encoding="utf-8") as f:
        for p in compile_list:
            f.write(f"file '{p.resolve()}'\n")

    cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list_path), "-c", "copy", str(output_path)]
    try:
        run_ffmpeg(cmd, description="Compile chapter video concatenation")
        if not output_path.exists() or output_path.stat().st_size == 0:
            raise RuntimeError(f"Compiled video is missing or empty: {output_path}")
    finally:
        concat_list_path.unlink(missing_ok=True)

    default_staged_path = out_dir / f"{project_slug}_full_recap.mp4"
    if output_path.resolve() != default_staged_path.resolve():
        with contextlib.suppress(Exception):
            shutil.copy2(output_path, default_staged_path)

    final_audio_path = None
    default_staged_audio = None
    if extract_audio:
        final_audio_path = project_dir / "audio" / "final.wav"
        final_audio_path.parent.mkdir(parents=True, exist_ok=True)
        cmd_audio = [
            "ffmpeg", "-y", "-i", str(output_path),
            "-vn", "-acodec", "pcm_s16le", "-ar", "48000", "-ac", "2",
            str(final_audio_path),
        ]
        run_ffmpeg(cmd_audio, description="Extract master audio from compiled video")
        if not final_audio_path.exists() or final_audio_path.stat().st_size == 0:
            raise RuntimeError(f"Compiled audio is missing or empty: {final_audio_path}")
        default_staged_audio = out_dir / f"{project_slug}_full_recap.wav"
        if final_audio_path.resolve() != default_staged_audio.resolve():
            with contextlib.suppress(Exception):
                shutil.copy2(final_audio_path, default_staged_audio)

    current_offset = 0.0
    timeline_items: List[ChapterTimelineItem] = []
    youtube_markers: List[ChapterMarker] = []
    for c_num in sorted_chaps:
        vid_p = chapter_videos[c_num]
        dur = probe_media_duration(vid_p)
        c_chap_dir = chapters_dir / str(c_num) if chapters_dir.exists() else None
        c_title = f"Tập {c_num}"
        sub_markers: List[ChapterMarker] = []

        if c_chap_dir and c_chap_dir.exists():
            c_json = c_chap_dir / "chapter.json"
            if c_json.exists():
                with contextlib.suppress(Exception):
                    c_title = read_json(c_json).get("title") or c_title
            s_json = c_chap_dir / "script.json"
            if s_json.exists():
                with contextlib.suppress(Exception):
                    s_data = read_json(s_json)
                    if dur <= 0:
                        dur = float(s_data.get("total_duration_seconds", 0.0) or 0.0)
                    scene_time = current_offset
                    for s_idx, seg in enumerate(s_data.get("segments", [])):
                        seg_dur = float(seg.get("duration", 0.0) or 4.0)
                        txt = seg.get("narrator_text", "")
                        lbl = f"{c_title}: {txt[:35].strip()}..." if txt else f"{c_title} - Cảnh {s_idx + 1}"
                        sub_markers.append(ChapterMarker(
                            time=format_seconds_to_timestamp(scene_time),
                            label=lbl,
                            seconds=round(scene_time, 2),
                        ))
                        scene_time += seg_dur

        if dur <= 0:
            dur = 10.0

        start_sec, end_sec = current_offset, current_offset + dur
        current_offset = end_sec
        item = ChapterTimelineItem(
            chapter_number=str(c_num),
            title=c_title,
            start_seconds=round(start_sec, 2),
            end_seconds=round(end_sec, 2),
            duration_seconds=round(dur, 2),
            start_timestamp=format_seconds_to_timestamp(start_sec),
            end_timestamp=format_seconds_to_timestamp(end_sec),
            source_video_path=str(vid_p),
            sub_markers=sub_markers,
        )
        timeline_items.append(item)
        youtube_markers.append(ChapterMarker(
            time=item.start_timestamp,
            label=f"Tập {c_num}: {c_title}" if not c_title.startswith(f"Tập {c_num}") else c_title,
            seconds=round(start_sec, 2),
        ))

    feature_timeline = FeatureTimeline(
        project_slug=project_slug,
        compiled_video_path=str(output_path),
        total_duration_seconds=round(current_offset, 2),
        chapters_count=len(sorted_chaps),
        chapters=timeline_items,
        youtube_chapter_markers=youtube_markers,
    )
    feature_timeline_path = project_dir / "feature_timeline.json"
    atomic_write_json(feature_timeline_path, feature_timeline)
    staged_timeline_path = out_dir / f"{project_slug}_timeline.json"
    atomic_write_json(staged_timeline_path, feature_timeline)

    return {
        "project_slug": project_slug,
        "compiled_video": str(output_path),
        "compiled_audio": str(final_audio_path) if final_audio_path else None,
        "export_video_path": str(default_staged_path if default_staged_path.exists() else output_path),
        "export_audio_path": str(default_staged_audio if (default_staged_audio and default_staged_audio.exists()) else final_audio_path),
        "chapters_compiled": [str(c) for c in sorted_chaps],
        "timeline_path": str(staged_timeline_path),
        "feature_timeline": str(feature_timeline_path),
    }
