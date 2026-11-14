"""
src/archive/hf_archive.py
Private Hugging Face Dataset Archive & Restoration Engine for Manga Recap Agentic Skill V4.

Provides:
- Strict security whitelist filtering across data/projects/<slug>/ and output/<slug>/
  (rejecting .env, OAuth tokens, client_secrets, kaggle tokens, symlinks, and traversal).
- Recursive SHA-256 manifest generation (archive_manifest.json).
- Push to private Hugging Face dataset (Cuong2004/manga-recap-archive) yielding validated ArchiveReceipt.
- Remote verification of dataset tree, file sizes, and byte-for-byte SHA-256 hashes.
- Atomic empty-dir sandbox restore verification testing all SHA-256 hashes, missing/extra files,
  real media validation (ffprobe/validate_video, validate_audio, validate_subtitles, PIL),
  and Pydantic schema validation across V4 models, plus ReleaseState transitions.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from huggingface_hub import HfApi, hf_hub_download, snapshot_download
from PIL import Image

from src.cli.exit_codes import CliValidationError
from src.core.contracts import (
    ArchiveReceipt,
    ReleaseRecord,
    ReleaseState,
    TombstoneRecord,
    YouTubeReceipt,
)
from src.core.human_review import HumanApproval
from src.core.logger import setup_logger
from src.core.schemas import (
    ChapterMetadata,
    PanelManifest,
    ProjectConfig,
    RecapScript,
    VoiceSelectionApproval,
    utc_iso_now,
)
from src.core.series_setup import SeriesSetupAnswers, validate_complete_setup
from src.core.state_machine import advance_release_state, load_release_record
from src.core.storage import (
    atomic_write_json,
    get_output_dir,
    get_project_dir,
    read_json,
)
from src.core.visual_contract import VisualPlan
from src.render.artifact_validation import (
    validate_audio,
    validate_subtitles,
    validate_video,
)

logger = setup_logger("hf_archive")

DEFAULT_HF_REPO_ID = "Cuong2004/manga-recap-archive"
FALLBACK_HF_TOKEN = ""
ARCHIVE_SCHEMA_VERSION = "4.0.0"
_GIT_SHA40_RE = re.compile(r"^[a-f0-9]{40}$")

# Explicit blacklist patterns that MUST NEVER be archived
FORBIDDEN_PATTERNS = [
    ".env*",
    "*.env",
    "*token*.json",
    "*.token",
    "token.pickle",
    "*.pickle",
    "*client_secrets*.json",
    "client_secret*.json",
    "*credentials*.json",
    "*kaggle*.json",
    "kaggle_token*",
    ".git*",
    "*.pyc",
    "*.pyo",
    "*.pyd",
    "__pycache__*",
    "*.tmp",
    "*.temp",
    "*.lock",
    "*.DS_Store",
    "*id_rsa*",
    "*.pem",
    "*.key",
]

# Allowed metadata/receipt filenames that legitimately include non-secret keywords
_SAFE_RECEIPT_FILENAMES = {
    "youtube_metadata.json",
    "youtube_receipt.json",
    "feature.youtube_receipt.json",
    "feature.youtube_metadata.json",
}

# Strictly whitelisted root project files
WHITELISTED_ROOT_FILES = {
    "project.json": "metadata",
    "characters.json": "metadata",
    "story_summary.md": "metadata",
    "series_setup_answers.json": "metadata",
    "voice_selection.json": "metadata",
    "preview_approval.json": "metadata",
    "feature_timeline.json": "metadata",
    "feature.youtube.json": "metadata",
    "feature.youtube_metadata.json": "youtube_metadata",
    "feature.youtube_receipt.json": "youtube_receipt",
    "production_job.json": "metadata",
    "release_state.json": "metadata",
    "tombstone.json": "metadata",
    "archive_manifest.json": "manifest",
    "archive_receipt.json": "receipt",
}

# Whitelisted chapter file patterns and roles (pattern, role, expected_depth)
WHITELISTED_CHAPTER_PATTERNS: List[Tuple[str, str, int]] = [
    ("chapter.json", "chapter_metadata", 1),
    ("manifest.json", "chapter_manifest", 1),
    ("script.json", "script", 1),
    ("visual_plan.json", "visual_plan", 1),
    ("artifact_validation.json", "artifact_validation", 1),
    ("thumbnail_selection.json", "thumbnail_selection", 1),
    ("youtube_metadata.json", "youtube_metadata", 1),
    ("youtube_receipt.json", "youtube_receipt", 1),
    ("thumbnail.jpg", "thumbnail", 1),
    ("chapter_*_thumbnail.jpg", "thumbnail", 1),
    ("raw/*.[jJ][pP][gG]", "raw_image", 2),
    ("raw/*.[jJ][pP][eE][gG]", "raw_image", 2),
    ("raw/*.[pP][nN][gG]", "raw_image", 2),
    ("raw/*.[wW][eE][bB][pP]", "raw_image", 2),
    ("panels/*.[jJ][pP][gG]", "panel_image", 2),
    ("panels/*.[jJ][pP][eE][gG]", "panel_image", 2),
    ("panels/*.[pP][nN][gG]", "panel_image", 2),
    ("panels/*.[wW][eE][bB][pP]", "panel_image", 2),
    ("contact_sheets/*.[jJ][pP][gG]", "contact_sheet", 2),
    ("contact_sheets/*.[jJ][pP][eE][gG]", "contact_sheet", 2),
    ("contact_sheets/*.[pP][nN][gG]", "contact_sheet", 2),
    ("contact_sheets/*.[wW][eE][bB][pP]", "contact_sheet", 2),
    ("audio/clips/*.wav", "audio_clip", 3),
    ("audio/mixed.m4a", "audio", 2),
    ("audio/*.wav", "audio", 2),
    ("subtitles/*.srt", "subtitles", 2),
    ("subtitles/*.vtt", "subtitles", 2),
    ("video/clips/*.mp4", "video_clip", 3),
    ("video/*.mp4", "video", 2),
    ("video/*.mkv", "video", 2),
    ("review_pack/*", "review_pack", 2),
]

# Whitelisted output/<slug>/ file patterns (relative to output/<slug>/, stored under output/...)
WHITELISTED_OUTPUT_PATTERNS: List[Tuple[str, str, int]] = [
    ("*.mp4", "output_video", 1),
    ("*.mkv", "output_video", 1),
    ("*.srt", "output_subtitles", 1),
    ("*.vtt", "output_subtitles", 1),
    ("*.wav", "output_audio", 1),
    ("*.m4a", "output_audio", 1),
    ("*.jpg", "output_thumbnail", 1),
    ("*.jpeg", "output_thumbnail", 1),
    ("*.png", "output_thumbnail", 1),
    ("*.webp", "output_thumbnail", 1),
    ("*.youtube.json", "youtube_metadata", 1),
    ("*.youtube_metadata.json", "youtube_metadata", 1),
    ("*.youtube_receipt.json", "youtube_receipt", 1),
    ("feature_timeline.json", "metadata", 1),
    ("production_job.json", "metadata", 1),
    ("export_bundle/*.mp4", "output_video", 2),
    ("export_bundle/*.mkv", "output_video", 2),
    ("export_bundle/*.srt", "output_subtitles", 2),
    ("export_bundle/*.vtt", "output_subtitles", 2),
    ("export_bundle/*.wav", "output_audio", 2),
    ("export_bundle/*.m4a", "output_audio", 2),
    ("export_bundle/*.jpg", "output_thumbnail", 2),
    ("export_bundle/*.jpeg", "output_thumbnail", 2),
    ("export_bundle/*.png", "output_thumbnail", 2),
    ("export_bundle/*.webp", "output_thumbnail", 2),
    ("export_bundle/*.json", "metadata", 2),
]


def resolve_hf_token(token: Optional[str] = None) -> str:
    """Resolves Hugging Face API token with explicit environment and fallback priority."""
    resolved = token or os.getenv("HF_TOKEN") or FALLBACK_HF_TOKEN
    return resolved.strip()


def get_hf_api(token: Optional[str] = None) -> HfApi:
    """Returns an authenticated HfApi client."""
    resolved_token = resolve_hf_token(token)
    return HfApi(token=resolved_token or None)


def ensure_archive_repo(api: HfApi, repo_id: str = DEFAULT_HF_REPO_ID) -> str:
    """Ensures private Hugging Face dataset repository exists."""
    try:
        api.create_repo(
            repo_id=repo_id,
            repo_type="dataset",
            private=True,
            exist_ok=True,
        )
    except Exception as e:
        logger.info(f"Archive repo create request notice for {repo_id}: {e}")

    try:
        info = api.repo_info(repo_id=repo_id, repo_type="dataset")
    except Exception as e:
        raise CliValidationError(
            f"Unable to verify archive repository {repo_id} visibility and access: {e}"
        ) from e
    if getattr(info, "private", None) is not True:
        raise CliValidationError(
            f"Archive repository {repo_id} is not confirmed PRIVATE; upload refused."
        )
    return repo_id


def compute_file_sha256(filepath: Path) -> str:
    """Computes streaming SHA-256 hash of a file."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def _has_symlink_component(root: Path, rel_subpath: Path) -> bool:
    """Return True if any directory or file component from root down to rel_subpath is a symlink."""
    cur = root
    if cur.is_symlink():
        return True
    for part in rel_subpath.parts:
        cur = cur / part
        if cur.is_symlink():
            return True
    return False


