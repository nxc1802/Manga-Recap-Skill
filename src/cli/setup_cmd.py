"""Show, validate, or record the series setup."""
import json
from pathlib import Path

from src.cli.exit_codes import CliValidationError
from src.core.series_setup import ALL_QUESTION_IDS, record_series_setup, require_series_setup
from src.core.storage import get_project_dir, read_json


def handle_setup(args):
    action, slug = args.setup_action, args.project
    root = get_project_dir(slug)
    if action == "record":
        answer = getattr(args, "answers_json", None)
        if not answer:
            raise CliValidationError("--answers-json is required for setup record")
        try:
            payload = json.loads(answer) if answer.lstrip().startswith("{") else read_json(Path(answer))
        except (OSError, ValueError) as exc:
            raise CliValidationError(f"Could not read setup answers: {exc}") from exc
        if not isinstance(payload, dict):
            raise CliValidationError("Setup answers must be a JSON object")
        setup = record_series_setup(slug, payload, reviewer=args.reviewer or "user", project_dir=root)
    else:
        setup = require_series_setup(slug, project_dir=root)
    return {
        "status": "success", "command": f"setup {action}", "project_slug": slug, "valid": True,
        "total_checks": len(ALL_QUESTION_IDS), "passed": len(ALL_QUESTION_IDS),
        "total_questions": len(ALL_QUESTION_IDS), "answers_path": str(root / "series_setup_answers.json"),
        **setup.model_dump(mode="json"),
    }
