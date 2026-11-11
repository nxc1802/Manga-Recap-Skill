"""L2 workflow checks with isolated test fixtures, never real-series/live evidence.

The PCM signal below exercises muxing/validation only; it is not neural TTS and
these tests must never be cited as G4/G6/G7 real chapter or human verification.
"""
import argparse
import copy
import json
import shutil
import wave
from pathlib import Path
import numpy as np
import pytest
from PIL import Image
from unit.test_series_setup import _make_valid_answers_dict
from src.core.series_setup import record_series_setup
from src.core.storage import (init_project_layout, init_chapter_layout, atomic_write_json,
                              read_json, calculate_sha256, load_chapter_metadata)
from src.core.schemas import PanelItem, PanelManifest, RecapScript, ScriptSegment
from src.core.human_review import (approve_preview, require_preview_approval, review_fingerprint)
from src.cli.exit_codes import CliValidationError
from src.render.chapter_renderer import render_chapter
from src.render.artifact_validation import fingerprint


@pytest.fixture
def unit_project(tmp_path, monkeypatch):
    monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path))
    init_project_layout("unit", "Isolated unit fixture")
    chapter = init_chapter_layout("unit", "1")
    # init layout helpers return metadata; use canonical storage path explicitly.
    root = tmp_path / "projects" / "unit"
    chapter = root / "chapters" / "1"
    answers = _make_valid_answers_dict()
    for key, value in dict(Q02="local", Q05=False, Q07=False, Q08={"no_upload": True}, Q09="preview", Q10=False).items():
        answers[key]["user_answer"] = value
    setup = record_series_setup("unit", answers, project_dir=root)
    atomic_write_json(root / "voice_selection.json", dict(project_slug="unit", primary_voice="Ngọc Huyền", reviewer="unit fixture", status="approved", speed=1.0))
    path = chapter / "panels" / "unit.png"
    Image.fromarray(np.tile(np.arange(200, dtype=np.uint8), (150, 1))).convert("RGB").save(path)
    panel = PanelItem(panel_id="p1", sequence_number=1, filename=path.name, relative_path="panels/unit.png", source_image="unit.png",
        width=200, height=150, aspect_ratio=200/150, crop_box=(0,0,200,150), sha256=calculate_sha256(path))
    atomic_write_json(chapter / "manifest.json", PanelManifest(project_slug="unit", chapter_number="1", panels=[panel]))
    audio = chapter / "audio" / "clips" / "unit_signal.wav"
    with wave.open(str(audio), "wb") as stream:
        stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(48000)
        stream.writeframes((np.sin(np.arange(24000)/10)*2000).astype("<i2").tobytes())
    script = RecapScript(project_slug="unit", chapter_number="1", segments=[ScriptSegment(seq_id="a", evidence_panel_ids=["p1"],
        narrator_text="Unit signal fixture", duration=0.5, audio_path="audio/clips/unit_signal.wav", checksum=calculate_sha256(audio))])
    atomic_write_json(chapter / "script.json", script)
    return root, chapter


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required for L2 fixture")
def test_reference_renderer_validates_then_human_gate(unit_project):
    root, chapter = unit_project
    with pytest.raises(CliValidationError, match="Gate G6"):
        render_chapter(root, "1", quality="final", approved=True, skip_preview=True)
    result = render_chapter(root, "1", quality="preview")
    assert result["validation"]["valid"]
    assert read_json(chapter / "chapter.json")["human_review_status"] == "unreviewed"
    config_hash = review_fingerprint(root)
    video_hash = fingerprint(result["video_path"])
    approval = approve_preview(root, "unit fixture", config_hash, video_hash)
    assert require_preview_approval(root) == approval
    final = render_chapter(root, "1", quality="final")
    assert fingerprint(final["video_path"]) == video_hash
    # Any source/config/media change invalidates approval; optional old flags do not help.
    script = read_json(chapter / "script.json")
    script["segments"][0]["narrator_text"] = "changed"
    atomic_write_json(chapter / "script.json", script)
    with pytest.raises(CliValidationError, match="stale"):
        require_preview_approval(root)


@pytest.mark.parametrize("damage", ["voice", "audio", "panel", "script"])
def test_invalid_inputs_cannot_complete(unit_project, damage):
    root, chapter = unit_project
    if damage == "voice": (root / "voice_selection.json").unlink()
    elif damage == "audio": (chapter / "audio" / "clips" / "unit_signal.wav").write_bytes(b"RIFFfake")
    elif damage == "panel": (chapter / "panels" / "unit.png").unlink()
    else:
        data = read_json(chapter / "script.json"); data["segments"][0]["status"] = "needs_review"
        atomic_write_json(chapter / "script.json", data)
    with pytest.raises(Exception):
        render_chapter(root, "1", quality="preview")
    assert read_json(chapter / "chapter.json")["overall_status"] != "completed"


def test_unsigned_old_approval_flags_fail_closed(unit_project):
    root, _ = unit_project
    atomic_write_json(root / "preview_approval.json", dict(status="approved", reviewer="unit fixture"))
    with pytest.raises(CliValidationError): require_preview_approval(root)


def test_legacy_completed_status_is_not_artifact_evidence(unit_project):
    from src.cli.status_cmd import handle_status
    root, chapter = unit_project
    metadata = read_json(chapter / "chapter.json")
    metadata["overall_status"] = "completed"
    atomic_write_json(chapter / "chapter.json", metadata)
    result = handle_status(argparse.Namespace(project="unit", chapter="1"))
    assert result["is_complete"] is False
    assert result["artifact_error"]


def test_tts_mock_flag_rejected_before_synthesis(unit_project):
    from src.cli.tts_cmd import handle_tts
    root, chapter = unit_project
    with pytest.raises(CliValidationError, match="real neural"):
        handle_tts(argparse.Namespace(project="unit", chapter="1", engine="mock"))
    assert read_json(chapter / "chapter.json")["overall_status"] != "completed"
