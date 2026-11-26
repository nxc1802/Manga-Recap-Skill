"""Overlap native VieNeu micro-batches with encoding on a second GPU."""
from __future__ import annotations

import multiprocessing as mp
import os
import queue
import shutil
import tempfile
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from src.cloud.lifecycle import check_disk_low_water
from src.cloud.payload import verify_payload_manifest
from src.cloud.scheduler import (
    ExecutionPolicy,
    UtteranceDispatcher,
    UtteranceItem,
    resolve_execution_policy,
)
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


class _ChapterShotPrep:
    """Stateful sequential chapter shot & subtitle preparer preserving frame/cue determinism."""

    def __init__(self, chapter, manifest, script, work, settings, encoder, quality, subtitle, watermark, watermark_xy):
        self.chapter = chapter
        self.number = chapter.name
        self.manifest = manifest
        self.script = script
        self.work = work
        self.settings = settings
        self.encoder = encoder
        self.quality = quality
        self.subtitle = subtitle
        self.watermark = watermark
        self.watermark_xy = watermark_xy
        self.plan_file = chapter / "visual_plan.json"
        self.authored = self.plan_file.exists()
        self.input_script = script.model_copy(deep=True) if self.authored else None
        self.geometry = dict(
            target_width=settings["target_width"],
            target_height=settings["target_height"],
            fps=settings["fps"],
            motion_enabled=settings["render_pan_mode"] == "animation",
        )
        self.groups = iter_segment_shots(
            chapter, manifest, script,
            self.geometry["target_width"], self.geometry["target_height"], self.geometry["motion_enabled"],
        )
        self.shots = []
        self.elapsed = 0.0
        self.cue_time = 0.0
        self.previous_cues = []
        self.pending_shot = None

    def advance_segments(self, segments):
        """Advances sequential shot preparation for newly ready ``segments``; returns ready ``(chapter, task)`` list."""
        ready_tasks = []
        if not segments or self.authored:
            return ready_tasks
        batch = [shot for _ in segments for shot in next(self.groups)]
        cues = self.subtitle.build_cues_from_segments(segments, start_offset=self.cue_time)
        for cue in cues:
            cue.start_seconds = round(cue.start_seconds, 3)
            cue.end_seconds = round(cue.end_seconds, 3)
        for segment in segments:
            self.cue_time += segment.duration
        all_cues = self.previous_cues + cues
        if self.pending_shot is not None:
            task, start_time, end_time = self.pending_shot
            task["local_subtitle_cues"] = slice_cues_for_window(all_cues, start_time, end_time)
            ready_tasks.append((self.number, task))
        tasks = build_shot_render_tasks(
            self.chapter, SimpleNamespace(shots=batch, **self.geometry), self.manifest,
            work_dir=self.work, encoder=self.encoder, subtitle_cues=all_cues,
            burn_subtitles=self.settings["burn_subtitles"],
            subtitle_font_size=self.settings["subtitle"]["font_size"],
            quality_mode=self.quality, watermark_png_path=self.watermark, watermark_xy=self.watermark_xy,
            start_seconds=self.elapsed, start_index=len(self.shots),
        )
        ready = tasks
        if self.settings["burn_subtitles"] and len(tasks) > 1:
            ready, last = tasks[:-1], tasks[-1]
            fps = last["fps"]
            boundary = round(self.elapsed * fps) + sum(task["frame_count"] for task in ready)
            self.pending_shot = last, boundary / fps, (boundary + last["frame_count"]) / fps
        else:
            self.pending_shot = None
        for task in ready:
            ready_tasks.append((self.number, task))
        if self.pending_shot:
            self.previous_cues = [cue for cue in all_cues if cue.end_seconds > self.pending_shot[1]]
        else:
            self.previous_cues = [cue for cue in cues if cue.end_seconds > self.cue_time - 1 / self.geometry["fps"]]
        for shot in batch:
            self.elapsed += shot.duration_sec
        self.shots.extend(batch)
        return ready_tasks

    def finalize(self):
        """Flushes trailing boundary shot or resolves authored VisualPlan after full chapter TTS."""
        ready_tasks = []
        if self.pending_shot is not None:
            ready_tasks.append((self.number, self.pending_shot[0]))
            self.pending_shot = None
        if self.authored:
            try:
                plan = resolve_visual_plan(self.chapter, self.manifest, self.script, **self.geometry)
            except ValueError:
                if self.input_script is None or self.input_script == self.script:
                    raise
                resynthesized = [shot for group in iter_segment_shots(
                    self.chapter, self.manifest, self.script,
                    self.geometry["target_width"], self.geometry["target_height"],
                    self.geometry["motion_enabled"],
                ) for shot in group]
                plan = assemble_visual_plan(self.manifest, self.script, resynthesized, **self.geometry)
            tasks = build_shot_render_tasks(
                self.chapter, plan, self.manifest, work_dir=self.work, encoder=self.encoder,
                subtitle_cues=self.subtitle.build_cues_from_segments(self.script.segments),
                burn_subtitles=self.settings["burn_subtitles"],
                subtitle_font_size=self.settings["subtitle"]["font_size"],
                quality_mode=self.quality, watermark_png_path=self.watermark, watermark_xy=self.watermark_xy,
            )
            self.shots = plan.shots
            for task in tasks:
                ready_tasks.append((self.number, task))
        else:
            plan = assemble_visual_plan(self.manifest, self.script, self.shots, **self.geometry)
        plan.save(self.plan_file)
        return ready_tasks, len(self.shots)


