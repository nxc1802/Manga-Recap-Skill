"""Domain workflow orchestration shared by local CLI, batch, and cloud runners."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import contextlib
import json
import os
from pathlib import Path
import re
import shutil
import time
import wave
from typing import Any, Callable, Dict, List, Optional, Tuple

from pydantic import ValidationError

from src.core.checkpoints import (
    get_resumable_stage,
    reset_stage_checkpoint,
    update_checkpoint,
)
from src.core.cleanup import clean_project_chapters
from src.core.contact_sheet import ContactSheetGenerator
from src.core.errors import (
    DomainValidationError,
    StorageCorruptError,
    StorageFileNotFoundError,
)
from src.core.human_review import (
    approve_preview,
    require_preview_approval,
    require_voice_approval,
)
from src.core.logger import logger
from src.core.panel_repack import (
    PanelPlan,
    apply_panel_plan,
    inspect_panels_with_candidates,
)
from src.core.project import (
    format_chapter_filename,
    get_chapter_dir,
    get_output_dir,
    get_project_dir,
    init_chapter_layout,
    init_project_layout,
    load_chapter_metadata,
    load_manifest,
    load_recap_script,
    resolve_existing_chapter_dir,
    resolve_existing_project_dir,
    stage_chapter_export,
)
from src.core.review_pack import create_review_pack
from src.core.schemas import (
    ChapterMetadata,
    CheckpointStatus,
    HumanReviewStatus,
    PanelManifest,
    PanelStatus,
    ProjectConfig,
    RawImageInfo,
    RecapScript,
    ScriptSegment,
    StageType,
    VoiceSelectionApproval,
    utc_iso_now,
)
from src.core.series_setup import extract_named_settings, require_series_setup
from src.core.storage import (
    atomic_write_json,
    calculate_sha256,
    calculate_utterance_fingerprint,
    read_json,
    verify_checksum,
)
from src.core.streaming_segmenter import StreamingSegmenter
from src.core.utils import assert_file_nonempty
from src.media.ffmpeg import get_wav_duration
from src.crawler.errors import CrawlerError
from src.crawler.verifier import ImageVerifier
from src.render.chapter_renderer import (
    compile_project_chapters,
    render_chapter,
    validate_existing_chapter,
)
from src.render.visual_policy import (
    generate_visual_plan,
    resolve_visual_plan,
    validate_narration_evidence,
)
from src.tts.catalog import resolve_voice_id
from src.tts.engine import get_tts_engine, get_wav_duration_seconds


def _find_existing_panel_path(chap_dir: Path, panels_dir: Path, *candidates: Optional[str]) -> Optional[Path]:
    for cand_str in candidates:
        if not cand_str:
            continue
        for base in (chap_dir, panels_dir):
            cand = base / cand_str
            if cand.exists() and cand.stat().st_size > 0:
                return cand
    return None


def _reset_downstream_render_artifacts(chap_dir: Path, stages: Tuple[StageType, ...]) -> None:
    for sub, pat in (("video", "*.mp4"), ("subtitles", "*.srt")):
        target_dir = chap_dir / sub
        if target_dir.exists():
            for f in target_dir.glob(pat):
                with contextlib.suppress(Exception):
                    f.unlink()
    (chap_dir / "visual_plan.json").unlink(missing_ok=True)
    for stg in stages:
        reset_stage_checkpoint(chap_dir, stg)


def _resolve_chapter_final_paths(chap_dir: Path, chap_num: str) -> Tuple[Path, Path]:
    vid = chap_dir / "video" / f"chapter_{chap_num}_final.mp4"
    if not vid.exists():
        vid = chap_dir / "video" / "final.mp4"
    srt = chap_dir / "subtitles" / f"chapter_{chap_num}.srt"
    if not srt.exists():
        srt = chap_dir / "subtitles" / "final.srt"
    return vid, srt


def parse_chapter_list(chapters_arg: Optional[str]) -> List[str]:
    """Parses comma/range chapter selectors like '1,2,3' or '1-5' into deduplicated strings."""
    if not chapters_arg:
        return []
    result: List[str] = []
    tokens = [t.strip() for t in str(chapters_arg).split(",") if t.strip()]
    for token in tokens:
        if "-" in token:
            parts = token.split("-", 1)
            try:
                start_i = int(parts[0].strip())
                end_i = int(parts[1].strip())
                for i in range(start_i, end_i + 1):
                    result.append(str(i))
            except ValueError:
                result.append(token)
        else:
            result.append(token)
    seen = set()
    deduped = []
    for c in result:
        if c not in seen:
            seen.add(c)
            deduped.append(c)
    return deduped


def discover_project_chapters(project_dir: Path) -> List[str]:
    """Auto-discovers registered or available chapters in project."""
    proj_json = project_dir / "project.json"
    if proj_json.exists():
        with contextlib.suppress(Exception):
            p_data = read_json(proj_json)
            if isinstance(p_data, dict) and p_data.get("chapters"):
                return [str(c) for c in p_data["chapters"]]

    chapters_dir = project_dir / "chapters"
    if chapters_dir.exists():
        entries = [
            e for e in os.listdir(chapters_dir)
            if (chapters_dir / e).is_dir() and not e.startswith(".")
        ]
        return sorted(entries, key=lambda x: (0, int(x)) if x.isdigit() else (1, x))
    return []


# ==============================================================================
# Inspect / Validate / Status
# ==============================================================================

def inspect_project_or_chapter(project_slug: str, chapter_num: Optional[str] = None) -> Dict[str, Any]:
    """Inspects project overview or chapter assets, metrics, and checkpoints."""
    if not project_slug:
        raise StorageFileNotFoundError("Project slug is required.")
    project_dir = resolve_existing_project_dir(project_slug)

    if chapter_num is None:
        project_cfg = read_json(project_dir / "project.json")
        characters_path = project_dir / "characters.json"
        char_count = 0
        if characters_path.exists():
            with contextlib.suppress(Exception):
                char_count = len(read_json(characters_path).get("characters", {}))

        chapters_dir = project_dir / "chapters"
        chapter_summaries: List[Dict[str, Any]] = []
        if chapters_dir.exists():
            for entry in sorted(os.listdir(chapters_dir)):
                chap_subdir = chapters_dir / entry
                if chap_subdir.is_dir():
                    chap_json = chap_subdir / "chapter.json"
                    status = "unknown"
                    if chap_json.exists():
                        with contextlib.suppress(Exception):
                            c_data = read_json(chap_json)
                            status = c_data.get("overall_status", c_data.get("status", "pending"))
                    has_video = (chap_subdir / "video").exists() and any(
                        (chap_subdir / "video").glob("*.mp4")
                    )
                    chapter_summaries.append({
                        "chapter_number": entry,
                        "status": status,
                        "has_video": has_video,
                    })

        return {
            "project_slug": project_slug,
            "title": project_cfg.get("title", project_slug),
            "language": project_cfg.get("language", "vi"),
            "total_chapters": len(chapter_summaries),
            "chapters": chapter_summaries,
            "characters_count": char_count,
            "has_story_summary": (project_dir / "story_summary.md").exists(),
        }

    chap_dir = resolve_existing_chapter_dir(project_slug, chapter_num, project_dir=project_dir)
    chap_meta: Dict[str, Any] = {}
    chap_json_path = chap_dir / "chapter.json"
    if chap_json_path.exists():
        with contextlib.suppress(Exception):
            chap_meta = read_json(chap_json_path)

    img_exts = {".jpg", ".jpeg", ".png", ".webp"}
    raw_dir = chap_dir / "raw"
    raw_count = len([f for f in raw_dir.glob("*") if f.suffix.lower() in img_exts]) if raw_dir.exists() else 0

    panels_dir = chap_dir / "panels"
    panels_count = len([f for f in panels_dir.glob("*") if f.suffix.lower() in img_exts]) if panels_dir.exists() else 0

    manifest_path = chap_dir / "manifest.json"
    contact_sheets_count = 0
    if manifest_path.exists():
        with contextlib.suppress(Exception):
            m_data = read_json(manifest_path)
            contact_sheets_count = len(m_data.get("contact_sheets", []))
            if panels_count == 0:
                panels_count = len(m_data.get("panels", []))

    cs_dir = chap_dir / "contact_sheets"
    if cs_dir.exists() and contact_sheets_count == 0:
        contact_sheets_count = len([f for f in cs_dir.glob("*") if f.suffix.lower() in img_exts])

    audio_clips_dir = chap_dir / "audio" / "clips"
    if not audio_clips_dir.exists():
        audio_clips_dir = chap_dir / "audio"

    audio_count = 0
    total_audio_seconds = 0.0
    if audio_clips_dir.exists():
        wav_files = list(audio_clips_dir.glob("*.wav"))
        audio_count = len(wav_files)
        for wav_path in wav_files:
            with contextlib.suppress(Exception):
                with wave.open(str(wav_path), "rb") as wf:
                    frames, rate = wf.getnframes(), wf.getframerate()
                    if rate > 0:
                        total_audio_seconds += frames / float(rate)

    script_path = chap_dir / "script.json"
    script_segments_count = 0
    if script_path.exists():
        with contextlib.suppress(Exception):
            script_segments_count = len(read_json(script_path).get("segments", []))

    video_dir = chap_dir / "video"
    video_files = (
        [{"filename": vf.name, "size_bytes": vf.stat().st_size} for vf in sorted(video_dir.glob("*.mp4"))]
        if video_dir.exists()
        else []
    )

    checkpoints = {
        stage_name: (s_info.get("status", "unknown") if isinstance(s_info, dict) else str(s_info))
        for stage_name, s_info in chap_meta.get("stages", {}).items()
    }

    return {
        "project_slug": project_slug,
        "chapter_number": str(chapter_num),
        "title": chap_meta.get("title", f"Chapter {chapter_num}"),
        "overall_status": chap_meta.get("overall_status", chap_meta.get("status", "pending")),
        "current_stage": chap_meta.get("current_stage", "crawl"),
        "assets": {
            "raw_images_count": raw_count,
            "panels_count": panels_count,
            "contact_sheets_count": contact_sheets_count,
            "audio_clips_count": audio_count,
            "total_audio_duration_seconds": round(total_audio_seconds, 2),
            "script_segments_count": script_segments_count,
            "video_files": video_files,
        },
        "checkpoints": checkpoints,
    }


def validate_project_or_chapter(
    project_slug: str,
    *,
    chapter: Optional[str] = None,
    strict: bool = False,
    pacing: bool = False,
) -> Dict[str, Any]:
    """Validates schema conformance, panel/audio evidence, and optional script pacing."""
    if not project_slug:
        raise DomainValidationError("Project slug is required.")
    project_dir = resolve_existing_project_dir(project_slug)

    errors: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []
    pacing_reports: Dict[str, Any] = {}
    total_checks = 0
    passed_checks = 0

    def check(condition: bool, target: str, message: str, is_warning: bool = False) -> None:
        nonlocal total_checks, passed_checks
        total_checks += 1
        if condition:
            passed_checks += 1
        else:
            item = {"target": target, "message": message}
            (warnings if is_warning else errors).append(item)

    project_json_path = project_dir / "project.json"
    proj_config: Optional[ProjectConfig] = None
    if project_json_path.exists():
        try:
            proj_config = ProjectConfig.model_validate(read_json(project_json_path))
            check(True, "project.json", "Valid ProjectConfig schema")
        except (ValidationError, StorageCorruptError) as e:
            check(False, "project.json", f"Invalid project.json schema: {e}")
    else:
        check(False, "project.json", "Missing project.json in project root")

    characters_path = project_dir / "characters.json"
    if characters_path.exists():
        try:
            from src.core.schemas import CharacterDictionary
            CharacterDictionary.model_validate(read_json(characters_path))
            check(True, "characters.json", "Valid CharacterDictionary schema")
        except (ValidationError, StorageCorruptError) as e:
            check(False, "characters.json", f"Invalid characters.json schema: {e}")

    summary_path = project_dir / "story_summary.md"
    if summary_path.exists():
        content = summary_path.read_text(encoding="utf-8").strip()
        check(len(content) > 0, "story_summary.md", "story_summary.md is empty", is_warning=True)
    else:
        check(False, "story_summary.md", "Missing story_summary.md", is_warning=True)

    chapters_to_check: List[str] = []
    if chapter is not None:
        chapters_to_check.append(str(chapter))
    else:
        chapters_dir = project_dir / "chapters"
        if chapters_dir.exists():
            chapters_to_check = [c for c in sorted(os.listdir(chapters_dir)) if (chapters_dir / c).is_dir()]

    for chap_num in chapters_to_check:
        chap_dir = resolve_existing_chapter_dir(project_slug, chap_num, project_dir=project_dir, require_exists=False)
        if not chap_dir.exists():
            check(False, f"chapter_{chap_num}", f"Chapter directory does not exist: {chap_dir}")
            continue

        chap_json_path = chap_dir / "chapter.json"
        chap_meta: Optional[ChapterMetadata] = None
        if chap_json_path.exists():
            try:
                chap_meta = ChapterMetadata.model_validate(read_json(chap_json_path))
                check(True, f"chapter_{chap_num}/chapter.json", "Valid ChapterMetadata schema")
                check(
                    chap_meta.technical_status in list(CheckpointStatus),
                    f"chapter_{chap_num}:technical_status",
                    f"Invalid technical_status: {chap_meta.technical_status}",
                )
                check(
                    chap_meta.human_review_status in list(HumanReviewStatus),
                    f"chapter_{chap_num}:human_review_status",
                    f"Invalid human_review_status: {chap_meta.human_review_status}",
                )
            except (ValidationError, StorageCorruptError) as e:
                check(False, f"chapter_{chap_num}/chapter.json", f"Invalid chapter.json schema: {e}")
        else:
            check(False, f"chapter_{chap_num}/chapter.json", "Missing chapter.json")

        manifest_path = chap_dir / "manifest.json"
        manifest_panel_ids: set[str] = set()
        skip_panel_ids: set[str] = set()
        panel_lookup: Dict[str, str] = {}
        panel_disk_paths: Dict[str, Path] = {}
        if manifest_path.exists():
            try:
                manifest_obj = PanelManifest.model_validate(read_json(manifest_path))
                check(True, f"chapter_{chap_num}/manifest.json", "Valid PanelManifest schema")
                panels_dir = chap_dir / "panels"
                for panel in manifest_obj.panels:
                    if panel.status == PanelStatus.SKIP:
                        skip_panel_ids.add(panel.panel_id)
                    panel_lookup[panel.panel_id] = panel.panel_id
                    manifest_panel_ids.add(panel.panel_id)
                    if panel.filename:
                        manifest_panel_ids.add(panel.filename)
                        panel_lookup[panel.filename] = panel.panel_id
                        panel_lookup[Path(panel.filename).name] = panel.panel_id
                    if panel.relative_path:
                        for key in (panel.relative_path, Path(panel.relative_path).name, Path(panel.relative_path).stem):
                            manifest_panel_ids.add(key)
                            panel_lookup[key] = panel.panel_id
                    found = _find_existing_panel_path(
                        chap_dir,
                        panels_dir,
                        panel.relative_path,
                        panel.panel_id,
                        Path(panel.relative_path).name if panel.relative_path else None,
                        panel.filename,
                    )
                    if found is not None:
                        panel_disk_paths[panel.panel_id] = found
                    check(
                        found is not None,
                        f"panel:{panel.panel_id}",
                        f"Referenced panel image not found on disk: {panel.relative_path}",
                    )
            except (ValidationError, StorageCorruptError) as e:
                check(False, f"chapter_{chap_num}/manifest.json", f"Invalid manifest.json schema: {e}")

        script_path = chap_dir / "script.json"
        if script_path.exists():
            try:
                script_obj = RecapScript.model_validate(read_json(script_path))
                check(True, f"chapter_{chap_num}/script.json", "Valid RecapScript schema")
                panels_dir = chap_dir / "panels"
                for seg in script_obj.segments:
                    check(
                        bool(seg.seq_id and str(seg.seq_id).strip()),
                        f"segment:{seg.seq_id}:seq_id",
                        "Segment seq_id must be a non-empty string",
                    )
                    if manifest_panel_ids:
                        for pid in seg.panel_ids:
                            canon_pid = (
                                panel_lookup.get(pid)
                                or panel_lookup.get(Path(pid).name)
                                or panel_lookup.get(Path(pid).stem)
                            )
                            if not canon_pid:
                                check(
                                    False,
                                    f"segment:{seg.seq_id}:panel:{pid}",
                                    f"Segment references panel '{pid}' not in manifest.json",
                                )
                            elif canon_pid in skip_panel_ids:
                                check(
                                    False,
                                    f"segment:{seg.seq_id}:panel_skip:{pid}",
                                    f"Segment references Group 3 SKIP panel '{canon_pid}' (forbidden in script)",
                                )
                            disk_found = canon_pid in panel_disk_paths or (
                                _find_existing_panel_path(chap_dir, panels_dir, pid, Path(pid).name, f"{pid}.jpg") is not None
                            )
                            check(
                                disk_found,
                                f"segment:{seg.seq_id}:panel_disk:{pid}",
                                f"Referenced panel image for segment '{seg.seq_id}' not found on disk: {pid}",
                            )

                    if seg.status == "synthesized" and seg.audio_path:
                        audio_file = Path(seg.audio_path)
                        if not audio_file.is_absolute():
                            audio_file = chap_dir / audio_file
                        exists_and_nonempty = audio_file.exists() and audio_file.stat().st_size > 0
                        final_vid_exists = (
                            (chap_dir / "video" / "final.mp4").exists()
                            or (get_output_dir(project_slug) / format_chapter_filename(chap_num)).exists()
                        )
                        if not (not exists_and_nonempty and final_vid_exists):
                            check(
                                exists_and_nonempty,
                                f"segment:{seg.seq_id}:audio",
                                f"Synthesized audio file missing or empty: {audio_file}",
                            )
                            if exists_and_nonempty:
                                if seg.checksum:
                                    actual_hash = calculate_sha256(audio_file)
                                    check(
                                        actual_hash.lower() == seg.checksum.strip().lower(),
                                        f"segment:{seg.seq_id}:audio_checksum",
                                        f"Synthesized audio checksum mismatch for {audio_file}: expected {seg.checksum}, got {actual_hash}",
                                    )
                                if seg.duration is not None:
                                    try:
                                        actual_dur = get_wav_duration_seconds(audio_file)
                                        check(
                                            abs(actual_dur - float(seg.duration)) <= 0.05,
                                            f"segment:{seg.seq_id}:audio_duration",
                                            f"Synthesized audio duration mismatch for {audio_file}: expected {seg.duration}s, got {actual_dur:.3f}s",
                                        )
                                    except Exception as err:
                                        check(
                                            False,
                                            f"segment:{seg.seq_id}:audio_duration",
                                            f"Failed to inspect duration for {audio_file}: {err}",
                                        )

                if pacing:
                    from src.core.pacing import estimate_script_duration_seconds
                    pacing_cfg = proj_config.script_pacing if proj_config else None
                    speed = proj_config.audio_settings.default_speed if proj_config else 1.0
                    p_report = estimate_script_duration_seconds(script_obj, speed=speed, pacing_config=pacing_cfg)
                    pacing_reports[str(chap_num)] = p_report
                    if not p_report.get("within_budget", True):
                        recs = "; ".join(p_report.get("recommendations", []))
                        check(False, f"chapter_{chap_num}/script_pacing", f"Script pacing warning: {recs}", is_warning=not strict)
                    else:
                        check(True, f"chapter_{chap_num}/script_pacing", "Script pacing is within budget")

            except (ValidationError, StorageCorruptError) as e:
                check(False, f"chapter_{chap_num}/script.json", f"Invalid script.json schema: {e}")

        if chap_meta and hasattr(chap_meta, "stages"):
            render_stage = chap_meta.stages.get("render")
            if render_stage and getattr(render_stage, "status", None) == "completed":
                video_dir = chap_dir / "video"
                has_video = video_dir.exists() and any(f.stat().st_size > 0 for f in video_dir.glob("*.mp4"))
                check(has_video, f"chapter_{chap_num}/video", "Render marked completed but no valid MP4 video found")

        if strict and chap_meta and hasattr(chap_meta, "stages"):
            for s_info in chap_meta.stages.values():
                for rel_path, expected_hash in (getattr(s_info, "checksums", {}) or {}).items():
                    target_file = chap_dir / rel_path
                    if target_file.exists():
                        check(verify_checksum(target_file, expected_hash), f"checksum:{rel_path}", f"Checksum mismatch for {rel_path}")
                    else:
                        check(False, f"checksum:{rel_path}", f"File for checksum not found: {rel_path}")

    is_valid = len(errors) == 0 and (not strict or len(warnings) == 0)
    result_payload: Dict[str, Any] = {
        "valid": is_valid,
        "project_slug": project_slug,
        "chapters_validated": chapters_to_check,
        "summary": {
            "total_checks": total_checks,
            "passed": passed_checks,
            "warnings_count": len(warnings),
            "errors_count": len(errors),
        },
        "errors": errors,
        "warnings": warnings,
    }
    if pacing:
        result_payload["pacing_reports"] = pacing_reports

    if not is_valid:
        raise DomainValidationError(
            f"Validation failed with {len(errors)} error(s) and {len(warnings)} warning(s).",
            details=errors,
            data=result_payload,
        )
    return result_payload


def verified_chapter_completion(project_dir: Path, chapter_num: str, claimed: bool) -> Tuple[bool, Optional[str]]:
    if not claimed:
        return False, None
    try:
        validate_existing_chapter(project_dir, chapter_num)
        return True, None
    except Exception as exc:
        return False, str(exc)


def _derive_chapter_next_action(
    project_dir: Path,
    chap_dir: Path,
    chapter_num: str,
    resumable_stage: Optional[StageType],
    is_complete: bool,
    artifact_error: Optional[str],
) -> Tuple[str, Optional[str]]:
    if is_complete:
        return "release_or_compile", None
    if str(chapter_num) == "1" and (chap_dir / "video" / "preview.mp4").is_file():
        try:
            require_preview_approval(project_dir)
        except Exception as exc:
            return "review_approve_chapter_1", str(exc)
    if not (chap_dir / "manifest.json").is_file():
        return "prepare_chapter", artifact_error or "Missing manifest.json; run prepare/crawl+segment"
    if not (chap_dir / "script.json").is_file():
        return "author_script", "Missing script.json; agent must inspect contact sheets and write narration"
    if resumable_stage in (StageType.CRAWL, StageType.SEGMENT, StageType.INGEST):
        return "prepare_chapter", artifact_error
    if resumable_stage in (StageType.SCRIPT, StageType.AGENT):
        return "author_script", artifact_error
    if resumable_stage in (StageType.TTS, StageType.AUDIO):
        return "synthesize_tts", artifact_error
    if str(chapter_num) == "1":
        return "render_preview", artifact_error
    return "produce_chapter", artifact_error


def _summarize_cloud_state(project_dir: Path, *, chapter_dir: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Collects non-secret Kaggle job lifecycle, policy, hardware telemetry, and chunk resume status."""
    summary: Dict[str, Any] = {}
    target_dir = chapter_dir if chapter_dir is not None else project_dir
    job_path = target_dir / "cloud_job.json"
    if not job_path.is_file() and chapter_dir is not None:
        # Series jobs store cloud_job.json at project root.
        job_path = project_dir / "cloud_job.json"
    if job_path.is_file():
        try:
            job = read_json(job_path)
            summary["job"] = {
                "status": job.get("status"),
                "kernel_ref": job.get("kernel_ref"),
                "account_name": job.get("account_name"),
                "job_type": job.get("job_type"),
                "queue_elapsed_sec": job.get("queue_elapsed_sec"),
                "total_elapsed_sec": job.get("total_elapsed_sec"),
                "last_remote_status": job.get("last_remote_status"),
                "error": job.get("error"),
                "updated_at": job.get("updated_at"),
            }
        except Exception as exc:
            summary["job"] = {"status": "corrupt", "error": str(exc)}

    plan_path = project_dir / "cloud_series_plan.json"
    if plan_path.is_file():
        try:
            plan_data = read_json(plan_path)
            chunks = list(plan_data.get("chunks") or [])
            summary["series_plan"] = {
                "status": plan_data.get("status"),
                "total_chunks": len(chunks),
                "completed_chunks": sum(1 for c in chunks if c.get("status") == "synced"),
                "skipped_verified": list(plan_data.get("skipped_verified") or []),
                "updated_at": plan_data.get("updated_at"),
            }
        except Exception:
            pass

    if chapter_dir is not None:
        rec_path = chapter_dir / "cloud_receipt.json"
        if rec_path.is_file():
            try:
                rec = read_json(rec_path)
                summary["receipt"] = {
                    "job_type": rec.get("job_type"),
                    "quality": rec.get("quality"),
                    "verified_at": rec.get("verified_at"),
                }
            except Exception:
                pass

    tel_path = target_dir / "cloud_telemetry.json"
    if not tel_path.is_file() and chapter_dir is not None:
        tel_path = project_dir / "cloud_telemetry.json"
    if tel_path.is_file():
        try:
            tel = read_json(tel_path)
            hw = tel.get("hardware") or {}
            summary["telemetry"] = {
                "policy": tel.get("policy"),
                "scheduler_policy": hw.get("scheduler_policy"),
                "devices": hw.get("devices"),
                "usable_nvenc_devices": hw.get("usable_nvenc_devices"),
                "timing": tel.get("timing"),
                "resources": tel.get("resources"),
            }
        except Exception:
            pass

    return summary or None


