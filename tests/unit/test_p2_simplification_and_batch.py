"""
tests/unit/test_p2_simplification_and_batch.py
Comprehensive regression and unit tests for Phase 3 — P2:
1. 5 operational stages and backward-compatible aliases.
2. Review Gates consolidation (Gate 0, Gate 1 series-level inheritance, Gate 2 --skip-preview).
3. Sequential batch processing, chapter failure isolation, and single-chapter retry.
4. Clean code architecture (absence of Supabase, YouTube, and cloud dependencies).
"""

import argparse
import ast
from pathlib import Path
from unittest.mock import patch

import pytest

from src.cli.batch_cmd import handle_batch, parse_chapter_list
from src.cli.compile_cmd import handle_compile
from src.cli.exit_codes import CliValidationError
from src.cli.render_cmd import handle_render
from src.core.schemas import (
    OPERATIONAL_STAGES,
    STAGE_ALIAS_MAP,
    CheckpointStatus,
    ProjectConfig,
    RecapScript,
    RenderSettings,
    ScriptSegment,
    StageCheckpoint,
    StageType,
    VoiceSelectionApproval,
    default_stages_factory,
)
from src.core.storage import (
    STAGE_EXECUTION_ORDER,
    atomic_write_json,
    get_chapter_dir,
    get_project_dir,
    get_resumable_stage,
    init_chapter_layout,
    init_project_layout,
    load_chapter_metadata,
    read_json,
    update_checkpoint,
)
from src.render.video_engine import VideoEngine


# ==============================================================================
# 1. 5 Operational Stages & Alias Mapping Tests
# ==============================================================================

class TestP2StageTypeMapping:
    """Tests the 5 operational stages and backward-compatible aliases."""

    def test_operational_stages_enum(self):
        """Verify StageType contains 5 operational stages and legacy aliases."""
        # 5 Operational stages
        assert StageType.INGEST == "ingest"
        assert StageType.AGENT == "agent"
        assert StageType.AUDIO == "audio"
        assert StageType.RENDER == "render"
        assert StageType.EXPORT == "export"

        # Legacy aliases
        assert StageType.CRAWL == "crawl"
        assert StageType.SEGMENT == "segment"
        assert StageType.SCRIPT == "script"
        assert StageType.TTS == "tts"
        assert StageType.FINALIZE == "finalize"

        assert len(OPERATIONAL_STAGES) == 5
        assert StageType.INGEST in OPERATIONAL_STAGES
        assert StageType.EXPORT in OPERATIONAL_STAGES

        assert STAGE_ALIAS_MAP[StageType.CRAWL] == StageType.INGEST
        assert STAGE_ALIAS_MAP[StageType.SEGMENT] == StageType.INGEST
        assert STAGE_ALIAS_MAP[StageType.SCRIPT] == StageType.AGENT
        assert STAGE_ALIAS_MAP[StageType.TTS] == StageType.AUDIO
        assert STAGE_ALIAS_MAP[StageType.FINALIZE] == StageType.EXPORT

    def test_default_stages_factory_coverage(self):
        """Verify default_stages_factory initializes all stages as PENDING."""
        stages = default_stages_factory()
        assert len(stages) == len(StageType)
        for stage in StageType:
            assert stage in stages
            assert isinstance(stages[stage], StageCheckpoint)
            assert stages[stage].status == CheckpointStatus.PENDING

    def test_alias_checkpoint_updates(self, tmp_path, monkeypatch):
        """Verify checkpoints can be updated with operational stages."""
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))
        init_project_layout("p2_alias_proj", "P2 Alias Project")
        chap_dir = init_chapter_layout("p2_alias_proj", 1)

        # Update operational stages
        meta = update_checkpoint(chap_dir, StageType.INGEST, CheckpointStatus.RUNNING)
        assert meta.stages[StageType.INGEST].status == CheckpointStatus.RUNNING
        assert meta.overall_status == CheckpointStatus.RUNNING

        meta = update_checkpoint(chap_dir, StageType.INGEST, CheckpointStatus.COMPLETED, artifacts=["raw/001.jpg"])
        assert meta.stages[StageType.INGEST].status == CheckpointStatus.COMPLETED
        assert "raw/001.jpg" in meta.stages[StageType.INGEST].artifacts

        # Test get_resumable_stage recognizes operational stages
        resumable = get_resumable_stage(chap_dir)
        assert resumable == StageType.AGENT

    def test_elimination_of_finalize_dead_code(self, tmp_path, monkeypatch):
        """Verify completing RENDER completes chapter checkpoint without phantom FINALIZE."""
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))
        init_project_layout("p2_fin_proj", "P2 Fin Project")
        chap_dir = init_chapter_layout("p2_fin_proj", 1)

        # Mark all prior operational stages completed
        for stg in [StageType.INGEST, StageType.AGENT, StageType.AUDIO]:
            update_checkpoint(chap_dir, stg, CheckpointStatus.RUNNING)
            update_checkpoint(chap_dir, stg, CheckpointStatus.COMPLETED)

        # Completing RENDER directly marks chapter as COMPLETED
        update_checkpoint(chap_dir, StageType.RENDER, CheckpointStatus.RUNNING)
        meta = update_checkpoint(chap_dir, StageType.RENDER, CheckpointStatus.COMPLETED, artifacts=["video/final.mp4"])

        assert meta.overall_status == CheckpointStatus.COMPLETED
        assert get_resumable_stage(chap_dir) is None

        # Backward compatibility: legacy update to FINALIZE doesn't error
        meta_fin = update_checkpoint(chap_dir, StageType.FINALIZE, CheckpointStatus.COMPLETED)
        assert meta_fin.overall_status == CheckpointStatus.COMPLETED


