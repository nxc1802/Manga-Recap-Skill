"""
src/compat/migrate.py
Explicit load-boundary normalization and idempotent migration for legacy V3/V4 artifacts:
- Legacy stage names (crawl/segment -> ingest, script -> agent, tts -> audio, finalize -> export)
- Legacy dict / script.csv / script.txt -> canonical RecapScript
- Legacy Q06 setup records and Q01-Q10 vs named settings normalization
- Legacy voice aliases and token path resolution
"""

from __future__ import annotations

import csv
import json
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.core.errors import DomainValidationError
from src.core.schemas import (
    ChapterMetadata,
    PanelManifest,
    ProjectConfig,
    RecapScript,
    STAGE_ALIAS_MAP,
    StageCheckpoint,
    StageType,
    VoiceSelectionApproval,
)
from src.core.storage import atomic_write_json, read_json
from src.core.version import PRODUCTION_SCHEMA_VERSION, SETUP_SCHEMA_VERSION
from src.tts.catalog import resolve_voice_id


def normalize_stage_type(stage: str | StageType) -> StageType:
    """Maps legacy stage names (crawl/segment/script/tts/finalize) to canonical 5 stages."""
    st = StageType(stage) if isinstance(stage, str) else stage
    return STAGE_ALIAS_MAP.get(st, st)


