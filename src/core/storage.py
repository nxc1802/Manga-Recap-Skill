"""
src/core/storage.py
Atomic JSON storage, streaming SHA-256 hashing, and cross-platform FileLock.
Project paths, checkpoints, and cleanup now live in dedicated modules
(`src.core.project`, `src.core.checkpoints`, `src.core.cleanup`); lazy
attribute lookup preserves backward-compatible imports cleanly.
"""

from __future__ import annotations

import contextlib
import datetime
from enum import Enum
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Union

from pydantic import BaseModel

from src.core.errors import (  # noqa: F401
    InvalidCheckpointTransitionError,
    StorageCorruptError,
    StorageError,
    StorageFileNotFoundError,
    StorageLockError,
    StorageLockTimeoutError,
    StorageWriteError,
)
from src.core.version import PRODUCTION_SCHEMA_VERSION


# ==============================================================================
# Content Fingerprinting & Checksums
# ==============================================================================

def calculate_sha256(content: Union[bytes, str, Path]) -> str:
    """Computes streaming SHA-256 hex digest for Path, bytes, or string."""
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
    emotion: str | None = None,
    emotion_cue: str | None = None,
    engine_mode: str = "neural",
) -> str:
    """Calculates canonical SHA-256 fingerprint of TTS utterance parameters."""
    raw = json.dumps(
        [PRODUCTION_SCHEMA_VERSION, text, voice, float(speed), emotion or "", emotion_cue or "", engine_mode],
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    return calculate_sha256(raw)


# ==============================================================================
# Atomic JSON I/O
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


def atomic_write_json(
    file_path: Path,
    data: Union[BaseModel, Dict[str, Any], List[Any]],
    indent: int = 2,
) -> None:
    """Writes JSON to a temp file in the target directory, fsyncs, and replaces atomically."""
    target = Path(file_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)

    payload = data.model_dump(mode="json") if isinstance(data, BaseModel) else data
    json_str = json.dumps(payload, ensure_ascii=False, indent=indent, default=_json_serial_fallback)

    prefix = f".{target.name}.tmp.{os.getpid()}."
    fd, tmp_path_str = tempfile.mkstemp(prefix=prefix, dir=target.parent)
    tmp_path = Path(tmp_path_str)

    try:
        with open(fd, "w", encoding="utf-8") as f:
            f.write(json_str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target)
    except Exception as exc:
        if tmp_path.exists():
            with contextlib.suppress(Exception):
                tmp_path.unlink()
        raise StorageWriteError(f"Failed to atomically write JSON to {target}: {exc}") from exc


def atomic_write_json_pair(first_path: Path, first_data: Any, second_path: Path, second_data: Any) -> None:
    """Prepare and fsync both JSON files before publishing either revision."""
    prepared = []
    try:
        for target, payload in ((Path(first_path), first_data), (Path(second_path), second_data)):
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(payload, BaseModel):
                payload = payload.model_dump(mode="json")
            fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.prepared.", dir=target.parent)
            prepared.append((Path(tmp), target))
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, default=_json_serial_fallback)
                handle.flush()
                os.fsync(handle.fileno())
        for tmp, target in prepared:
            os.replace(tmp, target)
            dir_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    except Exception as exc:
        raise StorageWriteError(f"Setup transaction failed: {exc}") from exc
    finally:
        for tmp, _ in prepared:
            tmp.unlink(missing_ok=True)


def read_json(file_path: Path) -> Dict[str, Any]:
    """Reads and parses a JSON file, raising StorageFileNotFoundError or StorageCorruptError."""
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
# Concurrency Control: Single-Writer FileLock
# ==============================================================================

class FileLock:
    """Process-safe, single-writer file lock via fcntl.flock (POSIX) or msvcrt (Windows)."""

    def __init__(self, resource_path: Path, timeout: float = 10.0, retry_interval: float = 0.05):
        self.resource_path = Path(resource_path).resolve()
        self.lock_path = self.resource_path.with_name(f"{self.resource_path.name}.lock")
        self.timeout = timeout
        self.retry_interval = retry_interval
        self._fd: int | None = None
        self._is_locked = False

    def acquire(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        start_time = time.monotonic()

        while True:
            try:
                self._fd = os.open(str(self.lock_path), os.O_CREAT | os.O_RDWR, 0o666)
                try:
                    import fcntl
                    fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except ImportError:
                    try:
                        import msvcrt
                        os.lseek(self._fd, 0, os.SEEK_SET)
                        msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
                    except ImportError:
                        pass

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
# Compatibility Re-exports (lazy to avoid circular imports)
# ==============================================================================

_PROJECT_EXPORTS = frozenset({
    "format_chapter_filename",
    "get_chapter_dir",
    "get_chapter_export_path",
    "get_output_dir",
    "get_project_dir",
    "get_projects_root",
    "get_workspace_dir",
    "get_youtube_accounts_dir",
    "init_chapter_layout",
    "init_project_layout",
    "load_chapter_metadata",
    "load_manifest",
    "load_project_config",
    "load_recap_script",
    "resolve_existing_chapter_dir",
    "resolve_existing_project_dir",
    "save_chapter_metadata",
    "save_manifest",
    "save_project_config",
    "save_recap_script",
    "stage_chapter_export",
})

_CHECKPOINT_EXPORTS = frozenset({
    "OPERATIONAL_STAGE_ORDER",
    "STAGE_EXECUTION_ORDER",
    "VALID_TRANSITIONS",
    "get_resumable_stage",
    "mark_chapter_human_approved",
    "record_chapter_render_result",
    "reset_stage_checkpoint",
    "update_checkpoint",
})

_CLEANUP_EXPORTS = frozenset({
    "clean_project_chapters",
    "cleanup_intermediate_files",
    "safe_cleanup_chapter_intermediates",
})


def __getattr__(name: str) -> Any:
    if name in _PROJECT_EXPORTS:
        from src.core import project
        return getattr(project, name)
    if name in _CHECKPOINT_EXPORTS:
        from src.core import checkpoints
        return getattr(checkpoints, name)
    if name in _CLEANUP_EXPORTS:
        from src.core import cleanup
        return getattr(cleanup, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
