"""
src/cli/segment_cmd.py
Subcommand handler: manga-recap segment
Segments raw manga page images into story panels, generates 4x4 contact sheets,
and creates schema-compliant manifest.json.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Dict

from src.cli.exit_codes import CliValidationError
from src.core.contact_sheet import ContactSheetGenerator
from src.core.schemas import (
    CheckpointStatus,
    PanelManifest,
    RawImageInfo,
    StageType,
    utc_iso_now,
)
from src.core.storage import (
    StorageFileNotFoundError,
    atomic_write_json,
    calculate_sha256,
    get_chapter_dir,
    get_project_dir,
    init_chapter_layout,
    init_project_layout,
    read_json,
    reset_stage_checkpoint,
    update_checkpoint,
)
from src.core.streaming_segmenter import StreamingSegmenter
from src.core.utils import get_project_data_dir


def handle_segment(args: argparse.Namespace) -> Dict[str, Any]:
    """Segments raw chapter images into panels and generates contact sheets."""
    project_slug = getattr(args, "project", "")
    chapter_num = getattr(args, "chapter", None)
    target_width = getattr(args, "target_width", 1080)
    threshold_variance = getattr(args, "threshold_variance", 8.0)
    min_gutter_height = getattr(args, "min_gutter_height", 15)
    margin_padding = getattr(args, "margin_padding", 12)
    cell_size = getattr(args, "cell_size", 480)
    force = getattr(args, "force", False)

    if not project_slug:
        raise CliValidationError("Project slug (--project) is required.")
    if chapter_num is None:
        raise CliValidationError("Chapter number (--chapter) is required.")

    # 1. Resolve project and chapter directories
    project_dir = get_project_dir(project_slug)
    if not project_dir.exists():
        fallback_dir = get_project_data_dir(project_slug)
        if fallback_dir.exists():
            project_dir = fallback_dir
        else:
            init_project_layout(project_slug, title=project_slug)
            project_dir = get_project_dir(project_slug)

    chap_dir = get_chapter_dir(project_slug, chapter_num)
    if not chap_dir.exists() or not (chap_dir / "chapter.json").exists():
        init_project_layout(project_slug, title=project_slug)
        chap_dir = init_chapter_layout(project_slug, chapter_num)

    manifest_path = chap_dir / "manifest.json"
    chap_json_path = chap_dir / "chapter.json"
    if manifest_path.exists() and not force:
        try:
            chap_data = read_json(chap_json_path)
            stages = chap_data.get("stages", {})
            seg_stage = stages.get("segment", {})
            if seg_stage.get("status") == CheckpointStatus.COMPLETED.value:
                m_data = read_json(manifest_path)
                manifest = PanelManifest.model_validate(m_data)
                return {
                    "project_slug": project_slug,
                    "chapter_number": str(chapter_num),
                    "total_raw_images": manifest.total_raw_images,
                    "total_panels": manifest.total_panels,
                    "total_contact_sheets": len(manifest.contact_sheets),
                    "manifest_path": str(manifest_path.relative_to(chap_dir)),
                    "status": "completed",
                    "cached": True,
                }
        except Exception:
            pass

    raw_dir = chap_dir / "raw"
    if not raw_dir.exists():
        raise CliValidationError(f"Raw directory does not exist: {raw_dir}. Run 'crawl' first.")

    valid_exts = {".jpg", ".jpeg", ".png", ".webp"}
    raw_files = [f for f in raw_dir.iterdir() if f.suffix.lower() in valid_exts and f.is_file()]
    if not raw_files:
        raise CliValidationError(f"No raw images found in {raw_dir}. Run 'crawl' first.")

    # 2. Update checkpoint -> RUNNING
    update_checkpoint(chap_dir, StageType.SEGMENT, CheckpointStatus.RUNNING)

    try:
        panels_dir = chap_dir / "panels"
        panels_dir.mkdir(parents=True, exist_ok=True)
        contact_sheets_dir = chap_dir / "contact_sheets"
        contact_sheets_dir.mkdir(parents=True, exist_ok=True)
        if force:
            for old_panel in panels_dir.glob("*"):
                try:
                    old_panel.unlink()
                except Exception:
                    pass
            for old_sheet in contact_sheets_dir.glob("*"):
                try:
                    old_sheet.unlink()
                except Exception:
                    pass

            # Invalidate downstream audio, subtitles, and video
            audio_clips_dir = chap_dir / "audio" / "clips"
            if audio_clips_dir.exists():
                for f in audio_clips_dir.glob("*.wav"):
                    try:
                        f.unlink()
                    except Exception:
                        pass
            subs_dir = chap_dir / "subtitles"
            if subs_dir.exists():
                for f in subs_dir.glob("*.srt"):
                    try:
                        f.unlink()
                    except Exception:
                        pass
            video_dir = chap_dir / "video"
            if video_dir.exists():
                for f in video_dir.glob("*.mp4"):
                    try:
                        f.unlink()
                    except Exception:
                        pass

            # Reset downstream checkpoints to PENDING
            for stg in [StageType.SCRIPT, StageType.TTS, StageType.RENDER, StageType.FINALIZE]:
                reset_stage_checkpoint(chap_dir, stg)

        # 3. Execute StreamingSegmenter
        segmenter = StreamingSegmenter(
            target_width=target_width,
            threshold_variance=threshold_variance,
            white_threshold=240.0,
            black_threshold=18.0,
            min_gutter_height=min_gutter_height,
            margin_padding=margin_padding,
            sufficiency_variance=100.0,
        )

        page_maps = segmenter.build_page_maps(raw_files)
        row_means, row_vars = segmenter.compute_1d_profiles(page_maps)
        intervals = segmenter.detect_cut_intervals(row_means, row_vars)
        refined_intervals = segmenter.refine_intervals(intervals, row_vars)

        panels = segmenter.extract_panels(page_maps, refined_intervals, panels_dir)

        # 4. Generate 4x4 Contact Sheets
        contact_sheet_gen = ContactSheetGenerator(cell_size=cell_size, columns=4, rows=4)
        contact_sheets = contact_sheet_gen.generate_sheets(panels, panels_dir, contact_sheets_dir)

        # 5. Build RawImageInfo records
        raw_images_info = [
            RawImageInfo(
                filename=p.filename,
                relative_path=f"raw/{p.filename}",
                width=p.orig_width,
                height=p.orig_height,
                sha256=calculate_sha256(p.path),
            )
            for p in page_maps
        ]

        # 6. Build and write manifest.json
        source_url = None
        chap_json_path = chap_dir / "chapter.json"
        if chap_json_path.exists():
            try:
                chap_data = read_json(chap_json_path)
                source_url = chap_data.get("source_url")
            except Exception:
                pass

        manifest = PanelManifest(
            schema_version="3.0.0",
            project_slug=project_slug,
            chapter_number=str(chapter_num),
            source_url=source_url,
            created_at=utc_iso_now(),
            total_raw_images=len(page_maps),
            total_panels=len(panels),
            raw_images=raw_images_info,
            panels=panels,
            contact_sheets=contact_sheets,
        )
        manifest_path = chap_dir / "manifest.json"
        atomic_write_json(manifest_path, manifest)

        # 7. Update checkpoint -> COMPLETED
        artifacts = (
            [p.relative_path for p in panels]
            + [s.relative_path for s in contact_sheets]
            + ["manifest.json"]
        )
        update_checkpoint(
            chap_dir,
            StageType.SEGMENT,
            CheckpointStatus.COMPLETED,
            artifacts=artifacts,
        )

        return {
            "project_slug": project_slug,
            "chapter_number": str(chapter_num),
            "total_raw_images": len(page_maps),
            "total_panels": len(panels),
            "total_contact_sheets": len(contact_sheets),
            "manifest_path": str(manifest_path.relative_to(chap_dir)),
            "status": "completed",
        }
    except Exception as e:
        update_checkpoint(chap_dir, StageType.SEGMENT, CheckpointStatus.FAILED, error=str(e))
        raise
