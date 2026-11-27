"""Intermediate cleanup and safe raw-purge domain operations."""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.core.errors import DomainValidationError, StorageError
from src.core.project import load_chapter_metadata, resolve_existing_project_dir
from src.core.release_schemas import ArchiveReceipt
from src.core.schemas import CheckpointStatus, StageType
from src.core.storage import acquire_lock, calculate_sha256, read_json
from src.core.utils import assert_file_nonempty

PROTECTED_FILENAMES = frozenset({
    "manifest.json",
    "script.json",
    "chapter.json",
    "project.json",
    "story_summary.md",
    "characters.json",
    "voice_selection.json",
    "preview_approval.json",
    "release_state.json",
    "series_setup_answers.json",
    "visual_plan.json",
    "archive_manifest.json",
    "archive_receipt.json",
    "youtube_receipt.json",
})

_TEMP_PATTERNS = (
    "*_raw_temp*.mp4",
    "*_subs_temp*.srt",
    "*concat_list*.txt",
    ".tmp_concat*",
    "*.tmp",
)


def cleanup_intermediate_files(chapter_dir: Path) -> List[str]:
    """Cleanly unlinks known temporary video/subtitle/concat files."""
    if not chapter_dir.exists():
        return []
    cleaned: List[str] = []
    seen = set()
    for pattern in _TEMP_PATTERNS:
        for p in chapter_dir.rglob(pattern):
            if p in seen:
                continue
            seen.add(p)
            try:
                p.unlink(missing_ok=True)
                cleaned.append(str(p.name))
            except Exception as e:
                with contextlib.suppress(Exception):
                    from src.core.logger import logger
                    logger.warning(f"Failed to remove temp file {p}: {e}")
    return cleaned


def safe_cleanup_chapter_intermediates(
    chapter_dir: Path,
    output_video_path: Path,
    min_size_bytes: int = 100000,
    clean_clips: bool = True,
) -> List[str]:
    """
    Safely cleans intermediate temporary files ONLY after verifying that output_video_path
    exists, is a valid file, and has size >= min_size_bytes.
    Never deletes raw/, panels/, or protected metadata/receipt JSON files.
    """
    chapter_dir = Path(chapter_dir).resolve()
    if output_video_path is None:
        raise StorageError("Safe cleanup aborted: output_video_path is None.")

    out_vid = Path(output_video_path).resolve()
    if not out_vid.exists() or not out_vid.is_file():
        raise StorageError(
            f"Safe cleanup aborted: output video does not exist or is not a file: {out_vid}"
        )

    file_size = out_vid.stat().st_size
    if file_size < min_size_bytes:
        raise StorageError(
            f"Safe cleanup aborted: output video {out_vid} size ({file_size} bytes) "
            f"is below minimum threshold ({min_size_bytes} bytes)."
        )

    if not chapter_dir.exists():
        return []

    cleaned: List[str] = []
    seen = set()
    raw_dir = (chapter_dir / "raw").resolve()
    panels_dir = (chapter_dir / "panels").resolve()

    def _unlink_file(p: Path) -> None:
        if p in seen or not p.is_file() or p.name in PROTECTED_FILENAMES:
            return
        seen.add(p)
        try:
            p.unlink(missing_ok=True)
            cleaned.append(str(p.name))
        except Exception as e:
            with contextlib.suppress(Exception):
                from src.core.logger import logger
                logger.warning(f"Failed to remove temp file {p}: {e}")

    for pattern in _TEMP_PATTERNS:
        for p in chapter_dir.rglob(pattern):
            try:
                if p.resolve().is_relative_to(raw_dir) or p.resolve().is_relative_to(panels_dir):
                    continue
            except (ValueError, AttributeError):
                if "raw" in p.parts or "panels" in p.parts:
                    continue
            _unlink_file(p)

    for temp_sub in ("temp_clips", "temp", "temp_frames", "frames"):
        temp_dir = chapter_dir / "video" / temp_sub
        if temp_dir.exists() and temp_dir.is_dir():
            for p in list(temp_dir.iterdir()):
                _unlink_file(p)
            with contextlib.suppress(Exception):
                if not any(temp_dir.iterdir()):
                    temp_dir.rmdir()

    if clean_clips:
        v_clips_dir = chapter_dir / "video" / "clips"
        if v_clips_dir.exists() and v_clips_dir.is_dir():
            for p in list(v_clips_dir.iterdir()):
                _unlink_file(p)
            with contextlib.suppress(Exception):
                if not any(v_clips_dir.iterdir()):
                    v_clips_dir.rmdir()

        v_dir = chapter_dir / "video"
        if v_dir.exists() and v_dir.is_dir():
            for p in v_dir.glob("raw_video_*.mp4"):
                _unlink_file(p)

        a_clips_dir = chapter_dir / "audio" / "clips"
        if a_clips_dir.exists() and a_clips_dir.is_dir():
            for p in list(a_clips_dir.iterdir()):
                _unlink_file(p)
            with contextlib.suppress(Exception):
                if not any(a_clips_dir.iterdir()):
                    a_clips_dir.rmdir()

        a_dir = chapter_dir / "audio"
        if a_dir.exists() and a_dir.is_dir():
            for p in a_dir.glob("speech_master_*.wav"):
                _unlink_file(p)

    return cleaned


