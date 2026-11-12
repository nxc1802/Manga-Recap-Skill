"""Unit and L2 FFmpeg tests for Phase H-J fast-path renderer, parallel shot NVENC/x264, and -c copy concat."""

from __future__ import annotations

import json
import shutil
import subprocess
import wave
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from PIL import Image

from src.core.human_review import approve_preview, review_fingerprint
from src.core.schemas import PanelItem, PanelManifest, RecapScript, ScriptSegment
from src.core.series_setup import ALL_QUESTION_IDS, QUESTION_METADATA, record_series_setup
from src.core.storage import atomic_write_json, calculate_sha256, init_chapter_layout, init_project_layout
from src.core.visual_contract import ProductionMotion, Region, Shot, VisualPlan
from src.production.hardware import (
    get_encoder_codec_args,
    probe_ffmpeg_capabilities,
    smoke_test_encoder,
)
from src.render.artifact_validation import fingerprint, validate_final_artifact, validate_video
from src.render.chapter_renderer import render_chapter
from src.render.fast_renderer import (
    build_shot_render_tasks,
    concat_shot_clips,
    render_shot_clip,
    render_visual_plan_fast,
)
from src.render.visual_plan_renderer import frame_schedule, render_frame, render_visual_plan


def _ffprobe_stream(path: Path) -> Dict[str, Any]:
    res = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-count_frames",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(res.stdout)


def _decode_frame_rgb(video_path: Path, frame_idx: int, width: int, height: int) -> np.ndarray:
    raw = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            "-vf",
            f"select=eq(n\\,{frame_idx})",
            "-pix_fmt",
            "rgb24",
            "-f",
            "rawvideo",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
    ).stdout
    return np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3)


def _make_panel(path: Path, arr: np.ndarray, panel_id: str, seq: int) -> PanelItem:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(arr).convert("RGB").save(path)
    h, w = arr.shape[:2]
    return PanelItem(
        panel_id=panel_id,
        sequence_number=seq,
        filename=path.name,
        relative_path=f"panels/{path.name}",
        source_image="page_01.png",
        width=w,
        height=h,
        aspect_ratio=w / float(h),
        crop_box=(0, 0, w, h),
        sha256=calculate_sha256(path),
        evidence_metadata={"text_detection_uncertain": False},
    )