# ==============================================================================
# 2. Review Gates Consolidation Tests
# ==============================================================================

class TestP2ReviewGatesConsolidation:
    """Tests Gate 0, Gate 1 series-level inheritance, and Gate 2 --skip-preview."""

    def test_gate_0_series_config_inheritance(self, tmp_path, monkeypatch):
        """Verify render command inherits Gate 0 settings from project.json."""
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))
        proj_dir = init_project_layout("p2_gate0_proj", "Gate 0 Project")
        chap_dir = init_chapter_layout("p2_gate0_proj", 1)

        # Configure project.json with specific settings
        p_cfg = read_json(proj_dir / "project.json")
        p_cfg["render_settings"]["video_mode"] = "landscape_fit"
        p_cfg["render_settings"]["bgm_path"] = "data/bgm_series.mp3"
        p_cfg["render_settings"]["burn_subtitles"] = False
        atomic_write_json(proj_dir / "project.json", p_cfg)

        # Script must exist
        script = RecapScript(
            project_slug="p2_gate0_proj",
            chapter_number="1",
            title="Ep 1",
            segments=[ScriptSegment(seq_id="001", panel_ids=["panel_001.jpg"], narrator_text="Sample text for testing")],
        )
        atomic_write_json(chap_dir / "script.json", script)

        # Approve Gate 1 at series level
        voice_sel = {
            "approved_strategy": "single_narrator",
            "primary_voice": "Trúc Ly",
            "speed": 1.0,
            "reviewer": "tester",
            "project_slug": "p2_gate0_proj",
            "status": "approved",
        }
        atomic_write_json(proj_dir / "voice_selection.json", voice_sel)

        # Mock VideoEngine.render_chapter to verify inherited arguments
        captured_kwargs = {}

        def mock_render(self, *args, **kwargs):
            nonlocal captured_kwargs
            captured_kwargs = kwargs
            return {
                "video_path": str(chap_dir / "video" / "chapter_1_final.mp4"),
                "subtitle_path": str(chap_dir / "subtitles" / "chapter_1.srt"),
                "quality": "final",
            }

        monkeypatch.setattr(VideoEngine, "render_chapter", mock_render)

        # Call handle_render with default arguments (no explicit bgm or video_mode)
        render_args = argparse.Namespace(
            project="p2_gate0_proj",
            chapter=1,
            quality="final",
            skip_preview=True,
            approved=False,
            force=False,
            allow_placeholders=True,
            keep_temp=False,
        )
        handle_render(render_args)

        # Assert inherited settings were used
        assert captured_kwargs.get("video_mode") == "landscape_fit"
        assert captured_kwargs.get("bgm_path") == "data/bgm_series.mp3"
        assert captured_kwargs.get("burn_subtitles") is False

    def test_gate_1_project_level_inheritance(self, tmp_path, monkeypatch):
        """Verify series-level voice_selection.json satisfies Gate 1 for all chapters."""
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))
        proj_dir = init_project_layout("p2_gate1_proj", "Gate 1 Series Project")
        chap1_dir = init_chapter_layout("p2_gate1_proj", 1)
        chap2_dir = init_chapter_layout("p2_gate1_proj", 2)

        # Approve voice selection at project root
        voice_sel = {
            "approved_strategy": "single_narrator",
            "primary_voice": "Trúc Ly",
            "speed": 1.0,
            "reviewer": "lead_editor",
            "project_slug": "p2_gate1_proj",
            "status": "approved",
        }
        atomic_write_json(proj_dir / "voice_selection.json", voice_sel)

        # Chapters do NOT have chapter-level voice_selection.json
        assert not (chap1_dir / "voice_selection.json").exists()
        assert not (chap2_dir / "voice_selection.json").exists()

        # Create dummy chapter videos
        v1 = chap1_dir / "video" / "chapter_1_final.mp4"
        v1.parent.mkdir(parents=True, exist_ok=True)
        v1.write_bytes(b"mp4_chap1")

        v2 = chap2_dir / "video" / "chapter_2_final.mp4"
        v2.parent.mkdir(parents=True, exist_ok=True)
        v2.write_bytes(b"mp4_chap2")

        # Create dummy project-level preview approval so Gate 2 also passes
        prev_approval = {
            "reviewer": "lead_editor",
            "project_slug": "p2_gate1_proj",
            "status": "approved",
        }
        atomic_write_json(proj_dir / "preview_approval.json", prev_approval)

        # Mock FFmpeg execution in compile_cmd
        with patch("src.cli.compile_cmd.run_ffmpeg") as mock_ffmpeg:
            mock_ffmpeg.return_value = 0
            (proj_dir / "video").mkdir(parents=True, exist_ok=True)
            (proj_dir / "video" / "final.mp4").write_bytes(b"compiled_video_output")

            compile_args = argparse.Namespace(
                project="p2_gate1_proj",
                chapters="1,2",
                output=None,
                approved=False,  # Not bypassing; relying on series-level approval
                extract_audio=False,
            )
            res = handle_compile(compile_args)
            assert res["chapters_compiled"] == ["1", "2"]

    def test_gate_2_skip_preview_in_render(self, tmp_path, monkeypatch):
        """Verify --skip-preview bypasses Gate 2 and renders directly to 1080p final."""
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))
        proj_dir = init_project_layout("p2_gate2_proj", "Gate 2 Project")
        chap_dir = init_chapter_layout("p2_gate2_proj", 1)

        # Approve Gate 1
        voice_sel = {
            "approved_strategy": "single_narrator",
            "primary_voice": "Trúc Ly",
            "reviewer": "editor",
            "project_slug": "p2_gate2_proj",
            "status": "approved",
        }
        atomic_write_json(proj_dir / "voice_selection.json", voice_sel)

        # Script exists
        script = RecapScript(
            project_slug="p2_gate2_proj",
            chapter_number="1",
            title="Ep 1",
            segments=[ScriptSegment(seq_id="001", panel_ids=["panel_001.jpg"], narrator_text="Narration")],
        )
        atomic_write_json(chap_dir / "script.json", script)

        # Gate 2 (preview.mp4 & preview_approval.json) are MISSING!
        assert not (chap_dir / "video" / "preview.mp4").exists()
        assert not (chap_dir / "preview_approval.json").exists()

        # 1. Without --skip-preview, final render MUST fail Gate 2
        with pytest.raises(CliValidationError) as exc_info:
            handle_render(argparse.Namespace(
                project="p2_gate2_proj",
                chapter=1,
                quality="final",
                skip_preview=False,
                approved=False,
            ))
        assert "Human Review Gate 2 not satisfied" in str(exc_info.value)

        # 2. With --skip-preview, final render succeeds directly
        def mock_render(self, *args, **kwargs):
            final_vid = chap_dir / "video" / "chapter_1_final.mp4"
            final_vid.parent.mkdir(parents=True, exist_ok=True)
            final_vid.write_bytes(b"final_1080p_mp4")
            return {
                "video_path": str(final_vid),
                "subtitle_path": None,
                "quality": "final",
            }

        monkeypatch.setattr(VideoEngine, "render_chapter", mock_render)

        res = handle_render(argparse.Namespace(
            project="p2_gate2_proj",
            chapter=1,
            quality="final",
            skip_preview=True,
            approved=False,
            allow_placeholders=True,
        ))
        assert res["quality"] == "final"
        assert Path(res["video_path"]).exists()


