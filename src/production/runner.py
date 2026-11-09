"""
src/production/runner.py
SingleSessionProductionRunner: coordinates end-to-end 4-stage cloud production
(TTS -> Render -> Compile -> Export Bundle) with crash-resume checkpointing,
persistent TTS singleton reuse, verified hardware encoder, and telemetry profiling.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from src.cli.batch_cmd import discover_project_chapters, parse_chapter_list
from src.cli.compile_cmd import handle_compile
from src.cli.render_cmd import handle_render
from src.cli.tts_cmd import handle_tts
from src.core.logger import logger
from src.core.schemas import CheckpointStatus, RecapScript, utc_iso_now
from src.core.storage import (
    StorageFileNotFoundError,
    atomic_write_json,
    format_chapter_filename,
    get_chapter_dir,
    get_output_dir,
    get_project_dir,
    load_chapter_metadata,
    read_json,
    stage_chapter_export,
)
from src.core.utils import get_project_data_dir
from src.production.hardware import resolve_video_encoder
from src.production.schemas import (
    ChapterProductionProgress,
    ProductionJobConfig,
    ProductionJobManifest,
    ProductionJobTelemetry,
    ProductionStageStatus,
)
from src.tts.engine import EngineMode, get_tts_engine


class SingleSessionProductionRunner:
    """
    Executes a single-session cloud production run across multiple chapters.
    Designed for zero-reinitialization overhead on Kaggle and Google Colab.
    """

    def __init__(
        self,
        manifest: ProductionJobManifest,
        job_file_path: Union[str, Path],
    ):
        self.manifest = manifest
        self.job_file_path = Path(job_file_path).resolve()
        self.project_dir = get_project_dir(manifest.project_slug)
        if not self.project_dir.exists():
            fallback_dir = get_project_data_dir(manifest.project_slug)
            if fallback_dir.exists():
                self.project_dir = fallback_dir

    @classmethod
    def load_or_create(
        cls,
        project_slug: str,
        config: Optional[ProductionJobConfig] = None,
        job_file_path: Optional[Union[str, Path]] = None,
        force: bool = False,
    ) -> SingleSessionProductionRunner:
        """
        Loads an existing production job manifest to resume or initializes a fresh one.
        """
        p_dir = get_project_dir(project_slug)
        if not p_dir.exists():
            fallback_dir = get_project_data_dir(project_slug)
            if fallback_dir.exists():
                p_dir = fallback_dir
            else:
                raise StorageFileNotFoundError(f"Project directory not found for: {project_slug}")

        job_path = Path(job_file_path).resolve() if job_file_path else (p_dir / "production_job.json")

        if not force and job_path.exists():
            try:
                manifest = ProductionJobManifest.load(job_path)
                logger.info(f"Loaded existing production job manifest from {job_path} (status: {manifest.status})")
                return cls(manifest=manifest, job_file_path=job_path)
            except Exception as e:
                logger.warning(f"Failed to load existing job file {job_path}: {e}. Creating new manifest.")

        if config is None:
            # Auto-discover chapters if none specified
            chaps = discover_project_chapters(p_dir)
            config = ProductionJobConfig(
                project_slug=project_slug,
                target_chapters=chaps,
            )

        # Initialize chapters progress
        chapters_prog: Dict[str, ChapterProductionProgress] = {}
        for c in config.target_chapters:
            chapters_prog[str(c)] = ChapterProductionProgress(chapter_number=str(c))

        manifest = ProductionJobManifest(
            project_slug=project_slug,
            config=config,
            chapters=chapters_prog,
        )
        manifest.save(job_path)
        return cls(manifest=manifest, job_file_path=job_path)

    def _save_checkpoint(self) -> None:
        """Atomically saves manifest state and guarantees zero secret leakage."""
        self.manifest.audit_secrets()
        self.manifest.save(self.job_file_path)

    @staticmethod
    def _is_valid_wav_clip(path: Path) -> bool:
        """Strictly validates that a WAV file exists, has valid RIFF/WAVE header, and audio frames."""
        if not path.exists() or path.stat().st_size <= 44:
            return False
        try:
            with open(path, "rb") as f:
                header = f.read(12)
                if len(header) < 12 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
                    return False
            import wave
            with wave.open(str(path), "rb") as wf:
                return wf.getnframes() > 0 and wf.getframerate() > 0 and wf.getnchannels() > 0
        except Exception:
            return False

    @staticmethod
    def _is_valid_mp4_video(path: Path) -> bool:
        """Strictly validates MP4 container box header and uses ffprobe for stream, codec, and duration verification."""
        if not path.exists() or path.stat().st_size < 1024:
            return False
        try:
            header = path.read_bytes()[:64]
            has_box = (
                b"ftyp" in header or b"moov" in header or
                (len(header) >= 8 and header[4:8] in (b"ftyp", b"moov", b"mdat"))
            )
            if not has_box:
                return False
        except Exception:
            return False

        ffprobe_bin = shutil.which("ffprobe")
        if ffprobe_bin:
            try:
                import subprocess
                cmd = [
                    ffprobe_bin,
                    "-v", "error",
                    "-show_entries", "stream=codec_type,codec_name:format=duration",
                    "-of", "json",
                    str(path)
                ]
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                if res.returncode != 0:
                    return False
                probe_data = json.loads(res.stdout)
                streams = probe_data.get("streams", [])
                has_video_stream = any(s.get("codec_type") == "video" and s.get("codec_name") for s in streams)
                if not has_video_stream:
                    return False
                dur = float(probe_data.get("format", {}).get("duration", 0.0))
                if dur <= 0.0:
                    return False
                return True
            except Exception:
                return False

        return True

    def _is_chapter_audio_complete(self, chapter_num: Union[str, int]) -> bool:
        """Checks if all segments in script.json already have valid audio clips on disk matching checksums."""
        chap_dir = get_chapter_dir(self.manifest.project_slug, str(chapter_num))
        if not chap_dir.exists():
            fallback = self.project_dir / "chapters" / str(chapter_num)
            if fallback.exists():
                chap_dir = fallback
            else:
                return False
        script_file = chap_dir / "script.json"
        if not script_file.exists():
            return False
        try:
            data = read_json(script_file)
            script = RecapScript.model_validate(data)
            if not script.segments:
                return False
            for seg in script.segments:
                wav_p = None
                if seg.audio_path:
                    cand = Path(seg.audio_path)
                    if not cand.is_absolute():
                        cand = chap_dir / cand
                    if self._is_valid_wav_clip(cand):
                        wav_p = cand
                if not wav_p:
                    cand = chap_dir / "audio" / "clips" / f"audio_{chapter_num}_{seg.seq_id}.wav"
                    if self._is_valid_wav_clip(cand):
                        wav_p = cand
                if not wav_p:
                    return False
                # Fix S1.3: Verify checksum match if present in script
                if seg.checksum:
                    actual_sha = hashlib.sha256(wav_p.read_bytes()).hexdigest()
                    if actual_sha != seg.checksum:
                        return False
            return True
        except Exception:
            return False

    def _is_chapter_render_complete(self, chapter_num: Union[str, int]) -> bool:
        """Checks if final MP4 video already exists and chapter status is completed."""
        chap_dir = get_chapter_dir(self.manifest.project_slug, str(chapter_num))
        if not chap_dir.exists():
            fallback = self.project_dir / "chapters" / str(chapter_num)
            if fallback.exists():
                chap_dir = fallback
            else:
                return False

        # Fix S1.3: Checkpoint must verify completion status with valid metadata
        chap_meta_file = chap_dir / "chapter.json"
        if not chap_meta_file.exists():
            return False
        try:
            chap_meta = load_chapter_metadata(chap_dir)
            if (
                chap_meta.technical_status != CheckpointStatus.COMPLETED
                and chap_meta.overall_status != CheckpointStatus.COMPLETED
            ):
                return False
        except Exception:
            return False

        final_mp4 = chap_dir / "video" / "final.mp4"
        if self._is_valid_mp4_video(final_mp4):
            return True
        staged_mp4 = get_output_dir(self.manifest.project_slug) / format_chapter_filename(chapter_num)
        return bool(self._is_valid_mp4_video(staged_mp4))

    def run(self) -> ProductionJobManifest:
        """
        Executes the 4-stage cloud production pipeline:
        Stage 1: Batch TTS (Persistent Singleton Engine)
        Stage 2: Batch Render (Verified Encoder + Clip Cache)
        Stage 3: Feature Compilation (Multi-Chapter Concatenation)
        Stage 4: Standalone Export Bundle & Telemetry Logging
        """
        wall_start_time = time.monotonic()
        cfg = self.manifest.config

        # Check if already completed and not force
        if self.manifest.status == ProductionStageStatus.COMPLETED and not cfg.force:
            logger.info("Production job already marked as COMPLETED. Returning cached manifest.")
            return self.manifest

        self.manifest.status = ProductionStageStatus.RUNNING
        self._save_checkpoint()

        logger.info(f"=== Starting Single-Session Cloud Production Job: {self.manifest.job_id} ===")
        logger.info(f"Project: {cfg.project_slug} | Chapters: {cfg.target_chapters}")

        # --- Stage 0: Hardware Encoder Resolution ---
        resolved_enc, codec_args, enc_msg = resolve_video_encoder(cfg.encoder)
        self.manifest.telemetry.resolved_encoder = resolved_enc
        self.manifest.telemetry.encoder_status_message = enc_msg
        logger.info(f"Resolved Video Encoder: {resolved_enc} ({enc_msg})")
        self._save_checkpoint()

        # Check existing audio artifacts before triggering TTS stage
        if not cfg.force:
            for chap_num in cfg.target_chapters:
                c_prog = self.manifest.chapters.get(str(chap_num))
                if c_prog and c_prog.tts_status != ProductionStageStatus.COMPLETED:
                    if self._is_chapter_audio_complete(chap_num):
                        c_prog.tts_status = ProductionStageStatus.COMPLETED
                        logger.info(f"[TTS] Chapter {chap_num} audio clips already exist on disk. Reusing audio cache.")
            self._save_checkpoint()

        # =====================================================================
        # Stage 1: Batch TTS with Persistent Singleton Engine
        # =====================================================================
        tts_stage_needed = cfg.force or any(
            p.tts_status != ProductionStageStatus.COMPLETED
            for p in self.manifest.chapters.values()
        )

        if tts_stage_needed:
            self.manifest.current_stage = "tts"
            self._save_checkpoint()
            logger.info("--- [Stage 1/4] Starting Persistent Batch TTS ---")

            mode_enum = EngineMode.MOCK if cfg.tts_engine == "mock" else EngineMode.NEURAL
            t_init_0 = time.monotonic()
            engine = get_tts_engine(force_mode=mode_enum)
            init_dur = getattr(engine, "init_duration_seconds", round(time.monotonic() - t_init_0, 3))
            self.manifest.telemetry.tts_model_init_seconds = init_dur
            logger.info(f"Persistent VieNeu-TTS Engine ready (mode={engine.mode}, init_time={init_dur:.3f}s)")

            t_tts_stage_start = time.monotonic()
            for chap_num in cfg.target_chapters:
                c_prog = self.manifest.chapters.get(str(chap_num))
                if not c_prog:
                    c_prog = ChapterProductionProgress(chapter_number=str(chap_num))
                    self.manifest.chapters[str(chap_num)] = c_prog

                if c_prog.tts_status == ProductionStageStatus.COMPLETED and not cfg.force:
                    logger.info(f"[TTS] Chapter {chap_num} already completed. Skipping.")
                    continue

                c_prog.tts_status = ProductionStageStatus.RUNNING
                self._save_checkpoint()

                t_ch_start = time.monotonic()
                try:
                    tts_args = argparse.Namespace(
                        project=cfg.project_slug,
                        chapter=chap_num,
                        engine=cfg.tts_engine,
                        voice=cfg.voice_override,
                        speed=cfg.speed_override,
                        workers=cfg.tts_workers,
                        force=cfg.force,
                    )
                    tts_res = handle_tts(tts_args)
                    c_prog.tts_status = ProductionStageStatus.COMPLETED
                    c_prog.tts_duration_seconds = round(time.monotonic() - t_ch_start, 3)
                    c_prog.tts_segments_synthesized = tts_res.get("segments_synthesized", 0)
                    c_prog.tts_segments_cached = tts_res.get("segments_cached", 0)
                    c_prog.error = None
                    logger.info(
                        f"[TTS] Chapter {chap_num} completed in {c_prog.tts_duration_seconds:.2f}s "
                        f"(synthesized: {c_prog.tts_segments_synthesized}, cached: {c_prog.tts_segments_cached})"
                    )
                except Exception as e:
                    c_prog.tts_status = ProductionStageStatus.FAILED
                    c_prog.error = f"TTS error: {e}"
                    logger.error(f"[TTS] Chapter {chap_num} failed: {e}")

                self._save_checkpoint()

            self.manifest.telemetry.tts_total_seconds = round(time.monotonic() - t_tts_stage_start, 3)
            self.manifest.telemetry.tts_inference_seconds = round(getattr(engine, "total_inference_seconds", 0.0), 3)
            self._save_checkpoint()
        else:
            logger.info("--- [Stage 1/4] Batch TTS already completed for all chapters. Skipping. ---")

        # Check existing render artifacts before triggering Render stage
        if not cfg.force:
            for chap_num in cfg.target_chapters:
                c_prog = self.manifest.chapters.get(str(chap_num))
                if c_prog and c_prog.render_status != ProductionStageStatus.COMPLETED:
                    if self._is_chapter_render_complete(chap_num):
                        c_prog.render_status = ProductionStageStatus.COMPLETED
                        logger.info(f"[Render] Chapter {chap_num} final video already exists on disk. Reusing render cache.")
            self._save_checkpoint()

        # =====================================================================
        # Stage 2: Batch Render with Verified Encoder & Clip Cache
        # =====================================================================
        render_stage_needed = cfg.force or any(
            p.render_status != ProductionStageStatus.COMPLETED
            for p in self.manifest.chapters.values()
        )

        if render_stage_needed:
            self.manifest.current_stage = "render"
            self._save_checkpoint()
            logger.info("--- [Stage 2/4] Starting Batch Render ---")

            t_render_stage_start = time.monotonic()
            for chap_num in cfg.target_chapters:
                c_prog = self.manifest.chapters.get(str(chap_num))
                if not c_prog:
                    continue

                if c_prog.render_status == ProductionStageStatus.COMPLETED and not cfg.force:
                    logger.info(f"[Render] Chapter {chap_num} already rendered. Skipping.")
                    continue

                if c_prog.tts_status == ProductionStageStatus.FAILED:
                    logger.warning(f"[Render] Skipping Chapter {chap_num} because TTS stage failed.")
                    continue

                c_prog.render_status = ProductionStageStatus.RUNNING
                self._save_checkpoint()

                t_ch_render = time.monotonic()
                try:
                    render_args = argparse.Namespace(
                        project=cfg.project_slug,
                        chapter=chap_num,
                        quality=cfg.quality,
                        suffix="final",
                        encoder=resolved_enc,
                        bgm=cfg.bgm_path,
                        burn_subtitles=cfg.burn_subtitles,
                        animation=cfg.animation,
                        video_mode=cfg.video_mode,
                        force=cfg.force,
                        skip_preview=True,
                        approved=True,
                        allow_placeholders=False,
                        workers=cfg.render_workers,
                        cleanup=cfg.cleanup_intermediates,
                        subtitle_lines=cfg.subtitle_lines,
                        subtitle_font_size=cfg.subtitle_font_size,
                        subtitle_chunking=cfg.subtitle_chunking,
                    )
                    render_res = handle_render(render_args)
                    c_prog.render_status = ProductionStageStatus.COMPLETED
                    c_prog.render_duration_seconds = round(time.monotonic() - t_ch_render, 3)
                    c_prog.cached_clips_count = render_res.get("cached_clips_count", 0)
                    c_prog.rendered_clips_count = render_res.get("rendered_clips_count", 0)
                    c_prog.video_path = render_res.get("video_path")
                    c_prog.subtitle_path = render_res.get("subtitle_path")
                    c_prog.export_video_path = render_res.get("export_video_path")
                    c_prog.export_subtitle_path = render_res.get("export_subtitle_path")
                    c_prog.error = None
                    logger.info(
                        f"[Render] Chapter {chap_num} rendered in {c_prog.render_duration_seconds:.2f}s "
                        f"(rendered: {c_prog.rendered_clips_count}, cached: {c_prog.cached_clips_count})"
                    )
                except Exception as e:
                    c_prog.render_status = ProductionStageStatus.FAILED
                    c_prog.error = f"Render error: {e}"
                    logger.error(f"[Render] Chapter {chap_num} failed: {e}")

                self._save_checkpoint()

            self.manifest.telemetry.render_total_seconds = round(time.monotonic() - t_render_stage_start, 3)
            self._save_checkpoint()
        else:
            logger.info("--- [Stage 2/4] Batch Render already completed for all chapters. Skipping. ---")

        # =====================================================================
        # Stage 3: Feature Compilation (Multi-Chapter Concatenation)
        # =====================================================================
        completed_chaps = [
            c_num for c_num, p in self.manifest.chapters.items()
            if p.render_status == ProductionStageStatus.COMPLETED
        ]

        if cfg.compile and completed_chaps:
            self.manifest.current_stage = "compile"
            self._save_checkpoint()
            logger.info(f"--- [Stage 3/4] Starting Feature Compilation ({len(completed_chaps)} chapters) ---")

            t_compile_start = time.monotonic()
            try:
                # Sort numerically
                def sort_key(x: str):
                    try:
                        return (0, int(x))
                    except ValueError:
                        return (1, x)

                sorted_completed = sorted(completed_chaps, key=sort_key)
                compile_args = argparse.Namespace(
                    project=cfg.project_slug,
                    chapters=",".join(sorted_completed),
                    output=None,
                    approved=True,
                    extract_audio=True,
                )
                comp_res = handle_compile(compile_args)
                self.manifest.compiled_video_path = comp_res.get("compiled_video")
                self.manifest.feature_timeline_path = comp_res.get("feature_timeline")
                self.manifest.master_audio_path = comp_res.get("compiled_audio")
                logger.info(f"[Compile] Master feature video ready: {self.manifest.compiled_video_path}")
            except Exception as e:
                logger.error(f"[Compile] Feature compilation failed: {e}")
                self.manifest.error = f"Compilation error: {e}"

            self.manifest.telemetry.compile_total_seconds = round(time.monotonic() - t_compile_start, 3)
            self._save_checkpoint()
        elif not cfg.compile:
            logger.info("--- [Stage 3/4] Feature Compilation disabled in config. Skipping. ---")

        # =====================================================================
        # Stage 4: Standalone Export Bundle & Telemetry Logging
        # =====================================================================
        if cfg.export_bundle:
            self.manifest.current_stage = "export"
            self._save_checkpoint()
            logger.info("--- [Stage 4/4] Generating Standalone Export Bundle ---")

            t_export_start = time.monotonic()
            out_dir = get_output_dir(cfg.project_slug)
            bundle_dir = out_dir / "export_bundle"
            bundle_dir.mkdir(parents=True, exist_ok=True)
            self.manifest.export_bundle_dir = str(bundle_dir)

            collected_artifacts: List[str] = []

            # 1. Copy or stage chapter video & subtitle files
            for c_num, p in self.manifest.chapters.items():
                if p.render_status == ProductionStageStatus.COMPLETED and p.video_path:
                    v_src = Path(p.video_path)
                    if v_src.exists():
                        target_v = bundle_dir / format_chapter_filename(c_num, ext="mp4", padded=True)
                        if not target_v.exists() or cfg.force:
                            shutil.copy2(v_src, target_v)
                        collected_artifacts.append(str(target_v))

                    if p.subtitle_path:
                        s_src = Path(p.subtitle_path)
                        if s_src.exists():
                            target_s = bundle_dir / format_chapter_filename(c_num, ext="srt", padded=True)
                            if not target_s.exists() or cfg.force:
                                shutil.copy2(s_src, target_s)
                            collected_artifacts.append(str(target_s))

            # 2. Copy compiled feature video and timeline
            if self.manifest.compiled_video_path:
                cv_src = Path(self.manifest.compiled_video_path)
                if cv_src.exists():
                    target_cv = bundle_dir / f"{cfg.project_slug}_full_feature.mp4"
                    if not target_cv.exists() or cfg.force:
                        shutil.copy2(cv_src, target_cv)
                    collected_artifacts.append(str(target_cv))

            if self.manifest.feature_timeline_path:
                tl_src = Path(self.manifest.feature_timeline_path)
                if tl_src.exists():
                    target_tl = bundle_dir / "feature_timeline.json"
                    if not target_tl.exists() or cfg.force:
                        shutil.copy2(tl_src, target_tl)
                    collected_artifacts.append(str(target_tl))

            if self.manifest.master_audio_path:
                ma_src = Path(self.manifest.master_audio_path)
                if ma_src.exists():
                    target_ma = bundle_dir / "master_audio.wav"
                    if not target_ma.exists() or cfg.force:
                        shutil.copy2(ma_src, target_ma)
                    collected_artifacts.append(str(target_ma))

            # 3. Copy thumbnails if present in project or chapters
            thumb_files = list(self.project_dir.glob("*thumb*.*")) + list(self.project_dir.glob("chapters/*/*thumb*.*"))
            for tf in thumb_files[:10]:
                if tf.is_file():
                    target_tf = bundle_dir / tf.name
                    if not target_tf.exists():
                        shutil.copy2(tf, target_tf)
                    collected_artifacts.append(str(target_tf))

            # 4. Copy production_job.json manifest itself
            target_manifest = bundle_dir / "production_job.json"
            collected_artifacts.append(str(target_manifest))

            self.manifest.artifacts = collected_artifacts
            self.manifest.telemetry.export_total_seconds = round(time.monotonic() - t_export_start, 3)
            self._save_checkpoint()
            # Also save a copy inside bundle_dir
            self.manifest.save(target_manifest)

        # =====================================================================
        # Finalization & Telemetry Aggregation
        # =====================================================================
        self.manifest.telemetry.total_wall_time_seconds = round(time.monotonic() - wall_start_time, 3)

        # Evaluate overall job status
        failed_count = sum(1 for p in self.manifest.chapters.values() if p.render_status == ProductionStageStatus.FAILED)
        total_chaps = len(self.manifest.chapters)
        if failed_count == total_chaps and total_chaps > 0:
            self.manifest.status = ProductionStageStatus.FAILED
            self.manifest.current_stage = "failed"
        else:
            self.manifest.status = ProductionStageStatus.COMPLETED
            self.manifest.current_stage = "completed"

        self.manifest.completed_at = utc_iso_now()
        self._save_checkpoint()

        logger.info(
            f"=== Single-Session Production Job Finished in {self.manifest.telemetry.total_wall_time_seconds:.2f}s "
            f"(Status: {self.manifest.status}, Encoder: {self.manifest.telemetry.resolved_encoder}) ==="
        )
        return self.manifest
