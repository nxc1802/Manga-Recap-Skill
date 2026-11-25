"""Standard POSIX-aligned exit codes for Manga Recap CLI."""

from enum import IntEnum

from src.core.errors import DomainValidationError


class ExitCode(IntEnum):
    SUCCESS = 0             # Successful completion of requested command
    GENERAL_ERROR = 1       # Unhandled runtime exception or general operational failure
    VALIDATION_ERROR = 2    # CLI argument errors, Pydantic schema validation failures, corrupt JSON
    IO_ERROR = 3            # Missing files/directories, storage write errors, permission issues
    DEPENDENCY_ERROR = 4    # Missing binary dependencies (ffmpeg, ffprobe, espeak, vieneu models)


# Compatibility alias so CLI and tests treat domain validation errors identically.
CliValidationError = DomainValidationError
