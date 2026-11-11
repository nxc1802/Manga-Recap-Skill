"""
src/cli/clean_cmd.py
Subcommand handler: manga-recap clean
Safely eliminates intermediate TTS utterance audio clips, intermediate video clips,
raw video concats, and obsolete renders after final output is verified.
Enforces the Two-Receipt Safe Cleanup Protocol and writes a persistent tombstone.json.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.cli.exit_codes import CliValidationError
from src.core.logger import logger
from src.core.schemas import utc_iso_now
from src.core.storage import (
    atomic_write_json,
    format_chapter_filename,
    get_output_dir,
    get_project_dir,
    read_json,
)


def _check_two_receipt_gate(
    project_slug: str,
    project_dir: Path,
    target_chapters: List[str],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Enforces the Two-Receipt Safe Cleanup Gate:
    1. Hugging Face archive_receipt.json must exist with verified status and commit SHA.
    2. YouTube youtube_receipt.json must exist for all target chapters (unless waived in Q08).

    Returns (archive_receipt_data, youtube_records_map).
    """
    archive_receipt_file = project_dir / "archive_receipt.json"
    if not archive_receipt_file.exists():
        raise CliValidationError(
            f"Two-Receipt Gate failed: Hugging Face archive receipt missing for '{project_slug}'. "
            f"Please run 'manga-recap archive push' and 'manga-recap archive verify' before deleting local assets."
        )

    try:
        archive_receipt = read_json(archive_receipt_file)
    except Exception as e:
        raise CliValidationError(f"Two-Receipt Gate failed: Corrupt archive_receipt.json: {e}") from e

    if archive_receipt.get("status") != "verified" or not archive_receipt.get("commit_sha"):
        raise CliValidationError(
            f"Two-Receipt Gate failed: Hugging Face archive receipt is unverified (status: {archive_receipt.get('status')}). "
            f"Run 'manga-recap archive verify' first."
        )

    youtube_waived = False
    setup_file = project_dir / "series_setup_answers.json"
    if setup_file.exists():
        try:
            s_data = read_json(setup_file)
            q08 = s_data.get("answers", {}).get("Q08", {})
            u_ans = q08.get("user_answer") if isinstance(q08, dict) else q08
            if isinstance(u_ans, str) and u_ans.strip().lower() in ("không upload", "khong upload", "none", "no"):
                youtube_waived = True
            elif isinstance(u_ans, bool) and not u_ans:
                youtube_waived = True
            elif isinstance(u_ans, dict) and u_ans.get("no_upload"):
                youtube_waived = True
        except Exception:
            pass

    youtube_records: Dict[str, Any] = {}
    if not youtube_waived:
        chapters_dir = project_dir / "chapters"
        missing_yt_chapters: List[str] = []
        for ch_num in target_chapters:
            chap_dir = chapters_dir / ch_num
            yt_receipt_file = chap_dir / "youtube_receipt.json"
            if not yt_receipt_file.exists():
                out_dir = get_output_dir(project_slug)
                staged_name = format_chapter_filename(ch_num, ext="youtube_receipt.json", padded=True)
                if (out_dir / staged_name).exists():
                    yt_receipt_file = out_dir / staged_name

            if not yt_receipt_file.exists():
                missing_yt_chapters.append(ch_num)
                continue

            try:
                yt_data = read_json(yt_receipt_file)
                if yt_data.get("status") not in ("verified", "uploaded") or not yt_data.get("video_id"):
                    missing_yt_chapters.append(ch_num)
                else:
                    youtube_records[ch_num] = {
                        "video_id": yt_data.get("video_id"),
                        "video_url": yt_data.get("video_url"),
                        "privacy_status": yt_data.get("privacy_status"),
                    }
            except Exception:
                missing_yt_chapters.append(ch_num)

        if missing_yt_chapters:
            raise CliValidationError(
                f"Two-Receipt Gate failed: Missing or unverified YouTube receipt for chapter(s): {', '.join(missing_yt_chapters)}. "
                f"Run 'manga-recap youtube verify' first, or configure Q08 as 'Không upload'."
            )

    return archive_receipt, youtube_records