def _prepare_chapter(project, number, work, job, quality, settings, encoder, worker, approval, jobs, events, batch_size):
    chapter = project / "chapters" / number
    script = load_recap_script(chapter)
    manifest = load_manifest(chapter)
    render = job != "tts"
    produce = job != "render"
    if render and quality == "final" and number == "1":
        from src.render.chapter_renderer import render_chapter

        render_chapter(project, number, quality="final", encoder=encoder)
        events.put(("ready", number, 0, False))
        return

    subtitle = _subtitle_generator(settings)
    watermark, watermark_xy = (None, None)
    if render:
        work.mkdir(parents=True, exist_ok=True)
        watermark, watermark_xy = prepare_shot_watermark(
            chapter, work, settings["target_width"], settings["target_height"])
        update_checkpoint(chapter, StageType.RENDER, CheckpointStatus.RUNNING)
    if produce:
        update_checkpoint(chapter, StageType.TTS, CheckpointStatus.RUNNING)
    voice = resolve_voice_id(approval.primary_voice)
    prep = (
        _ChapterShotPrep(chapter, manifest, script, work, settings, encoder, quality, subtitle, watermark, watermark_xy)
        if render
        else None
    )
    if prep is not None and not produce:
        prep.input_script = None
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
        if render and not prep.authored:
            t_prep = time.monotonic()
            for item in prep.advance_segments(segments):
                jobs.put(item)
            events.put(("span", "shot_prep", t_prep, time.monotonic(), number, "cpu", {"shots": len(segments)}))

    if produce:
        update_checkpoint(chapter, StageType.TTS, CheckpointStatus.COMPLETED,
                          artifacts=[s.audio_path for s in script.segments])
    total_shots = 0
    if render:
        final_tasks, total_shots = prep.finalize()
        for item in final_tasks:
            jobs.put(item)
    events.put(("ready", number, total_shots, render))


class _InProcessQueue:
    """Lock-free single-process FIFO queue avoiding OS pipe deadlocks on single GPU."""

    def __init__(self):
        self._buf = deque()

    def put(self, item):
        self._buf.append(item)

    def get(self):
        return self._buf.popleft()

    def qsize(self):
        return len(self._buf)

    def empty(self):
        return not self._buf

    def close(self):
        self._buf.clear()


class _HeartbeatEmitter:
    """Daemon heartbeat emitter that never blocks full event queues."""

    def __init__(self, events, worker_label, interval_sec=10.0):
        self._events = events
        self._worker_label = worker_label
        self._interval_sec = float(interval_sec)
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._emit()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _emit(self):
        payload = ("heartbeat", self._worker_label, time.monotonic(), 0)
        put_nowait = getattr(self._events, "put_nowait", None)
        try:
            if callable(put_nowait):
                put_nowait(payload)
            else:
                self._events.put(payload)
        except Exception:
            pass

    def _run(self):
        while not self._stop.wait(self._interval_sec):
            self._emit()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)


def _terminate_process_tree(child):
    """Best-effort child termination escalating terminate -> wait -> kill."""
    if not child.is_alive():
        child.join(timeout=0.5)
        return
    try:
        child.terminate()
        child.join(timeout=3.0)
        if child.is_alive():
            child.kill()
            child.join(timeout=2.0)
    except Exception:
        pass


def _encode_queue(jobs, events, gpu_id, workers):
    canvas_cache = {}
    hb = _HeartbeatEmitter(events, f"gpu:{gpu_id}")
    hb.start()

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

    try:
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
    finally:
        hb.stop()


def _audio_then_encode(project, chapters, work, job, single_preview, config, encoder, gpu_id,
                       process_count, workers, jobs, events, batch_size):
    from src.tts.cuda_worker import VieNeuCUDAWorker

    local_jobs = _InProcessQueue() if jobs is None else jobs
    hb = _HeartbeatEmitter(events, f"gpu:{gpu_id}")
    hb.start()
    try:
        approval = require_voice_approval(project)
        model = VieNeuCUDAWorker(gpu_id=gpu_id) if job != "render" else None
        try:
            for number in chapters:
                quality = "preview" if single_preview and number == "1" else "final"
                _prepare_chapter(project, number, work / number, job, quality, config["render_settings"],
                                 encoder, model, approval, local_jobs, events, batch_size)
        finally:
            if model is not None:
                model.unload()

        if job == "tts":
            return
        for _ in range(process_count * workers):
            local_jobs.put(None)
        _encode_queue(local_jobs, events, gpu_id, workers)
    finally:
        hb.stop()
        if jobs is None:
            local_jobs.close()


def _safe_qsize(q):
    if q is None or not hasattr(q, "qsize"):
        return -1
    try:
        return int(q.qsize())
    except (NotImplementedError, AttributeError):
        return -1