def _is_safe_receipt_name(filename: str) -> bool:
    if filename in _SAFE_RECEIPT_FILENAMES:
        return True
    return filename.endswith(".youtube_receipt.json") or filename.endswith(".youtube_metadata.json")


def is_whitelisted_archive_path(
    rel_path: Path,
    project_root: Path,
    output_root: Optional[Path] = None,
) -> Tuple[bool, Optional[str]]:
    """
    Evaluates whether a relative path is strictly whitelisted for cloud dataset archiving.
    Rejects directory traversal, symlink escape, and secret patterns.
    Supports both project files and output/<...> files (when parts[0] == 'output').
    Returns (is_allowed, role_or_rejection_reason).
    """
    posix_path = rel_path.as_posix()
    if rel_path.is_absolute() or ".." in rel_path.parts or not rel_path.parts:
        return False, f"Directory traversal or absolute path forbidden: {posix_path}"

    parts = rel_path.parts
    is_output_path = parts[0] == "output" and len(parts) >= 2

    if is_output_path and output_root is not None:
        base_root = output_root
        sub_rel = Path(*parts[1:])
    else:
        base_root = project_root
        sub_rel = rel_path

    # Reject symlinks anywhere along the path and verify containment
    if _has_symlink_component(base_root, sub_rel):
        return False, f"Symlink asset forbidden from archive: {posix_path}"

    abs_path = (base_root / sub_rel).resolve()
    try:
        root_resolved = base_root.resolve()
        if not abs_path.is_relative_to(root_resolved):
            return False, f"Directory traversal or symlink escape outside root: {posix_path}"
    except (ValueError, RuntimeError):
        return False, f"Invalid path resolution: {posix_path}"

    filename = rel_path.name

    # Hardcoded secret / credential blacklist check across all path components
    for pattern in FORBIDDEN_PATTERNS:
        if fnmatch.fnmatch(filename, pattern) or fnmatch.fnmatch(posix_path, pattern):
            return False, f"Matched forbidden secret pattern '{pattern}': {posix_path}"
        if any(fnmatch.fnmatch(part, pattern) for part in parts):
            return False, f"Path component matched forbidden pattern '{pattern}': {posix_path}"

    for kw in ("token", "secret", "credential", "passwd", "password"):
        if kw in filename.lower() and not _is_safe_receipt_name(filename):
            return False, f"Filename contains suspicious credential keyword '{kw}': {posix_path}"
        for parent_part in parts[:-1]:
            if kw in parent_part.lower():
                return False, f"Directory contains suspicious credential keyword '{kw}': {posix_path}"

    # Whitelist check for output/<...> files
    if is_output_path:
        sub_parts = parts[1:]
        subpath = "/".join(sub_parts)
        for pattern, role, expected_depth in WHITELISTED_OUTPUT_PATTERNS:
            if len(sub_parts) == expected_depth and fnmatch.fnmatch(subpath, pattern):
                return True, role
        return False, f"Output asset not in whitelisted patterns: {subpath}"

    # Whitelist check for root project files
    if len(parts) == 1:
        if filename in WHITELISTED_ROOT_FILES:
            return True, WHITELISTED_ROOT_FILES[filename]
        return False, f"Root file not in whitelisted project files: {filename}"

    # Whitelist check for chapter files (chapters/<num>/...)
    if parts[0] == "chapters" and len(parts) >= 3:
        sub_parts = parts[2:]
        subpath = "/".join(sub_parts)
        for pattern, role, expected_depth in WHITELISTED_CHAPTER_PATTERNS:
            if len(sub_parts) == expected_depth and fnmatch.fnmatch(subpath, pattern):
                return True, role
        return False, f"Chapter asset not in whitelisted patterns: {subpath}"

    return False, f"Path outside whitelisted project structure: {posix_path}"


