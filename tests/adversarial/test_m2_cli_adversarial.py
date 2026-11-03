"""
tests/adversarial/test_m2_cli_adversarial.py
Adversarial stress-test suite for Milestone M2 (PR-02 Deterministic CLI Suite & PR-03 Agentic Skill).
Authored by M2 Challenger 1 to empirically verify:
1. CLI Exit Codes across edge scenarios (0, 1, 2, 3, 4)
2. 100% Stdout Purity & JSON Parseability via direct subprocess execution
3. Argument fuzzing and error envelope handling
4. Inspect and Status command schema integrity
5. Defect regression verification (cleanup safety, FFmpeg returncodes, suffix versioning)
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import wave
import pytest

from src.cli.exit_codes import ExitCode
from src.cli.response import JsonEnvelope
from src.core.storage import (
    atomic_write_json,
    cleanup_intermediate_files,
    init_chapter_layout,
    init_project_layout,
    update_checkpoint,
)
from src.core.utils import (
    FFmpegExecutionError,
    get_next_video_path,
    resolve_video_output_path,
    run_ffmpeg,
)

PYTHON_BIN = sys.executable


def run_cli(args: list[str], env_overrides: dict[str, str] = None) -> subprocess.CompletedProcess:
    """Executes the CLI in a separate OS process to capture real stdout/stderr."""
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    cmd = [PYTHON_BIN, "-m", "src.cli.main"] + args
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


class TestCliExitCodesAndPurity:
    """Empirical challenge on CLI exit codes and 100% stdout purity."""

    def test_version_flag_success(self):
        proc = run_cli(["--version"])
        assert proc.returncode == 0
        assert "3.0.0" in proc.stdout or "3.0.0" in proc.stderr

    def test_missing_project_returns_io_error_exit_code_3(self):
        for subcmd in ["inspect", "status", "validate", "tts", "render", "compile"]:
            args = [subcmd, "-p", "nonexistent_slug_9999", "--json"]
            if subcmd in ["tts", "render"]:
                args += ["-c", "1"]
            proc = run_cli(args)
            assert proc.returncode == ExitCode.IO_ERROR, f"{subcmd} did not return exit code 3"
            
            # Stdout must be strictly parseable as single JSON envelope
            envelope = json.loads(proc.stdout)
            assert envelope["status"] == "error"
            assert envelope["exit_code"] == ExitCode.IO_ERROR
            assert envelope["error"]["code"] == "IO_ERROR"

    def test_missing_dependency_returns_exit_code_4(self, tmp_path):
        env = {"PATH": "", "MANGA_RECAP_DATA_DIR": str(tmp_path / "data")}
        init_project_layout("dep_test", "Dep Test")
        init_chapter_layout("dep_test", "1")

        for subcmd in ["render", "compile"]:
            args = [subcmd, "-p", "dep_test", "--json"]
            if subcmd == "render":
                args += ["-c", "1"]
            proc = run_cli(args, env_overrides=env)
            assert proc.returncode == ExitCode.DEPENDENCY_ERROR
            
            envelope = json.loads(proc.stdout)
            assert envelope["status"] == "error"
            assert envelope["exit_code"] == ExitCode.DEPENDENCY_ERROR
            assert envelope["error"]["code"] == "DEPENDENCY_ERROR"
            assert "not found on system PATH" in envelope["error"]["message"]

    def test_ffmpeg_execution_failure_returns_general_error_exit_code_1(self, tmp_path):
        env = {"MANGA_RECAP_DATA_DIR": str(tmp_path / "data")}
        init_project_layout("bad_video_proj", "Bad Video Proj")
        chap_dir = init_chapter_layout("bad_video_proj", "1")
        (chap_dir / "video").mkdir(parents=True, exist_ok=True)
        # Corrupted input file
        (chap_dir / "video" / "chapter_1_final_v1.mp4").write_bytes(b"INVALID_VIDEO_HEADER_DATA")

        proc = run_cli(["compile", "-p", "bad_video_proj", "--json"], env_overrides=env)
        assert proc.returncode == ExitCode.GENERAL_ERROR
        
        envelope = json.loads(proc.stdout)
        assert envelope["status"] == "error"
        assert envelope["exit_code"] == ExitCode.GENERAL_ERROR
        assert envelope["error"]["code"] == "GENERAL_ERROR"
        assert "Compile chapter video concatenation failed" in envelope["error"]["message"]

    def test_crawl_missing_source_flags_returns_validation_error_exit_code_2(self):
        proc = run_cli(["crawl", "-p", "dummy", "-c", "1", "--json"])
        assert proc.returncode == ExitCode.VALIDATION_ERROR
        envelope = json.loads(proc.stdout)
        assert envelope["status"] == "error"
        assert envelope["exit_code"] == ExitCode.VALIDATION_ERROR
        assert envelope["error"]["code"] == "VALIDATION_ERROR"
        assert "Either --url or --import-dir must be provided" in envelope["error"]["message"]

    def test_schema_validation_failure_returns_exit_code_2(self, tmp_path):
        env = {"MANGA_RECAP_DATA_DIR": str(tmp_path / "data")}
        init_project_layout("invalid_schema_proj", "Invalid Schema Proj")
        chap_dir = init_chapter_layout("invalid_schema_proj", "1")
        # Script references non-existent panel
        atomic_write_json(chap_dir / "manifest.json", {
            "schema_version": "3.0.0",
            "project_slug": "invalid_schema_proj",
            "chapter_number": "1",
            "total_panels": 1,
            "panels": [
                {
                    "panel_id": "panel_001",
                    "sequence_number": 1,
                    "filename": "panel_001.jpg",
                    "relative_path": "chapters/1/panels/panel_001.jpg",
                    "source_image": "raw_1.jpg",
                    "crop_box": [0, 0, 100, 100],
                    "width": 100,
                    "height": 100,
                    "aspect_ratio": 1.0,
                    "sha256": "0" * 64,
                    "status": "active"
                }
            ]
        })
        # Note: panel_001.jpg is missing on disk!

        proc = run_cli(["validate", "-p", "invalid_schema_proj", "-c", "1", "--json"], env_overrides=env)
        assert proc.returncode == ExitCode.VALIDATION_ERROR
        envelope = json.loads(proc.stdout)
        assert envelope["status"] == "error"
        assert envelope["exit_code"] == ExitCode.VALIDATION_ERROR
        assert envelope["data"]["valid"] is False

    def test_unrecognized_argument_emits_single_parseable_json_envelope(self):
        """
        Adversarial test revealing stdout pollution:
        When an unrecognized argument is passed with --json, JsonAwareArgumentParser
        prints a JSON response and raises SystemExit. Then main()'s except block
        catches SystemExit and prints a second JSON response, causing JSONDecodeError.
        """
        proc = run_cli(["inspect", "-p", "demo", "--unrecognized-flag-xyz", "--json"])
        assert proc.returncode == ExitCode.VALIDATION_ERROR
        
        # This will fail with JSONDecodeError: Extra data if duplicate JSONs are printed
        envelope = json.loads(proc.stdout)
        assert envelope["status"] == "error"
        assert envelope["exit_code"] == ExitCode.VALIDATION_ERROR

    def test_missing_subcommand_with_json_emits_single_parseable_json_envelope(self):
        proc = run_cli(["--json"])
        assert proc.returncode == ExitCode.VALIDATION_ERROR
        
        envelope = json.loads(proc.stdout)
        assert envelope["status"] == "error"
        assert envelope["exit_code"] == ExitCode.VALIDATION_ERROR

    def test_invalid_subcommand_with_json_emits_single_parseable_json_envelope(self):
        proc = run_cli(["invalid_subcommand", "--json"])
        assert proc.returncode == ExitCode.VALIDATION_ERROR
        
        envelope = json.loads(proc.stdout)
        assert envelope["status"] == "error"
        assert envelope["exit_code"] == ExitCode.VALIDATION_ERROR

    def test_permission_denied_returns_io_error_exit_code_3(self, tmp_path):
        """
        Adversarial test revealing exit code discrepancy:
        PermissionError is not caught in main.py's IO_ERROR block, falling into GENERAL_ERROR (1).
        """
        env = {"MANGA_RECAP_DATA_DIR": str(tmp_path / "data")}
        proj_dir = init_project_layout("locked_proj", "Locked Proj")
        os.chmod(proj_dir, 0o000)
        try:
            proc = run_cli(["inspect", "-p", "locked_proj", "--json"], env_overrides=env)
            assert proc.returncode == ExitCode.IO_ERROR, f"Expected 3, got {proc.returncode}"
            envelope = json.loads(proc.stdout)
            assert envelope["error"]["code"] == "IO_ERROR"
        finally:
            os.chmod(proj_dir, 0o755)

    def test_stdout_purity_under_verbose_logging(self, tmp_path):
        env = {"MANGA_RECAP_DATA_DIR": str(tmp_path / "data")}
        init_project_layout("purity_proj", "Purity Proj")
        init_chapter_layout("purity_proj", "1")

        # Run with -v and --verbose
        for flag in ["-v", "--verbose"]:
            proc = run_cli(["status", "-p", "purity_proj", "-c", "1", flag, "--json"], env_overrides=env)
            assert proc.returncode == ExitCode.SUCCESS
            
            # Stdout must be strictly pure JSON
            envelope = json.loads(proc.stdout)
            assert envelope["status"] == "success"
            assert envelope["command"] == "status"
            assert envelope["exit_code"] == 0
            assert envelope["data"]["chapter_number"] == "1"


class TestInspectAndStatusCommandIntegrity:
    """Verifies schema conformance of inspect and status commands."""

    @pytest.fixture
    def populated_project(self, tmp_path):
        env_dir = tmp_path / "data"
        os.environ["MANGA_RECAP_DATA_DIR"] = str(env_dir)

        proj_dir = init_project_layout("epic_saga", "The Epic Saga")
        chap_dir = init_chapter_layout("epic_saga", "1", "Awakening")

        # Write characters.json
        atomic_write_json(proj_dir / "characters.json", {
            "schema_version": "3.0.0",
            "characters": {
                "c1": {"name": "Grandpa", "role": "mentor", "voice_id": "Quang Sơn"}
            }
        })

        # Populate panel & manifest
        p1 = chap_dir / "panels" / "panel_001.jpg"
        p1.write_bytes(b"dummy_panel_content")
        atomic_write_json(chap_dir / "manifest.json", {
            "schema_version": "3.0.0",
            "project_slug": "epic_saga",
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
                    "sha256": "a" * 64,
                    "status": "active"
                }
            ],
            "contact_sheets": ["chapters/1/contact_sheets/sheet_001.jpg"]
        })

        # Populate script
        audio_clip = chap_dir / "audio" / "clips" / "audio_1_001.wav"
        with wave.open(str(audio_clip), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(48000)
            wf.writeframes(b"\x00\x00" * 48000 * 3)  # 3 seconds

        atomic_write_json(chap_dir / "script.json", {
            "schema_version": "3.0.0",
            "project_slug": "epic_saga",
            "chapter_number": "1",
            "title": "Awakening",
            "segments": [
                {
                    "seq_id": "001",
                    "panel_ids": ["panel_001"],
                    "narrator_text": "Tiếng sấm rền vang trên bầu trời u tối.",
                    "status": "synthesized",
                    "audio_path": "audio/clips/audio_1_001.wav",
                    "duration": 3.0
                }
            ]
        })

        # Video placeholder
        vid_file = chap_dir / "video" / "chapter_1_preview_v1.mp4"
        vid_file.write_bytes(b"fake_mp4_bytes")

        return proj_dir, chap_dir

    def test_inspect_project_schema_conformance(self, populated_project):
        proc = run_cli(["inspect", "-p", "epic_saga", "--json"])
        assert proc.returncode == ExitCode.SUCCESS
        envelope = json.loads(proc.stdout)
        assert JsonEnvelope.model_validate(envelope)
        data = envelope["data"]
        
        assert data["project_slug"] == "epic_saga"
        assert data["title"] == "The Epic Saga"
        assert data["total_chapters"] == 1
        assert len(data["chapters"]) == 1
        assert data["chapters"][0]["chapter_number"] == "1"
        assert data["characters_count"] == 1
        assert data["has_story_summary"] is True

    def test_inspect_chapter_schema_conformance(self, populated_project):
        proc = run_cli(["inspect", "-p", "epic_saga", "-c", "1", "--json"])
        assert proc.returncode == ExitCode.SUCCESS
        envelope = json.loads(proc.stdout)
        assert JsonEnvelope.model_validate(envelope)
        data = envelope["data"]

        assert data["project_slug"] == "epic_saga"
        assert data["chapter_number"] == "1"
        assets = data["assets"]
        assert assets["panels_count"] == 1
        assert assets["contact_sheets_count"] == 1
        assert assets["audio_clips_count"] == 1
        assert assets["total_audio_duration_seconds"] == 3.0
        assert assets["script_segments_count"] == 1
        assert len(assets["video_files"]) == 1
        assert assets["video_files"][0]["filename"] == "chapter_1_preview_v1.mp4"

    def test_status_project_overview(self, populated_project):
        proc = run_cli(["status", "-p", "epic_saga", "--json"])
        assert proc.returncode == ExitCode.SUCCESS
        envelope = json.loads(proc.stdout)
        assert JsonEnvelope.model_validate(envelope)
        data = envelope["data"]

        assert data["project_slug"] == "epic_saga"
        assert data["total_chapters"] == 1
        assert data["chapters"][0]["chapter_number"] == "1"
        assert data["chapters"][0]["resumable_stage"] == "crawl"

    def test_status_chapter_state_machine_advancement(self, populated_project):
        proj_dir, chap_dir = populated_project

        # Initially resumable stage is crawl
        proc = run_cli(["status", "-p", "epic_saga", "-c", "1", "--json"])
        envelope = json.loads(proc.stdout)
        assert envelope["data"]["resumable_stage"] == "crawl"

        # Advance crawl -> segment
        update_checkpoint(chap_dir, "crawl", "completed")
        proc = run_cli(["status", "-p", "epic_saga", "-c", "1", "--json"])
        envelope = json.loads(proc.stdout)
        assert envelope["data"]["resumable_stage"] == "segment"

        # Advance segment -> script
        update_checkpoint(chap_dir, "segment", "completed")
        proc = run_cli(["status", "-p", "epic_saga", "-c", "1", "--json"])
        envelope = json.loads(proc.stdout)
        assert envelope["data"]["resumable_stage"] == "script"


class TestDefectRegressionsAdversarial:
    """Verifies that legacy defects remain permanently remediated."""

    def test_cleanup_intermediate_files_never_deletes_production_assets(self, tmp_path):
        chap_dir = tmp_path / "chapters" / "1"
        chap_dir.mkdir(parents=True)
        (chap_dir / "raw").mkdir()
        (chap_dir / "panels").mkdir()
        (chap_dir / "video").mkdir()

        # Production assets
        p_json = chap_dir / "chapter.json"
        s_json = chap_dir / "script.json"
        raw_img = chap_dir / "raw" / "page_001.jpg"
        panel_img = chap_dir / "panels" / "panel_001.jpg"
        final_mp4 = chap_dir / "video" / "chapter_1_final_v1.mp4"

        p_json.write_text("{}")
        s_json.write_text("{}")
        raw_img.write_bytes(b"raw")
        panel_img.write_bytes(b"panel")
        final_mp4.write_bytes(b"video")

        # Scratch temp files
        temp_mp4 = chap_dir / "video" / "chapter_1_raw_temp.mp4"
        temp_srt = chap_dir / "video" / "chapter_1_subs_temp.srt"
        concat_txt = chap_dir / "compile_concat_list.txt"

        temp_mp4.write_bytes(b"temp")
        temp_srt.write_text("temp")
        concat_txt.write_text("temp")

        unlinked = cleanup_intermediate_files(chap_dir, keep_temp=False)
        assert len(unlinked) == 3

        # Scratch files deleted
        assert not temp_mp4.exists()
        assert not temp_srt.exists()
        assert not concat_txt.exists()

        # Production assets MUST remain untouched
        assert p_json.exists(), "chapter.json was deleted!"
        assert s_json.exists(), "script.json was deleted!"
        assert raw_img.exists(), "raw image was deleted!"
        assert panel_img.exists(), "panel crop was deleted!"
        assert final_mp4.exists(), "final video was deleted!"

    def test_suffix_versioning_increments_monotonically(self, tmp_path):
        chap_dir = tmp_path / "chapter_1"
        chap_dir.mkdir()

        # Generate preview versions 1..5
        paths = []
        for i in range(1, 6):
            v_path = resolve_video_output_path(chap_dir, 1, suffix="preview")
            assert v_path.name == f"chapter_1_preview_v{i}.mp4"
            v_path.write_bytes(b"content")
            paths.append(v_path)

        # Separate suffix 'final' starts independently at v1
        final_v1 = resolve_video_output_path(chap_dir, 1, suffix="final")
        assert final_v1.name == "chapter_1_final_v1.mp4"
        final_v1.write_bytes(b"content")

        final_v2 = resolve_video_output_path(chap_dir, 1, suffix="final")
        assert final_v2.name == "chapter_1_final_v2.mp4"

    def test_versioning_numeric_sort_beyond_nine(self, tmp_path):
        """Auto-increment must sort numerically (v10 > v9) not lexicographically."""
        chap_dir = tmp_path / "chap_high"
        video_dir = chap_dir / "video"
        video_dir.mkdir(parents=True, exist_ok=True)

        for i in range(1, 16):
            (video_dir / f"chapter_1_preview_v{i}.mp4").touch()

        next_path = resolve_video_output_path(chap_dir, 1, suffix="preview")
        assert next_path.name == "chapter_1_preview_v16.mp4", (
            f"Expected v16, got {next_path.name} (possible lexicographical sort bug)"
        )

    def test_cleanup_with_keep_temp_preserves_scratch_files(self, tmp_path):
        """When keep_temp=True, scratch files must NOT be unlinked."""
        chap_dir = tmp_path / "chapter_keep"
        chap_dir.mkdir(parents=True)
        temp_mp4 = chap_dir / "chapter_1_raw_temp.mp4"
        temp_mp4.write_text("scratch")

        unlinked = cleanup_intermediate_files(chap_dir, keep_temp=True)
        assert len(unlinked) == 0
        assert temp_mp4.exists()

    def test_run_ffmpeg_direct_failure_raises_ffmpeg_execution_error(self):
        """Direct call to run_ffmpeg with failing command raises FFmpegExecutionError."""
        if not shutil.which("ffmpeg"):
            pytest.skip("FFmpeg not installed")

        invalid_cmd = [
            "ffmpeg", "-f", "lavfi", "-i", "nullsrc=s=64x64:d=1",
            "-c:v", "nonexistent_bogus_codec_xyz", "-f", "null", "-"
        ]
        with pytest.raises(FFmpegExecutionError) as exc_info:
            run_ffmpeg(invalid_cmd, description="Invalid codec transcode")

        assert exc_info.value.returncode != 0
        assert "Invalid codec transcode failed" in str(exc_info.value)
        assert len(exc_info.value.stderr) > 0

    @pytest.mark.parametrize("fuzzed_args", [
        ["crawl", "-p", "proj", "-c", "1", "--max-panels", "not_a_number"],
        ["tts", "-p", "proj", "-c", "1", "--speed", "extremely_fast"],
        ["render", "-p", "proj", "-c", "1", "--quality", "ultra_hd_4k"],
    ])
    def test_subcommand_argument_fuzzing_exit_code_2(self, fuzzed_args: list[str]):
        """Subcommand argument fuzzing (bad types and choices) must return exit code 2."""
        proc = run_cli(fuzzed_args)
        assert proc.returncode == ExitCode.VALIDATION_ERROR

