"""
src/core/storage.py
Local JSON Storage Engine & File Locking for Manga Recap Agentic Skill V3.
Features:
- Crash-consistent atomic JSON writes with fsync and inode replacement
- Safe JSON reader with structured exception handling
- Cross-platform single-writer FileLock via fcntl / fallback
- Stage checkpoint update state machine
- Streaming SHA-256 content fingerprinting
- Path resolution helpers for data/projects/
"""

from __future__ import annotations

import contextlib
import datetime
from enum import Enum
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Union

from pydantic import BaseModel

from src.core.schemas import (
    ChapterMetadata,
    CheckpointStatus,
    HumanReviewStatus,
    Manifest,
    OPERATIONAL_STAGES,
    PanelManifest,
    ProjectConfig,
    RecapScript,
    STAGE_ALIAS_MAP,
    StageCheckpoint,
    StageType,
    utc_iso_now,
)


# ==============================================================================
# 1. Custom Exceptions
# ==============================================================================

class StorageError(Exception):
    """Base exception for all storage engine errors."""
    pass


class StorageFileNotFoundError(StorageError, FileNotFoundError):
    """Raised when a requested state or data file does not exist."""
    pass


class StorageCorruptError(StorageError, ValueError):
    """Raised when a JSON file contains invalid or unparseable syntax."""
    pass


class StorageWriteError(StorageError, IOError):
    """Raised when an atomic write fails to commit."""
    pass


class StorageLockError(StorageError):
    """Base exception for file locking errors."""
    pass


class StorageLockTimeoutError(StorageLockError):
    """Raised when an exclusive lock cannot be acquired within the timeout period."""
    pass


class InvalidCheckpointTransitionError(StorageError):
    """Raised when an illegal stage status transition is attempted."""
    pass


# ==============================================================================
# 2. Path Resolution Helpers
# ==============================================================================

def get_projects_root() -> Path:
    """Returns the base storage root directory: data/projects."""
    env_root = os.getenv("MANGA_RECAP_DATA_DIR")
    if env_root:
        return Path(env_root) / "projects"
    return Path("data") / "projects"


def get_project_dir(project_slug: str) -> Path:
    """Returns data/projects/<project_slug>."""
    return get_projects_root() / project_slug


def get_chapter_dir(project_slug: str, chapter_num: Union[str, int]) -> Path:
    """Returns data/projects/<project_slug>/chapters/<chapter_num>."""
    return get_project_dir(project_slug) / "chapters" / str(chapter_num)


def get_output_dir(project_slug: str) -> Path:
    """Returns standardized export directory: output/<project_slug>."""
    env_output = os.getenv("MANGA_RECAP_OUTPUT_DIR")
    if env_output:
        return Path(env_output) / project_slug
    return Path("output") / project_slug


def get_youtube_accounts_dir() -> Path:
    """Returns the base storage directory for YouTube accounts registry and tokens."""
    env_dir = os.getenv("YOUTUBE_ACCOUNTS_DIR")
    if env_dir:
        return Path(env_dir)
    data_dir = os.getenv("MANGA_RECAP_DATA_DIR")
    if data_dir:
        return Path(data_dir) / "youtube_accounts"
    return Path("data") / "youtube_accounts"


def get_workspace_dir(project_slug: str, chapter_num: Optional[Union[str, int]] = None) -> Path:
    """
    Returns standardized workspace directory:
    workspace/<project_slug> or workspace/<project_slug>/chapter_{chapter_num:03d}.
    """
    base = Path("workspace") / project_slug
    if chapter_num is not None:
        try:
            c_int = int(str(chapter_num).strip())
            return base / f"chapter_{c_int:03d}"
        except (ValueError, TypeError):
            return base / f"chapter_{chapter_num}"
    return base


def format_chapter_filename(
    chapter_num: Union[int, str],
    ext: str = "mp4",
    padded: bool = True
) -> str:
    """
    Standardizes chapter export file naming:
    chapter_{chapter_num:03d}.mp4 (or .srt) when padded is True (default),
    or chapter_{chapter_num}.mp4 when padded is False.
    Falls back gracefully if chapter_num cannot be cast to int.
    """
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
    """
    Returns standardized export path:
    output/<project_slug>/chapter_{chapter_num:03d}.<ext> (padded=True)
    or output/<project_slug>/chapter_{chapter_num}.<ext> (padded=False).
    """
    return get_output_dir(project_slug) / format_chapter_filename(chapter_num, ext=ext, padded=padded)


