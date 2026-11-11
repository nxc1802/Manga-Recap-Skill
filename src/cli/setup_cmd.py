"""
src/cli/setup_cmd.py
Subcommand handler: manga-recap setup [validate|record|show|migrate]
Enforces Strict Form 10-question user configuration gate and lifecycle management.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Optional

from src.cli.exit_codes import CliValidationError
from src.core.logger import setup_logger
from src.core.series_setup import (
    ALL_QUESTION_IDS,
    QUESTION_METADATA,
    SeriesSetupAnswers,
    migrate_legacy_setup,
    record_series_setup,
    validate_complete_setup,
)
from src.core.storage import get_project_dir, read_json

logger = setup_logger("setup_cmd")


def _load_answers_payload(answers_arg: str) -> Dict[str, Any]:
    """Loads answers payload from either a JSON file path or inline JSON string."""
    answers_path = Path(answers_arg)
    if answers_path.exists() and answers_path.is_file():
        try:
            return read_json(answers_path)
        except Exception as e:
            raise CliValidationError(f"Failed to read answers JSON file '{answers_path}': {e}") from e

    # Try parsing as inline JSON string
    try:
        data = json.loads(answers_arg)
        if isinstance(data, dict):
            return data
        raise CliValidationError("Answers payload must be a JSON object.")
    except json.JSONDecodeError as e:
        raise CliValidationError(
            f"Invalid answers JSON: string is neither an existing file path nor valid JSON: {e}"
        ) from e


def handle_setup(args: argparse.Namespace) -> Dict[str, Any]:
    """Dispatches manga-recap setup actions: validate, record, show, migrate."""
    action = getattr(args, "setup_action", "validate")
    project_slug = getattr(args, "project", "")
    if not project_slug:
        raise CliValidationError("Project slug (--project) is required for setup commands.")

    proj_dir = get_project_dir(project_slug)

    if action == "validate":
        answers_file = proj_dir / "series_setup_answers.json"
        if not answers_file.exists():
            missing_errors = [
                f"Missing question {qid} ({QUESTION_METADATA[qid]['title']})"
                for qid in ALL_QUESTION_IDS
            ]
            raise CliValidationError(
                f"Strict Form Gate failed: Series '{project_slug}' is missing required user configuration.",
                details=missing_errors,
                data={
                    "project_slug": project_slug,
                    "valid": False,
                    "total_checks": 10,
                    "passed": 0,
                    "errors": missing_errors,
                },
            )

        try:
            raw_data = read_json(answers_file)
            setup_obj = SeriesSetupAnswers.model_validate(raw_data)
        except Exception as e:
            raise CliValidationError(
                f"Strict Form Gate failed: Series '{project_slug}' setup file is corrupt: {e}",
                details=[str(e)],
                data={
                    "project_slug": project_slug,
                    "valid": False,
                    "errors": [str(e)],
                },
            ) from e

        is_valid, errors = validate_complete_setup(setup_obj)
        if not is_valid:
            raise CliValidationError(
                f"Strict Form Gate failed: Series '{project_slug}' setup validation failed.",
                details=errors,
                data={
                    "project_slug": project_slug,
                    "valid": False,
                    "total_checks": 10,
                    "passed": max(0, 10 - len(errors)),
                    "errors": errors,
                },
            )

        return {
            "status": "success",
            "command": "setup validate",
            "project_slug": project_slug,
            "valid": True,
            "total_checks": 10,
            "passed": 10,
            "snapshot_hash": setup_obj.snapshot_hash,
            "setup_revision": setup_obj.setup_revision,
            "legacy_migrated": setup_obj.legacy_migrated,
            "reviewer": setup_obj.reviewer,
        }

    elif action == "record":
        answers_arg = getattr(args, "answers_json", None)
        if not answers_arg:
            raise CliValidationError("--answers-json (or --answers) is required for 'setup record'.")

        payload = _load_answers_payload(answers_arg)
        reviewer = getattr(args, "reviewer", "user") or "user"

        saved_setup = record_series_setup(
            project_slug=project_slug,
            answers_data=payload,
            reviewer=reviewer,
            project_dir=proj_dir,
        )

        return {
            "status": "success",
            "command": "setup record",
            "project_slug": project_slug,
            "setup_revision": saved_setup.setup_revision,
            "snapshot_hash": saved_setup.snapshot_hash,
            "reviewer": saved_setup.reviewer,
            "user_confirmed_at": saved_setup.user_confirmed_at,
            "total_questions": len(saved_setup.answers),
            "answers_path": str(proj_dir / "series_setup_answers.json"),
        }

    elif action == "show":
        answers_file = proj_dir / "series_setup_answers.json"
        if not answers_file.exists():
            raise CliValidationError(
                f"Series '{project_slug}' does not have a recorded series_setup_answers.json.",
                data={"project_slug": project_slug, "exists": False},
            )

        raw_data = read_json(answers_file)
        setup_obj = SeriesSetupAnswers.model_validate(raw_data)

        return {
            "status": "success",
            "command": "setup show",
            "project_slug": project_slug,
            "setup_revision": setup_obj.setup_revision,
            "snapshot_hash": setup_obj.snapshot_hash,
            "reviewer": setup_obj.reviewer,
            "created_at": setup_obj.created_at,
            "updated_at": setup_obj.updated_at,
            "user_confirmed_at": setup_obj.user_confirmed_at,
            "legacy_migrated": setup_obj.legacy_migrated,
            "answers": {
                qid: item.model_dump()
                for qid, item in setup_obj.answers.items()
            },
        }

    elif action == "migrate":
        reviewer = getattr(args, "reviewer", "legacy_migrator") or "legacy_migrator"
        answers_arg = getattr(args, "answers_json", None)
        overrides = None
        if answers_arg:
            overrides = _load_answers_payload(answers_arg)

        migrated = migrate_legacy_setup(
            project_slug=project_slug,
            reviewer=reviewer,
            project_dir=proj_dir,
            overrides=overrides,
        )

        return {
            "status": "success",
            "command": "setup migrate",
            "project_slug": project_slug,
            "setup_revision": migrated.setup_revision,
            "snapshot_hash": migrated.snapshot_hash,
            "reviewer": migrated.reviewer,
            "legacy_migrated": True,
            "total_questions": len(migrated.answers),
            "answers_path": str(proj_dir / "series_setup_answers.json"),
        }

    else:
        raise CliValidationError(f"Unknown setup action: {action}")
