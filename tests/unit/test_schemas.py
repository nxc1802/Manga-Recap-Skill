"""
tests/unit/test_schemas.py
Comprehensive unit test suite for src/core/schemas.py.
"""

import pytest
from pydantic import ValidationError

from src.core.schemas import (
    CameraMotionType,
    ChapterMetadata,
    CheckpointStatus,
    CharacterDictionary,
    CharacterGender,
    CharacterProfile,
    CharacterRole,
    FocusBox,
    PanelItem,
    PanelManifest,
    PanelStatus,
    ProjectConfig,
    RecapScript,
    RenderSettings,
    ScriptSegment,
    StageCheckpoint,
    StageType,
    VoiceSelectionApproval,
    VoiceStrategy,
)


class TestFocusBox:
    def test_valid_box(self):
        box = FocusBox(x_min=0.1, y_min=0.2, x_max=0.8, y_max=0.9)
        assert box.x_min == 0.1
        assert box.focus_x == 45.0
        assert box.focus_y == 55.0
        assert box.crop_scale == 70.0

    def test_invalid_range_raises(self):
        with pytest.raises(ValidationError):
            FocusBox(x_min=-0.1, y_min=0.0, x_max=1.0, y_max=1.0)
        with pytest.raises(ValidationError):
            FocusBox(x_min=0.0, y_min=0.0, x_max=1.5, y_max=1.0)

    def test_invalid_order_raises(self):
        with pytest.raises(ValidationError, match="x_min .* must be <= x_max"):
            FocusBox(x_min=0.8, y_min=0.1, x_max=0.2, y_max=0.9)

    def test_from_center_and_scale(self):
        box = FocusBox.from_center_and_scale(focus_x=50.0, focus_y=50.0, crop_scale=50.0)
        assert box.x_min == 0.25
        assert box.x_max == 0.75
        assert box.y_min == 0.25
        assert box.y_max == 0.75


class TestProjectConfig:
    def test_project_config_defaults(self):
        cfg = ProjectConfig(
            project_id="proj_01",
            slug="warrior_grandpa",
            title="Warrior Grandpa"
        )
        assert cfg.language == "vi"
        assert cfg.render_settings.target_width == 1280
        assert cfg.audio_settings.sample_rate == 48000
        assert cfg.schema_version == "3.0.0"

    def test_project_serialization_roundtrip(self):
        cfg = ProjectConfig(
            project_id="proj_01",
            slug="test_slug",
            title="Test Title",
            chapters=["1", "2"]
        )
        json_dict = cfg.model_dump(mode="json")
        loaded = ProjectConfig.model_validate(json_dict)
        assert loaded == cfg


class TestChapterMetadata:
    def test_chapter_metadata_stages_initialized(self):
        meta = ChapterMetadata(
            chapter_id="c_01",
            chapter_number="1",
            project_slug="test_slug",
            title="Chapter 1"
        )
        assert len(meta.stages) == len(StageType)
        for stage in StageType:
            assert stage in meta.stages
            assert meta.stages[stage].status == CheckpointStatus.PENDING

    def test_stats_defaults(self):
        meta = ChapterMetadata(
            chapter_id="c_01",
            chapter_number="1",
            project_slug="test_slug",
            title="Chapter 1"
        )
        assert meta.stats.total_panels == 0
        assert meta.stats.total_audio_duration_seconds == 0.0


class TestRecapScript:
    def test_script_segment_validation(self):
        seg = ScriptSegment(
            seq_id="001",
            panel_ids=["panel_001.jpg"],
            narrator_text="Trong đêm tối...",
            camera_motion=CameraMotionType.PAN_LEFT,
            duration=3.45
        )
        assert seg.seq_id == "001"
        assert seg.camera_motion == CameraMotionType.PAN_LEFT

    def test_legacy_dict_conversion(self):
        legacy = {
            "1": {
                "text": "Lời kể một",
                "focus_x": 40,
                "focus_y": 60,
                "pan_direction": "horizontal",
                "crop_scale": 80
            },
            "2": "Lời kể hai"
        }
        script = RecapScript.from_legacy_dict("test_slug", "1", "Ep 1", legacy)
        assert len(script.segments) == 2
        assert script.segments[0].seq_id == "1"
        assert script.segments[0].camera_motion == CameraMotionType.PAN_LEFT
        assert script.segments[1].seq_id == "2"