def stage_chapter_export(
    project_slug: str,
    chapter_num: Union[int, str],
    video_path: Optional[Union[str, Path]] = None,
    subtitle_path: Optional[Union[str, Path]] = None,
) -> Dict[str, Optional[Path]]:
    """
    Stages/copies verified final MP4 and SRT into standardized output/<project_slug>/ directory.
    Guarantees standard zero-padded format: chapter_{chapter_num:03d}.mp4 and .srt (single copy).
    """
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
            # Standardized padded format (chapter_001.mp4)
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
            # Standardized padded format (chapter_001.srt)
            padded_name = format_chapter_filename(chapter_num, ext="srt", padded=True)
            dest_padded = out_dir / padded_name
            if s_src != dest_padded.resolve():
                shutil.copy2(s_src, dest_padded)
            result["subtitle_padded"] = dest_padded
            result["subtitle"] = dest_padded
            result["subtitle_unpadded"] = dest_padded

    return result


def init_project_layout(project_slug: str, title: str) -> Path:
    """
    Initializes standard project directory structure and default project.json.
    Returns project directory path.
    """
    project_dir = get_project_dir(project_slug)
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "chapters").mkdir(exist_ok=True)

    project_json = project_dir / "project.json"
    if not project_json.exists():
        cfg = ProjectConfig(
            project_id=str(uuid.uuid4()),
            slug=project_slug,
            title=title
        )
        atomic_write_json(project_json, cfg)

    summary_file = project_dir / "story_summary.md"
    if not summary_file.exists():
        initial_summary = (
            f"# Story Summary: {title}\n\n"
            f"## Project Overview\n- **Series Title**: {title}\n- **Slug**: {project_slug}\n\n"
            f"## World Building & Lore\n\n## Major Characters\n\n## Chapter Recaps & Timeline\n"
        )
        summary_file.write_text(initial_summary, encoding="utf-8")

    return project_dir


def init_chapter_layout(project_slug: str, chapter_num: Union[str, int], title: str = "") -> Path:
    """
    Initializes chapter directories (raw, panels, contact_sheets, audio, subtitles, video)
    and chapter.json.
    """
    chap_dir = get_chapter_dir(project_slug, chapter_num)
    for sub in ("raw", "panels", "contact_sheets", "audio", "subtitles", "video", "audio/clips"):
        (chap_dir / sub).mkdir(parents=True, exist_ok=True)

    chapter_json = chap_dir / "chapter.json"
    if not chapter_json.exists():
        meta = ChapterMetadata(
            chapter_id=f"{project_slug}_chap_{chapter_num}",
            chapter_number=str(chapter_num),
            project_slug=project_slug,
            title=title or f"Chapter {chapter_num}"
        )
        atomic_write_json(chapter_json, meta)

    return chap_dir


# ==============================================================================
# 3. Content Fingerprinting & Checksums
# ==============================================================================

def calculate_sha256(content: Union[bytes, str, Path]) -> str:
    """
    Computes streaming SHA-256 hex digest for Path, bytes, or string.
    """
    hasher = hashlib.sha256()

    if isinstance(content, Path):
        if not content.exists() or not content.is_file():
            raise StorageFileNotFoundError(f"File for hashing does not exist: {content}")
        with open(content, "rb") as f:
            while chunk := f.read(65536):
                hasher.update(chunk)
    elif isinstance(content, bytes):
        hasher.update(content)
    elif isinstance(content, str):
        hasher.update(content.encode("utf-8"))
    else:
        raise TypeError(f"Unsupported content type for SHA-256 calculation: {type(content)}")

    return hasher.hexdigest().lower()


compute_file_sha256 = calculate_sha256


def verify_checksum(file_path: Path, expected_hash: str) -> bool:
    """Verifies that a file's SHA-256 hash matches the expected value."""
    actual_hash = calculate_sha256(file_path)
    return actual_hash == expected_hash.strip().lower()


