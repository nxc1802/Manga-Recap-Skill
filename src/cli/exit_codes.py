"""
src/cli/exit_codes.py
Standard POSIX-aligned exit codes for Manga Recap Deterministic CLI Suite.
"""

from enum import IntEnum


class ExitCode(IntEnum):
    SUCCESS = 0             # Successful completion of requested command
    GENERAL_ERROR = 1       # Unhandled runtime exception or general operational failure
    VALIDATION_ERROR = 2    # CLI argument errors, Pydantic schema validation failures, corrupt JSON
    IO_ERROR = 3            # Missing files/directories, storage write errors, permission issues
    DEPENDENCY_ERROR = 4    # Missing binary dependencies (ffmpeg, ffprobe, espeak, vieneu models)


class CliValidationError(Exception):
    """Raised when data or argument validation fails."""
    def __init__(self, message: str, details=None, data=None):
        super().__init__(message)
        self.details = details
        self.data = data