def _make_audio_wav(path: Path, duration_sec: float, freq_hz: float = 440.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    sr = 48000
    n_samples = int(round(duration_sec * sr))
    samples = (np.sin(2.0 * np.pi * freq_hz * np.arange(n_samples) / sr) * 4000).astype("<i2")
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(samples.tobytes())
    return path


def _make_valid_setup_answers(source_dir: Path, *, burn_subtitles: bool = False) -> Dict[str, Any]:
    defaults = {
        "Q01": {"source": str(source_dir), "chapters": "1"},
        "Q02": "local",
        "Q03": "Ngọc Huyền",
        "Q04": 1.0,
        "Q05": burn_subtitles,
        "Q06": "static",
        "Q07": False,
        "Q08": {"no_upload": True},
        "Q09": "preview",
        "Q10": {"none": True},
    }
    return {
        qid: {
            "question_id": qid,
            "question_title": QUESTION_METADATA[qid]["title"],
            "user_answer": defaults[qid],
            "answer_source": "user",
        }
        for qid in ALL_QUESTION_IDS
    }


def test_hardware_smoke_uses_256x256_and_codec_args_options():
    with patch("shutil.which", return_value="/usr/bin/ffmpeg"), patch(
        "subprocess.run", return_value=MagicMock(returncode=0)
    ) as mock_run:
        assert smoke_test_encoder("h264_nvenc") is True
        cmd = mock_run.call_args.args[0]
        assert any("s=256x256" in str(arg) for arg in cmd)
        assert "64x64" not in " ".join(str(x) for x in cmd)

    nv_preview = get_encoder_codec_args("h264_nvenc", quality_mode="preview", gpu_index=0)
    assert "-gpu" in nv_preview and nv_preview[nv_preview.index("-gpu") + 1] == "0"
    assert "-profile:v" in nv_preview and "high" in nv_preview
    assert "-preset" in nv_preview and "p3" in nv_preview
    assert "-cq" in nv_preview and "24" in nv_preview

    nv_final = get_encoder_codec_args("h264_nvenc", quality_mode="final", gpu_index=0)
    assert "-preset" in nv_final and "p4" in nv_final
    assert "-cq" in nv_final and "20" in nv_final

    x264_preview = get_encoder_codec_args("libx264", quality_mode="preview")
    assert "-profile:v" in x264_preview and "high" in x264_preview
    assert "-preset" in x264_preview and "veryfast" in x264_preview


def test_probe_ffmpeg_capabilities_real_and_missing_binary():
    missing = probe_ffmpeg_capabilities("nonexistent_ffmpeg_binary_xyz")
    assert set(missing.keys()) >= {
        "h264_nvenc",
        "libx264",
        "crop",
        "scale",
        "overlay",
        "subtitles",
        "ass",
        "scale_cuda",
        "zoompan",
    }
    assert all(v is False for v in missing.values())

    if shutil.which("ffmpeg"):
        caps = probe_ffmpeg_capabilities()
        assert caps["libx264"] is True
        assert caps["crop"] is True
        assert caps["scale"] is True
        assert caps["overlay"] is True


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg required")
def test_static_fastpath_caches_canvases_and_matches_canonical_render_frame(tmp_path):
    y_grad = np.linspace(20, 220, 180, dtype=np.uint8)[:, None]
    x_grad = np.linspace(30, 210, 240, dtype=np.uint8)[None, :]
    img_arr = np.stack(
        [
            np.broadcast_to(x_grad, (180, 240)),
            np.broadcast_to(y_grad, (180, 240)),
            np.full((180, 240), 110, dtype=np.uint8),
        ],
        axis=-1,
    )
    p1 = _make_panel(tmp_path / "panels" / "p1.png", img_arr, "p1", 1)
    manifest = PanelManifest(project_slug="unit", chapter_number="1", panels=[p1])
    shots = [
        Shot(shot_id="s1", segment_id="seg1", source_panel_id="p1", duration_sec=0.4, motion_type=ProductionMotion.STATIC),
        Shot(
            shot_id="s2",
            segment_id="seg2",
            source_panel_id="p1",
            source_box=Region(x_min=0.1, y_min=0.1, x_max=0.9, y_max=0.9),
            duration_sec=0.3,
            motion_type=ProductionMotion.STATIC_FOCUS,
        ),
        Shot(shot_id="s3", segment_id="seg3", source_panel_id="p1", duration_sec=0.3, motion_type=ProductionMotion.STATIC),
    ]
    plan = VisualPlan(chapter_number="1", shots=shots, total_duration_sec=1.0, target_width=320, target_height=180, fps=10)

    call_count = 0
    real_render_frame = render_frame

    def counted_render_frame(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return real_render_frame(*args, **kwargs)

    with patch("src.render.fast_renderer.render_frame", side_effect=counted_render_frame):
        out_mp4 = render_visual_plan(plan, tmp_path, manifest, tmp_path / "fast_static.mp4")

    # Shots s1 and s3 share an identical canvas_key -> 2 unique canvases composed, not 3 shots or 10 frames.
    assert call_count == 2
    assert validate_video(out_mp4, 320, 180, 1.0, 10, require_audio=False)["frames"] == 10

    with Image.open(tmp_path / "panels" / "p1.png") as im:
        expected_s1 = np.array(render_frame(im, shots[0], plan, 0.0), dtype=np.int16)
        expected_s2 = np.array(render_frame(im, shots[1], plan, 0.0), dtype=np.int16)

    actual_s1 = _decode_frame_rgb(out_mp4, 0, 320, 180).astype(np.int16)
    actual_s2 = _decode_frame_rgb(out_mp4, 5, 320, 180).astype(np.int16)
    assert float(np.mean(np.abs(actual_s1 - expected_s1))) < 5.0
    assert float(np.mean(np.abs(actual_s2 - expected_s2))) < 5.0


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg required")
def test_slow_pan_down_fastpath_trajectory_matches_canonical_render_frame(tmp_path):
    # High-contrast vertical stripe gradient so top->bottom pan shifts luminance cleanly.
    rows = np.linspace(15, 240, 600, dtype=np.uint8)[:, None]
    cols = np.linspace(0, 40, 200, dtype=np.uint8)[None, :]
    img_arr = np.stack(
        [
            np.clip(rows.astype(np.int16) + cols, 0, 255).astype(np.uint8),
            np.broadcast_to(rows, (600, 200)),
            np.broadcast_to(255 - rows, (600, 200)),
        ],
        axis=-1,
    )
    p1 = _make_panel(tmp_path / "panels" / "tall.png", img_arr, "p1", 1)
    manifest = PanelManifest(project_slug="unit", chapter_number="1", panels=[p1])
    pan_shot = Shot(
        shot_id="pan1",
        segment_id="a",
        source_panel_id="p1",
        duration_sec=2.0,
        motion_type=ProductionMotion.SLOW_PAN_DOWN,
        scroll_fraction_per_second=0.12,
    )
    # Also verify zero-travel SLOW_PAN_DOWN (landscape crop) degrades safely without error.
    flat_shot = Shot(
        shot_id="pan_flat",
        segment_id="a",
        source_panel_id="p1",
        source_box=Region(x_min=0.0, y_min=0.0, x_max=1.0, y_max=0.15),
        duration_sec=0.5,
        motion_type=ProductionMotion.SLOW_PAN_DOWN,
        scroll_fraction_per_second=0.12,
    )
    plan = VisualPlan(
        chapter_number="1",
        shots=[pan_shot, flat_shot],
        total_duration_sec=2.5,
        target_width=200,
        target_height=100,
        fps=10,
    )
    out_mp4 = render_visual_plan_fast(plan, tmp_path, manifest, tmp_path / "pan.mp4", max_workers=2)
    assert validate_video(out_mp4, 200, 100, 2.5, 10, require_audio=False)["frames"] == 25

    with Image.open(tmp_path / "panels" / "tall.png") as im:
        exp_t0 = np.array(render_frame(im, pan_shot, plan, 0.0), dtype=np.int16)
        exp_t1 = np.array(render_frame(im, pan_shot, plan, 1.0), dtype=np.int16)
        exp_t19 = np.array(render_frame(im, pan_shot, plan, 1.9), dtype=np.int16)

    act_f0 = _decode_frame_rgb(out_mp4, 0, 200, 100).astype(np.int16)
    act_f10 = _decode_frame_rgb(out_mp4, 10, 200, 100).astype(np.int16)
    act_f19 = _decode_frame_rgb(out_mp4, 19, 200, 100).astype(np.int16)

    # Top-down progression increases red/green channel means monotonically.
    assert act_f0[:, :, 1].mean() < act_f10[:, :, 1].mean() < act_f19[:, :, 1].mean()
    assert float(np.mean(np.abs(act_f0 - exp_t0))) < 8.0
    assert float(np.mean(np.abs(act_f10 - exp_t1))) < 8.0
    assert float(np.mean(np.abs(act_f19 - exp_t19))) < 8.0


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg required")
def test_parallel_shot_clips_uniform_stream_and_zero_reencode_concat(tmp_path):
    arr1 = np.tile(np.arange(200, dtype=np.uint8), (200, 1))
    arr1 = np.stack([arr1, arr1, 255 - arr1], axis=-1)
    rows = np.linspace(30, 220, 600, dtype=np.uint8)[:, None]
    arr2 = np.stack([np.broadcast_to(rows, (600, 200)), np.full((600, 200), 90, dtype=np.uint8), np.broadcast_to(255 - rows, (600, 200))], axis=-1)

    p1 = _make_panel(tmp_path / "panels" / "p1.png", arr1, "p1", 1)
    p2 = _make_panel(tmp_path / "panels" / "p2.png", arr2, "p2", 2)
    manifest = PanelManifest(project_slug="unit", chapter_number="1", panels=[p1, p2])

    shots = [
        Shot(shot_id="s1", segment_id="seg1", source_panel_id="p1", duration_sec=0.43, motion_type=ProductionMotion.STATIC),
        Shot(shot_id="s2", segment_id="seg1", source_panel_id="p1", source_box=Region(x_min=0.2, y_min=0.2, x_max=0.8, y_max=0.8), duration_sec=0.67, motion_type=ProductionMotion.STATIC_FOCUS),
        Shot(shot_id="s3", segment_id="seg2", source_panel_id="p2", duration_sec=0.90, motion_type=ProductionMotion.SLOW_PAN_DOWN, scroll_fraction_per_second=0.12),
    ]
    plan = VisualPlan(chapter_number="1", shots=shots, total_duration_sec=2.0, target_width=320, target_height=180, fps=30)
    work_dir = tmp_path / "work"

    tasks = build_shot_render_tasks(tmp_path, plan, manifest, work_dir=work_dir, encoder="libx264")
    assert [t["frame_count"] for t in tasks] == [count for _, count in frame_schedule(plan)]
    assert sum(t["frame_count"] for t in tasks) == 60

    canvas_cache: Dict[str, Any] = {}
    clips = [render_shot_clip(t, canvas_cache=canvas_cache, gpu_index=0) for t in tasks]
    for clip, task in zip(clips, tasks):
        probe = _ffprobe_stream(clip)
        vstream = next(s for s in probe["streams"] if s["codec_type"] == "video")
        assert vstream["codec_name"] == "h264"
        assert vstream["pix_fmt"] == "yuv420p"
        assert (int(vstream["width"]), int(vstream["height"])) == (320, 180)
        assert vstream["avg_frame_rate"] == "30/1"
        assert vstream["time_base"] == "1/30000"
        assert vstream.get("sample_aspect_ratio", "1:1") == "1:1"
        assert int(vstream["nb_read_frames"]) == task["frame_count"]

    audio_wav = _make_audio_wav(tmp_path / "audio.wav", 2.0)
    final_mp4 = concat_shot_clips(
        clips,
        tmp_path / "concat.mp4",
        work_dir=work_dir,
        audio_path=audio_wav,
        burn_subtitles=False,
        target_height=180,
        fps=30,
        encoder="libx264",
    )
    report = validate_video(final_mp4, 320, 180, 2.0, 30, require_audio=True)
    assert report["frames"] == 60


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg required")
def test_concat_shot_clips_missing_libass_pillow_overlay_fallback(tmp_path):
    arr = np.full((180, 320, 3), (30, 70, 180), dtype=np.uint8)
    arr[20:60, 40:280] = (200, 200, 40)
    p1 = _make_panel(tmp_path / "panels" / "p1.png", arr, "p1", 1)
    manifest = PanelManifest(project_slug="unit", chapter_number="1", panels=[p1])
    plan = VisualPlan(
        chapter_number="1",
        shots=[Shot(shot_id="s1", segment_id="a", source_panel_id="p1", duration_sec=2.0)],
        total_duration_sec=2.0,
        target_width=320,
        target_height=180,
        fps=10,
    )
    work_dir = tmp_path / "work_sub"
    tasks = build_shot_render_tasks(tmp_path, plan, manifest, work_dir=work_dir)
    clips = [render_shot_clip(t) for t in tasks]
    srt_path = tmp_path / "captions.srt"
    srt_path.write_text("1\n00:00:00,500 --> 00:00:01,500\nTiếng Việt có dấu\n", encoding="utf-8")
    audio_wav = _make_audio_wav(tmp_path / "audio.wav", 2.0)

    no_libass_caps = {
        "h264_nvenc": False,
        "libx264": True,
        "h264_videotoolbox": False,
        "crop": True,
        "scale": True,
        "overlay": True,
        "subtitles": False,
        "ass": False,
        "scale_cuda": False,
        "zoompan": False,
    }
    with patch("src.render.fast_renderer._get_ffmpeg_capabilities", return_value=no_libass_caps):
        out_mp4 = concat_shot_clips(
            clips,
            tmp_path / "burned_no_libass.mp4",
            work_dir=work_dir,
            audio_path=audio_wav,
            subtitle_path=srt_path,
            burn_subtitles=True,
            target_height=180,
            fps=10,
            encoder="libx264",
        )

    assert validate_video(out_mp4, 320, 180, 2.0, 10, require_audio=True)["frames"] == 20
    # Frame 1 (t=0.1s, outside cue) has clean blue bottom band; frame 10 (t=1.0s, active cue) has dark pill + white text.
    frame_quiet = _decode_frame_rgb(out_mp4, 1, 320, 180)
    frame_active = _decode_frame_rgb(out_mp4, 10, 320, 180)
    bottom_quiet = frame_quiet[125:175, 40:280]
    bottom_active = frame_active[125:175, 40:280]
    assert float(bottom_active.var()) > float(bottom_quiet.var()) + 50.0


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg required")
def test_render_chapter_single_pass_burn_subtitles_and_preview_promotion(tmp_path, monkeypatch):
    monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path))
    init_project_layout("fast_unit", "Fastpath Chapter Test")
    init_chapter_layout("fast_unit", "1")
    root = tmp_path / "projects" / "fast_unit"
    chapter = root / "chapters" / "1"

    answers = _make_valid_setup_answers(chapter / "raw", burn_subtitles=True)
    record_series_setup("fast_unit", answers, project_dir=root)
    atomic_write_json(
        root / "voice_selection.json",
        dict(project_slug="fast_unit", primary_voice="Ngọc Huyền", reviewer="unit", status="approved", speed=1.0),
    )

    arr = np.tile(np.arange(200, dtype=np.uint8), (150, 1))
    arr = np.stack([arr, 255 - arr, np.full((150, 200), 100, dtype=np.uint8)], axis=-1)
    p1 = _make_panel(chapter / "panels" / "p1.png", arr, "p1", 1)
    atomic_write_json(chapter / "manifest.json", PanelManifest(project_slug="fast_unit", chapter_number="1", panels=[p1]))

    audio_rel = "audio/clips/seg_001.wav"
    audio_path = _make_audio_wav(chapter / audio_rel, 1.0)
    script = RecapScript(
        project_slug="fast_unit",
        chapter_number="1",
        segments=[
            ScriptSegment(
                seq_id="001",
                evidence_panel_ids=["p1"],
                narrator_text="Xin chào Đặng Ngọc Huyền!",
                duration=1.0,
                audio_path=audio_rel,
                checksum=calculate_sha256(audio_path),
            )
        ],
    )
    atomic_write_json(chapter / "script.json", script)

    preview_res = render_chapter(root, "1", quality="preview", encoder="libx264")
    assert preview_res["validation"]["valid"] is True
    assert preview_res["validation"]["video"]["frames"] == 30
    assert (chapter / "artifact_validation.json").is_file()
    assert not (chapter / "video" / "visual.mp4").exists()

    # Re-validate via validate_final_artifact and verify Chapter 1 final promotion preserves exact preview hash.
    preview_hash = fingerprint(preview_res["video_path"])
    approve_preview(root, "unit", review_fingerprint(root), preview_hash)
    final_res = render_chapter(root, "1", quality="final", encoder="libx264")
    assert fingerprint(final_res["video_path"]) == preview_hash
    recheck = validate_final_artifact(
        final_res["video_path"],
        final_res["subtitle_path"],
        [audio_path],
        VisualPlan.load(chapter / "visual_plan.json"),
        script,
        PanelManifest.model_validate_json((chapter / "manifest.json").read_text(encoding="utf-8")),
        "1",
        chapter_dir=chapter,
    )
    assert recheck["valid"] is True
