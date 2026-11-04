"""
tests/unit/test_video_engine.py
Unit and integration test suite for VideoEngine V2 coordinator (PR-07).
"""

import json
import math
import struct
import wave
from pathlib import Path
from PIL import Image

import pytest
from src.cli.main import main
from src.core.schemas import (
    CameraMotionType,
    CheckpointStatus,
    FocusBox,
    RecapScript,
    ScriptSegment,
    StageType,
)
from src.core.storage import (
    init_chapter_layout,
    init_project_layout,
    load_chapter_metadata,
    save_recap_script,
)
from src.render.video_engine import VideoEngine, VideoMode, video_engine


def make_test_wav(path: Path, duration_sec: float = 1.5, sample_rate: int = 48000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    num_samples = int(duration_sec * sample_rate)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        raw = bytearray()
        for i in range(num_samples):
            v = int(8000 * math.sin(2 * math.pi * 440 * i / sample_rate))
            raw.extend(struct.pack("<h", v))
        wf.writeframes(raw)
    return path


def make_test_image(path: Path, width: int = 800, height: int = 1200, color=(100, 150, 200)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (width, height), color=color)
    img.save(path, "JPEG")
    return path


class TestVideoEngineBasics:
    """Tests for VideoEngine initialization and legacy helper methods."""

    def setup_method(self):
        self.engine = VideoEngine(target_width=1280, target_height=720, fps=30)

    def test_initialization(self):
        assert self.engine.width == 1280
        assert self.engine.height == 720
        assert self.engine.fps == 30
        assert self.engine.camera_generator is not None
        assert self.engine.subtitle_generator is not None
        assert self.engine.audio_mixer is not None
        assert self.engine.validator is not None

    def test_detect_video_mode(self):
        assert self.engine.detect_video_mode(1600, 900) == VideoMode.LANDSCAPE_FIT
        assert self.engine.detect_video_mode(600, 1200) == VideoMode.PORTRAIT_VERTICAL
        assert self.engine.detect_video_mode(800, 800) == VideoMode.PORTRAIT_PADDED

    def test_split_text_into_chunks(self):
        text = "Câu thứ nhất rất rõ ràng. Câu thứ hai tiếp nối câu thứ nhất."
        chunks = self.engine.split_text_into_chunks(text, max_chars=30)
        assert len(chunks) >= 2
        for c in chunks:
            assert len(c) <= 35

    def test_create_panel_image_with_subs(self, tmp_path):
        img_p = make_test_image(tmp_path / "panel.jpg", width=800, height=800)
        out_p = tmp_path / "out_subbed.webp"
        res = self.engine.create_panel_image_with_subs(
            str(img_p),
            text="Phụ đề thử nghiệm",
            output_path=str(out_p),
            focus_x=50,
            focus_y=50,
            crop_scale=80,
        )
        assert Path(res).exists()
        with Image.open(res) as im:
            assert im.size == (1280, 720)


class TestVideoEngineChapterRender:
    """Full render pipeline tests using synthetic assets."""

    @pytest.fixture
    def setup_synthetic_chapter(self, tmp_path, monkeypatch):
        data_dir = tmp_path / "data"
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(data_dir))

        proj_slug = "synthetic_m4_proj"
        init_project_layout(proj_slug, "Synthetic M4 Project")
        init_chapter_layout(proj_slug, "1")

        proj_dir = data_dir / "projects" / proj_slug
        chap_dir = proj_dir / "chapters" / "1"

        # Create 2 panels
        p1 = make_test_image(chap_dir / "panels" / "panel_001.jpg", width=800, height=1200, color=(180, 50, 50))
        p2 = make_test_image(chap_dir / "panels" / "panel_002.jpg", width=1200, height=800, color=(50, 180, 50))

        # Create 2 audio clips (1.2s and 1.8s)
        a1 = make_test_wav(chap_dir / "audio" / "clips" / "audio_1_001.wav", duration_sec=1.2)
        a2 = make_test_wav(chap_dir / "audio" / "clips" / "audio_1_002.wav", duration_sec=1.8)

        # Create script.json
        script = RecapScript(
            project_slug=proj_slug,
            chapter_number="1",
            title="Episode 1 Test",
            segments=[
                ScriptSegment(
                    seq_id="001",
                    panel_ids=["panel_001.jpg"],
                    narrator_text="[thở dài] Nhân vật chính xuất hiện trong bóng đêm.",
                    camera_motion=CameraMotionType.ZOOM_IN,
                    focus_box=FocusBox(x_min=0.2, y_min=0.1, x_max=0.8, y_max=0.7),
                    duration=1.2,
                    audio_path="audio/clips/audio_1_001.wav",
                ),
                ScriptSegment(
                    seq_id="002",
                    panel_ids=["panel_002.jpg"],
                    narrator_text="Đối thủ lập tức tung đòn tấn công chí mạng!",
                    camera_motion=CameraMotionType.PAN_DOWN,
                    duration=1.8,
                    audio_path="audio/clips/audio_1_002.wav",
                ),
            ],
            total_duration_seconds=3.0,
        )
        save_recap_script(chap_dir, script)

        return proj_slug, proj_dir, chap_dir

    def test_render_chapter_preview(self, setup_synthetic_chapter):
        proj_slug, proj_dir, chap_dir = setup_synthetic_chapter
        engine = VideoEngine()

        result = engine.render_chapter(
            project_dir=proj_dir,
            chapter_num="1",
            quality="preview",
            burn_subtitles=True,
            keep_temp=False,
            suffix="preview",
        )

        assert result["quality"] == "preview"
        assert result["resolution"] == "1280x720"
        assert result["fps"] == 30.0
        assert Path(result["video_path"]).exists()
        assert Path(result["subtitle_path"]).exists()

        # Check validation
        assert result["validation"]["valid"] is True
        assert result["duration_diff_seconds"] <= 0.15

        # Check chapter.json checkpoint
        meta = load_chapter_metadata(chap_dir)
        assert meta.checkpoints[StageType.RENDER].status == CheckpointStatus.COMPLETED

    def test_render_chapter_final_1080p(self, setup_synthetic_chapter):
        proj_slug, proj_dir, chap_dir = setup_synthetic_chapter
        engine = VideoEngine()

        result = engine.render_chapter(
            project_dir=proj_dir,
            chapter_num="1",
            quality="final",
            burn_subtitles=True,
            keep_temp=False,
            suffix="final",
        )

        assert result["quality"] == "final"
        assert result["resolution"] == "1920x1080"
        assert result["fps"] == 30.0
        assert Path(result["video_path"]).exists()
        assert Path(result["subtitle_path"]).exists()

        # Check stream validation
        assert result["validation"]["valid"] is True
        assert result["validation"]["video"]["width"] == 1920
        assert result["validation"]["video"]["height"] == 1080
        assert result["duration_diff_seconds"] <= 0.15

        # Check chapter.json checkpoint
        meta = load_chapter_metadata(chap_dir)
        assert meta.checkpoints[StageType.RENDER].status == CheckpointStatus.COMPLETED

    def test_render_cli_command(self, setup_synthetic_chapter, capsys):
        proj_slug, proj_dir, chap_dir = setup_synthetic_chapter

        code = main(["render", "-p", proj_slug, "-c", "1", "--quality", "preview", "--json"])
        assert code == 0

        captured = capsys.readouterr()
        response = json.loads(captured.out.strip())
        assert response["status"] == "success"
        assert response["command"] == "render"
        assert response["exit_code"] == 0
        assert "video_path" in response["data"]
        assert Path(response["data"]["video_path"]).exists()
        assert response["data"]["validation"]["valid"] is True

    def test_render_chapter_clip_cache_and_force(self, setup_synthetic_chapter):
        proj_slug, proj_dir, chap_dir = setup_synthetic_chapter
        engine = VideoEngine()

        # Initial render with keep_temp=True so clips and metadata remain
        res1 = engine.render_chapter(
            project_dir=proj_dir,
            chapter_num="1",
            quality="preview",
            keep_temp=True,
            force=False,
        )
        assert res1["validation"]["valid"] is True

        clip_p = chap_dir / "video" / "clips" / "clip_preview_001.mp4"
        meta_p = chap_dir / "video" / "clips" / "clip_preview_001.meta.json"
        assert clip_p.exists()
        assert meta_p.exists()

        mtime_initial = clip_p.stat().st_mtime_ns

        # Second render with force=False -> Cache Hit (mtime unchanged)
        res2 = engine.render_chapter(
            project_dir=proj_dir,
            chapter_num="1",
            quality="preview",
            keep_temp=True,
            force=False,
        )
        assert res2["validation"]["valid"] is True
        assert clip_p.stat().st_mtime_ns == mtime_initial

        # Third render with force=True -> Cache Bypassed (clip re-rendered)
        res3 = engine.render_chapter(
            project_dir=proj_dir,
            chapter_num="1",
            quality="preview",
            keep_temp=True,
            force=True,
        )
        assert res3["validation"]["valid"] is True
        assert clip_p.stat().st_mtime_ns != mtime_initial

    def test_render_cli_command_with_force(self, setup_synthetic_chapter, capsys):
        proj_slug, proj_dir, chap_dir = setup_synthetic_chapter

        code = main(["render", "-p", proj_slug, "-c", "1", "--quality", "preview", "--force", "--json"])
        assert code == 0

        captured = capsys.readouterr()
        response = json.loads(captured.out.strip())
        assert response["status"] == "success"
        assert response["command"] == "render"
        assert response["exit_code"] == 0
        assert response["data"]["validation"]["valid"] is True

