"""Authoritative project/chapter path resolution, layout init, staging, and store helpers."""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Union

from src.core.errors import StorageFileNotFoundError
from src.core.schemas import (
    ChapterMetadata,
    PanelManifest,
    ProjectConfig,
    RecapScript,
)
from src.core.settings import get_runtime_settings
from src.core.storage import atomic_write_json, read_json


def get_projects_root() -> Path:
    """Returns the base storage root directory: data/projects."""
    return get_runtime_settings().projects_dir


def get_project_dir(project_slug: str) -> Path:
    """Returns data/projects/<project_slug>."""
    return get_projects_root() / project_slug


def get_chapter_dir(project_slug: str, chapter_num: Union[str, int]) -> Path:
    """Returns data/projects/<project_slug>/chapters/<chapter_num>."""
    return get_project_dir(project_slug) / "chapters" / str(chapter_num)


def get_output_dir(project_slug: str) -> Path:
    """Returns standardized export directory: output/<project_slug>."""
    return get_runtime_settings().output_dir / project_slug


def get_youtube_accounts_dir() -> Path:
    """Returns the base storage directory for YouTube accounts registry and tokens."""
    return get_runtime_settings().youtube_accounts_dir


def get_workspace_dir(project_slug: str, chapter_num: Optional[Union[str, int]] = None) -> Path:
    """Returns standardized workspace directory: workspace/<project_slug>[/chapter_NNN]."""
    base = get_runtime_settings().workspace_dir / project_slug
    if chapter_num is not None:
        try:
            c_int = int(str(chapter_num).strip())
            return base / f"chapter_{c_int:03d}"
        except (ValueError, TypeError):
            return base / f"chapter_{chapter_num}"
    return base


def resolve_existing_project_dir(project_slug: str, *, require_exists: bool = True) -> Path:
    """Resolve an existing project directory using canonical path first, then legacy fallback."""
    project_dir = get_project_dir(project_slug)
    if project_dir.exists():
        return project_dir
    from src.core.utils import get_project_data_dir

    fallback_dir = get_project_data_dir(project_slug)
    if fallback_dir.exists():
        return fallback_dir
    if require_exists:
        raise StorageFileNotFoundError(
            f"Project '{project_slug}' directory not found at {project_dir}"
        )
    return project_dir


def resolve_existing_chapter_dir(
    project_slug: str,
    chapter_num: Union[str, int],
    *,
    project_dir: Optional[Path] = None,
    require_exists: bool = True,
) -> Path:
    """Resolve an existing chapter directory within a canonical or fallback project root."""
    chap_dir = get_chapter_dir(project_slug, chapter_num)
    if chap_dir.exists():
        return chap_dir
    if project_dir is None:
        project_dir = resolve_existing_project_dir(project_slug, require_exists=require_exists)
    fallback_chap = Path(project_dir) / "chapters" / str(chapter_num)
    if fallback_chap.exists():
        return fallback_chap
    if require_exists:
        raise StorageFileNotFoundError(
            f"Chapter '{chapter_num}' directory not found at {chap_dir}"
        )
    return chap_dir


def format_chapter_filename(
    chapter_num: Union[int, str],
    ext: str = "mp4",
    padded: bool = True,
) -> str:
    """Standardizes chapter export file naming: chapter_{chapter_num:03d}.<ext>."""
    clean_ext = ext.lstrip(".")
    try:
        c_int = int(str(chapter_num).strip())
        return f"chapter_{c_int:03d}.{clean_ext}" if padded else f"chapter_{c_int}.{clean_ext}"
    except (ValueError, TypeError):
        return f"chapter_{chapter_num}.{clean_ext}"


def get_chapter_export_path(
    project_slug: str,
    chapter_num: Union[int, str],
    ext: str = "mp4",
    padded: bool = True,
) -> Path:
    """Returns standardized export path under output/<project_slug>/."""
    return get_output_dir(project_slug) / format_chapter_filename(chapter_num, ext=ext, padded=padded)


def stage_chapter_export(
    project_slug: str,
    chapter_num: Union[int, str],
    video_path: Optional[Union[str, Path]] = None,
    subtitle_path: Optional[Union[str, Path]] = None,
) -> Dict[str, Optional[Path]]:
    """Stages verified final MP4 and SRT into standardized output/<project_slug>/ directory."""
    out_dir = get_output_dir(project_slug)
    out_dir.mkdir(parents=True, exist_ok=True)

    result: Dict[str, Optional[Path]] = {
        "video_padded": None,
        "video": None,
        "video_unpadded": None,
        "subtitle_padded": None,
        "subtitle": None,
        "subtitle_unpadded": None,
    }

    if video_path:
        v_src = Path(video_path).resolve()
        if v_src.exists() and v_src.is_file():
            padded_name = format_chapter_filename(chapter_num, ext="mp4", padded=True)
            dest_padded = out_dir / padded_name
            if v_src != dest_padded.resolve():
                shutil.copy2(v_src, dest_padded)
            result["video_padded"] = dest_padded
            result["video"] = dest_padded
            result["video_unpadded"] = dest_padded

    if subtitle_path:
        s_src = Path(subtitle_path).resolve()
        if s_src.exists() and s_src.is_file():
            padded_name = format_chapter_filename(chapter_num, ext="srt", padded=True)
            dest_padded = out_dir / padded_name
            if s_src != dest_padded.resolve():
                shutil.copy2(s_src, dest_padded)
            result["subtitle_padded"] = dest_padded
            result["subtitle"] = dest_padded
            result["subtitle_unpadded"] = dest_padded

    return result


