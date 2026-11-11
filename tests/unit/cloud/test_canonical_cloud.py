"""L2 cloud packaging/parity tests. No Kaggle API, neural TTS or live claim."""
import shutil
import tarfile
from pathlib import Path
import pytest
from unit.test_canonical_workflow import unit_project
from src.cloud.payload import package_payload, verify_payload_manifest
from src.cloud.kaggle_service import KaggleCloudService, safe_extract_tar
from src.cloud.result_sync import sync_result
from src.core.storage import atomic_write_json, read_json
from src.render.artifact_validation import fingerprint
from src.render.chapter_renderer import render_chapter


def test_self_contained_payload_and_bootstrap(unit_project, tmp_path):
    project, chapter = unit_project
    archive, digest = package_payload(project, "1", tmp_path / "payload.tar.gz", "render")
    unpack = tmp_path / "unpack"
    unpack.mkdir()
    with tarfile.open(archive) as tar:
        safe_extract_tar(tar, unpack)
    manifest = verify_payload_manifest(unpack, digest)
    for required in ("src/core/visual_contract.py", "src/render/visual_plan_renderer.py", "project/series_setup_answers.json"):
        assert required in manifest["files"]
    service = object.__new__(KaggleCloudService)
    service._payload_manifest_hash = digest
    script = service.generate_worker_script("unit", "1", job_type="render", encoder_preference="libx264")
    compile(script, "worker.py", "exec")
    assert "from src.cloud.canonical_worker import run" in script
    assert digest in script
    assert "compose_frame_canvas" not in script
    assert script.index("Payload inventory mismatch") < script.index("pip', 'install'")
    (unpack / "src" / "core" / "visual_contract.py").write_text("tampered")
    with pytest.raises(ValueError, match="altered"):
        verify_payload_manifest(unpack, digest)


@pytest.mark.parametrize("damage", ["missing", "extra", "manifest", "symlink"])
def test_payload_missing_corrupt_or_extra_fails(unit_project, tmp_path, damage):
    project, _ = unit_project
    archive, digest = package_payload(project, "1", tmp_path / "payload.tar.gz", "render")
    unpack = tmp_path / "unpack"; unpack.mkdir()
    with tarfile.open(archive) as tar: safe_extract_tar(tar, unpack)
    if damage == "missing": (unpack / "project" / "project.json").unlink()
    elif damage == "extra": (unpack / "extra").write_text("unlisted")
    elif damage == "manifest": (unpack / "payload_manifest.json").write_text("{}")
    else: (unpack / "link").symlink_to(unpack / "requirements.txt")
    with pytest.raises(ValueError): verify_payload_manifest(unpack, digest)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required for L2 parity fixture")
@pytest.mark.parametrize("damage", [None, "plan", "video", "checksum", "job", "input"])
def test_cloud_semantic_parity_checked_before_checkpoint(unit_project, tmp_path, damage):
    project, chapter = unit_project
    render_chapter(project, "1", quality="preview")
    downloaded = tmp_path / "downloaded"
    shutil.copytree(chapter, downloaded / "chapter")
    metadata_path = chapter / "chapter.json"
    original = metadata_path.read_bytes()
    required = ["project.json", "series_setup_answers.json", "chapters/1/script.json", "chapters/1/manifest.json"]
    inputs = {name: fingerprint(project / name) for name in required}
    if damage == "plan":
        path = downloaded / "chapter" / "visual_plan.json"
        plan = read_json(path); plan["target_width"] = 1920; atomic_write_json(path, plan)
    elif damage == "video":
        (downloaded / "chapter" / "video" / "preview.mp4").write_bytes(b"0000ftyp" + b"0"*64)
    digest = "a" * 64
    receipt = dict(schema_version="4.1.0", project_slug="unit", chapter_number="1", job_type="render",
        payload_manifest_sha256=digest, files={p.relative_to(downloaded).as_posix():fingerprint(p) for p in downloaded.rglob("*") if p.is_file()})
    if damage == "checksum": receipt["files"]["chapter/script.json"] = "0"*64
    if damage == "job": receipt["chapter_number"] = "2"
    if damage == "input":
        data = read_json(project / "project.json"); data["description"] = "changed while running"
        atomic_write_json(project / "project.json", data)
    atomic_write_json(downloaded / "result_manifest.json", receipt)
    if damage:
        with pytest.raises(Exception): sync_result(project, "1", downloaded, "render", digest, inputs)
        assert metadata_path.read_bytes() == original
    else:
        result = sync_result(project, "1", downloaded, "render", digest, inputs)
        assert result["validation"]["valid"] and result["semantic_parity"]
        assert read_json(metadata_path)["human_review_status"] == "unreviewed"


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required for L2 interruption fixture")
def test_interrupted_cloud_copy_cannot_leave_completed_state(unit_project, tmp_path, monkeypatch):
    project, chapter = unit_project
    render_chapter(project, "1", quality="preview")
    downloaded = tmp_path / "downloaded"
    shutil.copytree(chapter, downloaded / "chapter")
    required = ["project.json", "series_setup_answers.json", "chapters/1/script.json", "chapters/1/manifest.json"]
    inputs = {name: fingerprint(project / name) for name in required}
    receipt = dict(schema_version="4.1.0", project_slug="unit", chapter_number="1", job_type="render",
        payload_manifest_sha256="a"*64, files={p.relative_to(downloaded).as_posix(): fingerprint(p) for p in downloaded.rglob("*") if p.is_file()})
    atomic_write_json(downloaded / "result_manifest.json", receipt)
    def fail_copy(*_, **__):
        raise OSError("interrupted artifact publication")
    monkeypatch.setattr("src.cloud.result_sync.shutil.copytree", fail_copy)
    with pytest.raises(OSError, match="interrupted"):
        sync_result(project, "1", downloaded, "render", "a"*64, inputs)
    metadata = read_json(chapter / "chapter.json")
    assert metadata["overall_status"] == "running"
    assert metadata["technical_status"] != "completed"
