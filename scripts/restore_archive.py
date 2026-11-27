"""Explicit standalone maintenance script to restore a verified Hugging Face project archive."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.archive.hf_archive import (  # noqa: E402
    DEFAULT_HF_REPO_ID,
    _checked_download,
    _has_symlink_component,
    _manifest_files,
    _remote_file,
    _require_revision,
    compute_file_sha256,
    is_whitelisted_archive_path,
)
from src.core.errors import DomainValidationError  # noqa: E402
from src.core.project import get_project_dir  # noqa: E402
from src.core.schemas import ArchiveReceipt  # noqa: E402
from src.core.storage import read_json  # noqa: E402


def archive_restore(
    project_slug: str,
    revision: Optional[str] = None,
    repo_id: Optional[str] = None,
    token: Optional[str] = None,
    target_dir: Optional[Path | str] = None,
    project_dir: Optional[Path | str] = None,
    allow_non_empty: bool = False,
) -> Dict[str, Any]:
    """Restores verified archived artifacts to ``target_dir`` without leaving partial corrupted files."""
    project = Path(project_dir or get_project_dir(project_slug))
    receipt_path = project / "archive_receipt.json"
    receipt = ArchiveReceipt.model_validate(read_json(receipt_path)) if receipt_path.is_file() else None
    repo = repo_id or (receipt.repo_id if receipt else DEFAULT_HF_REPO_ID)
    resolved_rev = _require_revision(revision or (receipt.commit_sha if receipt else None))
    target = Path(target_dir or project).absolute()
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise DomainValidationError("Restore target must be a regular directory")
    if target.exists() and any(target.iterdir()) and not allow_non_empty:
        raise DomainValidationError("Choose an empty --target-dir or explicitly allow a nonempty target")
    target.parent.mkdir(parents=True, exist_ok=True)
    remote_manifest = _remote_file(repo, resolved_rev, f"series/{project_slug}/archive_manifest.json", token)
    manifest = read_json(remote_manifest)
    if receipt and repo == receipt.repo_id and resolved_rev == receipt.commit_sha:
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
            shutil.copy2(_checked_download(repo, resolved_rev, entry, token), dest)
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
        revision=resolved_rev,
        target_dir=str(target),
        restored_files=restored,
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Restore a verified Hugging Face project snapshot (V7 standalone maintenance utility)."
    )
    parser.add_argument("-p", "--project", required=True, help="Project slug")
    parser.add_argument("--revision", default=None, help="Git commit SHA to restore (or inferred from archive_receipt.json)")
    parser.add_argument("--repo-id", default=None, help=f"HF Dataset repo ID (default: {DEFAULT_HF_REPO_ID})")
    parser.add_argument("--token", default=None, help="HF API token override")
    parser.add_argument("--target-dir", default=None, help="Empty target directory for restore")
    parser.add_argument("--allow-non-empty", action="store_true", help="Explicitly allow merging into a non-empty target directory")
    parser.add_argument("--json", action="store_true", help="Print result as JSON")
    args = parser.parse_args(argv)

    result = archive_restore(
        project_slug=args.project,
        revision=args.revision,
        repo_id=args.repo_id,
        token=args.token,
        target_dir=args.target_dir,
        allow_non_empty=bool(args.allow_non_empty),
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Restored {result['restored_files']} file(s) for '{result['project_slug']}' -> {result['target_dir']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
