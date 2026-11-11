"""
tests/performance/test_benchmark_harness.py
Unit tests verifying the reproducible benchmark harness, workload generator,
and performance profiling data schemas.
"""

import json
from pathlib import Path

import pytest

from scripts.benchmark.profile_runner import (
    PerformanceProfiler,
    get_peak_memory_mb,
    get_system_hardware_info,
)
from scripts.benchmark.workload_generator import (
    create_synthetic_bgm,
    create_synthetic_panel_image,
    generate_benchmark_project,
)
from src.cli.validate_cmd import handle_validate
from src.core.schemas import ProjectConfig, RecapScript
from src.core.storage import get_chapter_dir, get_project_dir, read_json
from src.production.hardware import (
    probe_available_encoders,
    resolve_encoder,
    smoke_test_encoder,
)


class TestWorkloadGenerator:
    """Tests the synthetic benchmark workload generator."""

    def test_create_synthetic_panel_image(self, tmp_path: Path):
        panel_path = tmp_path / "panel_001.jpg"
        create_synthetic_panel_image(panel_path, panel_idx=1, width=400, height=600)
        assert panel_path.exists()
        assert panel_path.stat().st_size > 1000

    def test_create_synthetic_bgm(self, tmp_path: Path):
        bgm_path = tmp_path / "test_bgm.wav"
        create_synthetic_bgm(bgm_path, duration_seconds=1.0)
        assert bgm_path.exists()
        assert bgm_path.stat().st_size > 1000

    def test_generate_benchmark_project_and_validate(self, tmp_path: Path):
        slug = "test_synth_benchmark"
        chapters_spec = [{"chapter": "1", "num_segments": 5, "num_panels": 3}]

        proj_dir = generate_benchmark_project(
            project_slug=slug,
            chapters_spec=chapters_spec,
            target_width=1280,
            target_height=720,
            fps=30,
        )

        assert proj_dir.exists()
        assert (proj_dir / "project.json").exists()
        assert (proj_dir / "characters.json").exists()
        assert (proj_dir / "voice_selection.json").exists()

        chap_dir = proj_dir / "chapters" / "1"
        assert (chap_dir / "manifest.json").exists()
        assert (chap_dir / "script.json").exists()
        assert (chap_dir / "chapter.json").exists()

        # Validate project schemas
        p_cfg = ProjectConfig.model_validate(read_json(proj_dir / "project.json"))
        assert p_cfg.slug == slug
        assert p_cfg.render_settings.target_width == 1280

        script_obj = RecapScript.model_validate(read_json(chap_dir / "script.json"))
        assert len(script_obj.segments) == 5

        # Check that CLI validate passes
        import argparse
        args = argparse.Namespace(project=slug, chapter="1", fix=False)
        val_res = handle_validate(args)
        assert val_res["valid"] is True, f"Validation errors: {val_res.get('errors')}"


class TestHardwareProfilingEnvironment:
    """Tests hardware capability introspection and encoder verification."""

    def test_get_system_hardware_info(self):
        hw = get_system_hardware_info()
        assert "os" in hw
        assert "machine" in hw
        assert "python_version" in hw
        assert "available_encoders" in hw
        assert isinstance(hw["available_encoders"], list)

    def test_peak_memory_mb(self):
        ram = get_peak_memory_mb()
        assert isinstance(ram, float)
        assert ram > 0.0

    def test_resolve_encoder_returns_functional_encoder(self):
        enc, args, msg = resolve_encoder("auto")
        assert enc in ("h264_videotoolbox", "libx264", "h264_nvenc")
        assert len(args) >= 2
        assert smoke_test_encoder(enc) is True

    def test_export_csv_and_markdown(self, tmp_path: Path):
        json_path = tmp_path / "baseline_data.json"
        profiler = PerformanceProfiler(json_path)
        # Seed minimal mock structure
        profiler.results["workloads"]["small"] = {
            "workload_type": "small",
            "segments_count": 35,
            "panels_count": 20,
            "video_duration_seconds": 140.0,
            "stage_1_validation": {"elapsed_seconds": 0.12},
            "stage_2_tts": {
                "cold": {"elapsed_seconds": 28.5, "real_time_factor": 0.20, "speedup_vs_realtime": 4.9},
                "warm": {"elapsed_seconds": 25.0, "real_time_factor": 0.18, "speedup_vs_realtime": 5.6},
                "cache_hit": {"elapsed_seconds": 0.05, "segments_cached": 35},
            },
            "stage_3_render": {
                "videotoolbox_hw": {"elapsed_seconds": 32.0, "video_size_mb": 15.0},
                "libx264_cpu": {"elapsed_seconds": 50.0, "video_size_mb": 14.8},
                "cached": {"elapsed_seconds": 0.5, "cached_clips": 35},
            },
            "stage_4_export": {"elapsed_seconds": 0.05, "has_srt": True, "has_mp4": True},
            "e2e_wall_clock_seconds": 60.67,
            "throughput_video_minutes_per_hour": 138.4,
            "peak_rss_mb": 350.0,
        }
        profiler.results["workloads"]["large"] = {
            "workload_type": "large",
            "segments_count": 120,
            "panels_count": 40,
            "video_duration_seconds": 480.0,
            "stage_1_validation": {"elapsed_seconds": 0.35},
            "stage_2_tts": {"elapsed_seconds": 88.0, "real_time_factor": 0.18, "speedup_vs_realtime": 5.4},
            "stage_3_render": {"elapsed_seconds": 110.0, "video_size_mb": 52.0},
            "stage_4_export": {"elapsed_seconds": 0.08, "has_srt": True, "has_mp4": True},
            "e2e_wall_clock_seconds": 198.43,
            "throughput_video_minutes_per_hour": 145.1,
            "peak_rss_mb": 420.0,
        }
        profiler.results["workloads"]["batch"] = {
            "workload_type": "batch",
            "chapters_count": 5,
            "total_segments_count": 100,
            "total_video_duration_seconds": 400.0,
            "chapters": [
                {
                    "chapter": str(i),
                    "elapsed_seconds": 35.0,
                    "tts": {"audio_duration_seconds": 80.0, "elapsed_seconds": 16.0},
                    "render": {"elapsed_seconds": 19.0},
                }
                for i in range(1, 6)
            ],
            "compilation": {"elapsed_seconds": 1.5, "output_size_mb": 45.0},
            "e2e_wall_clock_seconds": 176.5,
            "throughput_video_minutes_per_hour": 135.9,
            "peak_rss_mb": 430.0,
        }
        profiler.results["bottlenecks"] = profiler.compute_bottleneck_analysis()

        csv_p = tmp_path / "baseline_data.csv"
        md_p = tmp_path / "baseline.md"

        profiler.export_csv(csv_p)
        profiler.export_markdown(md_p)

        assert csv_p.exists()
        assert csv_p.stat().st_size > 200
        assert md_p.exists()
        assert md_p.stat().st_size > 1000
        content = md_p.read_text(encoding="utf-8")
        assert "Performance Baseline & Profiling Report" in content
        assert "Quantitative Bottleneck Analysis" in content