def _tts_worker_loop(tts_jobs, events, gpu_id, batch_size):
    from src.tts.cuda_worker import VieNeuCUDAWorker

    hb = _HeartbeatEmitter(events, f"gpu:{gpu_id}")
    hb.start()
    model = None
    try:
        model = VieNeuCUDAWorker(gpu_id=gpu_id)
        while (item := tts_jobs.get()) is not None:
            batch_id, task_dicts = item
            t0 = time.monotonic()
            results = model.synthesize_batch_tasks(
                task_dicts,
                batch_size=max(1, min(int(batch_size), len(task_dicts))),
                cpu_workers=2,
            )
            t1 = time.monotonic()
            clean_results = []
            for res in results:
                entry = dict(res)
                if "output_path" in entry:
                    entry["output_path"] = str(entry["output_path"])
                clean_results.append(entry)
            first_ch = str(task_dicts[0].get("chapter_number") or "")
            events.put((
                "span",
                "tts",
                t0,
                t1,
                first_ch,
                f"gpu:{gpu_id}",
                {"segments": len(clean_results), "batch_id": batch_id},
            ))
            events.put(("tts_batch_done", gpu_id, batch_id, clean_results))
    except Exception as exc:
        events.put(("error", f"TTS gpu:{gpu_id}: {exc}"))
        raise
    finally:
        if model is not None:
            model.unload()
        hb.stop()


def _tts_then_encode_worker_loop(tts_jobs, encode_jobs, events, gpu_id, workers, batch_size, can_encode):
    _tts_worker_loop(tts_jobs, events, gpu_id, batch_size)
    if can_encode and encode_jobs is not None:
        _encode_queue(encode_jobs, events, gpu_id, workers)


def _dual_tts_shared_worker_loop(tts_jobs, encode_jobs, events, gpu_id, workers, batch_size, can_encode):
    enc_thread = None
    if can_encode and encode_jobs is not None:
        enc_thread = threading.Thread(
            target=_encode_queue,
            args=(encode_jobs, events, gpu_id, workers),
            daemon=True,
        )
        enc_thread.start()
    try:
        _tts_worker_loop(tts_jobs, events, gpu_id, batch_size)
    finally:
        if enc_thread is not None:
            enc_thread.join()


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


class _AsyncChapterFinalizer:
    """Bounded CPU finalizer overlapping SRT/audio/mux with GPU workers and pruning verified temps."""

    def __init__(self, project, config, recorder, *, max_workers=1, min_free_disk_mb=256):
        self.project = project
        self.config = config
        self.recorder = recorder
        self.min_free_disk_mb = int(min_free_disk_mb)
        self._pool = ThreadPoolExecutor(max_workers=max(1, int(max_workers)))
        self._futures = {}
        self._order = []
        self._completed = set()

    def submit(self, number, ch_work, clip_paths, quality):
        num = str(number)
        if num in self._futures:
            return
        self._order.append(num)
        self._futures[num] = self._pool.submit(self._run_one, num, ch_work, list(clip_paths), quality)

    def _run_one(self, number, ch_work, clip_paths, quality):
        t0 = time.monotonic()
        _finish_chapter(self.project, number, ch_work, clip_paths, self.config, quality)
        self.recorder.record_stage("finalize", t0, time.monotonic(), chapter_id=str(number), device_id="cpu")
        if ch_work is not None and Path(ch_work).exists():
            shutil.rmtree(ch_work, ignore_errors=True)
        check_disk_low_water(ch_work.parent if ch_work is not None else self.project, min_free_mb=self.min_free_disk_mb)
        return number

    def poll_completed(self):
        for num, fut in list(self._futures.items()):
            if fut.done() and num not in self._completed:
                fut.result()
                self._completed.add(num)
        return self._completed

    def wait_ordered(self, canonical_order=None):
        order = [str(c) for c in canonical_order] if canonical_order else list(self._order)
        for num in order:
            fut = self._futures.get(num)
            if fut is not None:
                fut.result()
                self._completed.add(num)
        return self._completed

    def close(self, *, cancel_pending=False):
        self._pool.shutdown(wait=not cancel_pending, cancel_futures=cancel_pending)


