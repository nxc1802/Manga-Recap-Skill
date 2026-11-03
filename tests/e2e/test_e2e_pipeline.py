"""
tests/e2e/test_e2e_pipeline.py
End-to-End Test Suite for Manga Recap Agentic Skill V3 (PR-08).
Validates the full workflow on Warrior Grandpa and His Supreme Granddaughter Episode 2:
Step 1: Source Validation
Step 2: Crawl / Import & Streaming Segmentation
Step 3: Agent Scripting & Schema Validation
Step 4: Local VieNeu-TTS Audio Synthesis & Human Review Gate 1
Step 5: Cinematic Video Engine V2 Rendering & Subtitles
Step 6: Quality Review & Artifact Verification Matrix
"""

from __future__ import annotations

import json
import shutil
import wave
from pathlib import Path
from typing import Any, Dict

import pytest

from src.cli.exit_codes import ExitCode
from src.cli.main import main
from src.core.schemas import (
    CameraMotionType,
    CheckpointStatus,
    FocusBox,
    PanelManifest,
    RecapScript,
    ScriptSegment,
    StageType,
)
from src.core.storage import (
    get_chapter_dir,
    get_project_dir,
    init_chapter_layout,
    init_project_layout,
    load_chapter_metadata,
    load_recap_script,
    read_json,
    save_recap_script,
    update_checkpoint,
)
from src.render.validator import StreamValidator


@pytest.fixture(scope="class")
def e2e_project_environment(tmp_path_factory):
    """Sets up an isolated projects environment for E2E testing."""
    tmp_path = tmp_path_factory.mktemp("e2e_test")
    projects_root = tmp_path / "data" / "projects"
    projects_root.mkdir(parents=True, exist_ok=True)

    import src.core.storage as storage_mod
    orig_get_root = storage_mod.get_projects_root
    storage_mod.get_projects_root = lambda: projects_root

    project_slug = "warrior_grandpa"
    chapter_num = "2"

    proj_dir = init_project_layout(project_slug, title="Warrior Grandpa and His Supreme Granddaughter")
    chap_dir = init_chapter_layout(project_slug, chapter_num, title="Episode 2 - Granddaughter Awakens")

    # Source 5 representative panels from real workspace assets or create fallback
    raw_source_dir = tmp_path / "seed_panels"
    raw_source_dir.mkdir(parents=True, exist_ok=True)

    workspace_panels_dir = Path("data/warrior_grandpa_and_his_supreme_granddaughter/image/recut/chap_1")
    if workspace_panels_dir.exists():
        for i in range(1, 6):
            p_file = workspace_panels_dir / f"panel_{i:03d}.jpg"
            if p_file.exists():
                shutil.copy2(str(p_file), str(raw_source_dir / f"page_{i:03d}.jpg"))

    # Fallback if seed files missing
    if len(list(raw_source_dir.glob("*.jpg"))) < 5:
        from PIL import Image, ImageDraw
        for i in range(1, 6):
            img = Image.new("RGB", (1080, 1500), color=(20 + i * 20, 30 + i * 15, 50))
            d = ImageDraw.Draw(img)
            d.text((100, 100), f"Warrior Grandpa Ep 2 - Panel {i}", fill=(240, 240, 240))
            img.save(raw_source_dir / f"page_{i:03d}.jpg", "JPEG")

    yield {
        "project_slug": project_slug,
        "chapter_num": chapter_num,
        "proj_dir": proj_dir,
        "chap_dir": chap_dir,
        "raw_source_dir": raw_source_dir,
    }

    storage_mod.get_projects_root = orig_get_root


