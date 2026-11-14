"""Single-session Kaggle worker. Same TTS, planner, renderer and validators as local."""
import argparse
import json
import shutil
from pathlib import Path
from src.cloud.payload import verify_payload_manifest
from src.core.storage import read_json, atomic_write_json
from src.core.series_setup import require_series_setup
from src.cli.tts_cmd import handle_tts
from src.render.chapter_renderer import render_chapter
from src.render.artifact_validation import fingerprint


def run(root, manifest_hash, output_dir, encoder="libx264"):
    root, output = Path(root), Path(output_dir)
    # Verification must happen before Python import creates __pycache__ under
    # source; bootstrap sets PYTHONDONTWRITEBYTECODE for this reason.
    manifest = verify_payload_manifest(root, manifest_hash)
    project = root / "project"
    slug, number, job = manifest["project_slug"], manifest["chapter_number"], manifest["job_type"]
    # Storage resolution is shared with CLI, keeping the source/project layout.
    import os
    runtime_data = root / "data"
    (runtime_data / "projects").mkdir(parents=True, exist_ok=True)
    destination = runtime_data / "projects" / slug
    if destination.exists():
        raise ValueError("Worker requires a fresh runtime directory")
    shutil.copytree(project, destination)
    os.environ["MANGA_RECAP_DATA_DIR"] = str(runtime_data)
    require_series_setup(slug, project_dir=destination)
    chapter = destination / "chapters" / number
    if job in {"tts", "produce"}:
        handle_tts(argparse.Namespace(project=slug, chapter=number, engine="neural", kaggle=False, workers=1))
    result = None
    if job in {"render", "produce"}:
        quality = "preview" if number == "1" else "final"
        result = render_chapter(destination, number, quality=quality, encoder=encoder)
    output.mkdir(parents=True, exist_ok=True)
    shutil.copytree(chapter, output / "chapter", dirs_exist_ok=True)
    receipt = dict(schema_version="4.1.0", project_slug=slug, chapter_number=number, job_type=job,
                   payload_manifest_sha256=manifest_hash, render_result=result)
    receipt["files"] = {p.relative_to(output).as_posix(): fingerprint(p) for p in sorted(output.rglob("*")) if p.is_file()}
    atomic_write_json(output / "result_manifest.json", receipt)
    return receipt