def calculate_utterance_fingerprint(
    text: str,
    voice: str,
    speed: float,
    emotion: Optional[str] = None,
    emotion_cue: Optional[str] = None,
    engine_mode: str = "neural",
) -> str:
    """
    Calculates canonical SHA-256 fingerprint of (text + voice + speed + emotion + cue + engine).
    Guarantees TTS re-synthesis when any input parameter changes.
    """
    raw = f"{text}|{voice}|{speed:.2f}|{emotion or ''}|{emotion_cue or ''}|{engine_mode}"
    return calculate_sha256(raw)



# ==============================================================================
# 4. Atomic Write Engine
# ==============================================================================

def _json_serial_fallback(obj: Any) -> Any:
    """Fallback serializer for objects not natively serializable by json.dumps."""
    if isinstance(obj, (datetime.datetime, datetime.date)):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, Enum):
        return obj.value
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    raise TypeError(f"Type {type(obj)} not serializable to JSON")


def atomic_write_json(file_path: Path, data: Union[BaseModel, Dict[str, Any], List[Any]], indent: int = 2) -> None:
    """
    Writes data to a temporary file in the target directory, performs fsync,
    and replaces the target file atomically via os.replace.
    """
    target = Path(file_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)

    # Convert Pydantic v2 model to JSON-compatible dict
    if isinstance(data, BaseModel):
        payload = data.model_dump(mode="json")
    else:
        payload = data

    json_str = json.dumps(payload, ensure_ascii=False, indent=indent, default=_json_serial_fallback)

    # Create temporary file in the SAME directory to guarantee same filesystem inode rename
    prefix = f".{target.name}.tmp.{os.getpid()}."
    fd, tmp_path_str = tempfile.mkstemp(prefix=prefix, dir=target.parent)
    tmp_path = Path(tmp_path_str)

    try:
        with open(fd, "w", encoding="utf-8") as f:
            f.write(json_str)
            f.flush()
            os.fsync(f.fileno())

        # Atomic inode replacement
        os.replace(tmp_path, target)
    except Exception as exc:
        if tmp_path.exists():
            with contextlib.suppress(Exception):
                tmp_path.unlink()
        raise StorageWriteError(f"Failed to atomically write JSON to {target}: {exc}") from exc


