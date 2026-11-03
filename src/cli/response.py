"""
src/cli/response.py
Structured JSON output envelope and response printer for Manga Recap CLI suite.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Dict, Optional, Union
from pydantic import BaseModel, Field

from src.cli.exit_codes import CliValidationError, ExitCode


class CliErrorDetail(BaseModel):
    code: str
    message: str
    details: Optional[Any] = None


class JsonEnvelope(BaseModel):
    status: str = Field(..., description="'success' or 'error'")
    command: str
    exit_code: int
    data: Optional[Dict[str, Any]] = None
    error: Optional[CliErrorDetail] = None
    duration_seconds: float


def format_human_readable_data(data: Any, indent: int = 0) -> str:
    """Formats arbitrary nested dictionary or list for human-readable terminal output."""
    prefix = "  " * indent
    if isinstance(data, dict):
        lines = []
        for k, v in data.items():
            if isinstance(v, (dict, list)):
                lines.append(f"{prefix}{k}:")
                lines.append(format_human_readable_data(v, indent + 1))
            else:
                lines.append(f"{prefix}{k}: {v}")
        return "\n".join(lines)
    elif isinstance(data, list):
        lines = []
        for item in data:
            if isinstance(item, (dict, list)):
                lines.append(f"{prefix}-")
                lines.append(format_human_readable_data(item, indent + 1))
            else:
                lines.append(f"{prefix}- {item}")
        return "\n".join(lines)
    return f"{prefix}{data}"


def print_response(
    command: str,
    exit_code: Union[ExitCode, int],
    data: Optional[Dict[str, Any]] = None,
    error: Optional[Union[Dict[str, Any], CliErrorDetail]] = None,
    duration_seconds: float = 0.0,
    json_mode: bool = False,
) -> None:
    """
    Emits formatted CLI response.
    When json_mode=True: Emits strictly valid JSON envelope to sys.stdout.
    When json_mode=False: Emits clean human summary to stdout (success) or stderr (error).
    """
    code_val = int(exit_code)
    status_str = "success" if code_val == ExitCode.SUCCESS else "error"

    error_detail: Optional[CliErrorDetail] = None
    if error is not None:
        if isinstance(error, CliErrorDetail):
            error_detail = error
        elif isinstance(error, dict):
            error_detail = CliErrorDetail(
                code=str(error.get("code", "GENERAL_ERROR")),
                message=str(error.get("message", "An error occurred")),
                details=error.get("details"),
            )
        else:
            error_detail = CliErrorDetail(code="GENERAL_ERROR", message=str(error))

    envelope = JsonEnvelope(
        status=status_str,
        command=command,
        exit_code=code_val,
        data=data,
        error=error_detail,
        duration_seconds=round(duration_seconds, 4),
    )

    if json_mode:
        output_str = json.dumps(envelope.model_dump(), ensure_ascii=False, indent=2)
        sys.stdout.write(output_str + "\n")
        sys.stdout.flush()
    else:
        if code_val == ExitCode.SUCCESS:
            sys.stdout.write(f"[{command.upper()}] Success ({duration_seconds:.2f}s)\n")
            if data:
                sys.stdout.write(format_human_readable_data(data) + "\n")
            sys.stdout.flush()
        else:
            err_msg = error_detail.message if error_detail else "Unknown error"
            err_code = error_detail.code if error_detail else "ERROR"
            sys.stderr.write(f"[{command.upper()} ERROR] ({err_code}): {err_msg}\n")
            if error_detail and error_detail.details:
                sys.stderr.write(format_human_readable_data(error_detail.details, indent=1) + "\n")
            sys.stderr.flush()
