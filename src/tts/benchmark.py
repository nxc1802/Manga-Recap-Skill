"""
src/tts/benchmark.py
TTS Voice & Emotion Benchmark Suite Runner and Telemetry Generator.
Generates genuine 48 kHz uncompressed WAV audio, report.json, SUMMARY.md,
and zero-CORS benchmark_data.js.
"""

from __future__ import annotations

import datetime
import json
import math
import os
from pathlib import Path
import platform
import resource
import struct
import time
from typing import Any, Dict, List, Optional, Tuple
import wave

from src.core.logger import logger
from src.core.storage import atomic_write_json
from src.tts.engine import get_tts_engine, get_wav_duration_seconds
from src.tts.voice_profiles import VOICE_BY_NAME, VOICE_CATALOG_25, VoiceProfile

# Standard Reference Prompts for Voice Screening
REFERENCE_PROMPTS = [
    {
        "id": "ref_hook",
        "category": "screening",
        "emotion": "neutral",
        "text": "Chào mừng các bạn đã quay trở lại với kênh tóm tắt truyện tranh. Hôm nay chúng ta sẽ cùng theo dõi diễn biến tiếp theo của bộ truyện cực kỳ hấp dẫn này.",
        "description": "Standard YouTube Manga Recap Opening Hook"
    },
    {
        "id": "ref_cadence",
        "category": "screening",
        "emotion": "neutral",
        "text": "Màn đêm buông xuống bao trùm toàn bộ sơn cốc, cũng là lúc trận chiến sinh tử giữa hai đại tông môn chính thức bắt đầu.",
        "description": "Expository Narrative Cadence"
    }
]

# 8 Standardized Emotion Benchmark Prompts
EMOTION_BENCHMARK_PROMPTS = [
    {
        "id": "neutral",
        "category": "emotion",
        "emotion": "neutral",
        "name_vi": "Trần thuật trung tính",
        "text": "Sau khi rời khỏi thành trì, hắn lẳng lặng cất bước về phía cánh rừng rậm phía đông, nơi có ngôi đền cổ bị bỏ hoang từ trăm năm trước.",
        "cues": []
    },
    {
        "id": "excited",
        "category": "emotion",
        "emotion": "excited",
        "name_vi": "Hào hứng / Cao trào (Hype)",
        "text": "Không thể tin được! Cú đấm trực diện mang theo luồng kình lực hủy thiên diệt địa đã phá nát hoàn toàn kết giới phòng ngự của đối phương!",
        "cues": ["!"]
    },
    {
        "id": "suspenseful",
        "category": "emotion",
        "emotion": "suspenseful",
        "name_vi": "Hồi hộp / Căng thẳng (Tense)",
        "text": "Không gian bỗng nhiên tĩnh lặng đến nghẹt thở... Từng bước chân nặng trĩu tiến lại gần, lưỡi kiếm sắc lạnh đã kề sát sau gáy tự lúc nào.",
        "cues": ["..."]
    },
    {
        "id": "sad",
        "category": "emotion",
        "emotion": "sad",
        "name_vi": "Trầm buồn / Bi thương",
        "text": "Hắn lặng người quỳ bên đống đổ nát, nhìn kỷ vật cuối cùng của người thân dần tan biến trong ngọn lửa tàn. [thở dài] Tất cả... đã quá muộn rồi.",
        "cues": ["[thở dài]", "..."]
    },
    {
        "id": "angry",
        "category": "emotion",
        "emotion": "angry",
        "name_vi": "Phẫn nộ / Quyết liệt",
        "text": "Khốn kiếp! Các ngươi dám làm tổn thương đến sư muội, hôm nay dù có phải trả giá bằng cả tính mạng, ta cũng quyết không để kẻ nào rời khỏi đây lành lặn!",
        "cues": ["!"]
    },
    {
        "id": "whispered",
        "category": "emotion",
        "emotion": "whispered",
        "name_vi": "Thì thầm / Bí ẩn",
        "text": "Nghe cho kỹ đây... Bí mật này tuyệt đối không được để lọt vào tai trưởng lão, kẻ phản bội thực sự... đang ở ngay trong hàng ngũ chúng ta.",
        "cues": ["..."]
    },
    {
        "id": "cinematic_epic",
        "category": "emotion",
        "emotion": "cinematic_epic",
        "name_vi": "Sử thi / Hùng tráng",
        "text": "Dưới bầu trời cuồn cuộn mây sấm, ngàn vạn chiến binh cùng đồng thanh gầm thét, nghênh đón khoảnh khắc vương triều hưng suy được định đoạt trong trận huyết chiến này!",
        "cues": ["!"]
    },
    {
        "id": "comedic",
        "category": "emotion",
        "emotion": "comedic",
        "name_vi": "Hài hước / Bựa / Tinh nghịch",
        "text": "Ủa alo? Vừa nãy còn gáy to lắm mà, sao giờ lại ôm đầu chui tọt xuống gầm bàn thế này? [cười] Đúng là không trượt phát nào mà!",
        "cues": ["[cười]", "?"]
    }
]


