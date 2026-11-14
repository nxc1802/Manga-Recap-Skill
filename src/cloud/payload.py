"""Self-contained source/project payload inventory with fail-closed hash checking."""
import hashlib
import json
import tarfile
import tempfile
import shutil
from pathlib import Path
from src.core.storage import atomic_write_json, read_json
from src.render.artifact_validation import fingerprint


def verify_payload_manifest(root, expected_manifest_hash):
    root = Path(root).resolve()
    path = root / "payload_manifest.json"
    if fingerprint(path) != expected_manifest_hash:
        raise ValueError("Payload manifest hash mismatch")
    manifest = read_json(path)
    if manifest.get("schema_version") != "4.1.0" or not manifest.get("files"):
        raise ValueError("Invalid payload manifest")
    actual = set()
    for entry in root.rglob("*"):
        if entry.is_symlink():
            raise ValueError("Payload symlinks are forbidden")
        if entry.is_file() and entry != path:
            actual.add(entry.relative_to(root).as_posix())
    if actual != set(manifest["files"]):
        raise ValueError("Payload inventory differs from manifest")
    for relative, digest in manifest["files"].items():
        target = (root / relative).resolve()
        if not target.is_relative_to(root) or fingerprint(target) != digest:
            raise ValueError(f"Missing, unsafe or altered payload file: {relative}")
    for required in ("src/core/visual_contract.py", "src/render/visual_plan_renderer.py", "requirements.txt", "project/project.json", "project/series_setup_answers.json"):
        if required not in actual:
            raise ValueError(f"Required runtime asset is absent: {required}")
    return manifest


def package_payload(project_dir, chapter_number, output, job_type):
    from src.core.series_setup import require_series_setup
    root = Path(project_dir).resolve()
    slug = read_json(root / "project.json")["slug"]
    require_series_setup(slug, project_dir=root)
    if job_type not in {"tts", "render", "produce"}:
        raise ValueError("Unknown cloud job type")
    from src.core.storage import load_manifest, load_recap_script
    from src.render.visual_policy import validate_narration_evidence
    chapter = root / "chapters" / str(chapter_number)
    script, source_manifest = load_recap_script(chapter), load_manifest(chapter)
    validate_narration_evidence(chapter, script, source_manifest)
    if script.project_slug != slug or script.chapter_number != str(chapter_number):
        raise ValueError("Payload chapter/project identity mismatch")
    for name in ("chapter.json", "script.json", "manifest.json"):
        if not (chapter / name).is_file():
            raise ValueError(f"Missing required chapter payload: {name}")
    source_root = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix="manga-payload-") as temp:
        stage = Path(temp)
        files = {}
        for source in (source_root / "src").rglob("*.py"):
            files[source.relative_to(source_root).as_posix()] = source
        for name in ("requirements.txt", "pyproject.toml"):
            files[name] = source_root / name
        for name in ("project.json", "series_setup_answers.json", "voice_selection.json", "preview_approval.json"):
            if (root / name).is_file():
                files[f"project/{name}"] = root / name
        allowed_roots = {"raw", "panels", "audio", "subtitles", "contact_sheets"}
        allowed_files = {"chapter.json", "script.json", "manifest.json", "visual_plan.json", "thumbnail.jpg", "thumbnail.png", "thumbnail_selection.json"}
        for number in {"1", str(chapter_number)}:
            chapter = root / "chapters" / number
            if not chapter.is_dir():
                raise ValueError(f"Missing chapter payload: {number}")
            for path in chapter.rglob("*"):
                rel = path.relative_to(chapter)
                if path.is_symlink():
                    raise ValueError(f"Symlink in payload: {path.name}")
                if path.is_file() and (rel.parts[0] in allowed_roots or rel.as_posix() in allowed_files or (number == "1" and rel.as_posix() == "video/preview.mp4")):
                    if path.suffix in {".lock", ".tmp"}:
                        continue
                    files[f"project/chapters/{number}/{rel.as_posix()}"] = path
        bgm = read_json(root / "project.json").get("audio_settings", {}).get("bgm_path")
        if bgm:
            path = root / bgm
            if Path(bgm).is_absolute() or not path.resolve().is_relative_to(root):
                raise ValueError("Cloud BGM must be a portable project-relative asset")
            files[f"project/{bgm}"] = path
        for relative, path in files.items():
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"Missing/unsafe runtime asset: {relative}")
            destination = stage / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
        manifest = dict(schema_version="4.1.0", project_slug=slug, chapter_number=str(chapter_number), job_type=job_type,
                        files={relative: fingerprint(stage / relative) for relative in sorted(files)})
        atomic_write_json(stage / "payload_manifest.json", manifest)
        digest = fingerprint(stage / "payload_manifest.json")
        verify_payload_manifest(stage, digest)
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(output, "w:gz") as archive:
            for path in sorted(stage.rglob("*")):
                if path.is_file():
                    archive.add(path, arcname=path.relative_to(stage).as_posix())
    return output, digest
