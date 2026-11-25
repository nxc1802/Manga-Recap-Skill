"""Domain exception hierarchy decoupled from CLI exit codes."""

from __future__ import annotations

from typing import Any, Optional


class MangaRecapError(Exception):
    """Base domain exception for Manga Recap."""

    def __init__(self, message: str, details: Any = None, data: Any = None):
        super().__init__(message)
        self.details = details
        self.data = data


class DomainValidationError(MangaRecapError):
    """Raised when configuration, gate, schema, or input validation fails."""


class DependencyNotFoundError(MangaRecapError):
    """Raised when an external binary or runtime dependency is missing."""


class FFmpegExecutionError(MangaRecapError):
    """Raised when FFmpeg/FFprobe subprocess returns a non-zero exit code."""

    def __init__(self, message: str, returncode: int, stderr: str = ""):
        tail = stderr.strip()[-500:] if stderr else ""
        super().__init__(f"{message} (exit code {returncode}): {tail}")
        self.returncode = returncode
        self.stderr = stderr


class StorageError(MangaRecapError):
    """Base exception for storage engine errors."""


class StorageFileNotFoundError(StorageError, FileNotFoundError):
    """Raised when a requested state or data file does not exist."""


class StorageCorruptError(StorageError, ValueError):
    """Raised when a JSON file contains invalid or unparseable syntax."""


class StorageWriteError(StorageError, IOError):
    """Raised when an atomic write fails to commit."""


class StorageLockError(StorageError):
    """Base exception for file locking errors."""


class StorageLockTimeoutError(StorageLockError):
    """Raised when an exclusive lock cannot be acquired within the timeout period."""


class InvalidCheckpointTransitionError(StorageError):
    """Raised when an illegal stage status transition is attempted."""


class ProviderError(MangaRecapError):
    """Base exception for external provider integrations."""

    def __init__(
        self,
        message: str,
        *,
        category: str = "provider_error",
        provider: Optional[str] = None,
        retryable: bool = False,
        details: Any = None,
        data: Any = None,
    ):
        super().__init__(message, details=details, data=data)
        self.category = category
        self.provider = provider
        self.retryable = retryable


class ProviderAuthError(ProviderError):
    """Raised on authentication or authorization failures."""


class ProviderRateLimitError(ProviderError):
    """Raised when provider rate limits (e.g. HTTP 429) are hit."""


class ProviderQuotaError(ProviderError):
    """Raised when account quota is exhausted."""


class ProviderTimeoutError(ProviderError):
    """Raised when a provider queue or request times out."""


class ProviderExecutionError(ProviderError):
    """Raised when a remote provider job fails during execution."""


class ProviderIntegrityError(ProviderError):
    """Raised when provider payloads, receipts, or results fail verification."""
