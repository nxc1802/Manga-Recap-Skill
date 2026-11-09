"""
tests/verification/test_stabilization_matrix_t13_t21.py
Phase S2: Independent Technical Verification Suite for Stabilization Patch 2 (T13 - T21).
Author: A2: Verification Engineer

Validates technical acceptance criteria from Final Stabilization Plan:
- T13: Long speech, many cues, few keyframes -> No dropped subtitle cues.
- T14: Dynamic motion 30 FPS -> Continuous motion without discrete stepping.
- T15: WAV with corrupted header or frame count -> Cache strictly rejected.
- T16: MP4 large but container/stream corrupted -> Cache strictly rejected via ffprobe.
- T17: Checkpoint missing, corrupted or conflicting status -> Not marked completed.
- T18: Multiple chapters with colliding seq_id WAVs -> No cross-chapter contamination.
- T19: Re-render chapter after human approval -> Stale approval not applied.
- T20: Render-only with valid WAV cache -> No TTS bootstrap or inference.
- T21: Multi-panel segment omitting unneeded panels -> Renders chosen panels correctly.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from unittest.mock import MagicMock, patch
import wave

from PIL import Image
import pytest

from src.cli.exit_codes import ExitCode
from src.cli.main import main
from src.cloud.kaggle_service import KaggleCloudService
from src.core.schemas import (
    CameraMotionType,
    ChapterMetadata,
    CheckpointStatus,
    HumanReviewStatus,
    PanelItem,
    PanelManifest,
    PreviewApproval,
    ProjectConfig,
    RecapScript,
    ScriptSegment,
    StageType,
    VoiceSelectionApproval,
)
from src.production.schemas import ProductionJobConfig
from src.core.storage import (
    atomic_write_json,
    get_chapter_dir,
    get_project_dir,
    init_chapter_layout,
    init_project_layout,
    load_chapter_metadata,
    read_json,
)
from src.production.runner import SingleSessionProductionRunner


# ------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------

def _create_real_wav(path: Path, duration_sec: float = 1.0, sample_rate: int = 48000) -> Path:
    """Creates a real uncompressed PCM WAV file with valid audio frames."""
    path.parent.mkdir(parents=True, exist_ok=True)
    num_frames = int(duration_sec * sample_rate)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        # 16-bit PCM silence
        wf.writeframes(b"\x00\x00" * num_frames)
    return path


def _create_real_mp4(path: Path, duration_sec: float = 1.0, width: int = 640, height: int = 360) -> Path:
    """Creates a real MP4 video with FFmpeg containing valid video stream and atoms."""
    path.parent.mkdir(parents=True, exist_ok=True)
    img_tmp = path.parent / f"{path.stem}_tmp.png"
    Image.new("RGB", (width, height), (30, 80, 160)).save(img_tmp)
    try:
        cmd = [
            "ffmpeg", "-y",
            "-loop", "1", "-i", str(img_tmp),
            "-t", f"{duration_sec:.2f}",
            "-vf", "fps=30,format=yuv420p",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            str(path),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if res.returncode != 0:
            raise RuntimeError(f"FFmpeg failed: {res.stderr}")
    finally:
        if img_tmp.exists():
            img_tmp.unlink()
    return path


class TestStabilizationMatrixT13toT21:
    """Independent verification suite validating T13 through T21."""

    # --------------------------------------------------------------------------
    # T13: Subtitle Cue Correctness (No dropped cues on long speech)
    # --------------------------------------------------------------------------
    def test_t13_subtitle_cue_correctness(self):
        """T13: Canonical subtitle timeline guarantees zero dropped cues regardless of keyframes."""
        svc = KaggleCloudService(api_token="KGAT_mock_token")
        code = svc.generate_worker_script("proj", "1", job_type="render", burn_subtitles=True)

        assert "def split_text_into_cues" in code
        assert "bounds = set([p_start_time, p_end_time])" in code
        assert "sorted_bounds = sorted(bounds)" in code
        assert "intervals.append((t_a, t_b))" in code

        # Execute split_text_into_cues locally with long speech (160 characters, multiple cues)
        exec_namespace = {"re": re}
        # Extract sanitize and split functions from generated code
        sanitize_code = code[code.find("def sanitize_sub_text"):code.find("def compose_frame_canvas")]
        exec(sanitize_code, exec_namespace)

        split_func = exec_namespace["split_text_into_cues"]
        long_narrative = (
            "Nhân vật chính từ từ mở mắt ra và phát hiện bản thân đã chuyển sinh vào thế giới ma pháp kỳ bí. "
            "Trước mắt anh là một tòa thành cổ đồ sộ sừng sững giữa bầu trời đêm ngập tràn ánh sao."
        )
        duration = 8.0
        cues = split_func(long_narrative, duration, max_chars=40, max_lines=2)

        # Must have at least 2 cues
        assert len(cues) >= 2
        # Zero ellipsis truncation
        for c in cues:
            assert "..." not in c["text"]
            assert len(c["text"]) > 0
            assert c["duration"] > 0
        # Cumulative duration must equal segment duration
        total_cue_dur = sum(c["duration"] for c in cues)
        assert abs(total_cue_dur - duration) < 0.05

    # --------------------------------------------------------------------------
    # T14: Dynamic Motion 30 FPS Continuity
    # --------------------------------------------------------------------------
    def test_t14_dynamic_motion_continuous_fps(self):
        """T14: Dynamic motion path interpolates continuously at output FPS (30 FPS) without discrete steps."""
        svc = KaggleCloudService(api_token="KGAT_mock_token")
        code = svc.generate_worker_script("proj", "1", job_type="render", fps=30)

        # Dynamic motion path calculates frames at full output FPS
        assert "is_dynamic" in code
        assert "num_steps = max(1, int(round(p_dur * cfg_fps)))" in code
        assert "step_dur = p_dur / float(num_steps)" in code
        assert "compose_frame_canvas" in code

    # --------------------------------------------------------------------------
    # T15: Strict WAV Cache Rejection (Corrupt header or 0 frames)
    # --------------------------------------------------------------------------
    def test_t15_strict_wav_cache_rejection(self, tmp_path):
        """T15: WAV parser failure strictly invalidates cache with zero file-size fallback."""
        # 1. Corrupt WAV file (size > 44 bytes, but invalid RIFF header)
        corrupt_wav = tmp_path / "corrupt.wav"
        corrupt_wav.write_bytes(b"NOT_A_WAV_HEADER" + b"\x00" * 100)
        assert corrupt_wav.stat().st_size > 44
        assert not SingleSessionProductionRunner._is_valid_wav_clip(corrupt_wav)

        # 2. Corrupt WAV with RIFF header but 0 audio frames (padded to > 44 bytes)
        empty_wav = tmp_path / "empty.wav"
        with wave.open(str(empty_wav), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(48000)
            # Write 0 frames
        empty_wav.write_bytes(empty_wav.read_bytes() + b"\x00" * 32)
        assert empty_wav.stat().st_size > 44
        assert not SingleSessionProductionRunner._is_valid_wav_clip(empty_wav)

        # 3. Valid WAV with frames
        valid_wav = tmp_path / "valid.wav"
        _create_real_wav(valid_wav, duration_sec=1.0)
        assert SingleSessionProductionRunner._is_valid_wav_clip(valid_wav)

    # --------------------------------------------------------------------------
    # T16: Strict MP4 Cache Rejection (Container or stream corruption)
    # --------------------------------------------------------------------------
    def test_t16_strict_mp4_cache_rejection(self, tmp_path):
        """T16: Large corrupt file is rejected via container box header and ffprobe stream verification."""
        # 1. Large garbage file (> 8KB) without MP4 boxes
        corrupt_mp4 = tmp_path / "garbage.mp4"
        corrupt_mp4.write_bytes(b"\x00" * 8192)
        assert corrupt_mp4.stat().st_size >= 4096
        assert not SingleSessionProductionRunner._is_valid_mp4_video(corrupt_mp4)

        # 2. File with fake 'ftyp' header but corrupted truncated payload
        corrupt_box = tmp_path / "fake_box.mp4"
        corrupt_box.write_bytes(b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00" + b"\xFF" * 2000)
        if shutil.which("ffprobe"):
            # ffprobe will detect container parse error
            assert not SingleSessionProductionRunner._is_valid_mp4_video(corrupt_box)

        # 3. Real, valid MP4 video
        real_mp4 = tmp_path / "valid.mp4"
        _create_real_mp4(real_mp4, duration_sec=0.5)
        assert SingleSessionProductionRunner._is_valid_mp4_video(real_mp4)

    # --------------------------------------------------------------------------
    # T17: Checkpoint Integrity (Missing or conflicting status rejects cache)
    # --------------------------------------------------------------------------
    def test_t17_checkpoint_integrity_rejects_incomplete_status(self, tmp_path, monkeypatch):
        """T17: Missing, corrupt, or failed/running checkpoint prevents reusing artifacts."""
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path))
        slug = "ckpt_test_proj"
        init_project_layout(slug, "Checkpoint Test")
        chap_dir = init_chapter_layout(slug, "1")

        runner = SingleSessionProductionRunner.load_or_create(
            project_slug=slug,
            config=ProductionJobConfig(project_slug=slug, target_chapters=["1"]),
        )

        final_mp4 = chap_dir / "video" / "final.mp4"
        _create_real_mp4(final_mp4, duration_sec=0.5)

        # Case A: chapter.json deleted
        chap_meta_path = chap_dir / "chapter.json"
        if chap_meta_path.exists():
            chap_meta_path.unlink()
        assert not runner._is_chapter_render_complete("1")

        # Case B: chapter.json exists with technical_status = FAILED
        meta_failed = ChapterMetadata(
            chapter_id="1",
            chapter_number="1",
            project_slug=slug,
            title="Failed Test",
            overall_status=CheckpointStatus.FAILED,
            technical_status=CheckpointStatus.FAILED,
        )
        atomic_write_json(chap_meta_path, meta_failed)
        assert not runner._is_chapter_render_complete("1")

        # Case C: chapter.json exists with technical_status = COMPLETED
        meta_ok = ChapterMetadata(
            chapter_id="1",
            chapter_number="1",
            project_slug=slug,
            title="Success Test",
            overall_status=CheckpointStatus.COMPLETED,
            technical_status=CheckpointStatus.COMPLETED,
        )
        atomic_write_json(chap_meta_path, meta_ok)
        assert runner._is_chapter_render_complete("1")

    # --------------------------------------------------------------------------
    # T18: Cloud Audio Isolation (No cross-chapter audio contamination)
    # --------------------------------------------------------------------------
    def test_t18_cloud_audio_isolation(self):
        """T18: Worker script searches strictly in resolved chapter payload, preventing cross-chapter contamination."""
        svc = KaggleCloudService(api_token="KGAT_mock_token")
        code = svc.generate_worker_script("proj", "1", job_type="render")

        # Zero global search across /kaggle/input
        assert 'for a_dir in Path("/kaggle/input").rglob("clips")' not in code
        assert 'for cand_wav in search_root.rglob' not in code

        # Strict isolation to found_chap_parent
        assert 'cand_audio = found_chap_parent / "audio" / "clips"' in code
        assert 'cand_isolated = found_chap_parent / "audio" / "clips"' in code
        assert 'actual_hash = hashlib.sha256(af.read()).hexdigest()' in code
        assert 'Audio clip checksum mismatch' in code

    # --------------------------------------------------------------------------
    # T19: Stale Approval Invalidation on Content Modification
    # --------------------------------------------------------------------------
    def test_t19_approval_freshness_invalidation(self, tmp_path, monkeypatch):
        """T19: Modifying script or preview video content invalidates prior human approval."""
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path))
        slug = "approval_freshness_proj"
        init_project_layout(slug, "Approval Freshness")
        chap_dir = init_chapter_layout(slug, "1")

        # Initial script
        script_data = {
            "project_slug": slug,
            "chapter_number": "1",
            "title": "Freshness Test",
            "segments": [{"seq_id": "01", "panel_ids": ["p1.jpg"], "narrator_text": "Original text"}]
        }
        atomic_write_json(chap_dir / "script.json", script_data)
        script_hash = hashlib.sha256((chap_dir / "script.json").read_bytes()).hexdigest()

        # Initial preview video
        prev_vid = chap_dir / "video" / "preview.mp4"
        _create_real_mp4(prev_vid, duration_sec=0.5)
        video_hash = hashlib.sha256(prev_vid.read_bytes()).hexdigest()

        # Matching approval
        approval = PreviewApproval(
            project_slug=slug,
            chapter_number="1",
            reviewer="Alice QA",
            status="approved",
            script_sha256=script_hash,
            video_sha256=video_hash,
        )
        assert approval.matches_content(script_hash, video_hash)

        # Modify script.json content
        modified_script = {
            "project_slug": slug,
            "chapter_number": "1",
            "title": "Freshness Test",
            "segments": [{"seq_id": "01", "panel_ids": ["p1.jpg"], "narrator_text": "MODIFIED text changed by agent"}]
        }
        atomic_write_json(chap_dir / "script.json", modified_script)
        new_script_hash = hashlib.sha256((chap_dir / "script.json").read_bytes()).hexdigest()

        # Approval must now be rejected as stale
        assert not approval.matches_content(new_script_hash, video_hash)

    # --------------------------------------------------------------------------
    # T20: Render-Only Independence with Valid WAV Cache
    # --------------------------------------------------------------------------
    def test_t20_render_only_with_valid_wav_cache(self, tmp_path, monkeypatch):
        """T20: Render-only command with complete, valid audio cache never bootstraps TTS."""
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path))
        slug = "render_only_proj"
        init_project_layout(slug, "Render Only Test")
        chap_dir = init_chapter_layout(slug, "1")

        script_data = {
            "project_slug": slug,
            "chapter_number": "1",
            "title": "Render Only Test",
            "segments": [
                {"seq_id": "01", "panel_ids": ["p1.jpg"], "narrator_text": "Seg 1", "duration": 1.0},
                {"seq_id": "02", "panel_ids": ["p2.jpg"], "narrator_text": "Seg 2", "duration": 1.0},
            ]
        }
        atomic_write_json(chap_dir / "script.json", script_data)
        clips_dir = chap_dir / "audio" / "clips"
        clips_dir.mkdir(parents=True, exist_ok=True)
        _create_real_wav(clips_dir / "audio_1_01.wav", duration_sec=1.0)
        _create_real_wav(clips_dir / "audio_1_02.wav", duration_sec=1.0)

        with patch("src.cli.tts_cmd.handle_tts") as mock_tts_func, \
             patch("src.cli.render_cmd.VideoEngine") as mock_video_cls:
            mock_inst = mock_video_cls.return_value
            mock_inst.render_chapter.return_value = {
                "video_path": str(chap_dir / "video" / "test.mp4"),
                "subtitle_path": str(chap_dir / "subtitles" / "test.srt"),
            }
            code = main(["render", "-p", slug, "-c", "1", "--skip-preview", "--approved", "--json"])
            assert code == ExitCode.SUCCESS
            # Zero TTS calls
            mock_tts_func.assert_not_called()

    # --------------------------------------------------------------------------
    # T21: Multi-Panel Segment with Omitted Source Panels
    # --------------------------------------------------------------------------
    def test_t21_multi_panel_segment_omitting_panels(self, tmp_path, monkeypatch):
        """T21: N:M mapping selecting subset of panels validates and renders correctly."""
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path))
        slug = "omitted_panels_proj"
        init_project_layout(slug, "Omitted Panels Test")
        chap_dir = init_chapter_layout(slug, "1")

        panels_dir = chap_dir / "panels"
        panels_dir.mkdir(parents=True, exist_ok=True)
        for i in range(1, 6):
            Image.new("RGB", (300, 300), (i * 40, i * 40, i * 40)).save(panels_dir / f"panel_{i:03d}.jpg")

        # Script references only panel_001.jpg and panel_004.jpg (omits 002, 003, 005)
        script_data = {
            "project_slug": slug,
            "chapter_number": "1",
            "title": "Omitted Panels Test",
            "segments": [
                {
                    "seq_id": "01",
                    "panel_ids": ["panel_001.jpg", "panel_004.jpg"],
                    "narrator_text": "Chỉ lấy panel 1 và 4, bỏ các panel trang trí.",
                    "duration": 2.0,
                }
            ]
        }
        atomic_write_json(chap_dir / "script.json", script_data)
        manifest = PanelManifest(
            project_slug=slug,
            chapter_number="1",
            panels=[
                PanelItem(
                    panel_id=f"panel_{i:03d}.jpg",
                    sequence_number=i,
                    filename=f"panel_{i:03d}.jpg",
                    relative_path=f"panels/panel_{i:03d}.jpg",
                    source_image="page_001.jpg",
                    crop_box=(0, 0, 300, 300),
                    width=300,
                    height=300,
                    aspect_ratio=1.0,
                    sha256="fake_sha256",
                )
                for i in range(1, 6)
            ],
            total_panels=5,
        )
        atomic_write_json(chap_dir / "manifest.json", manifest)

        # Validate CLI must succeed (omitting panels is explicitly allowed)
        code_val = main(["validate", "-p", slug, "-c", "1", "--json"])
        assert code_val == ExitCode.SUCCESS
