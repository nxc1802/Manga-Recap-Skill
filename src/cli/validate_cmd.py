"""
src/cli/validate_cmd.py
Subcommand handler: manga-recap validate
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any, Dict, List

from pydantic import ValidationError

from src.cli.exit_codes import CliValidationError
from src.core.schemas import (
    CharacterDictionary,
    ChapterMetadata,
    CheckpointStatus,
    HumanReviewStatus,
    PanelManifest,
    PanelStatus,
    ProjectConfig,
    RecapScript,
    StageType,
)
from src.core.storage import (
    StorageCorruptError,
    StorageFileNotFoundError,
    calculate_sha256,
    format_chapter_filename,
    get_chapter_dir,
    get_output_dir,
    get_project_dir,
    read_json,
    update_checkpoint,
    verify_checksum,
)
from src.core.utils import get_project_data_dir
from src.tts.engine import get_wav_duration_seconds


def handle_validate(args: argparse.Namespace) -> Dict[str, Any]:
    """Validates schema conformance and asset presence for a project/chapter."""
    project_slug = getattr(args, "project", "")
    if not project_slug:
        raise CliValidationError("Project slug is required.")

    # 1. Resolve project directory
    project_dir = get_project_dir(project_slug)
    if not project_dir.exists():
        fallback_dir = get_project_data_dir(project_slug)
        if fallback_dir.exists():
            project_dir = fallback_dir
        else:
            raise StorageFileNotFoundError(
                f"Project '{project_slug}' directory not found at {project_dir}"
            )

    strict = getattr(args, "strict", False)
    chapter_arg = getattr(args, "chapter", None)
    check_pacing = getattr(args, "pacing", False)

    errors: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []
    pacing_reports: Dict[str, Any] = {}
    total_checks = 0
    passed_checks = 0

    def check(condition: bool, target: str, message: str, is_warning: bool = False):
        nonlocal total_checks, passed_checks
        total_checks += 1
        if condition:
            passed_checks += 1
        else:
            item = {"target": target, "message": message}
            if is_warning:
                warnings.append(item)
            else:
                errors.append(item)

    # 2. Validate project.json
    project_json_path = project_dir / "project.json"
    proj_config: Optional[ProjectConfig] = None
    if project_json_path.exists():
        try:
            p_data = read_json(project_json_path)
            proj_config = ProjectConfig.model_validate(p_data)
            check(True, "project.json", "Valid ProjectConfig schema")
        except (ValidationError, StorageCorruptError) as e:
            check(False, "project.json", f"Invalid project.json schema: {e}")
    else:
        check(False, "project.json", "Missing project.json in project root")

    # 3. Validate characters.json
    characters_path = project_dir / "characters.json"
    if characters_path.exists():
        try:
            c_data = read_json(characters_path)
            CharacterDictionary.model_validate(c_data)
            check(True, "characters.json", "Valid CharacterDictionary schema")
        except (ValidationError, StorageCorruptError) as e:
            check(False, "characters.json", f"Invalid characters.json schema: {e}")

    # 4. Validate story_summary.md
    summary_path = project_dir / "story_summary.md"
    if summary_path.exists():
        content = summary_path.read_text(encoding="utf-8").strip()
        check(len(content) > 0, "story_summary.md", "story_summary.md is empty", is_warning=True)
    else:
        check(False, "story_summary.md", "Missing story_summary.md", is_warning=True)

    # 5. Resolve chapters to validate
    chapters_to_check: List[str] = []
    if chapter_arg is not None:
        chapters_to_check.append(str(chapter_arg))
    else:
        chapters_dir = project_dir / "chapters"
        if chapters_dir.exists():
            for c_name in sorted(os.listdir(chapters_dir)):
                if (chapters_dir / c_name).is_dir():
                    chapters_to_check.append(c_name)

    # 6. Validate each chapter
    for chap_num in chapters_to_check:
        chap_dir = get_chapter_dir(project_slug, chap_num)
        if not chap_dir.exists():
            fallback_chap = project_dir / "chapters" / str(chap_num)
            if fallback_chap.exists():
                chap_dir = fallback_chap
            else:
                check(False, f"chapter_{chap_num}", f"Chapter directory does not exist: {chap_dir}")
                continue

        # chapter.json
        chap_json_path = chap_dir / "chapter.json"
        chap_meta: Any = None
        if chap_json_path.exists():
            try:
                ch_data = read_json(chap_json_path)
                chap_meta = ChapterMetadata.model_validate(ch_data)
                check(True, f"chapter_{chap_num}/chapter.json", "Valid ChapterMetadata schema")

                # Validate independent review statuses (technical_status vs human_review_status)
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

        # manifest.json & physical panels
        manifest_path = chap_dir / "manifest.json"
        manifest_panel_ids: set[str] = set()
        canonical_manifest_pids: set[str] = set()
        panel_lookup: Dict[str, str] = {}
        panel_disk_paths: Dict[str, Path] = {}
        if manifest_path.exists():
            try:
                m_data = read_json(manifest_path)
                manifest_obj = PanelManifest.model_validate(m_data)
                check(True, f"chapter_{chap_num}/manifest.json", "Valid PanelManifest schema")
                
                # Verify physical presence of panel images
                panels_dir = chap_dir / "panels"
                for panel in manifest_obj.panels:
                    p_status = getattr(panel, "status", None)
                    is_active = p_status not in {"skip", PanelStatus.SKIP} if p_status else True
                    if is_active:
                        canonical_manifest_pids.add(panel.panel_id)

                    panel_lookup[panel.panel_id] = panel.panel_id
                    manifest_panel_ids.add(panel.panel_id)
                    if getattr(panel, "filename", None):
                        manifest_panel_ids.add(panel.filename)
                        panel_lookup[panel.filename] = panel.panel_id
                        panel_lookup[Path(panel.filename).name] = panel.panel_id
                    if getattr(panel, "relative_path", None):
                        manifest_panel_ids.add(panel.relative_path)
                        manifest_panel_ids.add(Path(panel.relative_path).name)
                        manifest_panel_ids.add(Path(panel.relative_path).stem)
                        panel_lookup[panel.relative_path] = panel.panel_id
                        panel_lookup[Path(panel.relative_path).name] = panel.panel_id
                        panel_lookup[Path(panel.relative_path).stem] = panel.panel_id
                    # Check relative path or direct panel_id filename
                    candidate1 = chap_dir / panel.relative_path
                    candidate2 = panels_dir / panel.panel_id
                    candidate3 = panels_dir / Path(panel.relative_path).name
                    candidate4 = panels_dir / panel.filename if getattr(panel, "filename", None) else candidate2
                    found = False
                    for cand in (candidate1, candidate2, candidate3, candidate4):
                        if cand.exists() and cand.stat().st_size > 0:
                            found = True
                            panel_disk_paths[panel.panel_id] = cand
                            break
                    check(
                        found,
                        f"panel:{panel.panel_id}",
                        f"Referenced panel image not found on disk: {panel.relative_path}"
                    )
            except (ValidationError, StorageCorruptError) as e:
                check(False, f"chapter_{chap_num}/manifest.json", f"Invalid manifest.json schema: {e}")

        # script.json & audio files
        script_path = chap_dir / "script.json"
        script_errors_before = len(errors)
        if script_path.exists():
            try:
                s_data = read_json(script_path)
                script_obj = RecapScript.model_validate(s_data)
                check(True, f"chapter_{chap_num}/script.json", "Valid RecapScript schema")

                # Validate N:M panel mapping, opaque seq_id, and synthesized audio
                panels_dir = chap_dir / "panels"
                for seg in script_obj.segments:
                    # Validate opaque seq_id (e.g. '001', '002_1', 'scene_01')
                    check(
                        bool(seg.seq_id and str(seg.seq_id).strip()),
                        f"segment:{seg.seq_id}:seq_id",
                        "Segment seq_id must be a non-empty string",
                    )

                    # Fail-fast: check panel references in manifest and on disk when manifest is present
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

                            # Verify physical existence on disk (fail-fast)
                            disk_found = False
                            if canon_pid and canon_pid in panel_disk_paths:
                                disk_p = panel_disk_paths[canon_pid]
                                if disk_p.exists() and disk_p.stat().st_size > 0:
                                    disk_found = True
                            if not disk_found:
                                for candidate in [
                                    panels_dir / pid,
                                    panels_dir / Path(pid).name,
                                    chap_dir / pid,
                                    panels_dir / f"{pid}.jpg",
                                ]:
                                    if candidate.exists() and candidate.stat().st_size > 0:
                                        disk_found = True
                                        break
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
                        # If final video is already produced, intermediate audio clips may have been safely cleaned up
                        final_vid_exists = (
                            (chap_dir / "video" / "final.mp4").exists()
                            or (get_output_dir(project_slug) / format_chapter_filename(chap_num)).exists()
                        )
                        if not exists_and_nonempty and final_vid_exists:
                            # Intermediate clips were safely cleaned up after final video render
                            pass
                        else:
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

                if check_pacing:
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

                # When script validation passes without new errors, complete StageType.SCRIPT checkpoint
                if len(errors) == script_errors_before and chap_meta:
                    current_script_status = None
                    if hasattr(chap_meta, "stages"):
                        s_cp = chap_meta.stages.get(StageType.SCRIPT) or chap_meta.stages.get("script")
                        if s_cp:
                            current_script_status = getattr(s_cp, "status", None)
                            if hasattr(current_script_status, "value"):
                                current_script_status = current_script_status.value
                    if current_script_status == CheckpointStatus.FAILED.value:
                        update_checkpoint(chap_dir, StageType.SCRIPT, CheckpointStatus.RUNNING)
                        update_checkpoint(chap_dir, StageType.SCRIPT, CheckpointStatus.COMPLETED, artifacts=["script.json"])
                    elif current_script_status != CheckpointStatus.COMPLETED.value:
                        update_checkpoint(chap_dir, StageType.SCRIPT, CheckpointStatus.COMPLETED, artifacts=["script.json"])

            except (ValidationError, StorageCorruptError) as e:
                check(False, f"chapter_{chap_num}/script.json", f"Invalid script.json schema: {e}")

        # Final video verification if render stage completed
        if chap_meta and hasattr(chap_meta, "stages"):
            render_stage = chap_meta.stages.get("render")
            if render_stage and getattr(render_stage, "status", None) == "completed":
                video_dir = chap_dir / "video"
                has_video = video_dir.exists() and any(
                    f.stat().st_size > 0 for f in video_dir.glob("*.mp4")
                )
                check(has_video, f"chapter_{chap_num}/video", "Render marked completed but no valid MP4 video found")

        # Checksum verification in strict mode
        if strict and chap_meta and hasattr(chap_meta, "stages"):
            for stage_key, s_info in chap_meta.stages.items():
                checksums = getattr(s_info, "checksums", {}) or {}
                for rel_path, expected_hash in checksums.items():
                    target_file = chap_dir / rel_path
                    if target_file.exists():
                        match = verify_checksum(target_file, expected_hash)
                        check(match, f"checksum:{rel_path}", f"Checksum mismatch for {rel_path}")
                    else:
                        check(False, f"checksum:{rel_path}", f"File for checksum not found: {rel_path}")

    is_valid = len(errors) == 0 and (not strict or len(warnings) == 0)

    result_payload = {
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
    if check_pacing:
        result_payload["pacing_reports"] = pacing_reports

    if not is_valid:
        raise CliValidationError(
            f"Validation failed with {len(errors)} error(s) and {len(warnings)} warning(s).",
            details=errors,
            data=result_payload,
        )

    return result_payload
