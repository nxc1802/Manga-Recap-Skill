"""Semantic artifact validation. Exceptions are fatal; existence is never success."""
from __future__ import annotations
import hashlib
import json
import math
import re
import subprocess
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import numpy as np


class ArtifactValidationError(ValueError):
    pass


def fingerprint(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_audio(path, minimum_duration=0.05, minimum_rms=1e-5):
    try:
        with wave.open(str(path), "rb") as source:
            channels, rate, width, count = source.getnchannels(), source.getframerate(), source.getsampwidth(), source.getnframes()
            if source.getcomptype() != "NONE" or channels < 1 or rate < 1 or width not in (1, 2, 3, 4):
                raise ValueError("Unsupported PCM WAV format")
            duration = count / rate
            if not math.isfinite(duration) or duration < minimum_duration:
                raise ValueError("Audio duration below minimum")
            total, energy = 0, 0.0
            while raw := source.readframes(65536):
                if len(raw) % (width * channels):
                    raise ValueError("Truncated PCM samples")
                if width == 1:
                    values = (np.frombuffer(raw, dtype=np.uint8).astype(float) - 128) / 128
                elif width == 3:
                    data = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
                    values = data[:, 0] | (data[:, 1] << 8) | (data[:, 2] << 16)
                    values = ((values ^ 0x800000) - 0x800000).astype(float) / 8388608
                else:
                    values = np.frombuffer(raw, dtype="<i2" if width == 2 else "<i4").astype(float) / (2 ** (width * 8 - 1))
                total += len(values)
                energy += float(np.dot(values, values))
            if total != count * channels or total == 0:
                raise ValueError("WAV sample count/header mismatch")
            rms = math.sqrt(energy / total)
            if rms <= minimum_rms:
                raise ValueError("Silent or zero-energy audio")
        return dict(valid=True, duration=duration, sample_count=count, sample_rate=rate, rms=rms, sha256=fingerprint(path))
    except (wave.Error, EOFError, OSError, ValueError) as exc:
        raise ArtifactValidationError(f"Invalid audio {path}: {exc}") from exc


def _run(command):
    result = subprocess.run(command, capture_output=True, timeout=120)
    if result.returncode:
        raise ArtifactValidationError(result.stderr.decode(errors="replace")[-2000:])
    return result.stdout


def _probe_video_fast(path):
    data = json.loads(_run(["ffprobe", "-v", "error", "-count_packets", "-show_streams", "-show_format", "-of", "json", str(path)]))
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if video is not None:
        nb_packets = int(video.get("nb_read_packets") or 0)
        nb_frames = int(video.get("nb_frames") or 0)
        if nb_packets > 0 and (nb_frames == 0 or nb_packets == nb_frames):
            return data, nb_packets
    data = json.loads(_run(["ffprobe", "-v", "error", "-count_frames", "-show_streams", "-show_format", "-of", "json", str(path)]))
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    frames = int(video.get("nb_read_frames", 0)) if video is not None else 0
    return data, frames


def validate_video(path, width, height, expected_duration, fps, require_audio=True, tolerance=0.15):
    data, frames = _probe_video_fast(path)
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    if video is None or (require_audio and audio is None):
        raise ArtifactValidationError("Required video/audio stream missing")
    duration = float(data["format"].get("duration", 0))
    if not math.isfinite(duration) or duration <= 0 or abs(duration - expected_duration) > tolerance:
        raise ArtifactValidationError("Invalid or mismatched media duration")
    if (int(video.get("width", 0)), int(video.get("height", 0))) != (width, height):
        raise ArtifactValidationError("Wrong video dimensions")
    numerator, denominator = map(float, video["avg_frame_rate"].split("/"))
    if denominator <= 0 or abs(numerator / denominator - fps) > 0.01:
        raise ArtifactValidationError("Wrong video fps")
    if frames <= 0 or abs(frames - round(expected_duration * fps)) > 1:
        raise ArtifactValidationError("Missing or mismatched frame count")
    samples = []
    for fraction in (0.1, 0.5, 0.9):
        index = min(frames - 1, int(frames * fraction))
        target_ts = max(0.0, index / float(fps))
        pixels = _run(["ffmpeg", "-v", "error", "-ss", f"{target_ts:.6f}", "-i", str(path),
                       "-frames:v", "1", "-vf", "scale=64:64", "-pix_fmt", "gray", "-f", "rawvideo", "pipe:1"])
        if len(pixels) != 4096:
            pixels = _run(["ffmpeg", "-v", "error", "-i", str(path),
                           "-frames:v", "1", "-vf", f"select=eq(n\\,{index}),scale=64:64", "-pix_fmt", "gray", "-f", "rawvideo", "pipe:1"])
        values = np.frombuffer(pixels, dtype=np.uint8).astype(float)
        if values.size != 4096 or values.mean() <= 2 or values.var() <= 1:
            raise ArtifactValidationError("Black, blank or undecodable sampled video frame")
        samples.append(dict(at=fraction, variance=float(values.var()), mean=float(values.mean())))
    if require_audio:
        # Check actual decoded audio, not just an audio stream declaration.
        pcm = _run(["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "8000", "-f", "f32le", "pipe:1"])
        values = np.frombuffer(pcm, dtype="<f4").astype(float)
        if values.size == 0 or not np.isfinite(values).all() or np.sqrt(np.mean(values ** 2)) <= 1e-5:
            raise ArtifactValidationError("Final video audio is silent or invalid")
        audio_duration = float(audio.get("duration", duration))
        if not math.isfinite(audio_duration) or abs(audio_duration - expected_duration) > tolerance:
            raise ArtifactValidationError("Final audio/video timeline mismatch")
    return dict(valid=True, duration=duration, frames=frames, samples=samples, sha256=fingerprint(path))


_TIME = r"(\d{2,}):([0-5]\d):([0-5]\d),(\d{3})"
_CUE = re.compile(rf"^(\d+)\n{_TIME} --> {_TIME}\n(.+)$", re.S)


def validate_subtitles(path, duration, tolerance=0.05):
    content = Path(path).read_text(encoding="utf-8-sig").replace("\r\n", "\n").strip()
    if not content:
        raise ArtifactValidationError("No subtitle cues")
    previous = 0.0
    blocks = re.split(r"\n\s*\n", content)
    for index, block in enumerate(blocks, 1):
        match = _CUE.fullmatch(block)
        if not match or int(match[1]) != index or not match[10].strip():
            raise ArtifactValidationError("Malformed or empty subtitle cue")
        nums = list(map(int, match.groups()[1:9]))
        start = nums[0] * 3600 + nums[1] * 60 + nums[2] + nums[3] / 1000
        end = nums[4] * 3600 + nums[5] * 60 + nums[6] + nums[7] / 1000
        if start < previous - 1e-6 or end <= start or end > duration + tolerance:
            raise ArtifactValidationError("Subtitle timeline mismatch")
        previous = end
    return dict(valid=True, cue_count=len(blocks), sha256=fingerprint(path))


def validate_final_artifact(video, subtitle, audio_paths, plan, script, manifest, chapter_number, chapter_dir=None):
    from src.render.visual_policy import validate_visual_plan
    if chapter_dir is None:
        raise ArtifactValidationError("Final validation requires the source chapter directory")
    validate_visual_plan(plan, chapter_dir, manifest, script, target_width=plan.target_width,
        target_height=plan.target_height, fps=plan.fps, motion_enabled=plan.motion_enabled)
    if str(chapter_number) != plan.chapter_number or str(script.chapter_number) != plan.chapter_number or manifest.chapter_number != plan.chapter_number:
        raise ArtifactValidationError("Chapter identity mismatch")
    if len(audio_paths) != len(script.segments) or not audio_paths:
        raise ArtifactValidationError("Missing narration audio")
    if len(audio_paths) > 1:
        with ThreadPoolExecutor(max_workers=min(8, len(audio_paths))) as pool:
            audio = list(pool.map(validate_audio, audio_paths))
    else:
        audio = [validate_audio(path) for path in audio_paths]
    for segment, report in zip(script.segments, audio):
        if segment.duration is None or abs(segment.duration - report["duration"]) > 0.001:
            raise ArtifactValidationError("Narration duration metadata mismatch")
        if segment.checksum != report["sha256"]:
            raise ArtifactValidationError("Narration checksum mismatch")
        duration = sum(s.duration_sec for s in plan.get_shots_for_segment(segment.seq_id))
        if abs(duration - report["duration"]) > 1e-6:
            raise ArtifactValidationError("Segment/shot timeline mismatch")
    total = sum(item["duration"] for item in audio)
    if abs(total - plan.total_duration_sec) > 1e-6:
        raise ArtifactValidationError("Narration/VisualPlan duration mismatch")
    result = dict(valid=True, schema_version="4.1.0", chapter_number=plan.chapter_number,
        video=validate_video(video, plan.target_width, plan.target_height, total, plan.fps),
        subtitles=validate_subtitles(subtitle, total), audio=audio,
        plan_fingerprint=hashlib.sha256(plan.model_dump_json().encode()).hexdigest(),
        script_fingerprint=hashlib.sha256(script.model_dump_json().encode()).hexdigest(),
        evidence_fingerprint=hashlib.sha256(manifest.model_dump_json().encode()).hexdigest())
    return result
