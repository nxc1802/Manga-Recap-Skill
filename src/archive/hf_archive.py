"""Archive project files into a private Hugging Face dataset and restore by commit."""
from __future__ import annotations

import fnmatch
import os
import re
import shutil
import tempfile
from pathlib import Path

from huggingface_hub import HfApi, hf_hub_download

from src.cli.exit_codes import CliValidationError
from src.core.schemas import ArchiveReceipt, utc_iso_now
from src.core.storage import atomic_write_json, calculate_sha256 as compute_file_sha256, get_output_dir, get_project_dir, read_json

DEFAULT_HF_REPO_ID = "Cuong2004/manga-recap-archive"
FALLBACK_HF_TOKEN = ""
ARCHIVE_SCHEMA_VERSION = "4.0.0"
_GIT_SHA40_RE = re.compile(r"^[a-f0-9]{40}$")

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


def resolve_hf_token(token=None):
    return (token or os.getenv("HF_TOKEN") or FALLBACK_HF_TOKEN).strip()


def get_hf_api(token=None):
    return HfApi(token=resolve_hf_token(token) or None)


def ensure_archive_repo(api, repo_id=DEFAULT_HF_REPO_ID):
    api.create_repo(repo_id=repo_id, repo_type="dataset", private=True, exist_ok=True)
    info = api.repo_info(repo_id=repo_id, repo_type="dataset")
    if getattr(info, "private", None) is not True:
        raise CliValidationError(f"Archive repository {repo_id} must be private")
    return repo_id


def _has_symlink_component(root, relative):
    if root.is_symlink():
        return True
    for part in relative.parts:
        root /= part
        if root.is_symlink():
            return True
    return False


def _output_role(parts, suffix):
    name = parts[-1]
    if len(parts) != 1 and not (len(parts) == 2 and parts[0] == "export_bundle"):
        return None
    for extensions, role in ((_VIDEO, "output_video"), (_AUDIO, "output_audio"),
                             (_SUBTITLE, "output_subtitles"), (_IMAGE, "output_thumbnail")):
        if suffix in extensions:
            return role
    if name.endswith(".youtube_receipt.json"):
        return "youtube_receipt"
    if name.endswith((".youtube.json", ".youtube_metadata.json")):
        return "youtube_metadata"
    if name.endswith("_timeline.json") or name in {"feature_timeline.json", "production_job.json"}:
        return "metadata"
    return None


def _archive_role(parts, suffix):
    if len(parts) == 1:
        return _ROOT_ROLES.get(parts[0])
    if parts[0] == "output":
        return _output_role(parts[1:], suffix)
    if parts[0] in {"assets", "audio"} and len(parts) == 2 and suffix in _AUDIO:
        return "audio"
    if parts[0] != "chapters" or not parts[1].isdigit():
        return None
    if len(parts) == 3:
        if parts[2].startswith("chapter_") and parts[2].endswith("_thumbnail.jpg"):
            return "thumbnail"
        return _CHAPTER_ROLES.get(parts[2])
    if len(parts) == 4 and parts[2] in _CHAPTER_MEDIA:
        extensions, role = _CHAPTER_MEDIA[parts[2]]
        return role if suffix in extensions else None
    if len(parts) == 5 and parts[3] == "clips":
        if parts[2] == "audio" and suffix == ".wav":
            return "audio_clip"
        if parts[2] == "video" and suffix == ".mp4":
            return "video_clip"
    return None


def is_whitelisted_archive_path(rel_path, project_root, output_root=None):
    """Classify a portable project asset and reject symlinks and credentials."""
    parts = rel_path.parts
    if not parts or rel_path.is_absolute() or ".." in parts:
        return False, f"Unsafe archive path: {rel_path}"
    role = _archive_role(parts, rel_path.suffix.lower())
    if role is None:
        return False, f"Unknown archive asset: {rel_path}"
    for part in (value.casefold() for value in parts):
        if any(word in part for word in _SECRET_WORDS) or any(fnmatch.fnmatch(part, pat) for pat in _SECRET_PATTERNS):
            return False, f"Excluded archive asset: {rel_path}"
    if parts[0] == "output" and output_root is not None:
        root, relative = output_root, Path(*parts[1:])
    else:
        root, relative = project_root, rel_path
    if _has_symlink_component(root, relative):
        return False, f"Symlink in archive path: {rel_path}"
    return True, role


def generate_archive_manifest(project_slug, repo_id=DEFAULT_HF_REPO_ID, project_dir=None, output_dir=None):
    project, output = Path(project_dir or get_project_dir(project_slug)), Path(output_dir or get_output_dir(project_slug))
    if not project.is_dir() or project.is_symlink() or output.is_symlink():
        raise CliValidationError("An existing, regular project directory is required")
    entries, excluded = [], 0
    for root, prefix in ((project, Path()), (output, Path("output"))):
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            relative = prefix / path.relative_to(root)
            if relative.name in {"archive_manifest.json", "archive_receipt.json"}:
                continue
            allowed, role = is_whitelisted_archive_path(relative, project, output)
            if not allowed:
                excluded += 1
                continue
            entries.append(dict(relative_path=relative.as_posix(), role=role, size_bytes=path.stat().st_size,
                                sha256=compute_file_sha256(path), repo_path=f"series/{project_slug}/{relative.as_posix()}"))
    result = dict(schema_version=ARCHIVE_SCHEMA_VERSION, project_slug=project_slug, repo_id=repo_id,
                  created_at=utc_iso_now(), files=entries, total_files=len(entries),
                  total_bytes=sum(entry["size_bytes"] for entry in entries), excluded_files_count=excluded)
    atomic_write_json(project / "archive_manifest.json", result)
    return result


