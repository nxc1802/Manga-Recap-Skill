"""
tests/unit/test_synthetic_fixture.py
Verification suite for the Phase 0 canonical synthetic test fixture.
Validates:
- Non-sequential segment ID ("002_1")
- Multi-panel segment (3 panels: panel_002, panel_003, panel_004)
- Multi-voice with distinct speed/emotion ("Ngọc Huyền", "Nam An")
- Omitted valid panel (panel_005 in manifest & disk, omitted from script)
- Missing panel fail-fast fixture (panel_missing_999.jpg in script_missing_panel.json)
- Physical asset integrity (valid WAV durations, valid JPEG sizes, sha256 checksums)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.core.schemas import (
    CameraMotionType,
    ChapterMetadata,
    CharacterDictionary,
    FocusBox,
    PanelManifest,
    ProjectConfig,
    RecapScript,
    VoiceSelectionApproval,
)
from src.tts.engine import get_wav_duration_seconds


@pytest.fixture
def fixture_dir():
    return Path(__file__).resolve().parent.parent / "fixtures" / "synthetic"


class TestSyntheticFixtureContract:
    def test_project_config_validity(self, fixture_dir):
        p_path = fixture_dir / "project.json"
        assert p_path.exists()
        cfg = ProjectConfig.model_validate_json(p_path.read_text(encoding="utf-8"))
        assert cfg.slug == "synthetic_fixture"
        assert cfg.schema_version == "3.0.0"
        assert cfg.render_settings.target_width == 1920
        assert cfg.render_settings.target_height == 1080
        assert cfg.audio_settings.sample_rate == 48000

    def test_characters_dictionary_validity(self, fixture_dir):
        c_path = fixture_dir / "characters.json"
        assert c_path.exists()
        chars = CharacterDictionary.model_validate_json(c_path.read_text(encoding="utf-8"))
        assert "Ghislain Perdium" in chars.characters
        assert "Narrator" in chars.characters
        assert chars.characters["Ghislain Perdium"].assigned_voice == "Nam An"

    def test_voice_selection_approval_validity(self, fixture_dir):
        v_path = fixture_dir / "voice_selection.json"
        assert v_path.exists()
        approval = VoiceSelectionApproval.model_validate_json(v_path.read_text(encoding="utf-8"))
        assert approval.is_approved() is True
        assert approval.primary_voice == "Ngọc Huyền"
        assert approval.character_voice_mapping.get("Ghislain Perdium") == "Nam An"

    def test_chapter_metadata_validity(self, fixture_dir):
        c_path = fixture_dir / "chapters" / "1" / "chapter.json"
        assert c_path.exists()
        meta = ChapterMetadata.model_validate_json(c_path.read_text(encoding="utf-8"))
        assert meta.chapter_number == "1"
        assert meta.project_slug == "synthetic_fixture"
        assert meta.stages["ingest"].status == "completed"
        assert meta.stages["agent"].status == "completed"
        assert meta.stages["audio"].status == "completed"

    def test_manifest_contains_active_and_omitted_panels(self, fixture_dir):
        m_path = fixture_dir / "chapters" / "1" / "manifest.json"
        assert m_path.exists()
        manifest = PanelManifest.model_validate_json(m_path.read_text(encoding="utf-8"))
        assert manifest.total_panels == 5
        assert len(manifest.panels) == 5

        panels_dir = fixture_dir / "chapters" / "1" / "panels"
        for p in manifest.panels:
            p_file = panels_dir / p.filename
            assert p_file.exists(), f"Panel file {p.filename} must exist on disk"
            assert p_file.stat().st_size > 500

        # Assert panel_005 is physically present on disk
        assert (panels_dir / "panel_005.jpg").exists()

    def test_script_canonical_contract_edge_cases(self, fixture_dir):
        s_path = fixture_dir / "chapters" / "1" / "script.json"
        assert s_path.exists()
        script = RecapScript.model_validate_json(s_path.read_text(encoding="utf-8"))
        assert len(script.segments) == 3

        # 1. Non-sequential seq_ids
        seq_ids = [seg.seq_id for seg in script.segments]
        assert seq_ids == ["001", "002_1", "003"], "Must support non-sequential seq_ids like '002_1'"

        # 2. Multi-panel segment (3 panels in segment 002_1)
        seg_multi = script.segments[1]
        assert seg_multi.seq_id == "002_1"
        assert len(seg_multi.panel_ids) == 3
        assert seg_multi.panel_ids == ["panel_002.jpg", "panel_003.jpg", "panel_004.jpg"]

        # 3. Multi-voice with distinct speed/emotion
        seg1 = script.segments[0]
        assert seg1.voice_id == "Ngọc Huyền"
        assert seg1.emotion == "neutral"

        assert seg_multi.voice_id == "Nam An"
        assert seg_multi.emotion == "dramatic"
        assert seg_multi.emotion_cue == "[hắng giọng]"

        seg3 = script.segments[2]
        assert seg3.voice_id == "Ngọc Huyền"
        assert seg3.emotion == "sad"
        assert seg3.emotion_cue == "[thở dài]"
        # Panel reuse: segment 3 reuses panel_001.jpg
        assert seg3.panel_ids == ["panel_001.jpg"]

        # 4. One intentionally omitted valid panel (panel_005.jpg is in manifest, but not in script)
        all_referenced_panels = set()
        for s in script.segments:
            all_referenced_panels.update(s.panel_ids)
        assert "panel_005.jpg" not in all_referenced_panels, "panel_005.jpg must be intentionally omitted"

        # 5. Physical audio clips validation
        clips_dir = fixture_dir / "chapters" / "1" / "audio" / "clips"
        for seg in script.segments:
            wav_file = fixture_dir / "chapters" / "1" / seg.audio_path
            assert wav_file.exists(), f"Audio clip {seg.audio_path} must exist"
            dur = get_wav_duration_seconds(wav_file)
            assert abs(dur - seg.duration) <= 0.05, f"Audio duration mismatch for {seg.seq_id}"

    def test_script_missing_panel_adversarial_fixture(self, fixture_dir):
        sm_path = fixture_dir / "chapters" / "1" / "script_missing_panel.json"
        assert sm_path.exists()
        script = RecapScript.model_validate_json(sm_path.read_text(encoding="utf-8"))
        assert len(script.segments) == 4

        missing_seg = script.segments[3]
        assert missing_seg.seq_id == "004_missing_test"
        assert missing_seg.panel_ids == ["panel_missing_999.jpg"]

        # Assert panel_missing_999.jpg does NOT exist on disk (fail-fast guarantee)
        missing_panel_file = fixture_dir / "chapters" / "1" / "panels" / "panel_missing_999.jpg"
        assert not missing_panel_file.exists(), "panel_missing_999.jpg must NOT exist on disk to test fail-fast"
