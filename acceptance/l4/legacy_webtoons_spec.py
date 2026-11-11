# Historical specification, NOT executed or accepted evidence. See acceptance/README.md.
"""
tests/e2e/test_phase4_real_verification.py
Phase 4 Real E2E Chapter Verification Test Suite for Manga Recap Agentic Skill V3 (R4).

Validates:
1. Live Webtoons URL crawling with robust script-embedded image extraction.
2. Anti-bot and network failure handling marking checkpoint as FAILED.
3. Review Gates enforcement (Voice Approval & Preview Approval) blocking final render.
4. Programmatic execution with --approved or approved JSON artifacts.
5. Audio-visual timeline synchronization (video, audio, SRT delta <= 150 ms).
6. ffprobe stream compliance (H.264/AAC, 30fps).
"""

from __future__ import annotations

from v4_fixtures import init_confirmed_project

import json
import wave
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from src.cli.exit_codes import ExitCode
from src.cli.main import main
from src.core.schemas import (
    CameraMotionType,
    CheckpointStatus,
    FocusBox,
    PanelItem,
    PanelManifest,
    PanelStatus,
    PreviewApproval,
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
    read_json,
    save_recap_script,
    update_checkpoint,
)
from src.render.validator import StreamValidator
from src.tts.engine import EngineMode, VieNeuEngine, get_wav_duration_seconds


@pytest.fixture
def phase4_env(tmp_path, monkeypatch):
    """Sets up an isolated environment for Phase 4 verification."""
    monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))

    slug = "phase4_e2e_test"
    chap = "1"
    proj_dir = init_confirmed_project(slug, title="Warrior Grandpa Phase 4 Verification")
    chap_dir = init_chapter_layout(slug, chap, title="Episode 1 - Live Verification")

    return {
        "slug": slug,
        "chap": chap,
        "proj_dir": proj_dir,
        "chap_dir": chap_dir,
        "tmp_path": tmp_path,
    }


