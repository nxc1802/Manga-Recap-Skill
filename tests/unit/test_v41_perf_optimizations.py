"""Deterministic unit & media tests for V4.1 Performance Optimizations."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
from PIL import Image

from src.cloud.dual_t4_orchestrator import build_hardware_autotune_probe
from src.cloud.elastic_scheduler import StartupAutotuner
from src.core.schemas import PanelItem, PanelManifest
from src.core.storage import calculate_sha256
from src.core.visual_contract import ProductionMotion, Region, Shot, VisualPlan
from src.render.artifact_validation import ArtifactValidationError, validate_video
from src.render.fast_renderer import (
    _build_static_subtitle_intervals,
    _compute_pan_geometry,
    _get_or_create_pan_strip_png,
    build_shot_render_tasks,
    render_shot_clip,
)
from src.render.subtitle import SubtitleCue

pytestmark = [pytest.mark.ci_l3]


def _make_panel(path: Path, arr: np.ndarray, panel_id: str, index: int) -> PanelItem:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(arr, mode="RGB").save(path, format="PNG")
    h, w = arr.shape[:2]
    return PanelItem(
        panel_id=panel_id,
        sequence_number=index,
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


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg required")
def test_v41_static_subtitle_intervals_zero_overlay_fallback(tmp_path):
    """Static/StaticFocus with burn_subtitles and no libass uses PIL-composited loop intervals."""
    arr = np.full((180, 320, 3), (40, 80, 150), dtype=np.uint8)
    arr[20:70, 50:270] = (210, 190, 60)
    p1 = _make_panel(tmp_path / "panels" / "p1.png", arr, "p1", 1)
    manifest = PanelManifest(project_slug="v41", chapter_number="1", panels=[p1])
    plan = VisualPlan(
        chapter_number="1",
        shots=[Shot(shot_id="s1", segment_id="seg1", source_panel_id="p1", duration_sec=2.0, motion_type=ProductionMotion.STATIC_FOCUS)],
        total_duration_sec=2.0,
        target_width=320,
        target_height=180,
        fps=10,
    )
    cues = [
        SubtitleCue(index=1, start_seconds=0.3, end_seconds=1.1, text="Dòng phụ đề một"),
        SubtitleCue(index=2, start_seconds=1.3, end_seconds=1.9, text="Dòng phụ đề hai"),
    ]
    work_dir = tmp_path / "work"
    tasks = build_shot_render_tasks(
        tmp_path,
        plan,
        manifest,
        work_dir=work_dir,
        subtitle_cues=cues,
        burn_subtitles=True,
    )
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
    captured_cmds = []
    real_run = subprocess.run

    def spy_run(cmd, *args, **kwargs):
        if cmd and cmd[0] == "ffmpeg":
            captured_cmds.append(list(cmd))
        return real_run(cmd, *args, **kwargs)

    with (
        patch("src.render.fast_renderer._get_ffmpeg_capabilities", return_value=no_libass_caps),
        patch("src.render.fast_renderer.subprocess.run", side_effect=spy_run),
    ):
        clip = render_shot_clip(tasks[0])

    assert captured_cmds, "Expected ffmpeg invocation"
    cmd_str = " ".join(captured_cmds[0])
    assert "overlay=" not in cmd_str, "Should use PIL-composited loop intervals instead of CPU overlay"
    assert "loop=loop=" in cmd_str
    rep = validate_video(clip, 320, 180, 2.0, 10, require_audio=False)
    assert rep["frames"] == 20


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg required")
def test_v41_slow_pan_down_pre_scaled_strip_loop(tmp_path):
    """SLOW_PAN_DOWN pre-scales strip once and uses single-decode loop+crop without per-frame lanczos."""
    rows = np.linspace(20, 235, 600, dtype=np.uint8)[:, None]
    cols = np.linspace(0, 30, 200, dtype=np.uint8)[None, :]
    arr = np.stack(
        [
            np.clip(rows.astype(np.int16) + cols, 0, 255).astype(np.uint8),
            np.broadcast_to(rows, (600, 200)),
            np.broadcast_to(255 - rows, (600, 200)),
        ],
        axis=-1,
    )
    p1 = _make_panel(tmp_path / "panels" / "tall.png", arr, "p1", 1)
    manifest = PanelManifest(project_slug="v41", chapter_number="1", panels=[p1])
    plan = VisualPlan(
        chapter_number="1",
        shots=[
            Shot(
                shot_id="pan1",
                segment_id="seg1",
                source_panel_id="p1",
                duration_sec=2.0,
                motion_type=ProductionMotion.SLOW_PAN_DOWN,
                scroll_fraction_per_second=0.12,
            )
        ],
        total_duration_sec=2.0,
        target_width=320,
        target_height=180,
        fps=10,
    )
    tasks = build_shot_render_tasks(tmp_path, plan, manifest, work_dir=tmp_path / "work")
    captured_cmds = []
    real_run = subprocess.run

    def spy_run(cmd, *args, **kwargs):
        if cmd and cmd[0] == "ffmpeg":
            captured_cmds.append(list(cmd))
        return real_run(cmd, *args, **kwargs)

    with patch("src.render.fast_renderer.subprocess.run", side_effect=spy_run):
        clip = render_shot_clip(tasks[0])

    cmd_str = " ".join(captured_cmds[0])
    assert "flags=lanczos" not in cmd_str
    assert "loop=loop=19:size=1:start=0" in cmd_str
    rep = validate_video(clip, 320, 180, 2.0, 10, require_audio=False)
    assert rep["frames"] == 20
    # Verify vertical gradient moved between 10% and 90% timestamps
    assert rep["samples"][2]["mean"] != pytest.approx(rep["samples"][0]["mean"], abs=1.0)


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg required")
def test_v41_validate_video_fast_probe_rejects_bad_dimensions_and_duration(tmp_path):
    """Fast packet probe + -ss seek validates good clips and still rejects bad dimensions/duration."""
    arr = np.full((180, 320, 3), (60, 120, 200), dtype=np.uint8)
    arr[30:90, 40:260] = (230, 210, 50)
    p1 = _make_panel(tmp_path / "panels" / "p1.png", arr, "p1", 1)
    manifest = PanelManifest(project_slug="v41", chapter_number="1", panels=[p1])
    plan = VisualPlan(
        chapter_number="1",
        shots=[Shot(shot_id="s1", segment_id="seg1", source_panel_id="p1", duration_sec=1.0)],
        total_duration_sec=1.0,
        target_width=320,
        target_height=180,
        fps=10,
    )
    tasks = build_shot_render_tasks(tmp_path, plan, manifest, work_dir=tmp_path / "work")
    clip = render_shot_clip(tasks[0])

    rep = validate_video(clip, 320, 180, 1.0, 10, require_audio=False)
    assert rep["valid"] is True
    assert rep["frames"] == 10

    with pytest.raises(ArtifactValidationError, match="Wrong video dimensions"):
        validate_video(clip, 640, 360, 1.0, 10, require_audio=False)

    with pytest.raises(ArtifactValidationError, match="Invalid or mismatched media duration"):
        validate_video(clip, 320, 180, 5.0, 10, require_audio=False)


def test_v41_hardware_autotune_probe_scales_on_t4_vram_headroom():
    """Hardware-aware autotuner scales workers/replicas using >10GB T4 VRAM headroom."""
    smi_t4 = [
        {"gpu_index": 0, "name": "Tesla T4", "vram_total_mb": 15360.0, "vram_used_mb": 200.0},
        {"gpu_index": 1, "name": "Tesla T4", "vram_total_mb": 15360.0, "vram_used_mb": 200.0},
    ]
    probe = build_hardware_autotune_probe(smi_t4, encoder="h264_nvenc", cpu_count=4)
    tuner = StartupAutotuner(total_vram_mb=15360.0)
    frozen = tuner.autotune(probe)

    assert frozen.frozen is True
    assert frozen.nvenc_workers_per_gpu >= 3
    assert frozen.tts_replicas_per_gpu >= 2
    assert frozen.best_peak_vram_mb <= tuner.usable_vram_budget_mb