class TestCharacterDictionary:
    def test_character_profile_creation(self):
        cd = CharacterDictionary(
            project_slug="test_slug",
            characters={
                "Ghislain": CharacterProfile(
                    canonical_name="Ghislain",
                    aliases=["Ghislain Perdium", "Tiểu tử Perdium"],
                    role=CharacterRole.PROTAGONIST,
                    gender=CharacterGender.MALE,
                    core_features="Tóc đen, mắt sắc bén",
                    assigned_voice="Adam bựa"
                )
            }
        )
        assert "Ghislain" in cd.characters
        assert cd.characters["Ghislain"].role == CharacterRole.PROTAGONIST


class TestPanelManifest:
    def test_panel_item_and_manifest(self):
        manifest = PanelManifest(
            project_slug="test_slug",
            chapter_number="1",
            total_raw_images=1,
            total_panels=1,
            panels=[
                PanelItem(
                    panel_id="panel_001",
                    sequence_number=1,
                    filename="panel_001.jpg",
                    relative_path="panels/panel_001.jpg",
                    source_image="001.jpg",
                    crop_box=(0, 100, 800, 500),
                    width=800,
                    height=400,
                    aspect_ratio=2.0,
                    sha256="abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890",
                    status=PanelStatus.ACTIVE
                )
            ]
        )
        assert manifest.total_panels == 1
        assert manifest.panels[0].aspect_ratio == 2.0


class TestVoiceSelectionApproval:
    def test_direct_valid_approval(self):
        approval = VoiceSelectionApproval(
            project_slug="test_manga",
            voice_strategy="single_narrator",
            primary_voice="Trúc Ly",
            speed=1.1,
            reviewer="Lead Reviewer",
            status="approved",
            notes="Clear diction and balanced emotion",
        )
        assert approval.primary_voice == "Trúc Ly"
        assert approval.speed == 1.1
        assert approval.get_reviewer() == "Lead Reviewer"
        assert approval.is_approved() is True

    def test_ui_alias_normalization(self):
        ui_data = {
            "project_slug": "test_manga",
            "approved_strategy": "single_narrator",
            "primary_voice": "Hải Đăng",
            "default_speed": 1.15,
            "approved_by": "Senior Editor",
            "reviewed_at": "2026-11-04T05:00:00Z",
            "decision_notes": "Very punchy tone for action chapters",
            "character_voice_mapping": {"main": "Hải Đăng"},
            "audio_spec": {"sample_rate": 48000, "channels": 1},
        }
        approval = VoiceSelectionApproval.model_validate(ui_data)
        assert approval.voice_strategy == "single_narrator"
        assert approval.primary_voice == "Hải Đăng"
        assert approval.speed == 1.15
        assert approval.reviewer == "Senior Editor"
        assert approval.notes == "Very punchy tone for action chapters"
        assert approval.is_approved() is True

    def test_rejection_or_pending_status(self):
        pending = VoiceSelectionApproval(
            primary_voice="Trúc Ly",
            reviewer="Reviewer A",
            status="pending",
        )
        assert pending.is_approved() is False

    def test_missing_required_fields_raises(self):
        with pytest.raises(ValidationError):
            VoiceSelectionApproval.model_validate({"primary_voice": "Trúc Ly"})  # missing reviewer
        with pytest.raises(ValidationError):
            VoiceSelectionApproval.model_validate({"reviewer": "Reviewer B"})  # missing primary_voice

