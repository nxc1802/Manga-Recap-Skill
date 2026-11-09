"""
scripts/benchmark/measure_render_speedup.py
Empirical performance verification for Phase 5 A8 Video Rendering Optimization:
Compares unoptimized per-frame boxblur filterchain vs optimized Single-Frame Static Motion Hold.
All metrics are empirically derived from genuine FFmpeg execution runs.
"""

from __future__ import annotations

import json
import math
import struct
import time
import wave
from pathlib import Path
from PIL import Image

from src.core.schemas import CameraMotionType, VideoMode
from src.render.subtitle import SubtitleCue
from src.render.validator import StreamValidator
from src.render.video_engine import VideoEngine


def make_test_wav(path: Path, duration_sec: float = 3.5, sample_rate: int = 48000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    num_samples = int(duration_sec * sample_rate)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        raw = bytearray()
        for i in range(num_samples):
            v = int(6000 * math.sin(2 * math.pi * 440 * i / sample_rate))
            raw.extend(struct.pack("<h", v))
        wf.writeframes(raw)
    return path


def make_test_image(path: Path, width: int = 800, height: int = 1200, color=(140, 90, 180)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (width, height), color=color)
    img.save(path, "JPEG")
    return path


def run_empirical_render_comparison(num_segments: int = 20, output_dir: Path = Path("/tmp/render_benchmark")):
    output_dir.mkdir(parents=True, exist_ok=True)
    engine = VideoEngine(target_width=1920, target_height=1080, fps=30)
    validator = StreamValidator()

    # Create test assets (5 distinct panels reused across 20 segments)
    panel_paths = [
        make_test_image(output_dir / f"panel_{i}.jpg", width=800, height=1200, color=(50 * (i + 1), 40 * i, 120))
        for i in range(5)
    ]
    audio_paths = [
        make_test_wav(output_dir / f"audio_{i}.wav", duration_sec=3.0)
        for i in range(num_segments)
    ]

    print(f"=== Empirical Render Performance Benchmark: {num_segments} Segments (1080p, 30fps) ===")

    # 1. Benchmark: Optimized Path (Single-Frame Static Motion Hold)
    out_opt_dir = output_dir / "optimized"
    out_opt_dir.mkdir(parents=True, exist_ok=True)
    engine.clear_canvas_cache()

    t0 = time.perf_counter()
    for i in range(num_segments):
        panel_p = panel_paths[i % len(panel_paths)]
        audio_p = audio_paths[i]
        out_clip = out_opt_dir / f"clip_opt_{i:03d}.mp4"
        cues = [
            SubtitleCue(index=1, start_seconds=0.0, end_seconds=1.5, text="Đoạn thoại mở đầu câu chuyện"),
            SubtitleCue(index=2, start_seconds=1.5, end_seconds=3.0, text="Nhân vật chính lập tức phản công"),
        ]
        ok = engine.generate_panel_clip(
            img_path=str(panel_p),
            audio_path=str(audio_p),
            duration_ms=3000,
            output_path=str(out_clip),
            camera_motion=CameraMotionType.STATIC,
            video_mode=VideoMode.PORTRAIT_PADDED,
            burn_subtitles=True,
            subtitle_cues=cues,
            preset="ultrafast",
            crf="23",
        )
        assert ok is True
    t_opt_total = time.perf_counter() - t0
    avg_opt_clip = t_opt_total / num_segments

    # Validate output
    sample_clip = out_opt_dir / "clip_opt_000.mp4"
    val = validator.validate_video(sample_clip, expected_duration=3.0, expected_width=1920, expected_height=1080)
    assert val["valid"] is True

    # 2. Benchmark: Unoptimized Path (Filtercomplex boxblur on all 90 frames)
    # Simulate unoptimized execution by disabling single-frame hold
    import subprocess
    out_unopt_dir = output_dir / "unoptimized"
    out_unopt_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.perf_counter()
    for i in range(num_segments):
        panel_p = panel_paths[i % len(panel_paths)]
        audio_p = audio_paths[i]
        out_clip = out_unopt_dir / f"clip_unopt_{i:03d}.mp4"
        cam_filter, _ = engine.camera_generator.build_filterchain(
            img_w=800, img_h=1200, duration_sec=3.0,
            motion_type=CameraMotionType.STATIC,
            video_mode=VideoMode.PORTRAIT_PADDED,
        )
        # Create subtitle overlays
        sub_p1 = output_dir / f"sub1_{i}.png"
        sub_p2 = output_dir / f"sub2_{i}.png"
        engine.create_subtitle_overlay_png("Đoạn thoại mở đầu câu chuyện", sub_p1)
        engine.create_subtitle_overlay_png("Nhân vật chính lập tức phản công", sub_p2)

        fc = (
            f"[0:v]{cam_filter}[v_0];"
            f"[v_0][2:v]overlay=0:0:enable='between(t,0.000,1.500)'[v_1];"
            f"[v_1][3:v]overlay=0:0:enable='between(t,1.500,3.000)'[outv]"
        )
        cmd = [
            "ffmpeg", "-y",
            "-loop", "1", "-i", str(panel_p),
            "-i", str(audio_p),
            "-loop", "1", "-i", str(sub_p1),
            "-loop", "1", "-i", str(sub_p2),
            "-filter_complex", fc,
            "-map", "[outv]", "-map", "1:a",
            "-t", "3.0000",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-r", "30",
            str(out_clip),
        ]
        subprocess.run(cmd, capture_output=True, check=True)
        sub_p1.unlink(missing_ok=True)
        sub_p2.unlink(missing_ok=True)
    t_unopt_total = time.perf_counter() - t0
    avg_unopt_clip = t_unopt_total / num_segments

    speedup = t_unopt_total / t_opt_total if t_opt_total > 0 else 0.0

    print(f"Results for {num_segments} Segments (3.0s each = {num_segments * 3.0}s total video):")
    print(f"- Unoptimized (Per-Frame Filtercomplex): {t_unopt_total:.2f}s (avg: {avg_unopt_clip:.3f}s / clip)")
    print(f"- Optimized (Single-Frame Static Hold):  {t_opt_total:.2f}s (avg: {avg_opt_clip:.3f}s / clip)")
    print(f"- Measured Speedup:                      {speedup:.2f}x ({((t_unopt_total - t_opt_total) / t_unopt_total) * 100:.1f}% time reduction)")
    print(f"- Stream Validation:                     {val['valid']} ({val['video']['width']}x{val['video']['height']} @ {val['video']['fps']} fps)")

    metrics = {
        "num_segments": num_segments,
        "video_duration_seconds": num_segments * 3.0,
        "unoptimized_elapsed_seconds": round(t_unopt_total, 3),
        "optimized_elapsed_seconds": round(t_opt_total, 3),
        "unoptimized_avg_clip_seconds": round(avg_unopt_clip, 3),
        "optimized_avg_clip_seconds": round(avg_opt_clip, 3),
        "speedup_ratio": round(speedup, 2),
        "time_reduction_percent": round(((t_unopt_total - t_opt_total) / t_unopt_total) * 100, 1),
        "validation": val,
    }

    out_metrics_path = Path("docs/performance/render_optimization_metrics.json")
    out_metrics_path.parent.mkdir(parents=True, exist_ok=True)
    out_metrics_path.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    print(f"Metrics saved to {out_metrics_path}")
    return metrics


if __name__ == "__main__":
    run_empirical_render_comparison(20)
