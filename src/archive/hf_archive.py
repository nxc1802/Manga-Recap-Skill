"""
src/archive/hf_archive.py
Private Hugging Face Dataset Archive & Restoration Engine for Manga Recap Agentic Skill V4.

Provides:
- Strict security whitelist filtering (rejecting .env, OAuth tokens, client_secrets, kaggle tokens).
- Recursive SHA-256 manifest generation (archive_manifest.json).
- Push to private Hugging Face dataset (Cuong2004/manga-recap-archive) yielding archive_receipt.json.
- Remote verification of dataset tree, file sizes, metadata hashes, and LFS pointers.
- Round-trip sandbox restore verification testing schemas (RecapScript) and media headers (MP4/WAV).
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from huggingface_hub import HfApi, hf_hub_download, snapshot_download

from src.cli.exit_codes import CliValidationError
from src.core.logger import setup_logger
from src.core.schemas import RecapScript, utc_iso_now
from src.core.storage import (
    atomic_write_json,
    get_project_dir,
    read_json,
)

logger = setup_logger("hf_archive")

DEFAULT_HF_REPO_ID = "Cuong2004/manga-recap-archive"
FALLBACK_HF_TOKEN = ""
ARCHIVE_SCHEMA_VERSION = "4.0.0"

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

# Strictly whitelisted root project files
WHITELISTED_ROOT_FILES = {
    "project.json": "metadata",
    "characters.json": "metadata",
    "story_summary.md": "metadata",
    "series_setup_answers.json": "metadata",
    "feature_timeline.json": "metadata",
    "feature.youtube.json": "metadata",
    "tombstone.json": "metadata",
    "archive_manifest.json": "manifest",
    "archive_receipt.json": "receipt",
}

# Whitelisted chapter file patterns and roles
WHITELISTED_CHAPTER_PATTERNS: List[Tuple[str, str]] = [
    ("chapter.json", "chapter_metadata"),
    ("manifest.json", "chapter_manifest"),
    ("script.json", "script"),
    ("youtube_metadata.json", "youtube_metadata"),
    ("youtube_receipt.json", "youtube_receipt"),
    ("thumbnail.jpg", "thumbnail"),
    ("chapter_*_thumbnail.jpg", "thumbnail"),
    ("raw/*.[jJ][pP][gG]", "raw_image"),
    ("raw/*.[jJ][pP][eE][gG]", "raw_image"),
    ("raw/*.[pP][nN][gG]", "raw_image"),
    ("raw/*.[wW][eE][bB][pP]", "raw_image"),
    ("panels/*.[jJ][pP][gG]", "panel_image"),
    ("panels/*.[jJ][pP][eE][gG]", "panel_image"),
    ("panels/*.[pP][nN][gG]", "panel_image"),
    ("panels/*.[wW][eE][bB][pP]", "panel_image"),
    ("contact_sheets/*.[jJ][pP][gG]", "contact_sheet"),
    ("contact_sheets/*.[jJ][pP][eE][gG]", "contact_sheet"),
    ("contact_sheets/*.[pP][nN][gG]", "contact_sheet"),
    ("contact_sheets/*.[wW][eE][bB][pP]", "contact_sheet"),
    ("audio/clips/*.wav", "audio_clip"),
    ("audio/*.wav", "audio"),
    ("subtitles/*.srt", "subtitles"),
    ("subtitles/*.vtt", "subtitles"),
    ("video/clips/*.mp4", "video_clip"),
    ("video/*.mp4", "video"),
    ("video/*.mkv", "video"),
    ("review_pack/*", "review_pack"),
]


def resolve_hf_token(token: Optional[str] = None) -> str:
    """Resolves Hugging Face API token with explicit environment and fallback priority."""
    resolved = token or os.getenv("HF_TOKEN") or FALLBACK_HF_TOKEN
    return resolved.strip()


def get_hf_api(token: Optional[str] = None) -> HfApi:
    """Returns an authenticated HfApi client."""
    resolved_token = resolve_hf_token(token)
    return HfApi(token=resolved_token)


def ensure_archive_repo(api: HfApi, repo_id: str = DEFAULT_HF_REPO_ID) -> str:
    """Ensures private Hugging Face dataset repository exists."""
    try:
        api.create_repo(
            repo_id=repo_id,
            repo_type="dataset",
            private=True,
            exist_ok=True,
        )
        return repo_id
    except Exception as e:
        logger.warning(f"Note on create_repo for {repo_id}: {e}")
        return repo_id


def compute_file_sha256(filepath: Path) -> str:
    """Computes streaming SHA-256 hash of a file."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def is_whitelisted_archive_path(rel_path: Path, project_root: Path) -> Tuple[bool, Optional[str]]:
    """
    Evaluates whether a relative path is strictly whitelisted for cloud dataset archiving.
    Rejects directory traversal, symlink escape, and secret patterns.
    Returns (is_allowed, role_or_rejection_reason).
    """
    posix_path = rel_path.as_posix()
    abs_path = (project_root / rel_path).resolve()

    # 1. Symlink escape / Directory traversal check
    try:
        root_resolved = project_root.resolve()
        if not abs_path.is_relative_to(root_resolved):
            return False, f"Directory traversal or symlink escape outside project root: {posix_path}"
    except (ValueError, RuntimeError):
        return False, f"Invalid path resolution: {posix_path}"

    filename = rel_path.name
    lower_posix = posix_path.lower()

    # 2. Hardcoded secret / credential blacklist check
    for pattern in FORBIDDEN_PATTERNS:
        if fnmatch.fnmatch(filename, pattern) or fnmatch.fnmatch(posix_path, pattern):
            return False, f"Matched forbidden secret pattern '{pattern}': {posix_path}"

    # Also detect secret keywords in filenames
    for kw in ("token", "secret", "credential", "passwd", "password"):
        if kw in filename.lower() and filename not in ("youtube_metadata.json", "youtube_receipt.json"):
            return False, f"Filename contains suspicious credential keyword '{kw}': {posix_path}"

    # 3. Whitelist check for root project files
    parts = rel_path.parts
    if len(parts) == 1:
        if filename in WHITELISTED_ROOT_FILES:
            return True, WHITELISTED_ROOT_FILES[filename]
        return False, f"Root file not in whitelisted project files: {filename}"

    # 4. Whitelist check for chapter files (chapters/<num>/...)
    if parts[0] == "chapters" and len(parts) >= 3:
        # e.g., chapters/1/script.json -> subpath: script.json
        # e.g., chapters/1/audio/clips/001.wav -> subpath: audio/clips/001.wav
        subpath = "/".join(parts[2:])
        for pattern, role in WHITELISTED_CHAPTER_PATTERNS:
            if fnmatch.fnmatch(subpath, pattern):
                return True, role
        return False, f"Chapter asset not in whitelisted patterns: {subpath}"

    return False, f"Path outside whitelisted project structure: {posix_path}"


