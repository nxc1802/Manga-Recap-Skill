"""
src/cli/tts_cmd.py
Subcommand handler: manga-recap tts
Synthesizes speech audio for chapter script segments using local VieNeu-TTS engine.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Dict

from src.cli.exit_codes import CliValidationError
from src.core.schemas import CheckpointStatus, RecapScript, StageType
from src.core.storage import (
    StorageFileNotFoundError,
    atomic_write_json,
    get_chapter_dir,
    get_project_dir,
    read_json,
    update_checkpoint,
)
from src.core.utils import get_project_data_dir
from src.tts.engine import get_tts_engine


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

    script_raw = read_json(script_path)
    script_obj = RecapScript.model_validate(script_raw)

    audio_clips_dir = chap_dir / "audio" / "clips"
    audio_clips_dir.mkdir(parents=True, exist_ok=True)

    default_voice = voice_override or script_obj.default_voice or "Trúc Ly"
    engine = get_tts_engine()

    synthesized_count = 0
    cached_count = 0
    artifacts = []
    total_duration = 0.0

    for seg in script_obj.segments:
        voice = voice_override or seg.voice_id or default_voice
        speed = speed_override or 1.0
        seg_audio_file = audio_clips_dir / f"audio_{chapter_num}_{seg.seq_id}.wav"

        dur, checksum, was_cached = engine.synthesize_utterance(
            text=seg.narrator_text,
            voice=voice,
            output_path=seg_audio_file,
            speed=speed,
            emotion=seg.emotion,
            emotion_cue=seg.emotion_cue,
            use_cache=True,
            force=force_resynth,
        )

        seg.audio_path = str(seg_audio_file.relative_to(chap_dir))
        seg.duration = dur
        seg.checksum = checksum
        seg.status = "synthesized"
        total_duration += dur

        if was_cached:
            cached_count += 1
        else:
            synthesized_count += 1
        artifacts.append(seg.audio_path)

    script_obj.total_duration_seconds = round(total_duration, 3)

    # Atomic write script.json
    atomic_write_json(script_path, script_obj)

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
        "engine_mode": engine.mode,
    }