def _scan_root_for_manifest(
    scan_root: Path,
    project_slug: str,
    project_root: Path,
    output_root: Optional[Path],
    rel_prefix: Optional[Path],
    skip_names: Tuple[str, ...],
) -> Tuple[List[Dict[str, Any]], int, int]:
    """Walks a directory without following symlinks and collects whitelisted manifest entries."""
    entries: List[Dict[str, Any]] = []
    total_bytes = 0
    excluded_count = 0

    if scan_root.is_symlink():
        raise CliValidationError(f"Archive source root cannot be a symlink: {scan_root}")

    for root, dirs, files in os.walk(scan_root, followlinks=False):
        dirs.sort()
        files.sort()
        kept_dirs: List[str] = []
        for d in dirs:
            dir_abs = Path(root) / d
            if dir_abs.is_symlink():
                excluded_count += 1
                logger.debug(f"Excluding symlinked directory from archive: {dir_abs}")
                continue
            kept_dirs.append(d)
        dirs[:] = kept_dirs

        for f in files:
            file_abs = Path(root) / f
            try:
                local_rel = file_abs.relative_to(scan_root)
            except ValueError:
                continue

            if skip_names and local_rel.name in skip_names and len(local_rel.parts) == 1:
                continue

            manifest_rel = (rel_prefix / local_rel) if rel_prefix is not None else local_rel
            is_allowed, role_or_reason = is_whitelisted_archive_path(
                manifest_rel,
                project_root,
                output_root=output_root,
            )
            if not is_allowed:
                excluded_count += 1
                logger.debug(f"Excluding from archive: {manifest_rel} ({role_or_reason})")
                continue

            file_size = file_abs.stat().st_size
            sha256_hash = compute_file_sha256(file_abs)
            posix_rel = manifest_rel.as_posix()
            repo_path = f"series/{project_slug}/{posix_rel}"

            entries.append({
                "relative_path": posix_rel,
                "repo_path": repo_path,
                "sha256": sha256_hash,
                "size_bytes": file_size,
                "role": role_or_reason,
            })
            total_bytes += file_size

    return entries, total_bytes, excluded_count


