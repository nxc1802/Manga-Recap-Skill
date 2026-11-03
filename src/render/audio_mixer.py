"""
src/render/audio_mixer.py
Audio Concatenation, BGM Sidechain Ducking, and Master Audio Mixing Engine for Video Engine V2.
Performs deterministic 48 kHz 16-bit PCM WAV concatenation and dynamic sidechain compression with peak limiting.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from src.core.logger import logger
from src.core.schemas import ScriptSegment
from src.core.utils import run_ffmpeg
from src.tts.engine import get_wav_duration_seconds


@dataclass
class SpeechTimelineEntry:
    """Exact ground-truth timeline entry for a single synthesized speech utterance."""
    seq_id: str
    start_sec: float
    end_sec: float
    duration_sec: float
    text: str
    audio_path: Path


class AudioMixer:
    """
    Concatenates speech utterances with sample-level accuracy, loops background music,
    applies sidechain dynamic volume ducking, and enforces peak limiting.
    """

    def __init__(
        self,
        target_sample_rate: int = 48000,
        target_channels: int = 2,
        silence_padding_sec: float = 0.20,
        tail_padding_sec: float = 0.50,
    ):
        self.target_sample_rate = target_sample_rate
        self.target_channels = target_channels
        self.silence_padding_sec = silence_padding_sec
        self.tail_padding_sec = tail_padding_sec

    def concatenate_speech(
        self,
        segments: List[ScriptSegment],
        chapter_dir: Union[str, Path],
        output_wav_path: Union[str, Path],
    ) -> Tuple[Path, List[SpeechTimelineEntry]]:
        """
        Concatenates utterance WAV files from script segments into a master voice track.
        Injects silence padding (default 0.20s) between utterances and tail breathing room.
        Returns:
            Tuple[master_wav_path, timeline_entries]
        """
        out_p = Path(output_wav_path).resolve()
        out_p.parent.mkdir(parents=True, exist_ok=True)
        chap_p = Path(chapter_dir).resolve()

        timeline: List[SpeechTimelineEntry] = []
        valid_items: List[Tuple[ScriptSegment, Path]] = []

        for seg in segments:
            if not seg.audio_path:
                continue
            p = chap_p / seg.audio_path if not Path(seg.audio_path).is_absolute() else Path(seg.audio_path)
            if p.exists() and p.stat().st_size > 44:
                valid_items.append((seg, p))
            else:
                logger.warning(f"Utterance audio file not found: {p}. Skipping segment {seg.seq_id}.")

        if not valid_items:
            raise FileNotFoundError(f"No valid utterance WAV files found in chapter directory: {chap_p}")

        # Check format uniformity
        first_p = valid_items[0][1]
        with wave.open(str(first_p), "rb") as wf:
            nchannels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            framerate = wf.getframerate()

        uniform_format = True
        for _, p in valid_items[1:]:
            with wave.open(str(p), "rb") as wf:
                if (wf.getnchannels(), wf.getsampwidth(), wf.getframerate()) != (nchannels, sampwidth, framerate):
                    uniform_format = False
                    break

        silence_samples = int(round(self.silence_padding_sec * framerate))
        silence_bytes = b"\x00" * (silence_samples * sampwidth * nchannels) if silence_samples > 0 else b""

        tail_samples = int(round(self.tail_padding_sec * framerate))
        tail_bytes = b"\x00" * (tail_samples * sampwidth * nchannels) if tail_samples > 0 else b""

        current_time_sec = 0.0

        if uniform_format:
            # Fast in-process PCM binary stitching
            with wave.open(str(out_p), "wb") as out_wf:
                out_wf.setnchannels(nchannels)
                out_wf.setsampwidth(sampwidth)
                out_wf.setframerate(framerate)

                for idx, (seg, p) in enumerate(valid_items):
                    with wave.open(str(p), "rb") as in_wf:
                        n_frames = in_wf.getnframes()
                        frames = in_wf.readframes(n_frames)
                        dur_sec = round(n_frames / float(framerate), 4)

                    start_sec = round(current_time_sec, 4)
                    end_sec = round(start_sec + dur_sec, 4)

                    timeline.append(
                        SpeechTimelineEntry(
                            seq_id=str(seg.seq_id),
                            start_sec=start_sec,
                            end_sec=end_sec,
                            duration_sec=dur_sec,
                            text=str(seg.narrator_text or ""),
                            audio_path=p,
                        )
                    )

                    out_wf.writeframes(frames)
                    current_time_sec = end_sec

                    # Inter-segment silence
                    if idx < len(valid_items) - 1 and silence_bytes:
                        out_wf.writeframes(silence_bytes)
                        current_time_sec = round(current_time_sec + self.silence_padding_sec, 4)

                # Tail breathing room
                if tail_bytes:
                    out_wf.writeframes(tail_bytes)
                    current_time_sec = round(current_time_sec + self.tail_padding_sec, 4)

            logger.info(
                f"Fast PCM concatenation completed: {len(valid_items)} utterances -> {out_p} "
                f"({current_time_sec:.2f}s total duration)"
            )
            return out_p, timeline
        else:
            # Format normalized concat via FFmpeg
            logger.warning("Varying audio formats detected; normalizing via FFmpeg concat filter.")
            filter_inputs = []
            filter_parts = []
            for i, (seg, p) in enumerate(valid_items):
                filter_inputs.extend(["-i", str(p)])
                filter_parts.append(f"[{i}:a]aresample={self.target_sample_rate},aformat=sample_fmts=s16:channel_layouts=stereo[a{i}];")

            concat_str = "".join(f"[a{i}]" for i in range(len(valid_items))) + f"concat=n={len(valid_items)}:v=0:a=1[outa]"
            cmd = [
                "ffmpeg", "-y",
                *filter_inputs,
                "-filter_complex", "".join(filter_parts) + concat_str,
                "-map", "[outa]",
                "-c:a", "pcm_s16le",
                str(out_p),
            ]
            run_ffmpeg(cmd, description="Audio format-normalizing concatenation")

            # Build timeline from individual files
            for idx, (seg, p) in enumerate(valid_items):
                dur_sec = get_wav_duration_seconds(p)
                start_sec = round(current_time_sec, 4)
                end_sec = round(start_sec + dur_sec, 4)
                timeline.append(
                    SpeechTimelineEntry(
                        seq_id=str(seg.seq_id),
                        start_sec=start_sec,
                        end_sec=end_sec,
                        duration_sec=dur_sec,
                        text=str(seg.narrator_text or ""),
                        audio_path=p,
                    )
                )
                current_time_sec = round(end_sec + self.silence_padding_sec, 4)

            return out_p, timeline

    def concatenate_speech_utterances(
        self,
        audio_paths: List[Union[str, Path]],
        output_path: Union[str, Path],
        inter_segment_pause: float = 0.0,
    ) -> Tuple[Path, float]:
        """
        Concatenates uncompressed WAV files directly in pure Python PCM domain.
        Returns (output_path, total_duration_seconds).
        """
        out_p = Path(output_path).resolve()
        out_p.parent.mkdir(parents=True, exist_ok=True)

        valid_paths = [Path(p).resolve() for p in audio_paths if Path(p).exists() and Path(p).stat().st_size > 44]
        if not valid_paths:
            raise FileNotFoundError("No valid WAV files found to concatenate.")

        with wave.open(str(valid_paths[0]), "rb") as first_wf:
            nchannels = first_wf.getnchannels()
            sampwidth = first_wf.getsampwidth()
            framerate = first_wf.getframerate()

        uniform_format = True
        for p in valid_paths[1:]:
            with wave.open(str(p), "rb") as wf:
                if (wf.getnchannels(), wf.getsampwidth(), wf.getframerate()) != (nchannels, sampwidth, framerate):
                    uniform_format = False
                    break

        total_frames = 0
        pause_frames = int(round(inter_segment_pause * framerate))
        pause_bytes = b"\x00" * (pause_frames * sampwidth * nchannels) if pause_frames > 0 else b""

        if uniform_format:
            with wave.open(str(out_p), "wb") as out_wf:
                out_wf.setnchannels(nchannels)
                out_wf.setsampwidth(sampwidth)
                out_wf.setframerate(framerate)

                for idx, p in enumerate(valid_paths):
                    with wave.open(str(p), "rb") as in_wf:
                        frames = in_wf.readframes(in_wf.getnframes())
                        out_wf.writeframes(frames)
                        total_frames += in_wf.getnframes()

                    if idx < len(valid_paths) - 1 and pause_bytes:
                        out_wf.writeframes(pause_bytes)
                        total_frames += pause_frames

            total_duration = round(total_frames / float(framerate), 4)
            return out_p, total_duration
        else:
            filter_inputs = []
            filter_parts = []
            for i, p in enumerate(valid_paths):
                filter_inputs.extend(["-i", str(p)])
                filter_parts.append(f"[{i}:a]aresample=48000,aformat=sample_fmts=s16:channel_layouts=stereo[a{i}];")

            concat_str = "".join(f"[a{i}]" for i in range(len(valid_paths))) + f"concat=n={len(valid_paths)}:v=0:a=1[outa]"
            cmd = [
                "ffmpeg", "-y",
                *filter_inputs,
                "-filter_complex", "".join(filter_parts) + concat_str,
                "-map", "[outa]",
                "-c:a", "pcm_s16le",
                str(out_p),
            ]
            run_ffmpeg(cmd, description="FFmpeg audio concat")
            total_duration = get_wav_duration_seconds(out_p)
            return out_p, total_duration

    def mix_audio_track(
        self,
        speech_wav_path: Union[str, Path],
        output_path: Union[str, Path],
        bgm_path: Optional[Union[str, Path]] = None,
        bgm_volume: float = 0.15,
        voice_volume: float = 1.0,
        ducking_ratio: float = 5.0,
        fadeout_seconds: float = 2.0,
        audio_codec: str = "aac",
        audio_bitrate: str = "192k",
    ) -> Path:
        """
        Mixes speech master track with ducked background music.
        Employs sidechaincompress (threshold=0.08, ratio=5.0, attack=80, release=500)
        and alimiter (limit=0.95) to prevent digital clipping.
        """
        speech_p = Path(speech_wav_path).resolve()
        if not speech_p.exists():
            raise FileNotFoundError(f"Speech audio file not found: {speech_p}")

        total_duration = get_wav_duration_seconds(speech_p)
        out_p = Path(output_path).resolve()
        out_p.parent.mkdir(parents=True, exist_ok=True)

        fade_st = max(0.0, total_duration - fadeout_seconds)

        # Case 1: No BGM provided or BGM file missing
        if not bgm_path or not Path(bgm_path).exists():
            logger.info("No BGM track provided; outputting speech narration directly.")
            filter_str = f"volume={voice_volume:.2f},alimiter=limit=0.95:attack=5:release=50,afade=t=out:st={fade_st:.3f}:d={fadeout_seconds:.3f}"
            cmd = [
                "ffmpeg", "-y",
                "-i", str(speech_p),
                "-filter_complex", filter_str,
                "-c:a", audio_codec,
                "-b:a", audio_bitrate,
                "-ar", str(self.target_sample_rate),
                "-ac", str(self.target_channels),
                str(out_p),
            ]
            run_ffmpeg(cmd, description="Encode speech-only master audio")
            return out_p

        # Case 2: Mix BGM with dynamic sidechain ducking
        bgm_p = Path(bgm_path).resolve()
        logger.info(
            f"Mixing audio track: speech ({total_duration:.2f}s) + BGM {bgm_p.name} "
            f"(volume {bgm_volume:.2f}, ducking ratio {ducking_ratio})"
        )

        filter_complex = (
            f"[1:a]volume={bgm_volume:.3f}[bgm_base];"
            f"[bgm_base][0:a]sidechaincompress=threshold=0.08:ratio={ducking_ratio:.1f}:attack=80:release=500:knee=2.8[ducked_bgm];"
            f"[0:a]volume={voice_volume:.2f}[speech];"
            f"[speech][ducked_bgm]amix=inputs=2:duration=first:dropout_transition=2:weights=1.0 1.0[mixed];"
            f"[mixed]alimiter=limit=0.95:attack=5:release=50[limited];"
            f"[limited]afade=t=out:st={fade_st:.3f}:d={fadeout_seconds:.3f}[outa]"
        )

        cmd = [
            "ffmpeg", "-y",
            "-i", str(speech_p),
            "-stream_loop", "-1", "-i", str(bgm_p),
            "-filter_complex", filter_complex,
            "-map", "[outa]",
            "-t", f"{total_duration:.3f}",
            "-c:a", audio_codec,
            "-b:a", audio_bitrate,
            "-ar", str(self.target_sample_rate),
            "-ac", str(self.target_channels),
            str(out_p),
        ]
        run_ffmpeg(cmd, description="Mix ducked BGM and speech")

        if not out_p.exists() or out_p.stat().st_size == 0:
            raise RuntimeError(f"Mixed master audio output is empty or missing: {out_p}")

        return out_p

    def mix_bgm_with_ducking(
        self,
        voice_wav_path: Union[str, Path],
        bgm_path: Optional[Union[str, Path]],
        total_duration_sec: float,
        output_wav_path: Union[str, Path],
        bgm_volume: float = 0.15,
        ducking_ratio: float = 5.0,
    ) -> Path:
        """Alias for compatibility with Explorer 3 interface contract."""
        return self.mix_audio_track(
            speech_wav_path=voice_wav_path,
            output_path=output_wav_path,
            bgm_path=bgm_path,
            bgm_volume=bgm_volume,
            ducking_ratio=ducking_ratio,
            audio_codec="pcm_s16le",
        )

    def verify_no_clipping(self, audio_path: Union[str, Path]) -> Tuple[bool, float]:
        """
        Uses FFmpeg volumedetect to inspect the master audio peak level.
        Returns (is_clean, max_volume_db). If max_volume_db <= 0.0, output has no digital clipping.
        """
        p = Path(audio_path).resolve()
        if not p.exists():
            raise FileNotFoundError(f"Audio file not found: {p}")

        cmd = [
            "ffmpeg", "-i", str(p),
            "-filter_complex", "[0:a]volumedetect",
            "-f", "null", "-",
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        max_vol = 0.0
        for line in res.stderr.splitlines():
            if "max_volume:" in line:
                match = re.search(r"max_volume:\s*(-?[\d\.]+)\s*dB", line)
                if match:
                    max_vol = float(match.group(1))
                    break

        is_clean = max_vol <= 0.0
        return is_clean, max_vol
