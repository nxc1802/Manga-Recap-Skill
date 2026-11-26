"""Media & CPU finalization parity tests for Phase K3 overlapped finalizer and render fast paths."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
import wave
from pathlib import Path

import pytest
from PIL import Image

from src.cloud import dual_gpu_worker as worker
from src.cloud.scheduler import ExecutionPolicy
from src.core.human_review import approve_preview, review_fingerprint
from src.core.storage import atomic_write_json, calculate_sha256, load_manifest, load_recap_script, read_json
from src.core.visual_contract import VisualPlan
from src.render.fast_renderer import (
    build_shot_render_tasks,
    concat_shot_clips,
    frame_schedule,
    prepare_shot_watermark,
    render_shot_clip,
)
from src.render.subtitle import SubtitleCue
from src.render.visual_policy import assemble_visual_plan, iter_segment_shots
from tests.workflow.test_canonical_workflow import unit_project  # noqa: F401


def _probe_streams(video_path: Path) -> dict:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "stream=index,codec_type,codec_name,nb_frames,width,height",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(video_path),
    ]
    out = subprocess.check_output(cmd, text=True)
    return json.loads(out)


def _setup_two_ready_chapters(unit_project):
    project, ch1 = unit_project
    cfg = read_json(project / "project.json")
    rs = cfg["render_settings"]
    m1 = load_manifest(ch1)
    s1 = load_recap_script(ch1)
    if not (ch1 / "visual_plan.json").exists():
        shots1 = [
            sh
            for grp in iter_segment_shots(ch1, m1, s1, rs["target_width"], rs["target_height"], False)
            for sh in grp
        ]
        assemble_visual_plan(
            m1, s1, shots1,
            target_width=rs["target_width"], target_height=rs["target_height"],
            fps=rs["fps"], motion_enabled=False,
        ).save(ch1 / "visual_plan.json")

    (ch1 / "video").mkdir(parents=True, exist_ok=True)
    (ch1 / "subtitles").mkdir(parents=True, exist_ok=True)
    (ch1 / "video" / "preview.mp4").write_bytes(b"approved preview bytes")
    (ch1 / "subtitles" / "preview.srt").write_text("1\n00:00:00,000 --> 00:00:00,500\nok\n", encoding="utf-8")
    p_hash = calculate_sha256(ch1 / "video" / "preview.mp4")
    approve_preview(project, "tester", review_fingerprint(project), p_hash)

    for num in ("2", "3"):
        ch = project / "chapters" / num
        shutil.copytree(ch1, ch)
        (ch / "visual_plan.json").unlink(missing_ok=True)
        script = load_recap_script(ch)
        wav = ch / "audio" / "clips" / f"audio_{num}_s1.wav"
        wav.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(48000)
            w.writeframes(b"\x00\x00" * 24000)
        seg = script.segments[0].model_copy(
            update={
                "seq_id": f"c{num}_s1",
                "status": "synthesized",
                "duration": 0.5,
                "checksum": calculate_sha256(wav),
                "audio_path": wav.relative_to(ch).as_posix(),
            }
        )
        script.segments = [seg]
        script.total_duration_seconds = 0.5
        atomic_write_json(ch / "script.json", script)
        assemble_visual_plan(
            load_manifest(ch), script,
            [
                sh
                for grp in iter_segment_shots(ch, load_manifest(ch), script, rs["target_width"], rs["target_height"], False)
                for sh in grp
            ],
            target_width=rs["target_width"], target_height=rs["target_height"],
            fps=rs["fps"], motion_enabled=False,
        ).save(ch / "visual_plan.json")

    return project


def test_async_finalizer_overlaps_cleans_verified_temps_and_preserves_failure_evidence(unit_project, tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg required")
    project = _setup_two_ready_chapters(unit_project)
    config = read_json(project / "project.json")
    rs = config["render_settings"]
    recorder = worker.TelemetryRecorder(project_slug="unit", chapters=["2", "3"])
    work_root = tmp_path / "work"
    work_root.mkdir()

    # Render real shot clips into work/2 and work/3
    clip_map = {}
    for num in ("2", "3"):
        ch = project / "chapters" / num
        ch_work = work_root / num
        ch_work.mkdir(parents=True)
        plan = VisualPlan.load(ch / "visual_plan.json")
        tasks = build_shot_render_tasks(
            ch, plan, load_manifest(ch), work_dir=ch_work, encoder="libx264", quality_mode="final"
        )
        clip_map[num] = [render_shot_clip(t, canvas_cache={}, gpu_index=0) for t in tasks]

    # Submit out-of-order (3 before 2) cleanly without blocking caller
    finalizer = worker._AsyncChapterFinalizer(project, config, recorder, max_workers=1, min_free_disk_mb=1)
    t_submit = time.monotonic()
    finalizer.submit("3", work_root / "3", clip_map["3"], "final")
    finalizer.submit("2", work_root / "2", clip_map["2"], "final")
    submit_elapsed = time.monotonic() - t_submit
    assert submit_elapsed < 1.0  # Non-blocking submit
    done = finalizer.wait_ordered(canonical_order=["2", "3"])
    finalizer.close()
    assert done == {"2", "3"}

    # After verified finalize commit, temporary shot directories were pruned AND outputs are non-empty
    for num in ("2", "3"):
        assert not (work_root / num).exists()
        mp4 = project / "chapters" / num / "video" / "final.mp4"
        srt = project / "chapters" / num / "subtitles" / "final.srt"
        assert mp4.stat().st_size > 0
        assert srt.stat().st_size > 0
        probe = _probe_streams(mp4)
        types = {s["codec_type"] for s in probe["streams"]}
        assert {"video", "audio"}.issubset(types)

    # Failure safety: if _finish_chapter raises, temp scratch directory MUST NOT be deleted
    bad_work = work_root / "bad_99"
    bad_work.mkdir(parents=True)
    marker = bad_work / "evidence_clip.mp4"
    marker.write_bytes(b"keep me")
    bad_fin = worker._AsyncChapterFinalizer(project, config, recorder, max_workers=1, min_free_disk_mb=1)
    bad_fin.submit("99", bad_work, [marker], "final")
    with pytest.raises(Exception):
        bad_fin.wait_ordered(["99"])
    bad_fin.close()
    assert marker.exists()


def test_fast_path_static_and_pan_shots_with_boundary_cues_ffprobe_parity(unit_project, tmp_path):
    from src.core.visual_contract import ProductionMotion, Region, Shot

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg/FFprobe required")
    project, ch1 = unit_project
    tall_panel = ch1 / "panels" / "p_tall.png"
    Image.new("RGB", (640, 1280), color=(40, 120, 200)).save(tall_panel)
    work = tmp_path / "fast_work"
    work.mkdir()

    base_manifest = load_manifest(ch1)
    tall_entry = base_manifest.panels[0].model_copy(
        update={
            "panel_id": "p_tall",
            "filename": "p_tall.png",
            "relative_path": "panels/p_tall.png",
            "width": 640,
            "height": 1280,
            "aspect_ratio": 0.5,
            "sha256": calculate_sha256(tall_panel),
        }
    )
    manifest = base_manifest.model_copy(update={"panels": [tall_entry], "total_panels": 1})
    plan = VisualPlan(
        chapter_number="1",
        target_width=640,
        target_height=360,
        fps=12,
        total_duration_sec=2.0,
        shots=[
            Shot(
                shot_id="sh_0",
                segment_id="s1",
                source_panel_id="p_tall",
                source_box=Region(x_min=0.0, y_min=0.0, x_max=1.0, y_max=0.4),
                motion_type=ProductionMotion.STATIC,
                duration_sec=1.0,
            ),
            Shot(
                shot_id="sh_1",
                segment_id="s1",
                source_panel_id="p_tall",
                source_box=Region(x_min=0.0, y_min=0.0, x_max=1.0, y_max=1.0),
                motion_type=ProductionMotion.SLOW_PAN_DOWN,
                scroll_fraction_per_second=0.10,
                duration_sec=1.0,
            ),
        ],
    )
    cues = [
        SubtitleCue(index=1, start_seconds=0.0, end_seconds=1.1, text="Dòng một qua biên"),
        SubtitleCue(index=2, start_seconds=1.1, end_seconds=2.0, text="Dòng hai ổn định"),
    ]
    wm_path, wm_xy = prepare_shot_watermark(ch1, work, 640, 360)
    tasks = build_shot_render_tasks(
        ch1,
        plan,
        manifest,
        work_dir=work,
        encoder="libx264",
        subtitle_cues=cues,
        burn_subtitles=True,
        subtitle_font_size=24,
        watermark_png_path=wm_path,
        watermark_xy=wm_xy,
    )
    canvas_cache = {}
    clips = [render_shot_clip(t, canvas_cache=canvas_cache, gpu_index=0) for t in tasks]
    assert len(canvas_cache) >= 1

    wav = work / "audio.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(b"\x00\x00" * 96000)

    out_mp4 = tmp_path / "final_mux.mp4"
    concat_shot_clips(clips, out_mp4, work_dir=work, audio_path=wav)
    probe = _probe_streams(out_mp4)
    v_stream = next(s for s in probe["streams"] if s["codec_type"] == "video")
    a_stream = next(s for s in probe["streams"] if s["codec_type"] == "audio")
    expected_frames = sum(frames for _, frames in frame_schedule(plan))
    assert v_stream["codec_name"] == "h264"
    assert int(v_stream["width"]) == 640 and int(v_stream["height"]) == 360
    assert int(v_stream["nb_frames"]) == expected_frames
    assert a_stream["codec_name"] == "aac"
    assert 1.8 <= float(probe["format"]["duration"]) <= 2.2