def _build_tombstone(
    project_slug: str,
    archive_receipt: Optional[Dict[str, Any]],
    youtube_records: Dict[str, Any],
    total_reclaimed_bytes: int,
    cleaned_files_count: int,
) -> bytes:
    """
    Constructs and serializes tombstone.json payload with strict size enforcement (< 2048 bytes).
    Compactly maps {chapter_number: video_id} and applies progressive compression/truncation
    if total chapter count causes the JSON payload to approach or exceed 2 KB.
    """
    compact_yt: Dict[str, str] = {}
    for ch, val in youtube_records.items():
        if isinstance(val, dict):
            vid = val.get("video_id") or ""
        else:
            vid = str(val or "")
        if vid:
            compact_yt[str(ch)] = vid

    tombstone_data: Dict[str, Any] = {
        "tombstone_version": "4.0.0",
        "project_slug": project_slug,
        "archive_timestamp": archive_receipt.get("created_at") if archive_receipt else None,
        "cleanup_timestamp": utc_iso_now(),
        "hf_repo_id": archive_receipt.get("repo_id") if archive_receipt else None,
        "hf_commit_sha": archive_receipt.get("commit_sha") if archive_receipt else None,
        "hf_remote_url": archive_receipt.get("remote_url") if archive_receipt else None,
        "youtube_videos": compact_yt,
        "reclaimed_bytes": total_reclaimed_bytes,
        "cleaned_files_count": cleaned_files_count,
    }

    # Level 1: Formatted indentation (indent=2)
    formatted_bytes = json.dumps(tombstone_data, indent=2).encode("utf-8")
    if len(formatted_bytes) < 2048:
        return formatted_bytes

    # Level 2: Minification without whitespace
    minified_bytes = json.dumps(tombstone_data, separators=(",", ":")).encode("utf-8")
    if len(minified_bytes) < 2048:
        return minified_bytes

    # Level 3: Progressive truncation for very large chapter series (e.g. 100+ chapters)
    total_yt_count = len(compact_yt)
    tombstone_data["youtube_videos_total"] = total_yt_count
    tombstone_data["youtube_videos_truncated"] = True

    sorted_keys = sorted(compact_yt.keys(), key=lambda x: int(x) if x.isdigit() else x)
    truncated_yt = dict(compact_yt)

    for k in reversed(sorted_keys):
        if len(json.dumps(tombstone_data, separators=(",", ":")).encode("utf-8")) < 2000:
            break
        truncated_yt.pop(k, None)
        tombstone_data["youtube_videos"] = truncated_yt

    final_bytes = json.dumps(tombstone_data, separators=(",", ":")).encode("utf-8")
    if len(final_bytes) >= 2048:
        tombstone_data["youtube_videos"] = {}
        final_bytes = json.dumps(tombstone_data, separators=(",", ":")).encode("utf-8")

    return final_bytes


def handle_clean(args: argparse.Namespace) -> Dict[str, Any]:
    """Safely cleans intermediate caches and temporary files for a project/chapter."""
    project_slug = getattr(args, "project", "")
    if not project_slug:
        raise CliValidationError("Project slug is required.")

    dry_run = getattr(args, "dry_run", True)
    chapter_arg = getattr(args, "chapter", None)
    confirm_project = getattr(args, "confirm_project", None)
    bypass_receipts = getattr(args, "bypass_receipts", False)
    purge_raw = getattr(args, "purge_raw", False)

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

    archive_receipt: Optional[Dict[str, Any]] = None
    youtube_records: Dict[str, Any] = {}

    if not dry_run:
        # Safety Barrier: --confirm-project must match project_slug unconditionally
        if confirm_project != project_slug:
            raise CliValidationError(
                f"Safety barrier failed: --confirm-project '{project_slug}' is required for actual deletion "
                f"(got '{confirm_project}')."
            )

        # Two-Receipt Gate: strictly enforced unless explicit bypass_receipts override
        if not bypass_receipts:
            archive_receipt, youtube_records = _check_two_receipt_gate(
                project_slug=project_slug,
                project_dir=project_dir,
                target_chapters=target_chapters,
            )

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

        # 7. Optional purge of raw pages and panel crops after archive verification
        if purge_raw and (archive_receipt or (project_dir / "archive_receipt.json").exists()):
            for pattern in ("raw/*", "panels/*"):
                for p in chap_dir.glob(pattern):
                    if p.is_file():
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

    tombstone_created = False
    if not dry_run:
        # Collect any additional youtube receipts that were present in chapters
        for ch_num in target_chapters:
            if ch_num not in youtube_records:
                chap_dir = chapters_dir / ch_num
                rec_f = chap_dir / "youtube_receipt.json"
                if rec_f.exists():
                    try:
                        yd = read_json(rec_f)
                        if yd.get("video_id"):
                            youtube_records[ch_num] = {
                                "video_id": yd.get("video_id"),
                                "video_url": yd.get("video_url"),
                                "privacy_status": yd.get("privacy_status"),
                            }
                    except Exception:
                        pass

        tombstone_bytes = _build_tombstone(
            project_slug=project_slug,
            archive_receipt=archive_receipt,
            youtube_records=youtube_records,
            total_reclaimed_bytes=total_reclaimed_bytes,
            cleaned_files_count=len(cleaned_files),
        )

        tombstone_file = project_dir / "tombstone.json"
        tombstone_file.write_bytes(tombstone_bytes)
        tombstone_created = True
        logger.info(f"Preserved persistent tombstone ({len(tombstone_bytes)} bytes): {tombstone_file}")

    return {
        "project_slug": project_slug,
        "dry_run": dry_run,
        "chapters_cleaned": chapters_cleaned,
        "skipped_chapters": skipped_chapters,
        "cleaned_files_count": len(cleaned_files),
        "reclaimed_bytes": total_reclaimed_bytes,
        "reclaimed_mb": reclaimed_mb,
        "reclaimed_gb": reclaimed_gb,
        "tombstone_created": tombstone_created,
    }