def _run_scheduled_pipeline(
    project,
    chapters,
    job,
    *,
    plan,
    workers,
    batch_size,
    single_preview,
    config,
    recorder,
    worker_heartbeat_timeout_sec,
    min_free_disk_mb,
):
    from src.tts.engine import _prepare_tts_text

    render = job != "tts"
    produce = job != "render"
    settings = config["render_settings"]
    approval = require_voice_approval(project)
    voice = resolve_voice_id(approval.primary_voice)

    with tempfile.TemporaryDirectory(prefix="manga-shots-") as directory:
        work = Path(directory)
        check_disk_low_water(work, min_free_mb=min_free_disk_mb)

        completed = set()
        expected = {}
        clips = {}
        active_chapters = []
        prep_done = set()
        dispatcher = UtteranceDispatcher(max_batch_size=min(16, max(1, int(batch_size))))
        chapter_states = {}
        pending_encode = deque()

        for ch_idx, raw_num in enumerate(chapters):
            number = str(raw_num)
            quality = "preview" if single_preview and number == "1" else "final"
            if render and quality == "final" and number == "1":
                from src.render.chapter_renderer import render_chapter

                render_chapter(project, number, quality="final", encoder=plan.encoder)
                completed.add(number)
                continue

            active_chapters.append(number)
            clips[number] = {}
            ch_dir = project / "chapters" / number
            script = load_recap_script(ch_dir)
            manifest = load_manifest(ch_dir)
            ch_work = work / number
            watermark, watermark_xy = (None, None)
            if render:
                ch_work.mkdir(parents=True, exist_ok=True)
                watermark, watermark_xy = prepare_shot_watermark(
                    ch_dir, ch_work, settings["target_width"], settings["target_height"]
                )
                update_checkpoint(ch_dir, StageType.RENDER, CheckpointStatus.RUNNING)
            if produce:
                update_checkpoint(ch_dir, StageType.TTS, CheckpointStatus.RUNNING)

            subtitle = _subtitle_generator(settings)
            prep = (
                _ChapterShotPrep(ch_dir, manifest, script, ch_work, settings, plan.encoder, quality, subtitle, watermark, watermark_xy)
                if render
                else None
            )
            if prep is not None and not produce:
                prep.input_script = None

            ready_mask = [False] * len(script.segments)
            seg_index_map = {}
            for seg_idx, segment in enumerate(script.segments):
                seg_index_map[str(segment.seq_id)] = seg_idx
                if produce:
                    fp = calculate_utterance_fingerprint(
                        text=segment.narrator_text,
                        voice=voice,
                        speed=approval.speed,
                        emotion=segment.emotion,
                        emotion_cue=segment.emotion_cue,
                        engine_mode="neural",
                    )
                    output = ch_dir / "audio" / "clips" / f"audio_{ch_dir.name}_{segment.seq_id}.wav"
                    if not output.resolve().is_relative_to(ch_dir / "audio" / "clips"):
                        raise ValueError(f"Unsafe segment sequence ID: {segment.seq_id}")
                    if segment.voice_id and segment.is_manual_voice() and resolve_voice_id(segment.voice_id) != voice:
                        raise ValueError("Segment voice conflicts with the confirmed narrator")
                    reused = False
                    if segment.status == "synthesized" and segment.fingerprint == fp and segment.checksum:
                        try:
                            if calculate_sha256(output) == segment.checksum:
                                segment.duration = get_wav_duration(output)
                                segment.audio_path = output.relative_to(ch_dir).as_posix()
                                reused = True
                        except (OSError, ValueError):
                            pass
                    if reused:
                        ready_mask[seg_idx] = True
                    else:
                        prepared_text = _prepare_tts_text(segment.narrator_text, emotion_cue=segment.emotion_cue)
                        dispatcher.enqueue(
                            UtteranceItem(
                                chapter_index=ch_idx,
                                chapter_number=number,
                                segment_index=seg_idx,
                                seq_id=str(segment.seq_id),
                                text=prepared_text,
                                voice=voice,
                                speed=approval.speed,
                                emotion=segment.emotion,
                                emotion_cue=segment.emotion_cue,
                                output_path=output,
                                fingerprint=fp,
                            )
                        )
                else:
                    duration = get_wav_duration(resolve_audio(ch_dir, segment))
                    if segment.duration is None or abs(segment.duration - duration) > 0.001:
                        raise ValueError(f"Audio duration changed for {segment.seq_id}")
                    ready_mask[seg_idx] = True

            chapter_states[number] = {
                "dir": ch_dir,
                "script": script,
                "quality": quality,
                "prep": prep,
                "ready_mask": ready_mask,
                "next_shot_idx": 0,
                "seg_index_map": seg_index_map,
            }

            # Advance any immediately verified segments (e.g. render-only or cached TTS).
            _advance_chapter_prefix(
                number,
                chapter_states[number],
                produce=produce,
                render=render,
                pending_encode=pending_encode,
                expected=expected,
                prep_done=prep_done,
                completed=completed,
                recorder=recorder,
            )

        if not active_chapters or len(completed) == len(chapters):
            return

        ctx = mp.get_context("spawn")
        finalizer = (
            _AsyncChapterFinalizer(project, config, recorder, max_workers=1, min_free_disk_mb=min_free_disk_mb)
            if render
            else None
        )
        finished_ok = False
        try:
            if plan.policy == ExecutionPolicy.DUAL_TTS_THEN_DUAL_ENCODE and job == "produce":
                # Phase 1: Parallel TTS -> explicit model unload across both GPUs.
                if not dispatcher.is_drained:
                    _run_coordinator_stage(
                        ctx=ctx,
                        work=work,
                        project=project,
                        config=config,
                        plan=plan,
                        workers=workers,
                        batch_size=batch_size,
                        dispatcher=dispatcher,
                        chapter_states=chapter_states,
                        pending_encode=pending_encode,
                        expected=expected,
                        clips=clips,
                        prep_done=prep_done,
                        completed=completed,
                        active_chapters=active_chapters,
                        target_completed=set(),
                        tts_devices=plan.tts_devices,
                        encode_devices=(),
                        shared_encode=False,
                        produce=True,
                        render=render,
                        recorder=recorder,
                        worker_heartbeat_timeout_sec=worker_heartbeat_timeout_sec,
                        min_free_disk_mb=min_free_disk_mb,
                        finalizer=finalizer,
                    )
                # Phase 2: Dedicated full-VRAM dual-GPU NVENC encode.
                if len(completed) < len(chapters):
                    _run_coordinator_stage(
                        ctx=ctx,
                        work=work,
                        project=project,
                        config=config,
                        plan=plan,
                        workers=workers,
                        batch_size=batch_size,
                        dispatcher=None,
                        chapter_states=chapter_states,
                        pending_encode=pending_encode,
                        expected=expected,
                        clips=clips,
                        prep_done=prep_done,
                        completed=completed,
                        active_chapters=active_chapters,
                        target_completed=set(str(c) for c in chapters),
                        tts_devices=(),
                        encode_devices=plan.encode_devices,
                        shared_encode=False,
                        produce=False,
                        render=True,
                        recorder=recorder,
                        worker_heartbeat_timeout_sec=worker_heartbeat_timeout_sec,
                        min_free_disk_mb=min_free_disk_mb,
                        finalizer=finalizer,
                    )
            else:
                _run_coordinator_stage(
                    ctx=ctx,
                    work=work,
                    project=project,
                    config=config,
                    plan=plan,
                    workers=workers,
                    batch_size=batch_size,
                    dispatcher=dispatcher if (produce and not dispatcher.is_drained) else None,
                    chapter_states=chapter_states,
                    pending_encode=pending_encode,
                    expected=expected,
                    clips=clips,
                    prep_done=prep_done,
                    completed=completed,
                    active_chapters=active_chapters,
                    target_completed=set(str(c) for c in chapters),
                    tts_devices=plan.tts_devices if (produce and not dispatcher.is_drained) else (),
                    encode_devices=plan.encode_devices if render else (),
                    shared_encode=(plan.policy == ExecutionPolicy.DUAL_TTS_SHARED_ENCODE and job == "produce"),
                    produce=produce,
                    render=render,
                    recorder=recorder,
                    worker_heartbeat_timeout_sec=worker_heartbeat_timeout_sec,
                    min_free_disk_mb=min_free_disk_mb,
                    finalizer=finalizer,
                )
            if finalizer is not None:
                finalizer.wait_ordered(canonical_order=active_chapters)
            finished_ok = True
        finally:
            if finalizer is not None:
                finalizer.close(cancel_pending=not finished_ok)


def _advance_chapter_prefix(number, st, *, produce, render, pending_encode, expected, prep_done, completed, recorder):
    ready_mask = st["ready_mask"]
    script = st["script"]
    prep = st["prep"]
    idx = st["next_shot_idx"]
    batch = []
    while idx < len(ready_mask) and ready_mask[idx]:
        batch.append(script.segments[idx])
        idx += 1
    st["next_shot_idx"] = idx
    if batch and render and prep is not None and not prep.authored:
        t_prep = time.monotonic()
        pending_encode.extend(prep.advance_segments(batch))
        recorder.record_stage("shot_prep", t_prep, time.monotonic(), chapter_id=number, device_id="cpu", metadata={"shots": len(batch)})

    if idx == len(ready_mask) and number not in prep_done:
        ch_dir = st["dir"]
        if produce:
            script.total_duration_seconds = round(sum(s.duration for s in script.segments), 3)
            atomic_write_json(ch_dir / "script.json", script)
            update_checkpoint(
                ch_dir, StageType.TTS, CheckpointStatus.COMPLETED,
                artifacts=[s.audio_path for s in script.segments],
            )
        if not render:
            prep_done.add(number)
            completed.add(number)
            return
        if prep is not None:
            t_prep = time.monotonic()
            final_tasks, total_shots = prep.finalize()
            pending_encode.extend(final_tasks)
            recorder.record_stage("shot_prep", t_prep, time.monotonic(), chapter_id=number, device_id="cpu", metadata={"final_shots": len(final_tasks)})
            expected[number] = (total_shots, True)
            prep_done.add(number)


def _match_tts_results(batch_items, results):
    by_exact = {(item.chapter_number, item.seq_id): item for item in batch_items}
    by_seq_fifo = {}
    for item in batch_items:
        by_seq_fifo.setdefault(item.seq_id, deque()).append(item)
    matched = []
    for res in results:
        res_seq = str(res["seq_id"])
        res_ch = res.get("chapter_number")
        item = None
        if res_ch is not None:
            item = by_exact.get((str(res_ch), res_seq))
        if item is None and by_seq_fifo.get(res_seq):
            item = by_seq_fifo[res_seq].popleft()
        if item is None:
            raise RuntimeError(f"Unrecognized TTS result key: chapter={res_ch}, seq={res_seq}")
        matched.append((item, res))
    return matched


def _run_coordinator_stage(
    *,
    ctx,
    work,
    project,
    config,
    plan,
    workers,
    batch_size,
    dispatcher,
    chapter_states,
    pending_encode,
    expected,
    clips,
    prep_done,
    completed,
    active_chapters,
    target_completed,
    tts_devices,
    encode_devices,
    shared_encode,
    produce,
    render,
    recorder,
    worker_heartbeat_timeout_sec,
    min_free_disk_mb,
    finalizer=None,
):
    tts_devs = tuple(tts_devices)
    enc_devs = tuple(encode_devices)
    has_tts = bool(tts_devs and dispatcher is not None)
    has_enc = bool(enc_devs and render)
    if not has_tts and not has_enc:
        return

    tts_capacity = max(4, len(tts_devs) * 2)
    encode_capacity = max(8, len(enc_devs) * workers * 4)
    tts_jobs = ctx.Queue(maxsize=max(8, tts_capacity * 2)) if has_tts else None
    encode_jobs = ctx.Queue(maxsize=max(32, encode_capacity * 2)) if has_enc else None
    events = ctx.Queue(maxsize=512)

    sampler = ResourceSampler(
        work_root=work,
        interval_sec=1.5,
        queue_depth_fn=lambda: {
            "tts_qsize": _safe_qsize(tts_jobs),
            "encode_qsize": _safe_qsize(encode_jobs),
            "events_qsize": _safe_qsize(events),
        },
    )
    sampler.start()

    children = []
    enc_set = set(enc_devs)
    tts_set = set(tts_devs)
    if has_tts and not has_enc:
        for d in tts_devs:
            children.append(ctx.Process(target=_tts_worker_loop, args=(tts_jobs, events, d, batch_size)))
    elif not has_tts and has_enc:
        for d in enc_devs:
            children.append(ctx.Process(target=_encode_queue, args=(encode_jobs, events, d, workers)))
    elif shared_encode:
        for d in tts_devs:
            children.append(ctx.Process(
                target=_dual_tts_shared_worker_loop,
                args=(tts_jobs, encode_jobs, events, d, workers, batch_size, d in enc_set),
            ))
        for d in enc_devs:
            if d not in tts_set:
                children.append(ctx.Process(target=_encode_queue, args=(encode_jobs, events, d, workers)))
    else:
        # Policy D (one TTS + overlapping encode on secondary GPU -> producer joins encode after unload)
        for idx, d in enumerate(tts_devs):
            target = _tts_then_encode_worker_loop if idx == 0 else _tts_worker_loop
            args = (
                (tts_jobs, encode_jobs, events, d, workers, batch_size, d in enc_set)
                if idx == 0
                else (tts_jobs, events, d, batch_size)
            )
            children.append(ctx.Process(target=target, args=args))
        for d in enc_devs:
            if d not in tts_set:
                children.append(ctx.Process(target=_encode_queue, args=(encode_jobs, events, d, workers)))

    tts_in_flight = 0
    in_flight_batches = {}
    pending_tts_items = deque()
    tts_sentinels_sent = False
    encode_in_flight = 0
    encode_sentinels_queued = False
    rr_idx = 0
    started = []
    last_activity = time.monotonic()

    def _pump_queues():
        nonlocal tts_in_flight, tts_sentinels_sent, encode_in_flight, encode_sentinels_queued, rr_idx
        if has_tts and tts_jobs is not None:
            while dispatcher.pending_count > 0 and (tts_in_flight + len(pending_tts_items)) < tts_capacity:
                dev = tts_devs[rr_idx % len(tts_devs)]
                rr_idx += 1
                picked = dispatcher.next_batch(device_id=dev)
                if picked is None:
                    break
                b_id, b_items = picked
                in_flight_batches[b_id] = b_items
                pending_tts_items.append((b_id, [u.to_worker_task() for u in b_items]))
            if dispatcher.is_drained and not tts_sentinels_sent:
                for _ in tts_devs:
                    pending_tts_items.append(None)
                tts_sentinels_sent = True
            while pending_tts_items:
                item = pending_tts_items[0]
                try:
                    tts_jobs.put_nowait(item)
                except queue.Full:
                    break
                pending_tts_items.popleft()
                if item is not None:
                    tts_in_flight += 1

        if has_enc and encode_jobs is not None:
            if not encode_sentinels_queued and len(prep_done) == len(active_chapters):
                for _ in range(len(enc_devs) * workers):
                    pending_encode.append(None)
                encode_sentinels_queued = True
            while pending_encode and (encode_in_flight < encode_capacity or pending_encode[0] is None):
                item = pending_encode[0]
                try:
                    encode_jobs.put_nowait(item)
                except queue.Full:
                    break
                pending_encode.popleft()
                if item is not None:
                    encode_in_flight += 1

    def _stage_done():
        if has_tts and (not dispatcher.is_drained or pending_tts_items or not tts_sentinels_sent):
            return False
        if has_enc:
            return target_completed.issubset(completed)
        return True

    try:
        for child in children:
            child.start()
            started.append(child)
        _pump_queues()

        # Check chapters that might already have 0 shots to encode cleanly
        for number in list(active_chapters):
            if number in expected and expected[number][0] == 0 and number not in completed:
                if expected[number][1]:
                    q = chapter_states[number]["quality"]
                    if finalizer is not None:
                        finalizer.submit(number, work / number, [], q)
                    else:
                        with recorder.span("finalize", chapter_id=str(number), device_id="cpu"):
                            _finish_chapter(project, number, work / number, [], config, q)
                        shutil.rmtree(work / number, ignore_errors=True)
                completed.add(number)
        _pump_queues()

        while not _stage_done():
            if finalizer is not None:
                finalizer.poll_completed()
            try:
                event = events.get(timeout=0.25)
            except queue.Empty:
                if finalizer is not None:
                    finalizer.poll_completed()
                if any(child.exitcode not in (None, 0) for child in started):
                    raise RuntimeError("Worker process failed. See worker traceback.")
                if all(child.exitcode is not None for child in started) and not _stage_done():
                    raise RuntimeError("Workers exited before completing stage")
                if (
                    worker_heartbeat_timeout_sec is not None
                    and float(worker_heartbeat_timeout_sec) > 0
                    and (time.monotonic() - last_activity) > float(worker_heartbeat_timeout_sec)
                ):
                    raise RuntimeError(f"Worker heartbeat stalled for >{int(worker_heartbeat_timeout_sec)}s")
                _pump_queues()
                continue

            last_activity = time.monotonic()
            kind, *rest = event
            if kind == "heartbeat":
                continue
            if kind == "error":
                raise RuntimeError(f"Worker execution failed: {rest[0]}")
            if kind == "span":
                stage, s_t0, s_t1, ch_num, dev_id, meta = rest
                recorder.record_stage(stage, s_t0, s_t1, chapter_id=str(ch_num), device_id=str(dev_id), metadata=meta)
                continue
            if kind == "tts_batch_done":
                gpu_id, batch_id, results = rest
                tts_in_flight = max(0, tts_in_flight - 1)
                batch_items = in_flight_batches.pop(batch_id)
                eff_sizes = [int(r.get("effective_batch_size", len(batch_items))) for r in results]
                if eff_sizes and min(eff_sizes) < len(batch_items):
                    dispatcher.record_oom_backoff(gpu_id, min(eff_sizes))
                touched = set()
                for item, res in _match_tts_results(batch_items, results):
                    st = chapter_states[item.chapter_number]
                    seg = st["script"].segments[item.segment_index]
                    seg.duration = res["duration"]
                    seg.checksum = res["sha256"]
                    seg.audio_path = item.output_path.relative_to(st["dir"]).as_posix()
                    seg.fingerprint = item.fingerprint
                    seg.voice_id = seg.synthesized_voice = item.voice
                    if seg.voice_source != "manual":
                        seg.voice_source = "tts"
                    seg.status = "synthesized"
                    st["ready_mask"][item.segment_index] = True
                    touched.add(item.chapter_number)
                dispatcher.mark_completed(u.key for u in batch_items)
                for ch_num in sorted(touched, key=lambda c: active_chapters.index(c)):
                    _advance_chapter_prefix(
                        ch_num,
                        chapter_states[ch_num],
                        produce=produce,
                        render=render,
                        pending_encode=pending_encode,
                        expected=expected,
                        prep_done=prep_done,
                        completed=completed,
                        recorder=recorder,
                    )
                _pump_queues()
                continue

            if kind == "shot":
                encode_in_flight = max(0, encode_in_flight - 1)
                number, index, path = rest[0], rest[1], rest[2]
                clips[number][index] = Path(path)
                if number in expected and len(clips[number]) == expected[number][0] and number not in completed:
                    if expected[number][1]:
                        clip_list = [clips[number][i] for i in range(len(clips[number]))]
                        q = chapter_states[number]["quality"]
                        if finalizer is not None:
                            finalizer.submit(number, work / number, clip_list, q)
                        else:
                            with recorder.span("finalize", chapter_id=str(number), device_id="cpu"):
                                _finish_chapter(project, number, work / number, clip_list, config, q)
                            shutil.rmtree(work / number, ignore_errors=True)
                            check_disk_low_water(work, min_free_mb=min_free_disk_mb)
                    completed.add(number)
                _pump_queues()

        _pump_queues()
        for child in started:
            child.join(timeout=30)
            if child.exitcode != 0:
                raise RuntimeError(f"Worker failed to exit cleanly: {child.exitcode}")
    finally:
        sampler.stop()
        recorder.set_sampler_summary(sampler.summarize())
        for child in started:
            _terminate_process_tree(child)
        if tts_jobs is not None:
            tts_jobs.close()
        if encode_jobs is not None:
            encode_jobs.close()
        events.close()