def generate_mock_wav(
    output_path: Path,
    duration: float = 3.0,
    sample_rate: int = 48000,
    freq: float = 440.0,
) -> Tuple[float, List[float]]:
    """
    Generates a deterministic 48 kHz 16-bit PCM mono WAV file with smooth envelope.
    Returns (exact_duration, 64_normalized_peaks).
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    num_frames = int(sample_rate * duration)
    peaks_count = 64
    chunk_size = max(1, num_frames // peaks_count)

    frames = bytearray()
    peaks: List[float] = []
    current_chunk_max = 0

    for i in range(num_frames):
        t = float(i) / sample_rate
        env = min(1.0, t / 0.1) * min(1.0, (duration - t) / 0.1) if duration > 0.2 else 1.0
        val = 0.6 * math.sin(2.0 * math.pi * freq * t) + 0.3 * math.sin(4.0 * math.pi * freq * t)
        sample = int(val * env * 32767.0 * 0.7)
        sample = max(-32768, min(32767, sample))
        frames.extend(struct.pack("<h", sample))

        mag = abs(sample)
        if mag > current_chunk_max:
            current_chunk_max = mag

        if (i + 1) % chunk_size == 0 or i == num_frames - 1:
            peaks.append(round(current_chunk_max / 32767.0, 3))
            current_chunk_max = 0

    with wave.open(str(output_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(bytes(frames))

    exact_duration = num_frames / float(sample_rate)
    return round(exact_duration, 3), peaks[:peaks_count]


def compute_waveform_peaks(wav_path: Path, num_peaks: int = 64) -> List[float]:
    """Extracts downsampled normalized amplitude peaks from an existing WAV file."""
    with wave.open(str(wav_path), "rb") as wf:
        nchannels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        nframes = wf.getnframes()
        raw_bytes = wf.readframes(nframes)

    if sampwidth != 2 or nframes == 0:
        return [0.5] * num_peaks

    total_samples = nframes * nchannels
    fmt = f"<{total_samples}h"
    samples = struct.unpack(fmt, raw_bytes)

    channel_samples = samples[::nchannels]
    step = max(1, len(channel_samples) // num_peaks)
    peaks = []

    for i in range(0, len(channel_samples), step):
        chunk = channel_samples[i:i + step]
        if chunk:
            peak = max(abs(s) for s in chunk) / 32768.0
            peaks.append(round(min(1.0, peak), 3))
        if len(peaks) == num_peaks:
            break

    while len(peaks) < num_peaks:
        peaks.append(0.0)

    return peaks


def slugify_name(text: str) -> str:
    import unicodedata
    text = text.replace("đ", "d").replace("Đ", "D")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("utf-8")
    return text.lower().replace(" ", "_")


class TTSBenchmarkRunner:
    """Executes voice screening and emotion benchmarks, generating telemetry and artifacts."""

    def __init__(
        self,
        output_dir: Path,
        voices: Optional[List[str]] = None,
        emotions: Optional[List[str]] = None,
        repeats: int = 1,
        use_mock: bool = False,
        copy_to_lab: bool = True,
    ):
        self.output_dir = output_dir.resolve()
        self.samples_dir = self.output_dir / "samples"
        self.voices = voices or [vp.name for vp in VOICE_CATALOG_25]
        self.emotions = emotions or [ep["id"] for ep in EMOTION_BENCHMARK_PROMPTS]
        self.repeats = max(1, repeats)
        self.use_mock = use_mock
        self.copy_to_lab = copy_to_lab

    def run(self) -> Dict[str, Any]:
        self.samples_dir.mkdir(parents=True, exist_ok=True)
        start_time = time.perf_counter()
        benchmark_id = f"bench_{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d_%H%M%S')}"

        hardware_info = {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python_version": platform.python_version(),
            "backend": "mock" if self.use_mock else ("mps" if platform.system() == "Darwin" and platform.machine() == "arm64" else "cpu"),
            "sample_rate_target": 48000,
        }

        samples_data: List[Dict[str, Any]] = []

        # 1. Voice Screening Phase: Selected voices x Reference Prompts
        for v_name in self.voices:
            vp = VOICE_BY_NAME.get(v_name)
            v_info = {
                "name": v_name,
                "region": vp.region.value if vp else "Bắc",
                "gender": vp.gender.value if vp else "female",
                "role": vp.suggested_role if vp else "Narrator",
                "is_top10": vp.is_top10 if vp else False,
                "timbre": vp.timbre_description if vp else "",
            }

            for prompt in REFERENCE_PROMPTS:
                sample_item = self._synthesize_sample(
                    voice_name=v_name,
                    voice_info=v_info,
                    prompt_id=prompt["id"],
                    category=prompt["category"],
                    emotion=prompt["emotion"],
                    text=prompt["text"],
                )
                samples_data.append(sample_item)

        # 2. Emotion Benchmark Phase: Target voices x 8 Emotions
        emotion_prompts_map = {ep["id"]: ep for ep in EMOTION_BENCHMARK_PROMPTS}
        for emo_id in self.emotions:
            if emo_id not in emotion_prompts_map:
                continue
            ep = emotion_prompts_map[emo_id]

            # Primary voice ("Trúc Ly") + up to 2 other top10 voices
            target_voices = [v for v in self.voices if (VOICE_BY_NAME.get(v) and VOICE_BY_NAME[v].is_top10)][:3]
            if "Trúc Ly" not in target_voices and "Trúc Ly" in self.voices:
                target_voices.insert(0, "Trúc Ly")

            for v_name in target_voices:
                vp = VOICE_BY_NAME.get(v_name)
                v_info = {
                    "name": v_name,
                    "region": vp.region.value if vp else "Bắc",
                    "gender": vp.gender.value if vp else "female",
                    "role": vp.suggested_role if vp else "Narrator",
                    "is_top10": vp.is_top10 if vp else False,
                    "timbre": vp.timbre_description if vp else "",
                }
                sample_item = self._synthesize_sample(
                    voice_name=v_name,
                    voice_info=v_info,
                    prompt_id=f"emo_{emo_id}",
                    category="emotion",
                    emotion=emo_id,
                    text=ep["text"],
                )
                samples_data.append(sample_item)

        total_wall = time.perf_counter() - start_time
        max_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        rss_mb = round(max_rss / (1024 * 1024) if platform.system() == "Darwin" else max_rss / 1024, 2)
        hardware_info["peak_ram_mb"] = rss_mb

        valid_rtfs = [s["rtf"] for s in samples_data if s["rtf"] > 0]
        mean_rtf = round(sum(valid_rtfs) / len(valid_rtfs), 3) if valid_rtfs else 0.0
        total_audio_dur = round(sum(s["audio_duration_seconds"] for s in samples_data), 2)
        mean_lat = round(sum(s["synthesis_time_seconds"] for s in samples_data) / max(1, len(samples_data)), 3)

        summary = {
            "total_samples": len(samples_data),
            "voices_screened_count": len(set(s["voice_id"] for s in samples_data)),
            "emotions_tested_count": len(set(s["emotion"] for s in samples_data if s["category"] == "emotion")),
            "total_audio_duration_seconds": total_audio_dur,
            "total_generation_time_seconds": round(total_wall, 2),
            "mean_latency_seconds": mean_lat,
            "mean_rtf": mean_rtf,
            "min_rtf": min(valid_rtfs) if valid_rtfs else 0.0,
            "max_rtf": max(valid_rtfs) if valid_rtfs else 0.0,
        }

        report = {
            "benchmark_id": benchmark_id,
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "engine": "VieNeu-TTS v3 Turbo",
            "hardware": hardware_info,
            "summary": summary,
            "samples": samples_data,
        }

        # 1. Write report.json
        report_path = self.output_dir / "report.json"
        atomic_write_json(report_path, report)

        # 2. Write SUMMARY.md
        summary_md_path = self.output_dir / "SUMMARY.md"
        self._write_summary_md(summary_md_path, report)

        # 3. Write benchmark_data.js for zero-CORS browser loading
        data_js_path = self.output_dir / "benchmark_data.js"
        data_js_content = f"// Auto-generated by manga-recap tts_benchmark\nwindow.BENCHMARK_DATA = {json.dumps(report, ensure_ascii=False, indent=2)};\n"
        data_js_path.write_text(data_js_content, encoding="utf-8")

        # 4. Copy to docs/lab/
        lab_html_path = Path("docs/lab/index.html").resolve()
        if self.copy_to_lab:
            lab_dir = Path("docs/lab").resolve()
            lab_dir.mkdir(parents=True, exist_ok=True)
            lab_samples_dir = lab_dir / "samples"
            lab_samples_dir.mkdir(parents=True, exist_ok=True)

            (lab_dir / "benchmark_data.js").write_text(data_js_content, encoding="utf-8")
            (lab_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

            # Copy or link WAV files to docs/lab/samples
            import shutil
            for s_file in self.samples_dir.glob("*.wav"):
                dest = lab_samples_dir / s_file.name
                if not dest.exists():
                    try:
                        os.link(s_file, dest)
                    except Exception:
                        shutil.copy2(s_file, dest)

        logger.info(
            f"TTS Benchmark complete: {len(samples_data)} samples, mean RTF={mean_rtf}, "
            f"output: {self.output_dir}"
        )

        return {
            "benchmark_id": benchmark_id,
            "output_dir": str(self.output_dir),
            "report_json": str(report_path),
            "summary_md": str(summary_md_path),
            "lab_html": str(lab_html_path),
            "total_samples_generated": len(samples_data),
            "voices_screened_count": summary["voices_screened_count"],
            "total_voices_tested": summary["voices_screened_count"],
            "voices": self.voices,
            "emotions_tested_count": summary["emotions_tested_count"],
            "mean_rtf": mean_rtf,
            "mean_latency_seconds": mean_lat,
            "backend": hardware_info["backend"],
            "status": "completed",
        }

    def _synthesize_sample(
        self,
        voice_name: str,
        voice_info: Dict[str, Any],
        prompt_id: str,
        category: str,
        emotion: str,
        text: str,
    ) -> Dict[str, Any]:
        v_slug = slugify_name(voice_name)
        sample_filename = f"{v_slug}_{prompt_id}.wav"
        sample_path = self.samples_dir / sample_filename
        rel_audio_path = f"samples/{sample_filename}"

        t0 = time.perf_counter()
        if self.use_mock:
            dur = max(2.0, round(len(text.split()) / 3.2, 2))
            audio_duration, peaks = generate_mock_wav(
                sample_path,
                duration=dur,
                freq=220.0 + (abs(hash(voice_name)) % 200),
            )
        else:
            engine = get_tts_engine()
            dur = engine.synthesize_to_file(
                text=text,
                voice_id=voice_name,
                output_path=sample_path,
                speed=1.0,
            )
            peaks = compute_waveform_peaks(sample_path)
            audio_duration = get_wav_duration_seconds(sample_path)

        syn_time = round(time.perf_counter() - t0, 3)
        rtf = round(syn_time / max(0.01, audio_duration), 3)
        file_size = sample_path.stat().st_size if sample_path.exists() else 0

        return {
            "sample_id": f"{v_slug}_{prompt_id}",
            "voice_id": voice_name,
            "voice_info": voice_info,
            "category": category,
            "emotion": emotion,
            "prompt_text": text,
            "audio_file": rel_audio_path,
            "audio_duration_seconds": audio_duration,
            "synthesis_time_seconds": syn_time,
            "rtf": rtf,
            "sample_rate": 48000,
            "channels": 1,
            "file_size_bytes": file_size,
            "peaks": peaks,
        }

    def _write_summary_md(self, summary_path: Path, report: Dict[str, Any]):
        summary = report["summary"]
        hw = report["hardware"]
        samples = report["samples"]

        lines = [
            "# TTS Voice & Emotion Benchmark Summary",
            "",
            f"**Benchmark ID**: `{report['benchmark_id']}`  ",
            f"**Created At**: `{report['created_at']}`  ",
            f"**Engine**: `{report['engine']}` (Backend: `{hw['backend']}`)  ",
            f"**Platform**: `{hw['platform']}` (`{hw['machine']}`)  ",
            f"**Peak RAM**: `{hw.get('peak_ram_mb', 0)} MB`  ",
            "",
            "## 1. Aggregate Performance Telemetry",
            "",
            f"- **Total Samples Generated**: {summary['total_samples']}",
            f"- **Voices Screened**: {summary['voices_screened_count']}",
            f"- **Emotions Tested**: {summary['emotions_tested_count']}",
            f"- **Total Audio Duration**: {summary['total_audio_duration_seconds']}s",
            f"- **Total Generation Time**: {summary['total_generation_time_seconds']}s",
            f"- **Mean Real-Time Factor (RTF)**: **{summary['mean_rtf']}** (Min: {summary['min_rtf']}, Max: {summary['max_rtf']})",
            f"- **Mean Latency per Utterance**: {summary['mean_latency_seconds']}s",
            "",
            "## 2. Voice Screening Matrix (Standard Hook)",
            "",
            "| Voice | Region | Gender | Role | Duration (s) | RTF | Status |",
            "|---|---|---|---|---|---|---|",
        ]

        screening_samples = [s for s in samples if s["category"] == "screening" and "ref_hook" in s["sample_id"]]
        for s in screening_samples:
            vi = s["voice_info"]
            lines.append(
                f"| **{s['voice_id']}** | {vi['region']} | {vi['gender']} | {vi['role']} | "
                f"{s['audio_duration_seconds']:.2f}s | {s['rtf']:.3f} | 48kHz Mono |"
            )

        lines.extend([
            "",
            "## 3. Emotion Benchmark Matrix",
            "",
            "| Emotion | Name (VI) | Voice | Duration (s) | RTF | Cue Tags |",
            "|---|---|---|---|---|---|",
        ])

        emotion_samples = [s for s in samples if s["category"] == "emotion"]
        for s in emotion_samples:
            lines.append(
                f"| `{s['emotion']}` | {s['sample_id']} | {s['voice_id']} | "
                f"{s['audio_duration_seconds']:.2f}s | {s['rtf']:.3f} | 48kHz Mono |"
            )

        lines.extend([
            "",
            "## 4. Human Review Gate 1 Instructions",
            "",
            "1. Open the local listening lab interface: `docs/lab/index.html` in your web browser.",
            "2. Audition the Top 10 recommended voices and test emotion cues.",
            "3. Click **Export voice_selection.json** to approve your voice strategy before video rendering.",
            "",
        ])

        summary_path.write_text("\n".join(lines), encoding="utf-8")
