"""Semantic validation tests. Synthesized signals/images are test fixtures only."""
import json
import shutil
import subprocess
import wave
from pathlib import Path
import numpy as np
import pytest
from src.render.artifact_validation import (ArtifactValidationError, validate_audio, validate_video,
                                             validate_subtitles, validate_final_artifact)
from src.render.visual_plan_renderer import render_visual_plan
from src.core.visual_contract import Shot, VisualPlan
from src.core.schemas import PanelItem, PanelManifest
from src.core.storage import calculate_sha256
from PIL import Image


def wav(path, values, width=2):
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(width)
        stream.setframerate(48000)
        stream.writeframes(values)


def test_real_pcm_energy_and_header(tmp_path):
    path = tmp_path / "unit_signal.wav"
    values = (np.sin(np.arange(4800) / 10) * 1000).astype("<i2")
    wav(path, values.tobytes())
    result = validate_audio(path)
    assert result["duration"] == 0.1
    assert result["rms"] > 0
    assert result["sample_count"] == 4800


@pytest.mark.parametrize("kind", ["zero", "empty", "truncated", "header", "tiny", "inaudible"])
def test_invalid_wav_rejected(kind, tmp_path):
    path = tmp_path / "invalid.wav"
    if kind == "header": path.write_bytes(b"RIFF" + b"fake" * 30)
    else:
        count = 10 if kind == "tiny" else 4800
        samples = np.zeros(count, dtype="<i2")
        if kind == "tiny": samples[:] = 1000
        if kind == "inaudible":
            wav(path, np.ones(count, dtype="<i4").tobytes(), width=4)
        else: wav(path, b"" if kind == "empty" else samples.tobytes())
        if kind == "truncated": path.write_bytes(path.read_bytes()[:-15])
    with pytest.raises(ArtifactValidationError): validate_audio(path)


@pytest.mark.parametrize("text", ["", "1\n00:00:00,000 --> 00:00:01,000\n", "1\n00:00:01,000 --> 00:00:00,000\nx", "1\n00:00:00,000 --> 00:00:03,000\nx", "1\n00:99:00,000 --> 00:99:01,000\nx", "2\n00:00:00,000 --> 00:00:01,000\nx", "1\n00:00:00,000 --> 00:00:01,000\nx\n\n2\n00:00:00,500 --> 00:00:01,500\ny"])
def test_malformed_subtitles_fail_closed(text, tmp_path):
    path = tmp_path / "invalid.srt"
    path.write_text(text)
    with pytest.raises(ArtifactValidationError): validate_subtitles(path, 2)


def test_subtitle_valid_timeline(tmp_path):
    path = tmp_path / "valid.srt"
    path.write_text("1\n00:00:00,000 --> 00:00:01,000\nXin chào\n\n2\n00:00:01,000 --> 00:00:02,000\nTạm biệt\n")
    assert validate_subtitles(path, 2)["cue_count"] == 2


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="Media tools required")
def test_encoder_produces_decodable_frames_and_rejects_absent_audio(tmp_path):
    # L2 component verification only; no production setup/state or human approval.
    (tmp_path / "panels").mkdir()
    path = tmp_path / "panels" / "fixture.png"
    Image.fromarray(np.tile(np.arange(200, dtype=np.uint8), (100, 1))).convert("RGB").save(path)
    panel = PanelItem(panel_id="p", sequence_number=1, filename=path.name, relative_path="panels/fixture.png",
                      source_image="unit", crop_box=(0,0,200,100), width=200, height=100, aspect_ratio=2,
                      sha256=calculate_sha256(path))
    manifest = PanelManifest(project_slug="unit", chapter_number="1", panels=[panel])
    plan = VisualPlan(chapter_number="1", shots=[Shot(shot_id="s", segment_id="a", source_panel_id="p", duration_sec=0.5)],
                      total_duration_sec=0.5, target_width=320, target_height=180, fps=10)
    video = render_visual_plan(plan, tmp_path, manifest, tmp_path / "component.mp4")
    assert validate_video(video, 320, 180, 0.5, 10, require_audio=False)["frames"] == 5
    with pytest.raises(ArtifactValidationError, match="stream missing"):
        validate_video(video, 320, 180, 0.5, 10)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
@pytest.mark.parametrize("color", ["black", "white", "red"])
def test_real_encoded_blank_video_is_rejected(color, tmp_path):
    path = tmp_path / "blank.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color={color}:size=320x180:rate=10:duration=0.5", "-c:v", "libx264", str(path)], check=True)
    with pytest.raises(ArtifactValidationError, match="sampled video frame"):
        validate_video(path, 320, 180, 0.5, 10, require_audio=False)