def generate_archive_manifest(
    project_slug: str,
    repo_id: str = DEFAULT_HF_REPO_ID,
    project_dir: Optional[Path] = None,
    output_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Scans project directory (data/projects/<slug>/) AND output directory (output/<slug>/),
    filters against security whitelist, computes recursive SHA-256 hashes,
    and writes `archive_manifest.json`.
    """
    proj_dir = project_dir or get_project_dir(project_slug)
    if not proj_dir.exists():
        raise CliValidationError(f"Project directory does not exist: {proj_dir}")

    out_dir = output_dir or get_output_dir(project_slug)

    proj_entries, proj_bytes, proj_excluded = _scan_root_for_manifest(
        scan_root=proj_dir,
        project_slug=project_slug,
        project_root=proj_dir,
        output_root=None,
        rel_prefix=None,
        skip_names=("archive_manifest.json", "archive_receipt.json"),
    )

    out_entries: List[Dict[str, Any]] = []
    out_bytes = 0
    out_excluded = 0
    if out_dir.exists():
        if out_dir.is_symlink():
            raise CliValidationError(f"Output directory cannot be a symlink: {out_dir}")
        if out_dir.is_dir():
            out_entries, out_bytes, out_excluded = _scan_root_for_manifest(
                scan_root=out_dir,
                project_slug=project_slug,
                project_root=proj_dir,
                output_root=out_dir,
                rel_prefix=Path("output"),
                skip_names=(),
            )

    whitelisted_files = proj_entries + out_entries
    whitelisted_files.sort(key=lambda x: x["relative_path"])
    total_bytes = proj_bytes + out_bytes
    excluded_count = proj_excluded + out_excluded

    manifest: Dict[str, Any] = {
        "schema_version": ARCHIVE_SCHEMA_VERSION,
        "project_slug": project_slug,
        "repo_id": repo_id,
        "created_at": utc_iso_now(),
        "total_files": len(whitelisted_files),
        "total_bytes": total_bytes,
        "excluded_files_count": excluded_count,
        "files": whitelisted_files,
    }

    manifest_path = proj_dir / "archive_manifest.json"
    atomic_write_json(manifest_path, manifest)
    logger.info(
        f"Generated archive manifest for '{project_slug}': {len(whitelisted_files)} files, "
        f"{total_bytes} bytes ({manifest_path})"
    )
    return manifest


def _resolve_40hex_commit_sha(api: HfApi, target_repo: str, commit_info: Any) -> str:
    """Extracts and validates a 40-char hex commit SHA from upload_folder response."""
    commit_sha = ""
    if hasattr(commit_info, "oid") and commit_info.oid:
        commit_sha = str(commit_info.oid).strip()
    elif isinstance(commit_info, str) and commit_info.strip():
        commit_sha = commit_info.strip().split("/")[-1]

    if not _GIT_SHA40_RE.fullmatch(commit_sha):
        try:
            commits = api.list_repo_commits(repo_id=target_repo, repo_type="dataset")
            if commits:
                candidate = str(getattr(commits[0], "commit_id", "") or "").strip()
                if _GIT_SHA40_RE.fullmatch(candidate):
                    commit_sha = candidate
        except Exception:
            pass

    if not _GIT_SHA40_RE.fullmatch(commit_sha):
        raise CliValidationError(
            f"Archive upload to {target_repo} did not yield a valid 40-char hex commit SHA (got {commit_sha!r})."
        )
    return commit_sha


def archive_push(
    project_slug: str,
    repo_id: Optional[str] = None,
    token: Optional[str] = None,
    project_dir: Optional[Path] = None,
    output_dir: Optional[Path] = None,
    api_client: Optional[HfApi] = None,
) -> Dict[str, Any]:
    """
    Pushes project and output assets to private Hugging Face dataset under `series/<project_slug>/`.
    Strictly filters against security whitelist and writes validated `archive_receipt.json`.
    """
    proj_dir = project_dir or get_project_dir(project_slug)
    if not proj_dir.exists():
        raise CliValidationError(f"Project '{project_slug}' not found at {proj_dir}")
    out_dir = output_dir or get_output_dir(project_slug)

    target_repo = repo_id or DEFAULT_HF_REPO_ID
    api = api_client or get_hf_api(token)

    ensure_archive_repo(api, target_repo)

    manifest = generate_archive_manifest(
        project_slug=project_slug,
        repo_id=target_repo,
        project_dir=proj_dir,
        output_dir=out_dir,
    )
    manifest_path = proj_dir / "archive_manifest.json"

    staging_dir = Path(tempfile.mkdtemp(prefix=f"hf_stage_{project_slug}_"))
    try:
        shutil.copy2(manifest_path, staging_dir / "archive_manifest.json")

        for item in manifest["files"]:
            rel = Path(item["relative_path"])
            if rel.parts[0] == "output":
                src = out_dir / Path(*rel.parts[1:])
            else:
                src = proj_dir / rel
            dst = staging_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(src, dst)
            except (OSError, AttributeError):
                shutil.copy2(src, dst)

        logger.info(f"Staged {len(manifest['files']) + 1} files in {staging_dir}. Uploading to {target_repo}...")

        commit_message = (
            f"Archive series '{project_slug}' ({manifest['total_files']} files, {manifest['total_bytes']} bytes)"
        )
        commit_info = api.upload_folder(
            folder_path=str(staging_dir),
            path_in_repo=f"series/{project_slug}",
            repo_id=target_repo,
            repo_type="dataset",
            commit_message=commit_message,
        )

        commit_sha = _resolve_40hex_commit_sha(api, target_repo, commit_info)
        remote_url = f"https://huggingface.co/datasets/{target_repo}/tree/{commit_sha}/series/{project_slug}"

        receipt_model = ArchiveReceipt.model_validate({
            "schema_version": ARCHIVE_SCHEMA_VERSION,
            "receipt_type": "huggingface_archive",
            "project_slug": project_slug,
            "repo_id": target_repo,
            "commit_sha": commit_sha,
            "remote_url": remote_url,
            "created_at": utc_iso_now(),
            "verified_file_count": 0,
            "total_bytes": manifest["total_bytes"],
            "manifest_sha256": compute_file_sha256(manifest_path),
            "status": "uploaded",
        })

        receipt_path = proj_dir / "archive_receipt.json"
        atomic_write_json(receipt_path, receipt_model)
        logger.info(f"Emitted archive receipt: {receipt_path} (commit: {commit_sha})")

        if (proj_dir / "release_state.json").is_file():
            rec = load_release_record(proj_dir)
            if rec.state == ReleaseState.YOUTUBE_VERIFIED:
                advance_release_state(proj_dir, ReleaseState.ARCHIVED, chapter_number=rec.chapter_number)

        return {
            "status": "success",
            "project_slug": project_slug,
            "repo_id": target_repo,
            "commit_sha": commit_sha,
            "remote_url": remote_url,
            "files_uploaded": manifest["total_files"],
            "total_bytes": manifest["total_bytes"],
            "receipt_path": str(receipt_path),
        }

    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)


def archive_verify(
    project_slug: str,
    repo_id: Optional[str] = None,
    token: Optional[str] = None,
    revision: Optional[str] = None,
    project_dir: Optional[Path] = None,
    api_client: Optional[HfApi] = None,
) -> Dict[str, Any]:
    """
    Verifies remote repository tree, file sizes, and byte-for-byte SHA-256 hashes against archive_manifest.json.
    Updates archive_receipt.json status upon successful verification (validated via ArchiveReceipt).
    """
    proj_dir = project_dir or get_project_dir(project_slug)
    manifest_path = proj_dir / "archive_manifest.json"
    receipt_path = proj_dir / "archive_receipt.json"

    if not manifest_path.exists():
        raise CliValidationError(f"Archive manifest not found: {manifest_path}. Push to archive first.")
    if not receipt_path.exists():
        raise CliValidationError(f"Archive receipt not found: {receipt_path}. Push to archive first.")

    manifest = read_json(manifest_path)
    try:
        receipt_model = ArchiveReceipt.model_validate(read_json(receipt_path))
    except Exception as exc:
        raise CliValidationError(f"Invalid archive_receipt.json: {exc}") from exc

    target_repo = repo_id or receipt_model.repo_id or DEFAULT_HF_REPO_ID
    commit_sha = revision or receipt_model.commit_sha
    if not commit_sha or not _GIT_SHA40_RE.fullmatch(commit_sha):
        raise CliValidationError("Archive verification requires a pinned 40-char hex commit SHA from the upload receipt.")
    if receipt_model.repo_id != target_repo or manifest.get("repo_id") != target_repo:
        raise CliValidationError("Archive repo identity does not match manifest and upload receipt.")
    if manifest.get("project_slug") != project_slug or receipt_model.project_slug != project_slug:
        raise CliValidationError("Archive project identity mismatch; refusing to verify another series.")
    if compute_file_sha256(manifest_path) != receipt_model.manifest_sha256:
        raise CliValidationError("Archive manifest changed after upload; create a new archive commit.")
    api = api_client or get_hf_api(token)

    prefix = f"series/{project_slug}"
    logger.info(f"Verifying remote dataset tree for {target_repo} at revision {commit_sha}...")

    try:
        remote_tree = api.list_repo_tree(
            repo_id=target_repo,
            repo_type="dataset",
            path_in_repo=prefix,
            revision=commit_sha,
            recursive=True,
        )
    except Exception as e:
        raise CliValidationError(f"Failed to query remote repository {target_repo} at {commit_sha}: {e}") from e

    remote_files_map: Dict[str, Any] = {}
    for item in remote_tree:
        item_path = getattr(item, "path", None) if not isinstance(item, dict) else item.get("path")
        if item_path:
            remote_files_map[item_path] = item

    errors: List[str] = []
    verified_files = 0

    for item in manifest.get("files", []):
        r_path = item["repo_path"]
        expected_size = item["size_bytes"]
        expected_hash = item["sha256"]

        if r_path not in remote_files_map:
            errors.append(f"Remote file missing: {r_path}")
            continue

        remote_item = remote_files_map[r_path]
        remote_size = getattr(remote_item, "size", None) if not isinstance(remote_item, dict) else remote_item.get("size")

        if remote_size is not None and int(remote_size) != expected_size:
            errors.append(f"Size mismatch on {r_path}: expected {expected_size}, remote {remote_size}")
            continue

        try:
            downloaded_file = hf_hub_download(
                repo_id=target_repo,
                repo_type="dataset",
                filename=r_path,
                revision=commit_sha,
                token=resolve_hf_token(token),
            )
            computed_hash = compute_file_sha256(Path(downloaded_file))
        except Exception as e:
            errors.append(f"Could not download/hash {r_path}: {e}")
            continue
        if computed_hash != expected_hash:
            errors.append(f"SHA-256 mismatch for {r_path}: expected {expected_hash}, remote {computed_hash}")
            continue

        verified_files += 1

    if errors:
        raise CliValidationError(
            f"Remote verification failed with {len(errors)} errors:\n" + "\n".join(errors[:10])
        )

    updated_payload = receipt_model.model_dump()
    updated_payload["last_verified_at"] = utc_iso_now()
    updated_payload["verified_file_count"] = verified_files
    updated_payload["status"] = "verified"
    updated_payload["verification_revision"] = commit_sha
    updated_payload["restore_verified_at"] = None
    updated_payload["restored_revision"] = None
    updated_payload["restored_verified_files"] = None

    verified_receipt = ArchiveReceipt.model_validate(updated_payload)
    atomic_write_json(receipt_path, verified_receipt)

    if (proj_dir / "release_state.json").is_file():
        rec = load_release_record(proj_dir)
        if rec.state == ReleaseState.ARCHIVED:
            advance_release_state(proj_dir, ReleaseState.ARCHIVE_VERIFIED, chapter_number=rec.chapter_number)

    logger.info(f"Remote verification successful: {verified_files}/{len(manifest['files'])} files verified.")
    return {
        "status": "success",
        "verified": True,
        "project_slug": project_slug,
        "repo_id": target_repo,
        "commit_sha": commit_sha,
        "files_verified": verified_files,
        "total_files": len(manifest.get("files", [])),
        "receipt_path": str(receipt_path),
    }


def verify_media_file_header(filepath: Path) -> Tuple[bool, str]:
    """
    Validates binary header format for common media files (MP4, WAV, JPEG, PNG).
    Returns (is_valid, media_type_or_error).
    """
    if not filepath.exists() or filepath.stat().st_size == 0:
        return False, "Empty or non-existent file"

    ext = filepath.suffix.lower()
    with open(filepath, "rb") as f:
        header = f.read(32)

    if ext == ".mp4":
        if b"ftyp" in header[:16]:
            return True, "video/mp4"
        return False, f"Invalid MP4 header: {header[:16]!r}"

    if ext == ".wav":
        if header.startswith(b"RIFF") and header[8:12] == b"WAVE":
            return True, "audio/wav"
        return False, f"Invalid WAV header: {header[:16]!r}"

    if ext in (".jpg", ".jpeg"):
        if header.startswith(b"\xff\xd8"):
            return True, "image/jpeg"
        return False, f"Invalid JPEG header: {header[:8]!r}"

    if ext == ".png":
        if header.startswith(b"\x89PNG\r\n\x1a\n"):
            return True, "image/png"
        return False, f"Invalid PNG header: {header[:8]!r}"

    return True, f"generic/{ext.lstrip('.')}"


def _probe_media_streams(filepath: Path) -> Dict[str, Any]:
    """Runs ffprobe on a media file and returns parsed stream/format JSON."""
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(filepath)],
        capture_output=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise ValueError(proc.stderr.decode(errors="replace")[-500:] or f"ffprobe exit {proc.returncode}")
    return json.loads(proc.stdout.decode("utf-8"))


def _resolve_chapter_plan_for_media(rel_path: Path, stage_dir: Path) -> Optional[VisualPlan]:
    """Resolves VisualPlan for a restored chapter or output video/subtitle if present."""
    parts = rel_path.parts
    if parts[0] == "chapters" and len(parts) >= 3:
        vp_path = stage_dir / "chapters" / parts[1] / "visual_plan.json"
        if vp_path.is_file():
            return VisualPlan.load(vp_path)
    elif parts[0] == "output" and len(parts) == 2:
        m = re.match(r"^chapter_(\d+)\.(mp4|srt)$", rel_path.name)
        if m:
            ch_num = str(int(m.group(1)))
            vp_path = stage_dir / "chapters" / ch_num / "visual_plan.json"
            if vp_path.is_file():
                return VisualPlan.load(vp_path)
    return None


def _validate_restored_media(f_path: Path, rel: Path, stage_dir: Path) -> float:
    """
    Performs semantic media validation (ffprobe/validate_video, validate_audio,
    validate_subtitles, PIL.Image) on a restored asset. Returns probed/validated duration if applicable.
    """
    ext = f_path.suffix.lower()
    if ext in (".jpg", ".jpeg", ".png", ".webp"):
        if ext in (".jpg", ".jpeg", ".png"):
            ok, msg = verify_media_file_header(f_path)
            if not ok:
                raise ValueError(msg)
        with Image.open(f_path) as img:
            img.verify()
        with Image.open(f_path) as img2:
            if img2.width <= 0 or img2.height <= 0:
                raise ValueError("Image has non-positive dimensions")
        return 0.0

    if ext == ".wav":
        ok, msg = verify_media_file_header(f_path)
        if not ok:
            raise ValueError(msg)
        rep = validate_audio(f_path)
        return float(rep["duration"])

    if ext == ".m4a":
        info = _probe_media_streams(f_path)
        audio = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
        if audio is None:
            raise ValueError("No audio stream found in .m4a file")
        dur = float((info.get("format") or {}).get("duration") or 0)
        if dur <= 0:
            raise ValueError("Invalid audio duration in .m4a file")
        return dur

    if ext in (".mp4", ".mkv"):
        if ext == ".mp4":
            ok, msg = verify_media_file_header(f_path)
            if not ok:
                raise ValueError(msg)
        info = _probe_media_streams(f_path)
        v_stream = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
        a_stream = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
        if v_stream is None:
            raise ValueError("Missing video stream")
        width = int(v_stream.get("width", 0))
        height = int(v_stream.get("height", 0))
        duration = float((info.get("format") or {}).get("duration") or 0)
        num, den = map(float, v_stream["avg_frame_rate"].split("/"))
        fps = num / den if den > 0 else 0.0

        is_clip = "clips" in rel.parts
        plan = _resolve_chapter_plan_for_media(rel, stage_dir)
        if plan is not None and not is_clip and (f_path.name == "final.mp4" or rel.parts[0] == "output"):
            rep = validate_video(
                f_path,
                width=plan.target_width,
                height=plan.target_height,
                expected_duration=plan.total_duration_sec,
                fps=plan.fps,
                require_audio=True,
            )
        else:
            require_audio = False if f_path.name.startswith("visual") else ((a_stream is not None) if is_clip else True)
            rep = validate_video(
                f_path,
                width=width,
                height=height,
                expected_duration=duration,
                fps=fps,
                require_audio=require_audio,
            )
        return float(rep["duration"])

    if ext == ".srt":
        plan = _resolve_chapter_plan_for_media(rel, stage_dir)
        dur = plan.total_duration_sec if plan is not None else 86400.0 * 365
        validate_subtitles(f_path, duration=dur)
        return 0.0

    return 0.0


def _validate_restored_json_schema(f_path: Path, rel: Path, project_slug: str) -> bool:
    """
    Validates restored JSON document against its canonical V4 Pydantic contract.
    Returns True if a schema validator applied and succeeded.
    """
    parts = rel.parts
    name = f_path.name

    if parts == ("project.json",):
        cfg = ProjectConfig.model_validate(read_json(f_path))
        if cfg.slug != project_slug:
            raise ValueError(f"ProjectConfig slug mismatch: {cfg.slug!r} != {project_slug!r}")
        return True

    if parts == ("series_setup_answers.json",):
        setup = SeriesSetupAnswers.model_validate(read_json(f_path))
        if setup.project_slug != project_slug:
            raise ValueError(f"SeriesSetupAnswers slug mismatch: {setup.project_slug!r}")
        ok, errs = validate_complete_setup(setup)
        if not ok:
            raise ValueError("; ".join(errs))
        return True

    if parts == ("voice_selection.json",):
        vs = VoiceSelectionApproval.model_validate(read_json(f_path))
        if vs.project_slug and vs.project_slug != project_slug:
            raise ValueError(f"VoiceSelectionApproval slug mismatch: {vs.project_slug!r}")
        return True

    if parts == ("preview_approval.json",):
        ha = HumanApproval.model_validate(read_json(f_path))
        if ha.project_slug != project_slug:
            raise ValueError(f"HumanApproval slug mismatch: {ha.project_slug!r}")
        return True

    if parts == ("release_state.json",):
        rr = ReleaseRecord.model_validate(read_json(f_path))
        if rr.project_slug != project_slug:
            raise ValueError(f"ReleaseRecord slug mismatch: {rr.project_slug!r}")
        return True

    if parts == ("tombstone.json",):
        tr = TombstoneRecord.model_validate(read_json(f_path))
        if tr.project_slug != project_slug:
            raise ValueError(f"TombstoneRecord slug mismatch: {tr.project_slug!r}")
        return True

    if parts == ("feature.youtube_receipt.json",):
        yr = YouTubeReceipt.model_validate(read_json(f_path))
        if yr.project_slug != project_slug:
            raise ValueError(f"YouTubeReceipt slug mismatch: {yr.project_slug!r}")
        return True

    if parts[0] == "chapters" and len(parts) == 3:
        ch_num = parts[1]
        if name == "chapter.json":
            cm = ChapterMetadata.model_validate(read_json(f_path))
            if cm.project_slug != project_slug or str(cm.chapter_number) != ch_num:
                raise ValueError("ChapterMetadata identity mismatch")
            return True
        if name == "manifest.json":
            pm = PanelManifest.model_validate(read_json(f_path))
            if pm.project_slug != project_slug or str(pm.chapter_number) != ch_num:
                raise ValueError("PanelManifest identity mismatch")
            return True
        if name == "script.json":
            rs = RecapScript.model_validate(read_json(f_path))
            if rs.project_slug != project_slug or str(rs.chapter_number) != ch_num:
                raise ValueError("RecapScript identity mismatch")
            return True
        if name == "visual_plan.json":
            vp = VisualPlan.model_validate(read_json(f_path))
            if str(vp.chapter_number) != ch_num:
                raise ValueError("VisualPlan chapter_number mismatch")
            return True
        if name == "youtube_receipt.json":
            yr = YouTubeReceipt.model_validate(read_json(f_path))
            if yr.project_slug != project_slug or str(yr.chapter_number) != ch_num:
                raise ValueError("YouTubeReceipt identity mismatch")
            return True

    if parts[0] == "output" and (name == "feature.youtube_receipt.json" or name.endswith(".youtube_receipt.json")):
        yr = YouTubeReceipt.model_validate(read_json(f_path))
        if yr.project_slug != project_slug:
            raise ValueError("Output YouTubeReceipt slug mismatch")
        return True

    return False


def _commit_staged_restore(stage_dir: Path, restore_dest: Path, allow_non_empty: bool) -> None:
    """Atomically moves validated stage_dir contents into restore_dest."""
    if not restore_dest.exists():
        restore_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(stage_dir), str(restore_dest))
        return

    if restore_dest.is_symlink() or not restore_dest.is_dir():
        raise CliValidationError(f"Restore target must be a directory: {restore_dest}")

    is_empty = not any(restore_dest.iterdir())
    if not is_empty and not allow_non_empty:
        raise CliValidationError(f"Restore target directory must be empty: {restore_dest}")

    if is_empty:
        try:
            restore_dest.rmdir()
            shutil.move(str(stage_dir), str(restore_dest))
            return
        except OSError:
            pass

    # Fallback when empty mountpoint/dir cannot be rmdir'd or allow_non_empty=True
    for entry in stage_dir.iterdir():
        target_entry = restore_dest / entry.name
        if entry.is_dir():
            shutil.copytree(entry, target_entry, dirs_exist_ok=True, symlinks=False)
        else:
            shutil.copy2(entry, target_entry)


def archive_restore(
    project_slug: str,
    revision: Optional[str] = None,
    repo_id: Optional[str] = None,
    token: Optional[str] = None,
    target_dir: Optional[Path] = None,
    project_dir: Optional[Path] = None,
    allow_non_empty: bool = False,
) -> Dict[str, Any]:
    """
    Downloads archived series files into an isolated temp staging directory, validates every file
    SHA-256 against archive_manifest.json (detecting missing & extra files), runs real media
    validation (ffprobe/validate_video, validate_audio, validate_subtitles, PIL) and Pydantic
    schema validation across all V4 contracts, then atomically commits into an EMPTY target_dir.
    Updates local archive_receipt.json and advances ReleaseState to RESTORE_VERIFIED when applicable.
    """
    proj_dir = project_dir or get_project_dir(project_slug)
    receipt_path = proj_dir / "archive_receipt.json"

    local_receipt: Optional[ArchiveReceipt] = None
    if receipt_path.exists():
        try:
            local_receipt = ArchiveReceipt.model_validate(read_json(receipt_path))
        except Exception as exc:
            raise CliValidationError(f"Invalid local archive_receipt.json: {exc}") from exc
        if local_receipt.project_slug != project_slug:
            raise CliValidationError("Local archive_receipt.json project_slug mismatch.")
        target_repo = repo_id or local_receipt.repo_id
        commit_sha = revision or local_receipt.commit_sha
    else:
        target_repo = repo_id or DEFAULT_HF_REPO_ID
        commit_sha = revision or ""

    if not _GIT_SHA40_RE.fullmatch(commit_sha):
        raise CliValidationError(
            f"Archive restore requires a pinned 40-char hex commit SHA (got {commit_sha!r})."
        )

    if target_dir is not None:
        restore_dest = Path(target_dir)
        if restore_dest.exists():
            if restore_dest.is_symlink() or not restore_dest.is_dir():
                raise CliValidationError(f"Restore target must be a directory: {restore_dest}")
            if not allow_non_empty and any(restore_dest.iterdir()):
                raise CliValidationError(f"Restore target directory is not empty: {restore_dest}")
    else:
        restore_dest = Path(tempfile.mkdtemp(prefix=f"manga_restore_test_{project_slug}_"))

    logger.info(f"Initiating atomic sandbox restore for '{project_slug}' -> {restore_dest}...")

    download_cache = Path(tempfile.mkdtemp(prefix=f"hf_dl_{project_slug}_"))
    stage_parent = restore_dest.parent if restore_dest.parent.exists() else Path(tempfile.gettempdir())
    stage_dir = Path(tempfile.mkdtemp(prefix=f".hf_restore_stage_{project_slug}_", dir=str(stage_parent)))

    try:
        resolved_token = resolve_hf_token(token)
        snapshot_download(
            repo_id=target_repo,
            repo_type="dataset",
            revision=commit_sha,
            allow_patterns=f"series/{project_slug}/**",
            local_dir=str(download_cache),
            token=resolved_token,
        )

        source_series_dir = download_cache / "series" / project_slug
        if not source_series_dir.exists() or source_series_dir.is_symlink():
            raise CliValidationError(
                f"Restored directory structure not found or unsafe: {source_series_dir}"
            )

        # Copy snapshot into clean stage_dir while rejecting any symlinks
        actual_rel_paths: set[str] = set()
        for root, dirs, files in os.walk(source_series_dir, followlinks=False):
            for d in list(dirs):
                d_abs = Path(root) / d
                if d_abs.is_symlink():
                    raise CliValidationError(f"Restored archive contains forbidden symlink directory: {d_abs}")
            for f in files:
                src_file = Path(root) / f
                if src_file.is_symlink():
                    raise CliValidationError(f"Restored archive contains forbidden symlink file: {src_file}")
                rel_f = src_file.relative_to(source_series_dir)
                dst_file = stage_dir / rel_f
                dst_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_file, dst_file)
                posix_rel = rel_f.as_posix()
                if posix_rel not in ("archive_manifest.json", "archive_receipt.json"):
                    actual_rel_paths.add(posix_rel)

        # Validate manifest + receipt integrity before individual files
        manifest_file = stage_dir / "archive_manifest.json"
        if not manifest_file.exists():
            raise CliValidationError(f"Restored archive missing archive_manifest.json in {stage_dir}")

        manifest_data = read_json(manifest_file)
        if manifest_data.get("project_slug") != project_slug:
            raise CliValidationError("Restored archive_manifest.json project_slug mismatch")
        if manifest_data.get("repo_id") != target_repo:
            raise CliValidationError("Restored archive_manifest.json repo_id mismatch")

        manifest_sha = compute_file_sha256(manifest_file)
        if local_receipt is not None and local_receipt.manifest_sha256 != manifest_sha:
            raise CliValidationError(
                "Restored archive_manifest.json SHA-256 mismatches local archive_receipt.json"
            )

        schemas_validated_count = 0
        bundled_receipt_path = stage_dir / "archive_receipt.json"
        if bundled_receipt_path.is_file():
            try:
                bundled_receipt = ArchiveReceipt.model_validate(read_json(bundled_receipt_path))
                if (
                    bundled_receipt.project_slug != project_slug
                    or bundled_receipt.repo_id != target_repo
                    or bundled_receipt.manifest_sha256 != manifest_sha
                ):
                    raise ValueError("Bundled archive_receipt.json metadata/manifest_sha256 mismatch")
                schemas_validated_count += 1
            except Exception as exc:
                raise CliValidationError(f"Invalid bundled archive_receipt.json: {exc}") from exc

        restored_files_count = 0
        media_headers_validated_count = 0
        validation_errors: List[str] = []
        expected_rel_paths: set[str] = set()

        for item in manifest_data.get("files", []):
            rel_str = str(item.get("relative_path", ""))
            if rel_str in expected_rel_paths:
                validation_errors.append(f"Duplicate manifest entry: {rel_str}")
                continue
            expected_rel_paths.add(rel_str)

            rel = Path(rel_str)
            allowed, reason = is_whitelisted_archive_path(
                rel,
                stage_dir,
                output_root=(stage_dir / "output") if (rel.parts and rel.parts[0] == "output") else None,
            )
            if not allowed:
                validation_errors.append(f"Disallowed restored path {rel_str}: {reason}")
                continue

            f_path = stage_dir / rel
            if not f_path.is_file():
                validation_errors.append(f"Restored file missing: {rel_str}")
                continue

            actual_size = f_path.stat().st_size
            if actual_size != item["size_bytes"]:
                validation_errors.append(
                    f"Restored size mismatch on {rel_str}: {actual_size} vs {item['size_bytes']}"
                )
                continue

            restored_hash = compute_file_sha256(f_path)
            if restored_hash != item["sha256"]:
                validation_errors.append(
                    f"Restored SHA-256 mismatch on {rel_str}: {restored_hash} vs {item['sha256']}"
                )
                continue
            restored_files_count += 1

            # Real media validation (ffprobe/validate_video, validate_audio, validate_subtitles, PIL)
            ext = f_path.suffix.lower()
            if ext in (".mp4", ".mkv", ".wav", ".m4a", ".srt", ".jpg", ".jpeg", ".png", ".webp"):
                try:
                    _validate_restored_media(f_path, rel, stage_dir)
                    media_headers_validated_count += 1
                except Exception as exc:
                    validation_errors.append(f"Media validation failed for {rel_str}: {exc}")

            # Pydantic schema validation for JSON documents
            if ext == ".json":
                try:
                    if _validate_restored_json_schema(f_path, rel, project_slug):
                        schemas_validated_count += 1
                except Exception as exc:
                    validation_errors.append(f"Schema validation failed for {rel_str}: {exc}")

        extra_files = sorted(actual_rel_paths - expected_rel_paths)
        if extra_files:
            validation_errors.append(
                f"Extra unlisted files found in restored archive: {', '.join(extra_files[:5])}"
            )

        if validation_errors:
            raise CliValidationError(
                f"Restore verification failed with {len(validation_errors)} errors:\n"
                + "\n".join(validation_errors[:10])
            )

        # Atomic commit from validated stage_dir into empty restore_dest
        _commit_staged_restore(stage_dir, restore_dest, allow_non_empty=allow_non_empty)

    finally:
        shutil.rmtree(download_cache, ignore_errors=True)
        if stage_dir.exists():
            shutil.rmtree(stage_dir, ignore_errors=True)

    # Certify restoration in local receipt and advance release_state
    if receipt_path.exists():
        assert local_receipt is not None
        local_manifest_path = proj_dir / "archive_manifest.json"
        if (
            local_receipt.status != "verified"
            or local_receipt.commit_sha != commit_sha
            or local_receipt.repo_id != target_repo
            or not local_manifest_path.is_file()
            or local_receipt.manifest_sha256 != compute_file_sha256(local_manifest_path)
        ):
            raise CliValidationError(
                "Remote archive receipt/revision is not verified; cannot certify restoration."
            )
        updated_payload = local_receipt.model_dump()
        updated_payload["restore_verified_at"] = utc_iso_now()
        updated_payload["restored_revision"] = commit_sha
        updated_payload["restored_verified_files"] = restored_files_count
        restored_receipt = ArchiveReceipt.model_validate(updated_payload)
        atomic_write_json(receipt_path, restored_receipt)

        if (proj_dir / "release_state.json").is_file():
            rec = load_release_record(proj_dir)
            if rec.state == ReleaseState.ARCHIVE_VERIFIED:
                advance_release_state(
                    proj_dir,
                    ReleaseState.RESTORE_VERIFIED,
                    chapter_number=rec.chapter_number,
                )

    logger.info(
        f"Restore round-trip verification PASSED: {restored_files_count} files, "
        f"{schemas_validated_count} schemas, {media_headers_validated_count} media validated."
    )

    return {
        "status": "success",
        "restored": True,
        "project_slug": project_slug,
        "repo_id": target_repo,
        "revision": commit_sha,
        "target_dir": str(restore_dest),
        "files_validated": restored_files_count,
        "schemas_validated": schemas_validated_count,
        "media_headers_validated": media_headers_validated_count,
    }