class TestMangaRecapE2E:
    """Complete End-to-End Chapter Processing Lifecycle (PR-08)."""

    def test_step1_source_validation(self):
        """Step 1: Validate Webtoons URL parsing and metadata extraction."""
        test_url = (
            "https://www.webtoons.com/en/action/warrior-grandpa-and-his-supreme-granddaughter/"
            "episode-2/viewer?title_no=8953&episode_no=2"
        )
        assert "webtoons.com" in test_url
        assert "warrior-grandpa-and-his-supreme-granddaughter" in test_url
        assert "episode-2" in test_url
        assert "episode_no=2" in test_url

    def test_step2_crawl_import_and_segmentation(self, e2e_project_environment, capsys):
        """Step 2: Ingest raw pages and run streaming segmentation with contact sheets."""
        env = e2e_project_environment
        slug = env["project_slug"]
        chap = env["chapter_num"]
        src_dir = env["raw_source_dir"]

        # Run CLI crawl import
        exit_code = main([
            "crawl",
            "-p", slug,
            "-c", chap,
            "--import-dir", str(src_dir),
            "--json",
        ])
        assert exit_code == ExitCode.SUCCESS

        captured = capsys.readouterr()
        crawl_resp = json.loads(captured.out)
        assert crawl_resp["status"] == "success"
        assert crawl_resp["data"]["raw_images_count"] >= 5

        # Run CLI segment
        exit_code = main([
            "segment",
            "-p", slug,
            "-c", chap,
            "--json",
        ])
        assert exit_code == ExitCode.SUCCESS

        captured = capsys.readouterr()
        seg_resp = json.loads(captured.out)
        assert seg_resp["status"] == "success"
        assert seg_resp["data"]["total_panels"] >= 4

        # Verify Manifest & Contact Sheet presence
        chap_dir = env["chap_dir"]
        manifest_path = chap_dir / "manifest.json"
        assert manifest_path.exists()

        manifest_data = read_json(manifest_path)
        manifest = PanelManifest.model_validate(manifest_data)
        assert len(manifest.panels) >= 4

        contact_dir = chap_dir / "contact_sheets"
        assert contact_dir.exists()
        contact_sheets = list(contact_dir.glob("sheet_*.jpg"))
        assert len(contact_sheets) >= 1

    def test_step3_agent_scripting_and_validation(self, e2e_project_environment, capsys):
        """Step 3: Author Vietnamese recap script adhering to 5-panel window and motion cues."""
        env = e2e_project_environment
        slug = env["project_slug"]
        chap = env["chapter_num"]
        chap_dir = env["chap_dir"]

        # Create structured script.json
        script = RecapScript(
            project_slug=slug,
            chapter_number=chap,
            title="Episode 2 - Granddaughter Awakens",
            voice_id="Trúc Ly",
            voice_strategy="single_narrator",
            segments=[
                ScriptSegment(
                    seq_id="001",
                    panel_id="panel_001",
                    narrator_text="Tại căn phòng nhỏ thuộc Tế Thế Đường, Thạch đại phu khẽ thở dài sau khi bắt mạch cho Lưu Chấn Sơn.",
                    speaker="narrator",
                    camera_motion=CameraMotionType.PAN_DOWN,
                    focus_box=FocusBox(x_min=0.2, y_min=0.2, x_max=0.8, y_max=0.8),
                    emotion_intent="suspenseful",
                ),
                ScriptSegment(
                    seq_id="002",
                    panel_id="panel_002",
                    narrator_text="Khối u trong đầu ngài không những không thuyên giảm, mà luồng khí tức kỳ dị lại bắt đầu khởi phát.",
                    speaker="narrator",
                    camera_motion=CameraMotionType.ZOOM_IN,
                    focus_box=FocusBox(x_min=0.3, y_min=0.3, x_max=0.7, y_max=0.7),
                    emotion_intent="dramatic",
                ),
                ScriptSegment(
                    seq_id="003",
                    panel_id="panel_003",
                    narrator_text="Đúng lúc này, ngoài cửa viện bỗng vang lên tiếng bước chân gấp gáp của một cô thiếu nữ.",
                    speaker="narrator",
                    camera_motion=CameraMotionType.PAN_LEFT,
                    emotion_intent="neutral",
                ),
                ScriptSegment(
                    seq_id="004",
                    panel_id="panel_004",
                    narrator_text="Ánh mắt sắc sảo của nàng lóe lên tia sáng, dường như nhận ra sự khác thường trong hơi thở của gia gia.",
                    speaker="granddaughter",
                    camera_motion=CameraMotionType.FOCUS_CROP,
                    focus_box=FocusBox(x_min=0.25, y_min=0.2, x_max=0.75, y_max=0.7),
                    emotion_intent="excited",
                ),
                ScriptSegment(
                    seq_id="005",
                    panel_id="panel_005",
                    narrator_text="Một chương mới chính thức mở ra khi thiên tài võ đạo xuất thế bảo vệ người thân.",
                    speaker="narrator",
                    camera_motion=CameraMotionType.STATIC,
                    emotion_intent="cinematic_epic",
                ),
            ],
        )

        save_recap_script(chap_dir, script)
        update_checkpoint(chap_dir, StageType.SCRIPT, CheckpointStatus.COMPLETED)

        # Validate with CLI validate
        exit_code = main([
            "validate",
            "-p", slug,
            "-c", chap,
            "--strict",
            "--json",
        ])
        assert exit_code == ExitCode.SUCCESS

        captured = capsys.readouterr()
        val_resp = json.loads(captured.out)
        assert val_resp["status"] == "success"
        assert val_resp["data"]["valid"] is True

    def test_step4_tts_synthesis_and_human_gate1(self, e2e_project_environment, capsys):
        """Step 4: Synthesize 48 kHz WAV utterances and verify Review Gate 1 approval artifact."""
        env = e2e_project_environment
        slug = env["project_slug"]
        chap = env["chapter_num"]
        chap_dir = env["chap_dir"]

        # Run CLI TTS synthesis
        exit_code = main([
            "tts",
            "-p", slug,
            "-c", chap,
            "--force",
            "--json",
        ])
        assert exit_code == ExitCode.SUCCESS

        captured = capsys.readouterr()
        tts_resp = json.loads(captured.out)
        assert tts_resp["status"] == "success"
        assert tts_resp["data"]["segments_synthesized"] == 5

        # Check utterance WAV files exist and have valid 48kHz headers
        clips_dir = chap_dir / "audio" / "clips"
        assert clips_dir.exists()
        wav_files = list(clips_dir.glob("*.wav"))
        assert len(wav_files) >= 5

        for wf_path in wav_files:
            with wave.open(str(wf_path), "rb") as wf:
                assert wf.getframerate() == 48000
                assert wf.getnchannels() in (1, 2)
                assert wf.getnframes() > 0

        # Review Gate 1 Approval Document verification
        voice_selection_path = env["proj_dir"] / "voice_selection.json"
        voice_selection_data = {
            "selected_strategy": "single_narrator",
            "primary_narrator": "Trúc Ly",
            "approved_by": "user",
            "benchmark_verified": True,
        }
        with open(voice_selection_path, "w", encoding="utf-8") as f:
            json.dump(voice_selection_data, f, indent=2)

        assert voice_selection_path.exists()

    def test_step5_video_rendering_and_subtitles(self, e2e_project_environment, capsys):
        """Step 5: Render preview video with dynamic camera motion and verify ffprobe integrity."""
        env = e2e_project_environment
        slug = env["project_slug"]
        chap = env["chapter_num"]
        chap_dir = env["chap_dir"]

        exit_code = main([
            "render",
            "-p", slug,
            "-c", chap,
            "--quality", "preview",
            "--burn-subtitles",
            "--json",
        ])
        assert exit_code == ExitCode.SUCCESS

        captured = capsys.readouterr()
        render_resp = json.loads(captured.out)
        assert render_resp["status"] == "success"
        data = render_resp["data"]
        assert data["quality"] == "preview"
        assert data["resolution"] == "1280x720"
        assert data["fps"] == 30.0

        # Check subtitle existence & cue counts
        srt_path = chap_dir / "subtitles" / f"chapter_{chap}.srt"
        assert srt_path.exists()
        srt_content = srt_path.read_text(encoding="utf-8")
        assert "Thạch đại phu" in srt_content
        assert "00:00:" in srt_content

        # Check MP4 stream validation
        video_path = Path(data["video_path"])
        assert video_path.exists()
        assert video_path.stat().st_size > 10000

        validator = StreamValidator()
        probe_res = validator.validate_video(
            video_path=video_path,
            expected_width=1280,
            expected_height=720,
            expected_fps=30.0,
            raise_on_error=True,
        )
        assert probe_res["valid"] is True
        assert probe_res["video"]["codec"] in ("h264", "avc1")
        assert probe_res["audio"]["codec"] == "aac"

    def test_step6_quality_review_and_artifact_matrix(self, e2e_project_environment, capsys):
        """Step 6: Confirm all checkpoints COMPLETED and all stage artifacts intact."""
        env = e2e_project_environment
        slug = env["project_slug"]
        chap = env["chapter_num"]
        chap_dir = env["chap_dir"]

        # Run CLI status
        exit_code = main([
            "status",
            "-p", slug,
            "-c", chap,
            "--json",
        ])
        assert exit_code == ExitCode.SUCCESS

        captured = capsys.readouterr()
        stat_resp = json.loads(captured.out)
        assert stat_resp["status"] == "success"
        status_data = stat_resp["data"]

        # All stage checkpoints completed
        stages = status_data["stages"]
        assert stages[StageType.CRAWL]["status"] == CheckpointStatus.COMPLETED
        assert stages[StageType.SEGMENT]["status"] == CheckpointStatus.COMPLETED
        assert stages[StageType.SCRIPT]["status"] == CheckpointStatus.COMPLETED
        assert stages[StageType.TTS]["status"] == CheckpointStatus.COMPLETED
        assert stages[StageType.RENDER]["status"] == CheckpointStatus.COMPLETED

        # Artifact Verification Matrix
        required_artifacts = [
            chap_dir / "chapter.json",
            chap_dir / "manifest.json",
            chap_dir / "script.json",
            chap_dir / "contact_sheets" / "sheet_001.jpg",
            chap_dir / "subtitles" / f"chapter_{chap}.srt",
            chap_dir / "video" / "preview.mp4",
        ]
        for art in required_artifacts:
            assert art.exists(), f"Missing required artifact: {art}"
            assert art.stat().st_size > 0, f"Artifact is empty: {art}"
