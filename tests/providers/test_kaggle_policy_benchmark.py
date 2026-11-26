"""Offline comparative Dual-T4 policy benchmark tests (mocked GPUs + overlap-aware telemetry)."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.cloud import dual_gpu_worker as worker
from src.cloud.benchmark import (
    COMPARATIVE_POLICIES,
    DEFAULT_WORKLOAD_PROFILES,
    WorkloadProfile,
    benchmark_policies_simulated,
    simulate_policy_timeline,
)
from src.cloud.scheduler import ExecutionPolicy
from src.core.human_review import approve_preview, review_fingerprint
from src.core.storage import atomic_write_json, calculate_sha256, load_manifest, load_recap_script, read_json
from src.render.visual_policy import assemble_visual_plan, iter_segment_shots
from tests.workflow.test_canonical_workflow import unit_project  # noqa: F401


def test_offline_policy_benchmark_compares_a_through_d_with_honest_provenance():
    report = benchmark_policies_simulated(repeats=3)
    assert report["environment"] == "offline_mocked_dual_t4"
    assert report["live_kaggle_verified"] is False
    assert report["recommended_safe_rollback_policy"] == ExecutionPolicy.BASELINE_SPLIT

    for name in ("short_mixed", "tts_heavy", "render_heavy"):
        entry = report["workloads"][name]
        assert set(entry["results"].keys()) == set(COMPARATIVE_POLICIES)
        assert entry["speedup_vs_baseline_a"] >= 1.0
        for pol, stats in entry["results"].items():
            assert stats["min_makespan_sec"] <= stats["median_makespan_sec"] <= stats["max_makespan_sec"]
            assert stats["spread_sec"] >= 0.0
            assert stats["median_useful_chapters_per_session_hour"] > 0.0

    # On TTS-heavy workload, dual-GPU TTS policies B/C cut TTS wall time substantially vs single-GPU TTS A/D.
    tts_res = report["workloads"]["tts_heavy"]["results"]
    assert tts_res[ExecutionPolicy.DUAL_TTS_THEN_DUAL_ENCODE]["median_tts_union_sec"] < (
        tts_res[ExecutionPolicy.BASELINE_SPLIT]["median_tts_union_sec"] * 0.7
    )
    assert report["workloads"]["tts_heavy"]["fastest_policy"] in {
        ExecutionPolicy.DUAL_TTS_SHARED_ENCODE,
        ExecutionPolicy.DUAL_TTS_THEN_DUAL_ENCODE,
    }


def test_real_coordinator_bench_across_policies_a_b_c_d(unit_project, monkeypatch):
    import shutil
    import wave

    project, ch1 = unit_project
    cfg = read_json(project / "project.json")
    rs = cfg["render_settings"]
    m1 = load_manifest(ch1)
    s1 = load_recap_script(ch1)
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
    (ch1 / "video" / "preview.mp4").write_bytes(b"ch1 preview parity")
    (ch1 / "subtitles" / "preview.srt").write_text("1\n00:00:00,000 --> 00:00:00,500\nok\n", encoding="utf-8")
    p_hash = calculate_sha256(ch1 / "video" / "preview.mp4")
    approve_preview(project, "tester", review_fingerprint(project), p_hash)

    for num in ("2", "3"):
        ch = project / "chapters" / num
        shutil.copytree(ch1, ch)
        (ch / "visual_plan.json").unlink(missing_ok=True)
        m_data = read_json(ch / "manifest.json")
        m_data["chapter_number"] = num
        atomic_write_json(ch / "manifest.json", m_data)
        c_data = read_json(ch / "chapter.json")
        c_data["chapter_number"] = num
        atomic_write_json(ch / "chapter.json", c_data)

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

    class _TimedTTS:
        def __init__(self, gpu_id):
            self.gpu_id = gpu_id

        def synthesize_batch_tasks(self, tasks, batch_size=12, cpu_workers=2):
            time.sleep(0.015 * len(tasks))
            out = []
            for t in tasks:
                p = Path(t["output_path"])
                p.parent.mkdir(parents=True, exist_ok=True)
                with wave.open(str(p), "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(48000)
                    w.writeframes(b"\x00\x00" * 24000)
                out.append({
                    "chapter_number": t.get("chapter_number"),
                    "seq_id": t["seq_id"],
                    "duration": 0.5,
                    "sha256": calculate_sha256(p),
                    "effective_batch_size": len(tasks),
                })
            return out

        def unload(self):
            time.sleep(0.005)

    def _timed_encode(task, canvas_cache, gpu_index):
        time.sleep(0.015)
        p = Path(task["output_clip_path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"clip")
        return p

    monkeypatch.setattr(worker.mp, "get_context", lambda *_: _ThreadCtx())
    monkeypatch.setattr("src.tts.cuda_worker.VieNeuCUDAWorker", _TimedTTS)
    monkeypatch.setattr(worker, "render_shot_clip", _timed_encode)
    monkeypatch.setattr(
        worker,
        "_finish_chapter",
        lambda proj, num, w, c, cfg, q: (
            (proj / "chapters" / num / "video").mkdir(parents=True, exist_ok=True),
            (proj / "chapters" / num / "subtitles").mkdir(parents=True, exist_ok=True),
            (proj / "chapters" / num / "video" / f"{q}.mp4").write_bytes(b"mp4"),
            (proj / "chapters" / num / "subtitles" / f"{q}.srt").write_text("1\n00:00:00,000 --> 00:00:00,500\nok\n", encoding="utf-8"),
        ),
    )
    monkeypatch.setattr(
        "src.render.chapter_renderer.render_chapter",
        lambda proj, num, quality="final", encoder="h264_nvenc": (
            (proj / "chapters" / str(num) / "video" / "final.mp4").write_bytes(
                (proj / "chapters" / str(num) / "video" / "preview.mp4").read_bytes()
            ),
            (proj / "chapters" / str(num) / "subtitles" / "final.srt").write_text(
                (proj / "chapters" / str(num) / "subtitles" / "preview.srt").read_text(encoding="utf-8"),
                encoding="utf-8",
            ),
        ),
    )

    measured = {}
    for pol in COMPARATIVE_POLICIES:
        # Reset ch2 & ch3 segments to pending before each policy run
        for ch_num in ("2", "3"):
            ch = project / "chapters" / ch_num
            sc = load_recap_script(ch)
            sc = sc.model_copy(
                update={
                    "chapter_number": ch_num,
                    "segments": [
                        sc.segments[0].model_copy(
                            update={"seq_id": f"c{ch_num}_s{k}", "status": "pending", "duration": None, "checksum": None, "audio_path": None}
                        )
                        for k in range(3)
                    ],
                }
            )
            atomic_write_json(ch / "script.json", sc)
            (ch / "visual_plan.json").unlink(missing_ok=True)
        rec = worker.TelemetryRecorder(project_slug="unit", policy=pol, chapters=["1", "2", "3"])
        t0 = time.monotonic()
        worker.run_pipeline(
            project,
            ["1", "2", "3"],
            "produce",
            encoder="h264_nvenc",
            gpu_ids=(0, 1),
            render_workers=1,
            batch_size=2,
            single_preview=False,
            telemetry=rec,
            policy=pol,
            usable_nvenc_devices=(0, 1),
        )
        wall = time.monotonic() - t0
        rep = rec.build_report()
        stages = (rep.get("timing") or {}).get("stages") or {}
        measured[pol] = {
            "wall_sec": wall,
            "tts_union": float((stages.get("tts") or {}).get("wall_union_sec", 0.0)),
            "encode_union": float((stages.get("encode") or {}).get("wall_union_sec", 0.0)),
        }
        assert calculate_sha256(ch1 / "video" / "final.mp4") == p_hash
        for ch_num in ("2", "3"):
            assert (project / "chapters" / ch_num / "video" / "final.mp4").exists()

    assert len(measured) == 4
    assert all(m["tts_union"] > 0 and m["encode_union"] > 0 for m in measured.values())
