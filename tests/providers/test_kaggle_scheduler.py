"""Tests for K2 execution policies, CUDA/NVENC device isolation, and cost-aware TTS scheduler."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.cloud import dual_gpu_worker as worker
from src.cloud.scheduler import (
    MAX_CLOUD_TTS_BATCH_SIZE,
    SUPPORTED_BATCH_CAPS,
    ExecutionPolicy,
    UtteranceDispatcher,
    UtteranceItem,
    default_cloud_batch_size,
    estimate_utterance_cost,
    normalize_policy,
    pack_tts_batches,
    resolve_execution_policy,
)
from src.core.storage import atomic_write_json, calculate_sha256, load_recap_script, read_json
from src.core.visual_contract import VisualPlan
from src.render.fast_renderer import frame_schedule
from tests.workflow.test_canonical_workflow import unit_project  # noqa: F401


def test_policy_normalization_and_device_isolation():
    for alias, canonical in [
        ("A", ExecutionPolicy.BASELINE_SPLIT),
        ("policy_a", ExecutionPolicy.BASELINE_SPLIT),
        ("B", ExecutionPolicy.DUAL_TTS_SHARED_ENCODE),
        ("dual_tts_shared_encode", ExecutionPolicy.DUAL_TTS_SHARED_ENCODE),
        ("auto", ExecutionPolicy.AUTO),
    ]:
        assert normalize_policy(alias) == canonical

    with pytest.raises(ValueError, match="Unsupported cloud execution policy"):
        normalize_policy("unknown_policy")

    # Render-only skips TTS devices and scales across all usable NVENC GPUs
    r_plan = resolve_execution_policy("auto", job="render", encoder="h264_nvenc", cuda_devices=[0, 1], usable_nvenc_devices=[0, 1])
    assert r_plan.tts_devices == ()
    assert r_plan.encode_devices == (0, 1)

    # TTS-only auto uses both CUDA GPUs
    t_plan = resolve_execution_policy("auto", job="tts", encoder="h264_nvenc", cuda_devices=[0, 1], usable_nvenc_devices=[])
    assert t_plan.tts_devices == (0, 1)
    assert t_plan.encode_devices == ()

    # NVENC failure on GPU 0 isolates encoding to GPU 1 WITHOUT dropping healthy CUDA TTS device 0 or 1
    mixed_plan = resolve_execution_policy(
        "dual_tts_shared_encode",
        job="produce",
        encoder="h264_nvenc",
        cuda_devices=[0, 1],
        usable_nvenc_devices=[1],
    )
    assert mixed_plan.tts_devices == (0, 1)
    assert mixed_plan.encode_devices == (1,)
    assert mixed_plan.encoder == "h264_nvenc"

    # Full produce `auto` selects Policy B (dual_tts_shared_encode) on verified Dual-GPU + Dual-NVENC,
    # while single-GPU or degraded single-NVENC cleanly falls back to baseline_split.
    auto_dual = resolve_execution_policy(
        "auto", job="produce", encoder="h264_nvenc", cuda_devices=[0, 1], usable_nvenc_devices=[0, 1]
    )
    assert auto_dual.policy == ExecutionPolicy.DUAL_TTS_SHARED_ENCODE
    assert auto_dual.tts_devices == (0, 1)
    assert auto_dual.encode_devices == (0, 1)

    auto_degraded = resolve_execution_policy(
        "auto", job="produce", encoder="h264_nvenc", cuda_devices=[0, 1], usable_nvenc_devices=[0]
    )
    assert auto_degraded.policy == ExecutionPolicy.BASELINE_SPLIT


def test_cost_aware_dispatcher_composite_keys_batching_and_oom_backoff(tmp_path):
    assert estimate_utterance_cost("Ngắn.") < estimate_utterance_cost("Đây là một câu dài hơn nhiều, có dấu phẩy, và dấu chấm.")

    items = [
        # Same seq_id "s1" across chapters 1 and 2 must remain strictly distinct!
        UtteranceItem(0, "1", 0, "s1", "Ngắn một.", "huyen", 1.0, None, None, tmp_path / "1_s1.wav", "fp1"),
        UtteranceItem(1, "2", 0, "s1", "Ngắn hai.", "huyen", 1.0, None, None, tmp_path / "2_s1.wav", "fp2"),
        # Long utterance (>250 chars -> bucket 3, capped separately)
        UtteranceItem(1, "2", 1, "s2", "A" * 280, "huyen", 1.0, None, None, tmp_path / "2_s2.wav", "fp3"),
        # Different voice must never mix into same micro-batch
        UtteranceItem(1, "2", 2, "s3", "Giọng nam.", "nam", 1.0, None, None, tmp_path / "2_s3.wav", "fp4"),
    ]
    batches = pack_tts_batches(items, max_batch_size=12)
    assert len(batches) == 3
    assert [u.key for u in batches[0]] == [("1", "s1"), ("2", "s1")]

    disp = UtteranceDispatcher(max_batch_size=12, long_utterance_cap=4)
    disp.enqueue_many(items)
    assert disp.pending_count == 4
    with pytest.raises(ValueError, match="Duplicate utterance key"):
        disp.enqueue(items[0])

    # Adaptive per-device OOM cap lowers batch cap ONLY on device 0
    for idx in range(6):
        disp.enqueue(
            UtteranceItem(2, "3", idx, f"x{idx}", f"Câu {idx}", "huyen", 1.0, None, None, tmp_path / f"3_{idx}.wav", f"fpx{idx}")
        )
    disp.record_oom_backoff(0, 2)
    b0_id, b0 = disp.next_batch(device_id=0)
    assert len(b0) == 2
    assert disp.effective_cap_for_device(1) == 12
    disp.mark_completed(u.key for u in b0)

    # Different batch caps [4, 8, 12, 16, 24, 32] work cleanly
    for cap in SUPPORTED_BATCH_CAPS:
        sub = [
            UtteranceItem(0, "1", i, f"c{i}", "Ngắn.", "huyen", 1.0, None, None, tmp_path / f"c_{i}.wav", f"fp{i}")
            for i in range(40)
        ]
        packed = pack_tts_batches(sub, max_batch_size=cap)
        assert all(len(group) <= cap for group in packed)
        assert len(packed[0]) == min(cap, 40)


def _setup_two_chapter_project(unit_project):
    import shutil
    from src.core.human_review import approve_preview, review_fingerprint
    from src.render.visual_policy import assemble_visual_plan, iter_segment_shots

    project, ch1 = unit_project
    cfg = read_json(project / "project.json")
    rs = cfg["render_settings"]
    manifest1 = read_json(ch1 / "manifest.json")
    script1 = load_recap_script(ch1)
    # Build minimal valid visual_plan.json for Chapter 1 so review_fingerprint validates cleanly
    if not (ch1 / "visual_plan.json").exists():
        from src.core.storage import load_manifest
        m1 = load_manifest(ch1)
        shots = [
            sh
            for grp in iter_segment_shots(ch1, m1, script1, rs["target_width"], rs["target_height"], False)
            for sh in grp
        ]
        plan1 = assemble_visual_plan(
            m1, script1, shots,
            target_width=rs["target_width"], target_height=rs["target_height"],
            fps=rs["fps"], motion_enabled=False,
        )
        plan1.save(ch1 / "visual_plan.json")

    # Approve Chapter 1 preview cleanly so Chapters 2 & 3 pass Gate G6
    (ch1 / "video").mkdir(parents=True, exist_ok=True)
    (ch1 / "subtitles").mkdir(parents=True, exist_ok=True)
    (ch1 / "video" / "preview.mp4").write_bytes(b"approved preview video bytes")
    (ch1 / "subtitles" / "preview.srt").write_text("1\n00:00:00,000 --> 00:00:00,500\nok\n", encoding="utf-8")
    p_hash = calculate_sha256(ch1 / "video" / "preview.mp4")
    approve_preview(project, "tester", review_fingerprint(project), p_hash)

    for num in ("2", "3"):
        ch = project / "chapters" / num
        shutil.copytree(ch1, ch)
        (ch / "visual_plan.json").unlink(missing_ok=True)
        script = load_recap_script(ch)
        # Identical seq_ids ("s1", "s2") intentionally collide across chapters if not keyed by (chapter, seq_id)
        s0 = script.segments[0]
        script.segments = [
            s0.model_copy(update={"seq_id": "s1", "status": "pending", "duration": None, "checksum": None, "audio_path": None}),
            s0.model_copy(update={"seq_id": "s2", "status": "pending", "duration": None, "checksum": None, "audio_path": None}),
        ]
        atomic_write_json(ch / "script.json", script)

    return project, p_hash


class _DummyCtx:
    """Thread-backed stand-in for mp.get_context('spawn') so monkeypatched fakes run in-process across Policies B/C/D."""

    def Queue(self, maxsize=0):
        import queue

        class _ClosableQueue(queue.Queue):
            def close(self):
                pass

        return _ClosableQueue(maxsize=maxsize)

    def Process(self, target=None, args=()):
        class _ThreadProc:
            def __init__(self):
                self._exc = None
                self.exitcode = None
                self._thread = threading.Thread(target=self._run, daemon=True)

            def _run(self):
                try:
                    target(*args)
                    self.exitcode = 0
                except Exception as exc:
                    self._exc = exc
                    self.exitcode = 1

            def start(self):
                self._thread.start()

            def is_alive(self):
                return self._thread.is_alive()

            def join(self, timeout=None):
                self._thread.join(timeout=timeout)

            def terminate(self):
                pass

            def kill(self):
                pass

        return _ThreadProc()


@pytest.mark.parametrize(
    "policy_name",
    [
        ExecutionPolicy.DUAL_TTS_SHARED_ENCODE,
        ExecutionPolicy.BASELINE_SPLIT,
    ],
)
def test_policies_b_and_a_preserve_ch1_and_chapter_order(unit_project, monkeypatch, policy_name):
    project, ch1_preview_hash = _setup_two_chapter_project(unit_project)
    tts_calls = []
    encode_calls = []
    unloaded_gpus = []
    lock = threading.Lock()

    class FakeModel:
        def __init__(self, gpu_id):
            self.gpu_id = gpu_id

        def synthesize_batch_tasks(self, tasks, batch_size=12, cpu_workers=2):
            import wave

            out = []
            for task in tasks:
                p = Path(task["output_path"])
                p.parent.mkdir(parents=True, exist_ok=True)
                with wave.open(str(p), "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(48000)
                    w.writeframes(b"\x00\x00" * 24000)
                with lock:
                    tts_calls.append((self.gpu_id, task.get("chapter_number"), task["seq_id"]))
                out.append({
                    "chapter_number": task.get("chapter_number"),
                    "seq_id": task["seq_id"],
                    "duration": 0.5,
                    "sha256": calculate_sha256(p),
                    "effective_batch_size": len(tasks),
                })
            return out

        def unload(self):
            with lock:
                unloaded_gpus.append(self.gpu_id)

    def fake_render_shot(task, canvas_cache, gpu_index):
        p = Path(task["output_clip_path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"clip")
        with lock:
            encode_calls.append((gpu_index, p.parent.parent.name, task["shot_index"], task["frame_count"]))
        return p

    def fake_finish(proj, number, work_dir, clips, cfg, quality):
        chap = proj / "chapters" / number
        (chap / "video").mkdir(parents=True, exist_ok=True)
        (chap / "subtitles").mkdir(parents=True, exist_ok=True)
        (chap / "video" / f"{quality}.mp4").write_bytes(b"final video")
        (chap / "subtitles" / f"{quality}.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n", encoding="utf-8")

    monkeypatch.setattr(worker.mp, "get_context", lambda *_: _DummyCtx())
    monkeypatch.setattr("src.tts.cuda_worker.VieNeuCUDAWorker", FakeModel)
    monkeypatch.setattr(worker, "render_shot_clip", fake_render_shot)
    monkeypatch.setattr(worker, "_finish_chapter", fake_finish)
    # Also ensure Chapter 1 render_chapter promotion creates exact final.mp4 without real ffmpeg
    def fake_promote(proj, num, quality="final", encoder="h264_nvenc"):
        chap = proj / "chapters" / str(num)
        (chap / "video" / "final.mp4").write_bytes((chap / "video" / "preview.mp4").read_bytes())
        (chap / "subtitles" / "final.srt").write_text((chap / "subtitles" / "preview.srt").read_text(), encoding="utf-8")
        return {"video_path": str(chap / "video" / "final.mp4")}

    monkeypatch.setattr("src.render.chapter_renderer.render_chapter", fake_promote)

    telemetry = worker.TelemetryRecorder(project_slug="unit", chapters=["1", "2", "3"])
    worker.run_pipeline(
        project,
        ["1", "2", "3"],
        "produce",
        encoder="h264_nvenc",
        gpu_ids=(0, 1),
        render_workers=1,
        batch_size=4,
        single_preview=False,
        telemetry=telemetry,
        policy=policy_name,
        usable_nvenc_devices=(0, 1),
    )

    # Chapter 1 final video MUST match preview byte-for-byte
    assert calculate_sha256(project / "chapters" / "1" / "video" / "final.mp4") == ch1_preview_hash
    # Both Chapters 2 & 3 finished cleanly without seq_id collision
    for ch_num in ("2", "3"):
        ch_dir = project / "chapters" / ch_num
        assert (ch_dir / "video" / "final.mp4").exists()
        script = load_recap_script(ch_dir)
        assert [s.status for s in script.segments] == ["synthesized", "synthesized"]
        assert abs(script.total_duration_seconds - 1.0) < 1e-6
        plan = VisualPlan.load(ch_dir / "visual_plan.json")
        assert len(plan.shots) == 2

    if policy_name == ExecutionPolicy.DUAL_TTS_SHARED_ENCODE:
        assert set(unloaded_gpus) == {0, 1}
    else:
        assert unloaded_gpus == [0]

    report = telemetry.build_report()
    assert report["hardware"]["scheduler_policy"]["policy"] == policy_name


def test_tts_only_uses_dual_gpus_and_render_only_skips_tts(unit_project, monkeypatch):
    project, _ = _setup_two_chapter_project(unit_project)
    monkeypatch.setattr(worker.mp, "get_context", lambda *_: _DummyCtx())

    tts_gpus = set()

    class FakeTTS:
        def __init__(self, gpu_id):
            self.gpu_id = gpu_id

        def synthesize_batch_tasks(self, tasks, batch_size=12, cpu_workers=2):
            import wave

            tts_gpus.add(self.gpu_id)
            out = []
            for task in tasks:
                p = Path(task["output_path"])
                p.parent.mkdir(parents=True, exist_ok=True)
                with wave.open(str(p), "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(48000)
                    w.writeframes(b"\x00\x00" * 24000)
                out.append({
                    "chapter_number": task.get("chapter_number"),
                    "seq_id": task["seq_id"],
                    "duration": 0.5,
                    "sha256": calculate_sha256(p),
                    "effective_batch_size": len(tasks),
                })
            return out

        def unload(self):
            pass

    monkeypatch.setattr("src.tts.cuda_worker.VieNeuCUDAWorker", FakeTTS)
    # Run TTS-only across chapters 2 and 3 on 2 GPUs with small batch size so multiple batches distribute
    worker.run_pipeline(
        project,
        ["2", "3"],
        "tts",
        encoder="h264_nvenc",
        gpu_ids=(0, 1),
        batch_size=1,
        single_preview=False,
        policy="auto",
    )
    assert tts_gpus == {0, 1}
    for ch_num in ("2", "3"):
        s = load_recap_script(project / "chapters" / ch_num)
        assert all(seg.status == "synthesized" for seg in s.segments)

    # Render-only across both usable NVENC GPUs must never touch VieNeuCUDAWorker
    monkeypatch.setattr(
        "src.tts.cuda_worker.VieNeuCUDAWorker",
        lambda gpu_id: (_ for _ in ()).throw(AssertionError("Render-only must not initialize TTS")),
    )
    rendered_on = []

    def fake_enc(task, canvas_cache, gpu_index):
        p = Path(task["output_clip_path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"clip")
        rendered_on.append(gpu_index)
        return p

    monkeypatch.setattr(worker, "render_shot_clip", fake_enc)
    monkeypatch.setattr(
        worker,
        "_finish_chapter",
        lambda proj, num, w, c, cfg, q: (proj / "chapters" / num / "video" / f"{q}.mp4").write_bytes(b"done"),
    )

    worker.run_pipeline(
        project,
        ["2", "3"],
        "render",
        encoder="h264_nvenc",
        gpu_ids=(0, 1),
        render_workers=1,
        single_preview=False,
        policy="auto",
        usable_nvenc_devices=(0, 1),
    )
    assert set(rendered_on).issubset({0, 1})
    assert len(rendered_on) == 4


def test_authored_visual_plan_waits_for_full_tts_and_policy_a_baseline(unit_project, monkeypatch):
    from src.core.storage import load_manifest
    from src.render.visual_policy import assemble_visual_plan, iter_segment_shots

    project, _ = _setup_two_chapter_project(unit_project)
    ch2 = project / "chapters" / "2"
    cfg = read_json(project / "project.json")["render_settings"]
    m2 = load_manifest(ch2)
    s2 = load_recap_script(ch2)
    # Pre-author visual_plan.json on Chapter 2 with placeholder durations (matching schema >=0.1s)
    tmp_s2 = s2.model_copy(deep=True)
    for seg in tmp_s2.segments:
        seg.duration = 0.5
    shots2 = [
        sh
        for grp in iter_segment_shots(ch2, m2, tmp_s2, cfg["target_width"], cfg["target_height"], False)
        for sh in grp
    ]
    assemble_visual_plan(
        m2, tmp_s2, shots2,
        target_width=cfg["target_width"], target_height=cfg["target_height"],
        fps=cfg["fps"], motion_enabled=False,
    ).save(ch2 / "visual_plan.json")

    events_order = []

    class _SeqModel:
        def __init__(self, gpu_id):
            self.gpu_id = gpu_id

        def synthesize_batch_tasks(self, tasks, batch_size=12, cpu_workers=2):
            import wave

            out = []
            for task in tasks:
                p = Path(task["output_path"])
                p.parent.mkdir(parents=True, exist_ok=True)
                with wave.open(str(p), "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(48000)
                    w.writeframes(b"\x00\x00" * 24000)
                events_order.append(("tts", task.get("chapter_number"), task["seq_id"]))
                out.append({
                    "chapter_number": task.get("chapter_number"),
                    "seq_id": task["seq_id"],
                    "duration": 0.5,
                    "sha256": calculate_sha256(p),
                    "effective_batch_size": len(tasks),
                })
            return out

        def unload(self):
            pass

    def _rec_encode(task, canvas_cache, gpu_index):
        p = Path(task["output_clip_path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"clip")
        events_order.append(("enc", p.parent.parent.name, task["shot_index"]))
        return p

    monkeypatch.setattr(worker.mp, "get_context", lambda *_: _DummyCtx())
    monkeypatch.setattr("src.tts.cuda_worker.VieNeuCUDAWorker", _SeqModel)
    monkeypatch.setattr(worker, "render_shot_clip", _rec_encode)
    monkeypatch.setattr(
        worker,
        "_finish_chapter",
        lambda proj, num, w, c, cfg, q: (proj / "chapters" / num / "video" / f"{q}.mp4").write_bytes(b"done"),
    )

    worker.run_pipeline(
        project,
        ["2"],
        "produce",
        encoder="h264_nvenc",
        gpu_ids=(0, 1),
        render_workers=1,
        batch_size=1,
        single_preview=False,
        policy=ExecutionPolicy.DUAL_TTS_SHARED_ENCODE,
        usable_nvenc_devices=(0, 1),
    )
    ch2_tts = [i for i, e in enumerate(events_order) if e[0] == "tts"]
    ch2_enc = [i for i, e in enumerate(events_order) if e[0] == "enc"]
    # With authored VisualPlan present, NO Chapter 2 shot encodes before ALL Chapter 2 TTS segments complete!
    assert len(ch2_tts) == 2 and len(ch2_enc) == 2
    assert max(ch2_tts) < min(ch2_enc)


def test_default_cloud_batch_size_and_pipeline_override(unit_project, monkeypatch):
    from src.cloud.telemetry import TelemetryRecorder

    assert SUPPORTED_BATCH_CAPS == (4, 8, 12, 16, 24, 32)
    assert MAX_CLOUD_TTS_BATCH_SIZE == 32
    assert default_cloud_batch_size("produce") == 16
    assert default_cloud_batch_size("tts") == 32
    assert default_cloud_batch_size("render") == 16
    assert UtteranceDispatcher().max_batch_size == 16

    project, ch1 = unit_project
    recorded_batch_sizes = []
    monkeypatch.setattr(
        worker,
        "_run_scheduled_pipeline",
        lambda project, chapters, job, **kwargs: recorded_batch_sizes.append((job, kwargs["batch_size"])) or {"chapters": list(chapters)},
    )

    for job, override, expected in [
        ("produce", None, 16),
        ("tts", None, 32),
        ("produce", 24, 24),
    ]:
        rec = TelemetryRecorder(project_slug="unit", chapters=["1"])
        worker.run_pipeline(
            project,
            ["1"],
            job,
            encoder="h264_nvenc",
            gpu_ids=(0, 1),
            render_workers=2,
            batch_size=override,
            single_preview=True,
            telemetry=rec,
            policy="auto",
            usable_nvenc_devices=(0, 1),
        )
        assert rec._hardware["batch_size"] == expected
        assert recorded_batch_sizes[-1] == (job, expected)
