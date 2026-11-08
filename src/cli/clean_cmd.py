"""
src/cli/clean_cmd.py
Subcommand handler: manga-recap clean
Safely eliminates intermediate TTS utterance audio clips, intermediate video clips,
raw video concats, and obsolete renders after final output is verified.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Dict, List

from src.cli.exit_codes import CliValidationError
from src.core.storage import (
    format_chapter_filename,
    get_output_dir,
    get_project_dir,
)


def handle_clean(args: argparse.Namespace) -> Dict[str, Any]:
    """Safely cleans intermediate caches and temporary files for a project/chapter."""
    project_slug = getattr(args, "project", "")
    if not project_slug:
        raise CliValidationError("Project slug is required.")

    dry_run = getattr(args, "dry_run", False)
    chapter_arg = getattr(args, "chapter", None)

    project_dir = get_project_dir(project_slug)
    if not project_dir.exists():
        raise CliValidationError(f"Project '{project_slug}' not found at {project_dir}")

    out_dir = get_output_dir(project_slug)
    chapters_dir = project_dir / "chapters"

    target_chapters: List[str] = []
    if chapter_arg is not None:
        target_chapters = [str(chapter_arg).strip()]
    elif chapters_dir.exists():
        for ch in chapters_dir.iterdir():
            if ch.is_dir() and not ch.name.startswith("."):
                target_chapters.append(ch.name)
        try:
            target_chapters.sort(key=lambda x: int(x))
        except ValueError:
            target_chapters.sort()

    cleaned_files: List[str] = []
    total_reclaimed_bytes = 0
    chapters_cleaned: List[str] = []
    skipped_chapters: List[Dict[str, str]] = []

    for ch_num in target_chapters:
        chap_dir = chapters_dir / ch_num
        if not chap_dir.exists():
            continue

        # Look for verified final video
        out_vid = out_dir / format_chapter_filename(ch_num)
        final_vid = chap_dir / "video" / "final.mp4"

        verified_video: Path | None = None
        if out_vid.exists() and out_vid.stat().st_size >= 100000:
            verified_video = out_vid
        elif final_vid.exists() and final_vid.stat().st_size >= 100000:
            verified_video = final_vid

        if not verified_video:
            skipped_chapters.append({
                "chapter": ch_num,
                "reason": "No verified output video found (final.mp4 or output/<slug>/chapter_xxx.mp4 >= 100KB)",
            })
            continue

        ch_files_to_delete: List[Path] = []

        # 1. Intermediate audio clips
        a_clips_dir = chap_dir / "audio" / "clips"
        if a_clips_dir.exists():
            for p in a_clips_dir.glob("*.wav"):
                ch_files_to_delete.append(p)

        # 2. Intermediate speech master wav
        a_dir = chap_dir / "audio"
        if a_dir.exists():
            for p in a_dir.glob("speech_master_*.wav"):
                ch_files_to_delete.append(p)

        # 3. Intermediate video clips
        v_clips_dir = chap_dir / "video" / "clips"
        if v_clips_dir.exists():
            for p in v_clips_dir.glob("*"):
                if p.is_file():
                    ch_files_to_delete.append(p)

        # 4. Raw video concats
        v_dir = chap_dir / "video"
        if v_dir.exists():
            for p in v_dir.glob("raw_video_*.mp4"):
                ch_files_to_delete.append(p)

            # 5. Obsolete version renders (keep verified final.mp4)
            for p in v_dir.glob("*_v[0-9]*.mp4"):
                if p.resolve() != verified_video.resolve():
                    ch_files_to_delete.append(p)

        # 6. Temp clips and concat files
        for pattern in ("*_raw_temp*.mp4", "*_subs_temp*.srt", "*concat_list*.txt", "*.tmp"):
            for p in chap_dir.rglob(pattern):
                if p.is_file() and "raw" not in p.parts and "panels" not in p.parts:
                    ch_files_to_delete.append(p)

        for f in set(ch_files_to_delete):
            if f.exists():
                size = f.stat().st_size
                total_reclaimed_bytes += size
                cleaned_files.append(str(f.name))
                if not dry_run:
                    f.unlink(missing_ok=True)

        if not dry_run:
            # Clean empty directories
            if a_clips_dir.exists() and not any(a_clips_dir.iterdir()):
                a_clips_dir.rmdir()
            if v_clips_dir.exists() and not any(v_clips_dir.iterdir()):
                v_clips_dir.rmdir()

        chapters_cleaned.append(ch_num)

    # Project-level redundant files (audio/final.wav, output/*_full_recap.wav, duplicate feature mp4)
    project_audio_final = project_dir / "audio" / "final.wav"
    if project_audio_final.exists():
        size = project_audio_final.stat().st_size
        total_reclaimed_bytes += size
        cleaned_files.append(project_audio_final.name)
        if not dry_run:
            project_audio_final.unlink(missing_ok=True)

    out_full_wav = out_dir / f"{project_slug}_full_recap.wav"
    if out_full_wav.exists():
        size = out_full_wav.stat().st_size
        total_reclaimed_bytes += size
        cleaned_files.append(out_full_wav.name)
        if not dry_run:
            out_full_wav.unlink(missing_ok=True)

    # Duplicate feature mp4 / full_recap.mp4
    out_full_mp4 = out_dir / f"{project_slug}_full_recap.mp4"
    feature_candidates = list(out_dir.glob("*_feature.mp4"))
    if out_full_mp4.exists() and feature_candidates:
        feature_mp4 = feature_candidates[0]
        if out_full_mp4.stat().st_size == feature_mp4.stat().st_size:
            size = out_full_mp4.stat().st_size
            total_reclaimed_bytes += size
            cleaned_files.append(out_full_mp4.name)
            if not dry_run:
                out_full_mp4.unlink(missing_ok=True)

    out_full_srt = out_dir / f"{project_slug}_full_recap.srt"
    feature_srt_candidates = list(out_dir.glob("*_feature.srt"))
    if out_full_srt.exists() and feature_srt_candidates:
        if out_full_srt.stat().st_size == feature_srt_candidates[0].stat().st_size:
            size = out_full_srt.stat().st_size
            total_reclaimed_bytes += size
            cleaned_files.append(out_full_srt.name)
            if not dry_run:
                out_full_srt.unlink(missing_ok=True)

    reclaimed_mb = round(total_reclaimed_bytes / (1024 * 1024), 2)
    reclaimed_gb = round(total_reclaimed_bytes / (1024 * 1024 * 1024), 3)

    return {
        "project_slug": project_slug,
        "dry_run": dry_run,
        "chapters_cleaned": chapters_cleaned,
        "skipped_chapters": skipped_chapters,
        "cleaned_files_count": len(cleaned_files),
        "reclaimed_bytes": total_reclaimed_bytes,
        "reclaimed_mb": reclaimed_mb,
        "reclaimed_gb": reclaimed_gb,
    }