def get_project_status(project_slug: str, *, chapter: Optional[str] = None) -> Dict[str, Any]:
    """Derives authoritative project/chapter status from state + artifact evidence and reports next_action."""
    if not project_slug:
        raise StorageFileNotFoundError("Project slug is required.")
    project_dir = resolve_existing_project_dir(project_slug)
    read_json(project_dir / "project.json")

    if chapter is not None:
        chap_dir = resolve_existing_chapter_dir(project_slug, chapter, project_dir=project_dir)
        chap_json_path = chap_dir / "chapter.json"
        if not chap_json_path.exists():
            raise StorageFileNotFoundError(f"chapter.json not found in chapter directory: {chap_dir}")

        meta = ChapterMetadata.model_validate(read_json(chap_json_path))
        resumable_stage = get_resumable_stage(chap_dir)

        stages_dict: Dict[str, Any] = {}
        for stage_enum, cp in meta.stages.items():
            s_name = stage_enum.value if hasattr(stage_enum, "value") else str(stage_enum)
            stages_dict[s_name] = {
                "status": cp.status.value if hasattr(cp.status, "value") else str(cp.status),
                "started_at": cp.started_at,
                "completed_at": cp.completed_at,
                "retry_count": cp.retry_count,
                "error_message": cp.error_message,
                "artifacts_count": len(cp.artifacts),
            }

        claimed = (
            meta.overall_status == CheckpointStatus.COMPLETED
            or (resumable_stage is None and meta.overall_status != CheckpointStatus.FAILED)
        )
        is_complete, artifact_error = verified_chapter_completion(project_dir, str(chapter), claimed)
        next_action, blocker = _derive_chapter_next_action(
            project_dir, chap_dir, str(chapter), resumable_stage, is_complete, artifact_error
        )
        out: Dict[str, Any] = {
            "artifact_valid": is_complete,
            "artifact_error": artifact_error,
            "project_slug": project_slug,
            "chapter_number": str(chapter),
            "overall_status": meta.overall_status.value,
            "current_stage": meta.current_stage.value if meta.current_stage else None,
            "resumable_stage": resumable_stage.value if resumable_stage else None,
            "is_complete": is_complete,
            "next_action": next_action,
            "blocker": blocker,
            "stages": stages_dict,
        }
        cloud_info = _summarize_cloud_state(project_dir, chapter_dir=chap_dir)
        if cloud_info is not None:
            out["cloud"] = cloud_info
        return out

    chapters_dir = project_dir / "chapters"
    chapter_summaries: List[Dict[str, Any]] = []
    if chapters_dir.exists():
        for c_name in sorted(os.listdir(chapters_dir)):
            chap_subdir = chapters_dir / c_name
            c_json = chap_subdir / "chapter.json"
            if chap_subdir.is_dir() and c_json.exists():
                try:
                    c_meta = ChapterMetadata.model_validate(read_json(c_json))
                    c_resumable = get_resumable_stage(chap_subdir)
                    claimed = c_meta.overall_status == CheckpointStatus.COMPLETED or c_resumable is None
                    complete, artifact_error = verified_chapter_completion(project_dir, c_name, claimed)
                    next_action, blocker = _derive_chapter_next_action(
                        project_dir, chap_subdir, c_name, c_resumable, complete, artifact_error
                    )
                    chapter_summaries.append({
                        "artifact_valid": complete,
                        "artifact_error": artifact_error,
                        "chapter_number": c_name,
                        "overall_status": c_meta.overall_status.value,
                        "current_stage": c_meta.current_stage.value if c_meta.current_stage else None,
                        "resumable_stage": c_resumable.value if c_resumable else None,
                        "is_complete": complete,
                        "next_action": next_action,
                        "blocker": blocker,
                    })
                except Exception as exc:
                    chapter_summaries.append({
                        "chapter_number": c_name,
                        "overall_status": "corrupt",
                        "current_stage": None,
                        "resumable_stage": None,
                        "is_complete": False,
                        "next_action": "repair_chapter_metadata",
                        "blocker": str(exc),
                    })

    out_proj: Dict[str, Any] = {
        "project_slug": project_slug,
        "total_chapters": len(chapter_summaries),
        "chapters": chapter_summaries,
    }
    cloud_proj = _summarize_cloud_state(project_dir)
    if cloud_proj is not None:
        out_proj["cloud"] = cloud_proj
    return out_proj


