"""
src/cli/thumbnail_cmd.py
Subcommand handler: manga-recap thumbnail
Provides interactive candidate discovery, aesthetic 16:9 composition, and YouTube thumbnail updating.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.cli.exit_codes import CliValidationError
from src.core.logger import setup_logger
from src.core.storage import get_chapter_dir, get_output_dir, get_project_dir, read_json
from src.render.thumbnail_composer import compose_thumbnail, scan_thumbnail_candidates

logger = setup_logger("thumbnail_cmd")


def _resolve_panel_path(chap_dir: Path, panel_ref: Optional[str]) -> Optional[Path]:
    """Resolves panel reference (e.g., 'panel_026.jpg', 'panel_026', '26', or full path)."""
    if not panel_ref:
        return None

    cand_path = Path(panel_ref)
    if cand_path.exists():
        return cand_path.resolve()

    panels_dir = chap_dir / "panels"
    ref_str = str(panel_ref).strip()

    # Try exact filename
    if (panels_dir / ref_str).exists():
        return panels_dir / ref_str

    # Try with .jpg
    if not ref_str.endswith(".jpg") and not ref_str.endswith(".png"):
        if (panels_dir / f"{ref_str}.jpg").exists():
            return panels_dir / f"{ref_str}.jpg"

    # Try padded numeric
    if ref_str.isdigit():
        padded_name = f"panel_{int(ref_str):03d}.jpg"
        if (panels_dir / padded_name).exists():
            return panels_dir / padded_name

    # Try panel_ prefix
    if not ref_str.startswith("panel_"):
        if (panels_dir / f"panel_{ref_str}.jpg").exists():
            return panels_dir / f"panel_{ref_str}.jpg"

    return None


def handle_thumbnail(args: argparse.Namespace) -> Dict[str, Any]:
    """Handles the 'thumbnail' CLI subcommand."""
    from src.core.execution_policy import enforce_execution_policy
    enforce_execution_policy("thumbnail", args)
    project_slug = getattr(args, "project", "")
    if not project_slug:
        raise CliValidationError("Project slug is required (-p/--project).")

    chapter_num = str(getattr(args, "chapter", "1")).strip()
    proj_dir = get_project_dir(project_slug)
    if not proj_dir.exists():
        raise CliValidationError(f"Project '{project_slug}' not found at {proj_dir}")

    chap_dir = get_chapter_dir(project_slug, chapter_num)
    if not chap_dir.exists():
        raise CliValidationError(f"Chapter {chapter_num} directory not found at {chap_dir}")

    panels_dir = chap_dir / "panels"
    if not panels_dir.exists():
        raise CliValidationError(f"Panels directory not found at {panels_dir}. Run segment first.")

    # 1. Candidate Scanning Mode
    show_candidates = getattr(args, "candidates", False)
    left_arg = getattr(args, "left", None)
    center_arg = getattr(args, "center", None) or getattr(args, "main", None)
    right_arg = getattr(args, "right", None)

    # If user requests candidate scan or provided zero panel inputs
    if show_candidates or (not left_arg and not center_arg and not right_arg and not getattr(args, "auto", False)):
        script_segments = None
        script_p = chap_dir / "script.json"
        if script_p.exists():
            try:
                s_data = read_json(script_p)
                script_segments = s_data.get("segments", [])
            except Exception:
                pass

        limit = getattr(args, "limit", 8)
        candidates = scan_thumbnail_candidates(panels_dir, script_segments=script_segments, limit=limit)
        return {
            "status": "success",
            "action": "candidates",
            "project_slug": project_slug,
            "chapter_number": chapter_num,
            "total_candidates": len(candidates),
            "candidates": candidates,
            "instructions": "Use --left <id>, --center <id>, --right <id> to compose your thumbnail, with --title and --subtitle.",
        }

    # 2. Composition Mode
    from src.core.series_setup import require_series_setup, _normalize_bool
    setup = require_series_setup(project_slug)
    if _normalize_bool(setup.answers["Q07"].user_answer) is not True:
        raise CliValidationError("Thumbnail composition requires explicit Q07 enablement")
    if getattr(args, "auto", False):
        raise CliValidationError("Select source panels explicitly; --auto cannot satisfy the thumbnail detail gate")
    if getattr(args, "upload", False):
        raise CliValidationError("Use the YouTube publishing flow with explicit channel selection; thumbnail --upload is disabled")

    left_path = _resolve_panel_path(chap_dir, left_arg)
    center_path = _resolve_panel_path(chap_dir, center_arg)
    right_path = _resolve_panel_path(chap_dir, right_arg)

    if not center_path:
        raise CliValidationError(
            f"Could not resolve center/main panel '{center_arg}'. Specify a valid panel (e.g. --center panel_026.jpg)."
        )

    has_text = not getattr(args, "no_text", False)
    title = getattr(args, "title", None)
    subtitle = getattr(args, "subtitle", None)
    badge = getattr(args, "badge", None)

    font_hint = getattr(args, "font", None)
    color_preset = getattr(args, "color_preset", None)
    if has_text and (not title or not color_preset):
        raise CliValidationError("Choose --title and --color-preset explicitly, or --no-text")
    from src.core.storage import load_manifest
    from src.render.visual_policy import evidence_path
    manifest = load_manifest(chap_dir)
    approved_paths = {evidence_path(chap_dir, panel).resolve() for panel in manifest.panels}
    for selected in (left_path, center_path, right_path):
        if selected and selected.resolve() not in approved_paths:
            raise CliValidationError("Thumbnail source must be verified chapter evidence")

    stroke_width = getattr(args, "stroke_width", 6)

    # Target output paths
    out_dir = get_output_dir(project_slug)
    out_dir.mkdir(parents=True, exist_ok=True)
    custom_out = getattr(args, "output", None)

    if custom_out:
        final_thumb_p = Path(custom_out).resolve()
    else:
        final_thumb_p = out_dir / f"chapter_{chapter_num}_thumbnail.jpg"

    chap_thumb_p = chap_dir / "thumbnail.jpg"

    logger.info(f"Composing thumbnail for {project_slug} ch.{chapter_num} -> {final_thumb_p}...")
    compose_thumbnail(
        left_image_path=left_path,
        center_image_path=center_path,
        right_image_path=right_path,
        has_text=has_text,
        title=title,
        subtitle=subtitle,
        badge=badge,
        font_hint=font_hint,
        color_preset=color_preset or "yellow_black",
        stroke_width=stroke_width,
        output_path=final_thumb_p,
    )

    # Sync copy to chapter internal dir
    shutil.copy2(final_thumb_p, chap_thumb_p)
    from src.core.storage import atomic_write_json
    from src.render.artifact_validation import fingerprint
    atomic_write_json(chap_dir / "thumbnail_selection.json", {
        "schema_version": "4.1.0", "selection_source": "explicit_cli_arguments",
        "panels": {str(path.resolve().relative_to(chap_dir.resolve())): fingerprint(path)
                   for path in (left_path, center_path, right_path) if path},
        "title": title, "subtitle": subtitle, "badge": badge,
        "has_text": has_text, "color_preset": color_preset, "font": font_hint,
        "artifact_sha256": fingerprint(chap_thumb_p),
    })

    upload_result = None
    if getattr(args, "upload", False):
        from src.cli.youtube_cmd import get_authenticated_service
        account = getattr(args, "account", "default")
        # Load video ID from metadata or project records
        yt_meta_p = chap_dir / "youtube_metadata.json"
        video_id = None
        if yt_meta_p.exists():
            try:
                m_data = read_json(yt_meta_p)
                video_id = m_data.get("video_id")
            except Exception:
                pass

        if not video_id:
            # Check review pack or command output
            rp_json = chap_dir / "review_pack.json"
            if rp_json.exists():
                try:
                    rp_data = read_json(rp_json)
                    video_id = rp_data.get("video_id")
                except Exception:
                    pass

        if not video_id:
            # Fallback to checking youtube upload history or ask user
            logger.warning("No recorded video_id found in chapter metadata to update thumbnail on YouTube.")
        else:
            try:
                logger.info(f"Updating YouTube video '{video_id}' thumbnail via account '{account}'...")
                service = get_authenticated_service(account=account)
                import importlib
                media_mod = importlib.import_module("googleapiclient.http")
                media_file_upload = getattr(media_mod, "MediaFileUpload")
                service.thumbnails().set(
                    videoId=video_id,
                    media_body=media_file_upload(str(final_thumb_p), mimetype="image/jpeg"),
                ).execute()
                upload_result = {"status": "success", "video_id": video_id}
                logger.info(f"Successfully updated YouTube thumbnail for video {video_id}!")
            except Exception as e:
                logger.error(f"Failed to upload thumbnail to YouTube: {e}")
                upload_result = {"status": "error", "error": str(e)}

    return {
        "status": "success",
        "action": "compose",
        "project_slug": project_slug,
        "chapter_number": chapter_num,
        "thumbnail_path": str(final_thumb_p),
        "chapter_thumbnail_path": str(chap_thumb_p),
        "layout": {
            "left_panel": str(left_path.name) if left_path else None,
            "center_panel": str(center_path.name) if center_path else None,
            "right_panel": str(right_path.name) if right_path else None,
        },
        "typography": {
            "has_text": has_text,
            "title": title,
            "subtitle": subtitle,
            "badge": badge,
            "color_preset": color_preset,
        },
        "youtube_upload": upload_result,
    }
