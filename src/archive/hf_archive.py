"""Archive project files into a private Hugging Face dataset and restore by commit."""
from __future__ import annotations

import fnmatch
import shutil
import tempfile
from pathlib import Path

from huggingface_hub import HfApi, hf_hub_download

from src.core.errors import DomainValidationError
from src.core.project import get_output_dir, get_project_dir
from src.core.schemas import ArchiveReceipt, utc_iso_now
from src.core.storage import (
    atomic_write_json,
    calculate_sha256 as compute_file_sha256,
    read_json,
)
from src.providers.huggingface import (
    DEFAULT_HF_REPO_ID,
    FALLBACK_HF_TOKEN,
    HuggingFaceClient,
    get_hf_api,
    require_revision_sha as _require_revision,
    resolve_hf_token,
)

ARCHIVE_SCHEMA_VERSION = "4.0.0"

_IMAGE = {".jpg", ".jpeg", ".png", ".webp"}
_AUDIO = {".wav", ".m4a", ".mp3", ".ogg", ".flac", ".aac"}
_VIDEO = {".mp4", ".mkv"}
_SUBTITLE = {".srt", ".vtt"}
_ROOT_ROLES = dict.fromkeys((
    "project.json", "characters.json", "story_summary.md", "series_setup_answers.json",
    "voice_selection.json", "preview_approval.json", "feature_timeline.json",
    "feature.youtube.json", "production_job.json", "tombstone.json",
), "metadata") | {
    "feature.youtube_metadata.json": "youtube_metadata", "feature.youtube_receipt.json": "youtube_receipt",
    "archive_manifest.json": "manifest", "archive_receipt.json": "receipt",
}
_CHAPTER_ROLES = {
    "chapter.json": "chapter_metadata", "manifest.json": "chapter_manifest",
    "script.json": "script", "visual_plan.json": "visual_plan", "panel_plan.json": "panel_plan",
    "youtube_metadata.json": "youtube_metadata",
    "youtube_receipt.json": "youtube_receipt", "thumbnail.jpg": "thumbnail", "thumbnail.png": "thumbnail",
}
_CHAPTER_MEDIA = {
    "raw": (_IMAGE, "raw_image"), "panels": (_IMAGE, "panel_image"),
    "contact_sheets": (_IMAGE, "contact_sheet"), "audio": (_AUDIO, "audio"),
    "video": (_VIDEO, "video"), "subtitles": (_SUBTITLE, "subtitles"),
    "review_pack": ({".html", ".md", ".json", *_IMAGE}, "review_pack"),
}
_SECRET_PATTERNS = (".*", "*.env", "*.pem", "*.key", "*.pickle", "*.pkl", "*.tmp", "*.temp", "*.lock",
                    "__pycache__*", "*.pyc", "*.pyo", "*.pyd", "*id_rsa*", "*kaggle*.json")
_SECRET_WORDS = ("token", "secret", "credential", "passwd", "password")


def ensure_archive_repo(api, repo_id=DEFAULT_HF_REPO_ID):
    """Validates or creates a private dataset repository using provided SDK handle."""
    return HuggingFaceClient(api_client=api).ensure_private_repo(repo_id=repo_id)


def _has_symlink_component(root, relative):
    if root.is_symlink():
        return True
    for part in relative.parts:
        root /= part
        if root.is_symlink():
            return True
    return False


def _output_role(parts, suffix):
    if len(parts) > 2 or (len(parts) == 2 and parts[0] != "export_bundle"):
        return None
    name = parts[-1]
    if suffix == ".json":
        if name.endswith(".youtube_receipt.json"):
            return "youtube_receipt"
        if name.endswith(".youtube_metadata.json"):
            return "youtube_metadata"
        if name in {"archive_manifest.json", "feature_timeline.json", "feature.youtube.json"} or name.endswith("_timeline.json"):
            return "manifest"
    elif suffix in _VIDEO or suffix in _SUBTITLE:
        return "output_video" if suffix in _VIDEO else "output_subtitles"
    elif suffix in _IMAGE:
        return "thumbnail"
    return None


def _archive_role(parts, suffix):
    if not parts:
        return None
    if parts[0] == "output" and len(parts) >= 2:
        return _output_role(parts[1:], suffix)
    if len(parts) == 1:
        return _ROOT_ROLES.get(parts[0])
    if len(parts) >= 3 and parts[0] == "chapters":
        if len(parts) == 3:
            return _CHAPTER_ROLES.get(parts[2])
        if parts[2] in _CHAPTER_MEDIA:
            exts, role = _CHAPTER_MEDIA[parts[2]]
            if parts[2] == "audio" and len(parts) == 4 and parts[3].startswith("clips") and suffix in _AUDIO:
                return "audio_clip"
            if suffix in exts:
                return "audio_clip" if parts[2] == "audio" and len(parts) > 4 and parts[3] == "clips" else role
    return None


