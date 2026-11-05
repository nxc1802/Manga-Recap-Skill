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
    get_chapter_dir,
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

    errors: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []
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
    if project_json_path.exists():
        try:
            p_data = read_json(project_json_path)
            ProjectConfig.model_validate(p_data)
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
            except (ValidationError, StorageCorruptError) as e:
                check(False, f"chapter_{chap_num}/chapter.json", f"Invalid chapter.json schema: {e}")
        else:
            check(False, f"chapter_{chap_num}/chapter.json", "Missing chapter.json")

        # manifest.json & physical panels
        manifest_path = chap_dir / "manifest.json"
        manifest_panel_ids: set[str] = set()
        canonical_manifest_pids: set[str] = set()
        panel_lookup: Dict[str, str] = {}
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
                    found = candidate1.exists() or candidate2.exists() or candidate3.exists() or candidate4.exists()
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

                # Check panel references, 100% coverage, no duplicates, and synthesized audio
                covered_panel_ids: set[str] = set()
                duplicate_panel_ids: set[str] = set()

                for seg in script_obj.segments:
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
                            else:
                                if canon_pid in covered_panel_ids:
                                    duplicate_panel_ids.add(canon_pid)
                                    check(
                                        False,
                                        f"segment:{seg.seq_id}:duplicate_panel:{canon_pid}",
                                        f"Panel '{canon_pid}' is referenced multiple times across script segments",
                                    )
                                else:
                                    covered_panel_ids.add(canon_pid)

                    if seg.status == "synthesized" and seg.audio_path:
                        audio_file = Path(seg.audio_path)
                        if not audio_file.is_absolute():
                            audio_file = chap_dir / audio_file
                        exists_and_nonempty = audio_file.exists() and audio_file.stat().st_size > 0
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

                if canonical_manifest_pids:
                    missing_pids = canonical_manifest_pids - covered_panel_ids
                    if missing_pids:
                        check(
                            False,
                            f"chapter_{chap_num}:panel_coverage",
                            f"Script segments do not cover all active manifest panels. Missing panels: {sorted(list(missing_pids))}",
                        )

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

    if not is_valid:
        raise CliValidationError(
            f"Validation failed with {len(errors)} error(s) and {len(warnings)} warning(s).",
            details=errors,
            data=result_payload,
        )

    return result_payload