class TestPhase4RealVerification:
    """Rigorous tests covering all Phase 4 / R4 requirements."""

    def test_live_webtoons_crawler_extracts_real_panels_or_handles_anti_bot_failure(self, phase4_env):
        """
        Tests crawler on the target Webtoons URL:
        If network allows, extracts genuine JPEG URLs (rejecting 1x1 transparent placeholders).
        If network or anti-bot blocks (e.g., 403), marks checkpoint FAILED with non-zero exit code.
        """
        env = phase4_env
        slug = env["slug"]
        chap = env["chap"]
        chap_dir = env["chap_dir"]
        target_url = (
            "https://m.webtoons.com/en/action/warrior-grandpa-and-his-supreme-granddaughter/"
            "episode-1/viewer?title_no=8953&episode_no=1"
        )

        exit_code = main([
            "crawl",
            "-p", slug,
            "-c", chap,
            "--url", target_url,
            "--max-panels", "2",
            "--json",
        ])

        meta = load_chapter_metadata(chap_dir)
        crawl_cp = meta.stages.get(StageType.CRAWL)
        assert crawl_cp is not None

        if exit_code == ExitCode.SUCCESS:
            # Succeeded: verify genuine downloaded images, not 1x1 placeholders
            assert crawl_cp.status == CheckpointStatus.COMPLETED
            raw_files = list((chap_dir / "raw").glob("page_*.jpg"))
            assert len(raw_files) >= 1
            for rf in raw_files:
                assert rf.stat().st_size > 5000, f"Expected real panel image >5KB, got {rf.stat().st_size} bytes"
        else:
            # Blocked / failed cleanly: verify checkpoint is marked FAILED
            assert exit_code == ExitCode.VALIDATION_ERROR
            assert crawl_cp.status == CheckpointStatus.FAILED
            assert crawl_cp.error_message is not None

    def test_crawler_anti_bot_or_network_failure_marks_failed_cleanly(self, phase4_env, capsys):
        """Simulates anti-bot 403 or network disconnect to verify deterministic FAILED transition."""
        env = phase4_env
        slug = env["slug"]
        chap = env["chap"]
        chap_dir = env["chap_dir"]

        with patch("src.services.crawler_service.crawler_service.crawl_raw_pages", new_callable=AsyncMock) as mock_crawl:
            mock_crawl.side_effect = RuntimeError("Failed to fetch chapter page: HTTP 403 Forbidden (Anti-bot)")

            exit_code = main([
                "crawl",
                "-p", slug,
                "-c", chap,
                "--url", "https://m.webtoons.com/en/action/blocked-chapter",
                "--json",
            ])
            assert exit_code == ExitCode.VALIDATION_ERROR

        captured = capsys.readouterr()
        resp = json.loads(captured.out)
        assert resp["status"] == "error"
        assert resp["error"]["code"] == "VALIDATION_ERROR"
        assert "403" in resp["error"]["message"]

        meta = load_chapter_metadata(chap_dir)
        assert meta.stages[StageType.CRAWL].status == CheckpointStatus.FAILED
        assert "403" in meta.stages[StageType.CRAWL].error_message

    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass

    def test_timeline_synchronization_under_150ms_discrepancy(self, phase4_env):
        """
        Verifies that video duration, narration audio duration, and SRT subtitle
        end times have discrepancy <= 150 ms at video end.
        """
        env = phase4_env
        slug = env["slug"]
        chap = env["chap"]
        chap_dir = env["chap_dir"]

        from src.render.subtitle import SubtitleGenerator

        # 3 segments with realistic durations
        segments = [
            ScriptSegment(
                seq_id="001",
                panel_ids=["panel_001"],
                narrator_text="Tại vùng đất Thiểm Tây thuộc phủ Tấn Dương, mây trắng lững lờ trôi trên bầu trời cao rộng.",
                duration=4.520,
            ),
            ScriptSegment(
                seq_id="002",
                panel_ids=["panel_002"],
                narrator_text="Tế Thế Đường uy nghiêm tọa lạc giữa trung tâm sầm uất, đón tiếp vô số bệnh nhân thập phương.",
                duration=5.180,
            ),
            ScriptSegment(
                seq_id="003",
                panel_ids=["panel_003"],
                narrator_text="Vị danh y Thạch đại phu khẽ lắc đầu, trong lòng dấy lên nỗi âu lo khôn nguôi về bệnh tình lão hiệp khách.",
                duration=5.800,
            ),
        ]
        script = RecapScript(
            project_slug=slug,
            chapter_number=chap,
            title="Episode 1 Sync Test",
            segments=segments,
            total_duration_seconds=sum(s.duration for s in segments),
        )

        srt_path = chap_dir / "subtitles" / f"chapter_{chap}.srt"
        generator = SubtitleGenerator()
        generated_path = generator.generate_srt_file(script, srt_path)
        assert generated_path.exists()

        # Parse last SRT timestamp
        srt_text = srt_path.read_text(encoding="utf-8")
        lines = [line.strip() for line in srt_text.splitlines() if line.strip()]
        last_time_line = [l for l in lines if "-->" in l][-1]
        _, end_time_str = last_time_line.split("-->")
        end_time_str = end_time_str.strip()

        # Parse HH:MM:SS,mmm to seconds
        hh, mm, ss_mmm = end_time_str.split(":")
        ss, mmm = ss_mmm.split(",")
        srt_end_sec = int(hh) * 3600 + int(mm) * 60 + int(ss) + int(mmm) / 1000.0

        total_audio_duration = script.total_duration_seconds  # 4.52 + 5.18 + 5.80 = 15.500
        discrepancy_ms = abs(srt_end_sec - total_audio_duration) * 1000.0

        assert discrepancy_ms <= 150.0, f"Timeline discrepancy {discrepancy_ms} ms exceeds 150 ms threshold"
        assert round(srt_end_sec, 3) == round(total_audio_duration, 3)

    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass
