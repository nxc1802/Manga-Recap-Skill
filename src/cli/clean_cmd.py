"""
src/cli/clean_cmd.py
Subcommand handler: manga-recap clean
Safely eliminates intermediate TTS utterance audio clips, intermediate video clips,
raw video concats, and obsolete renders after final output is verified.
Enforces the Two-Receipt Safe Cleanup Protocol, ReleaseState.CLEANUP_ALLOWED,
real ffprobe/validate_video media checks, and writes an atomic TombstoneRecord.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.cli.exit_codes import CliValidationError
from src.core.contracts import (
    ArchiveReceipt,
    ReleaseState,
    TombstoneRecord,
    YouTubeReceipt,
)
from src.core.logger import logger
from src.core.schemas import utc_iso_now
from src.core.state_machine import (
    RELEASE_STATE_FILENAME,
    advance_release_state,
    load_release_record,
    transition,
)
from src.core.storage import (
    acquire_lock,
    atomic_write_json,
    format_chapter_filename,
    get_output_dir,
    get_project_dir,
    read_json,
)
from src.core.visual_contract import VisualPlan
from src.render.artifact_validation import validate_subtitles, validate_video


def _is_youtube_waived(project_dir: Path) -> bool:
    """Returns True only when series_setup_answers.json explicitly configures Q08 as no_upload."""
    setup_file = project_dir / "series_setup_answers.json"
    if not setup_file.exists():
        return False
    try:
        s_data = read_json(setup_file)
        q08 = s_data.get("answers", {}).get("Q08", {})
        u_ans = q08.get("user_answer") if isinstance(q08, dict) else q08
        if isinstance(u_ans, str) and u_ans.strip().lower() in (
            "không upload",
            "khong upload",
            "none",
            "no",
            "no_upload",
        ):
            return True
        if isinstance(u_ans, bool) and not u_ans:
            return True
        if isinstance(u_ans, dict) and u_ans.get("no_upload") is True:
            return True
    except Exception:
        return False
    return False


def _validate_verified_youtube_receipt(
    yt_receipt_file: Path,
    project_slug: str,
    chapter_number: str,
) -> YouTubeReceipt:
    """Validates a YouTube receipt file against YouTubeReceipt schema and verified requirements."""
    yt = YouTubeReceipt.model_validate(read_json(yt_receipt_file))
    if yt.project_slug != project_slug:
        raise ValueError(f"YouTubeReceipt project_slug mismatch: {yt.project_slug!r}")
    if str(yt.chapter_number) != str(chapter_number):
        raise ValueError(f"YouTubeReceipt chapter_number mismatch: {yt.chapter_number!r}")
    if yt.status != "verified":
        raise ValueError(f"YouTubeReceipt status is {yt.status!r}, expected 'verified'")
    if yt.upload_status != "processed":
        raise ValueError(f"YouTubeReceipt upload_status is {yt.upload_status!r}, expected 'processed'")
    if not yt.video_id.strip():
        raise ValueError("YouTubeReceipt video_id is blank")
    if not yt.verified_at or not yt.verified_at.strip():
        raise ValueError("YouTubeReceipt verified_at is missing")
    if yt.captions_required and (not yt.captions_uploaded or yt.has_captions is False):
        raise ValueError("YouTubeReceipt missing verified captions")
    if yt.thumbnail_required and (not yt.thumbnail_uploaded or yt.has_custom_thumbnail is False):
        raise ValueError("YouTubeReceipt missing verified custom thumbnail")
    return yt


def _verify_unchanged_manifest_and_state(
    project_slug: str,
    project_dir: Path,
    out_dir: Path,
    manifest_data: Dict[str, Any],
) -> None:
    """Verifies archived project/output files still on disk match their archived SHA-256."""
    if manifest_data.get("project_slug") != project_slug:
        raise CliValidationError("Two-Receipt Gate failed: archive_manifest.json project_slug mismatch.")

    skip_rel = {
        "archive_manifest.json",
        "archive_receipt.json",
        "release_state.json",
        "tombstone.json",
    }
    for item in manifest_data.get("files", []):
        rel_str = str(item.get("relative_path", ""))
        if not rel_str or rel_str in skip_rel:
            continue
        rel = Path(rel_str)
        if rel.parts[0] == "output":
            target = out_dir / Path(*rel.parts[1:])
        else:
            target = project_dir / rel
        if target.is_file():
            actual_hash = hashlib.sha256(target.read_bytes()).hexdigest()
            if actual_hash != item.get("sha256"):
                raise CliValidationError(
                    f"Two-Receipt Gate failed: project state drifted after archive for '{rel_str}'."
                )


def _check_two_receipt_gate(
    project_slug: str,
    project_dir: Path,
    target_chapters: List[str],
    *,
    dry_run: bool = False,
) -> Tuple[ArchiveReceipt, Dict[str, Any]]:
    """
    Enforces the Two-Receipt Safe Cleanup Gate:
    1. Hugging Face archive_receipt.json must validate as ArchiveReceipt with verified status,
       remote verification, restore verification at the same pinned 40-hex commit SHA, and
       matching archive_manifest.json SHA-256 (plus unchanged project state).
    2. YouTube youtube_receipt.json must validate as YouTubeReceipt (status='verified',
       upload_status='processed', verified_at) for all target chapters (unless waived in Q08).
    3. Canonical ReleaseState.CLEANUP_ALLOWED must hold (or transition via state_machine).

    Returns (validated_ArchiveReceipt, youtube_records_map).
    """
    archive_receipt_file = project_dir / "archive_receipt.json"
    if not archive_receipt_file.exists():
        raise CliValidationError(
            f"Two-Receipt Gate failed: Hugging Face archive receipt missing for '{project_slug}'. "
            f"Please run 'manga-recap archive push' and 'manga-recap archive verify' before deleting local assets."
        )

    try:
        raw_archive = read_json(archive_receipt_file)
    except Exception as e:
        raise CliValidationError(f"Two-Receipt Gate failed: Corrupt archive_receipt.json: {e}") from e

    try:
        archive_receipt = ArchiveReceipt.model_validate(raw_archive)
    except Exception as e:
        raise CliValidationError(
            f"Two-Receipt Gate failed: Hugging Face archive receipt is unverified or invalid: {e}"
        ) from e

    if archive_receipt.project_slug != project_slug:
        raise CliValidationError("Two-Receipt Gate failed: archive_receipt.json project_slug mismatch.")

    if archive_receipt.status != "verified" or not archive_receipt.commit_sha:
        raise CliValidationError(
            f"Two-Receipt Gate failed: Hugging Face archive receipt is unverified (status: {archive_receipt.status}). "
            f"Run 'manga-recap archive verify' first."
        )

    if (
        not archive_receipt.last_verified_at
        or not archive_receipt.restore_verified_at
        or archive_receipt.verified_file_count <= 0
        or archive_receipt.restored_verified_files is None
        or archive_receipt.restored_verified_files <= 0
        or archive_receipt.restored_revision != archive_receipt.commit_sha
        or archive_receipt.verification_revision != archive_receipt.commit_sha
    ):
        raise CliValidationError(
            "Two-Receipt Gate failed: archive must pass remote SHA-256 verification "
            "and a full SHA-256 restore test at the same pinned revision before deletion."
        )

    manifest_file = project_dir / "archive_manifest.json"
    if not manifest_file.is_file():
        raise CliValidationError("Two-Receipt Gate failed: archive_manifest.json is missing.")
    if hashlib.sha256(manifest_file.read_bytes()).hexdigest() != archive_receipt.manifest_sha256:
        raise CliValidationError("Two-Receipt Gate failed: archive manifest changed since verification.")

    out_dir = get_output_dir(project_slug)
    try:
        manifest_data = read_json(manifest_file)
    except Exception as e:
        raise CliValidationError(f"Two-Receipt Gate failed: Corrupt archive_manifest.json: {e}") from e
    _verify_unchanged_manifest_and_state(project_slug, project_dir, out_dir, manifest_data)

    youtube_waived = _is_youtube_waived(project_dir)

    youtube_records: Dict[str, Any] = {}
    if not youtube_waived:
        chapters_dir = project_dir / "chapters"
        missing_yt_chapters: List[str] = []
        for ch_num in target_chapters:
            chap_dir = chapters_dir / ch_num
            yt_receipt_file = chap_dir / "youtube_receipt.json"
            if not yt_receipt_file.exists():
                staged_name = format_chapter_filename(ch_num, ext="youtube_receipt.json", padded=True)
                if (out_dir / staged_name).exists():
                    yt_receipt_file = out_dir / staged_name

            if not yt_receipt_file.exists():
                missing_yt_chapters.append(ch_num)
                continue

            try:
                yt_model = _validate_verified_youtube_receipt(yt_receipt_file, project_slug, ch_num)
                youtube_records[ch_num] = {
                    "video_id": yt_model.video_id,
                    "video_url": yt_model.video_url,
                    "privacy_status": yt_model.privacy_status,
                }
            except Exception:
                missing_yt_chapters.append(ch_num)

        if missing_yt_chapters:
            raise CliValidationError(
                f"Two-Receipt Gate failed: Missing or unverified YouTube receipt for chapter(s): {', '.join(missing_yt_chapters)}. "
                f"Run 'manga-recap youtube verify' first, or configure Q08 as 'Không upload'."
            )

    # Enforce ReleaseState.CLEANUP_ALLOWED via canonical state_machine
    state_file = project_dir / RELEASE_STATE_FILENAME
    if state_file.is_file():
        try:
            rec = load_release_record(project_dir)
        except Exception as exc:
            raise CliValidationError(f"Two-Receipt Gate failed: Invalid release_state.json: {exc}") from exc

        if not youtube_waived:
            if rec.state == ReleaseState.RESTORE_VERIFIED and not dry_run:
                try:
                    rec = advance_release_state(
                        project_dir,
                        ReleaseState.CLEANUP_ALLOWED,
                        chapter_number=rec.chapter_number,
                    )
                except Exception as exc:
                    raise CliValidationError(
                        f"Two-Receipt Gate failed: Cannot advance ReleaseState to CLEANUP_ALLOWED: {exc}"
                    ) from exc
            elif rec.state != ReleaseState.CLEANUP_ALLOWED:
                raise CliValidationError(
                    f"Two-Receipt Gate failed: release_state is {rec.state.value}, must reach CLEANUP_ALLOWED before deletion."
                )

            if rec.evidence_hashes.get("archive_commit_sha") and rec.evidence_hashes["archive_commit_sha"] != archive_receipt.commit_sha:
                raise CliValidationError("Two-Receipt Gate failed: Archive commit_sha drifted since release_state.")
            if rec.evidence_hashes.get("archive_manifest_sha256") and rec.evidence_hashes["archive_manifest_sha256"] != archive_receipt.manifest_sha256:
                raise CliValidationError("Two-Receipt Gate failed: Archive manifest_sha256 drifted since release_state.")
            if rec.chapter_number in youtube_records and rec.evidence_hashes.get("youtube_video_id"):
                if rec.evidence_hashes["youtube_video_id"] != youtube_records[rec.chapter_number]["video_id"]:
                    raise CliValidationError("Two-Receipt Gate failed: YouTube video_id drifted since release_state.")
        else:
            allowed_waived = {
                ReleaseState.PRODUCTION_READY,
                ReleaseState.RESTORE_VERIFIED,
                ReleaseState.CLEANUP_ALLOWED,
            }
            if rec.state not in allowed_waived:
                raise CliValidationError(
                    f"Two-Receipt Gate failed: release_state is {rec.state.value}, cannot clean in no_upload mode."
                )
    elif not youtube_waived:
        # Evaluate state_machine transition guard directly even when release_state.json was not initialized
        for ch_num in target_chapters:
            try:
                transition(
                    ReleaseState.RESTORE_VERIFIED,
                    ReleaseState.CLEANUP_ALLOWED,
                    evidence={
                        "project_dir": project_dir,
                        "project_slug": project_slug,
                        "chapter_number": ch_num,
                    },
                )
            except Exception as exc:
                raise CliValidationError(
                    f"Two-Receipt Gate failed: state_machine CLEANUP_ALLOWED guard failed for chapter {ch_num}: {exc}"
                ) from exc

    return archive_receipt, youtube_records


def _validate_video_with_ffprobe(vid_path: Path, chap_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Runs ffprobe + semantic validate_video on an MP4 file."""
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(vid_path)],
        capture_output=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise ValueError(proc.stderr.decode(errors="replace")[-500:] or f"ffprobe exit {proc.returncode}")
    info = json.loads(proc.stdout.decode("utf-8"))
    v_stream = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if v_stream is None:
        raise ValueError("Missing video stream")
    width = int(v_stream.get("width", 0))
    height = int(v_stream.get("height", 0))
    duration = float((info.get("format") or {}).get("duration") or 0)
    num, den = map(float, v_stream["avg_frame_rate"].split("/"))
    fps = num / den if den > 0 else 0.0

    vp_path = (chap_dir / "visual_plan.json") if chap_dir is not None else None
    if vp_path is not None and vp_path.is_file():
        vp = VisualPlan.load(vp_path)
        return validate_video(
            vid_path,
            width=vp.target_width,
            height=vp.target_height,
            expected_duration=vp.total_duration_sec,
            fps=vp.fps,
            require_audio=True,
        )
    return validate_video(
        vid_path,
        width=width,
        height=height,
        expected_duration=duration,
        fps=fps,
        require_audio=True,
    )


