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
    proj_dir = init_project_layout(slug, title="Warrior Grandpa Phase 4 Verification")
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

    def test_review_gates_enforcement_and_programmatic_bypass(self, phase4_env, capsys):
        """
        Verifies that final render requires Gate 1 (Voice) and Gate 2 (Preview),
        and verifies that explicit --approved satisfies gates programmatically.
        """
        env = phase4_env
        slug = env["slug"]
        chap = env["chap"]
        chap_dir = env["chap_dir"]

        # Seed valid script and dummy audio
        audio_dir = chap_dir / "audio" / "clips"
        audio_dir.mkdir(parents=True, exist_ok=True)
        seg_audio = audio_dir / f"audio_{chap}_001.wav"

        # Create valid 48kHz WAV
        with wave.open(str(seg_audio), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(48000)
            wf.writeframes(b"\x00\x00" * 48000 * 2)  # 2.0s

        script = RecapScript(
            project_slug=slug,
            chapter_number=chap,
            title="Episode 1 Gate Test",
            default_voice="Trúc Ly",
            segments=[
                ScriptSegment(
                    seq_id="001",
                    panel_ids=["panel_001"],
                    narrator_text="Tại vùng đất Thiểm Tây thuộc phủ Tấn Dương, bầu trời bao la mở ra câu chuyện võ hiệp kỳ bí.",
                    duration=2.0,
                    audio_path=f"audio/clips/{seg_audio.name}",
                    camera_motion=CameraMotionType.STATIC,
                    status="synthesized",
                )
            ],
            total_duration_seconds=2.0,
        )
        save_recap_script(chap_dir, script)
        update_checkpoint(chap_dir, StageType.SCRIPT, CheckpointStatus.COMPLETED)
        update_checkpoint(chap_dir, StageType.TTS, CheckpointStatus.COMPLETED)

        # 1. Attempt final render without Gate 1 or Gate 2
        exit_code = main([
            "render",
            "-p", slug,
            "-c", chap,
            "--quality", "final",
            "--json",
        ])
        assert exit_code == ExitCode.VALIDATION_ERROR
        captured = capsys.readouterr()
        assert "Gate 1 not satisfied" in captured.out

        # 2. Add Gate 1 voice_selection.json, but no Gate 2
        voice_sel = VoiceSelectionApproval(
            project_slug=slug,
            voice_strategy="single_narrator",
            primary_voice="Trúc Ly",
            speed=1.0,
            reviewer="lead_director",
            status="approved",
        )
        atomic_write_json(env["proj_dir"] / "voice_selection.json", voice_sel.model_dump())

        exit_code = main([
            "render",
            "-p", slug,
            "-c", chap,
            "--quality", "final",
            "--json",
        ])
        assert exit_code == ExitCode.VALIDATION_ERROR
        captured = capsys.readouterr()
        assert "Gate 2 not satisfied" in captured.out

        # 3. Pass --approved flag programmatically: bypasses gate blocking
        # Mock VideoEngine.render_chapter to verify --approved allows execution to proceed
        with patch("src.render.video_engine.VideoEngine.render_chapter") as mock_render:
            mock_render.return_value = {
                "valid": True,
                "video_path": str(chap_dir / "video" / "chapter_1_final.mp4"),
                "subtitle_path": str(chap_dir / "subtitles" / "chapter_1.srt"),
                "quality": "final",
                "suffix": "final",
                "resolution": "1920x1080",
                "fps": 30.0,
                "video_duration_seconds": 2.0,
                "audio_duration_seconds": 2.0,
                "duration_diff_seconds": 0.0,
                "validation": {"valid": True},
                "cleaned_files_count": 0,
            }
            exit_code = main([
                "render",
                "-p", slug,
                "-c", chap,
                "--quality", "final",
                "--approved",
                "--json",
            ])
            assert exit_code == ExitCode.SUCCESS

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

    def test_full_e2e_pipeline_real_chapter_verification(self, phase4_env, capsys):
        """
        Executes complete real chapter pipeline:
        Crawl/Import -> Segment -> Script -> Validate -> Gate 1 -> Neural TTS ->
        Preview Render -> Gate 2 -> Final Render -> Compile -> Status.
        Verifies real playable MP4 with neural audio, review gate enforcement,
        ffprobe stream attributes, and <= 150 ms timeline synchronization.
        """
        env = phase4_env
        slug = env["slug"]
        chap = env["chap"]
        chap_dir = env["chap_dir"]
        proj_dir = env["proj_dir"]

        # Step 1: Import 2 genuine raw panels from real warrior_grandpa_live assets or create high-res test panels
        import shutil
        from PIL import Image, ImageDraw

        seed_dir = env["tmp_path"] / "seed_panels"
        seed_dir.mkdir(parents=True, exist_ok=True)
        live_raw = Path("data/projects/warrior_grandpa_live/chapters/1/raw")
        if live_raw.exists() and len(list(live_raw.glob("page_*.jpg"))) >= 2:
            shutil.copy2(str(live_raw / "page_001.jpg"), str(seed_dir / "page_001.jpg"))
            shutil.copy2(str(live_raw / "page_002.jpg"), str(seed_dir / "page_002.jpg"))
        else:
            for idx in (1, 2):
                img = Image.new("RGB", (800, 1280), color=(20 + idx * 30, 40 + idx * 20, 60))
                d = ImageDraw.Draw(img)
                d.text((50, 100), f"Warrior Grandpa Page {idx}", fill=(240, 240, 240))
                img.save(seed_dir / f"page_{idx:03d}.jpg", "JPEG")

        # Run Crawl import
        exit_code = main(["crawl", "-p", slug, "-c", chap, "--import-dir", str(seed_dir), "--json"])
        assert exit_code == ExitCode.SUCCESS

        # Step 2: Segment
        exit_code = main(["segment", "-p", slug, "-c", chap, "--json"])
        assert exit_code == ExitCode.SUCCESS

        # Load manifest to get generated panel IDs
        manifest_data = read_json(chap_dir / "manifest.json")
        panel_ids = [p["panel_id"] for p in manifest_data["panels"]]
        assert len(panel_ids) >= 1

        # Step 3: Script & Validate
        segments = []
        sample_texts = [
            "Tại vùng đất Thiểm Tây thuộc phủ Tấn Dương, bầu trời bao la mở ra một câu chuyện võ lâm đầy bí ẩn.",
            "Khắp chốn thành trì sầm uất với những mái đình cổ kính, dòng người tấp nập qua lại dưới ánh nắng ban mai.",
            "Thế nhưng bên trong Tế Thế Đường yên ắng, không khí căng thẳng bao trùm khi vị y sư khẽ thốt lên lời ái ngại.",
        ]
        for idx, pid in enumerate(panel_ids):
            txt = sample_texts[idx % len(sample_texts)]
            segments.append(
                ScriptSegment(
                    seq_id=f"{idx + 1:03d}",
                    panel_ids=[pid],
                    narrator_text=txt,
                    camera_motion=CameraMotionType.STATIC if idx == 0 else CameraMotionType.PAN_LEFT,
                    emotion="neutral",
                )
            )

        script = RecapScript(
            project_slug=slug,
            chapter_number=chap,
            title="Episode 1 - Live Verification",
            default_voice="Trúc Ly",
            segments=segments,
        )
        save_recap_script(chap_dir, script)

        exit_code = main(["validate", "-p", slug, "-c", chap, "--strict", "--json"])
        assert exit_code == ExitCode.SUCCESS

        # Step 4: Human Review Gate 1 Approval Document
        voice_sel = VoiceSelectionApproval(
            project_slug=slug,
            voice_strategy="single_narrator",
            primary_voice="Trúc Ly",
            speed=1.0,
            reviewer="lead_director",
            status="approved",
            notes="Trúc Ly voice profile approved for real neural narration.",
        )
        atomic_write_json(proj_dir / "voice_selection.json", voice_sel.model_dump())

        # Step 5: Synthesize with real neural VieNeu-TTS engine
        exit_code = main(["tts", "-p", slug, "-c", chap, "--engine", "neural", "--force", "--json"])
        assert exit_code == ExitCode.SUCCESS

        # Confirm WAV audio files exist, have 48 kHz headers, and real audio frames
        clips_dir = chap_dir / "audio" / "clips"
        wav_files = list(clips_dir.glob("*.wav"))
        assert len(wav_files) == len(segments)
        for wf_path in wav_files:
            with wave.open(str(wf_path), "rb") as wf:
                assert wf.getframerate() == 48000
                assert wf.getnframes() > 0

        # Step 6: Render preview video
        exit_code = main(["render", "-p", slug, "-c", chap, "--quality", "preview", "--burn-subtitles", "--json"])
        assert exit_code == ExitCode.SUCCESS

        # Step 7: Human Review Gate 2 Preview Approval Document
        prev_approval = PreviewApproval(
            project_slug=slug,
            chapter_number=chap,
            preview_video_path="video/chapter_1_preview_v1.mp4",
            reviewer="lead_qa_reviewer",
            status="approved",
            notes="Preview video verified: 720p 30fps H.264 video, crisp Vietnamese audio.",
        )
        atomic_write_json(chap_dir / "preview_approval.json", prev_approval.model_dump())

        # Step 8: Render final 1080p video
        exit_code = main(["render", "-p", slug, "-c", chap, "--quality", "final", "--burn-subtitles", "--json"])
        assert exit_code == ExitCode.SUCCESS

        captured = capsys.readouterr()
        # Find json envelope in captured stdout
        render_json = None
        for line in reversed(captured.out.splitlines()):
            if line.strip().startswith("{") and line.strip().endswith("}"):
                try:
                    render_json = json.loads(line)
                    break
                except Exception:
                    continue
        if render_json is None:
            # Parse entire output block
            start_idx = captured.out.rfind("{\n  \"status\"")
            if start_idx != -1:
                render_json = json.loads(captured.out[start_idx:])

        assert render_json is not None
        assert render_json["status"] == "success"
        r_data = render_json["data"]
        final_video_path = Path(r_data["video_path"])
        assert final_video_path.exists()
        assert final_video_path.stat().st_size > 10000

        # Validate with StreamValidator
        validator = StreamValidator()
        probe_res = validator.validate_video(
            video_path=final_video_path,
            expected_width=1920,
            expected_height=1080,
            expected_fps=30.0,
            tolerance_seconds=0.150,
            raise_on_error=True,
        )
        assert probe_res["valid"] is True
        assert probe_res["video"]["codec"] in ("h264", "avc1")
        assert probe_res["audio"]["codec"] == "aac"
        assert probe_res["duration_diff_seconds"] <= 0.150

        # Step 9: Multi-Chapter Compilation
        compiled_output = proj_dir / "video" / "full_recap.mp4"
        exit_code = main(["compile", "-p", slug, "--chapters", chap, "-o", str(compiled_output), "--json"])
        assert exit_code == ExitCode.SUCCESS
        assert compiled_output.exists()

        # Step 10: Status check
        exit_code = main(["status", "-p", slug, "-c", chap, "--json"])
        assert exit_code == ExitCode.SUCCESS

