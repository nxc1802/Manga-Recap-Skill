from pathlib import Path
from queue import Queue
from threading import Event, Thread

from src.cloud import dual_gpu_worker as worker
from src.core.storage import atomic_write_json, load_recap_script
from src.core.visual_contract import VisualPlan
from src.render.fast_renderer import frame_schedule
from tests.unit.test_canonical_workflow import unit_project  # noqa: F401 — pytest fixture


def test_sound_and_visual_overlap_before_model_unloads(unit_project, monkeypatch, tmp_path):
    project, chapter = unit_project
    script = load_recap_script(chapter)
    script.segments.extend([
        script.segments[0].model_copy(update={"seq_id": "b"}),
        script.segments[0].model_copy(update={"seq_id": "c"}),
    ])
    for segment in script.segments:
        segment.duration = None
    atomic_write_json(chapter / "script.json", script)

    rendered = Event()
    released = Event()
    second_on_gpu_one = Event()
    tail_on_gpu_zero = Event()
    assignments = []
    jobs, events = Queue(), Queue()
    config = worker.read_json(project / "project.json")

    class Model:
        def __init__(self, gpu_id):
            assert gpu_id == 0

        def unload(self):
            released.set()

    def fake_synthesis(model, chapter, segments, voice, speed, batch_size):
        for segment in segments:
            if segment.seq_id == "b":
                assert rendered.wait(timeout=5), "GPU 1 must encode while TTS is active"
            if segment.seq_id == "c":
                assert second_on_gpu_one.wait(timeout=5)
            segment.duration = 0.51

    def fake_encode(task, canvas_cache, gpu_index):
        if gpu_index == 1:
            if task["shot_index"] == 0:
                rendered.set()
            else:
                second_on_gpu_one.set()
                assert released.wait(timeout=5)
                assert tail_on_gpu_zero.wait(timeout=5), "GPU 0 must drain remaining render work"
        else:
            assert released.is_set()
            tail_on_gpu_zero.set()
        assignments.append((task["shot_index"], task["frame_count"], gpu_index))
        path = Path(task["output_clip_path"])
        path.write_bytes(b"encoded fixture")
        return path

    monkeypatch.setattr("src.tts.cuda_worker.VieNeuCUDAWorker", Model)
    monkeypatch.setattr(worker, "_synthesize_batch", fake_synthesis)
    monkeypatch.setattr(worker, "render_shot_clip", fake_encode)
    visual = Thread(target=worker._encode_queue, args=(jobs, events, 1, 1), daemon=True)
    visual.start()
    worker._audio_then_encode(project, ["1"], tmp_path / "work", "produce", True,
                              config, "h264_nvenc", 0, 2, 1, jobs, events, 1)
    visual.join(timeout=5)
    assert not visual.is_alive()
    assert any(index == 0 and gpu == 1 for index, _, gpu in assignments)
    assert {(i, gpu) for i, _, gpu in assignments} == {(0, 1), (1, 1), (2, 0)}
    plan = VisualPlan.load(chapter / "visual_plan.json")
    assert [frames for _, frames, _ in sorted(assignments)] == [frames for _, frames in frame_schedule(plan)]


def test_single_device_render_runs_through_spawn_and_mux(unit_project):
    import shutil

    if not shutil.which("ffmpeg"):
        import pytest
        pytest.skip("FFmpeg required")
    project, chapter = unit_project
    worker.run_pipeline(project, ["1"], "render", encoder="libx264", gpu_ids=[0], render_workers=1)
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


def test_cloud_sync_publishes_only_nonempty_outputs(unit_project, tmp_path):
    import json
    import shutil
    import tarfile

    import pytest

    from src.cloud import payload
    from src.cloud.kaggle_service import KaggleCloudService
    from src.core.storage import read_json

    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg required")
    project, chapter = unit_project
    service = object.__new__(KaggleCloudService)
    archive = service.package_chapter_payload("unit", "1", tmp_path / "payload.tar.gz", job_type="render")
    with tarfile.open(archive) as tar:
        manifest = json.load(tar.extractfile("payload_manifest.json"))
    runtime = tmp_path / "runtime_project"
    shutil.copytree(project, runtime)
    worker.run_pipeline(runtime, ["1"], "render", encoder="libx264", gpu_ids=[0], render_workers=1)
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
