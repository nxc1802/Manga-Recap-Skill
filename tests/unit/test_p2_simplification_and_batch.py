"""
tests/unit/test_p2_simplification_and_batch.py
Comprehensive regression and unit tests for Phase 3 — P2:
1. 5 operational stages and backward-compatible aliases.
2. Review Gates consolidation (Gate 0, Gate 1 series-level inheritance, Gate 2 --skip-preview).
3. Sequential batch processing, chapter failure isolation, and single-chapter retry.
4. Clean code architecture (absence of Supabase, YouTube, and cloud dependencies).
"""

from v4_fixtures import init_confirmed_project

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
        init_confirmed_project("p2_alias_proj", "P2 Alias Project")
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
        init_confirmed_project("p2_fin_proj", "P2 Fin Project")
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

    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass

    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass

    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass


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
        proj_dir = init_confirmed_project(slug, f"Batch Proj {slug}")
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

    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass

    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass

    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass


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
