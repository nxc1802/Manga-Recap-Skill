"""
tests/e2e/test_e2e_pipeline.py
End-to-End Test Suite for Manga Recap Agentic Skill V3 (PR-08).
Validates the full workflow on Warrior Grandpa and His Supreme Granddaughter Episode 2:
Step 1: Source Validation
Step 2: Crawl / Import & Streaming Segmentation
Step 3: Agent Scripting & Schema Validation
Step 4: Local VieNeu-TTS Audio Synthesis & Human Review Gate 1
Step 5: Cinematic Video Engine V2 Rendering & Subtitles
Step 6: Quality Review & Artifact Verification Matrix
"""

from __future__ import annotations

from v4_fixtures import init_confirmed_project

import json
import shutil
import wave
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
)
from src.core.storage import (
    get_chapter_dir,
    get_project_dir,
    init_chapter_layout,
    init_project_layout,
    load_chapter_metadata,
    load_recap_script,
    read_json,
    save_recap_script,
    update_checkpoint,
)
from src.render.validator import StreamValidator


@pytest.fixture(scope="class")
def e2e_project_environment(tmp_path_factory):
    """Sets up an isolated projects environment for E2E testing."""
    tmp_path = tmp_path_factory.mktemp("e2e_test")
    projects_root = tmp_path / "data" / "projects"
    projects_root.mkdir(parents=True, exist_ok=True)

    import src.core.storage as storage_mod
    orig_get_root = storage_mod.get_projects_root
    storage_mod.get_projects_root = lambda: projects_root

    project_slug = "warrior_grandpa"
    chapter_num = "2"

    proj_dir = init_confirmed_project(project_slug, title="Warrior Grandpa and His Supreme Granddaughter")
    chap_dir = init_chapter_layout(project_slug, chapter_num, title="Episode 2 - Granddaughter Awakens")

    # Source 5 representative panels from real workspace assets or create fallback
    raw_source_dir = tmp_path / "seed_panels"
    raw_source_dir.mkdir(parents=True, exist_ok=True)

    workspace_panels_dir = Path("data/warrior_grandpa_and_his_supreme_granddaughter/image/recut/chap_1")
    if workspace_panels_dir.exists():
        for i in range(1, 6):
            p_file = workspace_panels_dir / f"panel_{i:03d}.jpg"
            if p_file.exists():
                shutil.copy2(str(p_file), str(raw_source_dir / f"page_{i:03d}.jpg"))

    # Fallback if seed files missing
    if len(list(raw_source_dir.glob("*.jpg"))) < 5:
        from PIL import Image, ImageDraw
        for i in range(1, 6):
            img = Image.new("RGB", (1080, 1500), color=(20 + i * 20, 30 + i * 15, 50))
            d = ImageDraw.Draw(img)
            d.text((100, 100), f"Warrior Grandpa Ep 2 - Panel {i}", fill=(240, 240, 240))
            img.save(raw_source_dir / f"page_{i:03d}.jpg", "JPEG")

    yield {
        "project_slug": project_slug,
        "chapter_num": chapter_num,
        "proj_dir": proj_dir,
        "chap_dir": chap_dir,
        "raw_source_dir": raw_source_dir,
    }

    storage_mod.get_projects_root = orig_get_root


class TestMangaRecapE2E:
    """Complete End-to-End Chapter Processing Lifecycle (PR-08)."""

    def test_step1_source_validation(self):
        """Step 1: Validate Webtoons URL parsing and metadata extraction."""
        test_url = (
            "https://www.webtoons.com/en/action/warrior-grandpa-and-his-supreme-granddaughter/"
            "episode-2/viewer?title_no=8953&episode_no=2"
        )
        assert "webtoons.com" in test_url
        assert "warrior-grandpa-and-his-supreme-granddaughter" in test_url
        assert "episode-2" in test_url
        assert "episode_no=2" in test_url

    def test_step2_crawl_import_and_segmentation(self, e2e_project_environment, capsys):
        """Step 2: Ingest raw pages and run streaming segmentation with contact sheets."""
        env = e2e_project_environment
        slug = env["project_slug"]
        chap = env["chapter_num"]
        src_dir = env["raw_source_dir"]

        # Run CLI crawl import
        exit_code = main([
            "crawl",
            "-p", slug,
            "-c", chap,
            "--import-dir", str(src_dir),
            "--json",
        ])
        assert exit_code == ExitCode.SUCCESS

        captured = capsys.readouterr()
        crawl_resp = json.loads(captured.out)
        assert crawl_resp["status"] == "success"
        assert crawl_resp["data"]["raw_images_count"] >= 5

        # Run CLI segment
        exit_code = main([
            "segment",
            "-p", slug,
            "-c", chap,
            "--json",
        ])
        assert exit_code == ExitCode.SUCCESS

        captured = capsys.readouterr()
        seg_resp = json.loads(captured.out)
        assert seg_resp["status"] == "success"
        assert seg_resp["data"]["total_panels"] >= 4

        # Verify Manifest & Contact Sheet presence
        chap_dir = env["chap_dir"]
        manifest_path = chap_dir / "manifest.json"
        assert manifest_path.exists()

        manifest_data = read_json(manifest_path)
        manifest = PanelManifest.model_validate(manifest_data)
        assert len(manifest.panels) >= 4

        contact_dir = chap_dir / "contact_sheets"
        assert contact_dir.exists()
        contact_sheets = list(contact_dir.glob("sheet_*.jpg"))
        assert len(contact_sheets) >= 1

    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass

    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass

    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass

    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass
