"""Overlap native VieNeu micro-batches with encoding on a second GPU."""
from __future__ import annotations

import multiprocessing as mp
import os
import queue
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from src.cloud.payload import verify_payload_manifest
from src.cloud.telemetry import (
    ResourceSampler,
    TelemetryRecorder,
    probe_worker_hardware,
    save_telemetry_report,
)
from src.core.human_review import require_preview_approval, require_voice_approval
from src.core.schemas import CheckpointStatus, StageType
from src.core.storage import (
    atomic_write_json, calculate_sha256, calculate_utterance_fingerprint,
    load_manifest, load_recap_script, read_json, update_checkpoint,
)
from src.core.utils import assert_file_nonempty, get_wav_duration
from src.core.visual_contract import VisualPlan
from src.render.chapter_renderer import prepare_audio, resolve_audio
from src.render.fast_renderer import (
    build_shot_render_tasks, concat_shot_clips, prepare_shot_watermark, render_shot_clip,
    slice_cues_for_window,
)
from src.render.subtitle import SubtitleGenerator
from src.render.visual_policy import assemble_visual_plan, iter_segment_shots, resolve_visual_plan
from src.tts.catalog import resolve_voice_id


def _synthesize_batch(worker, chapter, segments, voice, speed, batch_size):
    tasks = []
    for segment in segments:
        fingerprint = calculate_utterance_fingerprint(
            text=segment.narrator_text, voice=voice, speed=speed, emotion=segment.emotion,
            emotion_cue=segment.emotion_cue, engine_mode="neural",
        )
        output = chapter / "audio" / "clips" / f"audio_{chapter.name}_{segment.seq_id}.wav"
        if not output.resolve().is_relative_to(chapter / "audio" / "clips"):
            raise ValueError(f"Unsafe segment sequence ID: {segment.seq_id}")
        if segment.voice_id and segment.is_manual_voice() and resolve_voice_id(segment.voice_id) != voice:
            raise ValueError("Segment voice conflicts with the confirmed narrator")
        if segment.status == "synthesized" and segment.fingerprint == fingerprint and segment.checksum:
            try:
                if calculate_sha256(output) == segment.checksum:
                    segment.duration = get_wav_duration(output)
                    segment.audio_path = output.relative_to(chapter).as_posix()
                    continue
            except (OSError, ValueError):
                pass
        tasks.append(dict(seq_id=segment.seq_id, text=segment.narrator_text, voice=voice, speed=speed,
                          emotion=segment.emotion, emotion_cue=segment.emotion_cue,
                          output_path=output, fingerprint=fingerprint))

    if not tasks:
        return
    results = worker.synthesize_batch_tasks(tasks, batch_size=batch_size, cpu_workers=2)
    by_id = {result["seq_id"]: result for result in results}
    pending = {task["seq_id"]: task for task in tasks}
    for segment in segments:
        task = pending.get(segment.seq_id)
        if task is None:
            continue
        result = by_id[segment.seq_id]
        segment.duration = result["duration"]
        segment.checksum = result["sha256"]
        segment.audio_path = task["output_path"].relative_to(chapter).as_posix()
        segment.fingerprint = task["fingerprint"]
        segment.voice_id = segment.synthesized_voice = voice
        if segment.voice_source != "manual":
            segment.voice_source = "tts"
        segment.status = "synthesized"


def _subtitle_generator(settings):
    subtitle = settings["subtitle"]
    return SubtitleGenerator(max_chars_per_line=subtitle["max_chars_per_line"],
                             max_lines=subtitle["max_lines"], chunking_mode=subtitle["chunking"])


def _prepare_chapter(project, number, work, job, quality, settings, encoder, worker, approval, jobs, events, batch_size):
    chapter = project / "chapters" / number
    script = load_recap_script(chapter)
    manifest = load_manifest(chapter)
    plan_file = chapter / "visual_plan.json"
    render = job != "tts"
    produce = job != "render"
    if render and quality == "final" and number == "1":
        from src.render.chapter_renderer import render_chapter

        render_chapter(project, number, quality="final", encoder=encoder)
        events.put(("ready", number, 0, False))
        return

    geometry = dict(target_width=settings["target_width"], target_height=settings["target_height"],
                    fps=settings["fps"], motion_enabled=settings["render_pan_mode"] == "animation")
    subtitle = _subtitle_generator(settings)
    if render:
        work.mkdir(parents=True)
        watermark, watermark_xy = prepare_shot_watermark(
            chapter, work, geometry["target_width"], geometry["target_height"])
        update_checkpoint(chapter, StageType.RENDER, CheckpointStatus.RUNNING)
    if produce:
        update_checkpoint(chapter, StageType.TTS, CheckpointStatus.RUNNING)
    voice = resolve_voice_id(approval.primary_voice)
    authored = render and plan_file.exists()
    input_script = script.model_copy(deep=True) if authored and produce else None
    groups = iter_segment_shots(chapter, manifest, script, geometry["target_width"],
                                geometry["target_height"], geometry["motion_enabled"])
    shots = []
    elapsed = 0.0
    cue_time = 0.0
    previous_cues = []
    pending_shot = None
    for start in range(0, len(script.segments), batch_size):
        segments = script.segments[start:start + batch_size]
        if produce:
            t_tts = time.monotonic()
            _synthesize_batch(worker, chapter, segments, voice, approval.speed, batch_size)
            events.put((
                "span",
                "tts",
                t_tts,
                time.monotonic(),
                number,
                f"gpu:{getattr(worker, 'gpu_id', 0)}",
                {"segments": len(segments)},
            ))
            if start + batch_size >= len(script.segments):
                script.total_duration_seconds = round(sum(s.duration for s in script.segments), 3)
            atomic_write_json(chapter / "script.json", script)
        else:
            for segment in segments:
                duration = get_wav_duration(resolve_audio(chapter, segment))
                if segment.duration is None or abs(segment.duration - duration) > 0.001:
                    raise ValueError(f"Audio duration changed for {segment.seq_id}")
        if render and not authored:
            t_prep = time.monotonic()
            batch = [shot for _ in segments for shot in next(groups)]
            cues = subtitle.build_cues_from_segments(segments, start_offset=cue_time)
            for cue in cues:
                cue.start_seconds = round(cue.start_seconds, 3)
                cue.end_seconds = round(cue.end_seconds, 3)
            for segment in segments:
                cue_time += segment.duration
            all_cues = previous_cues + cues
            if pending_shot is not None:
                task, start_time, end_time = pending_shot
                task["local_subtitle_cues"] = slice_cues_for_window(all_cues, start_time, end_time)
                jobs.put((number, task))
            tasks = build_shot_render_tasks(
                chapter, SimpleNamespace(shots=batch, **geometry), manifest, work_dir=work,
                encoder=encoder, subtitle_cues=all_cues, burn_subtitles=settings["burn_subtitles"],
                subtitle_font_size=settings["subtitle"]["font_size"], quality_mode=quality,
                watermark_png_path=watermark, watermark_xy=watermark_xy,
                start_seconds=elapsed, start_index=len(shots),
            )
            ready = tasks
            if settings["burn_subtitles"] and len(tasks) > 1:
                # The last shot can overlap the next micro-batch by a fraction of a frame.
                # Queue the other shots immediately, keeping the boundary cue consistent.
                ready, last = tasks[:-1], tasks[-1]
                fps = last["fps"]
                boundary = round(elapsed * fps) + sum(task["frame_count"] for task in ready)
                pending_shot = last, boundary / fps, (boundary + last["frame_count"]) / fps
            else:
                pending_shot = None
            for task in ready:
                jobs.put((number, task))
            if pending_shot:
                previous_cues = [cue for cue in all_cues if cue.end_seconds > pending_shot[1]]
            else:
                previous_cues = [cue for cue in cues if cue.end_seconds > cue_time - 1 / geometry["fps"]]
            for shot in batch:
                elapsed += shot.duration_sec
            shots.extend(batch)
            events.put(("span", "shot_prep", t_prep, time.monotonic(), number, "cpu", {"shots": len(batch)}))

    if pending_shot is not None:
        jobs.put((number, pending_shot[0]))
    if produce:
        update_checkpoint(chapter, StageType.TTS, CheckpointStatus.COMPLETED,
                          artifacts=[s.audio_path for s in script.segments])
    if render:
        if authored:
            try:
                plan = resolve_visual_plan(chapter, manifest, script, **geometry)
            except ValueError:
                if input_script is None or input_script == script:
                    raise
                resynthesized = [shot for group in iter_segment_shots(
                    chapter, manifest, script, geometry["target_width"], geometry["target_height"],
                    geometry["motion_enabled"],
                ) for shot in group]
                plan = assemble_visual_plan(manifest, script, resynthesized, **geometry)
            tasks = build_shot_render_tasks(
                chapter, plan, manifest, work_dir=work, encoder=encoder,
                subtitle_cues=subtitle.build_cues_from_segments(script.segments),
                burn_subtitles=settings["burn_subtitles"], subtitle_font_size=settings["subtitle"]["font_size"],
                quality_mode=quality, watermark_png_path=watermark, watermark_xy=watermark_xy,
            )
            shots = plan.shots
            for task in tasks:
                jobs.put((number, task))
        else:
            plan = assemble_visual_plan(manifest, script, shots, **geometry)
        plan.save(plan_file)
    events.put(("ready", number, len(shots), render))


def _encode_queue(jobs, events, gpu_id, workers):
    canvas_cache = {}

    def encode():
        while (item := jobs.get()) is not None:
            number, task = item
            t_enc = time.monotonic()
            clip = render_shot_clip(task, canvas_cache=canvas_cache, gpu_index=gpu_id)
            assert_file_nonempty(clip)
            t_end = time.monotonic()
            events.put(("shot", number, task["shot_index"], str(clip)))
            events.put((
                "span",
                "encode",
                t_enc,
                t_end,
                number,
                f"gpu:{gpu_id}",
                {"shot_index": task["shot_index"], "frame_count": task.get("frame_count")},
            ))

    # A failed encoder must fail the process even if its peer is blocked on the queue.
    local_errors = queue.Queue()

    def guarded_encode():
        try:
            encode()
        except Exception as exc:
            local_errors.put(exc)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(guarded_encode) for _ in range(workers)]
        while not all(future.done() for future in futures):
            try:
                error = local_errors.get(timeout=0.25)
            except queue.Empty:
                continue
            events.put(("error", str(error)))
            break
        else:
            if not local_errors.empty():
                raise local_errors.get()


def _audio_then_encode(project, chapters, work, job, single_preview, config, encoder, gpu_id,
                       process_count, workers, jobs, events, batch_size):
    from src.tts.cuda_worker import VieNeuCUDAWorker

    approval = require_voice_approval(project)
    model = VieNeuCUDAWorker(gpu_id=gpu_id) if job != "render" else None
    try:
        for number in chapters:
            quality = "preview" if single_preview and number == "1" else "final"
            _prepare_chapter(project, number, work / number, job, quality, config["render_settings"],
                             encoder, model, approval, jobs, events, batch_size)
    finally:
        if model is not None:
            model.unload()

    if job == "tts":
        return
    for _ in range(process_count * workers):
        jobs.put(None)
    _encode_queue(jobs, events, gpu_id, workers)


def _finish_chapter(project, number, work, clips, config, quality):
    chapter = project / "chapters" / number
    script = load_recap_script(chapter)
    plan = VisualPlan.load(chapter / "visual_plan.json")
    subtitles = chapter / "subtitles" / f"{quality}.srt"
    _subtitle_generator(config["render_settings"]).generate_srt_file(script, subtitles)
    mixed = prepare_audio(project, chapter, script, config["audio_settings"])
    video = chapter / "video" / f"{quality}.mp4"
    concat_shot_clips(clips, video, work_dir=work, audio_path=mixed)
    assert_file_nonempty(video)
    assert_file_nonempty(subtitles)
    update_checkpoint(chapter, StageType.RENDER, CheckpointStatus.COMPLETED,
                      artifacts=[f"video/{quality}.mp4", f"subtitles/{quality}.srt", "visual_plan.json"])


def run_pipeline(project, chapters, job, *, encoder="h264_nvenc", gpu_ids=(0, 1), render_workers=2,
                 batch_size=12, single_preview=True, telemetry=None):
    """Use the same shot queue on one device. Encoding then starts after TTS unload."""
    project = Path(project).resolve()
    config = read_json(project / "project.json")
    workers = max(1, min(4, int(render_workers)))
    if job not in {"tts", "render", "produce"} or not chapters or batch_size < 1:
        raise ValueError("Invalid worker job")
    if not single_preview or any(number != "1" for number in chapters):
        require_preview_approval(project)
    devices = list(gpu_ids)[:2] or [0]
    if job == "tts" or encoder != "h264_nvenc":
        devices = devices[:1]

    recorder = telemetry or TelemetryRecorder(project_slug=project.name, chapters=list(chapters))

    with tempfile.TemporaryDirectory(prefix="manga-shots-") as directory:
        work = Path(directory)
        sampler = ResourceSampler(
            work_root=work,
            interval_sec=1.5,
            queue_depth_fn=lambda: {
                "jobs_qsize": jobs.qsize() if hasattr(jobs, "qsize") else -1,
                "events_qsize": events.qsize() if hasattr(events, "qsize") else -1,
            },
        )
        ctx = mp.get_context("spawn")
        jobs = ctx.Queue()
        events = ctx.Queue()
        sampler.start()
        children = [ctx.Process(target=_audio_then_encode, args=(
            project, chapters, work, job, single_preview, config, encoder, devices[0], len(devices),
            workers, jobs, events, batch_size,
        ))]
        if len(devices) == 2:
            children.append(ctx.Process(target=_encode_queue, args=(jobs, events, devices[1], workers)))

        expected = {}
        clips = {number: {} for number in chapters}
        completed = set()
        started = []
        try:
            for child in children:
                child.start()
                started.append(child)
            while len(completed) < len(chapters):
                try:
                    event = events.get(timeout=0.5)
                except queue.Empty:
                    if any(child.exitcode not in (None, 0) for child in started):
                        raise RuntimeError("Audio or visual process failed. See worker traceback.")
                    if all(child.exitcode is not None for child in started):
                        raise RuntimeError("Worker exited before reporting all chapters")
                    continue
                kind, *rest = event
                if kind == "error":
                    raise RuntimeError(f"Shot encoding failed: {rest[0]}")
                if kind == "span":
                    stage, s_t0, s_t1, ch_num, dev_id, meta = rest
                    recorder.record_stage(
                        stage,
                        s_t0,
                        s_t1,
                        chapter_id=str(ch_num),
                        device_id=str(dev_id),
                        metadata=meta,
                    )
                    continue
                number = rest[0]
                if kind == "ready":
                    expected[number] = tuple(rest[1:])
                else:
                    index, path = rest[1:]
                    clips[number][index] = Path(path)
                if number in expected and len(clips[number]) == expected[number][0]:
                    if expected[number][1]:
                        quality = "preview" if single_preview and number == "1" else "final"
                        with recorder.span("finalize", chapter_id=str(number), device_id="cpu"):
                            _finish_chapter(
                                project,
                                number,
                                work / number,
                                [clips[number][i] for i in range(len(clips[number]))],
                                config,
                                quality,
                            )
                    completed.add(number)
            for child in started:
                child.join(timeout=30)
                if child.exitcode != 0:
                    raise RuntimeError(f"Worker failed to exit cleanly: {child.exitcode}")
        finally:
            sampler.stop()
            recorder.set_sampler_summary(sampler.summarize())
            for child in started:
                if child.is_alive():
                    child.terminate()
                child.join()
            jobs.close()
            events.close()


def run(root, manifest_hash, output_dir, encoder="h264_nvenc", render_workers=2, compile_feature=False):
    """Kaggle entry point. The bootstrap calls this exclusively from __main__."""
    recorder = TelemetryRecorder(job_id=f"kaggle-{manifest_hash[:12]}")
    with recorder.span("bootstrap_verify"):
        manifest = verify_payload_manifest(root, manifest_hash)
        recorder.project_slug = manifest["project_slug"]
        recorder.chapters = list(manifest["chapters"])
        runtime_data = root / "data"
        project = runtime_data / "projects" / manifest["project_slug"]
        shutil.copytree(root / "project", project)
        os.environ["MANGA_RECAP_DATA_DIR"] = str(runtime_data)
        os.environ["MANGA_RECAP_OUTPUT_DIR"] = str(root / "exports")

    from src.cloud.payload import export_results
    from src.production.hardware import smoke_test_encoder

    hardware = probe_worker_hardware(root, probe_nvenc_devices=True)
    recorder.set_hardware(hardware)

    devices = [int(d["index"]) for d in hardware.get("devices") or []][:2]
    usable_nvenc = set(hardware.get("usable_nvenc_devices") or [])
    if encoder == "h264_nvenc" and (not devices or not usable_nvenc and not smoke_test_encoder("h264_nvenc")):
        encoder = "libx264"

    chapters = manifest["chapters"]
    single_preview = not manifest.get("series")
    run_pipeline(
        project,
        chapters,
        manifest["job_type"],
        encoder=encoder,
        gpu_ids=devices,
        render_workers=render_workers,
        single_preview=single_preview,
        telemetry=recorder,
    )
    out_path = Path(output_dir)
    with recorder.span("export_and_compile"):
        export_results(project, manifest, manifest_hash, out_path, compile_feature=compile_feature)
    save_telemetry_report(out_path / "cloud_telemetry.json", recorder.build_report())