def run_pipeline(project, chapters, job, *, encoder="h264_nvenc", gpu_ids=(0, 1), render_workers=2,
                 batch_size=12, single_preview=True, telemetry=None,
                 worker_heartbeat_timeout_sec=1800.0, min_free_disk_mb=256,
                 policy="auto", usable_nvenc_devices=None):
    """Run scheduled single- or dual-GPU TTS and shot encoding pipeline cleanly."""
    project = Path(project).resolve()
    config = read_json(project / "project.json")
    workers = max(1, min(4, int(render_workers)))
    if job not in {"tts", "render", "produce"} or not chapters or batch_size < 1:
        raise ValueError("Invalid worker job")
    if not single_preview or any(number != "1" for number in chapters):
        require_preview_approval(project)
    raw_devices = list(gpu_ids)[:2] or [0]
    plan = resolve_execution_policy(
        policy,
        job=job,
        encoder=encoder,
        cuda_devices=raw_devices,
        usable_nvenc_devices=usable_nvenc_devices,
    )
    eff_encoder = plan.encoder

    recorder = telemetry or TelemetryRecorder(project_slug=project.name, policy=plan.policy, chapters=list(chapters))
    recorder.policy = plan.policy
    recorder.set_hardware({**(recorder._hardware or {}), "scheduler_policy": plan.as_dict()})

    if job in {"tts", "render"} or plan.policy != ExecutionPolicy.BASELINE_SPLIT:
        return _run_scheduled_pipeline(
            project,
            chapters,
            job,
            plan=plan,
            workers=workers,
            batch_size=batch_size,
            single_preview=single_preview,
            config=config,
            recorder=recorder,
            worker_heartbeat_timeout_sec=worker_heartbeat_timeout_sec,
            min_free_disk_mb=min_free_disk_mb,
        )

    devices = list(plan.encode_devices if eff_encoder == "h264_nvenc" else plan.tts_devices)[:2] or [0]
    if len(plan.tts_devices) >= 1 and len(plan.encode_devices) >= 2 and eff_encoder == "h264_nvenc":
        ordered = [plan.tts_devices[0]]
        for d in plan.encode_devices:
            if d not in ordered:
                ordered.append(d)
        devices = ordered[:2]

    with tempfile.TemporaryDirectory(prefix="manga-shots-") as directory:
        work = Path(directory)
        check_disk_low_water(work, min_free_mb=min_free_disk_mb)
        ctx = mp.get_context("spawn")
        # Only bind a cross-process job queue when a secondary encode consumer runs concurrently.
        jobs = ctx.Queue(maxsize=max(32, workers * 16)) if len(devices) == 2 else None
        events = ctx.Queue(maxsize=512)
        sampler = ResourceSampler(
            work_root=work,
            interval_sec=1.5,
            queue_depth_fn=lambda: {
                "jobs_qsize": _safe_qsize(jobs),
                "events_qsize": _safe_qsize(events),
            },
        )
        sampler.start()
        children = [ctx.Process(target=_audio_then_encode, args=(
            project, chapters, work, job, single_preview, config, eff_encoder, devices[0], len(devices),
            workers, jobs, events, batch_size,
        ))]
        if len(devices) == 2:
            children.append(ctx.Process(target=_encode_queue, args=(jobs, events, devices[1], workers)))

        expected = {}
        clips = {number: {} for number in chapters}
        completed = set()
        started = []
        last_activity = time.monotonic()
        heartbeats = {}
        finalizer = _AsyncChapterFinalizer(project, config, recorder, max_workers=1, min_free_disk_mb=min_free_disk_mb)
        finished_ok = False
        try:
            for child in children:
                child.start()
                started.append(child)
            while len(completed) < len(chapters):
                finalizer.poll_completed()
                try:
                    event = events.get(timeout=0.5)
                except queue.Empty:
                    finalizer.poll_completed()
                    if any(child.exitcode not in (None, 0) for child in started):
                        raise RuntimeError("Audio or visual process failed. See worker traceback.")
                    if all(child.exitcode is not None for child in started):
                        raise RuntimeError("Worker exited before reporting all chapters")
                    if (
                        worker_heartbeat_timeout_sec is not None
                        and float(worker_heartbeat_timeout_sec) > 0
                        and (time.monotonic() - last_activity) > float(worker_heartbeat_timeout_sec)
                    ):
                        raise RuntimeError(
                            f"Worker heartbeat stalled for >{int(worker_heartbeat_timeout_sec)}s"
                        )
                    continue
                last_activity = time.monotonic()
                kind, *rest = event
                if kind == "heartbeat":
                    worker_label, ts, _ = rest
                    heartbeats[worker_label] = ts
                    continue
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
                        finalizer.submit(
                            number,
                            work / number,
                            [clips[number][i] for i in range(len(clips[number]))],
                            quality,
                        )
                    completed.add(number)
            for child in started:
                child.join(timeout=30)
                if child.exitcode != 0:
                    raise RuntimeError(f"Worker failed to exit cleanly: {child.exitcode}")
            finalizer.wait_ordered(canonical_order=chapters)
            finished_ok = True
        finally:
            finalizer.close(cancel_pending=not finished_ok)
            sampler.stop()
            recorder.set_sampler_summary(sampler.summarize())
            for child in started:
                _terminate_process_tree(child)
            if jobs is not None:
                jobs.close()
            events.close()


def run(root, manifest_hash, output_dir, encoder="h264_nvenc", render_workers=2, compile_feature=False,
        policy="auto"):
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
    usable_nvenc = sorted(int(x) for x in (hardware.get("usable_nvenc_devices") or []))
    if encoder == "h264_nvenc" and (not devices or not usable_nvenc and not smoke_test_encoder("h264_nvenc")):
        encoder = "libx264"

    chapters = manifest["chapters"]
    single_preview = not manifest.get("series")
    effective_policy = manifest.get("cloud_policy") or policy
    run_pipeline(
        project,
        chapters,
        manifest["job_type"],
        encoder=encoder,
        gpu_ids=devices,
        render_workers=render_workers,
        single_preview=single_preview,
        telemetry=recorder,
        policy=effective_policy,
        usable_nvenc_devices=usable_nvenc if encoder == "h264_nvenc" else (),
    )
    out_path = Path(output_dir)
    with recorder.span("export_and_compile"):
        export_results(project, manifest, manifest_hash, out_path, compile_feature=compile_feature)
    save_telemetry_report(out_path / "cloud_telemetry.json", recorder.build_report())