def _verified_archived_files(root: Path, slug: str) -> Dict[str, str]:
    receipt = ArchiveReceipt.model_validate(read_json(root / "archive_receipt.json"))
    manifest_path = root / "archive_manifest.json"
    manifest = read_json(manifest_path)
    if (
        receipt.status != "verified"
        or receipt.verification_revision != receipt.commit_sha
        or receipt.project_slug != slug
        or manifest.get("project_slug") != slug
        or manifest.get("repo_id") != receipt.repo_id
        or calculate_sha256(manifest_path) != receipt.manifest_sha256
    ):
        raise DomainValidationError("Verify the current archive before purging raw pages and panels")
    return {entry["relative_path"]: entry["sha256"] for entry in manifest["files"]}


def clean_project_chapters(
    project_slug: str,
    *,
    chapter: Optional[Union[str, int]] = None,
    dry_run: bool = True,
    confirm_project: Optional[str] = None,
    bypass_receipts: bool = False,
    purge_raw: bool = False,
) -> Dict[str, Any]:
    """
    Domain cleanup operation for chapter intermediates and verified-archive raw purge.
    Preserves final outputs, raw assets (unless purge_raw with verified archive), and
    active running stages; defaults to dry_run=True.
    """
    root = resolve_existing_project_dir(project_slug, require_exists=False).resolve()
    if not root.is_dir():
        raise DomainValidationError(f"Project not found: {project_slug}")
    if not dry_run and confirm_project != project_slug:
        raise DomainValidationError(f"Specify --confirm-project {project_slug} to delete files")
    if not dry_run and bypass_receipts:
        raise DomainValidationError("--bypass-receipts is for dry runs")

    archived = _verified_archived_files(root, project_slug) if purge_raw and not dry_run else {}
    chapters = root / "chapters"
    if chapter is not None and not str(chapter).isdigit():
        raise DomainValidationError("Chapter must be numeric")
    targets = [chapters / str(chapter)] if chapter is not None else sorted(chapters.glob("*"))

    patterns = (
        "audio/clips/*.wav",
        "audio/speech_master*.wav",
        "audio/mixed.m4a",
        "video/clips/*",
        "video/raw_video_*.mp4",
        "video/*_v[0-9]*.mp4",
        "video/final.previous.mp4",
        "video/final.stale.*.mp4",
        "video/.*.partial.mp4",
        "video/*.previous",
        "subtitles/*.previous",
        "**/*concat_list*.txt",
        "**/*.tmp",
    )
    if purge_raw:
        patterns += ("raw/*", "panels/*")

    cleaned, skipped, names = [], [], []
    size = 0
    for chap_dir in targets:
        if not chap_dir.name.isdigit() or not chap_dir.is_dir() or chap_dir.is_symlink():
            continue
        try:
            assert_file_nonempty(chap_dir / "video" / "final.mp4")
            assert_file_nonempty(chap_dir / "subtitles" / "final.srt")
        except FileNotFoundError as exc:
            skipped.append({"chapter": chap_dir.name, "reason": str(exc)})
            continue

        with acquire_lock(chap_dir / "chapter.json"):
            if (chap_dir / "chapter.json").is_file():
                states = load_chapter_metadata(chap_dir).stages
                busy = any(
                    states[stage].status == CheckpointStatus.RUNNING
                    for stage in (StageType.AUDIO, StageType.TTS, StageType.RENDER)
                    if stage in states
                )
                if busy:
                    skipped.append({"chapter": chap_dir.name, "reason": "Audio or render is running"})
                    continue

            candidates = sorted(
                {
                    path
                    for pattern in patterns
                    for path in chap_dir.glob(pattern)
                    if path.is_file()
                    and not path.is_symlink()
                    and path.resolve().is_relative_to(chap_dir)
                    and (purge_raw or path.relative_to(chap_dir).parts[0] not in {"raw", "panels"})
                }
            )
            if purge_raw and not dry_run:
                for path in candidates:
                    if path.relative_to(chap_dir).parts[0] in {"raw", "panels"}:
                        if archived.get(path.relative_to(root).as_posix()) != calculate_sha256(path):
                            raise DomainValidationError(f"Archive does not cover current source file: {path}")

            for path in candidates:
                size += path.stat().st_size
                names.append(path.relative_to(root).as_posix())
                if not dry_run:
                    path.unlink()
        cleaned.append(chap_dir.name)

    return {
        "project_slug": project_slug,
        "dry_run": dry_run,
        "chapters_cleaned": cleaned,
        "skipped_chapters": skipped,
        "cleaned_files": names,
        "cleaned_files_count": len(names),
        "reclaimed_bytes": size,
        "reclaimed_mb": round(size / 2**20, 2),
        "reclaimed_gb": round(size / 2**30, 2),
    }