def init_project_layout(project_slug: str, title: str) -> Path:
    """Initializes standard project directory structure and default project.json."""
    project_dir = get_project_dir(project_slug)
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "chapters").mkdir(exist_ok=True)

    project_json = project_dir / "project.json"
    if not project_json.exists():
        cfg = ProjectConfig(
            project_id=str(uuid.uuid4()),
            slug=project_slug,
            title=title,
        )
        atomic_write_json(project_json, cfg)

    summary_file = project_dir / "story_summary.md"
    if not summary_file.exists():
        initial_summary = (
            f"# Story Summary: {title}\n\n"
            f"## Project Overview\n- **Series Title**: {title}\n- **Slug**: {project_slug}\n\n"
            "## World Building & Lore\n\n## Major Characters\n\n## Chapter Recaps & Timeline\n"
        )
        summary_file.write_text(initial_summary, encoding="utf-8")

    return project_dir


def init_chapter_layout(project_slug: str, chapter_num: Union[str, int], title: str = "") -> Path:
    """Initializes chapter directories and chapter.json."""
    chap_dir = get_chapter_dir(project_slug, chapter_num)
    for sub in ("raw", "panels", "contact_sheets", "audio", "subtitles", "video", "audio/clips"):
        (chap_dir / sub).mkdir(parents=True, exist_ok=True)

    chapter_json = chap_dir / "chapter.json"
    if not chapter_json.exists():
        meta = ChapterMetadata(
            chapter_id=f"{project_slug}_chap_{chapter_num}",
            chapter_number=str(chapter_num),
            project_slug=project_slug,
            title=title or f"Chapter {chapter_num}",
        )
        atomic_write_json(chapter_json, meta)

    return chap_dir


def _chapter_json_path(chapter_dir_or_file: Path, default_name: str) -> Path:
    p = Path(chapter_dir_or_file)
    return p if p.suffix == ".json" else p / default_name


def load_project_config(project_dir: Path) -> ProjectConfig:
    """Loads and validates project.json for the given project directory or file path."""
    proj_file = _chapter_json_path(project_dir, "project.json")
    if not proj_file.exists():
        raise StorageFileNotFoundError(f"Project config not found at {proj_file}")
    return ProjectConfig.model_validate(read_json(proj_file))


def save_project_config(project_dir: Path, config: Union[ProjectConfig, Dict[str, Any]]) -> Path:
    """Atomically writes project.json for the given project directory or file path."""
    proj_file = _chapter_json_path(project_dir, "project.json")
    atomic_write_json(proj_file, config)
    return proj_file


def load_chapter_metadata(chapter_dir: Path) -> ChapterMetadata:
    """Loads and validates chapter.json for the given chapter directory or file path."""
    chap_file = _chapter_json_path(chapter_dir, "chapter.json")
    if not chap_file.exists():
        raise StorageFileNotFoundError(f"Chapter metadata not found at {chap_file}")
    return ChapterMetadata.model_validate(read_json(chap_file))


def save_chapter_metadata(chapter_dir: Path, metadata: Union[ChapterMetadata, Dict[str, Any]]) -> Path:
    """Atomically writes chapter.json for the given chapter directory or file path."""
    chap_file = _chapter_json_path(chapter_dir, "chapter.json")
    atomic_write_json(chap_file, metadata)
    return chap_file


def load_recap_script(chapter_dir: Path) -> RecapScript:
    """Loads and validates script.json for the given chapter directory or file path."""
    script_file = _chapter_json_path(chapter_dir, "script.json")
    if not script_file.exists():
        raise StorageFileNotFoundError(f"Recap script not found at {script_file}")
    return RecapScript.model_validate(read_json(script_file))


def save_recap_script(chapter_dir: Path, script: Union[RecapScript, Dict[str, Any]]) -> None:
    """Atomically writes script.json for the given chapter directory or file path."""
    script_file = _chapter_json_path(chapter_dir, "script.json")
    atomic_write_json(script_file, script)


def load_manifest(chapter_dir: Path) -> Optional[PanelManifest]:
    """Loads and validates manifest.json, or returns None if absent."""
    manifest_file = _chapter_json_path(chapter_dir, "manifest.json")
    if not manifest_file.exists():
        return None
    return PanelManifest.model_validate(read_json(manifest_file))


def save_manifest(chapter_dir: Path, manifest: Union[PanelManifest, Dict[str, Any]]) -> Path:
    """Atomically writes manifest.json and returns its path."""
    manifest_file = _chapter_json_path(chapter_dir, "manifest.json")
    atomic_write_json(manifest_file, manifest)
    return manifest_file
