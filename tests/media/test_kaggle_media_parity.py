"""End-to-end real-FFmpeg multi-chapter media parity test across K2/K3/K4 cloud execution."""

from __future__ import annotations

import json
import shutil
import subprocess
import wave
from pathlib import Path

import pytest
from PIL import Image

from src.cloud import dual_gpu_worker as worker
from src.cloud.scheduler import ExecutionPolicy
from src.core.human_review import approve_preview, review_fingerprint
from src.core.storage import atomic_write_json, calculate_sha256, load_manifest, load_recap_script, read_json
from src.core.visual_contract import VisualPlan
from src.render.fast_renderer import frame_schedule
from src.render.visual_policy import assemble_visual_plan, iter_segment_shots
from tests.workflow.test_canonical_workflow import unit_project  # noqa: F401


def _ffprobe(path: Path) -> dict:
    return json.loads(
        subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type,codec_name,width,height,nb_frames",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                str(path),
            ],
            text=True,
        )
    )


def _write_tone_wav(path: Path, seconds: float = 1.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    n_samples = int(48000 * seconds)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(b"\x10\x00" * n_samples)


def test_multichapter_cloud_produce_media_parity_with_bgm_watermark_and_subtitles(unit_project, monkeypatch):
    from src.core.series_setup import record_series_setup

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg and FFprobe required")

    project, ch1 = unit_project
    # Configure BGM, watermark, burned subtitles, and tall animated-ready panels cleanly via series setup
    wm_dir = project.parent.parent / "watermarks"
    wm_dir.mkdir(exist_ok=True)
    Image.new("RGBA", (64, 24), (255, 255, 255, 180)).save(wm_dir / "default.png")
    bgm_wav = project / "bgm.wav"
    _write_tone_wav(bgm_wav, seconds=3.0)

    saved_setup = read_json(project / "series_setup_answers.json")["answers"]
    saved_setup["Q05"]["user_answer"] = True
    saved_setup["Q10"]["user_answer"] = {"bgm_path": "bgm.wav", "bgm_volume": 0.2, "voice_volume": 1.0}
    record_series_setup("unit", saved_setup, project_dir=project)

    proj_cfg = read_json(project / "project.json")
    rs = proj_cfg["render_settings"]
    m1 = load_manifest(ch1)
    s1 = load_recap_script(ch1)
    # Replace Ch1 preview with a genuine tiny FFmpeg video + SRT before Gate G6 approval
    shots1 = [
        sh
        for grp in iter_segment_shots(ch1, m1, s1, rs["target_width"], rs["target_height"], True)
        for sh in grp
    ]
    assemble_visual_plan(
        m1, s1, shots1,
        target_width=rs["target_width"], target_height=rs["target_height"],
        fps=rs["fps"], motion_enabled=True,
    ).save(ch1 / "visual_plan.json")
    (ch1 / "video").mkdir(parents=True, exist_ok=True)
    (ch1 / "subtitles").mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", f"color=c=black:s={rs['target_width']}x{rs['target_height']}:d=0.5:r={rs['fps']}",
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-t", "0.5",
            "-c:v", "libx264", "-c:a", "aac",
            str(ch1 / "video" / "preview.mp4"),
        ],
        check=True,
    )
    (ch1 / "subtitles" / "preview.srt").write_text("1\n00:00:00,000 --> 00:00:00,500\nok\n", encoding="utf-8")
    p_hash = calculate_sha256(ch1 / "video" / "preview.mp4")
    approve_preview(project, "reviewer", review_fingerprint(project), p_hash)

    # Create Chapters 2 (mixed length + tall pan panel) & 3 (static panel)
    for ch_num, seg_count in (("2", 2), ("3", 1)):
        ch = project / "chapters" / ch_num
        shutil.copytree(ch1, ch)
        (ch / "visual_plan.json").unlink(missing_ok=True)
        tall_panel = ch / "panels" / "panel_tall.png"
        tall_img = Image.new("RGB", (rs["target_width"], rs["target_height"] * 2), color=(40, 80, 140))
        from PIL import ImageDraw

        draw = ImageDraw.Draw(tall_img)
        for band_y in range(0, rs["target_height"] * 2, 48):
            draw.rectangle([20, band_y, rs["target_width"] - 20, band_y + 24], fill=(220, 230, 240))
        tall_img.save(tall_panel)
        m_data = read_json(ch / "manifest.json")
        m_data["chapter_number"] = ch_num
        p_template = dict(m_data["panels"][0])
        if ch_num == "2":
            p_template.update({
                "panel_id": "p_tall",
                "filename": "panel_tall.png",
                "relative_path": "panels/panel_tall.png",
                "width": rs["target_width"],
                "height": rs["target_height"] * 2,
                "aspect_ratio": 0.5,
                "sha256": calculate_sha256(tall_panel),
            })
            m_data["panels"][0] = p_template
        atomic_write_json(ch / "manifest.json", m_data)

        c_data = read_json(ch / "chapter.json")
        c_data["chapter_number"] = ch_num
        atomic_write_json(ch / "chapter.json", c_data)

        sc = load_recap_script(ch)
        s0 = sc.segments[0]
        panel_ref = "p_tall" if ch_num == "2" else m_data["panels"][0]["panel_id"]
        new_segs = []
        for idx in range(seg_count):
            new_segs.append(
                s0.model_copy(
                    update={
                        "seq_id": f"c{ch_num}_s{idx}",
                        "narrator_text": f"Lời bình chương {ch_num} đoạn {idx + 1}.",
                        "panel_ids": [panel_ref],
                        "evidence_panel_ids": [panel_ref],
                        "status": "pending",
                        "duration": None,
                        "checksum": None,
                        "audio_path": None,
                    }
                )
            )
        sc = sc.model_copy(update={"chapter_number": ch_num, "segments": new_segs})
        atomic_write_json(ch / "script.json", sc)

    class _RealWavFakeTTS:
        def __init__(self, gpu_id):
            self.gpu_id = gpu_id

        def synthesize_batch_tasks(self, tasks, batch_size=12, cpu_workers=2):
            out = []
            for t in tasks:
                p = Path(t["output_path"])
                _write_tone_wav(p, seconds=0.75)
                out.append({
                    "chapter_number": t.get("chapter_number"),
                    "seq_id": t["seq_id"],
                    "duration": 0.75,
                    "sha256": calculate_sha256(p),
                    "effective_batch_size": len(tasks),
                })
            return out

        def unload(self):
            pass

    import threading

    class _ThreadCtx:
        def Queue(self, maxsize=0):
            import queue

            class _ClosableQ(queue.Queue):
                def close(self):
                    pass

            return _ClosableQ(maxsize=maxsize)

        def Process(self, target=None, args=()):
            class _Proc:
                def __init__(self):
                    self.exitcode = None
                    self._t = threading.Thread(target=self._run, daemon=True)

                def _run(self):
                    try:
                        target(*args)
                        self.exitcode = 0
                    except Exception:
                        import traceback

                        traceback.print_exc()
                        self.exitcode = 1

                def start(self):
                    self._t.start()

                def is_alive(self):
                    return self._t.is_alive()

                def join(self, timeout=None):
                    self._t.join(timeout=timeout)

                def terminate(self):
                    pass

                def kill(self):
                    pass

            return _Proc()

    monkeypatch.setattr(worker.mp, "get_context", lambda *_: _ThreadCtx())
    monkeypatch.setattr("src.tts.cuda_worker.VieNeuCUDAWorker", _RealWavFakeTTS)

    telemetry = worker.TelemetryRecorder(project_slug="unit", chapters=["1", "2", "3"])
    worker.run_pipeline(
        project,
        ["1", "2", "3"],
        "produce",
        encoder="libx264",
        gpu_ids=(0,),
        render_workers=1,
        batch_size=4,
        single_preview=False,
        telemetry=telemetry,
        policy=ExecutionPolicy.DUAL_TTS_THEN_DUAL_ENCODE,
    )

    # Verify Chapter 1 preview -> final byte preservation
    assert calculate_sha256(ch1 / "video" / "final.mp4") == p_hash
    # Verify Chapters 2 & 3 real FFmpeg outputs against ffprobe and frame_schedule
    for ch_num, expected_segs in (("2", 2), ("3", 1)):
        ch_dir = project / "chapters" / ch_num
        mp4 = ch_dir / "video" / "final.mp4"
        srt = ch_dir / "subtitles" / "final.srt"
        assert mp4.stat().st_size > 0
        assert srt.stat().st_size > 0
        plan = VisualPlan.load(ch_dir / "visual_plan.json")
        expected_frames = sum(f for _, f in frame_schedule(plan))
        probe = _ffprobe(mp4)
        v_s = next(s for s in probe["streams"] if s["codec_type"] == "video")
        a_s = next(s for s in probe["streams"] if s["codec_type"] == "audio")
        assert v_s["codec_name"] == "h264"
        assert int(v_s["width"]) == rs["target_width"]
        assert int(v_s["height"]) == rs["target_height"]
        assert int(v_s["nb_frames"]) == expected_frames
        assert a_s["codec_name"] == "aac"
        sc = load_recap_script(ch_dir)
        assert abs(sc.total_duration_seconds - expected_segs * 0.75) < 1e-6
