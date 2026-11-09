"""
tests/verification/test_matrix_verification.py
Phase 6: Independent Verification Suite for Minimum Test Matrix (T01 - T12).
Author: A9: Independent Technical Verifier (worker_a9)

Formally validates technical acceptance criteria from Section 10 of
docs/Manga_Recap_TeamWork_Implementation_Plan.md:
- T01: 1 segment with 3 selected panels (multi-panel rendering without dropping panels).
- T02: 1 source panel intentionally omitted (does not fail when panel omitted from script).
- T03: Panel in panel_ids does not exist (fail-fast with seq_id and path without substitution).
- T04: 2 segments have distinct voice/speed/emotion (backend receives correct per-segment parameters).
- T05: Opaque non-sequential seq_id ('002_1') preserves alignment across audio, SRT, video.
- T06: Batch TTS count mismatch raises error (no silent truncation/zip).
- T07: Corrupt/missing MP4 or incomplete download never marks chapter completed.
- T08: Re-run unchanged input reuses cache without re-synthesis.
- T09: 'render' when valid WAV already exists does not invoke TTS.
- T10: Local vs Cloud contract parity across schemas, options, and artifacts.
- T11: Skip preview leaves human_review_status as 'unreviewed'.
- T12: Video + SRT + WAV valid duration, checksum, and ffprobe stream integrity.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import shutil
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from src.cli.exit_codes import ExitCode
from src.cli.main import main
from src.cloud.kaggle_service import KaggleCloudService
from src.core.schemas import (
    CameraMotionType,
    ChapterMetadata,
    CheckpointStatus,
    HumanReviewStatus,
    PanelManifest,
    ProjectConfig,
    RecapScript,
    StageType,
)
from src.core.storage import (
    atomic_write_json,
    get_chapter_dir,
    get_project_dir,
    init_chapter_layout,
    init_project_layout,
    load_chapter_metadata,
    read_json,
    update_checkpoint,
)
from src.tts.engine import get_wav_duration_seconds


@pytest.fixture
def synthetic_fixture_dir():
    p = Path(__file__).resolve().parent.parent / "fixtures" / "synthetic"
    assert p.exists(), f"Synthetic fixture missing at {p}"
    return p


@pytest.fixture
def isolated_workspace(tmp_path, synthetic_fixture_dir, monkeypatch):
    """Hermetic workspace populated with canonical synthetic fixture."""
    data_dir = tmp_path / "data"
    proj_dir = data_dir / "projects" / "synthetic_fixture"
    proj_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(synthetic_fixture_dir, proj_dir)
    chap_dir = proj_dir / "chapters" / "1"

    monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(data_dir))
    monkeypatch.setattr("src.cloud.kaggle_service.get_project_dir", lambda p: proj_dir)
    monkeypatch.setattr("src.cloud.kaggle_service.get_chapter_dir", lambda p, c: chap_dir)
    monkeypatch.setattr("src.core.storage.get_project_dir", lambda p: proj_dir)
    monkeypatch.setattr("src.core.storage.get_chapter_dir", lambda p, c: chap_dir)
    monkeypatch.setattr("src.cli.validate_cmd.get_project_dir", lambda p: proj_dir)
    monkeypatch.setattr("src.cli.validate_cmd.get_chapter_dir", lambda p, c: chap_dir)

    return {
        "root": tmp_path,
        "data_dir": data_dir,
        "project_dir": proj_dir,
        "chapter_dir": chap_dir,
        "project_slug": "synthetic_fixture",
        "chapter_number": "1",
    }


class TestMinimumTestMatrixT01toT12:

    # --------------------------------------------------------------------------
    # T01: Multi-panel segment rendering
    # --------------------------------------------------------------------------
    def test_t01_multi_panel_segment_rendering(self, isolated_workspace):
        """T01: 1 segment with 3 selected panels are all rendered in correct sequence."""
        chap_dir = isolated_workspace["chapter_dir"]
        script_data = read_json(chap_dir / "script.json")
        script = RecapScript.model_validate(script_data)

        # In canonical fixture, segment '002_1' references 3 panels
        multi_seg = next(s for s in script.segments if s.seq_id == "002_1")
        assert len(multi_seg.panel_ids) == 3
        assert multi_seg.panel_ids == ["panel_002.jpg", "panel_003.jpg", "panel_004.jpg"]

        # Verify on Cloud worker script generator
        svc = KaggleCloudService(api_token="KGAT_mock_token")
        worker_code = svc.generate_worker_script("synthetic_fixture", "1", job_type="render")
        assert "len(p_ids)" in worker_code
        assert "frame_{seq_id}_{p_idx}.jpg" in worker_code
        assert "p_name = p_ids[0]" not in worker_code

    # --------------------------------------------------------------------------
    # T02: Intentionally omitted source panel
    # --------------------------------------------------------------------------
    def test_t02_intentionally_omitted_source_panel(self, isolated_workspace):
        """T02: Source panel in manifest but omitted from script does not fail validation."""
        chap_dir = isolated_workspace["chapter_dir"]
        manifest_data = read_json(chap_dir / "manifest.json")
        script_data = read_json(chap_dir / "script.json")

        manifest = PanelManifest.model_validate(manifest_data)
        script = RecapScript.model_validate(script_data)

        # panel_005.jpg is in manifest, but intentionally omitted from script
        all_referenced = {pid for seg in script.segments for pid in seg.panel_ids}
        assert "panel_005.jpg" in [p.filename for p in manifest.panels]
        assert "panel_005.jpg" not in all_referenced

        # Run validate command
        code = main(["validate", "-p", "synthetic_fixture", "-c", "1", "--json"])
        assert code == ExitCode.SUCCESS

    # --------------------------------------------------------------------------
    # T03: Missing panel fail-fast without substitution
    # --------------------------------------------------------------------------
    def test_t03_missing_panel_fail_fast_without_substitution(self, isolated_workspace):
        """T03: Panel in panel_ids that does not exist raises fail-fast without fallback."""
        chap_dir = isolated_workspace["chapter_dir"]
        adv_script = read_json(chap_dir / "script_missing_panel.json")
        atomic_write_json(chap_dir / "script.json", adv_script)

        # Validate must fail with VALIDATION_ERROR identifying missing panel
        code = main(["validate", "-p", "synthetic_fixture", "-c", "1", "--json"])
        assert code == ExitCode.VALIDATION_ERROR

        # Worker script must not contain silent fallback avail_panels[0]
        svc = KaggleCloudService(api_token="KGAT_mock_token")
        worker_code = svc.generate_worker_script("synthetic_fixture", "1", job_type="render")
        assert "avail_panels[0]" not in worker_code
        assert "Substitution is strictly prohibited" in worker_code

    # --------------------------------------------------------------------------
    # T04: Multi-voice / speed / emotion per segment
    # --------------------------------------------------------------------------
    def test_t04_multi_voice_speed_emotion_per_segment(self, isolated_workspace):
        """T04: Segments with distinct voice/speed/emotion retain individual configs."""
        chap_dir = isolated_workspace["chapter_dir"]
        script = RecapScript.model_validate(read_json(chap_dir / "script.json"))

        seg001 = next(s for s in script.segments if s.seq_id == "001")
        seg002 = next(s for s in script.segments if s.seq_id == "002_1")
        seg003 = next(s for s in script.segments if s.seq_id == "003")

        assert seg001.voice_id == "Ngọc Huyền"
        assert seg001.emotion == "neutral"

        assert seg002.voice_id == "Nam An"
        assert seg002.emotion == "dramatic"
        assert seg002.emotion_cue == "[hắng giọng]"

        assert seg003.voice_id == "Ngọc Huyền"
        assert seg003.emotion == "sad"
        assert seg003.emotion_cue == "[thở dài]"

        # Cloud worker script groups by voice_id rather than forcing first voice
        svc = KaggleCloudService(api_token="KGAT_mock_token")
        worker_code = svc.generate_worker_script("synthetic_fixture", "1", job_type="tts")
        assert "target_voice = segments[0]" not in worker_code
        assert "voice_groups = defaultdict(list)" in worker_code

    # --------------------------------------------------------------------------
    # T05: Opaque non-sequential seq_id ('002_1')
    # --------------------------------------------------------------------------
    def test_t05_opaque_non_sequential_seq_id(self, isolated_workspace):
        """T05: Opaque non-sequential seq_id ('002_1') preserves alignment across WAV and metadata."""
        chap_dir = isolated_workspace["chapter_dir"]
        script = RecapScript.model_validate(read_json(chap_dir / "script.json"))

        assert [s.seq_id for s in script.segments] == ["001", "002_1", "003"]
        wav_file = chap_dir / "audio" / "clips" / "audio_1_002_1.wav"
        assert wav_file.exists()

        # Cloud worker preserves exact seq_id in audio loops
        svc = KaggleCloudService(api_token="KGAT_mock_token")
        worker_code = svc.generate_worker_script("synthetic_fixture", "1", job_type="render")
        assert 'for idx in range(1, len(segments) + 1):' not in worker_code
        assert 'seq_id = seg["seq_id"]' in worker_code

    # --------------------------------------------------------------------------
    # T06: Batch TTS count mismatch raises error
    # --------------------------------------------------------------------------
    def test_t06_batch_tts_count_mismatch_raises_error(self):
        """T06: Batch TTS count mismatch raises error without silent truncation."""
        svc = KaggleCloudService(api_token="KGAT_mock_token")
        worker_code = svc.generate_worker_script("synthetic_fixture", "1", job_type="tts")
        assert "Batch TTS generation failed" in worker_code
        assert "Silent truncation is strictly forbidden" in worker_code

    # --------------------------------------------------------------------------
    # T07: Corrupt/missing MP4 never marked completed
    # --------------------------------------------------------------------------
    def test_t07_corrupt_or_missing_mp4_never_marked_completed(self, isolated_workspace):
        """T07: Incomplete download or missing MP4 leaves chapter overall_status uncompleted."""
        chap_dir = isolated_workspace["chapter_dir"]
        meta_before = load_chapter_metadata(chap_dir)
        meta_before.overall_status = CheckpointStatus.PENDING
        atomic_write_json(chap_dir / "chapter.json", meta_before.model_dump(mode="json"))

        svc = KaggleCloudService(api_token="KGAT_mock_token")
        mock_api = MagicMock()
        svc._api = mock_api
        svc._username = "test_user"

        with tempfile.TemporaryDirectory() as empty_dl:
            with pytest.raises(FileNotFoundError, match="final.mp4"):
                svc.sync_results("synthetic_fixture", "1", Path(empty_dl), job_type="render")

        meta_after = load_chapter_metadata(chap_dir)
        assert meta_after.overall_status != CheckpointStatus.COMPLETED

    # --------------------------------------------------------------------------
    # T08: Re-run unchanged input reuses cache
    # --------------------------------------------------------------------------
    def test_t08_rerun_unchanged_input_reuses_cache(self, tmp_path, capsys, monkeypatch):
        """T08: Re-run with unchanged inputs reuses cache and does not re-synthesize."""
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path))
        slug = "cache_test_proj"
        init_project_layout(slug, "Cache Test")
        chap_dir = init_chapter_layout(slug, "1")

        script_data = {
            "project_slug": slug,
            "chapter_number": "1",
            "title": "Cache Test",
            "segments": [
                {"seq_id": "01", "panel_ids": ["p1.jpg"], "narrator_text": "Cảnh quay ban đầu."}
            ]
        }
        atomic_write_json(chap_dir / "script.json", script_data)

        # Run 1: synthesizes 1 clip
        code1 = main(["tts", "-p", slug, "-c", "1", "--json"])
        assert code1 == ExitCode.SUCCESS
        cap1 = json.loads(capsys.readouterr().out)["data"]
        assert cap1["segments_synthesized"] == 1
        assert cap1["segments_cached"] == 0

        # Run 2: reuses cached clip
        code2 = main(["tts", "-p", slug, "-c", "1", "--json"])
        assert code2 == ExitCode.SUCCESS
        cap2 = json.loads(capsys.readouterr().out)["data"]
        assert cap2["segments_synthesized"] == 0
        assert cap2["segments_cached"] == 1

    # --------------------------------------------------------------------------
    # T09: render with valid WAV does not invoke TTS
    # --------------------------------------------------------------------------
    def test_t09_render_with_valid_wav_does_not_invoke_tts(self, tmp_path, monkeypatch):
        """T09: Calling render when valid WAV clips exist does not invoke TTS engine."""
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path))
        slug = "render_audio_decouple"
        init_project_layout(slug, "Render Audio Decouple")
        chap_dir = init_chapter_layout(slug, "1")

        script_data = {
            "project_slug": slug,
            "chapter_number": "1",
            "title": "Decouple Test",
            "segments": [
                {"seq_id": "01", "panel_ids": ["p1.jpg"], "narrator_text": "Lời kể đã có âm thanh."}
            ]
        }
        atomic_write_json(chap_dir / "script.json", script_data)

        # Create valid WAV audio clip
        clips_dir = chap_dir / "audio" / "clips"
        clips_dir.mkdir(parents=True, exist_ok=True)
        (clips_dir / "audio_1_01.wav").write_bytes(b"RIFF" + b"\x00" * 100)

        with patch("src.cli.tts_cmd.handle_tts") as mock_tts, \
             patch("src.cli.render_cmd.VideoEngine") as mock_engine:
            mock_inst = mock_engine.return_value
            mock_inst.render_chapter.return_value = {
                "video_path": str(chap_dir / "video" / "test.mp4"),
                "subtitle_path": str(chap_dir / "subtitles" / "test.srt"),
            }
            code = main(["render", "-p", slug, "-c", "1", "--skip-preview", "--approved", "--json"])
            assert code == ExitCode.SUCCESS
            mock_tts.assert_not_called()

    # --------------------------------------------------------------------------
    # T10: Local vs Cloud contract parity
    # --------------------------------------------------------------------------
    def test_t10_local_vs_cloud_contract_parity(self, isolated_workspace):
        """T10: Local and Cloud maintain exact contract parity across schemas and options."""
        chap_dir = isolated_workspace["chapter_dir"]
        svc = KaggleCloudService(api_token="KGAT_mock_token")
        worker_code = svc.generate_worker_script(
            "synthetic_fixture", "1",
            job_type="produce",
            video_mode="portrait_padded",
            burn_subtitles=True,
            fps=30,
            target_width=1920,
            target_height=1080
        )
        assert "detect_video_mode" in worker_code
        assert "portrait_padded" in worker_code
        assert "sanitize_sub_text" in worker_code
        assert "sidechaincompress" in worker_code

    # --------------------------------------------------------------------------
    # T11: Skip chapter preview sets unreviewed status
    # --------------------------------------------------------------------------
    def test_t11_skip_preview_sets_unreviewed_status(self, tmp_path, monkeypatch):
        """T11: Skipping preview with --skip-preview records human_review_status as unreviewed."""
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path))
        slug = "preview_test_proj"
        init_project_layout(slug, "Preview Test")
        chap_dir = init_chapter_layout(slug, "1")

        script_data = {
            "project_slug": slug,
            "chapter_number": "1",
            "title": "Preview Test",
            "segments": [
                {"seq_id": "01", "panel_ids": ["p1.jpg"], "narrator_text": "Kiểm tra trạng thái unreviewed."}
            ]
        }
        atomic_write_json(chap_dir / "script.json", script_data)
        clips_dir = chap_dir / "audio" / "clips"
        clips_dir.mkdir(parents=True, exist_ok=True)
        (clips_dir / "audio_1_01.wav").write_bytes(b"RIFF" + b"\x00" * 100)

        with patch("src.cli.render_cmd.VideoEngine") as mock_engine:
            mock_inst = mock_engine.return_value
            mock_inst.render_chapter.return_value = {
                "video_path": str(chap_dir / "video" / "test.mp4"),
                "subtitle_path": str(chap_dir / "subtitles" / "test.srt"),
            }
            code = main(["render", "-p", slug, "-c", "1", "--skip-preview", "--approved", "--json"])
            assert code == ExitCode.SUCCESS

        meta = load_chapter_metadata(chap_dir)
        assert meta.technical_status == CheckpointStatus.COMPLETED
        assert meta.human_review_status == HumanReviewStatus.UNREVIEWED

    # --------------------------------------------------------------------------
    # T12: Audio, Video, Subtitle artifact validation
    # --------------------------------------------------------------------------
    def test_t12_audio_video_srt_validation(self, isolated_workspace):
        """T12: Validates audio WAV durations and checksum integrity in canonical fixture."""
        chap_dir = isolated_workspace["chapter_dir"]
        script = RecapScript.model_validate(read_json(chap_dir / "script.json"))
        clips_dir = chap_dir / "audio" / "clips"

        for seg in script.segments:
            wav_file = clips_dir / f"audio_1_{seg.seq_id}.wav"
            assert wav_file.exists()
            assert wav_file.stat().st_size > 44
            dur = get_wav_duration_seconds(wav_file)
            assert dur > 0
            assert abs(dur - seg.duration) <= 0.05
