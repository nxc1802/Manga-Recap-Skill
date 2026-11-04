"""
tests/unit/test_cli.py
Unit tests verifying the unified CLI suite (manga-recap):
- Subcommand registration & argument parsing
- POSIX-aligned exit codes (0, 1, 2, 3, 4)
- Structured JSON envelope format & stdout purity
- Subcommand implementations: inspect, validate, status, crawl, tts, tts_benchmark, render, compile
"""

import argparse
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch, MagicMock
import pytest

from src.cli.exit_codes import ExitCode
from src.cli.main import build_parser, main
from src.cli.response import JsonEnvelope
from src.core.storage import (
    atomic_write_json,
    init_chapter_layout,
    init_project_layout,
    update_checkpoint,
)


class TestCliParserAndDispatch:
    """Verifies argument parsing and subcommand dispatch."""

    def test_parser_subcommands_registered(self):
        parser = build_parser()
        subparsers_action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        registered = list(subparsers_action.choices.keys())
        expected = ["crawl", "segment", "inspect", "validate", "tts", "tts_benchmark", "render", "compile", "status"]
        for cmd in expected:
            assert cmd in registered, f"Subcommand '{cmd}' not registered"

    def test_cli_version_flag(self, capsys):
        code = main(["--version"])
        assert code == 0
        captured = capsys.readouterr()
        assert "3.0.0" in captured.out or "3.0.0" in captured.err

    def test_subcommand_help_flag(self, capsys):
        for subcmd in ["inspect", "validate", "status", "crawl", "tts", "tts_benchmark", "render", "compile"]:
            code = main([subcmd, "--help"])
            assert code == 0

    def test_missing_subcommand_raises_or_exits_2(self):
        code = main([])
        assert code == ExitCode.VALIDATION_ERROR

    def test_missing_required_argument_exits_2(self):
        code = main(["inspect"])
        assert code == ExitCode.VALIDATION_ERROR

    def test_unknown_subcommand_exits_2(self):
        code = main(["unknown_cmd"])
        assert code == ExitCode.VALIDATION_ERROR