# ==============================================================================
# Ingest & Prepare
# ==============================================================================

def crawl_chapter_assets(
    project_slug: str,
    chapter_num: str,
    *,
    url: Optional[str] = None,
    import_dir: Optional[str] = None,
    max_panels: Optional[int] = None,
    verify: bool = False,
) -> Dict[str, Any]:
    """Crawls online pages or imports local raw images into chapters/<N>/raw."""
    if not project_slug:
        raise DomainValidationError("Project slug (--project) is required.")
    if chapter_num is None:
        raise DomainValidationError("Chapter number (--chapter) is required.")
    if not url and not import_dir:
        raise DomainValidationError("Either --url or --import-dir must be provided for crawl.")

    require_series_setup(project_slug)
    init_project_layout(project_slug, title=project_slug)
    chap_dir = init_chapter_layout(project_slug, chapter_num)
    raw_dir = chap_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    update_checkpoint(chap_dir, StageType.CRAWL, CheckpointStatus.RUNNING)

    imported_files: List[str] = []
    valid_exts = {".jpg", ".jpeg", ".png", ".webp"}
    if import_dir:
        import_path = Path(import_dir).resolve()
        if not import_path.exists():
            err_msg = f"Import directory does not exist: {import_path}"
            update_checkpoint(chap_dir, StageType.CRAWL, CheckpointStatus.FAILED, error=err_msg)
            raise DomainValidationError(err_msg)
        files = sorted(f for f in import_path.iterdir() if f.suffix.lower() in valid_exts)
        if max_panels:
            files = files[:max_panels]
        for idx, src_file in enumerate(files, start=1):
            dest_name = f"page_{idx:03d}{src_file.suffix.lower()}"
            shutil.copy2(src_file, raw_dir / dest_name)
            imported_files.append(dest_name)
    elif url:
        try:
            from src.services.crawler_service import crawler_service
            downloaded = asyncio.run(
                crawler_service.crawl_raw_pages(
                    url=url,
                    output_raw_dir=raw_dir,
                    max_panels=max_panels,
                    chapter_number=str(chapter_num),
                    project_slug=project_slug,
                    strict_verify=verify,
                )
            )
            imported_files.extend(downloaded)
        except CrawlerError as e:
            err_msg = f"[{e.code.value}] {e}"
            update_checkpoint(chap_dir, StageType.CRAWL, CheckpointStatus.FAILED, error=err_msg)
            raise DomainValidationError(f"Crawl failed: {err_msg}") from e
        except Exception as e:
            err_msg = str(e)
            update_checkpoint(chap_dir, StageType.CRAWL, CheckpointStatus.FAILED, error=err_msg)
            raise DomainValidationError(f"Crawl failed: {err_msg}") from e

    raw_count = len([f for f in raw_dir.glob("*") if f.suffix.lower() in valid_exts])
    if raw_count == 0:
        err_msg = "No raw images found"
        update_checkpoint(chap_dir, StageType.CRAWL, CheckpointStatus.FAILED, error=err_msg)
        raise DomainValidationError(f"Crawl failed: {err_msg}")

    report = None
    if verify:
        report = ImageVerifier.verify_chapter_raw_images(raw_dir, strict=True)
        if not report.is_valid:
            err_msg = f"Raw image verification failed: {'; '.join(report.errors)}"
            update_checkpoint(chap_dir, StageType.CRAWL, CheckpointStatus.FAILED, error=err_msg)
            raise DomainValidationError(err_msg)

    update_checkpoint(chap_dir, StageType.CRAWL, CheckpointStatus.COMPLETED, artifacts=imported_files)
    result: Dict[str, Any] = {
        "project_slug": project_slug,
        "chapter_number": str(chapter_num),
        "raw_images_count": raw_count,
        "source": url or str(import_dir),
        "status": "completed",
    }
    if report:
        result["verification"] = {
            "is_valid": report.is_valid,
            "total_pages": report.total_pages,
            "corrupt_pages": report.corrupt_pages,
            "duplicate_pages": report.duplicate_pages,
            "missing_indices": report.missing_indices,
        }
    return result