def normalize_chapter_data(raw_data: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
    """
    Non-destructively normalizes legacy chapter.json keys (status -> technical/overall_status,
    human_review_status default) without discarding unknown user keys.
    Returns (normalized_dict, changed).
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

    # Ensure canonical stages reflect legacy stage completions without overwriting canonical entries.
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


def parse_legacy_script_csv(csv_path: Path, project_slug: str, chapter_number: str, title: str = "") -> RecapScript:
    """Converts a legacy memory/script.csv chapter slice into a validated RecapScript."""
    if not csv_path.is_file():
        raise DomainValidationError(f"Legacy script.csv not found: {csv_path}")

    rows: Dict[str, Dict[str, Any]] = {}
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for r in reader:
            if str(r.get("chapter", "")).strip() != str(chapter_number).strip():
                continue
            seq = str(r.get("sequence_id", "")).strip()
            if not seq:
                continue
            rows[seq] = {
                "text": str(r.get("text", "")),
                "focus_x": float(r.get("focus_x") or 50),
                "focus_y": float(r.get("focus_y") or 50),
                "pan_direction": str(r.get("pan_direction") or "static"),
                "crop_scale": float(r.get("crop_scale") or 100),
            }

    if not rows:
        raise DomainValidationError(f"No rows for chapter {chapter_number} found in {csv_path}")

    def _sort_key(item: Tuple[str, Dict[str, Any]]) -> Tuple[int, float | str]:
        seq = item[0]
        try:
            return (0, float(seq.replace("_", ".")))
        except ValueError:
            m = re.match(r"^(\d+)", seq)
            return (0, float(m.group(1))) if m else (1, seq)

    ordered = dict(sorted(rows.items(), key=_sort_key))
    script = RecapScript.from_legacy_dict(
        project_slug=project_slug,
        chapter_number=str(chapter_number),
        title=title or f"Chapter {chapter_number}",
        legacy_data=ordered,
    )
    script.metadata = {**script.metadata, "migrated_from": str(csv_path.name)}
    return script


def normalize_legacy_script_data(
    raw_data: Dict[str, Any],
    *,
    project_slug: str = "",
    chapter_number: str = "1",
    title: str = "Untitled Chapter",
) -> Tuple[RecapScript, bool]:
    """
    Converts either canonical RecapScript dict or legacy dict mapping seq_id -> text/dict
    into a validated RecapScript. Returns (script, was_migrated).
    """
    if isinstance(raw_data, dict) and "segments" in raw_data:
        data = dict(raw_data)
        data.pop("project_title", None)
        if not data.get("project_slug") and project_slug:
            data["project_slug"] = project_slug
        # Preserve unknown top-level keys inside metadata instead of discarding them.
        known_fields = set(RecapScript.model_fields)
        unknown = {k: v for k, v in data.items() if k not in known_fields}
        if unknown:
            for k in unknown:
                data.pop(k, None)
            meta = dict(data.get("metadata") or {})
            meta.setdefault("preserved_legacy_fields", {}).update(unknown)
            data["metadata"] = meta
        # Ensure segments preserve canonical voice IDs when legacy aliases appear.
        segments = []
        migrated = bool(unknown)
        for seg in data.get("segments", []):
            if isinstance(seg, dict):
                s = dict(seg)
                if "text" in s and "narrator_text" not in s:
                    s["narrator_text"] = s.pop("text")
                    migrated = True
                if s.get("voice_id"):
                    resolved = resolve_voice_id(s["voice_id"])
                    if resolved != s["voice_id"]:
                        s["voice_id"] = resolved
                        migrated = True
                segments.append(s)
            else:
                segments.append(seg)
        data["segments"] = segments
        return RecapScript.model_validate(data), migrated

    if isinstance(raw_data, dict):
        legacy_slice = raw_data.get("scripts", raw_data)
        return (
            RecapScript.from_legacy_dict(project_slug, str(chapter_number), title, legacy_slice),
            True,
        )

    raise DomainValidationError("Invalid script JSON structure")


def migrate_project_artifacts(
    project_dir: Path,
    *,
    dry_run: bool = True,
    backup: bool = True,
) -> Dict[str, Any]:
    """
    Validates and optionally migrates legacy project/chapter artifacts in place.
    - Non-destructive when dry_run=True.
    - Validates models BEFORE writing; creates .bak files when backup=True.
    - Idempotent: re-running reports zero pending updates.
    """
    root = Path(project_dir).resolve()
    if not root.is_dir():
        raise DomainValidationError(f"Project directory not found: {root}")

    planned: List[Dict[str, Any]] = []
    applied: List[str] = []

    def _write_if_needed(path: Path, payload: Any, reason: str) -> None:
        planned.append({"path": str(path.relative_to(root)), "reason": reason})
        if dry_run:
            return
        if backup and path.is_file():
            bak = path.with_name(f"{path.name}.bak")
            if not bak.exists():
                shutil.copy2(path, bak)
        atomic_write_json(path, payload)
        applied.append(str(path.relative_to(root)))

    # 1. project.json validation & voice normalization
    proj_path = root / "project.json"
    if not proj_path.is_file():
        raise DomainValidationError(f"Missing project.json in {root}")
    raw_proj = read_json(proj_path)
    proj_cfg = ProjectConfig.model_validate(raw_proj)
    resolved_voice = resolve_voice_id(proj_cfg.audio_settings.default_voice)
    if resolved_voice != proj_cfg.audio_settings.default_voice:
        proj_cfg.audio_settings.default_voice = resolved_voice
        _write_if_needed(proj_path, proj_cfg, "normalize legacy default_voice alias")

    # 2. legacy memory/character_dictionary.json -> characters.json
    legacy_chars = root / "memory" / "character_dictionary.json"
    canon_chars = root / "characters.json"
    if legacy_chars.is_file() and not canon_chars.is_file():
        raw_chars = read_json(legacy_chars)
        payload = (
            raw_chars
            if isinstance(raw_chars, dict) and "characters" in raw_chars
            else {"project_slug": proj_cfg.slug, "characters": raw_chars}
        )
        _write_if_needed(canon_chars, payload, "migrate memory/character_dictionary.json -> characters.json")

    # 3. voice_selection.json alias normalization
    voice_path = root / "voice_selection.json"
    if voice_path.is_file():
        raw_voice = read_json(voice_path)
        vs = VoiceSelectionApproval.model_validate(raw_voice)
        norm_voice = resolve_voice_id(vs.primary_voice)
        if norm_voice != vs.primary_voice:
            vs.primary_voice = norm_voice
            _write_if_needed(voice_path, vs, "normalize legacy voice alias in voice_selection.json")

    # 4. Chapter metadata & scripts
    chapters_dir = root / "chapters"
    csv_path = root / "memory" / "script.csv"
    if chapters_dir.is_dir():
        for chap_dir in sorted(chapters_dir.iterdir()):
            if not chap_dir.is_dir() or chap_dir.name.startswith("."):
                continue
            c_json = chap_dir / "chapter.json"
            if c_json.is_file():
                norm_ch, changed = normalize_chapter_data(read_json(c_json))
                meta = ChapterMetadata.model_validate(norm_ch)
                if changed:
                    _write_if_needed(c_json, meta, "normalize chapter.json status/stages")

            s_json = chap_dir / "script.json"
            if s_json.is_file():
                raw_s = read_json(s_json)
                script_obj, migrated = normalize_legacy_script_data(
                    raw_s, project_slug=proj_cfg.slug, chapter_number=chap_dir.name
                )
                if migrated:
                    _write_if_needed(s_json, script_obj, "normalize legacy script.json")
            elif csv_path.is_file():
                try:
                    script_obj = parse_legacy_script_csv(csv_path, proj_cfg.slug, chap_dir.name)
                    _write_if_needed(s_json, script_obj, "migrate memory/script.csv -> script.json")
                except DomainValidationError:
                    pass

            m_json = chap_dir / "manifest.json"
            if m_json.is_file():
                PanelManifest.model_validate(read_json(m_json))

    return {
        "project_slug": proj_cfg.slug,
        "dry_run": dry_run,
        "backup": backup,
        "planned_count": len(planned),
        "planned_updates": planned,
        "applied_count": len(applied),
        "applied_files": applied,
    }
