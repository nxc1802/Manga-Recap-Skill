"""
src/cli/render_cmd.py
Subcommand handler: manga-recap render
"""

from __future__ import annotations

import argparse
import contextlib
import shutil
from pathlib import Path
from typing import Any, Dict

from src.cli.exit_codes import CliValidationError
from src.core.logger import logger
from src.core.schemas import CheckpointStatus, HumanReviewStatus, StageType, utc_iso_now
from src.core.storage import (
    StorageFileNotFoundError,
    acquire_lock,
    atomic_write_json,
    cleanup_intermediate_files,
    get_chapter_dir,
    get_output_dir,
    get_project_dir,
    load_chapter_metadata,
    read_json,
    safe_cleanup_chapter_intermediates,
    stage_chapter_export,
    update_checkpoint,
)
from src.core.utils import (
    DependencyNotFoundError,
    get_project_data_dir,
    resolve_video_output_path,
)
from src.render.video_engine import VideoEngine


def handle_render(args: argparse.Namespace) -> Dict[str, Any]:
    """Renders panel video clips, mixes BGM, burns subtitles, and outputs MP4."""
    # Kaggle Cloud Offloading Mode
    if getattr(args, "kaggle", False):
        project_slug = getattr(args, "project", "")
        chapter_num = getattr(args, "chapter", None)
        if not project_slug:
            raise CliValidationError("Project slug (--project) is required.")
        if chapter_num is None:
            raise CliValidationError("Chapter number (--chapter) is required.")

        logger.info(f"Offloading Render for {project_slug} ch.{chapter_num} to Kaggle Cloud GPU (Hardware NVENC)...")
        from src.cloud.kaggle_service import KaggleCloudService
        service = KaggleCloudService()
        if not service.is_authenticated:
            raise CliValidationError("Kaggle API authentication failed. Ensure KAGGLE_API_TOKEN is configured in ~/.kaggle/access_token.")
        cloud_res = service.dispatch_chapter_job(
            project_slug=project_slug,
            chapter_number=str(chapter_num),
            gpu=True,
            wait=True,
        )
        return {
            "status": "success",
            "command": "render",
            "project": project_slug,
            "chapter": str(chapter_num),
            "provider": "kaggle",
            "mode": "cloud_nvenc",
            "video_path": cloud_res.get("final_video"),
            "subtitle_path": cloud_res.get("subtitles"),
            "telemetry": cloud_res.get("telemetry", {}),
        }

    # Check binary dependencies first
    if not shutil.which("ffmpeg"):
        raise DependencyNotFoundError("FFmpeg executable not found on system PATH. Please install FFmpeg.")
    if not shutil.which("ffprobe"):
        raise DependencyNotFoundError("FFprobe executable not found on system PATH. Please install FFprobe.")

    project_slug = getattr(args, "project", "")
    chapter_num = getattr(args, "chapter", None)
    quality = getattr(args, "quality", "final")
    suffix = getattr(args, "suffix", "final")
    cleanup = getattr(args, "cleanup", False)
    keep_temp_arg = getattr(args, "keep_temp", None)
    keep_temp = (not cleanup) if keep_temp_arg is None else bool(keep_temp_arg)
    bgm_path = getattr(args, "bgm", None)
    burn_subs = getattr(args, "burn_subtitles", None)
    allow_animation = getattr(args, "animation", None)
    force = getattr(args, "force", False)
    skip_preview = getattr(args, "skip_preview", False) or getattr(args, "skip_review", False)
    approved = getattr(args, "approved", False)
    video_mode = getattr(args, "video_mode", None)
    allow_placeholders = getattr(args, "allow_placeholders", False)
    workers = getattr(args, "workers", None)
    hw_accel = getattr(args, "hw_accel", None)
    encoder = getattr(args, "encoder", None)
    subtitle_lines = getattr(args, "subtitle_lines", None)
    subtitle_font_size = getattr(args, "subtitle_font_size", None)
    subtitle_chunking = getattr(args, "subtitle_chunking", None)

    if not project_slug:
        raise CliValidationError("Project slug (--project) is required.")
    if chapter_num is None:
        raise CliValidationError("Chapter number (--chapter) is required.")

    project_dir = get_project_dir(project_slug)
    fallback_dir = get_project_data_dir(project_slug)
    eff_dir = project_dir if project_dir.exists() else (fallback_dir if fallback_dir.exists() else project_dir)

    # Strict Form Gate
    from src.core.series_setup import require_series_setup
    require_series_setup(project_slug, project_dir=eff_dir)

    if not eff_dir.exists():
        raise StorageFileNotFoundError(
            f"Project '{project_slug}' directory not found at {eff_dir}"
        )
    project_dir = eff_dir

    # Gate 0: Inherit video and render settings from series-level project.json
    proj_cfg_path = project_dir / "project.json"
    if proj_cfg_path.exists():
        with contextlib.suppress(Exception):
            p_data = read_json(proj_cfg_path)
            if isinstance(p_data, dict):
                r_set = p_data.get("render_settings", {})
                a_set = p_data.get("audio_settings", {})
                if bgm_path is None:
                    bgm_path = r_set.get("bgm_path") or a_set.get("bgm_path")
                if video_mode is None or video_mode == "auto":
                    video_mode = r_set.get("video_mode") or video_mode or "auto"
                if burn_subs is None and "burn_subtitles" in r_set:
                    burn_subs = r_set["burn_subtitles"]
                if allow_animation is None and "animation" in r_set:
                    allow_animation = r_set["animation"]
                if workers is None and "workers" in r_set:
                    workers = r_set["workers"]
                if hw_accel is None and "hw_accel" in r_set:
                    hw_accel = r_set["hw_accel"]
                if encoder is None and "encoder" in r_set:
                    encoder = r_set["encoder"]
                sub_set = r_set.get("subtitle", {})
                if subtitle_lines is None and "max_lines" in sub_set:
                    subtitle_lines = sub_set["max_lines"]
                if subtitle_font_size is None and "font_size" in sub_set:
                    subtitle_font_size = sub_set["font_size"]
                if subtitle_chunking is None and "chunking" in sub_set:
                    subtitle_chunking = sub_set["chunking"]

    # Fallback to defaults if neither CLI nor project.json specified them
    if video_mode is None:
        video_mode = "auto"
    if burn_subs is None:
        burn_subs = True
    if allow_animation is None:
        allow_animation = True
    if subtitle_lines is None:
        subtitle_lines = 1
    if subtitle_font_size is None:
        subtitle_font_size = 26
    if subtitle_chunking is None:
        subtitle_chunking = "sentence-aware"

    # Resolve encoder with runtime probing & smoke test
    from src.production.hardware import resolve_video_encoder
    requested_enc = encoder
    if not requested_enc and hw_accel:
        if hw_accel == "videotoolbox":
            requested_enc = "h264_videotoolbox"
        elif hw_accel == "nvenc":
            requested_enc = "h264_nvenc"
        elif hw_accel == "none":
            requested_enc = "libx264"
        elif hw_accel == "auto":
            requested_enc = "auto"
    if not requested_enc:
        requested_enc = "auto"

    resolved_encoder, codec_args, enc_msg = resolve_video_encoder(requested_enc)
    logger.info(f"Video Encoder: {enc_msg} ({resolved_encoder})")
    encoder = resolved_encoder
    if encoder == "h264_nvenc":
        hw_accel = "nvenc"
    elif encoder == "h264_videotoolbox":
        hw_accel = "videotoolbox"
    else:
        hw_accel = "none"

    chap_dir = get_chapter_dir(project_slug, chapter_num)
    if not chap_dir.exists():
        fallback_chap = project_dir / "chapters" / str(chapter_num)
        if fallback_chap.exists():
            chap_dir = fallback_chap
        else:
            raise StorageFileNotFoundError(
                f"Chapter '{chapter_num}' directory not found at {chap_dir}"
            )

    script_path = chap_dir / "script.json"
    if not script_path.exists():
        raise StorageFileNotFoundError(
            f"script.json not found in chapter directory: {chap_dir}"
        )

    # Enforce Human Review Gates: before final 1080p render, ensure Gate 1 (Voice) and Gate 2 (Preview) are satisfied
    if quality == "final":
        if not approved:
            # Gate 1: Voice Selection Approval
            voice_sel_path = project_dir / "voice_selection.json"
            if not voice_sel_path.exists():
                chap_voice_sel = chap_dir / "voice_selection.json"
                if chap_voice_sel.exists():
                    voice_sel_path = chap_voice_sel

            if not voice_sel_path.exists():
                raise CliValidationError(
                    "Human Review Gate 1 not satisfied: 'voice_selection.json' not found. "
                    "Voice strategy must be approved before final render (or pass --approved)."
                )

            import json
            from src.core.schemas import PreviewApproval, VoiceSelectionApproval
            from src.tts.catalog import is_valid_voice_id

            try:
                raw_sel = json.loads(voice_sel_path.read_text(encoding="utf-8"))
                approval = VoiceSelectionApproval.model_validate(raw_sel)
            except Exception as err:
                raise CliValidationError(
                    f"Human Review Gate 1 schema validation failed for '{voice_sel_path}': {err}."
                ) from err

            if not approval.is_approved():
                raise CliValidationError(
                    f"Human Review Gate 1 rejected: '{voice_sel_path}' is not approved. "
                    "A valid reviewer identifier ('reviewer' or 'approved_by') and primary voice are required."
                )

            if approval.project_slug and approval.project_slug.strip() not in (project_slug, "default_project", "manga_recap_project"):
                raise CliValidationError(
                    f"Human Review Gate 1 project mismatch: 'voice_selection.json' is for project '{approval.project_slug}', "
                    f"but current render target is '{project_slug}'."
                )

            if not is_valid_voice_id(approval.primary_voice):
                raise CliValidationError(
                    f"Human Review Gate 1 invalid: approved voice '{approval.primary_voice}' is not recognized in official catalog."
                )

            # Gate 2: Preview Video & Approval (optional via --skip-preview)
            if not skip_preview:
                video_dir = chap_dir / "video"
                has_preview_video = (
                    (video_dir / "preview.mp4").exists()
                    or (video_dir / f"chapter_{chapter_num}_preview.mp4").exists()
                    or (any(video_dir.glob("*preview*.mp4")) if video_dir.exists() else False)
                )
                if not has_preview_video:
                    raise CliValidationError(
                        "Human Review Gate 2 not satisfied: preview video ('preview.mp4') not found. "
                        "A preview render must be generated before final render (or pass --skip-preview or --approved)."
                    )

                prev_sel_path = project_dir / "preview_approval.json"
                if not prev_sel_path.exists():
                    chap_prev_sel = chap_dir / "preview_approval.json"
                    if chap_prev_sel.exists():
                        prev_sel_path = chap_prev_sel

                if not prev_sel_path.exists():
                    raise CliValidationError(
                        "Human Review Gate 2 not satisfied: 'preview_approval.json' not found. "
                        "Preview video must be approved before final render (or pass --skip-preview or --approved)."
                    )

                try:
                    raw_prev = json.loads(prev_sel_path.read_text(encoding="utf-8"))
                    prev_approval = PreviewApproval.model_validate(raw_prev)
                except Exception as err:
                    raise CliValidationError(
                        f"Human Review Gate 2 schema validation failed for '{prev_sel_path}': {err}."
                    ) from err

                if not prev_approval.is_approved():
                    raise CliValidationError(
                        f"Human Review Gate 2 rejected: '{prev_sel_path}' is not approved. "
                        "A valid reviewer identifier is required."
                    )

                if prev_approval.project_slug and prev_approval.project_slug.strip() not in (project_slug, "default_project", "manga_recap_project"):
                    raise CliValidationError(
                        f"Human Review Gate 2 project mismatch: 'preview_approval.json' is for project '{prev_approval.project_slug}', "
                        f"but current render target is '{project_slug}'."
                    )

                # Fix S1.5: Verify approval freshness against content fingerprints
                import hashlib
                curr_s_hash = hashlib.sha256(script_path.read_bytes()).hexdigest()
                curr_pv_path = chap_dir / "video" / "preview.mp4"
                curr_v_hash = hashlib.sha256(curr_pv_path.read_bytes()).hexdigest() if curr_pv_path.exists() and curr_pv_path.stat().st_size > 0 else None

                if prev_approval.script_sha256 and prev_approval.script_sha256 != curr_s_hash:
                    raise CliValidationError(
                        "Human Review Gate 2 approval is stale: script.json content hash has changed since editorial sign-off."
                    )
                if prev_approval.video_sha256 and curr_v_hash and prev_approval.video_sha256 != curr_v_hash:
                    raise CliValidationError(
                        "Human Review Gate 2 approval is stale: preview video content hash has changed since editorial sign-off."
                    )

    # Prioritize existing valid audio clips; only invoke TTS if audio is missing
    from src.core.schemas import RecapScript
    script_raw = read_json(script_path)
    if isinstance(script_raw, dict):
        if not script_raw.get("project_slug"):
            script_raw["project_slug"] = project_slug
        if not script_raw.get("chapter_number"):
            script_raw["chapter_number"] = str(chapter_num)
    script_obj = RecapScript.model_validate(script_raw)

    audio_missing = False
    for seg in script_obj.segments:
        wav_p = None
        if seg.audio_path:
            cand = Path(seg.audio_path)
            if not cand.is_absolute():
                cand = chap_dir / cand
            if cand.exists() and cand.stat().st_size > 44:
                wav_p = cand
        if not wav_p:
            cand = chap_dir / "audio" / "clips" / f"audio_{chapter_num}_{seg.seq_id}.wav"
            if cand.exists() and cand.stat().st_size > 44:
                wav_p = cand
        if not wav_p:
            audio_missing = True
            break

    if audio_missing:
        logger.info(f"Audio clips missing for chapter {chapter_num}; invoking TTS synthesis...")
        from src.cli.tts_cmd import handle_tts
        tts_args = argparse.Namespace(
            project=project_slug,
            chapter=chapter_num,
            voice=getattr(args, "voice", None),
            speed=getattr(args, "speed", None),
            force=force,
            engine=getattr(args, "tts_engine", "neural"),
            mock=getattr(args, "mock", False),
            workers=workers,
            kaggle=getattr(args, "kaggle", False),
        )
        handle_tts(tts_args)
    else:
        logger.info(f"Existing valid audio clips found for chapter {chapter_num}; reusing audio without invoking TTS.")

    # Execute full Video Engine V2 render pipeline
    try:
        engine = VideoEngine(max_workers=workers or 2, hw_accel=hw_accel)
    except TypeError:
        engine = VideoEngine()
    render_result = engine.render_chapter(
        project_dir=project_dir,
        chapter_num=chapter_num,
        quality=quality,
        bgm_path=bgm_path,
        burn_subtitles=burn_subs,
        keep_temp=keep_temp,
        force=force,
        suffix=suffix,
        video_mode=video_mode,
        allow_animation=allow_animation,
        allow_placeholders=allow_placeholders,
        max_workers=workers or 2,
        hw_accel=hw_accel,
        encoder=encoder,
        subtitle_lines=subtitle_lines,
        subtitle_font_size=subtitle_font_size,
        subtitle_chunking=subtitle_chunking,
    )

    # Completing RENDER completes chapter checkpoint & stages export
    staged_export: Dict[str, Optional[Path]] = {}
    if quality == "final":
        with contextlib.suppress(Exception):
            with acquire_lock(chap_dir / "chapter.json"):
                chap_meta = load_chapter_metadata(chap_dir)
                chap_meta.overall_status = CheckpointStatus.COMPLETED
                chap_meta.technical_status = CheckpointStatus.COMPLETED

                # Fix High 4: Verify actual human review approval artifact with valid reviewer signature.
                # Bypassing gates via --approved or --skip-preview allows technical completion,
                # but human_review_status is ONLY marked APPROVED if valid editorial approvals exist.
                has_editorial_approval = False
                try:
                    from src.core.schemas import PreviewApproval, VoiceSelectionApproval
                    v_p = project_dir / "voice_selection.json"
                    if not v_p.exists():
                        v_p = chap_dir / "voice_selection.json"
                    p_p = project_dir / "preview_approval.json"
                    if not p_p.exists():
                        p_p = chap_dir / "preview_approval.json"

                    if v_p.exists() and p_p.exists():
                        v_app = VoiceSelectionApproval.model_validate(json.loads(v_p.read_text(encoding="utf-8")))
                        p_app = PreviewApproval.model_validate(json.loads(p_p.read_text(encoding="utf-8")))
                        if v_app.is_approved() and p_app.is_approved():
                            # Fix S1.5: Invalidate approval if content fingerprint has changed
                            import hashlib
                            curr_s_hash = hashlib.sha256(script_path.read_bytes()).hexdigest()
                            curr_pv_path = chap_dir / "video" / "preview.mp4"
                            curr_v_hash = hashlib.sha256(curr_pv_path.read_bytes()).hexdigest() if curr_pv_path.exists() and curr_pv_path.stat().st_size > 0 else None
                            if p_app.matches_content(curr_s_hash, curr_v_hash):
                                has_editorial_approval = True
                except Exception:
                    has_editorial_approval = False

                if has_editorial_approval:
                    chap_meta.human_review_status = HumanReviewStatus.APPROVED
                else:
                    chap_meta.human_review_status = HumanReviewStatus.UNREVIEWED

                chap_meta.updated_at = utc_iso_now()
                atomic_write_json(chap_dir / "chapter.json", chap_meta)

        # Stage final verified MP4 and SRT into standardized output/<project_slug>/
        staged_export = stage_chapter_export(
            project_slug=project_slug,
            chapter_num=chapter_num,
            video_path=render_result.get("video_path"),
            subtitle_path=render_result.get("subtitle_path"),
        )
        with contextlib.suppress(Exception):
            export_art = str(staged_export.get("video_padded") or staged_export.get("video") or "")
            if export_art:
                update_checkpoint(
                    chap_dir,
                    StageType.EXPORT,
                    CheckpointStatus.COMPLETED,
                    artifacts=[export_art],
                )

    safe_cleaned_files = []
    if cleanup:
        if render_result.get("video_path") and Path(render_result["video_path"]).exists():
            safe_cleaned_files = safe_cleanup_chapter_intermediates(
                chapter_dir=chap_dir,
                output_video_path=Path(render_result["video_path"]),
            )
    cleaned_files = cleanup_intermediate_files(chap_dir, keep_temp=keep_temp)

    export_vid = staged_export.get("video_padded") or staged_export.get("video") if staged_export else None
    export_srt = staged_export.get("subtitle_padded") or staged_export.get("subtitle") if staged_export else None

    return {
        "project_slug": project_slug,
        "chapter_number": str(chapter_num),
        "video_path": render_result["video_path"],
        "subtitle_path": render_result.get("subtitle_path"),
        "export_video_path": str(export_vid) if export_vid else None,
        "export_subtitle_path": str(export_srt) if export_srt else None,
        "quality": quality,
        "suffix": suffix,
        "resolution": render_result.get("resolution", "1280x720"),
        "fps": render_result.get("fps", 30.0),
        "video_duration_seconds": render_result.get("video_duration_seconds", 0.0),
        "audio_duration_seconds": render_result.get("audio_duration_seconds", 0.0),
        "duration_diff_seconds": render_result.get("duration_diff_seconds", 0.0),
        "validation": render_result.get("validation", {}),
        "encoder": render_result.get("encoder", encoder),
        "cached_clips_count": render_result.get("cached_clips_count", 0),
        "rendered_clips_count": render_result.get("rendered_clips_count", 0),
        "cleaned_files_count": len(cleaned_files) + len(safe_cleaned_files) + render_result.get("cleaned_files_count", 0),
    }