def segment_chapter_assets(
    project_slug: str,
    chapter_num: str,
    *,
    target_width: int = 1080,
    threshold_variance: float = 8.0,
    min_gutter_height: int = 15,
    margin_padding: int = 12,
    cell_size: int = 480,
    force: bool = False,
    segmenter_cls: Any = None,
    contact_sheet_cls: Any = None,
) -> Dict[str, Any]:
    """Segments raw pages into panels and 4x4 contact sheets."""
    if not project_slug:
        raise DomainValidationError("Project slug (--project) is required.")
    if chapter_num is None:
        raise DomainValidationError("Chapter number (--chapter) is required.")

    project_dir = resolve_existing_project_dir(project_slug, require_exists=False)
    if not project_dir.exists():
        init_project_layout(project_slug, title=project_slug)

    chap_dir = get_chapter_dir(project_slug, chapter_num)
    if not chap_dir.exists() or not (chap_dir / "chapter.json").exists():
        init_project_layout(project_slug, title=project_slug)
        chap_dir = init_chapter_layout(project_slug, chapter_num)

    manifest_path = chap_dir / "manifest.json"
    chap_json_path = chap_dir / "chapter.json"
    if manifest_path.exists() and not force:
        with contextlib.suppress(Exception):
            chap_data = read_json(chap_json_path)
            if chap_data.get("stages", {}).get("segment", {}).get("status") == CheckpointStatus.COMPLETED.value:
                manifest = PanelManifest.model_validate(read_json(manifest_path))
                return {
                    "project_slug": project_slug,
                    "chapter_number": str(chapter_num),
                    "total_raw_images": manifest.total_raw_images,
                    "total_panels": manifest.total_panels,
                    "total_contact_sheets": len(manifest.contact_sheets),
                    "manifest_path": str(manifest_path.relative_to(chap_dir)),
                    "status": "completed",
                    "cached": True,
                }

    raw_dir = chap_dir / "raw"
    if not raw_dir.exists():
        raise DomainValidationError(f"Raw directory does not exist: {raw_dir}. Run 'crawl' first.")

    valid_exts = {".jpg", ".jpeg", ".png", ".webp"}
    raw_files = [f for f in raw_dir.iterdir() if f.suffix.lower() in valid_exts and f.is_file()]
    if not raw_files:
        raise DomainValidationError(f"No raw images found in {raw_dir}. Run 'crawl' first.")

    update_checkpoint(chap_dir, StageType.SEGMENT, CheckpointStatus.RUNNING)
    try:
        panels_dir = chap_dir / "panels"
        panels_dir.mkdir(parents=True, exist_ok=True)
        contact_sheets_dir = chap_dir / "contact_sheets"
        contact_sheets_dir.mkdir(parents=True, exist_ok=True)

        if force:
            for d in (panels_dir, contact_sheets_dir):
                for f in d.glob("*"):
                    with contextlib.suppress(Exception):
                        f.unlink()
            clips_dir = chap_dir / "audio" / "clips"
            if clips_dir.exists():
                for f in clips_dir.glob("*.wav"):
                    with contextlib.suppress(Exception):
                        f.unlink()
            _reset_downstream_render_artifacts(
                chap_dir,
                (StageType.SCRIPT, StageType.TTS, StageType.RENDER, StageType.FINALIZE),
            )

        seg_factory = segmenter_cls or StreamingSegmenter
        cs_factory = contact_sheet_cls or ContactSheetGenerator

        segmenter = seg_factory(
            target_width=target_width,
            threshold_variance=threshold_variance,
            white_threshold=240.0,
            black_threshold=18.0,
            min_gutter_height=min_gutter_height,
            margin_padding=margin_padding,
            sufficiency_variance=100.0,
        )
        page_maps = segmenter.build_page_maps(raw_files)
        row_means, row_vars = segmenter.compute_1d_profiles(page_maps)
        intervals = segmenter.detect_cut_intervals(row_means, row_vars)
        refined_intervals = segmenter.refine_intervals(intervals, row_vars)
        panels = segmenter.extract_panels(page_maps, refined_intervals, panels_dir)

        contact_sheets = cs_factory(cell_size=cell_size, columns=4, rows=4).generate_sheets(
            panels, panels_dir, contact_sheets_dir
        )

        raw_images_info = [
            RawImageInfo(
                filename=p.filename,
                relative_path=f"raw/{p.filename}",
                width=p.orig_width,
                height=p.orig_height,
                sha256=calculate_sha256(p.path),
            )
            for p in page_maps
        ]

        source_url = None
        if chap_json_path.exists():
            with contextlib.suppress(Exception):
                source_url = read_json(chap_json_path).get("source_url")

        manifest = PanelManifest(
            schema_version="3.0.0",
            project_slug=project_slug,
            chapter_number=str(chapter_num),
            source_url=source_url,
            created_at=utc_iso_now(),
            total_raw_images=len(page_maps),
            total_panels=len(panels),
            raw_images=raw_images_info,
            panels=panels,
            contact_sheets=contact_sheets,
        )
        atomic_write_json(manifest_path, manifest)

        artifacts = (
            [p.relative_path for p in panels]
            + [s.relative_path for s in contact_sheets]
            + ["manifest.json"]
        )
        update_checkpoint(chap_dir, StageType.SEGMENT, CheckpointStatus.COMPLETED, artifacts=artifacts)

        return {
            "project_slug": project_slug,
            "chapter_number": str(chapter_num),
            "total_raw_images": len(page_maps),
            "total_panels": len(panels),
            "total_contact_sheets": len(contact_sheets),
            "manifest_path": str(manifest_path.relative_to(chap_dir)),
            "status": "completed",
        }
    except Exception as e:
        update_checkpoint(chap_dir, StageType.SEGMENT, CheckpointStatus.FAILED, error=str(e))
        raise


