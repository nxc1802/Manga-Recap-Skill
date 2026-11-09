"""
tests/unit/test_review_pack.py
Unit test suite for Human Review Pack generator and CLI subcommand (Phase 3 P1 Human-Driven QA).
Validates:
- Generation of Markdown, HTML, and JSON review packs
- N:M panel mapping extraction (multi-panel segments, panel reuse, omitted panels)
- Faithful story review checklist and zero automated benchmark contract
- Uncertainty flag extraction ([CAN_XAC_MINH: ...])
- Error handling on missing project or chapter
- Review status decoupling (technical_status vs human_review_status)
"""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from src.cli.exit_codes import ExitCode
from src.cli.main import main
from src.cli.review_pack import build_review_pack_data, handle_review_pack
from src.core.schemas import HumanReviewStatus


@pytest.fixture
def synthetic_fixture_dir():
    return Path(__file__).resolve().parent.parent / "fixtures" / "synthetic"


class TestReviewPackGenerator:
    def test_build_review_pack_data_synthetic_fixture(self, synthetic_fixture_dir):
        chap_dir = synthetic_fixture_dir / "chapters" / "1"
        data = build_review_pack_data(
            project_dir=synthetic_fixture_dir,
            chap_dir=chap_dir,
            project_slug="synthetic_fixture",
            chapter_num="1",
        )

        assert data["project_slug"] == "synthetic_fixture"
        assert data["chapter_number"] == "1"
        assert data["human_review_status"] == HumanReviewStatus.UNREVIEWED.value
        assert data["technical_status"] == "pending"

        summary = data["summary"]
        assert summary["total_segments"] == 3
        assert summary["total_audio_duration_seconds"] == 12.7
        assert summary["total_manifest_panels"] == 5
        assert summary["referenced_panels_count"] == 4
        assert summary["omitted_panels_count"] == 1
        assert summary["reused_panels_count"] == 1

        # Check omitted panel is panel_005.jpg
        omitted_ids = [op["panel_id"] for op in data["omitted_panels"]]
        assert omitted_ids == ["panel_005.jpg"]

        # Check reused panel is panel_001.jpg
        reused_ids = [rp["panel_id"] for rp in data["reused_panels"]]
        assert reused_ids == ["panel_001.jpg"]
        assert data["reused_panels"][0]["use_count"] == 2
        assert "001" in data["reused_panels"][0]["segments"]
        assert "003" in data["reused_panels"][0]["segments"]

        # Check multi-panel segment 002_1
        seg_002_1 = next(s for s in data["segments"] if s["seq_id"] == "002_1")
        assert len(seg_002_1["panel_ids"]) == 3
        assert seg_002_1["panel_ids"] == ["panel_002.jpg", "panel_003.jpg", "panel_004.jpg"]

    def test_cli_review_pack_command_generation(self, synthetic_fixture_dir, tmp_path, capsys):
        out_dir = tmp_path / "review_out"
        code = main([
            "review-pack",
            "-p", "synthetic_fixture",
            "-c", "1",
            "--out-dir", str(out_dir),
            "--format", "all",
            "--json",
        ])
        assert code == ExitCode.SUCCESS.value

        captured = capsys.readouterr()
        envelope = json.loads(captured.out)
        assert envelope["status"] == "success"
        assert envelope["command"] == "review-pack"

        # Verify generated files exist on disk
        md_file = out_dir / "review_pack.md"
        html_file = out_dir / "review_pack.html"
        json_file = out_dir / "review_pack.json"

        assert md_file.exists()
        assert html_file.exists()
        assert json_file.exists()

        # Check markdown content
        md_text = md_file.read_text(encoding="utf-8")
        assert "# Human Review Pack — Chapter 1: Synthetic Fixture Chapter 1" in md_text
        assert "Faithful Storytelling" in md_text
        assert "Essential Panel Retention" in md_text
        assert "panel_005.jpg" in md_text
        assert "panel_002.jpg, panel_003.jpg, panel_004.jpg" in md_text
        assert "002_1" in md_text

        # Check html content
        html_text = html_file.read_text(encoding="utf-8")
        assert "<!DOCTYPE html>" in html_text
        assert "badge-unreviewed" in html_text
        assert "Human Review Inspection Checklist" in html_text
        assert "panel_005.jpg" in html_text

        # Check json content
        json_data = json.loads(json_file.read_text(encoding="utf-8"))
        assert json_data["summary"]["total_segments"] == 3
        assert json_data["summary"]["omitted_panels_count"] == 1

    def test_review_pack_extracts_can_xac_minh_flags(self, synthetic_fixture_dir, tmp_path):
        """Verifies that flagged segments with [CAN_XAC_MINH] are detected."""
        # Create a modified script with a [CAN_XAC_MINH] tag
        proj_dir = tmp_path / "flag_proj"
        proj_dir.mkdir(parents=True)
        (proj_dir / "project.json").write_text(
            json.dumps({"slug": "flag_proj", "title": "Flag Proj"}), encoding="utf-8"
        )
        chap_dir = proj_dir / "chapters" / "1"
        chap_dir.mkdir(parents=True)
        (chap_dir / "chapter.json").write_text(
            json.dumps({
                "chapter_number": "1",
                "title": "Ch 1",
                "technical_status": "completed",
                "human_review_status": "unreviewed",
            }),
            encoding="utf-8",
        )
        (chap_dir / "manifest.json").write_text(
            json.dumps({
                "total_panels": 2,
                "panels": [
                    {"filename": "panel_001.jpg", "status": "active"},
                    {"filename": "panel_002.jpg", "status": "active"},
                ],
            }),
            encoding="utf-8",
        )
        (chap_dir / "script.json").write_text(
            json.dumps({
                "segments": [
                    {
                        "seq_id": "001",
                        "panel_ids": ["panel_001.jpg"],
                        "narrator_text": "[CAN_XAC_MINH: Chữ trong bóng thoại bị mờ] Bóng người áo đen đứng im.",
                        "status": "needs_review",
                    },
                    {
                        "seq_id": "002",
                        "panel_ids": ["panel_002.jpg"],
                        "narrator_text": "Hắn vung kiếm chém tới!",
                        "status": "pending",
                    },
                ]
            }),
            encoding="utf-8",
        )

        data = build_review_pack_data(proj_dir, chap_dir, "flag_proj", "1")
        assert data["summary"]["flagged_needs_review_count"] == 1
        assert len(data["flagged_segments"]) == 1
        assert data["flagged_segments"][0]["seq_id"] == "001"
        assert "[CAN_XAC_MINH" in data["flagged_segments"][0]["text"]

    def test_cli_review_pack_fails_when_project_not_found(self, capsys):
        code = main(["review-pack", "-p", "non_existent_project_999", "-c", "1", "--json"])
        assert code == ExitCode.IO_ERROR.value

        captured = capsys.readouterr()
        envelope = json.loads(captured.out)
        assert envelope["status"] == "error"
        assert envelope["exit_code"] == ExitCode.IO_ERROR.value