class TestExitCodesAndJsonEnvelope:
    """Verifies POSIX exit codes and JSON envelope schema conformance."""

    def test_exit_code_enum_values(self):
        assert ExitCode.SUCCESS == 0
        assert ExitCode.GENERAL_ERROR == 1
        assert ExitCode.VALIDATION_ERROR == 2
        assert ExitCode.IO_ERROR == 3
        assert ExitCode.DEPENDENCY_ERROR == 4

    def test_json_envelope_on_io_error(self, capsys):
        code = main(["inspect", "-p", "nonexistent_slug_12345", "--json"])
        assert code == ExitCode.IO_ERROR
        captured = capsys.readouterr()
        data = json.loads(captured.out)

        envelope = JsonEnvelope.model_validate(data)
        assert envelope.status == "error"
        assert envelope.command == "inspect"
        assert envelope.exit_code == ExitCode.IO_ERROR
        assert envelope.data is None
        assert envelope.error is not None
        assert envelope.error.code == "IO_ERROR"
        assert envelope.duration_seconds >= 0.0

    def test_json_envelope_on_argument_validation_error(self, capsys):
        code = main(["inspect", "--json"])  # Missing -p
        assert code == ExitCode.VALIDATION_ERROR
        captured = capsys.readouterr()
        data = json.loads(captured.out)

        assert data["status"] == "error"
        assert data["exit_code"] == ExitCode.VALIDATION_ERROR
        assert data["error"]["code"] == "VALIDATION_ERROR"

    def test_json_envelope_on_dependency_error(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))
        init_project_layout("test_proj", "Test Proj")
        init_chapter_layout("test_proj", "1")

        with patch("shutil.which", return_value=None):
            code = main(["render", "-p", "test_proj", "-c", "1", "--json"])
            assert code == ExitCode.DEPENDENCY_ERROR

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["status"] == "error"
        assert data["exit_code"] == ExitCode.DEPENDENCY_ERROR
        assert data["error"]["code"] == "DEPENDENCY_ERROR"

    def test_stdout_purity_with_logging(self, tmp_path, capsys, monkeypatch):
        from src.core.logger import logger
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))

        init_project_layout("pure_proj", "Pure Proj")
        init_chapter_layout("pure_proj", "1")

        logger.info("This is a log message that must NOT appear on stdout in json mode")

        code = main(["status", "-p", "pure_proj", "-c", "1", "--json"])
        assert code == ExitCode.SUCCESS

        captured = capsys.readouterr()
        # Stdout must be strictly parsable as JSON without leading or trailing log lines
        parsed = json.loads(captured.out.strip())
        assert parsed["status"] == "success"
        assert parsed["exit_code"] == 0
        assert parsed["command"] == "status"

    def test_root_missing_subcommand_emits_single_parseable_json_envelope(self, capsys):
        code = main(["--json"])
        assert code == ExitCode.VALIDATION_ERROR
        captured = capsys.readouterr()
        # Must be strictly parseable as a single JSON envelope
        data = json.loads(captured.out)
        assert data["status"] == "error"
        assert data["exit_code"] == ExitCode.VALIDATION_ERROR
        assert data["error"]["code"] == "VALIDATION_ERROR"

    def test_root_unknown_subcommand_emits_single_parseable_json_envelope(self, capsys):
        code = main(["unknown_subcommand_xyz", "--json"])
        assert code == ExitCode.VALIDATION_ERROR
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["status"] == "error"
        assert data["exit_code"] == ExitCode.VALIDATION_ERROR
        assert data["error"]["code"] == "VALIDATION_ERROR"

    def test_subcommand_invalid_flag_emits_single_parseable_json_envelope(self, capsys):
        code = main(["inspect", "-p", "foo", "--invalid-flag-abc", "--json"])
        assert code == ExitCode.VALIDATION_ERROR
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["status"] == "error"
        assert data["command"] == "inspect"
        assert data["exit_code"] == ExitCode.VALIDATION_ERROR
        assert data["error"]["code"] == "VALIDATION_ERROR"

    def test_permission_error_maps_to_io_error_code_3(self, capsys):
        with patch("src.cli.main.execute_subcommand", side_effect=PermissionError("Permission denied: /data/locked")):
            code = main(["inspect", "-p", "foo", "--json"])
            assert code == ExitCode.IO_ERROR
            captured = capsys.readouterr()
            data = json.loads(captured.out)
            assert data["status"] == "error"
            assert data["exit_code"] == ExitCode.IO_ERROR
            assert data["error"]["code"] == "IO_ERROR"
            assert "Permission denied" in data["error"]["message"]

    def test_subprocess_single_json_envelope_on_parse_errors(self):
        """Verifies true subprocess CLI execution produces exactly one clean parseable JSON envelope."""
        for cmd_args in [
            ["--json"],
            ["unknown", "--json"],
            ["inspect", "-p", "foo", "--invalid-flag", "--json"],
        ]:
            proc = subprocess.run(
                [sys.executable, "-m", "src.cli.main"] + cmd_args,
                capture_output=True,
                text=True,
            )
            assert proc.returncode == ExitCode.VALIDATION_ERROR
            # json.loads fails with JSONDecodeError if duplicate envelopes are emitted
            data = json.loads(proc.stdout)
            assert data["status"] == "error"
            assert data["exit_code"] == ExitCode.VALIDATION_ERROR
            assert data["error"]["code"] == "VALIDATION_ERROR"