def repack_chapter_panels(
    project_slug: str,
    chapter_num: str,
    *,
    plan_path: Optional[str] = None,
    target_width: int = 1080,
    cell_size: int = 480,
    grid_cols: int = 4,
    grid_rows: int = 4,
) -> Dict[str, Any]:
    if not project_slug:
        raise DomainValidationError("Project slug (--project) is required.")
    if chapter_num is None:
        raise DomainValidationError("Chapter number (--chapter) is required.")
    chap_dir = get_chapter_dir(project_slug, chapter_num)
    if not chap_dir.exists():
        raise DomainValidationError(f"Chapter directory does not exist: {chap_dir}")
    target_plan = Path(plan_path) if plan_path else (chap_dir / "panel_plan.json")
    if not target_plan.exists():
        raise DomainValidationError(f"panel_plan.json not found at {target_plan}")
    try:
        plan_obj = PanelPlan.model_validate(read_json(target_plan))
    except Exception as exc:
        raise DomainValidationError(f"Invalid panel_plan.json: {exc}") from exc
    return apply_panel_plan(
        chapter_dir=chap_dir,
        plan=plan_obj,
        target_width=target_width,
        cell_size=cell_size,
        grid_cols=grid_cols,
        grid_rows=grid_rows,
    )


def inspect_flagged_panels(project_slug: str, chapter_num: str, panel_ids: List[str]) -> Dict[str, Any]:
    if not project_slug or chapter_num is None or not panel_ids:
        raise DomainValidationError("--project, --chapter, and --panels (comma-separated) are required.")
    chap_dir = get_chapter_dir(project_slug, chapter_num)
    return inspect_panels_with_candidates(chap_dir, panel_ids)


def decide_chapter_panels(
    project_slug: str,
    chapter_num: str,
    *,
    workers: int = 2,
    refresh: bool = False,
) -> Dict[str, Any]:
    if not project_slug or chapter_num is None:
        raise DomainValidationError("Both --project and --chapter are required.")
    from src.ingest.decision import run_decision_pipeline
    chap_dir = get_chapter_dir(project_slug, chapter_num)
    return run_decision_pipeline(
        chap_dir,
        max_workers=workers,
        refresh=refresh,
    )


def prepare_chapter(
    project_slug: str,
    chapter_num: str,
    *,
    url: Optional[str] = None,
    import_dir: Optional[str] = None,
    max_panels: Optional[int] = None,
    verify: bool = False,
    target_width: int = 1080,
    force: bool = False,
    workers: int = 2,
) -> Dict[str, Any]:
    """
    Canonical Stage 2 (Prepare): crawl/import -> segment -> decide -> inspect summary.
    Never authors creative narration; returns manifest & contact sheets ready for Agent authoring.
    """
    setup = require_series_setup(project_slug)
    named = extract_named_settings(setup)
    source_cfg = named.get("source") or {}
    effective_url = url
    effective_import = import_dir
    if not effective_url and not effective_import:
        src_val = source_cfg.get("source") or source_cfg.get("url") or source_cfg.get("dir")
        if isinstance(src_val, str) and src_val.strip():
            if src_val.startswith(("http://", "https://")):
                effective_url = src_val
            else:
                effective_import = src_val

    chap_dir = get_chapter_dir(project_slug, chapter_num)
    raw_dir = chap_dir / "raw"
    has_raw = raw_dir.is_dir() and any(raw_dir.iterdir())
    if force or not has_raw or url or import_dir:
        crawl_res = crawl_chapter_assets(
            project_slug,
            str(chapter_num),
            url=effective_url,
            import_dir=effective_import,
            max_panels=max_panels,
            verify=verify,
        )
    else:
        crawl_res = {"status": "cached", "raw_images_count": len(list(raw_dir.iterdir()))}

    seg_res = segment_chapter_assets(
        project_slug,
        str(chapter_num),
        target_width=target_width,
        force=force,
    )
    decide_res = decide_chapter_panels(
        project_slug, str(chapter_num), workers=workers, refresh=force
    )
    inspect_res = inspect_project_or_chapter(project_slug, str(chapter_num))
    update_checkpoint(
        chap_dir,
        StageType.INGEST,
        CheckpointStatus.COMPLETED,
        artifacts=["manifest.json"],
    )
    return {
        "project_slug": project_slug,
        "chapter_number": str(chapter_num),
        "crawl": crawl_res,
        "segment": seg_res,
        "decide": decide_res,
        "inspect": inspect_res,
        "next_action": "author_script",
    }