def is_whitelisted_archive_path(rel_path, project_root, output_root=None):
    """Allowlists only non-secret, symlink-free project and output media files."""
    relative = Path(rel_path)
    parts = relative.parts
    if not parts or relative.is_absolute() or ".." in parts:
        return False, "Path escapes project boundary"
    if any(fnmatch.fnmatch(part.lower(), pat) or any(word in part.lower() for word in _SECRET_WORDS)
           for part in parts for pat in _SECRET_PATTERNS):
        return False, f"Forbidden secret or temp pattern: {relative}"
    is_output = parts[0] == "output" and output_root is not None
    base = Path(output_root if is_output else project_root)
    rel = Path(*parts[1:]) if is_output else relative
    target = base / rel
    if _has_symlink_component(base, rel) or not target.resolve().is_relative_to(base.resolve()):
        return False, f"Symlink escape or unsafe resolution: {relative}"
    role = _archive_role(parts, relative.suffix.lower())
    return (True, role) if role else (False, f"Not in archive whitelist: {relative}")


def generate_archive_manifest(project_slug, repo_id=DEFAULT_HF_REPO_ID, project_dir=None, output_dir=None):
    """Generates SHA-256 inventory of archive-eligible project and output artifacts."""
    project = Path(project_dir or get_project_dir(project_slug))
    output = Path(output_dir or get_output_dir(project_slug))
    files, total = [], 0
    for root, prefix in [(project, Path()), *([(output, Path("output"))] if output.exists() else [])]:
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            rel = prefix / path.relative_to(root)
            if rel.as_posix() in {"archive_manifest.json", "archive_receipt.json"}:
                continue
            ok, role = is_whitelisted_archive_path(rel, project, output)
            if not ok:
                continue
            size = path.stat().st_size
            total += size
            files.append({
                "relative_path": rel.as_posix(),
                "repo_path": f"series/{project_slug}/{rel.as_posix()}",
                "role": role,
                "size_bytes": size,
                "sha256": compute_file_sha256(path),
            })
    manifest = {
        "schema_version": ARCHIVE_SCHEMA_VERSION,
        "project_slug": project_slug,
        "repo_id": repo_id,
        "created_at": utc_iso_now(),
        "total_files": len(files),
        "total_bytes": total,
        "files": files,
    }
    atomic_write_json(project / "archive_manifest.json", manifest)
    return manifest


def archive_push(project_slug, repo_id=None, token=None, project_dir=None, output_dir=None, api_client=None):
    """Stages verified whitelist artifacts and commits them to the private HF dataset."""
    project, output = Path(project_dir or get_project_dir(project_slug)), Path(output_dir or get_output_dir(project_slug))
    repo = repo_id or DEFAULT_HF_REPO_ID
    client = HuggingFaceClient(token=token, api_client=api_client, download_fn=hf_hub_download)
    client.ensure_private_repo(repo)
    manifest = generate_archive_manifest(project_slug, repo, project, output)
    manifest_path = project / "archive_manifest.json"
    with tempfile.TemporaryDirectory(prefix="manga-archive-") as directory:
        stage = Path(directory)
        shutil.copy2(manifest_path, stage / manifest_path.name)
        for entry in manifest["files"]:
            relative = Path(entry["relative_path"])
            source = output / Path(*relative.parts[1:]) if relative.parts[0] == "output" else project / relative
            dest = stage / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)
            if compute_file_sha256(dest) != entry["sha256"]:
                raise DomainValidationError(f"Changed during archive preparation: {relative}")
        revision = client.upload_folder(
            folder_path=stage,
            path_in_repo=f"series/{project_slug}",
            repo_id=repo,
            commit_message=f"Archive {project_slug}",
        )
    receipt = ArchiveReceipt(
        project_slug=project_slug,
        repo_id=repo,
        commit_sha=revision,
        remote_url=f"https://huggingface.co/datasets/{repo}/tree/{revision}/series/{project_slug}",
        created_at=utc_iso_now(),
        verified_file_count=0,
        total_bytes=manifest["total_bytes"],
        manifest_sha256=compute_file_sha256(manifest_path),
        status="uploaded",
    )
    path = project / "archive_receipt.json"
    atomic_write_json(path, receipt)
    return dict(
        status="success",
        project_slug=project_slug,
        repo_id=repo,
        commit_sha=revision,
        remote_url=receipt.remote_url,
        files_uploaded=manifest["total_files"],
        total_bytes=manifest["total_bytes"],
        receipt_path=str(path),
    )


def _remote_file(repo, revision, filename, token):
    return HuggingFaceClient(token=token, download_fn=hf_hub_download).download_file(
        repo_id=repo, revision=revision, filename=filename
    )