class TestSubcommandImplementations:
    """Verifies functionality of inspect, validate, status, and other subcommands."""

    @pytest.fixture
    def setup_project(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))

        proj_dir = init_project_layout("demo_manga", "Demo Manga Series")
        chap_dir = init_chapter_layout("demo_manga", "1", "Episode 1")

        # Create panels and manifest
        panels_dir = chap_dir / "panels"
        panels_dir.mkdir(parents=True, exist_ok=True)
        p1 = panels_dir / "panel_001.jpg"
        p1.write_bytes(b"dummy image data")

        manifest_data = {
            "schema_version": "3.0.0",
            "project_slug": "demo_manga",
            "chapter_number": "1",
            "total_panels": 1,
            "panels": [
                {
                    "panel_id": "panel_001",
                    "sequence_number": 1,
                    "filename": "panel_001.jpg",
                    "relative_path": "chapters/1/panels/panel_001.jpg",
                    "source_image": "page_001.jpg",
                    "crop_box": [0, 0, 800, 600],
                    "width": 800,
                    "height": 600,
                    "aspect_ratio": 1.333,
                    "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                    "status": "active",
                }
            ],
        }
        atomic_write_json(chap_dir / "manifest.json", manifest_data)

        # Create script
        script_data = {
            "schema_version": "3.0.0",
            "project_slug": "demo_manga",
            "chapter_number": "1",
            "title": "Episode 1",
            "voice_strategy": "single_narrator",
            "default_voice": "Trúc Ly",
            "segments": [
                {
                    "seq_id": "001",
                    "panel_ids": ["panel_001.jpg"],
                    "narrator_text": "Mở đầu trận chiến vô tiền khoáng hậu tại đỉnh núi tuyết.",
                    "camera_motion": "zoom_in",
                }
            ],
        }
        atomic_write_json(chap_dir / "script.json", script_data)

        return proj_dir, chap_dir

    def test_inspect_project_overview(self, setup_project, capsys):
        code = main(["inspect", "-p", "demo_manga", "--json"])
        assert code == ExitCode.SUCCESS

        captured = capsys.readouterr()
        data = json.loads(captured.out)["data"]
        assert data["project_slug"] == "demo_manga"
        assert data["total_chapters"] == 1
        assert data["chapters"][0]["chapter_number"] == "1"

    def test_inspect_chapter_details(self, setup_project, capsys):
        code = main(["inspect", "-p", "demo_manga", "-c", "1", "--json"])
        assert code == ExitCode.SUCCESS

        captured = capsys.readouterr()
        data = json.loads(captured.out)["data"]
        assert data["chapter_number"] == "1"
        assert data["assets"]["panels_count"] == 1
        assert data["assets"]["script_segments_count"] == 1
        assert "crawl" in data["checkpoints"]

    def test_validate_success(self, setup_project, capsys):
        code = main(["validate", "-p", "demo_manga", "-c", "1", "--json"])
        assert code == ExitCode.SUCCESS

        captured = capsys.readouterr()
        data = json.loads(captured.out)["data"]
        assert data["valid"] is True
        assert data["summary"]["errors_count"] == 0

    def test_validate_failure_on_missing_panel(self, setup_project, capsys):
        proj_dir, chap_dir = setup_project
        # Remove physical panel file to trigger validation error
        p1 = chap_dir / "panels" / "panel_001.jpg"
        if p1.exists():
            p1.unlink()

        code = main(["validate", "-p", "demo_manga", "-c", "1", "--json"])
        assert code == ExitCode.VALIDATION_ERROR

        captured = capsys.readouterr()
        resp = json.loads(captured.out)
        assert resp["status"] == "error"
        assert resp["exit_code"] == ExitCode.VALIDATION_ERROR
        # Either in data or error details
        data_payload = resp.get("data")
        error_details = resp["error"].get("details")
        if data_payload:
            assert data_payload["summary"]["errors_count"] > 0
        elif isinstance(error_details, list):
            assert len(error_details) > 0

    def test_status_reporting_and_resumable_stage(self, setup_project, capsys):
        proj_dir, chap_dir = setup_project
        # Default chapter has crawl=pending -> resumable stage is crawl
        code = main(["status", "-p", "demo_manga", "-c", "1", "--json"])
        assert code == ExitCode.SUCCESS

        captured = capsys.readouterr()
        data = json.loads(captured.out)["data"]
        assert data["chapter_number"] == "1"
        assert data["resumable_stage"] == "crawl"

        # Update crawl to completed -> resumable stage becomes segment
        update_checkpoint(chap_dir, stage="crawl", status="completed")
        capsys.readouterr()

        code = main(["status", "-p", "demo_manga", "-c", "1", "--json"])
        assert code == ExitCode.SUCCESS
        captured = capsys.readouterr()
        data = json.loads(captured.out)["data"]
        assert data["resumable_stage"] == "segment"

    def test_crawl_import_directory(self, tmp_path, setup_project, capsys):
        proj_dir, chap_dir = setup_project
        # Create a mock source directory with 2 image files
        src_dir = tmp_path / "raw_source"
        src_dir.mkdir()
        (src_dir / "page_1.jpg").write_bytes(b"page1")
        (src_dir / "page_2.png").write_bytes(b"page2")

        code = main([
            "crawl",
            "-p", "demo_manga",
            "-c", "2",
            "--import-dir", str(src_dir),
            "--json",
        ])
        assert code == ExitCode.SUCCESS

        captured = capsys.readouterr()
        data = json.loads(captured.out)["data"]
        assert data["raw_images_count"] == 2
        assert data["status"] == "completed"

    def test_segment_command(self, tmp_path, setup_project, capsys):
        from PIL import Image
        from src.core.schemas import StageType, CheckpointStatus
        proj_dir, chap_dir = setup_project
        raw_dir = chap_dir / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)

        update_checkpoint(chap_dir, StageType.CRAWL, CheckpointStatus.COMPLETED)

        # Create valid test image in raw_dir
        im = Image.new("RGB", (1080, 1500), "white")
        # Draw some content so it detects a panel
        for y in range(200, 800):
            for x in range(200, 800):
                im.putpixel((x, y), (10, 20, 30))
        im.save(raw_dir / "page_001.jpg")

        code = main([
            "segment",
            "-p", "demo_manga",
            "-c", "1",
            "--json",
        ])
        assert code == ExitCode.SUCCESS

        captured = capsys.readouterr()
        data = json.loads(captured.out)["data"]
        assert data["status"] == "completed"
        assert data["total_panels"] >= 1
        assert (chap_dir / "manifest.json").exists()

        # Check status advances to script
        status_code = main(["status", "-p", "demo_manga", "-c", "1", "--json"])
        assert status_code == ExitCode.SUCCESS
        status_captured = capsys.readouterr()
        status_data = json.loads(status_captured.out)["data"]
        assert status_data["resumable_stage"] == "script"

    def test_tts_command(self, tmp_path, setup_project, capsys, monkeypatch):
        import wave
        proj_dir, chap_dir = setup_project

        # Write test script.json
        script_data = {
            "schema_version": "3.0.0",
            "project_slug": "demo_manga",
            "chapter_number": "1",
            "title": "Chapter 1",
            "voice_strategy": "single_narrator",
            "default_voice": "Trúc Ly",
            "segments": [
                {
                    "seq_id": "001",
                    "panel_ids": ["panel_001"],
                    "narrator_text": "Mở đầu trận chiến nảy lửa.",
                    "voice_id": "Trúc Ly",
                    "camera_motion": "static",
                },
                {
                    "seq_id": "002",
                    "panel_ids": ["panel_002"],
                    "narrator_text": "Tiếng gầm thét vang dội khắp chiến trường.",
                    "voice_id": "Hải Đăng",
                    "camera_motion": "pan_left",
                }
            ]
        }
        atomic_write_json(chap_dir / "script.json", script_data)

        # First run: synthesis
        code = main(["tts", "-p", "demo_manga", "-c", "1", "--json"])
        assert code == ExitCode.SUCCESS
        captured = capsys.readouterr()
        data = json.loads(captured.out)["data"]
        assert data["segments_synthesized"] == 2
        assert data["total_duration_seconds"] > 0

        # Verify generated WAV
        wav1 = chap_dir / "audio" / "clips" / "audio_1_001.wav"
        assert wav1.exists()
        with wave.open(str(wav1), "rb") as wf:
            assert wf.getframerate() == 48000
            assert wf.getnchannels() == 1
            assert wf.getsampwidth() == 2

        # Second run: resume valid segments without re-synthesis
        code2 = main(["tts", "-p", "demo_manga", "-c", "1", "--json"])
        assert code2 == ExitCode.SUCCESS
        captured2 = capsys.readouterr()
        data2 = json.loads(captured2.out)["data"]
        assert data2["segments_synthesized"] == 0
        assert data2["segments_cached"] == 2

        # Verify real SHA-256 checksum is populated in script.json
        script_after = json.loads((chap_dir / "script.json").read_text())
        for s in script_after["segments"]:
            assert s["checksum"] is not None
            assert len(s["checksum"]) == 64

        # Verify no .cache_key sidecars are created
        assert not list(chap_dir.glob("**/*.cache_key"))

        # Third run with --force: forces full re-synthesis
        code3 = main(["tts", "-p", "demo_manga", "-c", "1", "--force", "--json"])
        assert code3 == ExitCode.SUCCESS
        captured3 = capsys.readouterr()
        data3 = json.loads(captured3.out)["data"]
        assert data3["segments_synthesized"] == 2
        assert data3["segments_cached"] == 0

    def test_tts_uses_approved_voice_and_character_mapping(self, tmp_path, capsys, monkeypatch):
        """Verifies that tts subcommand adopts primary voice, speed, and character mappings from approved voice_selection.json."""
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))
        init_project_layout("voice_strat_proj", "Voice Strategy Project")
        init_chapter_layout("voice_strat_proj", "1")

        proj_dir = tmp_path / "data" / "projects" / "voice_strat_proj"
        chap_dir = proj_dir / "chapters" / "1"

        script_data = {
            "project_slug": "voice_strat_proj",
            "chapter_number": "1",
            "title": "Strategy Ep 1",
            "segments": [
                {
                    "seq_id": "01",
                    "panel_ids": ["panel_01.jpg"],
                    "character": "Protagonist",
                    "narrator_text": "Ta nhất định phải chiến thắng!",
                    "duration": 2.0,
                },
                {
                    "seq_id": "02",
                    "panel_ids": ["panel_02.jpg"],
                    "narrator_text": "Trận chiến tiếp tục diễn ra vô cùng ác liệt.",
                    "duration": 2.0,
                },
            ],
        }
        (chap_dir / "script.json").write_text(json.dumps(script_data), encoding="utf-8")

        approval_data = {
            "project_slug": "voice_strat_proj",
            "primary_voice": "Mai Anh",
            "default_speed": 1.15,
            "approved_by": "Narrator Director",
            "status": "approved",
            "character_voice_mapping": {
                "Protagonist": "Thiện Minh",
            },
        }
        (proj_dir / "voice_selection.json").write_text(json.dumps(approval_data), encoding="utf-8")

        code = main(["tts", "-p", "voice_strat_proj", "-c", "1", "--mock", "--json"])
        assert code == ExitCode.SUCCESS

        captured = capsys.readouterr()
        res = json.loads(captured.out)
        assert res["status"] == "success"
        assert res["data"]["segments_synthesized"] == 2

        # Check updated script.json recording exact voices used
        saved_script = json.loads((chap_dir / "script.json").read_text(encoding="utf-8"))
        # First segment (Protagonist) uses character mapping Thiện Minh
        assert saved_script["segments"][0]["voice_id"] == "Thiện Minh"
        # Second segment uses approved primary voice Mai Anh
        assert saved_script["segments"][1]["voice_id"] == "Mai Anh"

    def test_tts_voice_priority_manual_vs_approval_update(self, tmp_path, capsys, monkeypatch):
        """
        Regression test: updating voice_selection.json always takes effect on auto-saved segments,
        while strictly preserving segments that the Agent manually assigned a custom voice to.
        """
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))
        init_project_layout("priority_proj", "Priority Project")
        init_chapter_layout("priority_proj", "1")

        proj_dir = tmp_path / "data" / "projects" / "priority_proj"
        chap_dir = proj_dir / "chapters" / "1"

        script_data = {
            "project_slug": "priority_proj",
            "chapter_number": "1",
            "title": "Voice Priority Test",
            "segments": [
                {
                    "seq_id": "01",
                    "panel_ids": ["panel_01.jpg"],
                    "character": "Protagonist",
                    "narrator_text": "Ta là nhân vật chính!",
                },
                {
                    "seq_id": "02",
                    "panel_ids": ["panel_02.jpg"],
                    "voice_id": "Bá Thao",
                    "voice_source": "manual",
                    "narrator_text": "Giọng phản diện này do tác giả đặt thủ công.",
                },
                {
                    "seq_id": "03",
                    "panel_ids": ["panel_03.jpg"],
                    "narrator_text": "Lời dẫn truyện chung của tác phẩm.",
                },
            ],
        }
        (chap_dir / "script.json").write_text(json.dumps(script_data), encoding="utf-8")

        # Initial Approval (V1): Primary voice = "Trúc Ly", Protagonist = "Thiện Minh"
        approval_v1 = {
            "project_slug": "priority_proj",
            "primary_voice": "Trúc Ly",
            "approved_by": "Reviewer A",
            "status": "approved",
            "character_voice_mapping": {
                "Protagonist": "Thiện Minh",
            },
        }
        (proj_dir / "voice_selection.json").write_text(json.dumps(approval_v1), encoding="utf-8")

        # Run TTS initial synthesis
        code = main(["tts", "-p", "priority_proj", "-c", "1", "--mock", "--force", "--json"])
        assert code == ExitCode.SUCCESS

        saved_v1 = json.loads((chap_dir / "script.json").read_text(encoding="utf-8"))
        assert saved_v1["segments"][0]["voice_id"] == "Thiện Minh"
        assert saved_v1["segments"][1]["voice_id"] == "Bá Thao"
        assert saved_v1["segments"][2]["voice_id"] == "Trúc Ly"

        # User updates approval in Evaluation Lab (V2):
        # Change Primary voice to "Mai Anh" and Protagonist to "Quốc Tuấn"
        approval_v2 = {
            "project_slug": "priority_proj",
            "primary_voice": "Mai Anh",
            "approved_by": "Senior Reviewer",
            "status": "approved",
            "character_voice_mapping": {
                "Protagonist": "Quốc Tuấn",
            },
        }
        (proj_dir / "voice_selection.json").write_text(json.dumps(approval_v2), encoding="utf-8")

        # Re-run TTS with --force -> approval updates MUST take effect!
        code2 = main(["tts", "-p", "priority_proj", "-c", "1", "--mock", "--force", "--json"])
        assert code2 == ExitCode.SUCCESS

        saved_v2 = json.loads((chap_dir / "script.json").read_text(encoding="utf-8"))
        # Segment 01 (Protagonist) adopted new mapped voice "Quốc Tuấn"
        assert saved_v2["segments"][0]["voice_id"] == "Quốc Tuấn"
        # Segment 02 (Manual voice) preserved manual assignment "Bá Thao"
        assert saved_v2["segments"][1]["voice_id"] == "Bá Thao"
        # Segment 03 (Narrator) adopted new primary voice "Mai Anh" instead of being stuck on "Trúc Ly"
        assert saved_v2["segments"][2]["voice_id"] == "Mai Anh"


    def test_tts_benchmark_command(self, tmp_path, capsys):
        """Verifies tts_benchmark runs with isolated output dir and does not clobber docs/lab/."""
        bench_out = tmp_path / "cli_bench_out"
        code = main([
            "tts_benchmark",
            "--voices", "Trúc Ly,Adam bựa",
            "--out-dir", str(bench_out),
            "--no-copy-to-lab",
            "--mock",
            "--json",
        ])
        assert code == ExitCode.SUCCESS

        captured = capsys.readouterr()
        data = json.loads(captured.out)["data"]
        assert data["total_voices_tested"] == 2
        assert data["voices"][0] == "Trúc Ly"
        assert Path(data["output_directory"]) == bench_out
        assert (bench_out / "report.json").exists()
        assert (bench_out / "SUMMARY.md").exists()

    def test_tts_benchmark_does_not_modify_lab_when_no_copy(self, tmp_path, capsys):
        """Verifies that running benchmark with --no-copy-to-lab never modifies docs/lab/ assets."""
        lab_data_file = Path("docs/lab/benchmark_data.js")
        before_mtime = lab_data_file.stat().st_mtime_ns if lab_data_file.exists() else None

        bench_out = tmp_path / "isolated_bench"
        code = main([
            "tts_benchmark",
            "--voices", "Trúc Ly",
            "--out-dir", str(bench_out),
            "--no-copy-to-lab",
            "--mock",
            "--json",
        ])
        assert code == ExitCode.SUCCESS

        if before_mtime is not None:
            after_mtime = lab_data_file.stat().st_mtime_ns
            assert after_mtime == before_mtime, (
                "docs/lab/benchmark_data.js was modified despite --no-copy-to-lab!"
            )

    def test_render_final_requires_voice_selection_approval(self, tmp_path, capsys, monkeypatch):
        """Verifies that final render fails without approved voice_selection.json."""
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))
        init_project_layout("review_proj", "Review Proj")
        init_chapter_layout("review_proj", "1")

        chap_dir = tmp_path / "data" / "projects" / "review_proj" / "chapters" / "1"
        script_data = {
            "project_slug": "review_proj",
            "chapter_number": "1",
            "segments": [
                {
                    "seq_id": "01",
                    "panel_ids": ["panel_01.jpg"],
                    "narrator_text": "Sample text",
                    "duration": 2.0,
                }
            ],
        }
        (chap_dir / "script.json").write_text(json.dumps(script_data), encoding="utf-8")

        code = main(["render", "-p", "review_proj", "-c", "1", "--quality", "final", "--json"])
        assert code == ExitCode.VALIDATION_ERROR
        captured = capsys.readouterr()
        assert "Human Review Gate 1" in captured.out

    def test_render_final_passes_with_skip_review(self, tmp_path, capsys, monkeypatch):
        """Verifies that final render can bypass human review gate when --skip-review is passed."""
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))
        init_project_layout("skip_proj", "Skip Proj")
        init_chapter_layout("skip_proj", "1")

        chap_dir = tmp_path / "data" / "projects" / "skip_proj" / "chapters" / "1"
        script_data = {
            "project_slug": "skip_proj",
            "chapter_number": "1",
            "segments": [
                {
                    "seq_id": "01",
                    "panel_ids": ["panel_01.jpg"],
                    "narrator_text": "Sample text",
                    "duration": 2.0,
                }
            ],
        }
        (chap_dir / "script.json").write_text(json.dumps(script_data), encoding="utf-8")

        # Mock VideoEngine.render_chapter to avoid running full FFmpeg in CLI unit test
        with patch("src.cli.render_cmd.VideoEngine") as mock_engine:
            mock_inst = mock_engine.return_value
            mock_inst.render_chapter.return_value = {
                "video_path": str(chap_dir / "video" / "test.mp4"),
                "subtitle_path": str(chap_dir / "subtitles" / "test.srt"),
            }
            code = main(["render", "-p", "skip_proj", "-c", "1", "--quality", "final", "--skip-review", "--json"])
            assert code == ExitCode.SUCCESS

    def test_render_final_succeeds_with_valid_voice_selection_approval(self, tmp_path, capsys, monkeypatch):
        """Verifies that final render succeeds when approved voice_selection.json is provided."""
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))
        init_project_layout("approved_proj", "Approved Proj")
        init_chapter_layout("approved_proj", "1")

        proj_dir = tmp_path / "data" / "projects" / "approved_proj"
        chap_dir = proj_dir / "chapters" / "1"
        script_data = {
            "project_slug": "approved_proj",
            "chapter_number": "1",
            "title": "Approved Chapter",
            "segments": [
                {
                    "seq_id": "01",
                    "panel_ids": ["panel_01.jpg"],
                    "narrator_text": "Sample text",
                    "duration": 2.0,
                }
            ],
        }
        (chap_dir / "script.json").write_text(json.dumps(script_data), encoding="utf-8")

        approval_data = {
            "project_slug": "approved_proj",
            "approved_strategy": "single_narrator",
            "primary_voice": "Trúc Ly",
            "default_speed": 1.0,
            "approved_by": "Senior Editor",
            "status": "approved",
        }
        (proj_dir / "voice_selection.json").write_text(json.dumps(approval_data), encoding="utf-8")

        with patch("src.cli.render_cmd.VideoEngine") as mock_engine:
            mock_inst = mock_engine.return_value
            mock_inst.render_chapter.return_value = {
                "video_path": str(chap_dir / "video" / "test.mp4"),
                "subtitle_path": str(chap_dir / "subtitles" / "test.srt"),
            }
            code = main(["render", "-p", "approved_proj", "-c", "1", "--quality", "final", "--json"])
            assert code == ExitCode.SUCCESS

    def test_render_final_fails_with_invalid_catalog_voice(self, tmp_path, capsys, monkeypatch):
        """Verifies that final render fails when voice_selection.json specifies an unknown voice."""
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))
        init_project_layout("bad_voice_proj", "Bad Voice Proj")
        init_chapter_layout("bad_voice_proj", "1")

        proj_dir = tmp_path / "data" / "projects" / "bad_voice_proj"
        chap_dir = proj_dir / "chapters" / "1"
        script_data = {
            "project_slug": "bad_voice_proj",
            "chapter_number": "1",
            "title": "Bad Voice Project",
            "segments": [
                {
                    "seq_id": "01",
                    "panel_ids": ["panel_01.jpg"],
                    "narrator_text": "Sample text",
                    "duration": 2.0,
                }
            ],
        }
        (chap_dir / "script.json").write_text(json.dumps(script_data), encoding="utf-8")

        bad_approval = {
            "project_slug": "bad_voice_proj",
            "primary_voice": "Unknown_Fake_Voice_99",
            "reviewer": "Reviewer X",
            "status": "approved",
        }
        (proj_dir / "voice_selection.json").write_text(json.dumps(bad_approval), encoding="utf-8")

        code = main(["render", "-p", "bad_voice_proj", "-c", "1", "--quality", "final", "--json"])
        assert code == ExitCode.VALIDATION_ERROR
        captured = capsys.readouterr()
        assert "not recognized in official catalog" in captured.out

