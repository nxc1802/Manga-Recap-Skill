"""
src/cli/tts_cmd.py
Subcommand handler: manga-recap tts
Synthesizes speech audio for chapter script segments using local VieNeu-TTS engine.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from src.cli.exit_codes import CliValidationError
from src.core.schemas import CheckpointStatus, RecapScript, ScriptSegment, StageType
from src.core.storage import (
    StorageFileNotFoundError,
    atomic_write_json,
    calculate_sha256,
    calculate_utterance_fingerprint,
    get_chapter_dir,
    get_project_dir,
    read_json,
    reset_stage_checkpoint,
    update_checkpoint,
)
from src.core.utils import get_project_data_dir
from src.tts.engine import get_tts_engine, get_wav_duration_seconds


def resolve_segment_voice(
    seg: ScriptSegment,
    voice_override: Optional[str] = None,
    approved_voice: Optional[str] = None,
    approved_character_mapping: Optional[Dict[str, str]] = None,
    default_voice: Optional[str] = None,
) -> Tuple[str, str]:
    """
    Resolves the exact voice to use for a segment based on strict priority:
    1. CLI explicit voice override (--voice) -> source: 'override'
    2. Character voice mapping from approved Review Gate 1 -> source: 'approved'
    3. Manually assigned voice by Agent (voice_source == 'manual' or is_manual_voice()) -> source: 'manual'
    4. Primary approved voice from Review Gate 1 (voice_selection.json) -> source: 'approved'
    5. Fallback default voice (from script or system default) -> source: 'default'

    Returns (resolved_voice, voice_source).
    """
    if voice_override:
        return voice_override, "override"

    if seg.character and approved_character_mapping and seg.character in approved_character_mapping:
        return approved_character_mapping[seg.character], "approved"

    if seg.is_manual_voice() and seg.voice_id:
        return seg.voice_id, "manual"

    if approved_voice:
        return approved_voice, "approved"

    if seg.voice_id and seg.voice_source != "tts":
        return seg.voice_id, "manual"

    return default_voice or "Trúc Ly", "default"


def handle_tts(args: argparse.Namespace) -> Dict[str, Any]:
    """Synthesizes speech audio for chapter script segments using local VieNeu-TTS engine."""
    project_slug = getattr(args, "project", "")
    chapter_num = getattr(args, "chapter", None)
    voice_override = getattr(args, "voice", None)
    speed_override = getattr(args, "speed", None)
    force_resynth = getattr(args, "force", False)

    if not project_slug:
        raise CliValidationError("Project slug (--project) is required.")
    if chapter_num is None:
        raise CliValidationError("Chapter number (--chapter) is required.")

    project_dir = get_project_dir(project_slug)
    if not project_dir.exists():
        fallback_dir = get_project_data_dir(project_slug)
        if fallback_dir.exists():
            project_dir = fallback_dir
        else:
            raise StorageFileNotFoundError(
                f"Project '{project_slug}' directory not found at {project_dir}"
            )

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
            f"Script file script.json not found in chapter directory: {chap_dir}"
        )

    # Checkpoint -> RUNNING
    update_checkpoint(chap_dir, StageType.TTS, CheckpointStatus.RUNNING)

    try:
        script_raw = read_json(script_path)
        script_obj = RecapScript.model_validate(script_raw)

        audio_clips_dir = chap_dir / "audio" / "clips"
        audio_clips_dir.mkdir(parents=True, exist_ok=True)

        # Check if voice_selection.json exists to load approved strategy, voice, and speed
        voice_sel_file = project_dir / "voice_selection.json"
        if not voice_sel_file.exists():
            voice_sel_file = chap_dir / "voice_selection.json"

        approved_voice = None
        approved_speed = None
        approved_character_mapping: Dict[str, str] = {}
        if voice_sel_file.exists():
            try:
                import json
                from src.core.schemas import VoiceSelectionApproval
                sel_data = json.loads(voice_sel_file.read_text(encoding="utf-8"))
                appr = VoiceSelectionApproval.model_validate(sel_data)
                if appr.is_approved():
                    approved_voice = appr.primary_voice
                    approved_speed = appr.speed
                    approved_character_mapping = appr.character_voice_mapping or {}
            except Exception:
                pass

        default_voice = voice_override or approved_voice or script_obj.default_voice or "Trúc Ly"
        default_speed = speed_override or approved_speed or 1.0
        
        # Resolve requested engine mode
        requested_engine = getattr(args, "engine", "neural")
        if getattr(args, "mock", False):
            requested_engine = "mock"

        from src.tts.engine import EngineMode
        if requested_engine == "neural":
            engine = get_tts_engine(force_mode=EngineMode.NEURAL)
        elif requested_engine == "mock":
            engine = get_tts_engine(force_mode=EngineMode.MOCK)
        else:
            engine = get_tts_engine()

        synthesized_count = 0
        cached_count = 0
        artifacts = []
        total_duration = 0.0

        for seg in script_obj.segments:
            voice, v_source = resolve_segment_voice(
                seg=seg,
                voice_override=voice_override,
                approved_voice=approved_voice,
                approved_character_mapping=approved_character_mapping,
                default_voice=default_voice,
            )
            speed = speed_override or default_speed
            seg_audio_file = audio_clips_dir / f"audio_{chapter_num}_{seg.seq_id}.wav"
            engine_mode = getattr(engine, "mode", "neural")
            calculated_fingerprint = calculate_utterance_fingerprint(
                text=seg.narrator_text,
                voice=voice,
                speed=speed,
                emotion=seg.emotion,
                emotion_cue=seg.emotion_cue,
                engine_mode=engine_mode,
            )

            # Check if segment can be resumed / skipped when --force is omitted
            can_resume = False
            if not force_resynth and seg_audio_file.exists() and seg_audio_file.stat().st_size > 44:
                if (
                    seg.status == "synthesized"
                    and seg.checksum is not None
                    and getattr(seg, "fingerprint", None) is not None
                    and seg.fingerprint == calculated_fingerprint
                    and (seg.synthesized_voice == voice or seg.voice_id == voice)
                ):
                    try:
                        dur = get_wav_duration_seconds(seg_audio_file)
                        if dur > 0:
                            actual_checksum = calculate_sha256(seg_audio_file)
                            if seg.checksum == actual_checksum:
                                can_resume = True
                                seg.duration = dur
                                seg.synthesized_voice = voice
                                seg.audio_path = str(seg_audio_file.relative_to(chap_dir))
                    except Exception:
                        can_resume = False

            if can_resume:
                cached_count += 1
                total_duration += seg.duration
                artifacts.append(seg.audio_path)
                continue

            dur, checksum, was_cached = engine.synthesize_utterance(
                text=seg.narrator_text,
                voice=voice,
                output_path=seg_audio_file,
                speed=speed,
                emotion=seg.emotion,
                emotion_cue=seg.emotion_cue,
                force=force_resynth,
            )

            seg.synthesized_voice = voice
            if v_source == "manual":
                seg.voice_id = voice
                seg.voice_source = "manual"
            else:
                seg.voice_id = voice
                seg.voice_source = "tts"

            actual_checksum = calculate_sha256(seg_audio_file)
            seg.audio_path = str(seg_audio_file.relative_to(chap_dir))
            seg.duration = dur
            seg.checksum = actual_checksum
            seg.fingerprint = calculated_fingerprint
            seg.status = "synthesized"
            total_duration += dur

            synthesized_count += 1
            artifacts.append(seg.audio_path)

            # Checkpoint script after each newly synthesized segment for crash-safety
            atomic_write_json(script_path, script_obj)

        script_obj.total_duration_seconds = round(total_duration, 3)

        # Atomic write script.json
        atomic_write_json(script_path, script_obj)

        # Invalidate downstream video and subtitles if any audio was newly synthesized or force re-synthesized
        if synthesized_count > 0 or force_resynth:
            video_dir = chap_dir / "video"
            if video_dir.exists():
                for vf in video_dir.glob("*.mp4"):
                    try:
                        vf.unlink()
                    except Exception:
                        pass
            subs_dir = chap_dir / "subtitles"
            if subs_dir.exists():
                for sf in subs_dir.glob("*.srt"):
                    try:
                        sf.unlink()
                    except Exception:
                        pass
            reset_stage_checkpoint(chap_dir, StageType.RENDER)
            reset_stage_checkpoint(chap_dir, StageType.FINALIZE)

        # Checkpoint -> COMPLETED
        update_checkpoint(
            chap_dir,
            StageType.TTS,
            CheckpointStatus.COMPLETED,
            artifacts=artifacts,
        )

        return {
            "project_slug": project_slug,
            "chapter_number": str(chapter_num),
            "segments_synthesized": synthesized_count,
            "segments_cached": cached_count,
            "total_duration_seconds": round(total_duration, 3),
            "default_voice": default_voice,
            "artifacts": artifacts,
            "engine_mode": engine.mode,
        }
    except Exception as e:
        update_checkpoint(chap_dir, StageType.TTS, CheckpointStatus.FAILED, error=str(e))
        raise