# ==============================================================================
# TTS / Render / Preview / Approve / Compile / Batch / Produce
# ==============================================================================

def resolve_segment_voice(
    seg: ScriptSegment,
    approved_voice: str,
) -> Tuple[str, str]:
    """Enforces canonical single-narrator voice across all segments."""
    if seg.is_manual_voice() and seg.voice_id and resolve_voice_id(seg.voice_id) != approved_voice:
        raise DomainValidationError(
            f"Segment '{seg.seq_id}' voice '{seg.voice_id}' conflicts with confirmed single narrator '{approved_voice}'."
        )
    return approved_voice, "approved"


def synthesize_chapter_tts(
    project_slug: str,
    chapter_num: str,
    *,
    force: bool = False,
    no_cache: bool = False,
    engine: str = "neural",
    workers: Optional[int] = None,
    kaggle: bool = False,
    mock: bool = False,
    force_reload: bool = False,
    tts_engine_factory: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """Synthesizes speech audio for chapter script segments using local VieNeu-TTS or Kaggle."""
    if not project_slug:
        raise DomainValidationError("Project slug (--project) is required.")
    if chapter_num is None:
        raise DomainValidationError("Chapter number (--chapter) is required.")

    project_dir = resolve_existing_project_dir(project_slug)
    chap_dir = resolve_existing_chapter_dir(project_slug, chapter_num, project_dir=project_dir)

    script_path = chap_dir / "script.json"
    if not script_path.exists():
        raise StorageFileNotFoundError(f"Script file script.json not found in chapter directory: {chap_dir}")

    force_resynth = bool(force or no_cache)

    if kaggle:
        logger.info(f"Offloading TTS for {project_slug} ch.{chapter_num} to Kaggle Cloud GPU...")
        from src.cloud.kaggle_service import KaggleCloudService
        service = KaggleCloudService()
        if not service.is_authenticated:
            raise DomainValidationError(
                "Kaggle API authentication failed. Ensure KAGGLE_API_TOKEN is configured in ~/.kaggle/access_token."
            )
        update_checkpoint(chap_dir, StageType.TTS, CheckpointStatus.RUNNING)
        cloud_res = service.dispatch_chapter_job(
            project_slug=project_slug,
            chapter_number=str(chapter_num),
            gpu=True,
            wait=True,
            job_type="tts",
        )
        update_checkpoint(chap_dir, StageType.TTS, CheckpointStatus.COMPLETED)
        return {
            "status": "success",
            "command": "tts",
            "project": project_slug,
            "chapter": str(chapter_num),
            "provider": "kaggle",
            "mode": "cloud_gpu",
            "audio_clips_dir": str(chap_dir / "audio" / "clips"),
            "telemetry": cloud_res.get("telemetry", {}),
        }

    update_checkpoint(chap_dir, StageType.TTS, CheckpointStatus.RUNNING)
    try:
        script_obj = RecapScript.model_validate(read_json(script_path))
        validate_narration_evidence(chap_dir, script_obj)

        audio_clips_dir = chap_dir / "audio" / "clips"
        audio_clips_dir.mkdir(parents=True, exist_ok=True)
        if no_cache and audio_clips_dir.exists():
            for old_clip in audio_clips_dir.glob("*.wav"):
                old_clip.unlink(missing_ok=True)

        approval = require_voice_approval(project_dir)
        default_voice = resolve_voice_id(approval.primary_voice)
        default_speed = approval.speed

        if mock or engine != "neural":
            raise DomainValidationError("Production TTS requires the real neural engine")

        factory = tts_engine_factory or get_tts_engine
        tts_engine = factory(force_reload=force_reload)
        if getattr(tts_engine, "mode", None) != "neural":
            raise DomainValidationError("Neural engine unavailable; refusing fallback audio")

        synthesized_count = 0
        cached_count = 0
        artifacts: List[str] = []
        total_duration = 0.0
        max_workers = workers or 2
        uncached_tasks = []

        for seg in script_obj.segments:
            seg_voice, v_source = resolve_segment_voice(
                seg=seg,
                approved_voice=default_voice,
            )

            seg_audio_file = audio_clips_dir / f"audio_{chapter_num}_{seg.seq_id}.wav"
            engine_mode = getattr(tts_engine, "mode", "neural")
            calculated_fingerprint = calculate_utterance_fingerprint(
                text=seg.narrator_text,
                voice=seg_voice,
                speed=default_speed,
                emotion=seg.emotion,
                emotion_cue=seg.emotion_cue,
                engine_mode=engine_mode,
            )

            can_resume = False
            if not force_resynth and seg_audio_file.exists() and seg_audio_file.stat().st_size > 44:
                if (
                    seg.status == "synthesized"
                    and seg.checksum is not None
                    and seg.fingerprint is not None
                    and seg.fingerprint == calculated_fingerprint
                    and (seg.synthesized_voice == seg_voice or seg.voice_id == seg_voice)
                ):
                    try:
                        dur = get_wav_duration(seg_audio_file)
                        if dur > 0 and calculate_sha256(seg_audio_file) == seg.checksum:
                            can_resume = True
                            seg.duration = dur
                            seg.synthesized_voice = seg_voice
                            seg.audio_path = str(seg_audio_file.relative_to(chap_dir))
                    except Exception:
                        can_resume = False

            if can_resume:
                cached_count += 1
                total_duration += seg.duration or 0.0
                if seg.audio_path:
                    artifacts.append(seg.audio_path)
            else:
                uncached_tasks.append((seg, seg_voice, v_source, default_speed, seg_audio_file, calculated_fingerprint))

        if uncached_tasks:
            def _synth_seg(task_item):
                s, v, v_s, sp, s_file, c_fp = task_item
                _, checksum, _ = tts_engine.synthesize_utterance(
                    text=s.narrator_text,
                    voice=v,
                    output_path=s_file,
                    speed=sp,
                    emotion=s.emotion,
                    emotion_cue=s.emotion_cue,
                    force=force_resynth,
                )
                dur = get_wav_duration(s_file)
                s.synthesized_voice = v
                s.voice_id = v
                s.voice_source = "manual" if v_s == "manual" else "tts"
                s.audio_path = str(s_file.relative_to(chap_dir))
                s.duration = dur
                s.checksum = checksum
                s.fingerprint = c_fp
                s.status = "synthesized"
                return s, dur

            if max_workers > 1 and len(uncached_tasks) > 1:
                with ThreadPoolExecutor(max_workers=max_workers) as pool:
                    for s, dur in pool.map(_synth_seg, uncached_tasks):
                        synthesized_count += 1
                        total_duration += dur
                        if s.audio_path:
                            artifacts.append(s.audio_path)
                        atomic_write_json(script_path, script_obj)
            else:
                for task_item in uncached_tasks:
                    s, dur = _synth_seg(task_item)
                    synthesized_count += 1
                    total_duration += dur
                    if s.audio_path:
                        artifacts.append(s.audio_path)
                    atomic_write_json(script_path, script_obj)

        script_obj.total_duration_seconds = round(total_duration, 3)
        atomic_write_json(script_path, script_obj)

        if synthesized_count > 0 or force_resynth:
            _reset_downstream_render_artifacts(chap_dir, (StageType.RENDER, StageType.FINALIZE))

        if not script_obj.segments:
            raise DomainValidationError("Empty script cannot complete audio stage")
        for segment in script_obj.segments:
            if not segment.audio_path:
                raise DomainValidationError(f"Missing audio_path for segment {segment.seq_id}")
            get_wav_duration(chap_dir / segment.audio_path)

        update_checkpoint(chap_dir, StageType.TTS, CheckpointStatus.COMPLETED, artifacts=artifacts)
        return {
            "project_slug": project_slug,
            "chapter_number": str(chapter_num),
            "segments_synthesized": synthesized_count,
            "segments_cached": cached_count,
            "total_duration_seconds": round(total_duration, 3),
            "default_voice": default_voice,
            "artifacts": artifacts,
            "engine_mode": tts_engine.mode,
            "init_duration_seconds": tts_engine.init_duration_seconds,
            "total_inference_seconds": round(tts_engine.total_inference_seconds, 3),
            "utterance_count": tts_engine.utterance_count,
        }
    except Exception as e:
        update_checkpoint(chap_dir, StageType.TTS, CheckpointStatus.FAILED, error=str(e))
        raise


def render_chapter_workflow(
    project_slug: str,
    chapter_num: str,
    *,
    quality: str = "final",
    encoder: Optional[str] = None,
    hw_accel: Optional[str] = None,
    workers: Optional[int] = None,
    kaggle: bool = False,
    cleanup: bool = False,
) -> Dict[str, Any]:
    """Renders a chapter locally or on Kaggle using confirmed project settings."""
    if encoder is None and hw_accel:
        encoder = {"none": "libx264", "auto": "auto", "videotoolbox": "h264_videotoolbox"}[hw_accel]
    render_workers = workers or 4
    options = dict(
        quality=quality,
        encoder=encoder,
        render_workers=render_workers,
    )
    if kaggle:
        expected_quality = "preview" if str(chapter_num) == "1" else "final"
        if quality != expected_quality:
            raise DomainValidationError(
                "Cloud renders chapter 1 preview and later chapters final. Promote reviewed chapter 1 locally."
            )
        from src.cloud.kaggle_service import KaggleCloudService
        result = KaggleCloudService().dispatch_chapter_job(
            project_slug,
            str(chapter_num),
            job_type="render",
            wait=True,
            encoder_preference=options["encoder"] or "auto",
            render_workers=render_workers,
        )
    else:
        result = render_chapter(get_project_dir(project_slug), str(chapter_num), **options)

    assert_file_nonempty(result["video_path"])
    assert_file_nonempty(result["subtitle_path"])

    if quality == "final":
        if "export_paths" not in result:
            staged = stage_chapter_export(project_slug, chapter_num, result["video_path"], result["subtitle_path"])
            result["export_paths"] = {k: str(v) if v else None for k, v in staged.items()}
        if cleanup:
            result["cleanup"] = clean_project_chapters(
                project_slug, chapter=chapter_num, dry_run=False, confirm_project=project_slug
            )
    else:
        result["review_pack"] = create_review_pack(project_slug, str(chapter_num), fmt="all")
    return result


def preview_chapter(
    project_slug: str,
    chapter_num: str = "1",
    *,
    encoder: Optional[str] = None,
    render_workers: Optional[int] = None,
    tts_workers: Optional[int] = None,
    kaggle: bool = False,
    force: bool = False,
) -> Dict[str, Any]:
    """Canonical Stage 4 (Preview): validate script -> TTS -> preview render -> review pack."""
    if str(chapter_num) != "1":
        raise DomainValidationError("Preview workflow is dedicated to Chapter 1")
    require_series_setup(project_slug)
    chap_dir = resolve_existing_chapter_dir(project_slug, str(chapter_num))
    if not (chap_dir / "script.json").is_file():
        raise DomainValidationError(
            "Missing script.json for Chapter 1; inspect contact sheets and author narration first."
        )
    validate_project_or_chapter(project_slug, chapter=str(chapter_num))
    tts_res = synthesize_chapter_tts(
        project_slug,
        str(chapter_num),
        force=force,
        workers=tts_workers,
        kaggle=kaggle,
    )
    render_res = render_chapter_workflow(
        project_slug,
        str(chapter_num),
        quality="preview",
        encoder=encoder,
        workers=render_workers,
        kaggle=kaggle,
    )
    return {
        "project_slug": project_slug,
        "chapter_number": str(chapter_num),
        "tts": tts_res,
        "render": render_res,
        "review_pack": render_res.get("review_pack"),
        "next_action": "review_approve_chapter_1",
    }


def approve_chapter_preview(
    project_slug: str,
    *,
    reviewer: str,
    config_fingerprint: str,
    artifact_hash: str,
) -> Dict[str, Any]:
    """Canonical Stage 5 (Review approval) bound to exact config and preview artifact hashes."""
    require_series_setup(project_slug)
    return approve_preview(
        get_project_dir(project_slug), reviewer, config_fingerprint, artifact_hash
    ).model_dump(mode="json")


def batch_chapters(
    project_slug: str,
    *,
    chapters: Optional[str] = None,
    retry_chapter: Optional[str] = None,
    compile_after: bool = False,
    output: Optional[str] = None,
    extract_audio: bool = True,
    force: bool = False,
    encoder: Optional[str] = None,
    hw_accel: Optional[str] = None,
    tts_workers: int = 2,
    render_workers: int = 2,
    cleanup: bool = False,
    kaggle: bool = False,
) -> Dict[str, Any]:
    """Sequential multi-chapter batch processing with caching and failure isolation."""
    if not project_slug:
        raise DomainValidationError("Project slug (--project) is required for batch processing.")

    eff_dir = resolve_existing_project_dir(project_slug, require_exists=False)
    require_series_setup(project_slug, project_dir=eff_dir)
    if not eff_dir.exists():
        raise StorageFileNotFoundError(f"Project '{project_slug}' directory not found at {eff_dir}")
    project_dir = eff_dir

    if not (project_dir / "project.json").exists():
        raise StorageFileNotFoundError(f"Project config 'project.json' not found in {project_dir}")

    require_voice_approval(project_dir)
    require_preview_approval(project_dir)

    if retry_chapter is not None:
        target_chapters = [str(retry_chapter)]
    elif chapters:
        target_chapters = parse_chapter_list(chapters)
    else:
        target_chapters = discover_project_chapters(project_dir)

    if not target_chapters:
        raise DomainValidationError(f"No chapters found or specified to process for project '{project_slug}'.")

    output_dir = get_output_dir(project_slug)
    output_dir.mkdir(parents=True, exist_ok=True)

    completed_chaps: List[str] = []
    cached_chaps: List[str] = []
    failed_chaps: List[Dict[str, Any]] = []
    chapter_results: List[Dict[str, Any]] = []

    for chap_num in target_chapters:
        t0 = time.monotonic()
        chap_dir = resolve_existing_chapter_dir(project_slug, chap_num, project_dir=project_dir, require_exists=False)

        if not force and (retry_chapter is None or str(chap_num) != str(retry_chapter)):
            chap_json = chap_dir / "chapter.json"
            if chap_json.exists():
                with contextlib.suppress(Exception):
                    meta = load_chapter_metadata(chap_dir)
                    if meta.overall_status == CheckpointStatus.COMPLETED:
                        validate_existing_chapter(project_dir, chap_num)
                        cached_chaps.append(str(chap_num))
                        vid_path, srt_path = _resolve_chapter_final_paths(chap_dir, chap_num)

                        staged_padded = output_dir / format_chapter_filename(chap_num, ext="mp4", padded=True)
                        if vid_path.exists() and not staged_padded.exists():
                            stage_chapter_export(project_slug, chap_num, video_path=vid_path, subtitle_path=srt_path)

                        target_vid = staged_padded if staged_padded.exists() else vid_path
                        chapter_results.append({
                            "chapter": str(chap_num),
                            "status": "cached",
                            "video_path": str(target_vid),
                            "duration_seconds": round(time.monotonic() - t0, 3),
                        })
                        continue

        active_stage: Optional[StageType] = None
        try:
            logger.info(f"--- [Batch] Starting Chapter {chap_num} ({project_slug}) ---")
            if not chap_dir.exists():
                raise StorageFileNotFoundError(f"Chapter directory not found at {chap_dir}")
            if not (chap_dir / "script.json").exists():
                raise StorageFileNotFoundError(f"script.json not found in chapter directory: {chap_dir}")

            active_stage = StageType.AGENT
            validate_project_or_chapter(project_slug, chapter=chap_num, strict=False)

            if str(chap_num) == "1":
                active_stage = StageType.RENDER
                render_res = render_chapter_workflow(project_slug, "1", quality="final")
            elif kaggle:
                logger.info(f"[Batch] Offloading Chapter {chap_num} to Kaggle Cloud GPU (TTS + Hardware NVENC)...")
                active_stage = StageType.RENDER
                from src.cloud.kaggle_service import KaggleCloudService
                service = KaggleCloudService()
                if not service.is_authenticated:
                    raise DomainValidationError(
                        "Kaggle API authentication failed. Ensure KAGGLE_API_TOKEN is configured in ~/.kaggle/access_token."
                    )
                cloud_res = service.dispatch_chapter_job(
                    project_slug=project_slug,
                    chapter_number=str(chap_num),
                    gpu=True,
                    wait=True,
                    job_type="produce",
                    render_workers=render_workers,
                    encoder_preference=encoder or "auto",
                )
                render_res = {
                    "video_path": cloud_res.get("final_video"),
                    "subtitle_path": cloud_res.get("subtitles"),
                    "export_paths": cloud_res.get("export_paths", {}),
                }
            else:
                active_stage = StageType.AUDIO
                synthesize_chapter_tts(
                    project_slug,
                    chap_num,
                    workers=tts_workers,
                    force=force,
                )
                c_meta = load_chapter_metadata(chap_dir)
                audio_cp = c_meta.stages.get(StageType.AUDIO)
                if not audio_cp or audio_cp.status != CheckpointStatus.COMPLETED:
                    update_checkpoint(chap_dir, StageType.AUDIO, CheckpointStatus.COMPLETED)

                active_stage = StageType.RENDER
                render_res = render_chapter_workflow(
                    project_slug,
                    chap_num,
                    quality="final",
                    encoder=encoder,
                    hw_accel=hw_accel,
                    workers=render_workers,
                )

            active_stage = StageType.EXPORT
            src_video = Path(render_res["video_path"]) if render_res.get("video_path") else (chap_dir / "video" / f"chapter_{chap_num}_final.mp4")
            src_srt = Path(render_res["subtitle_path"]) if render_res.get("subtitle_path") else (chap_dir / "subtitles" / f"chapter_{chap_num}.srt")

            staged = render_res.get("export_paths") or stage_chapter_export(
                project_slug=project_slug, chapter_num=chap_num, video_path=src_video, subtitle_path=src_srt
            )
            staged_video = staged.get("video_padded") or staged.get("video")
            dest_video = Path(staged_video) if staged_video else src_video
            if cleanup:
                clean_project_chapters(project_slug, chapter=chap_num, dry_run=False, confirm_project=project_slug)

            c_meta = load_chapter_metadata(chap_dir)
            exp_cp = c_meta.stages.get(StageType.EXPORT)
            if not exp_cp or exp_cp.status != CheckpointStatus.COMPLETED:
                update_checkpoint(
                    chap_dir,
                    StageType.EXPORT,
                    CheckpointStatus.COMPLETED,
                    artifacts=[f"output/{project_slug}/{dest_video.name}"],
                )

            completed_chaps.append(str(chap_num))
            chapter_results.append({
                "chapter": str(chap_num),
                "status": "completed",
                "video_path": str(dest_video if dest_video.exists() else src_video),
                "duration_seconds": round(time.monotonic() - t0, 3),
            })
            logger.info(f"--- [Batch] Chapter {chap_num} Completed Successfully ---")

        except Exception as chap_err:
            logger.error(f"[Batch] Chapter {chap_num} execution failed: {chap_err}")
            if chap_dir.exists() and (chap_dir / "chapter.json").exists():
                with contextlib.suppress(Exception):
                    c_meta = load_chapter_metadata(chap_dir)
                    target_stg = active_stage or c_meta.current_stage
                    stg_cp = c_meta.stages.get(target_stg)
                    if not stg_cp or stg_cp.status != CheckpointStatus.RUNNING:
                        update_checkpoint(chap_dir, target_stg, CheckpointStatus.RUNNING)
                    update_checkpoint(chap_dir, stage=target_stg, status=CheckpointStatus.FAILED, error=str(chap_err))
            failed_chaps.append({"chapter": str(chap_num), "error": str(chap_err), "duration_seconds": round(time.monotonic() - t0, 3)})
            chapter_results.append({"chapter": str(chap_num), "status": "failed", "error": str(chap_err), "duration_seconds": round(time.monotonic() - t0, 3)})

    compile_result = None
    if compile_after:
        active_chaps = sorted(
            list(set(completed_chaps + cached_chaps)),
            key=lambda x: int(x) if x.isdigit() else x,
        )
        if active_chaps:
            target_out = output or str(output_dir / f"{project_slug}_full_recap.mp4")
            try:
                compile_result = compile_project_chapters(
                    project_slug,
                    chapters=",".join(active_chaps),
                    output=target_out,
                    extract_audio=extract_audio,
                )
            except Exception as comp_err:
                logger.error(f"[Batch] Compilation failed: {comp_err}")
                compile_result = {"error": str(comp_err)}

    all_succeeded = len(failed_chaps) == 0 and not (compile_result and compile_result.get("error"))
    return {
        "success": all_succeeded,
        "project_slug": project_slug,
        "total_chapters": len(target_chapters),
        "completed": completed_chaps,
        "cached": cached_chaps,
        "failed": failed_chaps,
        "compile_output": compile_result,
        "chapters_detail": chapter_results,
    }


def produce_chapters(
    project_slug: str,
    *,
    chapters: Optional[str] = None,
    retry_chapter: Optional[str] = None,
    compile_after: bool = False,
    cleanup: bool = False,
    kaggle: Optional[bool] = None,
    encoder: Optional[str] = None,
    render_workers: int = 2,
    tts_workers: int = 2,
    force: bool = False,
) -> Dict[str, Any]:
    """
    Canonical Stage 6 (Produce): routes single/multi-chapter production via the confirmed
    compute backend (local or Kaggle Dual-T4) after verifying Chapter 1 approval and scripts.
    """
    setup = require_series_setup(project_slug)
    project_dir = resolve_existing_project_dir(project_slug)
    require_voice_approval(project_dir)
    require_preview_approval(project_dir)

    if retry_chapter is not None:
        target_chapters = [str(retry_chapter)]
    elif chapters:
        target_chapters = parse_chapter_list(chapters)
    else:
        target_chapters = discover_project_chapters(project_dir)

    if not target_chapters:
        raise DomainValidationError(f"No chapters found or specified to produce for '{project_slug}'.")

    # Fail fast with actionable missing-script / unresolved-review info before starting compute.
    for ch in target_chapters:
        chap_dir = resolve_existing_chapter_dir(project_slug, ch, project_dir=project_dir)
        if not (chap_dir / "script.json").is_file():
            raise DomainValidationError(
                f"Chapter {ch} is missing script.json; agent authoring is required before produce.",
                data={"next_action": "author_script", "chapter": str(ch)},
            )
        validate_narration_evidence(chap_dir, load_recap_script(chap_dir), load_manifest(chap_dir))

    named = extract_named_settings(setup)
    use_kaggle = (str(named.get("compute", "local")).strip().lower() == "kaggle") if kaggle is None else bool(kaggle)

    if use_kaggle:
        from src.cloud.kaggle_service import KaggleCloudService
        service = KaggleCloudService()
        if not service.is_authenticated:
            raise DomainValidationError("Kaggle API authentication failed.")
        cloud_res = service.dispatch_series_job(
            project_slug=project_slug,
            chapters=target_chapters,
            gpu=True,
            wait=True,
            compile_feature=compile_after,
            render_workers=render_workers,
            encoder_preference=encoder or "auto",
        )
        return {
            "project_slug": project_slug,
            "compute": "kaggle",
            "chapters": target_chapters,
            "result": cloud_res,
        }

    return batch_chapters(
        project_slug,
        chapters=",".join(target_chapters),
        retry_chapter=retry_chapter,
        compile_after=compile_after,
        cleanup=cleanup,
        force=force,
        encoder=encoder,
        render_workers=render_workers,
        tts_workers=tts_workers,
        kaggle=False,
    )


def generate_chapter_visual_plan(project_slug: str, chapter_num: str, *, regenerate: bool = False) -> Dict[str, Any]:
    """Generates or explicitly regenerates a canonical VisualPlan from evidence."""
    root = resolve_existing_project_dir(project_slug)
    chapter = resolve_existing_chapter_dir(project_slug, chapter_num, project_dir=root)
    settings = read_json(root / "project.json")["render_settings"]
    kwargs = dict(
        target_width=settings["target_width"],
        target_height=settings["target_height"],
        fps=settings["fps"],
        motion_enabled=settings["render_pan_mode"] == "animation",
    )
    manifest, script = load_manifest(chapter), load_recap_script(chapter)
    path = chapter / "visual_plan.json"
    if regenerate:
        plan = generate_visual_plan(chapter, manifest, script, **kwargs)
        if path.exists():
            path.replace(chapter / f"visual_plan.stale.{calculate_sha256(path)[:16]}.json")
    else:
        plan = resolve_visual_plan(chapter, manifest, script, **kwargs)
    plan.save(path)
    return {
        "path": str(path),
        "input_fingerprint": plan.input_fingerprint,
        "shots": len(plan.shots),
        "duration": plan.total_duration_sec,
    }