# ==============================================================================
# 3. Batch Mode Sequential Execution, Isolation & Retry Tests
# ==============================================================================

class TestP2BatchExecution:
    """Tests manga-recap batch command, failure isolation, and single-chapter retry."""

    def test_batch_parse_chapter_list(self):
        """Verify parse_chapter_list supports comma lists, ranges, and deduplication."""
        assert parse_chapter_list("1,2,3") == ["1", "2", "3"]
        assert parse_chapter_list("1-3") == ["1", "2", "3"]
        assert parse_chapter_list("1, 2-4, 5") == ["1", "2", "3", "4", "5"]
        assert parse_chapter_list("1, 2, 2, 3") == ["1", "2", "3"]
        assert parse_chapter_list("") == []
        assert parse_chapter_list(None) == []

    def _setup_batch_test_project(self, tmp_path, monkeypatch, slug: str, chapters: list[str]):
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))
        proj_dir = init_project_layout(slug, f"Batch Proj {slug}")
        # Approve Gate 1
        voice_sel = {
            "approved_strategy": "single_narrator",
            "primary_voice": "Trúc Ly",
            "reviewer": "batch_runner",
            "project_slug": slug,
            "status": "approved",
        }
        atomic_write_json(proj_dir / "voice_selection.json", voice_sel)

        for c in chapters:
            c_dir = init_chapter_layout(slug, c)
            script = RecapScript(
                project_slug=slug,
                chapter_number=str(c),
                title=f"Chap {c}",
                segments=[ScriptSegment(seq_id="001", panel_ids=["panel_001.jpg"], narrator_text=f"Recap text for chapter {c}")],
            )
            atomic_write_json(c_dir / "script.json", script)
        return proj_dir

    def test_batch_sequential_execution_and_caching(self, tmp_path, monkeypatch):
        """Verify batch executes chapters sequentially and caches completed chapters."""
        slug = "p2_batch_seq"
        proj_dir = self._setup_batch_test_project(tmp_path, monkeypatch, slug, ["1", "2"])

        # Mock TTS and Render
        from src.cli import render_cmd, tts_cmd

        monkeypatch.setattr(tts_cmd, "handle_tts", lambda args: {"synthesized": 1, "reused": 0})

        def mock_render(args):
            c_dir = get_chapter_dir(slug, args.chapter)
            vid = c_dir / "video" / f"chapter_{args.chapter}_final.mp4"
            vid.parent.mkdir(parents=True, exist_ok=True)
            vid.write_bytes(b"rendered_mp4")
            srt = c_dir / "subtitles" / f"chapter_{args.chapter}.srt"
            srt.parent.mkdir(parents=True, exist_ok=True)
            srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nSub\n", encoding="utf-8")
            return {"video_path": str(vid), "subtitle_path": str(srt), "quality": "final"}

        monkeypatch.setattr(render_cmd, "handle_render", mock_render)

        # 1. Run batch on chapters 1 and 2
        batch_args = argparse.Namespace(
            project=slug,
            chapters="1,2",
            skip_preview=True,
            approved=True,
            allow_placeholders=True,
            compile=False,
            force=False,
        )
        res1 = handle_batch(batch_args)
        assert res1["success"] is True
        assert res1["completed"] == ["1", "2"]
        assert res1["cached"] == []
        assert res1["failed"] == []

        # Verify staged exports in output/<slug>/
        out_dir = Path("output") / slug
        assert (out_dir / "chapter_001.mp4").exists()
        assert (out_dir / "chapter_002.mp4").exists()

        # 2. Re-run batch -> Both chapters must be recognized as CACHED
        res2 = handle_batch(batch_args)
        assert res2["success"] is True
        assert res2["completed"] == []
        assert res2["cached"] == ["1", "2"]
        assert res2["failed"] == []

    def test_chapter_failure_isolation(self, tmp_path, monkeypatch):
        """Verify that a failure on Chapter 2 does not crash the process; Chapter 3 continues."""
        slug = "p2_batch_iso"
        proj_dir = self._setup_batch_test_project(tmp_path, monkeypatch, slug, ["1", "2", "3"])

        from src.cli import render_cmd, tts_cmd

        monkeypatch.setattr(tts_cmd, "handle_tts", lambda args: {"synthesized": 1, "reused": 0})

        def mock_render(args):
            if str(args.chapter) == "2":
                raise RuntimeError("Simulated GPU out-of-memory on Chapter 2")
            c_dir = get_chapter_dir(slug, args.chapter)
            vid = c_dir / "video" / f"chapter_{args.chapter}_final.mp4"
            vid.parent.mkdir(parents=True, exist_ok=True)
            vid.write_bytes(b"rendered_mp4")
            return {"video_path": str(vid), "subtitle_path": None, "quality": "final"}

        monkeypatch.setattr(render_cmd, "handle_render", mock_render)

        # Run batch on chapters 1, 2, 3
        batch_args = argparse.Namespace(
            project=slug,
            chapters="1,2,3",
            skip_preview=True,
            approved=True,
            allow_placeholders=True,
            compile=False,
            force=False,
        )
        res = handle_batch(batch_args)

        # Process must not raise; failure is cleanly isolated
        assert res["success"] is False
        assert res["completed"] == ["1", "3"]
        assert len(res["failed"]) == 1
        assert res["failed"][0]["chapter"] == "2"
        assert "Simulated GPU out-of-memory" in res["failed"][0]["error"]

        # Checkpoint of Chapter 2 must be marked FAILED
        c2_meta = load_chapter_metadata(get_chapter_dir(slug, 2))
        assert c2_meta.overall_status == CheckpointStatus.FAILED

        # Chapter 1 and 3 checkpoints must be COMPLETED
        c1_meta = load_chapter_metadata(get_chapter_dir(slug, 1))
        c3_meta = load_chapter_metadata(get_chapter_dir(slug, 3))
        assert c1_meta.overall_status == CheckpointStatus.COMPLETED
        assert c3_meta.overall_status == CheckpointStatus.COMPLETED

    def test_single_chapter_retry(self, tmp_path, monkeypatch):
        """Verify --retry-chapter resumes only specified chapter without touching completed chapters."""
        slug = "p2_batch_retry"
        proj_dir = self._setup_batch_test_project(tmp_path, monkeypatch, slug, ["1", "2"])

        from src.cli import render_cmd, tts_cmd
        monkeypatch.setattr(tts_cmd, "handle_tts", lambda args: {"synthesized": 1, "reused": 0})

        call_count = {"1": 0, "2": 0}

        def mock_render(args):
            c_num = str(args.chapter)
            call_count[c_num] += 1
            if c_num == "2" and call_count["2"] == 1:
                raise RuntimeError("Temporary render glitch")
            c_dir = get_chapter_dir(slug, c_num)
            vid = c_dir / "video" / f"chapter_{c_num}_final.mp4"
            vid.parent.mkdir(parents=True, exist_ok=True)
            vid.write_bytes(b"mp4")
            return {"video_path": str(vid), "subtitle_path": None, "quality": "final"}

        monkeypatch.setattr(render_cmd, "handle_render", mock_render)

        # 1. Run batch: Chapter 1 succeeds, Chapter 2 fails
        res1 = handle_batch(argparse.Namespace(
            project=slug,
            chapters="1,2",
            skip_preview=True,
            approved=True,
            allow_placeholders=True,
            compile=False,
            force=False,
        ))
        assert res1["completed"] == ["1"]
        assert len(res1["failed"]) == 1
        assert call_count["1"] == 1
        assert call_count["2"] == 1

        # 2. Retry only Chapter 2
        res2 = handle_batch(argparse.Namespace(
            project=slug,
            retry_chapter="2",
            skip_preview=True,
            approved=True,
            allow_placeholders=True,
            compile=False,
            force=False,
        ))
        assert res2["success"] is True
        assert res2["completed"] == ["2"]
        assert res2["failed"] == []
        # Chapter 1 was NOT called again
        assert call_count["1"] == 1
        assert call_count["2"] == 2