def _require_revision(revision):
    if not _GIT_SHA40_RE.fullmatch(revision or ""):
        raise CliValidationError("Provide the 40-character commit SHA from the archive receipt")
    return revision


def archive_push(project_slug, repo_id=None, token=None, project_dir=None, output_dir=None, api_client=None):
    project, output = Path(project_dir or get_project_dir(project_slug)), Path(output_dir or get_output_dir(project_slug))
    repo = repo_id or DEFAULT_HF_REPO_ID
    api = api_client or get_hf_api(token)
    ensure_archive_repo(api, repo)
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
                raise CliValidationError(f"Changed during archive preparation: {relative}")
        commit = api.upload_folder(folder_path=str(stage), path_in_repo=f"series/{project_slug}",
                                   repo_id=repo, repo_type="dataset", commit_message=f"Archive {project_slug}")
    revision = _require_revision(getattr(commit, "oid", None))
    receipt = ArchiveReceipt(project_slug=project_slug, repo_id=repo, commit_sha=revision,
        remote_url=f"https://huggingface.co/datasets/{repo}/tree/{revision}/series/{project_slug}",
        created_at=utc_iso_now(), verified_file_count=0, total_bytes=manifest["total_bytes"],
        manifest_sha256=compute_file_sha256(manifest_path), status="uploaded")
    path = project / "archive_receipt.json"
    atomic_write_json(path, receipt)
    return dict(status="success", project_slug=project_slug, repo_id=repo, commit_sha=revision,
                remote_url=receipt.remote_url, files_uploaded=manifest["total_files"],
                total_bytes=manifest["total_bytes"], receipt_path=str(path))


def _remote_file(repo, revision, filename, token):
    return Path(hf_hub_download(repo_id=repo, revision=revision, filename=filename, repo_type="dataset",
                                token=resolve_hf_token(token) or None))


def _manifest_files(manifest, project_slug, repo):
    if manifest.get("project_slug") != project_slug or manifest.get("repo_id") != repo:
        raise CliValidationError("Archive identity mismatch")
    seen = set()
    for entry in manifest["files"]:
        relative = Path(entry["relative_path"])
        expected = f"series/{project_slug}/{relative.as_posix()}"
        if relative.is_absolute() or ".." in relative.parts or relative in seen or entry["repo_path"] != expected:
            raise CliValidationError(f"Unsafe or duplicate archived path: {relative}")
        seen.add(relative)
        yield relative, entry


def _checked_download(repo, revision, entry, token):
    path = _remote_file(repo, revision, entry["repo_path"], token)
    if path.stat().st_size != entry["size_bytes"] or compute_file_sha256(path) != entry["sha256"]:
        raise CliValidationError(f"Archived file integrity mismatch: {entry['relative_path']}")
    return path


def archive_verify(project_slug, repo_id=None, token=None, revision=None, project_dir=None, api_client=None):
    project = Path(project_dir or get_project_dir(project_slug))
    path = project / "archive_receipt.json"
    receipt = ArchiveReceipt.model_validate(read_json(path))
    repo, revision = repo_id or receipt.repo_id, _require_revision(revision or receipt.commit_sha)
    if receipt.project_slug != project_slug or receipt.repo_id != repo or receipt.commit_sha != revision:
        raise CliValidationError("Archive verification must match the upload receipt")
    manifest_path = project / "archive_manifest.json"
    remote_manifest = _remote_file(repo, revision, f"series/{project_slug}/archive_manifest.json", token)
    if any(compute_file_sha256(p) != receipt.manifest_sha256 for p in (manifest_path, remote_manifest)):
        raise CliValidationError("Archive manifest differs from upload receipt")
    files = list(_manifest_files(read_json(manifest_path), project_slug, repo))
    for _, entry in files:
        _checked_download(repo, revision, entry, token)
    receipt.status, receipt.verified_file_count = "verified", len(files)
    receipt.verification_revision, receipt.last_verified_at = revision, utc_iso_now()
    atomic_write_json(path, receipt)
    return dict(status="success", verified=True, project_slug=project_slug, repo_id=repo,
                commit_sha=revision, verified_file_count=len(files), receipt_path=str(path))


def archive_restore(project_slug, revision=None, repo_id=None, token=None, target_dir=None,
                    project_dir=None, allow_non_empty=False):
    project = Path(project_dir or get_project_dir(project_slug))
    receipt_path = project / "archive_receipt.json"
    receipt = ArchiveReceipt.model_validate(read_json(receipt_path)) if receipt_path.is_file() else None
    repo = repo_id or (receipt.repo_id if receipt else DEFAULT_HF_REPO_ID)
    revision = _require_revision(revision or (receipt.commit_sha if receipt else None))
    target = Path(target_dir or project).absolute()
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise CliValidationError("Restore target must be a regular directory")
    if target.exists() and any(target.iterdir()) and not allow_non_empty:
        raise CliValidationError("Choose an empty --target-dir or explicitly allow a nonempty target")
    target.parent.mkdir(parents=True, exist_ok=True)
    remote_manifest = _remote_file(repo, revision, f"series/{project_slug}/archive_manifest.json", token)
    manifest = read_json(remote_manifest)
    if receipt and repo == receipt.repo_id and revision == receipt.commit_sha:
        if compute_file_sha256(remote_manifest) != receipt.manifest_sha256:
            raise CliValidationError("Archive manifest differs from the local receipt")
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
                    raise CliValidationError(f"Symlink in restore destination: {file.relative_to(stage)}")
            for file in stage.rglob("*"):
                if file.is_file():
                    dest = target / file.relative_to(stage)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    file.replace(dest)
    return dict(status="success", restored=True, project_slug=project_slug, repo_id=repo,
                revision=revision, target_dir=str(target), files_validated=restored)