def _verify_chapter_output_media(
    chap_dir: Path,
    out_dir: Path,
    ch_num: str,
) -> Tuple[Optional[Path], Optional[str]]:
    """
    Verifies final chapter video via ffprobe/validate_video (and validate_subtitles if SRT exists).
    Returns (verified_video_path, failure_reason_or_None).
    """
    out_vid = out_dir / format_chapter_filename(ch_num)
    final_vid = chap_dir / "video" / "final.mp4"

    candidate: Optional[Path] = None
    if out_vid.is_file() and out_vid.stat().st_size > 0:
        candidate = out_vid
    elif final_vid.is_file() and final_vid.stat().st_size > 0:
        candidate = final_vid

    if candidate is None:
        return None, "No output video found (final.mp4 or output/<slug>/chapter_xxx.mp4)"

    try:
        v_report = _validate_video_with_ffprobe(candidate, chap_dir=chap_dir)
    except Exception as exc:
        return None, f"Output video ffprobe/validate_video failed for {candidate.name}: {exc}"

    # Validate SRT if present
    srt_candidates = [
        out_dir / format_chapter_filename(ch_num, ext="srt"),
        chap_dir / "subtitles" / "final.srt",
        chap_dir / "subtitles" / f"chapter_{ch_num}.srt",
    ]
    for srt_path in srt_candidates:
        if srt_path.is_file():
            try:
                validate_subtitles(srt_path, duration=float(v_report["duration"]))
            except Exception as exc:
                return None, f"Subtitle validation failed for {srt_path.name}: {exc}"
            break

    return candidate, None