def read_json(file_path: Path) -> Dict[str, Any]:
    """
    Reads and parses a JSON file with error handling.
    Raises StorageFileNotFoundError if missing, StorageCorruptError if invalid.
    """
    target = Path(file_path).resolve()
    if not target.exists():
        raise StorageFileNotFoundError(f"State file not found: {target}")

    try:
        content = target.read_text(encoding="utf-8")
        return json.loads(content)
    except json.JSONDecodeError as exc:
        raise StorageCorruptError(f"Corrupted or invalid JSON in {target}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise StorageCorruptError(f"Invalid UTF-8 encoding in {target}: {exc}") from exc


# ==============================================================================
# 5. Concurrency Control: Single-Writer FileLock
# ==============================================================================

class FileLock:
    """
    Process-safe, single-writer file lock.
    Uses POSIX fcntl.flock on macOS / Linux.
    Supports context manager protocol.
    """

    def __init__(self, resource_path: Path, timeout: float = 10.0, retry_interval: float = 0.05):
        self.resource_path = Path(resource_path).resolve()
        self.lock_path = self.resource_path.with_name(f"{self.resource_path.name}.lock")
        self.timeout = timeout
        self.retry_interval = retry_interval
        self._fd: Optional[int] = None
        self._is_locked = False

    def acquire(self) -> None:
        """Acquires exclusive lock, spinning until timeout."""
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        start_time = time.monotonic()

        while True:
            try:
                # Open lock file descriptor
                self._fd = os.open(str(self.lock_path), os.O_CREAT | os.O_RDWR, 0o666)

                # Attempt non-blocking exclusive lock (POSIX fcntl or Windows msvcrt)
                try:
                    import fcntl
                    fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except ImportError:
                    try:
                        import msvcrt
                        os.lseek(self._fd, 0, os.SEEK_SET)
                        msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
                    except ImportError:
                        pass  # Basic file existence lock fallback

                # Write PID into lock file for debugging
                os.write(self._fd, f"{os.getpid()}\n".encode("utf-8"))
                self._is_locked = True
                return
            except (IOError, OSError) as err:
                if self._fd is not None:
                    with contextlib.suppress(Exception):
                        os.close(self._fd)
                    self._fd = None

                if time.monotonic() - start_time >= self.timeout:
                    raise StorageLockTimeoutError(
                        f"Timed out after {self.timeout}s waiting for exclusive lock on {self.resource_path}"
                    ) from err

                time.sleep(self.retry_interval)

    def release(self) -> None:
        """Releases lock and closes file descriptor."""
        if not self._is_locked or self._fd is None:
            return

        try:
            try:
                import fcntl
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            except ImportError:
                try:
                    import msvcrt
                    os.lseek(self._fd, 0, os.SEEK_SET)
                    msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
                except Exception:
                    pass
            os.close(self._fd)
        finally:
            self._fd = None
            self._is_locked = False

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.release()


def acquire_lock(resource_path: Path, timeout: float = 10.0, retry_interval: float = 0.05) -> FileLock:
    """Convenience factory returning an unentered FileLock."""
    return FileLock(resource_path, timeout=timeout, retry_interval=retry_interval)


# ==============================================================================
# 6. Stage Checkpoint State Machine
# ==============================================================================

# Valid state transitions
VALID_TRANSITIONS: Dict[CheckpointStatus, List[CheckpointStatus]] = {
    CheckpointStatus.PENDING: [CheckpointStatus.RUNNING, CheckpointStatus.COMPLETED],
    CheckpointStatus.RUNNING: [
        CheckpointStatus.COMPLETED,
        CheckpointStatus.FAILED,
        CheckpointStatus.NEEDS_REVIEW,
        CheckpointStatus.RUNNING,
        CheckpointStatus.PENDING,
    ],
    CheckpointStatus.FAILED: [CheckpointStatus.RUNNING, CheckpointStatus.PENDING],
    CheckpointStatus.NEEDS_REVIEW: [CheckpointStatus.RUNNING, CheckpointStatus.COMPLETED],
    CheckpointStatus.COMPLETED: [CheckpointStatus.RUNNING]  # Allowed for force re-runs
}

STAGE_EXECUTION_ORDER: List[StageType] = [
    StageType.CRAWL,
    StageType.SEGMENT,
    StageType.SCRIPT,
    StageType.TTS,
    StageType.RENDER,
    StageType.FINALIZE
]

OPERATIONAL_STAGE_ORDER: List[StageType] = [
    StageType.INGEST,
    StageType.AGENT,
    StageType.AUDIO,
    StageType.RENDER,
    StageType.EXPORT,
]


def update_checkpoint(
    chapter_path: Path,
    stage: Union[str, StageType],
    status: Union[str, CheckpointStatus],
    error: Optional[str] = None,
    artifacts: Optional[List[str]] = None,
    checksums: Optional[Dict[str, str]] = None,
    human_review_status: Optional[Union[str, HumanReviewStatus]] = None,
) -> ChapterMetadata:
    """
    Thread- and process-safe atomic update to chapter.json checkpoints.
    Validates stage transitions and updates timestamps.
    """
    chapter_path = Path(chapter_path)
    chap_file = chapter_path if chapter_path.suffix == ".json" else chapter_path / "chapter.json"
    target_stage = StageType(stage) if isinstance(stage, str) else stage
    target_status = CheckpointStatus(status) if isinstance(status, str) else status

    with acquire_lock(chap_file):
        raw_data = read_json(chap_file)
        meta = ChapterMetadata.model_validate(raw_data)

        stage_cp = meta.stages.get(target_stage)
        if not stage_cp:
            stage_cp = StageCheckpoint(stage=target_stage, status=CheckpointStatus.PENDING)
            meta.stages[target_stage] = stage_cp

        # Validate transition
        allowed = VALID_TRANSITIONS.get(stage_cp.status, [])
        if target_status not in allowed:
            raise InvalidCheckpointTransitionError(
                f"Illegal transition for stage '{target_stage.value}': '{stage_cp.status.value}' -> '{target_status.value}'"
            )

        # Update stage checkpoint fields
        now_str = utc_iso_now()
        stage_cp.status = target_status

        if target_status == CheckpointStatus.RUNNING:
            stage_cp.started_at = now_str
            stage_cp.error_message = None
            meta.current_stage = target_stage
            meta.overall_status = CheckpointStatus.RUNNING
        elif target_status == CheckpointStatus.COMPLETED:
            stage_cp.completed_at = now_str
            stage_cp.error_message = None
            if artifacts:
                stage_cp.artifacts = sorted(list(set(stage_cp.artifacts + artifacts)))
            if checksums:
                stage_cp.checksums.update(checksums)
            
            # Check if all stages completed
            legacy_complete = all(
                meta.stages.get(s) and meta.stages[s].status == CheckpointStatus.COMPLETED
                for s in STAGE_EXECUTION_ORDER
            )
            op_complete = all(
                meta.stages.get(s) and meta.stages[s].status == CheckpointStatus.COMPLETED
                for s in [StageType.INGEST, StageType.AGENT, StageType.AUDIO, StageType.RENDER]
            )
            if legacy_complete or op_complete or target_stage in (StageType.FINALIZE, StageType.EXPORT):
                meta.overall_status = CheckpointStatus.COMPLETED
        elif target_status == CheckpointStatus.FAILED:
            stage_cp.error_message = error or "Unspecified error occurred"
            stage_cp.retry_count += 1
            meta.overall_status = CheckpointStatus.FAILED
        elif target_status == CheckpointStatus.NEEDS_REVIEW:
            stage_cp.error_message = error
            meta.overall_status = CheckpointStatus.NEEDS_REVIEW

        meta.technical_status = meta.overall_status
        if human_review_status is not None:
            meta.human_review_status = (
                HumanReviewStatus(human_review_status)
                if isinstance(human_review_status, str)
                else human_review_status
            )
        meta.updated_at = now_str

        # Commit update
        atomic_write_json(chap_file, meta)
        return meta


def reset_stage_checkpoint(
    chapter_path: Path,
    stage: Union[str, StageType]
) -> ChapterMetadata:
    """
    Resets a stage checkpoint back to PENDING and clears its artifacts and checksums,
    used during downstream artifact invalidation.
    """
    chapter_path = Path(chapter_path)
    chap_file = chapter_path if chapter_path.suffix == ".json" else chapter_path / "chapter.json"
    target_stage = StageType(stage) if isinstance(stage, str) else stage

    with acquire_lock(chap_file):
        raw_data = read_json(chap_file)
        meta = ChapterMetadata.model_validate(raw_data)

        stage_cp = meta.stages.get(target_stage)
        if not stage_cp:
            stage_cp = StageCheckpoint(stage=target_stage, status=CheckpointStatus.PENDING)
            meta.stages[target_stage] = stage_cp
        else:
            stage_cp.status = CheckpointStatus.PENDING
            stage_cp.started_at = None
            stage_cp.completed_at = None
            stage_cp.error_message = None
            stage_cp.artifacts = []
            stage_cp.checksums = {}

        if meta.overall_status == CheckpointStatus.COMPLETED:
            meta.overall_status = CheckpointStatus.PENDING
        meta.updated_at = utc_iso_now()

        atomic_write_json(chap_file, meta)
        return meta



def get_resumable_stage(chapter_path: Path) -> Optional[StageType]:
    """
    Inspects chapter.json to find the first stage requiring execution.
    Returns None if all stages are completed.
    """
    chapter_path = Path(chapter_path)
    chap_file = chapter_path if chapter_path.suffix == ".json" else chapter_path / "chapter.json"
    raw_data = read_json(chap_file)
    meta = ChapterMetadata.model_validate(raw_data)

    if meta.overall_status == CheckpointStatus.COMPLETED:
        return None

    # Determine whether chapter uses operational 5-stages or legacy stages
    has_op = any(
        meta.stages.get(s) and meta.stages[s].status != CheckpointStatus.PENDING
        for s in (StageType.INGEST, StageType.AGENT, StageType.AUDIO)
    )
    order_to_check = OPERATIONAL_STAGE_ORDER if has_op else STAGE_EXECUTION_ORDER

    for stage in order_to_check:
        cp = meta.stages.get(stage)
        if not cp or cp.status in (
            CheckpointStatus.PENDING,
            CheckpointStatus.FAILED,
            CheckpointStatus.NEEDS_REVIEW,
            CheckpointStatus.RUNNING,
        ):
            return stage
    return None


# ==============================================================================
# 7. Intermediate Asset Cleanup & Storage Manager Facade
# ==============================================================================

def cleanup_intermediate_files(chapter_dir: Path, keep_temp: bool = False) -> List[str]:
    """
    Cleanly unlinks known temporary video/subtitle/concat files if keep_temp is False.
    Returns the list of unlinked filenames.
    """
    if keep_temp or not chapter_dir.exists():
        return []
    cleaned: List[str] = []
    seen = set()
    patterns = ["*_raw_temp*.mp4", "*_subs_temp*.srt", "*concat_list*.txt", ".tmp_concat*", "*.tmp"]
    for pattern in patterns:
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
    Safely cleans intermediate temporary files (split clips, temp concat files, temp cached frames,
    intermediate audio/video clips, and raw video concats)
    ONLY after verifying that output_video_path exists, is a valid file, and has size >= min_size_bytes.

    Safety Guarantees:
    - If output_video_path is missing, empty, or smaller than min_size_bytes, ABORTS and raises
      StorageError without deleting any files!
    - NEVER deletes raw images (in raw/), panels (in panels/), manifest.json, script.json, or chapter.json.
    - Preserves metadata, final audio, and final subtitles.
    - Cleans temporary split clips (*_raw_temp*.mp4, temp_clip_*.mp4, segment_*.mp4 in temp directories),
      concat files (*concat_list*.txt, .tmp_concat*), and temporary frames.
    - When clean_clips=True, also unlinks intermediate clips in video/clips/, raw_video_*.mp4,
      intermediate audio clips in audio/clips/, and speech_master_*.wav.

    Returns the list of unlinked filenames.
    """
    chapter_dir = Path(chapter_dir).resolve()
    if output_video_path is None:
        raise StorageError("Safe cleanup aborted: output_video_path is None.")

    out_vid = Path(output_video_path).resolve()

    # 1. Output verification check
    if not out_vid.exists() or not out_vid.is_file():
        raise StorageError(
            f"Safe cleanup aborted: output video does not exist or is not a file: {out_vid}"
        )

    file_size = out_vid.stat().st_size
    if file_size < min_size_bytes:
        raise StorageError(
            f"Safe cleanup aborted: output video {out_vid} size ({file_size} bytes) is below minimum threshold ({min_size_bytes} bytes)."
        )

    if not chapter_dir.exists():
        return []

    # 2. Protected filenames and directories
    PROTECTED_FILENAMES = {
        "manifest.json",
        "script.json",
        "chapter.json",
        "project.json",
        "story_summary.md",
        "characters.json",
        "voice_selection.json",
        "preview_approval.json",
    }

    TEMP_PATTERNS = [
        "*_raw_temp*.mp4",
        "*_subs_temp*.srt",
        "*concat_list*.txt",
        ".tmp_concat*",
        "*.tmp",
    ]

    cleaned: List[str] = []
    seen = set()

    raw_dir = (chapter_dir / "raw").resolve()
    panels_dir = (chapter_dir / "panels").resolve()

    # First clean files matching known temp patterns across the chapter
    for pattern in TEMP_PATTERNS:
        for p in chapter_dir.rglob(pattern):
            if p in seen or not p.is_file():
                continue
            if p.name in PROTECTED_FILENAMES:
                continue
            # NEVER delete anything inside raw/ or panels/
            try:
                if p.resolve().is_relative_to(raw_dir) or p.resolve().is_relative_to(panels_dir):
                    continue
            except (ValueError, AttributeError):
                if "raw" in p.parts or "panels" in p.parts:
                    continue
            seen.add(p)
            try:
                p.unlink(missing_ok=True)
                cleaned.append(str(p.name))
            except Exception as e:
                with contextlib.suppress(Exception):
                    from src.core.logger import logger
                    logger.warning(f"Failed to remove temp file {p}: {e}")

    # Also clean temporary split clips and cached frames in video/temp_clips, video/temp, video/frames
    for temp_sub in ("temp_clips", "temp", "temp_frames", "frames"):
        temp_dir = chapter_dir / "video" / temp_sub
        if temp_dir.exists() and temp_dir.is_dir():
            for p in list(temp_dir.iterdir()):
                if p in seen or not p.is_file():
                    continue
                if p.name in PROTECTED_FILENAMES:
                    continue
                seen.add(p)
                try:
                    p.unlink(missing_ok=True)
                    cleaned.append(str(p.name))
                except Exception as e:
                    with contextlib.suppress(Exception):
                        from src.core.logger import logger
                        logger.warning(f"Failed to remove temp clip {p}: {e}")
            with contextlib.suppress(Exception):
                if not any(temp_dir.iterdir()):
                    temp_dir.rmdir()

    # When clean_clips is True, safely clean intermediate video clips, raw concat video, and audio clips
    if clean_clips:
        # 1. Video clips in video/clips/
        v_clips_dir = chapter_dir / "video" / "clips"
        if v_clips_dir.exists() and v_clips_dir.is_dir():
            for p in list(v_clips_dir.iterdir()):
                if p in seen or not p.is_file():
                    continue
                if p.name in PROTECTED_FILENAMES:
                    continue
                seen.add(p)
                try:
                    p.unlink(missing_ok=True)
                    cleaned.append(str(p.name))
                except Exception as e:
                    with contextlib.suppress(Exception):
                        from src.core.logger import logger
                        logger.warning(f"Failed to remove video clip {p}: {e}")
            with contextlib.suppress(Exception):
                if not any(v_clips_dir.iterdir()):
                    v_clips_dir.rmdir()

        # 2. Raw concatenated videos in video/
        v_dir = chapter_dir / "video"
        if v_dir.exists() and v_dir.is_dir():
            for p in v_dir.glob("raw_video_*.mp4"):
                if p in seen or not p.is_file():
                    continue
                seen.add(p)
                try:
                    p.unlink(missing_ok=True)
                    cleaned.append(str(p.name))
                except Exception:
                    pass

        # 3. Intermediate audio clips in audio/clips/
        a_clips_dir = chapter_dir / "audio" / "clips"
        if a_clips_dir.exists() and a_clips_dir.is_dir():
            for p in list(a_clips_dir.iterdir()):
                if p in seen or not p.is_file():
                    continue
                if p.name in PROTECTED_FILENAMES:
                    continue
                seen.add(p)
                try:
                    p.unlink(missing_ok=True)
                    cleaned.append(str(p.name))
                except Exception as e:
                    with contextlib.suppress(Exception):
                        from src.core.logger import logger
                        logger.warning(f"Failed to remove audio clip {p}: {e}")
            with contextlib.suppress(Exception):
                if not any(a_clips_dir.iterdir()):
                    a_clips_dir.rmdir()

        # 4. Intermediate uncompressed speech master WAVs in audio/
        a_dir = chapter_dir / "audio"
        if a_dir.exists() and a_dir.is_dir():
            for p in a_dir.glob("speech_master_*.wav"):
                if p in seen or not p.is_file():
                    continue
                seen.add(p)
                try:
                    p.unlink(missing_ok=True)
                    cleaned.append(str(p.name))
                except Exception:
                    pass

    return cleaned


def load_chapter_metadata(chapter_dir: Path) -> ChapterMetadata:
    """Loads and validates chapter.json for the given chapter directory or file path."""
    chap_file = chapter_dir if chapter_dir.suffix == ".json" else chapter_dir / "chapter.json"
    if not chap_file.exists():
        raise StorageFileNotFoundError(f"Chapter metadata not found at {chap_file}")
    data = read_json(chap_file)
    return ChapterMetadata.model_validate(data)


def load_recap_script(chapter_dir: Path) -> RecapScript:
    """Loads and validates script.json for the given chapter directory or file path."""
    script_file = chapter_dir if chapter_dir.suffix == ".json" else chapter_dir / "script.json"
    if not script_file.exists():
        raise StorageFileNotFoundError(f"Recap script not found at {script_file}")
    data = read_json(script_file)
    return RecapScript.model_validate(data)


def save_recap_script(chapter_dir: Path, script: Union[RecapScript, Dict[str, Any]]) -> None:
    """Atomically writes script.json for the given chapter directory or file path."""
    script_file = chapter_dir if chapter_dir.suffix == ".json" else chapter_dir / "script.json"
    atomic_write_json(script_file, script)


def load_manifest(chapter_dir: Path) -> Optional[PanelManifest]:
    """
    Loads and validates manifest.json for the given chapter directory or file path.
    Returns None if manifest.json does not exist.
    """
    chapter_dir = Path(chapter_dir)
    manifest_file = chapter_dir if chapter_dir.suffix == ".json" else chapter_dir / "manifest.json"
    if not manifest_file.exists():
        return None
    data = read_json(manifest_file)
    return PanelManifest.model_validate(data)


def save_manifest(chapter_dir: Path, manifest: Union[PanelManifest, Dict[str, Any]]) -> Path:
    """
    Atomically writes manifest.json for the given chapter directory or file path.
    Returns the resolved Path to the saved manifest.json.
    """
    chapter_dir = Path(chapter_dir)
    manifest_file = chapter_dir if chapter_dir.suffix == ".json" else chapter_dir / "manifest.json"
    atomic_write_json(manifest_file, manifest)
    return manifest_file


class StorageManager:
    """Convenience facade for storage management, paths, checkpoints, and cleanup."""

    @staticmethod
    def cleanup_intermediate_files(chapter_dir: Path, keep_temp: bool = False) -> List[str]:
        return cleanup_intermediate_files(chapter_dir, keep_temp=keep_temp)

    @staticmethod
    def safe_cleanup_chapter_intermediates(
        chapter_dir: Path,
        output_video_path: Path,
        min_size_bytes: int = 100000,
        clean_clips: bool = True,
    ) -> List[str]:
        return safe_cleanup_chapter_intermediates(
            chapter_dir,
            output_video_path=output_video_path,
            min_size_bytes=min_size_bytes,
            clean_clips=clean_clips,
        )

    @staticmethod
    def get_projects_root() -> Path:
        return get_projects_root()

    @staticmethod
    def get_project_dir(project_slug: str) -> Path:
        return get_project_dir(project_slug)

    @staticmethod
    def get_chapter_dir(project_slug: str, chapter_num: Union[str, int]) -> Path:
        return get_chapter_dir(project_slug, chapter_num)

    @staticmethod
    def get_output_dir(project_slug: str) -> Path:
        return get_output_dir(project_slug)

    @staticmethod
    def get_workspace_dir(project_slug: str, chapter_num: Optional[Union[str, int]] = None) -> Path:
        return get_workspace_dir(project_slug, chapter_num)

    @staticmethod
    def format_chapter_filename(chapter_num: Union[int, str], ext: str = "mp4", padded: bool = True) -> str:
        return format_chapter_filename(chapter_num, ext=ext, padded=padded)

    @staticmethod
    def get_chapter_export_path(project_slug: str, chapter_num: Union[int, str], ext: str = "mp4", padded: bool = True) -> Path:
        return get_chapter_export_path(project_slug, chapter_num, ext=ext, padded=padded)

    @staticmethod
    def stage_chapter_export(
        project_slug: str,
        chapter_num: Union[int, str],
        video_path: Optional[Union[str, Path]] = None,
        subtitle_path: Optional[Union[str, Path]] = None,
    ) -> Dict[str, Optional[Path]]:
        return stage_chapter_export(project_slug, chapter_num, video_path=video_path, subtitle_path=subtitle_path)

    @staticmethod
    def init_project_layout(project_slug: str, title: str) -> Path:
        return init_project_layout(project_slug, title)

    @staticmethod
    def init_chapter_layout(project_slug: str, chapter_num: Union[str, int], title: str = "") -> Path:
        return init_chapter_layout(project_slug, chapter_num, title=title)

    @staticmethod
    def get_resumable_stage(chapter_path: Path) -> Optional[StageType]:
        return get_resumable_stage(chapter_path)

    @staticmethod
    def update_checkpoint(
        chapter_path: Path,
        stage: Union[str, StageType],
        status: Union[str, CheckpointStatus],
        error: Optional[str] = None,
        artifacts: Optional[List[str]] = None,
        checksums: Optional[Dict[str, str]] = None
    ) -> ChapterMetadata:
        return update_checkpoint(chapter_path, stage, status, error, artifacts, checksums)

    @staticmethod
    def load_manifest(chapter_dir: Path) -> Optional[PanelManifest]:
        return load_manifest(chapter_dir)

    @staticmethod
    def save_manifest(chapter_dir: Path, manifest: Union[PanelManifest, Dict[str, Any]]) -> Path:
        return save_manifest(chapter_dir, manifest)