# ==============================================================================
# 4. Clean Code Architecture & Absence of Cloud Relics Tests
# ==============================================================================

class TestP2CodeCleanupAndZeroCloud:
    """Tests the complete elimination of Supabase, YouTube, and cloud dependencies."""

    def test_zero_supabase_or_youtube_imports(self):
        """Verify zero 'import supabase' or 'googleapiclient' across all src/ files."""
        src_dir = Path("src")
        violations = []

        for py_file in src_dir.rglob("*.py"):
            code = py_file.read_text(encoding="utf-8")
            try:
                tree = ast.parse(code, filename=str(py_file))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for name in node.names:
                            if name.name in ("supabase", "googleapiclient", "google_auth_oauthlib", "google_auth_httplib2"):
                                violations.append((str(py_file), name.name))
                    elif isinstance(node, ast.ImportFrom):
                        if node.module and any(node.module.startswith(pkg) for pkg in ("supabase", "googleapiclient", "google_auth_oauthlib", "google_auth_httplib2")):
                            violations.append((str(py_file), node.module))
            except SyntaxError:
                pass

        assert len(violations) == 0, f"Found cloud dependencies in src/: {violations}"

    def test_requirements_and_env_cleanliness(self):
        """Verify requirements.txt and .env.example contain no YouTube / cloud packages."""
        req_txt = Path("requirements.txt").read_text(encoding="utf-8")
        assert "google-api-python-client" not in req_txt
        assert "google-auth-oauthlib" not in req_txt
        assert "google-auth-httplib2" not in req_txt
        assert "supabase" not in req_txt

        env_example = Path(".env.example").read_text(encoding="utf-8")
        assert "YOUTUBE_CLIENT_SECRETS_FILE" not in env_example
        assert "YOUTUBE_CREDENTIALS_FILE" not in env_example

    def test_obsolete_files_removed(self):
        """Verify obsolete files have been removed from the repository."""
        assert not Path("src/core/supabase_client.py").exists()
        assert not Path("src/cli/youtube_uploader.py").exists()
        assert not Path("src/cli/compile_chapters.py").exists()
        assert not Path("src/cli/crawler_cli.py").exists()
        assert not Path("src/cli/import_agent_script.py").exists()

    def test_tts_benchmark_lab_isolation(self):
        """Verify TTSBenchmarkRunner is isolated and not imported by production CLIs."""
        production_clis = [
            Path("src/cli/render_cmd.py"),
            Path("src/cli/batch_cmd.py"),
            Path("src/cli/tts_cmd.py"),
            Path("src/cli/crawl_cmd.py"),
            Path("src/cli/segment_cmd.py"),
            Path("src/render/video_engine.py"),
        ]
        for cli_file in production_clis:
            code = cli_file.read_text(encoding="utf-8")
            assert "TTSBenchmarkRunner" not in code
            assert "tts_benchmark" not in code or cli_file.name == "main.py"