def generate_archive_manifest(
    project_slug: str,
    repo_id: str = DEFAULT_HF_REPO_ID,
    project_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Scans project directory, filters against security whitelist, computes recursive SHA-256 hashes,
    and writes `archive_manifest.json`.
    """
    proj_dir = project_dir or get_project_dir(project_slug)
    if not proj_dir.exists():
        raise CliValidationError(f"Project directory does not exist: {proj_dir}")

    whitelisted_files: List[Dict[str, Any]] = []
    total_bytes = 0
    excluded_count = 0

    for root, dirs, files in os.walk(proj_dir):
        # Sort for deterministic processing
        dirs.sort()
        files.sort()
        for f in files:
            file_abs = Path(root) / f
            try:
                rel_path = file_abs.relative_to(proj_dir)
            except ValueError:
                continue

            # Don't include previous archive_manifest.json in the manifest calculation itself
            if rel_path.name in ("archive_manifest.json", "archive_receipt.json"):
                continue

            is_allowed, role_or_reason = is_whitelisted_archive_path(rel_path, proj_dir)
            if not is_allowed:
                excluded_count += 1
                logger.debug(f"Excluding from archive: {rel_path} ({role_or_reason})")
                continue

            file_size = file_abs.stat().st_size
            sha256_hash = compute_file_sha256(file_abs)
            posix_rel = rel_path.as_posix()
            repo_path = f"series/{project_slug}/{posix_rel}"

            whitelisted_files.append({
                "relative_path": posix_rel,
                "repo_path": repo_path,
                "sha256": sha256_hash,
                "size_bytes": file_size,
                "role": role_or_reason,
            })
            total_bytes += file_size

    # Sort deterministically by relative_path
    whitelisted_files.sort(key=lambda x: x["relative_path"])

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


def archive_push(
    project_slug: str,
    repo_id: Optional[str] = None,
    token: Optional[str] = None,
    project_dir: Optional[Path] = None,
    api_client: Optional[HfApi] = None,
) -> Dict[str, Any]:
    """
    Pushes project assets to private Hugging Face dataset under `series/<project_slug>/`.
    Strictly filters against security whitelist and writes `archive_receipt.json`.
    """
    proj_dir = project_dir or get_project_dir(project_slug)
    if not proj_dir.exists():
        raise CliValidationError(f"Project '{project_slug}' not found at {proj_dir}")

    target_repo = repo_id or DEFAULT_HF_REPO_ID
    api = api_client or get_hf_api(token)

    # 1. Ensure private repo exists
    ensure_archive_repo(api, target_repo)

    # 2. Generate archive manifest
    manifest = generate_archive_manifest(project_slug=project_slug, repo_id=target_repo, project_dir=proj_dir)
    manifest_path = proj_dir / "archive_manifest.json"

    # 3. Create a clean isolated staging folder containing strictly whitelisted files
    staging_dir = Path(tempfile.mkdtemp(prefix=f"hf_stage_{project_slug}_"))
    try:
        # Copy manifest
        shutil.copy2(manifest_path, staging_dir / "archive_manifest.json")

        for item in manifest["files"]:
            rel = Path(item["relative_path"])
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

        commit_sha = ""
        if hasattr(commit_info, "oid"):
            commit_sha = str(commit_info.oid)
        elif isinstance(commit_info, str):
            commit_sha = commit_info.split("/")[-1]
        else:
            try:
                commits = api.list_repo_commits(repo_id=target_repo, repo_type="dataset")
                if commits:
                    commit_sha = getattr(commits[0], "commit_id", "main")
            except Exception:
                commit_sha = "latest"

        remote_url = f"https://huggingface.co/datasets/{target_repo}/tree/{commit_sha}/series/{project_slug}"

        # 4. Generate immutable archive_receipt.json
        receipt: Dict[str, Any] = {
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
            "status": "uploaded",  # NOT trusted until archive_verify + archive_restore succeed.
        }

        receipt_path = proj_dir / "archive_receipt.json"
        atomic_write_json(receipt_path, receipt)
        logger.info(f"Emitted archive receipt: {receipt_path} (commit: {commit_sha})")

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
    Verifies remote repository tree, file sizes, metadata hashes, and LFS pointers against archive_manifest.json.
    Updates archive_receipt.json status upon successful verification.
    """
    proj_dir = project_dir or get_project_dir(project_slug)
    manifest_path = proj_dir / "archive_manifest.json"
    receipt_path = proj_dir / "archive_receipt.json"

    if not manifest_path.exists():
        raise CliValidationError(f"Archive manifest not found: {manifest_path}. Push to archive first.")
    if not receipt_path.exists():
        raise CliValidationError(f"Archive receipt not found: {receipt_path}. Push to archive first.")

    manifest = read_json(manifest_path)
    receipt = read_json(receipt_path)

    target_repo = repo_id or receipt.get("repo_id") or DEFAULT_HF_REPO_ID
    commit_sha = revision or receipt.get("commit_sha")
    if not commit_sha:
        raise CliValidationError("Archive verification requires a pinned commit SHA from the upload receipt.")
    if receipt.get("repo_id") != target_repo or manifest.get("repo_id") != target_repo:
        raise CliValidationError("Archive repo identity does not match manifest and upload receipt.")
    if manifest.get("project_slug") != project_slug or receipt.get("project_slug") != project_slug:
        raise CliValidationError("Archive project identity mismatch; refusing to verify another series.")
    if compute_file_sha256(manifest_path) != receipt.get("manifest_sha256"):
        raise CliValidationError("Archive manifest changed after upload; create a new archive commit.")
    api = api_client or get_hf_api(token)

    prefix = f"series/{project_slug}"
    logger.info(f"Verifying remote dataset tree for {target_repo} at revision {commit_sha}...")

    # 1. Enumerate remote tree
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
        # item has path, size, lfs, etc.
        item_path = getattr(item, "path", None) or item.get("path")
        if item_path:
            remote_files_map[item_path] = item

    errors: List[str] = []
    verified_files = 0

    # 2. Verify all files from manifest exist on remote
    for item in manifest.get("files", []):
        r_path = item["repo_path"]
        expected_size = item["size_bytes"]
        expected_hash = item["sha256"]

        if r_path not in remote_files_map:
            errors.append(f"Remote file missing: {r_path}")
            continue

        remote_item = remote_files_map[r_path]
        remote_size = getattr(remote_item, "size", None)
        if remote_size is None and isinstance(remote_item, dict):
            remote_size = remote_item.get("size")

        if remote_size is not None and int(remote_size) != expected_size:
            errors.append(f"Size mismatch on {r_path}: expected {expected_size}, remote {remote_size}")
            continue

        # Every required archive artifact (including MP4, WAV and images) MUST be
        # independently verified byte-for-byte before authorizing cleanup.
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

    # 3. Update receipt
    receipt["last_verified_at"] = utc_iso_now()
    receipt["verified_file_count"] = verified_files
    receipt["status"] = "verified"
    receipt["verification_revision"] = commit_sha
    receipt.pop("restore_verified_at", None)
    receipt.pop("restored_revision", None)
    receipt.pop("restored_verified_files", None)
    atomic_write_json(receipt_path, receipt)

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
        # MP4 files contain 'ftyp' in first 16 bytes (usually bytes 4..8)
        if b"ftyp" in header[:16]:
            return True, "video/mp4"
        return False, f"Invalid MP4 header: {header[:16]!r}"

    if ext == ".wav":
        # WAV files start with 'RIFF' and contain 'WAVE' at bytes 8..12
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

    # Text, markdown, srt, etc.
    return True, f"generic/{ext.lstrip('.')}"


def archive_restore(
    project_slug: str,
    revision: Optional[str] = None,
    repo_id: Optional[str] = None,
    token: Optional[str] = None,
    target_dir: Optional[Path] = None,
    project_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Downloads archived series files into sandbox directory, validates restored schemas (RecapScript)
    and verifies media headers (MP4 / WAV). Required prior to safe cleanup.
    """
    proj_dir = project_dir or get_project_dir(project_slug)
    receipt_path = proj_dir / "archive_receipt.json"

    target_repo = repo_id or DEFAULT_HF_REPO_ID
    commit_sha = revision or "main"
    if receipt_path.exists():
        try:
            r_data = read_json(receipt_path)
            target_repo = repo_id or r_data.get("repo_id") or target_repo
            commit_sha = revision or r_data.get("commit_sha") or commit_sha
        except Exception:
            pass

    restore_dest = target_dir or Path(f"/tmp/manga_restore_test_{project_slug}")
    restore_dest.mkdir(parents=True, exist_ok=True)

    logger.info(f"Initiating sandbox restore test for '{project_slug}' into {restore_dest}...")

    # 1. Download files from Hugging Face snapshot
    download_cache = Path(tempfile.mkdtemp(prefix=f"hf_dl_{project_slug}_"))
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
        if not source_series_dir.exists():
            raise CliValidationError(
                f"Restored directory structure not found: {source_series_dir} in download cache."
            )

        # Copy all restored files to restore_dest
        for root, _, files in os.walk(source_series_dir):
            for f in files:
                src_file = Path(root) / f
                rel_f = src_file.relative_to(source_series_dir)
                dst_file = restore_dest / rel_f
                dst_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_file, dst_file)

    finally:
        shutil.rmtree(download_cache, ignore_errors=True)

    # 2. Validate restored content
    manifest_file = restore_dest / "archive_manifest.json"
    if not manifest_file.exists():
        raise CliValidationError(f"Restored archive missing archive_manifest.json in {restore_dest}")

    manifest_data = read_json(manifest_file)
    restored_files_count = 0
    schemas_validated_count = 0
    media_headers_validated_count = 0
    validation_errors: List[str] = []

    # Validate all files against manifest SHA-256
    for item in manifest_data.get("files", []):
        rel = Path(item["relative_path"])
        f_path = restore_dest / rel
        if not f_path.exists():
            validation_errors.append(f"Restored file missing: {rel}")
            continue

        actual_size = f_path.stat().st_size
        if actual_size != item["size_bytes"]:
            validation_errors.append(f"Restored size mismatch on {rel}: {actual_size} vs {item['size_bytes']}")
            continue

        restored_hash = compute_file_sha256(f_path)
        if restored_hash != item["sha256"]:
            validation_errors.append(
                f"Restored SHA-256 mismatch on {rel}: {restored_hash} vs {item['sha256']}"
            )
            continue
        restored_files_count += 1

        # Check media header if applicable
        if f_path.suffix.lower() in (".mp4", ".wav", ".jpg", ".jpeg", ".png"):
            ok, msg = verify_media_file_header(f_path)
            if not ok:
                validation_errors.append(f"Header validation failed for {rel}: {msg}")
            else:
                media_headers_validated_count += 1

        # Check script.json schema conformance
        if f_path.name == "script.json":
            try:
                s_data = read_json(f_path)
                RecapScript.model_validate(s_data)
                schemas_validated_count += 1
            except Exception as e:
                validation_errors.append(f"RecapScript schema validation failed for {rel}: {e}")

    if validation_errors:
        raise CliValidationError(
            f"Restore verification failed with {len(validation_errors)} errors:\n"
            + "\n".join(validation_errors[:10])
        )

    # Only a fully verified round-trip is sufficient evidence for local cleanup.
    if receipt_path.exists():
        receipt_data = read_json(receipt_path)
        if (
            receipt_data.get("status") != "verified"
            or receipt_data.get("commit_sha") != commit_sha
            or receipt_data.get("repo_id") != target_repo
            or receipt_data.get("manifest_sha256") != compute_file_sha256(
                proj_dir / "archive_manifest.json"
            )
        ):
            raise CliValidationError(
                "Remote archive receipt/revision is not verified; cannot certify restoration."
            )
        receipt_data["restore_verified_at"] = utc_iso_now()
        receipt_data["restored_revision"] = commit_sha
        receipt_data["restored_verified_files"] = restored_files_count
        atomic_write_json(receipt_path, receipt_data)

    logger.info(
        f"Restore round-trip verification PASSED: {restored_files_count} files, "
        f"{schemas_validated_count} schemas, {media_headers_validated_count} media headers verified."
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
