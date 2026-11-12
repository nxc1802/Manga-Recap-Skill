"""Export reviewable JSON Schema. Run --check in CI to detect contract drift."""
import argparse
import json
from pathlib import Path
from src.core.series_setup import SeriesSetupAnswers
from src.core.schemas import ProjectConfig, PanelManifest, RecapScript
from src.core.visual_contract import VisualPlan, Shot
from src.core.human_review import HumanApproval
from src.core.contracts import ArchiveReceipt, YouTubeReceipt, ReleaseRecord, TombstoneRecord

MODELS = dict(project=ProjectConfig, series_setup=SeriesSetupAnswers, script=RecapScript, panel_evidence=PanelManifest,
              visual_plan=VisualPlan, shot=Shot, preview_approval=HumanApproval,
              archive_receipt=ArchiveReceipt, youtube_receipt=YouTubeReceipt, release_state=ReleaseRecord,
              tombstone=TombstoneRecord)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1] / "schemas" / "v4"
    root.mkdir(parents=True, exist_ok=True)
    for name, model in MODELS.items():
        path = root / f"{name}.schema.json"
        expected = json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2) + "\n"
        if args.check:
            if not path.exists() or path.read_text() != expected:
                raise SystemExit(f"Schema drift: {path}. Version and regenerate intentionally.")
        else:
            path.write_text(expected)


if __name__ == "__main__":
    main()
