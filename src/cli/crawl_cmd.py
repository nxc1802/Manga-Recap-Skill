"""
src/cli/crawl_cmd.py
Subcommand handler: manga-recap crawl
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
from pathlib import Path
from typing import Any, Dict, Optional

from src.cli.exit_codes import CliValidationError
from src.core.schemas import CheckpointStatus, StageType
from src.core.storage import (
    get_chapter_dir,
    init_chapter_layout,
    init_project_layout,
    update_checkpoint,
)
from src.crawler.errors import CrawlerError
from src.crawler.verifier import ImageVerifier


def handle_crawl(args: argparse.Namespace) -> Dict[str, Any]:
    """Crawls online webtoon pages or imports local panel images."""
    project_slug = getattr(args, "project", "")
    chapter_num = getattr(args, "chapter", None)
    url = getattr(args, "url", None)
    import_dir = getattr(args, "import_dir", None)
    max_panels = getattr(args, "max_panels", None)
    verify_flag = getattr(args, "verify", False)

    if not project_slug:
        raise CliValidationError("Project slug (--project) is required.")
    if chapter_num is None:
        raise CliValidationError("Chapter number (--chapter) is required.")

    if not url and not import_dir:
        raise CliValidationError("Either --url or --import-dir must be provided for crawl.")

    # Strict Form Mode: fail-closed if 10/10 questions setup is incomplete or missing
    from src.core.series_setup import require_series_setup
    require_series_setup(project_slug)

    # Initialize storage layout
    init_project_layout(project_slug, title=project_slug)
    chap_dir = init_chapter_layout(project_slug, chapter_num)
    raw_dir = chap_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    update_checkpoint(chap_dir, StageType.CRAWL, CheckpointStatus.RUNNING)

    imported_files = []
    if import_dir:
        import_path = Path(import_dir).resolve()
        if not import_path.exists():
            err_msg = f"Import directory does not exist: {import_path}"
            update_checkpoint(chap_dir, StageType.CRAWL, CheckpointStatus.FAILED, error=err_msg)
            raise CliValidationError(err_msg)

        valid_exts = {".jpg", ".jpeg", ".png", ".webp"}
        files = sorted([f for f in import_path.iterdir() if f.suffix.lower() in valid_exts])
        if max_panels:
            files = files[:max_panels]

        for idx, src_file in enumerate(files, start=1):
            dest_name = f"page_{idx:03d}{src_file.suffix.lower()}"
            dest_path = raw_dir / dest_name
            shutil.copy2(src_file, dest_path)
            imported_files.append(dest_name)

    elif url:
        # Online crawl via crawler_service
        try:
            from src.services.crawler_service import crawler_service
            downloaded = asyncio.run(
                crawler_service.crawl_raw_pages(
                    url=url,
                    output_raw_dir=raw_dir,
                    max_panels=max_panels,
                    chapter_number=str(chapter_num),
                    project_slug=project_slug,
                    strict_verify=verify_flag,
                )
            )
            imported_files.extend(downloaded)
        except CrawlerError as e:
            err_msg = f"[{e.code.value}] {e}"
            update_checkpoint(
                chap_dir,
                StageType.CRAWL,
                CheckpointStatus.FAILED,
                error=err_msg,
            )
            raise CliValidationError(f"Crawl failed: {err_msg}") from e
        except Exception as e:
            err_msg = str(e)
            update_checkpoint(
                chap_dir,
                StageType.CRAWL,
                CheckpointStatus.FAILED,
                error=err_msg,
            )
            raise CliValidationError(f"Crawl failed: {err_msg}") from e

    raw_count = len([f for f in raw_dir.glob("*") if f.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}])

    if raw_count == 0:
        err_msg = "No raw images found"
        update_checkpoint(
            chap_dir,
            StageType.CRAWL,
            CheckpointStatus.FAILED,
            error=err_msg,
        )
        raise CliValidationError(f"Crawl failed: {err_msg}")

    # Optional strict verification
    report = None
    if verify_flag:
        report = ImageVerifier.verify_chapter_raw_images(raw_dir, strict=True)
        if not report.is_valid:
            err_msg = f"Raw image verification failed: {'; '.join(report.errors)}"
            update_checkpoint(
                chap_dir,
                StageType.CRAWL,
                CheckpointStatus.FAILED,
                error=err_msg,
            )
            raise CliValidationError(err_msg)

    update_checkpoint(
        chap_dir,
        StageType.CRAWL,
        CheckpointStatus.COMPLETED,
        artifacts=imported_files,
    )

    result: Dict[str, Any] = {
        "project_slug": project_slug,
        "chapter_number": str(chapter_num),
        "raw_images_count": raw_count,
        "source": url or str(import_dir),
        "status": "completed",
    }
    if report:
        result["verification"] = {
            "is_valid": report.is_valid,
            "total_pages": report.total_pages,
            "corrupt_pages": report.corrupt_pages,
            "duplicate_pages": report.duplicate_pages,
            "missing_indices": report.missing_indices,
        }

    return result
