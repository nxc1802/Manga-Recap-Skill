"""
src/cli/batch_cmd.py
Subcommand handler: manga-recap batch
Sequential multi-chapter batch processing, failure isolation, and single-chapter retry.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.cli.exit_codes import CliValidationError
from src.core.logger import logger
from src.core.schemas import (
    CheckpointStatus,
    StageType,
    VoiceSelectionApproval,
)
from src.core.storage import (
    StorageFileNotFoundError,
    format_chapter_filename,
    get_chapter_dir,
    get_output_dir,
    get_project_dir,
    load_chapter_metadata,
    read_json,
    safe_cleanup_chapter_intermediates,
    stage_chapter_export,
    update_checkpoint,
)
from src.core.utils import get_project_data_dir


def parse_chapter_list(chapters_arg: Optional[str]) -> List[str]:
    """
    Parses chapter argument into a list of chapter strings.
    Supports formats: "1,2,3", "1-3", "1, 2, 3", "1-2, 4".
    """
    if not chapters_arg:
        return []
    result: List[str] = []
    tokens = [t.strip() for t in chapters_arg.split(",") if t.strip()]
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
    # Deduplicate while preserving order
    seen = set()
    deduped = []
    for c in result:
        if c not in seen:
            seen.add(c)
            deduped.append(c)
    return deduped


def discover_project_chapters(project_dir: Path) -> List[str]:
    """Auto-discovers registered or available chapters in project."""
    # 1. From project.json chapters list
    proj_json = project_dir / "project.json"
    if proj_json.exists():
        with contextlib.suppress(Exception):
            p_data = read_json(proj_json)
            if isinstance(p_data, dict) and p_data.get("chapters"):
                return [str(c) for c in p_data["chapters"]]

    # 2. From chapters directory
    chapters_dir = project_dir / "chapters"
    if chapters_dir.exists():
        entries = [
            e for e in os.listdir(chapters_dir)
            if (chapters_dir / e).is_dir() and not e.startswith(".")
        ]
        # Sort numerically if integer, else lexicographically
        def sort_key(x: str):
            try:
                return (0, int(x))
            except ValueError:
                return (1, x)
        return sorted(entries, key=sort_key)
    return []


def handle_batch(args: argparse.Namespace) -> Dict[str, Any]:
    """
    Sequential multi-chapter batch processor.
    - Gate 0: Inherits series configurations from project.json.
    - Gate 1: Approves/verifies voice selection once at project level.
    - Gate 2: Skips preview by default (--skip-preview) for direct 1080p final render.
    - Chapter Failure Isolation: Errors on chapter K mark it FAILED and record error,
      allowing subsequent chapters to proceed without crashing the process.
    - Single-Chapter Retry: --retry-chapter resumes only specified chapter.
    """
    project_slug = getattr(args, "project", "")
    if not project_slug:
        raise CliValidationError("Project slug (--project) is required for batch processing.")

    project_dir = get_project_dir(project_slug)
    if not project_dir.exists():
        fallback_dir = get_project_data_dir(project_slug)
        if fallback_dir.exists():
            project_dir = fallback_dir
        else:
            raise StorageFileNotFoundError(
                f"Project '{project_slug}' directory not found at {project_dir}"
            )

    proj_json_path = project_dir / "project.json"
    if not proj_json_path.exists():
        raise StorageFileNotFoundError(
            f"Project config 'project.json' not found in {project_dir}"
        )

    # Gate 1: Check Series-Level Voice Selection
    approved = getattr(args, "approved", False)
    if not approved:
        v_sel_path = project_dir / "voice_selection.json"
        if v_sel_path.exists():
            try:
                v_data = read_json(v_sel_path)
                v_approval = VoiceSelectionApproval.model_validate(v_data)
                if not v_approval.is_approved():
                    raise CliValidationError(
                        f"Series-level Review Gate 1 rejected: '{v_sel_path}' is not approved."
                    )
            except Exception as e:
                if isinstance(e, CliValidationError):
                    raise
                raise CliValidationError(
                    f"Series-level Review Gate 1 validation failed for '{v_sel_path}': {e}"
                ) from e

    # Determine chapters to process
    retry_chapter = getattr(args, "retry_chapter", None)
    chapters_arg = getattr(args, "chapters", None)

    if retry_chapter is not None:
        target_chapters = [str(retry_chapter)]
    elif chapters_arg:
        target_chapters = parse_chapter_list(chapters_arg)
    else:
        target_chapters = discover_project_chapters(project_dir)

    if not target_chapters:
        raise CliValidationError(
            f"No chapters found or specified to process for project '{project_slug}'."
        )

    force = getattr(args, "force", False)
    skip_preview = getattr(args, "skip_preview", True)
    allow_placeholders = getattr(args, "allow_placeholders", False)
    cleanup = getattr(args, "cleanup", False)
    output_dir = get_output_dir(project_slug)
    output_dir.mkdir(parents=True, exist_ok=True)

    from src.cli.render_cmd import handle_render
    from src.cli.tts_cmd import handle_tts
    from src.cli.validate_cmd import handle_validate

    completed_chaps: List[str] = []
    cached_chaps: List[str] = []
    failed_chaps: List[Dict[str, Any]] = []
    chapter_results: List[Dict[str, Any]] = []

    for chap_num in target_chapters:
        t0 = time.monotonic()
        chap_dir = get_chapter_dir(project_slug, chap_num)
        if not chap_dir.exists():
            fallback_chap = project_dir / "chapters" / str(chap_num)
            if fallback_chap.exists():
                chap_dir = fallback_chap

        # 1. Caching check: if chapter is already completed, skip unless force or retry
        if not force and (retry_chapter is None or str(chap_num) != str(retry_chapter)):
            chap_json = chap_dir / "chapter.json"
            if chap_json.exists():
                with contextlib.suppress(Exception):
                    meta = load_chapter_metadata(chap_dir)
                    if meta.overall_status == CheckpointStatus.COMPLETED:
                        cached_chaps.append(str(chap_num))
                        vid_path = chap_dir / "video" / f"chapter_{chap_num}_final.mp4"
                        if not vid_path.exists():
                            vid_path = chap_dir / "video" / "final.mp4"
                        srt_path = chap_dir / "subtitles" / f"chapter_{chap_num}.srt"
                        if not srt_path.exists():
                            srt_path = chap_dir / "subtitles" / "final.srt"

                        # Ensure staged export exists in output/<project_slug>/
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

        # 2. Sequential Chapter Pipeline with Error Isolation
        try:
            logger.info(f"--- [Batch] Starting Chapter {chap_num} ({project_slug}) ---")

            if not chap_dir.exists():
                raise StorageFileNotFoundError(f"Chapter directory not found at {chap_dir}")

            script_path = chap_dir / "script.json"
            if not script_path.exists():
                raise StorageFileNotFoundError(f"script.json not found in chapter directory: {chap_dir}")

            # Operational Stage 1: Ingest
            c_meta = load_chapter_metadata(chap_dir)
            ing_cp = c_meta.stages.get(StageType.INGEST)
            if not ing_cp or ing_cp.status != CheckpointStatus.COMPLETED:
                update_checkpoint(chap_dir, StageType.INGEST, CheckpointStatus.RUNNING)
                update_checkpoint(chap_dir, StageType.INGEST, CheckpointStatus.COMPLETED)

            # Operational Stage 2: Agent (Validate script.json)
            val_args = argparse.Namespace(project=project_slug, chapter=chap_num, strict=False)
            val_res = handle_validate(val_args)
            if not val_res.get("valid", True) and val_res.get("errors"):
                err_msg = "; ".join(f"{e.get('target')}: {e.get('message')}" for e in val_res["errors"])
                raise CliValidationError(f"Script validation failed: {err_msg}")
            c_meta = load_chapter_metadata(chap_dir)
            agent_cp = c_meta.stages.get(StageType.AGENT)
            if not agent_cp or agent_cp.status != CheckpointStatus.COMPLETED:
                update_checkpoint(chap_dir, StageType.AGENT, CheckpointStatus.RUNNING)
                update_checkpoint(chap_dir, StageType.AGENT, CheckpointStatus.COMPLETED, artifacts=["script.json"])

            if getattr(args, "kaggle", False):
                logger.info(f"[Batch] Offloading Chapter {chap_num} to Kaggle Cloud GPU (TTS + Hardware NVENC)...")
                from src.cloud.kaggle_service import KaggleCloudService
                service = KaggleCloudService()
                if not service.is_authenticated:
                    raise CliValidationError("Kaggle API authentication failed. Ensure KAGGLE_API_TOKEN is configured in ~/.kaggle/access_token.")
                cloud_res = service.dispatch_chapter_job(
                    project_slug=project_slug,
                    chapter_number=str(chap_num),
                    gpu=True,
                    wait=True,
                )
                render_res = {
                    "video_path": cloud_res.get("final_video"),
                    "subtitle_path": cloud_res.get("subtitles"),
                    "telemetry": cloud_res.get("telemetry", {}),
                }
            else:
                # Operational Stage 3: Audio (VieNeu-TTS)
                tts_args = argparse.Namespace(
                    project=project_slug,
                    chapter=chap_num,
                    engine=getattr(args, "engine", getattr(args, "tts_engine", "neural")),
                    voice=getattr(args, "voice", None),
                    speed=getattr(args, "speed", None),
                    workers=getattr(args, "tts_workers", getattr(args, "workers", None)),
                    force=force,
                )
                handle_tts(tts_args)
                c_meta = load_chapter_metadata(chap_dir)
                audio_cp = c_meta.stages.get(StageType.AUDIO)
                if not audio_cp or audio_cp.status != CheckpointStatus.COMPLETED:
                    update_checkpoint(chap_dir, StageType.AUDIO, CheckpointStatus.COMPLETED)

                # Operational Stage 4: Render (Final 1080p, skip_preview=True)
                render_args = argparse.Namespace(
                    project=project_slug,
                    chapter=chap_num,
                    quality="final",
                    suffix="final",
                    encoder=getattr(args, "encoder", None),
                    hw_accel=getattr(args, "hw_accel", None),
                    workers=getattr(args, "render_workers", getattr(args, "workers", None)),
                    bgm=getattr(args, "bgm", None),
                    burn_subtitles=getattr(args, "burn_subtitles", True),
                    animation=getattr(args, "animation", True),
                    video_mode=getattr(args, "video_mode", "auto"),
                    force=force,
                    skip_preview=skip_preview,
                    approved=True,
                    allow_placeholders=allow_placeholders,
                    keep_temp=getattr(args, "keep_temp", None),
                    cleanup=cleanup,
                    subtitle_lines=getattr(args, "subtitle_lines", None),
                    subtitle_font_size=getattr(args, "subtitle_font_size", None),
                    subtitle_chunking=getattr(args, "subtitle_chunking", None),
                )
                render_res = handle_render(render_args)

            # Operational Stage 5: Export (Stage MP4 and SRT to output/)
            src_video = Path(render_res["video_path"]) if render_res.get("video_path") else (chap_dir / "video" / f"chapter_{chap_num}_final.mp4")
            src_srt = Path(render_res["subtitle_path"]) if render_res.get("subtitle_path") else (chap_dir / "subtitles" / f"chapter_{chap_num}.srt")

            staged = stage_chapter_export(
                project_slug=project_slug,
                chapter_num=chap_num,
                video_path=src_video,
                subtitle_path=src_srt,
            )
            dest_video = staged.get("video_padded") or staged.get("video") or (output_dir / f"chapter_{chap_num}.mp4")

            if cleanup and src_video.exists():
                safe_cleanup_chapter_intermediates(chap_dir, output_video_path=src_video)

            c_meta = load_chapter_metadata(chap_dir)
            exp_cp = c_meta.stages.get(StageType.EXPORT)
            if not exp_cp or exp_cp.status != CheckpointStatus.COMPLETED:
                update_checkpoint(
                    chap_dir,
                    StageType.EXPORT,
                    CheckpointStatus.COMPLETED,
                    artifacts=[f"output/{project_slug}/{dest_video.name}"]
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
            # Isolate failure: mark chapter checkpoint as FAILED
            if chap_dir.exists() and (chap_dir / "chapter.json").exists():
                with contextlib.suppress(Exception):
                    c_meta = load_chapter_metadata(chap_dir)
                    target_stg = StageType.RENDER
                    stg_cp = c_meta.stages.get(target_stg)
                    if not stg_cp or stg_cp.status == CheckpointStatus.PENDING:
                        update_checkpoint(chap_dir, target_stg, CheckpointStatus.RUNNING)
                    update_checkpoint(
                        chap_dir,
                        stage=target_stg,
                        status=CheckpointStatus.FAILED,
                        error=str(chap_err),
                    )
            failed_chaps.append({
                "chapter": str(chap_num),
                "error": str(chap_err),
                "duration_seconds": round(time.monotonic() - t0, 3),
            })
            chapter_results.append({
                "chapter": str(chap_num),
                "status": "failed",
                "error": str(chap_err),
                "duration_seconds": round(time.monotonic() - t0, 3),
            })
            # IMPORTANT: Do not crash! Loop proceeds to next chapter.

    # 3. Optional Multi-Chapter Compilation
    compile_result = None
    if getattr(args, "compile", False):
        active_chaps = sorted(
            list(set(completed_chaps + cached_chaps)),
            key=lambda x: int(x) if x.isdigit() else x
        )
        if active_chaps:
            from src.cli.compile_cmd import handle_compile
            compile_args = argparse.Namespace(
                project=project_slug,
                chapters=",".join(active_chaps),
                output=getattr(args, "output", str(output_dir / f"{project_slug}_full_recap.mp4")),
                approved=True,
                extract_audio=getattr(args, "extract_audio", True),
            )
            try:
                compile_result = handle_compile(compile_args)
            except Exception as comp_err:
                logger.error(f"[Batch] Compilation failed: {comp_err}")
                compile_result = {"error": str(comp_err)}

    all_succeeded = len(failed_chaps) == 0
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
