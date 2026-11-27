from pathlib import Path

import pytest

from src.cloud import dual_gpu_worker as worker
from src.core.storage import atomic_write_json
from tests.workflow.test_canonical_workflow import unit_project  # noqa: F401 — pytest fixture


def test_dual_gpu_preflight_rejects_single_gpu_and_cpu_encoder(unit_project):
    project, _ = unit_project
    with pytest.raises(RuntimeError, match="requires dual Tesla T4"):
        worker.run_pipeline(project, ["1"], "render", encoder="h264_nvenc", gpu_ids=(0,))
    with pytest.raises(RuntimeError, match="requires encoder='h264_nvenc'"):
        worker.run_pipeline(project, ["1"], "render", encoder="libx264", gpu_ids=(0, 1))
    with pytest.raises(RuntimeError, match="usable h264_nvenc on both GPUs"):
        worker.run_pipeline(
            project, ["1"], "produce", encoder="h264_nvenc", gpu_ids=(0, 1), usable_nvenc_devices=(0,)
        )


def _thread_mp_context():
    import queue
    import threading

    class _ThreadCtx:
        def Queue(self, maxsize=0):
            class _ClosableQueue(queue.Queue):
                def close(self):
                    pass

            return _ClosableQueue(maxsize=maxsize)

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

    return _ThreadCtx()


def test_spawn_context_ipc_and_picklable_render_tasks(unit_project, tmp_path):
    import multiprocessing as mp
    import pickle

    from src.core.storage import load_manifest, load_recap_script
    from src.render.fast_renderer import build_shot_render_tasks
    from src.render.visual_policy import assemble_visual_plan, iter_segment_shots

    project, chapter = unit_project
    manifest = load_manifest(chapter)
    script = load_recap_script(chapter)
    for seg in script.segments:
        seg.duration = 0.5
    shots = [sh for grp in iter_segment_shots(chapter, manifest, script, 1280, 720, False) for sh in grp]
    plan = assemble_visual_plan(manifest, script, shots, target_width=1280, target_height=720, fps=30, motion_enabled=False)
    tasks = build_shot_render_tasks(chapter, plan, manifest, work_dir=tmp_path, encoder="h264_nvenc")
    assert len(pickle.dumps(tasks)) > 0

    ctx = mp.get_context("spawn")
    jobs = ctx.Queue(maxsize=8)
    events = ctx.Queue(maxsize=16)
    proc = ctx.Process(target=worker._encode_queue, args=(jobs, events, 0, 1))
    proc.start()
    try:
        jobs.put(None)
        proc.join(timeout=15)
        assert proc.exitcode == 0
    finally:
        if proc.is_alive():
            proc.terminate()
        jobs.close()
        events.close()


def test_scheduled_dual_gpu_render_runs_through_mux(unit_project, monkeypatch):
    import shutil

    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg required")
    real_render = worker.render_shot_clip
    monkeypatch.setattr(worker.mp, "get_context", lambda *_: _thread_mp_context())
    monkeypatch.setattr(
        worker,
        "render_shot_clip",
        lambda task, *, canvas_cache=None, gpu_index=0: real_render(
            {**task, "encoder": "libx264"},
            canvas_cache=canvas_cache,
            gpu_index=gpu_index,
        ),
    )
    project, chapter = unit_project
    worker.run_pipeline(project, ["1"], "render", encoder="h264_nvenc", gpu_ids=(0, 1), render_workers=1)
    assert (chapter / "video" / "preview.mp4").stat().st_size > 0
    assert (chapter / "subtitles" / "preview.srt").stat().st_size > 0


def test_kaggle_payload_includes_watermark_and_generates_spawn_safe_script(unit_project, tmp_path):
    import ast
    import tarfile

    from src.cloud.kaggle_service import KaggleCloudService

    project, _ = unit_project
    watermarks = project.parent.parent / "watermarks"
    watermarks.mkdir()
    (watermarks / "default.png").write_bytes(b"watermark fixture")
    service = object.__new__(KaggleCloudService)
    archive = service.package_chapter_payload("unit", "1", tmp_path / "payload.tar.gz", job_type="produce")
    with tarfile.open(archive) as payload:
        assert "data/watermarks/default.png" in payload.getnames()
        assert "src/cloud/dual_gpu_worker.py" in payload.getnames()

    script = service.generate_worker_script("unit", "1")
    assert "from src.cloud.dual_gpu_worker import run" in script
    assert "shutil.rmtree(root, ignore_errors=True)" in script
    assert isinstance(ast.parse(script).body[-1], ast.If)
    assert "__name__ == '__main__'" in script


def test_cloud_sync_publishes_only_nonempty_outputs(unit_project, tmp_path, monkeypatch):
    import json
    import shutil
    import tarfile

    from src.cloud import payload
    from src.cloud.kaggle_service import KaggleCloudService
    from src.core.storage import read_json

    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg required")
    real_render = worker.render_shot_clip
    monkeypatch.setattr(worker.mp, "get_context", lambda *_: _thread_mp_context())
    monkeypatch.setattr(
        worker,
        "render_shot_clip",
        lambda task, *, canvas_cache=None, gpu_index=0: real_render(
            {**task, "encoder": "libx264"},
            canvas_cache=canvas_cache,
            gpu_index=gpu_index,
        ),
    )
    project, chapter = unit_project
    service = object.__new__(KaggleCloudService)
    archive = service.package_chapter_payload("unit", "1", tmp_path / "payload.tar.gz", job_type="render")
    with tarfile.open(archive) as tar:
        manifest = json.load(tar.extractfile("payload_manifest.json"))
    runtime = tmp_path / "runtime_project"
    shutil.copytree(project, runtime)
    worker.run_pipeline(runtime, ["1"], "render", encoder="h264_nvenc", gpu_ids=(0, 1), render_workers=1)
    output = tmp_path / "cloud_result"
    payload.export_results(runtime, manifest, service._payload_manifest_hash, output)
    video = output / "chapters" / "1" / "video" / "preview.mp4"
    original = video.read_bytes()
    video.write_bytes(b"")
    with pytest.raises(FileNotFoundError, match="Missing or empty"):
        service.sync_results("unit", "1", output, job_type="render")
    assert not (chapter / "video" / "preview.mp4").exists()
    video.write_bytes(original)
    result = service.sync_results("unit", "1", output, job_type="render")
    assert Path(result["video_path"]).stat().st_size == len(original)
    assert read_json(chapter / "chapter.json")["overall_status"] == "needs_review"


def test_watermark_uses_account_from_structured_setup(unit_project):
    from PIL import Image

    from src.core.storage import read_json
    from src.render.watermark import resolve_channel_watermark

    project, _ = unit_project
    setup_file = project / "series_setup_answers.json"
    setup = read_json(setup_file)
    setup["answers"]["Q08"]["user_answer"] = {"account": "muon_review", "privacy": "unlisted"}
    atomic_write_json(setup_file, setup)
    logo = project.parent.parent / "watermarks" / "muon_review.jpg"
    logo.parent.mkdir()
    Image.new("RGB", (40, 20)).save(logo)
    assert resolve_channel_watermark(project_dir=project) == logo.resolve()
