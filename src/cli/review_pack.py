"""
src/cli/review_pack.py
Subcommand handler: manga-recap review-pack
Generates lightweight, transparent Human Review Packs in Markdown, HTML, and JSON.
Reuses existing contact sheets, manifests, scripts, audio clips, and preview videos.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from src.cli.exit_codes import CliValidationError, ExitCode
from src.cli.response import print_response
from src.core.schemas import (
    ChapterMetadata,
    CheckpointStatus,
    HumanReviewStatus,
    PanelManifest,
    ProjectConfig,
    RecapScript,
)
from src.core.storage import (
    StorageFileNotFoundError,
    atomic_write_json,
    get_chapter_dir,
    get_project_dir,
    read_json,
)
from src.core.utils import get_project_data_dir


def build_review_pack_data(
    project_dir: Path, chap_dir: Path, project_slug: str, chapter_num: str
) -> Dict[str, Any]:
    """Extracts and correlates all metadata and assets for the review pack."""
    # 1. Read Project Config
    project_cfg_dict: Dict[str, Any] = {}
    p_json = project_dir / "project.json"
    if p_json.exists():
        try:
            project_cfg_dict = read_json(p_json)
        except Exception:
            pass

    # 2. Read Chapter Metadata
    chap_meta_dict: Dict[str, Any] = {}
    c_json = chap_dir / "chapter.json"
    if c_json.exists():
        try:
            chap_meta_dict = read_json(c_json)
        except Exception:
            pass

    technical_status = chap_meta_dict.get(
        "technical_status", chap_meta_dict.get("overall_status", "pending")
    )
    human_review_status = chap_meta_dict.get(
        "human_review_status", HumanReviewStatus.UNREVIEWED.value
    )

    # 3. Read Manifest
    manifest_panels: List[Dict[str, Any]] = []
    raw_contact_sheets: List[Any] = []
    m_json = chap_dir / "manifest.json"
    if m_json.exists():
        try:
            m_data = read_json(m_json)
            manifest_panels = m_data.get("panels", [])
            raw_contact_sheets = m_data.get("contact_sheets", [])
        except Exception:
            pass

    # Normalize contact sheets (support both dicts and string filenames)
    contact_sheets: List[Dict[str, str]] = []
    for cs in raw_contact_sheets:
        if isinstance(cs, dict):
            rel = cs.get("relative_path") or f"contact_sheets/{cs.get('sheet_id', 'sheet_01')}.jpg"
            fn = Path(rel).name
            contact_sheets.append({"filename": fn, "relative_path": rel, "sheet_id": cs.get("sheet_id", fn)})
        elif isinstance(cs, str):
            rel = f"contact_sheets/{cs}" if not cs.startswith("contact_sheets/") else cs
            fn = Path(cs).name
            contact_sheets.append({"filename": fn, "relative_path": rel, "sheet_id": Path(cs).stem})

    # Fallback scan for contact sheets on disk if empty
    cs_dir = chap_dir / "contact_sheets"
    if cs_dir.exists() and not contact_sheets:
        disk_sheets = sorted([
            f.name
            for f in cs_dir.glob("*")
            if f.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
        ])
        for cs_name in disk_sheets:
            contact_sheets.append({
                "filename": cs_name,
                "relative_path": f"contact_sheets/{cs_name}",
                "sheet_id": Path(cs_name).stem,
            })

    # Fallback scan for panels on disk
    panels_dir = chap_dir / "panels"
    disk_panels = sorted([
        f.name
        for f in panels_dir.glob("*")
        if f.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
    ]) if panels_dir.exists() else []

    # Canonical panels registry
    canonical_panels: Dict[str, Dict[str, Any]] = {}
    for p in manifest_panels:
        fn = p.get("filename") or f"{p.get('panel_id')}.jpg"
        canonical_panels[fn] = p

    for dp in disk_panels:
        if dp not in canonical_panels:
            canonical_panels[dp] = {
                "filename": dp,
                "panel_id": Path(dp).stem,
                "status": "active",
                "width": None,
                "height": None,
                "aspect_ratio": None,
            }

    # 4. Read Script
    script_segments: List[Dict[str, Any]] = []
    script_meta: Dict[str, Any] = {}
    s_json = chap_dir / "script.json"
    if s_json.exists():
        try:
            s_data = read_json(s_json)
            script_segments = s_data.get("segments", [])
            script_meta = s_data.get("metadata", {})
        except Exception:
            pass

    # 5. Analyze N:M Mapping, Omissions, and Reused Panels
    panel_usage_count: Dict[str, int] = {}
    panel_to_segments: Dict[str, List[str]] = {}
    flagged_segments: List[Dict[str, Any]] = []
    total_audio_seconds = 0.0

    for seg in script_segments:
        s_id = str(seg.get("seq_id", ""))
        p_ids = seg.get("panel_ids", [])
        dur = float(seg.get("duration") or 0.0)
        total_audio_seconds += dur

        text = seg.get("narrator_text", "")
        notes = seg.get("notes", "")
        status = seg.get("status", "")
        if "[CAN_XAC_MINH" in text or "[CAN_XAC_MINH" in str(notes) or status == "needs_review":
            flagged_segments.append({
                "seq_id": s_id,
                "text": text,
                "status": status,
                "panel_ids": p_ids,
            })

        for pid in p_ids:
            panel_usage_count[pid] = panel_usage_count.get(pid, 0) + 1
            panel_to_segments.setdefault(pid, []).append(s_id)

    referenced_pids_set = set(panel_usage_count.keys())

    # Detect Omissions from canonical panels
    omitted_panels: List[Dict[str, Any]] = []
    referenced_canonical_count = 0

    for fn, p_info in sorted(canonical_panels.items()):
        pid_alias = p_info.get("panel_id", "")
        is_referenced = (fn in referenced_pids_set) or (pid_alias in referenced_pids_set)
        if is_referenced:
            referenced_canonical_count += 1
        else:
            omitted_panels.append({
                "panel_id": fn,
                "width": p_info.get("width"),
                "height": p_info.get("height"),
                "aspect_ratio": p_info.get("aspect_ratio"),
                "manifest_status": p_info.get("status", "unknown"),
                "note": "Non-essential panel omitted by Agent (Verify essential story retention)",
            })

    reused_panels: List[Dict[str, Any]] = []
    for pid, count in panel_usage_count.items():
        if count > 1:
            reused_panels.append({
                "panel_id": pid,
                "use_count": count,
                "segments": panel_to_segments.get(pid, []),
            })

    # 6. Discover Media Files
    video_dir = chap_dir / "video"
    video_files: List[Dict[str, Any]] = []
    if video_dir.exists():
        for vf in sorted(video_dir.glob("*.mp4")):
            video_files.append({
                "filename": vf.name,
                "path": str(vf),
                "size_bytes": vf.stat().st_size,
            })

    subtitles_dir = chap_dir / "subtitles"
    subtitles_files = sorted([
        f.name for f in subtitles_dir.glob("*.srt")
    ]) if subtitles_dir.exists() else []

    audio_dir = chap_dir / "audio" / "clips"
    if not audio_dir.exists():
        audio_dir = chap_dir / "audio"
    audio_clips_count = len(list(audio_dir.glob("*.wav"))) if audio_dir.exists() else 0

    return {
        "project_slug": project_slug,
        "project_title": project_cfg_dict.get("title", project_slug),
        "chapter_number": str(chapter_num),
        "chapter_title": chap_meta_dict.get("title", f"Chapter {chapter_num}"),
        "technical_status": technical_status,
        "human_review_status": human_review_status,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "summary": {
            "total_segments": len(script_segments),
            "total_audio_duration_seconds": round(total_audio_seconds, 2),
            "total_manifest_panels": len(canonical_panels),
            "referenced_panels_count": referenced_canonical_count,
            "omitted_panels_count": len(omitted_panels),
            "reused_panels_count": len(reused_panels),
            "flagged_needs_review_count": len(flagged_segments),
            "contact_sheets_count": len(contact_sheets),
            "audio_clips_count": audio_clips_count,
            "has_video": len(video_files) > 0,
        },
        "contact_sheets": contact_sheets,
        "segments": script_segments,
        "omitted_panels": omitted_panels,
        "reused_panels": reused_panels,
        "flagged_segments": flagged_segments,
        "video_files": video_files,
        "subtitle_files": subtitles_files,
    }


def generate_markdown_review_pack(data: Dict[str, Any], chap_dir: Path) -> str:
    """Renders a comprehensive, clean Markdown review pack document."""
    lines: List[str] = []
    lines.append(f"# Human Review Pack — Chapter {data['chapter_number']}: {data['chapter_title']}")
    lines.append("")
    lines.append(f"- **Project:** `{data['project_slug']}` ({data['project_title']})")
    lines.append(f"- **Generated At:** {data['created_at']}")
    lines.append(f"- **Technical Status:** `{data['technical_status']}`")
    lines.append(f"- **Human Review Status:** `{data['human_review_status']}`")
    lines.append("")

    # Summary table
    s = data["summary"]
    lines.append("## 1. Chapter Execution Summary")
    lines.append("")
    lines.append("| Metric | Value | Meaning |")
    lines.append("|---|---|---|")
    lines.append(f"| **Segments Count** | `{s['total_segments']}` | Total narration segments in `script.json` |")
    lines.append(f"| **Total Audio Duration** | `{s['total_audio_duration_seconds']}s` | Combined speech waveform length |")
    lines.append(f"| **Total Panels in Manifest** | `{s['total_manifest_panels']}` | Extracted chapter panels on disk |")
    lines.append(f"| **Referenced Panels** | `{s['referenced_panels_count']}` | Unique panels included in video |")
    lines.append(f"| **Omitted Panels** | `{s['omitted_panels_count']}` | Non-essential panels skipped by Agent |")
    lines.append(f"| **Reused Panels** | `{s['reused_panels_count']}` | Panels referenced in >1 segment |")
    lines.append(f"| **Needs Review Flags** | `{s['flagged_needs_review_count']}` | Ambiguous items tagged `[CAN_XAC_MINH]` |")
    lines.append(f"| **Video Output** | `{'Yes (' + str(len(data['video_files'])) + ' files)' if s['has_video'] else 'Not Rendered'}` | Rendered MP4 files |")
    lines.append("")

    # Reviewer Checklist
    lines.append("## 2. Human Reviewer Inspection Checklist")
    lines.append("")
    lines.append("Please evaluate the following 5 criteria before approving:")
    lines.append("1. [ ] **Faithful Storytelling**: The script faithfully reflects original manga plot and character dialogues without hallucinating ungrounded lore.")
    lines.append("2. [ ] **Essential Panel Retention**: All critical story progression, martial arts climax, and dialogue panels are included in the video (inspect Omitted Panels table below).")
    lines.append("3. [ ] **Voice Authenticity & Cadence**: Character voices, pronouns, and emotional expressions sound natural.")
    lines.append("4. [ ] **Visual Framing & Anti-Chat-Box**: Character faces/actions are centered without cutoffs; camera does NOT zoom into empty speech bubbles.")
    lines.append("5. [ ] **A/V Sync & Subtitle Legibility**: Narration aligns with on-screen panels, and subtitles are easy to read.")
    lines.append("")

    # Contact Sheets
    lines.append("## 3. Visual Contact Sheets")
    lines.append("")
    if data["contact_sheets"]:
        lines.append("Inspect these 4x4 visual grids to rapidly verify scene continuity:")
        lines.append("")
        for cs in data["contact_sheets"]:
            cs_fn = cs["filename"] if isinstance(cs, dict) else str(cs)
            cs_rel = cs.get("relative_path", f"contact_sheets/{cs_fn}") if isinstance(cs, dict) else f"contact_sheets/{cs_fn}"
            lines.append(f"- **{cs_fn}**: `![]({cs_rel})` (Path: `{chap_dir / cs_rel}`)")
    else:
        lines.append("*No contact sheet images found in `contact_sheets/`.*")
    lines.append("")

    # Flagged Segments (if any)
    if data["flagged_segments"]:
        lines.append("## 4. ⚠️ Segments Requiring Editorial Verification (`[CAN_XAC_MINH]`)")
        lines.append("")
        lines.append("| Seq ID | Panels | Status | Narration / Flagged Issue |")
        lines.append("|---|---|---|---|")
        for f in data["flagged_segments"]:
            pids = ", ".join(f.get("panel_ids", []))
            txt = f.get("text", "").replace("|", "\\|")
            lines.append(f"| **{f.get('seq_id')}** | `{pids}` | `{f.get('status')}` | {txt} |")
        lines.append("")

    # Alignment Table
    lines.append("## 5. Script-to-Panel Alignment Table (N:M Mapping)")
    lines.append("")
    lines.append("| Seq ID | Panels (`panel_ids`) | Character / Voice | Camera / Focus | Duration | Narrator Text & Direct Dialogue | Status |")
    lines.append("|---|---|---|---|---|---|---|")
    for seg in data["segments"]:
        s_id = seg.get("seq_id", "")
        p_ids = ", ".join(seg.get("panel_ids", []))
        char = seg.get("character") or "Narrator"
        voice = seg.get("voice_id", "")
        char_voice = f"{char} ({voice})" if voice else char
        motion = seg.get("camera_motion", "static")
        dur = f"{seg.get('duration')}s" if seg.get("duration") else "pending"
        text = seg.get("narrator_text", "").replace("|", "\\|")
        status = seg.get("status", "pending")
        lines.append(f"| **{s_id}** | `{p_ids}` | {char_voice} | `{motion}` | `{dur}` | {text} | `{status}` |")
    lines.append("")

    # Omitted Panels Log
    lines.append("## 6. Omitted Panels Log (Essential Story Verification)")
    lines.append("")
    if data["omitted_panels"]:
        lines.append("The following panels from `manifest.json` were omitted from `script.json`. Verify that none contain critical story revelations:")
        lines.append("")
        lines.append("| Panel ID | Dimensions | Aspect Ratio | Manifest Status | Reviewer Verification Note |")
        lines.append("|---|---|---|---|---|")
        for op in data["omitted_panels"]:
            dim = f"{op.get('width', '?')}x{op.get('height', '?')}"
            ar = f"{op.get('aspect_ratio', '?'):.2f}" if isinstance(op.get('aspect_ratio'), (int, float)) else "?"
            lines.append(f"| `{op['panel_id']}` | {dim} | {ar} | `{op['manifest_status']}` | {op['note']} |")
    else:
        lines.append("*All panels in manifest are referenced in the script (100% panel coverage).*")
    lines.append("")

    # Reused Panels Log
    if data["reused_panels"]:
        lines.append("## 7. Reused Panels Log")
        lines.append("")
        lines.append("| Panel ID | Usage Count | Referenced in Segments |")
        lines.append("|---|---|---|")
        for rp in data["reused_panels"]:
            segs = ", ".join(rp["segments"])
            lines.append(f"| `{rp['panel_id']}` | {rp['use_count']} times | `{segs}` |")
        lines.append("")

    # Media Artifacts
    lines.append("## 8. Media Artifacts & Preview Files")
    lines.append("")
    if data["video_files"]:
        lines.append("### Video Outputs")
        for vf in data["video_files"]:
            sz_mb = round(vf["size_bytes"] / (1024 * 1024), 2)
            lines.append(f"- **{vf['filename']}** ({sz_mb} MB) -> `{vf['path']}`")
    else:
        lines.append("- *No video files rendered yet (`video/*.mp4`).*")

    if data["subtitle_files"]:
        lines.append("### Subtitles")
        for sf in data["subtitle_files"]:
            lines.append(f"- `{sf}` (Path: `{chap_dir / 'subtitles' / sf}`)")

    lines.append("")
    lines.append("---")
    lines.append("## 9. Editorial Decision & Feedback Protocol")
    lines.append("")
    lines.append("### To Approve:")
    lines.append("After a human has watched and approved the preview, record the exact hashes from `review_pack.json`:")
    lines.append("```bash")
    binding = data.get("review_binding", {})
    lines.append(f"manga-recap review-approve -p {data['project_slug']} --reviewer HUMAN_NAME --config-fingerprint {binding.get('config_fingerprint', 'CONFIG_HASH')} --artifact-hash {binding.get('artifact_hash', 'ARTIFACT_HASH')} --json")
    lines.append("```")
    lines.append("")
    lines.append("### To Request Edits:")
    lines.append("Specify feedback targeting exact `seq_id` or `panel_ids`. The agent will edit `script.json` and incrementally re-synthesize only the modified clips.")
    lines.append("")
    return "\n".join(lines)


def generate_html_review_pack(data: Dict[str, Any], chap_dir: Path) -> str:
    """Renders a standalone, responsive HTML review pack."""
    title = html.escape(f"Review Pack: {data['project_title']} - Ch.{data['chapter_number']}")
    s = data["summary"]

    html_parts: List[str] = []
    html_parts.append("<!DOCTYPE html>")
    html_parts.append("<html lang=\"vi\">")
    html_parts.append("<head>")
    html_parts.append("  <meta charset=\"UTF-8\">")
    html_parts.append("  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">")
    html_parts.append(f"  <title>{title}</title>")
    html_parts.append("  <style>")
    html_parts.append("    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; line-height: 1.6; color: #1f2937; background: #f9fafb; margin: 0; padding: 24px; }")
    html_parts.append("    .container { max-width: 1200px; margin: 0 auto; background: #fff; padding: 32px; border-radius: 12px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1); }")
    html_parts.append("    h1, h2, h3 { color: #111827; }")
    html_parts.append("    h1 { border-bottom: 2px solid #e5e7eb; padding-bottom: 12px; }")
    html_parts.append("    .badge { display: inline-block; padding: 4px 10px; border-radius: 9999px; font-size: 0.85rem; font-weight: 600; }")
    html_parts.append("    .badge-approved { background: #d1fae5; color: #065f46; }")
    html_parts.append("    .badge-unreviewed { background: #fef3c7; color: #92400e; }")
    html_parts.append("    .badge-changes { background: #fee2e2; color: #991b1b; }")
    html_parts.append("    .grid-metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin: 24px 0; }")
    html_parts.append("    .metric-card { background: #f3f4f6; padding: 16px; border-radius: 8px; border-left: 4px solid #3b82f6; }")
    html_parts.append("    .metric-card h4 { margin: 0 0 8px 0; color: #4b5563; font-size: 0.9rem; text-transform: uppercase; }")
    html_parts.append("    .metric-card p { margin: 0; font-size: 1.5rem; font-weight: 700; color: #111827; }")
    html_parts.append("    table { width: 100%; border-collapse: collapse; margin: 20px 0; font-size: 0.95rem; }")
    html_parts.append("    th, td { border: 1px solid #e5e7eb; padding: 10px 14px; text-align: left; }")
    html_parts.append("    th { background: #f9fafb; font-weight: 600; color: #374151; }")
    html_parts.append("    tr:nth-child(even) { background: #f9fafb; }")
    html_parts.append("    .flag-warning { background: #fffbeb; color: #b45309; font-weight: 600; padding: 2px 6px; border-radius: 4px; }")
    html_parts.append("    .gallery { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 16px; margin: 20px 0; }")
    html_parts.append("    .gallery-item { border: 1px solid #e5e7eb; border-radius: 8px; overflow: hidden; background: #fafafa; }")
    html_parts.append("    .gallery-item img { width: 100%; height: auto; display: block; }")
    html_parts.append("    .gallery-item p { padding: 8px 12px; margin: 0; font-size: 0.85rem; font-weight: 600; }")
    html_parts.append("    .checklist { background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 8px; padding: 20px; margin: 24px 0; }")
    html_parts.append("    .checklist ul { margin: 0; padding-left: 20px; }")
    html_parts.append("    .checklist li { margin-bottom: 8px; }")
    html_parts.append("  </style>")
    html_parts.append("</head>")
    html_parts.append("<body>")
    html_parts.append("  <div class=\"container\">")

    # Header
    status_class = "badge-unreviewed"
    if data["human_review_status"] == HumanReviewStatus.APPROVED.value:
        status_class = "badge-approved"
    elif data["human_review_status"] == HumanReviewStatus.CHANGES_REQUESTED.value:
        status_class = "badge-changes"

    html_parts.append(f"    <h1>{title}</h1>")
    html_parts.append("    <div>")
    html_parts.append(f"      <span><strong>Project:</strong> {html.escape(data['project_slug'])} | </span>")
    html_parts.append(f"      <span><strong>Technical:</strong> <code>{html.escape(str(data['technical_status']))}</code> | </span>")
    html_parts.append(f"      <span><strong>Review Status:</strong> <span class=\"badge {status_class}\">{html.escape(str(data['human_review_status']))}</span></span>")
    html_parts.append("    </div>")

    # Metrics
    html_parts.append("    <div class=\"grid-metrics\">")
    html_parts.append(f"      <div class=\"metric-card\"><h4>Segments</h4><p>{s['total_segments']}</p></div>")
    html_parts.append(f"      <div class=\"metric-card\"><h4>Audio Duration</h4><p>{s['total_audio_duration_seconds']}s</p></div>")
    html_parts.append(f"      <div class=\"metric-card\"><h4>Referenced Panels</h4><p>{s['referenced_panels_count']} / {s['total_manifest_panels']}</p></div>")
    html_parts.append(f"      <div class=\"metric-card\"><h4>Omitted Panels</h4><p>{s['omitted_panels_count']}</p></div>")
    html_parts.append(f"      <div class=\"metric-card\"><h4>Needs Review</h4><p>{s['flagged_needs_review_count']}</p></div>")
    html_parts.append("    </div>")

    # Checklist
    html_parts.append("    <div class=\"checklist\">")
    html_parts.append("      <h3>Human Review Inspection Checklist</h3>")
    html_parts.append("      <ul>")
    html_parts.append("        <li><strong>Faithful Storytelling</strong>: Confirm narration faithfully reflects manga panel dialogues and observable actions without hallucinated lore.</li>")
    html_parts.append("        <li><strong>Essential Panel Retention</strong>: Confirm that all critical story beats and combat climax panels are retained (check Omitted Panels table below).</li>")
    html_parts.append("        <li><strong>Voice & Pronunciation</strong>: Verify natural Vietnamese cadence, character name pronunciation, and audible emotion cues.</li>")
    html_parts.append("        <li><strong>Framing & Anti-Chat-Box</strong>: Verify camera focus centers on character facial expressions/eyes, avoiding empty speech bubbles.</li>")
    html_parts.append("        <li><strong>Audio/Video Sync</strong>: Verify smooth Ken Burns camera pacing and legible burned subtitles.</li>")
    html_parts.append("      </ul>")
    html_parts.append("    </div>")

    # Contact Sheets
    if data["contact_sheets"]:
        html_parts.append("    <h2>Visual Contact Sheets (4x4 Grids)</h2>")
        html_parts.append("    <div class=\"gallery\">")
        for cs in data["contact_sheets"]:
            cs_fn = cs["filename"] if isinstance(cs, dict) else str(cs)
            cs_rel = cs.get("relative_path", f"contact_sheets/{cs_fn}") if isinstance(cs, dict) else f"contact_sheets/{cs_fn}"
            html_parts.append(f"      <div class=\"gallery-item\"><a href=\"{cs_rel}\" target=\"_blank\"><img src=\"{cs_rel}\" alt=\"{html.escape(cs_fn)}\" loading=\"lazy\"></a><p>{html.escape(cs_fn)}</p></div>")
        html_parts.append("    </div>")

    # Alignment Table
    html_parts.append("    <h2>Script-to-Panel Alignment Table (N:M Mapping)</h2>")
    html_parts.append("    <table>")
    html_parts.append("      <thead><tr><th>Seq ID</th><th>Panels</th><th>Character / Voice</th><th>Motion</th><th>Duration</th><th>Narration & Dialogue</th><th>Status</th></tr></thead>")
    html_parts.append("      <tbody>")
    for seg in data["segments"]:
        s_id = html.escape(str(seg.get("seq_id", "")))
        p_ids = html.escape(", ".join(seg.get("panel_ids", [])))
        char = html.escape(str(seg.get("character") or "Narrator"))
        voice = html.escape(str(seg.get("voice_id", "")))
        char_voice = f"{char} ({voice})" if voice else char
        motion = html.escape(str(seg.get("camera_motion", "static")))
        dur = f"{seg.get('duration')}s" if seg.get("duration") else "pending"
        text = html.escape(str(seg.get("narrator_text", "")))
        if "[CAN_XAC_MINH" in text:
            text = f"<span class=\"flag-warning\">{text}</span>"
        status = html.escape(str(seg.get("status", "pending")))
        html_parts.append(f"        <tr><td><strong>{s_id}</strong></td><td><code>{p_ids}</code></td><td>{char_voice}</td><td><code>{motion}</code></td><td>{dur}</td><td>{text}</td><td><code>{status}</code></td></tr>")
    html_parts.append("      </tbody>")
    html_parts.append("    </table>")

    # Omitted Panels Table
    html_parts.append("    <h2>Omitted Panels Log (Non-Essential Verification)</h2>")
    if data["omitted_panels"]:
        html_parts.append("    <table>")
        html_parts.append("      <thead><tr><th>Panel ID</th><th>Dimensions</th><th>Aspect Ratio</th><th>Status</th><th>Verification Note</th></tr></thead>")
        html_parts.append("      <tbody>")
        for op in data["omitted_panels"]:
            pid = html.escape(str(op["panel_id"]))
            dim = html.escape(f"{op.get('width', '?')}x{op.get('height', '?')}")
            ar = f"{op.get('aspect_ratio', '?'):.2f}" if isinstance(op.get('aspect_ratio'), (int, float)) else "?"
            st = html.escape(str(op.get("manifest_status", "")))
            note = html.escape(str(op.get("note", "")))
            html_parts.append(f"        <tr><td><code>{pid}</code></td><td>{dim}</td><td>{ar}</td><td><code>{st}</code></td><td>{note}</td></tr>")
        html_parts.append("      </tbody>")
        html_parts.append("    </table>")
    else:
        html_parts.append("    <p><em>All panels in manifest are referenced in the script.</em></p>")

    html_parts.append("  </div>")
    html_parts.append("</body>")
    html_parts.append("</html>")
    return "\n".join(html_parts)


def handle_review_pack(args: argparse.Namespace) -> Dict[str, Any]:
    """CLI handler generating the review pack files and returning summary data."""
    project_slug = getattr(args, "project", "")
    if not project_slug:
        raise CliValidationError("Project slug is required.")

    chapter_num = getattr(args, "chapter", None)
    if chapter_num is None:
        raise CliValidationError("Chapter number is required for generating a review pack.")

    # 1. Resolve Project Directory
    project_dir = get_project_dir(project_slug)
    if not project_dir.exists():
        fallback_dir = get_project_data_dir(project_slug)
        if fallback_dir.exists():
            project_dir = fallback_dir
        else:
            # Check tests/fixtures fallback (e.g. synthetic fixture)
            fixture_fallback = Path("tests/fixtures") / project_slug
            if fixture_fallback.exists():
                project_dir = fixture_fallback
            elif project_slug in ("synthetic", "synthetic_fixture") and Path("tests/fixtures/synthetic").exists():
                project_dir = Path("tests/fixtures/synthetic")
            else:
                raise StorageFileNotFoundError(f"Project '{project_slug}' directory not found at {project_dir}")

    # 2. Resolve Chapter Directory
    chap_dir = get_chapter_dir(project_slug, str(chapter_num))
    if not chap_dir.exists():
        fallback_chap = project_dir / "chapters" / str(chapter_num)
        if fallback_chap.exists():
            chap_dir = fallback_chap
        else:
            raise StorageFileNotFoundError(f"Chapter '{chapter_num}' directory not found at {chap_dir}")

    # 3. Determine output directory
    out_dir_arg = getattr(args, "out_dir", None)
    out_dir = Path(out_dir_arg) if out_dir_arg else chap_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    pack_format = getattr(args, "format", "all")

    # 4. Extract data
    data = build_review_pack_data(project_dir, chap_dir, project_slug, str(chapter_num))

    if str(chapter_num) == "1":
        from src.core.human_review import require_preview_approval
        try:
            require_preview_approval(project_dir)
            data["human_review_status"] = "approved"
        except Exception:
            data["human_review_status"] = "unreviewed"

    generated_files: Dict[str, str] = {}

    if str(chapter_num) == "1" and (chap_dir / "video" / "preview.mp4").is_file():
        from src.core.human_review import review_fingerprint
        from src.render.artifact_validation import fingerprint
        data["review_binding"] = {
            "artifact_path": "video/preview.mp4",
            "config_fingerprint": review_fingerprint(project_dir),
            "artifact_hash": fingerprint(chap_dir / "video" / "preview.mp4"),
        }

    # 5. Write Markdown
    if pack_format in {"md", "all"}:
        md_content = generate_markdown_review_pack(data, chap_dir)
        md_path = out_dir / "review_pack.md"
        md_path.write_text(md_content, encoding="utf-8")
        generated_files["markdown"] = str(md_path)

    # 6. Write HTML
    if pack_format in {"html", "all"}:
        html_content = generate_html_review_pack(data, chap_dir)
        html_path = out_dir / "review_pack.html"
        html_path.write_text(html_content, encoding="utf-8")
        generated_files["html"] = str(html_path)

    # 7. Write JSON
    if pack_format in {"json", "all"}:
        json_path = out_dir / "review_pack.json"
        atomic_write_json(json_path, data)
        generated_files["json"] = str(json_path)

    return {
        "project_slug": project_slug,
        "chapter_number": str(chapter_num),
        "technical_status": data["technical_status"],
        "human_review_status": data["human_review_status"],
        "summary": data["summary"],
        "generated_files": generated_files,
    }


def main(argv: Optional[List[str]] = None) -> int:
    """Standalone CLI entry point for review_pack."""
    start_time = time.monotonic()
    raw_args = list(argv) if argv is not None else sys.argv[1:]
    json_mode = "--json" in raw_args

    parser = argparse.ArgumentParser(
        prog="manga-recap review-pack",
        description="Generate lightweight static human review packs in Markdown, HTML, and JSON.",
    )
    parser.add_argument("-p", "--project", required=True, help="Project slug")
    parser.add_argument("-c", "--chapter", required=True, help="Chapter number")
    parser.add_argument(
        "--format",
        choices=["md", "html", "json", "all"],
        default="all",
        help="Output formats to generate (default: all)",
    )
    parser.add_argument("--out-dir", default=None, help="Custom output directory")
    parser.add_argument("--json", action="store_true", help="Output JSON envelope")

    try:
        args = parser.parse_args(raw_args)
        data = handle_review_pack(args)
        duration = round(time.monotonic() - start_time, 4)
        print_response(
            command="review-pack",
            exit_code=ExitCode.SUCCESS,
            data=data,
            duration=duration,
            json_mode=json_mode,
        )
        return ExitCode.SUCCESS.value
    except CliValidationError as e:
        duration = round(time.monotonic() - start_time, 4)
        print_response(
            command="review-pack",
            exit_code=ExitCode.VALIDATION_ERROR,
            error=str(e),
            duration=duration,
            json_mode=json_mode,
        )
        return ExitCode.VALIDATION_ERROR.value
    except StorageFileNotFoundError as e:
        duration = round(time.monotonic() - start_time, 4)
        print_response(
            command="review-pack",
            exit_code=ExitCode.IO_ERROR,
            error=str(e),
            duration=duration,
            json_mode=json_mode,
        )
        return ExitCode.IO_ERROR.value
    except Exception as e:
        duration = round(time.monotonic() - start_time, 4)
        print_response(
            command="review-pack",
            exit_code=ExitCode.GENERAL_ERROR,
            error=f"Unexpected error: {e}",
            duration=duration,
            json_mode=json_mode,
        )
        return ExitCode.GENERAL_ERROR.value


if __name__ == "__main__":
    sys.exit(main())