def _build_and_write_tombstone(
    tombstone_file: Path,
    project_slug: str,
    archive_receipt: ArchiveReceipt,
    youtube_records: Dict[str, Any],
    total_reclaimed_bytes: int,
    cleaned_files_count: int,
) -> int:
    """
    Constructs, validates against TombstoneRecord, enforces < 2048 bytes,
    and atomically writes tombstone.json. Returns written byte length.
    """
    compact_yt: Dict[str, str] = {}
    for ch, val in youtube_records.items():
        if isinstance(val, dict):
            vid = val.get("video_id") or ""
        else:
            vid = str(val or "")
        if vid:
            compact_yt[str(ch)] = vid

    base_payload: Dict[str, Any] = {
        "schema_version": "4.0.0",
        "tombstone_version": "4.0.0",
        "project_slug": project_slug,
        "archive_timestamp": archive_receipt.created_at,
        "cleanup_timestamp": utc_iso_now(),
        "hf_repo_id": archive_receipt.repo_id,
        "hf_commit_sha": archive_receipt.commit_sha,
        "hf_remote_url": archive_receipt.remote_url,
        "manifest_sha256": archive_receipt.manifest_sha256,
        "youtube_videos": compact_yt,
        "reclaimed_bytes": total_reclaimed_bytes,
        "cleaned_files_count": cleaned_files_count,
    }

    record = TombstoneRecord.model_validate(base_payload)

    # Level 1: Formatted indentation (indent=2) via atomic_write_json
    formatted = record.model_dump_json(indent=2).encode("utf-8")
    if len(formatted) < 2048:
        atomic_write_json(tombstone_file, record)
        return tombstone_file.stat().st_size

    # Level 2: Minified JSON
    minified = json.dumps(record.model_dump(mode="json", exclude_none=True), separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(minified) >= 2048:
        # Level 3: Progressive truncation for very large chapter series
        total_yt_count = len(compact_yt)
        truncated_payload = dict(base_payload)
        truncated_payload["youtube_videos_total"] = total_yt_count
        truncated_payload["youtube_videos_truncated"] = True

        sorted_keys = sorted(compact_yt.keys(), key=lambda x: int(x) if x.isdigit() else x)
        truncated_yt = dict(compact_yt)
        for k in reversed(sorted_keys):
            candidate_rec = TombstoneRecord.model_validate(truncated_payload)
            cand_bytes = json.dumps(
                candidate_rec.model_dump(mode="json", exclude_none=True),
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
            if len(cand_bytes) < 2000:
                minified = cand_bytes
                break
            truncated_yt.pop(k, None)
            truncated_payload["youtube_videos"] = truncated_yt
        else:
            truncated_payload["youtube_videos"] = {}
            candidate_rec = TombstoneRecord.model_validate(truncated_payload)
            minified = json.dumps(
                candidate_rec.model_dump(mode="json", exclude_none=True),
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")

    # Atomic write for compact <2048 byte payload
    with acquire_lock(tombstone_file):
        tmp_path = tombstone_file.with_name(f"{tombstone_file.name}.{os.getpid()}.tmp")
        with open(tmp_path, "wb") as fh:
            fh.write(minified)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, tombstone_file)
    return len(minified)


def _feature_cleanup_allowed(
    project_slug: str,
    project_dir: Path,
    out_dir: Path,
    dry_run: bool,
    youtube_waived: bool,
) -> bool:
    """Checks whether project-level feature artifact cleanup is authorized."""
    if youtube_waived or dry_run:
        return True
    for candidate in (out_dir / "feature.youtube_receipt.json", project_dir / "feature.youtube_receipt.json"):
        if candidate.is_file():
            try:
                yt = YouTubeReceipt.model_validate(read_json(candidate))
                if (
                    yt.project_slug == project_slug
                    and yt.status == "verified"
                    and yt.upload_status == "processed"
                    and bool(yt.video_id.strip())
                    and bool(yt.verified_at and yt.verified_at.strip())
                ):
                    return True
            except Exception:
                continue
    return False


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

    archive_receipt: Optional[ArchiveReceipt] = None
    youtube_records: Dict[str, Any] = {}

    if not dry_run and bypass_receipts:
        raise CliValidationError(
            "--bypass-receipts is forbidden for destructive cleanup. "
            "Dry runs are always permitted without archive or YouTube receipts."
        )

    if not dry_run:
        if confirm_project != project_slug:
            raise CliValidationError(
                f"Safety barrier failed: --confirm-project '{project_slug}' is required for actual deletion "
                f"(got '{confirm_project}')."
            )

        archive_receipt, youtube_records = _check_two_receipt_gate(
            project_slug=project_slug,
            project_dir=project_dir,
            target_chapters=target_chapters,
            dry_run=False,
        )

    cleaned_files: List[str] = []
    total_reclaimed_bytes = 0
    chapters_cleaned: List[str] = []
    skipped_chapters: List[Dict[str, str]] = []

    for ch_num in target_chapters:
        chap_dir = chapters_dir / ch_num
        if not chap_dir.exists():
            continue

        verified_video, fail_reason = _verify_chapter_output_media(chap_dir, out_dir, ch_num)
        if not verified_video:
            skipped_chapters.append({
                "chapter": ch_num,
                "reason": fail_reason or "Output media validation failed",
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

        # 4. Raw video concats and obsolete version renders (keep verified final.mp4)
        v_dir = chap_dir / "video"
        if v_dir.exists():
            for p in v_dir.glob("raw_video_*.mp4"):
                ch_files_to_delete.append(p)

            for p in v_dir.glob("*_v[0-9]*.mp4"):
                if p.resolve() != verified_video.resolve():
                    ch_files_to_delete.append(p)

        # 5. Temp clips and concat files
        for pattern in ("*_raw_temp*.mp4", "*_subs_temp*.srt", "*concat_list*.txt", "*.tmp"):
            for p in chap_dir.rglob(pattern):
                if p.is_file() and "raw" not in p.parts and "panels" not in p.parts:
                    ch_files_to_delete.append(p)

        # 6. Optional purge of raw pages and panel crops after archive verification
        if purge_raw and (archive_receipt is not None or dry_run):
            if purge_raw and not dry_run and archive_receipt is None:
                pass
            else:
                for pattern in ("raw/*", "panels/*"):
                    for p in chap_dir.glob(pattern):
                        if p.is_file():
                            ch_files_to_delete.append(p)

        for f in sorted(set(ch_files_to_delete)):
            if f.exists():
                size = f.stat().st_size
                total_reclaimed_bytes += size
                cleaned_files.append(str(f.name))
                if not dry_run:
                    f.unlink(missing_ok=True)

        if not dry_run:
            if a_clips_dir.exists() and not any(a_clips_dir.iterdir()):
                a_clips_dir.rmdir()
            if v_clips_dir.exists() and not any(v_clips_dir.iterdir()):
                v_clips_dir.rmdir()

        chapters_cleaned.append(ch_num)

    # Guard project-level feature deletions: NEVER delete on single-chapter -c,
    # when any chapter was skipped, or when feature.youtube_receipt.json is unverified.
    youtube_waived = _is_youtube_waived(project_dir)
    can_clean_project_level = (
        chapter_arg is None
        and len(target_chapters) > 0
        and len(skipped_chapters) == 0
        and len(chapters_cleaned) == len(target_chapters)
    )
    if can_clean_project_level:
        project_audio_final = project_dir / "audio" / "final.wav"
        out_full_wav = out_dir / f"{project_slug}_full_recap.wav"
        out_full_mp4 = out_dir / f"{project_slug}_full_recap.mp4"
        out_full_srt = out_dir / f"{project_slug}_full_recap.srt"
        feature_candidates = sorted(out_dir.glob("*_feature.mp4"))
        feature_srt_candidates = sorted(out_dir.glob("*_feature.srt"))

        feature_artifacts_present = any(
            p.exists() for p in (project_audio_final, out_full_wav, out_full_mp4, out_full_srt)
        )
        if feature_artifacts_present and _feature_cleanup_allowed(
            project_slug, project_dir, out_dir, dry_run, youtube_waived
        ):
            if project_audio_final.exists():
                total_reclaimed_bytes += project_audio_final.stat().st_size
                cleaned_files.append(project_audio_final.name)
                if not dry_run:
                    project_audio_final.unlink(missing_ok=True)

            if out_full_wav.exists():
                total_reclaimed_bytes += out_full_wav.stat().st_size
                cleaned_files.append(out_full_wav.name)
                if not dry_run:
                    out_full_wav.unlink(missing_ok=True)

            if out_full_mp4.exists() and feature_candidates:
                feature_mp4 = feature_candidates[0]
                if out_full_mp4.stat().st_size == feature_mp4.stat().st_size:
                    feature_ok = True
                    if not dry_run:
                        try:
                            _validate_video_with_ffprobe(feature_mp4)
                        except Exception:
                            feature_ok = False
                    if feature_ok:
                        total_reclaimed_bytes += out_full_mp4.stat().st_size
                        cleaned_files.append(out_full_mp4.name)
                        if not dry_run:
                            out_full_mp4.unlink(missing_ok=True)

            if out_full_srt.exists() and feature_srt_candidates:
                if out_full_srt.stat().st_size == feature_srt_candidates[0].stat().st_size:
                    total_reclaimed_bytes += out_full_srt.stat().st_size
                    cleaned_files.append(out_full_srt.name)
                    if not dry_run:
                        out_full_srt.unlink(missing_ok=True)

    reclaimed_mb = round(total_reclaimed_bytes / (1024 * 1024), 2)
    reclaimed_gb = round(total_reclaimed_bytes / (1024 * 1024 * 1024), 3)

    tombstone_created = False
    if not dry_run:
        assert archive_receipt is not None
        for ch_num in target_chapters:
            if ch_num not in youtube_records:
                chap_dir = chapters_dir / ch_num
                rec_f = chap_dir / "youtube_receipt.json"
                if rec_f.exists():
                    try:
                        yt_model = _validate_verified_youtube_receipt(rec_f, project_slug, ch_num)
                        youtube_records[ch_num] = {
                            "video_id": yt_model.video_id,
                            "video_url": yt_model.video_url,
                            "privacy_status": yt_model.privacy_status,
                        }
                    except Exception:
                        pass

        tombstone_file = project_dir / "tombstone.json"
        written_bytes = _build_and_write_tombstone(
            tombstone_file=tombstone_file,
            project_slug=project_slug,
            archive_receipt=archive_receipt,
            youtube_records=youtube_records,
            total_reclaimed_bytes=total_reclaimed_bytes,
            cleaned_files_count=len(cleaned_files),
        )
        tombstone_created = True
        logger.info(f"Preserved persistent tombstone ({written_bytes} bytes): {tombstone_file}")

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
