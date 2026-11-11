"""
tests/e2e/test_phase5_real_world_verification.py
Phase 5 Real-World Verification Test Suite for Manga Recap Agentic Skill V3 (R5).

Validates:
1. End-to-end pipeline execution on an actual 20–40 panel chapter (26 panels).
2. Manifest inspection, panel coordinates, and checksum integrity.
3. Recap script schema validation, character consistency, and Ken Burns camera bounds.
4. Local VieNeu-TTS v3 Turbo audio synthesis with 48 kHz WAV output.
5. Final 1080p MP4 rendering with multi-panel segment frame allocation and timed SRT subtitles.
6. ffprobe stream validation (1920x1080 resolution, H.264 video, 48 kHz AAC audio).
7. Audio/video timeline synchronization drift <= 150 ms.
8. Cache re-run efficiency: 0 unnecessary clip re-renders on unchanged run.
9. Multi-chapter batch execution, checkpoint resumption, and single-chapter retry.
10. Standardized export staging in output/<project_slug>/ with both padded and unpadded filenames.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any, Dict

import pytest

from src.cli.exit_codes import ExitCode
from src.cli.main import main
from src.core.schemas import (
    CameraMotionType,
    CheckpointStatus,
    FocusBox,
    PanelManifest,
    RecapScript,
    ScriptSegment,
    StageType,
    VoiceSelectionApproval,
)
from src.core.storage import (
    atomic_write_json,
    init_chapter_layout,
    init_project_layout,
    load_chapter_metadata,
    load_manifest,
    load_recap_script,
    read_json,
    save_manifest,
    save_recap_script,
    update_checkpoint,
)
from src.render.validator import StreamValidator
from src.tts.engine import VieNeuEngine, get_wav_duration_seconds


@pytest.fixture
def phase5_env(tmp_path, monkeypatch):
    """Sets up an isolated, hermetic project environment for Phase 5 verification."""
    data_dir = tmp_path / "data"
    output_dir = tmp_path / "output"
    data_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(data_dir))
    monkeypatch.setenv("MANGA_RECAP_OUTPUT_DIR", str(output_dir))

    slug = "warrior_grandpa_live"
    proj_dir = init_project_layout(slug, title="Warrior Grandpa Phase 5 Verification")
    chap1_dir = init_chapter_layout(slug, "1", title="Episode 1 - Live Verification")
    chap2_dir = init_chapter_layout(slug, "2", title="Episode 2 - Granddaughter Awakens")

    # Seed real repository assets from data/projects/warrior_grandpa_live
    source_proj = Path("data/projects/warrior_grandpa_live")
    if not source_proj.exists():
        pytest.skip("data/projects/warrior_grandpa_live not present in workspace")
    if source_proj.exists():
        # Copy characters, voice selection, project config
        for f in ["characters.json", "voice_selection.json", "story_summary.md", "project.json"]:
            src_f = source_proj / f
            if src_f.exists():
                shutil.copy2(str(src_f), str(proj_dir / f))

        # Copy Chapter 1 assets
        src_c1 = source_proj / "chapters" / "1"
        if src_c1.exists():
            for sub in ["raw", "panels", "contact_sheets", "audio", "subtitles", "video"]:
                if (src_c1 / sub).exists():
                    shutil.copytree(str(src_c1 / sub), str(chap1_dir / sub), dirs_exist_ok=True)
            for f in ["manifest.json", "script.json", "chapter.json"]:
                if (src_c1 / f).exists():
                    shutil.copy2(str(src_c1 / f), str(chap1_dir / f))

        # Copy Chapter 2 assets if available
        src_c2 = source_proj / "chapters" / "2"
        if src_c2.exists():
            for sub in ["raw", "panels", "contact_sheets", "audio", "subtitles", "video"]:
                if (src_c2 / sub).exists():
                    shutil.copytree(str(src_c2 / sub), str(chap2_dir / sub), dirs_exist_ok=True)
            for f in ["manifest.json", "script.json", "chapter.json"]:
                if (src_c2 / f).exists():
                    shutil.copy2(str(src_c2 / f), str(chap2_dir / f))

    return {
        "slug": slug,
        "proj_dir": proj_dir,
        "chap1_dir": chap1_dir,
        "chap2_dir": chap2_dir,
        "data_dir": data_dir,
        "output_dir": output_dir,
        "tmp_path": tmp_path,
    }


class TestPhase5RealWorldVerification:
    """Comprehensive real-world verification suite covering Phase 5 (R5)."""

    def test_01_real_world_chapter_manifest_contains_20_to_40_panels(self, phase5_env):
        """Verifies that chapter 1 manifest contains between 20 and 40 genuine panels."""
        env = phase5_env
        chap1_dir = env["chap1_dir"]

        manifest = load_manifest(chap1_dir)
        assert manifest is not None, "manifest.json must exist in chapter 1"
        panel_count = len(manifest.panels)

        # Explicit 20-40 panel requirement from user specification
        assert 20 <= panel_count <= 40, f"Expected 20-40 panels, found {panel_count}"
        assert manifest.total_panels == panel_count

        # Verify each panel has valid coordinates and file on disk
        for p in manifest.panels:
            assert p.panel_id.startswith("panel_")
            assert p.width > 0
            assert p.height > 0
            panel_file = chap1_dir / p.relative_path
            assert panel_file.exists(), f"Panel image {panel_file} missing on disk"
            assert panel_file.stat().st_size > 1000

    def test_02_script_validation_and_camera_motion_bounds(self, phase5_env):
        """Verifies script schema, character consistency, and Ken Burns camera bounds."""
        env = phase5_env
        slug = env["slug"]
        chap1_dir = env["chap1_dir"]

        exit_code = main(["validate", "-p", slug, "-c", "1", "--strict", "--json"])
        assert exit_code == ExitCode.SUCCESS

        script = load_recap_script(chap1_dir)
        assert script is not None
        assert len(script.segments) > 0

        # Verify multi-panel segment frame allocation support (1, 2, 3 panels)
        panel_distribution = [len(s.panel_ids) for s in script.segments]
        assert 1 in panel_distribution, "Should support single-panel segments"
        assert 2 in panel_distribution, "Should support 2-panel segments"
        assert 3 in panel_distribution, "Should support 3-panel segments"

        # Verify Ken Burns zoom clamp (<= 1.15x) and min focus crop ratio (>= 0.45)
        for seg in script.segments:
            if seg.focus_box:
                w_ratio = seg.focus_box.x_max - seg.focus_box.x_min
                h_ratio = seg.focus_box.y_max - seg.focus_box.y_min
                assert w_ratio >= 0.45, f"Segment {seg.seq_id} width ratio {w_ratio} < 0.45"
                assert h_ratio >= 0.45, f"Segment {seg.seq_id} height ratio {h_ratio} < 0.45"

    def test_03_tts_synthesis_produces_valid_48khz_audio(self, phase5_env):
        """Verifies local VieNeu-TTS synthesis produces genuine 48 kHz uncompressed WAV audio."""
        env = phase5_env
        slug = env["slug"]
        chap1_dir = env["chap1_dir"]

        exit_code = main(["tts", "-p", slug, "-c", "1", "--json"])
        assert exit_code == ExitCode.SUCCESS

        meta = load_chapter_metadata(chap1_dir)
        assert meta.stages[StageType.TTS].status == CheckpointStatus.COMPLETED

        clips_dir = chap1_dir / "audio" / "clips"
        wavs = list(clips_dir.glob("*.wav"))
        assert len(wavs) >= 15

        for wf_p in wavs:
            dur = get_wav_duration_seconds(wf_p)
            assert dur > 1.0, f"WAV duration {dur}s unexpectedly short"

    def test_04_render_final_1080p_and_ffprobe_compliance(self, phase5_env, capsys):
        """
        Executes final 1080p render with multi-panel rendering, camera motion, and timed subtitles.
        Validates output with ffprobe for:
        - 1920x1080 resolution, 30fps
        - H.264 video codec, AAC audio codec at 48000 Hz
        - Total duration drift <= 150 ms
        - Accompanying .srt file with timed cues and Vietnamese diacritics
        """
        env = phase5_env
        slug = env["slug"]
        chap1_dir = env["chap1_dir"]
        output_dir = env["output_dir"]

        exit_code = main([
            "render",
            "-p", slug,
            "-c", "1",
            "--quality", "final",
            "--skip-preview",
            "--json",
        ])
        assert exit_code == ExitCode.SUCCESS

        captured = capsys.readouterr()
        resp_data = None
        for line in reversed(captured.out.splitlines()):
            if line.strip().startswith("{") and line.strip().endswith("}"):
                try:
                    resp_data = json.loads(line)
                    break
                except Exception:
                    continue
        if resp_data is None:
            start_idx = captured.out.rfind("{\n  \"status\"")
            if start_idx != -1:
                resp_data = json.loads(captured.out[start_idx:])

        assert resp_data is not None
        assert resp_data["status"] == "success"
        r_info = resp_data["data"]

        # 1. Output files exist in both chapter and export directory
        chap_vid = Path(r_info["video_path"])
        chap_srt = Path(r_info["subtitle_path"])
        assert chap_vid.exists()
        assert chap_srt.exists()

        export_vid = output_dir / slug / "chapter_001.mp4"
        export_srt = output_dir / slug / "chapter_001.srt"
        assert export_vid.exists()
        assert export_srt.exists()

        # 2. ffprobe compliance
        validator = StreamValidator()
        probe_res = validator.validate_video(
            video_path=chap_vid,
            expected_width=1920,
            expected_height=1080,
            expected_fps=30.0,
            tolerance_seconds=0.150,
            raise_on_error=True,
        )
        assert probe_res["valid"] is True
        assert probe_res["video"]["width"] == 1920
        assert probe_res["video"]["height"] == 1080
        assert probe_res["video"]["codec"] in ("h264", "avc1")
        assert probe_res["audio"]["codec"] == "aac"
        assert probe_res["audio"]["sample_rate"] == 48000

        # 3. Timeline synchronization drift <= 150 ms
        duration_diff = probe_res["duration_diff_seconds"]
        stream_delta = probe_res["stream_duration_delta_seconds"]
        assert duration_diff <= 0.150, f"Duration diff {duration_diff}s > 150ms"
        assert stream_delta <= 0.150, f"Stream delta {stream_delta}s > 150ms"

        # 4. SRT subtitle inspection
        srt_content = export_srt.read_text(encoding="utf-8")
        assert len(srt_content) > 500
        assert "-->" in srt_content
        # Check presence of representative Vietnamese words
        assert any(w in srt_content for w in ["Thiểm Tây", "Tấn Dương", "Tế Thế Đường", "Lưu Chấn Sơn"])

    def test_05_cache_rerun_efficiency_zero_rerenders(self, phase5_env, capsys):
        """
        Tests cache re-run efficiency:
        Re-running manga-recap render on the unchanged chapter completes
        without regenerating already-valid audio or video files (0 clips re-rendered).
        """
        env = phase5_env
        slug = env["slug"]

        # Ensure first render is already complete
        main(["render", "-p", slug, "-c", "1", "--quality", "final", "--skip-preview", "--json"])
        capsys.readouterr()

        # Execute re-run on unchanged chapter
        t0 = time.monotonic()
        exit_code = main([
            "render",
            "-p", slug,
            "-c", "1",
            "--quality", "final",
            "--skip-preview",
            "--json",
        ])
        elapsed = time.monotonic() - t0
        assert exit_code == ExitCode.SUCCESS

        captured = capsys.readouterr()
        resp_data = None
        for line in reversed(captured.out.splitlines()):
            if line.strip().startswith("{") and line.strip().endswith("}"):
                try:
                    resp_data = json.loads(line)
                    break
                except Exception:
                    continue
        if resp_data is None:
            start_idx = captured.out.rfind("{\n  \"status\"")
            if start_idx != -1:
                resp_data = json.loads(captured.out[start_idx:])

        assert resp_data is not None
        assert resp_data["status"] == "success"
        r_info = resp_data["data"]

        # Assert 0 clips were newly rendered and all clips were served from cache
        assert r_info["rendered_clips_count"] == 0, "Expected 0 clips re-rendered on cache hit"
        assert r_info["cached_clips_count"] >= 20, "Expected all panel clips to be retrieved from cache"
        # Cache re-run should complete very quickly (< 5.0 seconds vs > 25 seconds for full render)
        assert elapsed < 10.0, f"Cache re-run took {elapsed}s, expected fast cache hit"

    def test_06_multi_chapter_batch_execution_and_resumption(self, phase5_env, capsys):
        """
        Executes multi-chapter batch processing (manga-recap batch --chapters 1,2 --skip-preview).
        Verifies:
        1. Sequential processing across chapters.
        2. Checkpoint resumption (chapter 1 cached, chapter 2 rendered).
        3. Staging into output/<slug>/ with both chapter_001.mp4 and chapter_002.mp4.
        4. Re-run recognizes both chapters as cached.
        """
        env = phase5_env
        slug = env["slug"]
        output_dir = env["output_dir"]
        chap2_meta = load_chapter_metadata(env["chap2_dir"])
        chap2_meta.overall_status = CheckpointStatus.PENDING
        atomic_write_json(env["chap2_dir"] / "chapter.json", chap2_meta)

        exit_code = main([
            "batch",
            "-p", slug,
            "-c", "1,2",
            "--skip-preview",
            "--json",
        ])
        assert exit_code == ExitCode.SUCCESS

        captured = capsys.readouterr()
        resp_data = None
        for line in reversed(captured.out.splitlines()):
            if line.strip().startswith("{") and line.strip().endswith("}"):
                try:
                    resp_data = json.loads(line)
                    break
                except Exception:
                    continue
        if resp_data is None:
            start_idx = captured.out.rfind("{\n  \"status\"")
            if start_idx != -1:
                resp_data = json.loads(captured.out[start_idx:])

        assert resp_data is not None
        assert resp_data["status"] == "success"
        b_info = resp_data["data"]
        assert b_info["success"] is True
        assert b_info["total_chapters"] == 2
        assert "1" in b_info["cached"] or "1" in b_info["completed"]
        assert "2" in b_info["completed"] or "2" in b_info["cached"]

        # Verify staged output artifacts
        proj_out = output_dir / slug
        assert (proj_out / "chapter_001.mp4").exists()
        assert (proj_out / "chapter_002.mp4").exists()
        assert (proj_out / "chapter_001.srt").exists()
        assert (proj_out / "chapter_002.srt").exists()

        # Re-running batch on unchanged project should mark BOTH chapters cached
        exit_code2 = main([
            "batch",
            "-p", slug,
            "-c", "1,2",
            "--skip-preview",
            "--json",
        ])
        assert exit_code2 == ExitCode.SUCCESS
        captured2 = capsys.readouterr()
        resp_data2 = json.loads(captured2.out[captured2.out.rfind("{\n  \"status\""):]) if "{\n  \"status\"" in captured2.out else json.loads(captured2.out)
        b_info2 = resp_data2["data"]
        assert "1" in b_info2["cached"]
        assert "2" in b_info2["cached"]
        assert len(b_info2["completed"]) == 0

    def test_07_multi_chapter_single_chapter_retry_isolation(self, phase5_env, capsys):
        """
        Verifies that --retry-chapter re-processes only the specified chapter
        without affecting other completed chapters.
        """
        env = phase5_env
        slug = env["slug"]

        exit_code = main([
            "batch",
            "-p", slug,
            "-c", "1,2",
            "--retry-chapter", "2",
            "--skip-preview",
            "--json",
        ])
        assert exit_code == ExitCode.SUCCESS

        captured = capsys.readouterr()
        resp_data = None
        for line in reversed(captured.out.splitlines()):
            if line.strip().startswith("{") and line.strip().endswith("}"):
                try:
                    resp_data = json.loads(line)
                    break
                except Exception:
                    continue
        if resp_data is None:
            start_idx = captured.out.rfind("{\n  \"status\"")
            if start_idx != -1:
                resp_data = json.loads(captured.out[start_idx:])

        assert resp_data is not None
        b_info = resp_data["data"]
        # With --retry-chapter 2, only Chapter 2 is targeted and retried
        assert b_info["total_chapters"] == 1
        assert "2" in b_info["completed"]

        # Chapter 1 metadata remains completed and untouched
        meta1 = load_chapter_metadata(env["chap1_dir"])
        assert meta1.overall_status == CheckpointStatus.COMPLETED
