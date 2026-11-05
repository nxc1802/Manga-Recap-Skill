"""
src/cli/compile_cmd.py
Subcommand handler: manga-recap compile
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List

from src.cli.exit_codes import CliValidationError
from src.core.schemas import CheckpointStatus, PreviewApproval, VoiceSelectionApproval
from src.core.storage import StorageFileNotFoundError, get_project_dir, read_json
from src.core.utils import DependencyNotFoundError, get_project_data_dir, run_ffmpeg


def handle_compile(args: argparse.Namespace) -> Dict[str, Any]:
    """Concatenates multiple chapter videos into a continuous full recap video."""
    if not shutil.which("ffmpeg"):
        raise DependencyNotFoundError("FFmpeg executable not found on system PATH. Please install FFmpeg.")

    project_slug = getattr(args, "project", "")
    if not project_slug:
        raise CliValidationError("Project slug (--project) is required.")

    project_dir = get_project_dir(project_slug)
    if not project_dir.exists():
        fallback_dir = get_project_data_dir(project_slug)
        if fallback_dir.exists():
            project_dir = fallback_dir
        else:
            raise StorageFileNotFoundError(
                f"Project '{project_slug}' directory not found at {project_dir}"
            )

    chapters_arg = getattr(args, "chapters", None)
    output_arg = getattr(args, "output", None)
    extract_audio = getattr(args, "extract_audio", True)
    approved = getattr(args, "approved", False)

    requested_chaps: List[str] = []
    if chapters_arg:
        requested_chaps = [c.strip() for c in str(chapters_arg).split(",") if c.strip()]

    # Collect video files per chapter
    chapter_videos: Dict[int, Path] = {}

    # Check canonical paths: data/projects/<slug>/chapters/<c>/video/
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
                finals = sorted([f for f in candidate_mp4s if "final" in f.name.lower()])
                non_previews = sorted([f for f in candidate_mp4s if "preview" not in f.name.lower()])
                if finals:
                    chapter_videos[c_num] = finals[-1]
                elif non_previews:
                    chapter_videos[c_num] = non_previews[-1]
                elif candidate_mp4s:
                    raise StorageFileNotFoundError(
                        f"Chapter {c_num} only has preview video ({candidate_mp4s[0].name}). "
                        "Final rendered video is required for compilation; preview video cannot be used for final export."
                    )

    # Also check legacy/flat paths: project_dir / video / chapters /
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
        raise StorageFileNotFoundError(
            f"No rendered chapter videos found to compile in {project_dir}"
        )

    if requested_chaps:
        missing_chaps = [c for c in requested_chaps if int(c) not in chapter_videos]
        if missing_chaps:
            raise StorageFileNotFoundError(
                f"Missing final rendered video for requested chapter(s): {', '.join(missing_chaps)} in {project_dir}"
            )

    for c_num, vid_path in chapter_videos.items():
        if "preview" in vid_path.name.lower() and "final" not in vid_path.name.lower():
            raise StorageFileNotFoundError(
                f"Chapter {c_num} video '{vid_path.name}' is a preview video. "
                "Final rendered video is required for compilation; preview video cannot be used for final export."
            )

    # Verify Review Gates unless --approved is explicitly provided
    if not approved:
        # Check Series-level Gate 1 (Voice selection) once for the project
        proj_v_sel = project_dir / "voice_selection.json"
        proj_voice_approved = False
        if proj_v_sel.exists():
            try:
                v_data = read_json(proj_v_sel)
                v_approval = VoiceSelectionApproval.model_validate(v_data)
                if v_approval.is_approved():
                    proj_voice_approved = True
            except Exception as e:
                raise CliValidationError(
                    f"Project Review Gate 1 schema validation failed for '{proj_v_sel}': {e}. Supply --approved to bypass."
                ) from e

        # Check Series-level Gate 2 (Preview approval) once for the project
        proj_p_sel = project_dir / "preview_approval.json"
        proj_preview_approved = False
        if proj_p_sel.exists():
            try:
                p_data = read_json(proj_p_sel)
                p_approval = PreviewApproval.model_validate(p_data)
                if p_approval.is_approved():
                    proj_preview_approved = True
            except Exception as e:
                raise CliValidationError(
                    f"Project Review Gate 2 schema validation failed for '{proj_p_sel}': {e}. Supply --approved to bypass."
                ) from e

        for c_num in sorted(chapter_videos.keys()):
            c_dir = chapters_dir / str(c_num)
            chap_json = c_dir / "chapter.json"
            if chap_json.exists():
                try:
                    c_meta = read_json(chap_json)
                except Exception as e:
                    raise CliValidationError(
                        f"Failed to read metadata for Chapter {c_num} from '{chap_json}': {e}"
                    ) from e

                if not isinstance(c_meta, dict):
                    raise CliValidationError(
                        f"Invalid metadata format in '{chap_json}': expected JSON object."
                    )

                if c_meta.get("overall_status") == CheckpointStatus.NEEDS_REVIEW.value:
                    raise CliValidationError(
                        f"Chapter {c_num} requires human review before compilation. Supply --approved to bypass."
                    )

            # Check Gate 1: Voice selection (if not already verified at project level)
            if not proj_voice_approved:
                v_sel = project_dir / "voice_selection.json"
                if not v_sel.exists():
                    v_sel = c_dir / "voice_selection.json"
                if not v_sel.exists():
                    raise CliValidationError(
                        f"Human Review Gate 1 not satisfied: 'voice_selection.json' not found for Chapter {c_num}. "
                        "Voice strategy must be approved before compilation (or pass --approved)."
                    )

                try:
                    v_data = read_json(v_sel)
                    v_approval = VoiceSelectionApproval.model_validate(v_data)
                except Exception as e:
                    raise CliValidationError(
                        f"Chapter {c_num} Review Gate 1 schema validation failed for '{v_sel}': {e}. Supply --approved to bypass."
                    ) from e

                if not v_approval.is_approved():
                    raise CliValidationError(
                        f"Chapter {c_num} Review Gate 1 rejected: '{v_sel}' is not approved. Supply --approved to bypass."
                    )

            # Check Gate 2: Preview Approval (if not already verified at project level)
            if not proj_preview_approved:
                p_sel = project_dir / "preview_approval.json"
                if not p_sel.exists():
                    p_sel = c_dir / "preview_approval.json"
                if not p_sel.exists():
                    raise CliValidationError(
                        f"Human Review Gate 2 not satisfied: 'preview_approval.json' not found for Chapter {c_num}. "
                        "Preview video must be approved before compilation (or pass --approved)."
                    )

                try:
                    p_data = read_json(p_sel)
                    p_approval = PreviewApproval.model_validate(p_data)
                except Exception as e:
                    raise CliValidationError(
                        f"Chapter {c_num} Review Gate 2 schema validation failed for '{p_sel}': {e}. Supply --approved to bypass."
                    ) from e

                if not p_approval.is_approved():
                    raise CliValidationError(
                        f"Chapter {c_num} Review Gate 2 rejected: '{p_sel}' is not approved. Supply --approved to bypass."
                    )

    sorted_chaps = sorted(chapter_videos.keys())
    compile_list = [chapter_videos[c] for c in sorted_chaps]

    # Determine destination
    if output_arg:
        output_path = Path(output_arg).resolve()
    else:
        output_path = project_dir / "video" / "final.mp4"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Concat demuxer list
    concat_list_path = project_dir / "compile_concat_list.txt"
    with open(concat_list_path, "w", encoding="utf-8") as f:
        for p in compile_list:
            f.write(f"file '{p.resolve()}'\n")

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat_list_path),
        "-c", "copy",
        str(output_path),
    ]

    try:
        run_ffmpeg(cmd, description="Compile chapter video concatenation")
        if not output_path.exists() or output_path.stat().st_size == 0:
            raise RuntimeError(f"Compiled video is missing or empty: {output_path}")
    finally:
        if concat_list_path.exists():
            concat_list_path.unlink(missing_ok=True)

    # Extract master WAV audio if requested
    final_audio_path = None
    if extract_audio:
        final_audio_path = project_dir / "audio" / "final.wav"
        final_audio_path.parent.mkdir(parents=True, exist_ok=True)
        cmd_audio = [
            "ffmpeg", "-y",
            "-i", str(output_path),
            "-vn", "-acodec", "pcm_s16le", "-ar", "48000", "-ac", "2",
            str(final_audio_path),
        ]
        run_ffmpeg(cmd_audio, description="Extract master audio from compiled video")
        if not final_audio_path.exists() or final_audio_path.stat().st_size == 0:
            raise RuntimeError(f"Compiled audio is missing or empty: {final_audio_path}")

    return {
        "project_slug": project_slug,
        "compiled_video": str(output_path),
        "compiled_audio": str(final_audio_path) if final_audio_path else None,
        "chapters_compiled": [str(c) for c in sorted_chaps],
    }