def _manifest_files(manifest, project_slug, repo):
    if manifest.get("project_slug") != project_slug or manifest.get("repo_id") != repo:
        raise DomainValidationError("Archive identity mismatch")
    seen = set()
    for entry in manifest["files"]:
        relative = Path(entry["relative_path"])
        expected = f"series/{project_slug}/{relative.as_posix()}"
        if relative.is_absolute() or ".." in relative.parts or relative in seen or entry["repo_path"] != expected:
            raise DomainValidationError(f"Unsafe or duplicate archived path: {relative}")
        seen.add(relative)
        yield relative, entry


def _checked_download(repo, revision, entry, token):
    path = _remote_file(repo, revision, entry["repo_path"], token)
    if path.stat().st_size != entry["size_bytes"] or compute_file_sha256(path) != entry["sha256"]:
        raise DomainValidationError(f"Archived file integrity mismatch: {entry['relative_path']}")
    return path


def archive_verify(project_slug, repo_id=None, token=None, revision=None, project_dir=None, api_client=None):
    """Downloads every archived artifact at pinned commit SHA and verifies size & SHA-256."""
    project = Path(project_dir or get_project_dir(project_slug))
    path = project / "archive_receipt.json"
    receipt = ArchiveReceipt.model_validate(read_json(path))
    repo, revision = repo_id or receipt.repo_id, _require_revision(revision or receipt.commit_sha)
    if receipt.project_slug != project_slug or receipt.repo_id != repo or receipt.commit_sha != revision:
        raise DomainValidationError("Archive verification must match the upload receipt")
    manifest_path = project / "archive_manifest.json"
    remote_manifest = _remote_file(repo, revision, f"series/{project_slug}/archive_manifest.json", token)
    if any(compute_file_sha256(p) != receipt.manifest_sha256 for p in (manifest_path, remote_manifest)):
        raise DomainValidationError("Archive manifest differs from upload receipt")
    files = list(_manifest_files(read_json(manifest_path), project_slug, repo))
    for _, entry in files:
        _checked_download(repo, revision, entry, token)
    receipt.status, receipt.verified_file_count = "verified", len(files)
    receipt.verification_revision, receipt.last_verified_at = revision, utc_iso_now()
    atomic_write_json(path, receipt)
    return dict(
        status="success",
        verified=True,
        project_slug=project_slug,
        repo_id=repo,
        commit_sha=revision,
        verified_file_count=len(files),
        receipt_path=str(path),
    )


def archive_restore(
    project_slug,
    revision=None,
    repo_id=None,
    token=None,
    target_dir=None,
    project_dir=None,
    allow_non_empty=False,
):
    """Restores verified archived artifacts to ``target_dir`` without leaving partial corrupted files."""
    project = Path(project_dir or get_project_dir(project_slug))
    receipt_path = project / "archive_receipt.json"
    receipt = ArchiveReceipt.model_validate(read_json(receipt_path)) if receipt_path.is_file() else None
    repo = repo_id or (receipt.repo_id if receipt else DEFAULT_HF_REPO_ID)
    revision = _require_revision(revision or (receipt.commit_sha if receipt else None))
    target = Path(target_dir or project).absolute()
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise DomainValidationError("Restore target must be a regular directory")
    if target.exists() and any(target.iterdir()) and not allow_non_empty:
        raise DomainValidationError("Choose an empty --target-dir or explicitly allow a nonempty target")
    target.parent.mkdir(parents=True, exist_ok=True)
    remote_manifest = _remote_file(repo, revision, f"series/{project_slug}/archive_manifest.json", token)
    manifest = read_json(remote_manifest)
    if receipt and repo == receipt.repo_id and revision == receipt.commit_sha:
        if compute_file_sha256(remote_manifest) != receipt.manifest_sha256:
            raise DomainValidationError("Archive manifest differs from the local receipt")
    files = list(_manifest_files(manifest, project_slug, repo))
    restored = 0
    with tempfile.TemporaryDirectory(prefix=".manga-restore-", dir=target.parent) as directory:
        stage = Path(directory) / "project"
        stage.mkdir()
        shutil.copy2(remote_manifest, stage / "archive_manifest.json")
        for relative, entry in files:
            if not is_whitelisted_archive_path(relative, stage)[0]:
                continue
            dest = stage / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(_checked_download(repo, revision, entry, token), dest)
            restored += 1
        if not target.exists() or not any(target.iterdir()):
            stage.replace(target)
        else:
            for file in stage.rglob("*"):
                if file.is_file() and _has_symlink_component(target, file.relative_to(stage)):
                    raise DomainValidationError(f"Symlink in restore destination: {file.relative_to(stage)}")
            for file in stage.rglob("*"):
                if file.is_file():
                    dest = target / file.relative_to(stage)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    file.replace(dest)
    return dict(
        status="success",
        project_slug=project_slug,
        repo_id=repo,
        revision=revision,
        target_dir=str(target),
        restored_files=restored,
    )
