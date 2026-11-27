#!/usr/bin/env python3
"""
Standalone explicit migration utility for legacy Manga Recap V3/V4/V5 artifacts.

Usage:
  python scripts/migrate_legacy_project.py --project-dir data/projects/<slug>
  python scripts/migrate_legacy_project.py --project-dir data/projects/<slug> --apply --backup

Handles:
- Legacy chapter.json stage names (crawl/segment/script/tts/finalize -> ingest/agent/audio/render/export)
- Legacy memory/script.csv & legacy dict JSON scripts -> canonical script.json
- Legacy memory/character_dictionary.json -> characters.json
- Legacy V4 series_setup_answers.json (strips Q06/Q07 -> schema_version 5.0.0)
- Legacy 9-preset panel crop metadata -> clears obsolete crop keys & marks panels needs_review
- Legacy voice preset aliases (edge-tts-*) -> VieNeu voice IDs
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.errors import DomainValidationError  # noqa: E402
from src.core.schemas import (  # noqa: E402
    CameraMotionType,
    ChapterMetadata,
    FocusBox,
    PanelManifest,
    PanelStatus,
    ProjectConfig,
    RecapScript,
    STAGE_ALIAS_MAP,
    ScriptSegment,
    StageCheckpoint,
    StageType,
    VoiceSelectionApproval,
)
from src.core.series_setup import ALL_QUESTION_IDS, QUESTION_METADATA  # noqa: E402
from src.core.storage import atomic_write_json, read_json  # noqa: E402
from src.core.version import PRODUCTION_SCHEMA_VERSION, SETUP_SCHEMA_VERSION  # noqa: E402
from src.tts.catalog import resolve_voice_id  # noqa: E402


def normalize_stage_type(stage: str | StageType) -> StageType:
    """Maps legacy stage names (crawl/segment/script/tts/finalize) to canonical 5 stages."""
    st = StageType(stage) if isinstance(stage, str) else stage
    return STAGE_ALIAS_MAP.get(st, st)


def normalize_chapter_data(raw_data: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
    """
    Non-destructively normalizes legacy chapter.json keys without discarding unknown user keys.
    Returns `(normalized_dict, changed)`.
    """
    if not isinstance(raw_data, dict):
        raise DomainValidationError("chapter.json must be a JSON object")
    data = dict(raw_data)
    changed = False

    if "overall_status" not in data and "technical_status" in data:
        data["overall_status"] = data["technical_status"]
        changed = True
    elif "technical_status" not in data and "overall_status" in data:
        data["technical_status"] = data["overall_status"]
        changed = True
    elif "overall_status" not in data and "status" in data:
        legacy_map = {
            "pending": "pending",
            "processing": "running",
            "crawled": "pending",
            "scripted": "pending",
            "completed": "completed",
            "failed": "failed",
            "needs_review": "needs_review",
        }
        mapped = legacy_map.get(str(data["status"]).strip().lower(), "pending")
        data["overall_status"] = mapped
        data["technical_status"] = mapped
        changed = True

    if "human_review_status" not in data:
        data["human_review_status"] = "unreviewed"
        changed = True

    stages = dict(data.get("stages") or {})
    for old_stage, canon_stage in STAGE_ALIAS_MAP.items():
        old_cp = stages.get(old_stage.value)
        if not isinstance(old_cp, dict):
            continue
        canon_cp = stages.get(canon_stage.value)
        if not isinstance(canon_cp, dict) or canon_cp.get("status") == "pending":
            if old_cp.get("status") != "pending":
                cloned = dict(old_cp)
                cloned["stage"] = canon_stage.value
                stages[canon_stage.value] = cloned
                changed = True
    if stages != data.get("stages"):
        data["stages"] = stages

    return data, changed


def legacy_script_dict_to_recap_script(
    project_slug: str,
    chapter_number: str,
    title: str,
    legacy_data: Dict[str, Any],
) -> RecapScript:
    """Explicit offline converter from legacy dict/CSV script format to canonical RecapScript."""
    segments: List[ScriptSegment] = []
    for seq_str, content in legacy_data.items():
        if isinstance(content, str):
            segments.append(
                ScriptSegment(
                    seq_id=str(seq_str),
                    panel_ids=[f"panel_{seq_str}.jpg"],
                    narrator_text=content,
                    camera_motion=CameraMotionType.STATIC,
                )
            )
        elif isinstance(content, dict):
            fx = float(content.get("focus_x", 50))
            fy = float(content.get("focus_y", 50))
            cs = float(content.get("crop_scale", 100))
            box = FocusBox.from_center_and_scale(focus_x=fx, focus_y=fy, crop_scale=cs)
            motion_str = str(content.get("pan_direction", "static")).lower()
            motion = (
                CameraMotionType.SLOW_PAN_DOWN
                if motion_str in {"vertical", "slow_pan_down"}
                else CameraMotionType.STATIC
            )
            segments.append(
                ScriptSegment(
                    seq_id=str(seq_str),
                    panel_ids=content.get("panel_ids", [f"panel_{seq_str}.jpg"]),
                    narrator_text=content.get("text", ""),
                    character=content.get("character"),
                    emotion=content.get("emotion", "neutral"),
                    camera_motion=motion,
                    focus_box=box,
                    status=content.get("status", "pending"),
                )
            )
    return RecapScript(
        project_slug=project_slug,
        chapter_number=str(chapter_number),
        title=title or f"Chapter {chapter_number}",
        segments=segments,
    )


def parse_legacy_script_csv(
    csv_path: Path,
    project_slug: str,
    chapter_number: str,
    title: str = "",
) -> RecapScript:
    """Converts a legacy memory/script.csv chapter slice into a validated RecapScript."""
    if not csv_path.is_file():
        raise DomainValidationError(f"Legacy script.csv not found: {csv_path}")

    rows_by_seq: Dict[str, Dict[str, Any]] = {}
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for idx, row in enumerate(reader, start=1):
            row_chap = str(row.get("chapter") or row.get("chapter_number") or chapter_number).strip()
            if row_chap != str(chapter_number).strip():
                continue
            seq_raw = str(row.get("sequence_id") or row.get("seq_id") or f"{idx:03d}").strip()
            text = (row.get("text") or row.get("narrator_text") or "").strip()
            if not text:
                continue
            rows_by_seq[seq_raw] = {
                "text": text,
                "focus_x": float(row.get("focus_x") or 50),
                "focus_y": float(row.get("focus_y") or 50),
                "pan_direction": (row.get("pan_direction") or "static").strip(),
                "crop_scale": float(row.get("crop_scale") or 100),
            }

    if not rows_by_seq:
        raise DomainValidationError(
            f"No script rows found for chapter {chapter_number} in {csv_path}"
        )

    ordered = {
        k: rows_by_seq[k]
        for k in sorted(rows_by_seq.keys(), key=lambda x: [int(p) if p.isdigit() else 0 for p in re.findall(r"\d+|\D+", x)])
    }
    return legacy_script_dict_to_recap_script(
        project_slug=project_slug,
        chapter_number=str(chapter_number),
        title=title or f"Chapter {chapter_number}",
        legacy_data=ordered,
    )


def normalize_legacy_script_data(
    raw_data: Dict[str, Any],
    *,
    project_slug: str = "",
    chapter_number: str = "1",
    title: str = "",
) -> Tuple[RecapScript, bool]:
    """Converts legacy V3 dict scripts or upgrades segment voice aliases cleanly."""
    if not isinstance(raw_data, dict):
        raise DomainValidationError("script.json must be a JSON object")

    if "segments" in raw_data and isinstance(raw_data["segments"], list):
        data = dict(raw_data)
        known_root = set(RecapScript.model_fields.keys())
        preserved_root = {k: data[k] for k in list(data.keys()) if k not in known_root}
        changed = False
        if preserved_root:
            for k in preserved_root:
                data.pop(k, None)
            meta = dict(data.get("metadata") or {})
            meta["preserved_legacy_fields"] = preserved_root
            data["metadata"] = meta
            changed = True

        known_seg = set(ScriptSegment.model_fields.keys()) | {"text"}
        cleaned_segs: List[Dict[str, Any]] = []
        for idx, seg in enumerate(data["segments"], start=1):
            if not isinstance(seg, dict):
                raise DomainValidationError(f"Segment {idx} must be an object")
            s = dict(seg)
            if "narrator_text" not in s and "text" in s:
                s["narrator_text"] = s.pop("text")
                changed = True
            if s.get("voice_id"):
                resolved = resolve_voice_id(s["voice_id"])
                if resolved != s["voice_id"]:
                    s["voice_id"] = resolved
                    changed = True
            extra_seg = {k: v for k, v in list(s.items()) if k not in known_seg}
            for k in extra_seg:
                s.pop(k, None)
                changed = True
            cleaned_segs.append(s)
        data["segments"] = cleaned_segs
        data.setdefault("schema_version", PRODUCTION_SCHEMA_VERSION)
        data.setdefault("voice_strategy", "single_narrator")
        if data["voice_strategy"] != "single_narrator":
            data["voice_strategy"] = "single_narrator"
            changed = True
        return RecapScript.model_validate(data), changed

    legacy_slice = raw_data.get("scripts") if isinstance(raw_data.get("scripts"), dict) else raw_data
    return legacy_script_dict_to_recap_script(
        project_slug, str(chapter_number), title, legacy_slice
    ), True


def _migrate_setup_v4_to_v5(setup_data: Dict[str, Any]) -> Tuple[Dict[str, Any], bool, Optional[str]]:
    """Upgrades V4 series_setup_answers.json (Q01-Q10) to canonical V5 (Q01-Q05, Q08-Q10)."""
    if setup_data.get("schema_version") == SETUP_SCHEMA_VERSION and set(setup_data.get("answers", {})) == set(ALL_QUESTION_IDS):
        return setup_data, False, None

    data = dict(setup_data)
    answers = dict(data.get("answers") or {})
    legacy_q06 = answers.pop("Q06", None)
    answers.pop("Q07", None)
    for qid in ALL_QUESTION_IDS:
        if qid in answers and isinstance(answers[qid], dict):
            answers[qid] = {
                "question_id": qid,
                "question_title": QUESTION_METADATA[qid]["title"],
                "user_answer": answers[qid].get("user_answer"),
                "answer_source": answers[qid].get("answer_source", "user"),
                "confirmed_at": answers[qid].get("confirmed_at", data.get("updated_at")),
            }
    data["schema_version"] = SETUP_SCHEMA_VERSION
    data["answers"] = answers
    pan_mode = None
    if isinstance(legacy_q06, dict):
        raw_m = str(legacy_q06.get("user_answer", "")).strip().casefold()
        pan_mode = "static" if raw_m == "static" else "animation"
    return data, True, pan_mode


def migrate_project_artifacts(
    project_dir: Path,
    *,
    dry_run: bool = True,
    backup: bool = True,
) -> Dict[str, Any]:
    """
    Explicitly migrates a legacy V3/V4/V5 project directory cleanly.
    - Non-destructive when `dry_run=True`.
    - Creates `.bak` copies before overwriting existing files when `backup=True`.
    """
    root = Path(project_dir)
    if not root.exists():
        raise DomainValidationError(f"Project directory does not exist: {root}")

    planned: List[Dict[str, Any]] = []
    applied: List[Dict[str, Any]] = []

    def _record_write(path: Path, payload: Any, description: str) -> None:
        planned.append({"path": str(path), "action": description})
        if dry_run:
            return
        if backup and path.exists():
            bak = path.with_suffix(path.suffix + ".bak")
            if not bak.exists():
                shutil.copy2(path, bak)
        atomic_write_json(path, payload)
        applied.append({"path": str(path), "action": description})

    # 0. Setup upgrade (V4 Q06/Q07 -> V5 8-question contract)
    setup_path = root / "series_setup_answers.json"
    migrated_pan_mode: Optional[str] = None
    if setup_path.is_file():
        raw_setup = read_json(setup_path)
        norm_setup, setup_changed, migrated_pan_mode = _migrate_setup_v4_to_v5(raw_setup)
        if setup_changed:
            _record_write(setup_path, norm_setup, "upgrade_setup_schema_to_5.0.0")

    # 1. project.json normalization
    proj_path = root / "project.json"
    if not proj_path.is_file():
        raise DomainValidationError(f"Missing project.json in {root}")
    raw_proj = read_json(proj_path)
    proj_changed = False
    if raw_proj.get("schema_version") != SETUP_SCHEMA_VERSION and setup_path.is_file():
        raw_proj["schema_version"] = SETUP_SCHEMA_VERSION
        proj_changed = True
    if migrated_pan_mode and isinstance(raw_proj.get("render_settings"), dict):
        if raw_proj["render_settings"].get("render_pan_mode") != migrated_pan_mode:
            raw_proj["render_settings"]["render_pan_mode"] = migrated_pan_mode
            proj_changed = True
    if isinstance(raw_proj.get("render_settings"), dict):
        rs = raw_proj["render_settings"]
        if rs.get("video_mode", "landscape_fit") != "landscape_fit":
            rs["video_mode"] = "landscape_fit"
            proj_changed = True
        sub = rs.get("subtitle")
        if isinstance(sub, dict) and sub.get("chunking") == "fixed-chars":
            sub["chunking"] = "sentence-aware"
            proj_changed = True
    if isinstance(raw_proj.get("audio_settings"), dict):
        old_v = raw_proj["audio_settings"].get("default_voice")
        if old_v:
            canon_v = resolve_voice_id(old_v)
            if canon_v != old_v:
                raw_proj["audio_settings"]["default_voice"] = canon_v
                proj_changed = True
    proj_cfg = ProjectConfig.model_validate(raw_proj)
    if proj_changed:
        _record_write(proj_path, proj_cfg, "normalize_project_config")

    # 2. voice_selection.json normalization
    vs_path = root / "voice_selection.json"
    if vs_path.is_file():
        raw_vs = read_json(vs_path)
        vs_changed = False
        if raw_vs.get("primary_voice"):
            canon_v = resolve_voice_id(raw_vs["primary_voice"])
            if canon_v != raw_vs["primary_voice"]:
                raw_vs["primary_voice"] = canon_v
                vs_changed = True
        if raw_vs.get("voice_strategy", "single_narrator") != "single_narrator":
            raw_vs["voice_strategy"] = "single_narrator"
            vs_changed = True
        VoiceSelectionApproval.model_validate(raw_vs)
        if vs_changed:
            _record_write(vs_path, raw_vs, "normalize_voice_selection")

    # 3. memory/character_dictionary.json -> characters.json
    mem_char = root / "memory" / "character_dictionary.json"
    canon_char = root / "characters.json"
    if mem_char.is_file() and not canon_char.is_file():
        raw_chars = read_json(mem_char)
        _record_write(
            canon_char,
            {"schema_version": "3.0.0", "project_slug": proj_cfg.slug, "characters": raw_chars},
            "migrate_legacy_character_dictionary",
        )

    # 4. Chapters migration (chapter.json, manifest legacy_9preset cleanup, script.csv/script.json)
    chapters_dir = root / "chapters"
    if chapters_dir.is_dir():
        for chap_dir in sorted(d for d in chapters_dir.iterdir() if d.is_dir()):
            c_json = chap_dir / "chapter.json"
            if c_json.is_file():
                norm_ch, changed = normalize_chapter_data(read_json(c_json))
                ChapterMetadata.model_validate(norm_ch)
                if changed:
                    _record_write(c_json, norm_ch, f"normalize_chapter_{chap_dir.name}")

            m_json = chap_dir / "manifest.json"
            if m_json.is_file():
                manifest = PanelManifest.model_validate(read_json(m_json))
                m_changed = False
                for p in manifest.panels:
                    meta = p.evidence_metadata or {}
                    is_legacy_crop = (
                        meta.get("crop_method") == "legacy_9preset"
                        or meta.get("decision_policy") in ("legacy_9preset", "9preset_safe")
                        or ("center_crop_preset" in meta and meta.get("crop_method") != "4sided_pct")
                    )
                    if is_legacy_crop:
                        for k in ("center_crop_preset", "display_crop", "crop_method", "decision_policy", "crop_policy_version"):
                            meta.pop(k, None)
                        meta["crop_safe"] = False
                        meta["decision_valid"] = False
                        meta["review_reason"] = "legacy_9preset_migrated_redecide_required"
                        p.evidence_metadata = meta
                        p.status = PanelStatus.NEEDS_REVIEW
                        m_changed = True
                if m_changed:
                    _record_write(m_json, manifest, f"invalidate_legacy_crop_manifest_{chap_dir.name}")

            s_json = chap_dir / "script.json"
            if s_json.is_file():
                script_obj, migrated = normalize_legacy_script_data(
                    read_json(s_json),
                    project_slug=proj_cfg.slug,
                    chapter_number=chap_dir.name,
                )
                if migrated:
                    _record_write(s_json, script_obj, f"normalize_script_{chap_dir.name}")
            else:
                for csv_path in (chap_dir / "script.csv", root / "memory" / "script.csv"):
                    if csv_path.is_file():
                        script_obj = parse_legacy_script_csv(csv_path, proj_cfg.slug, chap_dir.name)
                        _record_write(s_json, script_obj, f"convert_script_csv_chap_{chap_dir.name}")
                        break

    return {
        "project_slug": proj_cfg.slug,
        "project_dir": str(root),
        "dry_run": dry_run,
        "planned_count": len(planned),
        "applied_count": len(applied),
        "planned": planned,
        "applied": applied,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Migrate legacy Manga Recap project artifacts to V7/V5 format.")
    parser.add_argument("--project-dir", required=True, help="Path to data/projects/<slug>")
    parser.add_argument("--apply", action="store_true", help="Write changes atomically (default is dry-run)")
    parser.add_argument("--no-backup", action="store_true", help="Skip creating .bak files when applying")
    args = parser.parse_args(argv)
    res = migrate_project_artifacts(
        Path(args.project_dir),
        dry_run=not args.apply,
        backup=not args.no_backup,
    )
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
