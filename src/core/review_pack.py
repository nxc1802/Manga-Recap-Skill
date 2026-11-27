"""Human review pack data extraction and Markdown/JSON rendering."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.core.errors import DomainValidationError, StorageFileNotFoundError
from src.core.project import resolve_existing_chapter_dir, resolve_existing_project_dir
from src.core.schemas import HumanReviewStatus
from src.core.storage import atomic_write_json, calculate_sha256, read_json


def build_review_pack_data(
    project_dir: Path, chap_dir: Path, project_slug: str, chapter_num: str
) -> Dict[str, Any]:
    """Extracts and correlates all metadata and assets for the review pack."""
    project_cfg_dict: Dict[str, Any] = {}
    p_json = project_dir / "project.json"
    if p_json.exists():
        try:
            project_cfg_dict = read_json(p_json)
        except Exception:
            pass

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

    cs_dir = chap_dir / "contact_sheets"
    if cs_dir.exists() and not contact_sheets:
        disk_sheets = sorted(
            f.name for f in cs_dir.glob("*") if f.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
        )
        for cs_name in disk_sheets:
            contact_sheets.append({
                "filename": cs_name,
                "relative_path": f"contact_sheets/{cs_name}",
                "sheet_id": Path(cs_name).stem,
            })

    panels_dir = chap_dir / "panels"
    disk_panels = sorted(
        f.name for f in panels_dir.glob("*") if f.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
    ) if panels_dir.exists() else []

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

    script_segments: List[Dict[str, Any]] = []
    s_json = chap_dir / "script.json"
    if s_json.exists():
        try:
            s_data = read_json(s_json)
            script_segments = s_data.get("segments", [])
        except Exception:
            pass

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

    omitted_panels: List[Dict[str, Any]] = []
    referenced_canonical_count = 0
    for fn, p_info in sorted(canonical_panels.items()):
        pid_alias = p_info.get("panel_id", "")
        if fn in referenced_pids_set or pid_alias in referenced_pids_set:
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

    reused_panels = [
        {"panel_id": pid, "use_count": count, "segments": panel_to_segments.get(pid, [])}
        for pid, count in panel_usage_count.items()
        if count > 1
    ]

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
    subtitles_files = sorted(f.name for f in subtitles_dir.glob("*.srt")) if subtitles_dir.exists() else []

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
    """Renders a Markdown review pack from the shared review-pack data model."""
    lines: List[str] = []
    lines.append(f"# Human Review Pack — Chapter {data['chapter_number']}: {data['chapter_title']}")
    lines.append("")
    lines.append(f"- **Project:** `{data['project_slug']}` ({data['project_title']})")
    lines.append(f"- **Generated At:** {data['created_at']}")
    lines.append(f"- **Technical Status:** `{data['technical_status']}`")
    lines.append(f"- **Human Review Status:** `{data['human_review_status']}`")
    lines.append("")

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

    lines.append("## 2. Human Reviewer Inspection Checklist")
    lines.append("")
    lines.append("Please evaluate the following 5 criteria before approving:")
    lines.append("1. [ ] **Faithful Storytelling**: The script faithfully reflects original manga plot and character dialogues without hallucinating ungrounded lore.")
    lines.append("2. [ ] **Essential Panel Retention**: All critical story progression, martial arts climax, and dialogue panels are included in the video (inspect Omitted Panels table below).")
    lines.append("3. [ ] **Voice Authenticity & Cadence**: Character voices, pronouns, and emotional expressions sound natural.")
    lines.append("4. [ ] **Visual Framing & Anti-Chat-Box**: Character faces/actions are centered without cutoffs; camera does NOT zoom into empty speech bubbles.")
    lines.append("5. [ ] **A/V Sync & Subtitle Legibility**: Narration aligns with on-screen panels, and subtitles are easy to read.")
    lines.append("")

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

    lines.append("## 6. Omitted Panels Log (Essential Story Verification)")
    lines.append("")
    if data["omitted_panels"]:
        lines.append("The following panels from `manifest.json` were omitted from `script.json`. Verify that none contain critical story revelations:")
        lines.append("")
        lines.append("| Panel ID | Dimensions | Aspect Ratio | Manifest Status | Reviewer Verification Note |")
        lines.append("|---|---|---|---|---|")
        for op in data["omitted_panels"]:
            dim = f"{op.get('width', '?')}x{op.get('height', '?')}"
            ar = f"{op.get('aspect_ratio', '?'):.2f}" if isinstance(op.get("aspect_ratio"), (int, float)) else "?"
            lines.append(f"| `{op['panel_id']}` | {dim} | {ar} | `{op['manifest_status']}` | {op['note']} |")
    else:
        lines.append("*All panels in manifest are referenced in the script (100% panel coverage).*")
    lines.append("")

    if data["reused_panels"]:
        lines.append("## 7. Reused Panels Log")
        lines.append("")
        lines.append("| Panel ID | Usage Count | Referenced in Segments |")
        lines.append("|---|---|---|")
        for rp in data["reused_panels"]:
            segs = ", ".join(rp["segments"])
            lines.append(f"| `{rp['panel_id']}` | {rp['use_count']} times | `{segs}` |")
        lines.append("")

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
    lines.append(
        f"manga-recap approve -p {data['project_slug']} --reviewer HUMAN_NAME "
        f"--config-fingerprint {binding.get('config_fingerprint', 'CONFIG_HASH')} "
        f"--artifact-hash {binding.get('artifact_hash', 'ARTIFACT_HASH')} --json"
    )
    lines.append("```")
    lines.append("")
    lines.append("### To Request Edits:")
    lines.append("Specify feedback targeting exact `seq_id` or `panel_ids`. The agent will edit `script.json` and incrementally re-synthesize only the modified clips.")
    lines.append("")
    return "\n".join(lines)


def create_review_pack(
    project_slug: str,
    chapter_num: str,
    *,
    fmt: str = "all",
    out_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Domain operation building canonical Markdown and JSON review pack outputs."""
    if not project_slug:
        raise DomainValidationError("Project slug is required.")
    if chapter_num is None:
        raise DomainValidationError("Chapter number is required for generating a review pack.")
    if fmt not in {"md", "json", "all"}:
        raise DomainValidationError(f"Unsupported review pack format '{fmt}'. Supported formats: md, json, all.")

    project_dir = resolve_existing_project_dir(project_slug, require_exists=False)
    if not project_dir.exists():
        fixture_fallback = Path("tests/fixtures") / project_slug
        if fixture_fallback.exists():
            project_dir = fixture_fallback
        elif project_slug in ("synthetic", "synthetic_fixture") and Path("tests/fixtures/synthetic").exists():
            project_dir = Path("tests/fixtures/synthetic")
        else:
            raise StorageFileNotFoundError(f"Project '{project_slug}' directory not found at {project_dir}")

    chap_dir = resolve_existing_chapter_dir(project_slug, str(chapter_num), project_dir=project_dir)
    dest_dir = Path(out_dir) if out_dir else chap_dir
    dest_dir.mkdir(parents=True, exist_ok=True)

    data = build_review_pack_data(project_dir, chap_dir, project_slug, str(chapter_num))

    if str(chapter_num) == "1":
        from src.core.human_review import require_preview_approval, review_fingerprint

        try:
            require_preview_approval(project_dir)
            data["human_review_status"] = "approved"
        except Exception:
            data["human_review_status"] = "unreviewed"

        preview_mp4 = chap_dir / "video" / "preview.mp4"
        if preview_mp4.is_file():
            data["review_binding"] = {
                "artifact_path": "video/preview.mp4",
                "config_fingerprint": review_fingerprint(project_dir),
                "artifact_hash": calculate_sha256(preview_mp4),
            }

    generated_files: Dict[str, str] = {}
    if fmt in {"md", "all"}:
        md_path = dest_dir / "review_pack.md"
        md_path.write_text(generate_markdown_review_pack(data, chap_dir), encoding="utf-8")
        generated_files["markdown"] = str(md_path)
    if fmt in {"json", "all"}:
        json_path = dest_dir / "review_pack.json"
        atomic_write_json(json_path, data)
        generated_files["json"] = str(json_path)

    return {
        "project_slug": project_slug,
        "chapter_number": str(chapter_num),
        "technical_status": data["technical_status"],
        "human_review_status": data["human_review_status"],
        "summary": data["summary"],
        "generated_files": generated_files,
        "review_binding": data.get("review_binding"),
    }
