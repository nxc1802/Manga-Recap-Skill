"""
src/cli/status_cmd.py
Subcommand handler: manga-recap status
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any, Dict, List

from src.core.schemas import ChapterMetadata, CheckpointStatus
from src.core.storage import (
    StorageFileNotFoundError,
    get_chapter_dir,
    get_project_dir,
    get_resumable_stage,
    read_json,
)
from src.core.utils import get_project_data_dir


def handle_status(args: argparse.Namespace) -> Dict[str, Any]:
    """Queries project or chapter execution status and checkpoint state."""
    project_slug = getattr(args, "project", "")
    if not project_slug:
        raise StorageFileNotFoundError("Project slug is required.")

    project_dir = get_project_dir(project_slug)
    if not project_dir.exists():
        fallback_dir = get_project_data_dir(project_slug)
        if fallback_dir.exists():
            project_dir = fallback_dir
        else:
            raise StorageFileNotFoundError(
                f"Project '{project_slug}' directory not found at {project_dir}"
            )

    chapter_arg = getattr(args, "chapter", None)

    # 1. Chapter-level status
    if chapter_arg is not None:
        chap_dir = get_chapter_dir(project_slug, chapter_arg)
        if not chap_dir.exists():
            fallback_chap = project_dir / "chapters" / str(chapter_arg)
            if fallback_chap.exists():
                chap_dir = fallback_chap
            else:
                raise StorageFileNotFoundError(
                    f"Chapter '{chapter_arg}' directory not found at {chap_dir}"
                )

        chap_json_path = chap_dir / "chapter.json"
        if not chap_json_path.exists():
            raise StorageFileNotFoundError(
                f"chapter.json not found in chapter directory: {chap_dir}"
            )

        ch_data = read_json(chap_json_path)
        meta = ChapterMetadata.model_validate(ch_data)
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

        is_complete = (
            meta.overall_status == CheckpointStatus.COMPLETED
            or (resumable_stage is None and meta.overall_status != CheckpointStatus.FAILED)
        )

        return {
            "project_slug": project_slug,
            "chapter_number": str(chapter_arg),
            "overall_status": meta.overall_status.value,
            "current_stage": meta.current_stage.value if meta.current_stage else None,
            "resumable_stage": resumable_stage.value if resumable_stage else None,
            "is_complete": is_complete,
            "stages": stages_dict,
        }

    # 2. Project-level multi-chapter status overview
    chapters_dir = project_dir / "chapters"
    chapter_summaries: List[Dict[str, Any]] = []
    if chapters_dir.exists():
        for c_name in sorted(os.listdir(chapters_dir)):
            chap_subdir = chapters_dir / c_name
            if chap_subdir.is_dir():
                c_json = chap_subdir / "chapter.json"
                if c_json.exists():
                    try:
                        c_data = read_json(c_json)
                        c_meta = ChapterMetadata.model_validate(c_data)
                        c_resumable = get_resumable_stage(chap_subdir)
                        chapter_summaries.append({
                            "chapter_number": c_name,
                            "overall_status": c_meta.overall_status.value,
                            "current_stage": c_meta.current_stage.value if c_meta.current_stage else None,
                            "resumable_stage": c_resumable.value if c_resumable else None,
                            "is_complete": c_meta.overall_status == CheckpointStatus.COMPLETED or c_resumable is None,
                        })
                    except Exception:
                        chapter_summaries.append({
                            "chapter_number": c_name,
                            "overall_status": "corrupt",
                            "current_stage": None,
                            "resumable_stage": None,
                            "is_complete": False,
                        })

    return {
        "project_slug": project_slug,
        "total_chapters": len(chapter_summaries),
        "chapters": chapter_summaries,
    }
