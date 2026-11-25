"""Tests for V6 canonical workflow orchestration and registry-driven CLI dispatch."""

import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PIL import Image

from src.cli.main import COMMAND_REGISTRY, build_parser, execute_subcommand, main
from src.core.errors import DomainValidationError
from src.core.human_review import review_fingerprint
from src.core.project import init_chapter_layout, init_project_layout
from src.core.schemas import PanelItem, PanelManifest, RecapScript, ScriptSegment
from src.core.series_setup import record_series_setup
from src.core.storage import atomic_write_json
from src.core.workflow import (
    get_project_status,
    prepare_chapter,
    preview_chapter,
    produce_chapters,
)
from tests.core.test_series_setup import _make_valid_answers_dict


def _create_test_project(tmp_path: Path, slug: str = "wf-series", compute: str = "local") -> Path:
    proj_dir = init_project_layout(slug, "Workflow Series")
    answers = _make_valid_answers_dict()
    answers["Q02"]["user_answer"] = compute
    record_series_setup(slug, answers, reviewer="tester", project_dir=proj_dir)
    atomic_write_json(
        proj_dir / "voice_selection.json",
        {
            "project_slug": slug,
            "primary_voice": "Ngoc Huyen",
            "reviewer": "tester",
            "status": "approved",
            "speed": 1.0,
        },
    )
    return proj_dir


def _create_raw_import_dir(tmp_path: Path, count: int = 2) -> Path:
    raw_dir = tmp_path / "raw_import"
    raw_dir.mkdir(parents=True, exist_ok=True)
    for idx in range(1, count + 1):
        img = Image.new("RGB", (800, 1200), color=(240, 240, 240))
        img.save(raw_dir / f"{idx:03d}.png")
    return raw_dir


def _write_minimal_script_and_manifest(chap_dir: Path, slug: str = "wf-series", chapter_num: str = "1") -> None:
    init_chapter_layout(slug, chapter_num)
    raw_abs = chap_dir / "raw" / "001.png"
    panel_rel = "panels/panel_001.jpg"
    panel_abs = chap_dir / panel_rel
    Image.new("RGB", (600, 800), color=(220, 220, 220)).save(raw_abs)
    Image.new("RGB", (600, 800), color=(200, 200, 200)).save(panel_abs)

    manifest = PanelManifest(
        project_slug=slug,
        chapter_number=chapter_num,
        total_raw_images=1,
        total_panels=1,
        panels=[
            PanelItem(
                panel_id="panel_001",
                sequence_number=1,
                filename="panel_001.jpg",
                relative_path=panel_rel,
                source_image="001.png",
                crop_box=(0, 0, 600, 800),
                width=600,
                height=800,
                aspect_ratio=0.75,
                sha256=hashlib.sha256(panel_abs.read_bytes()).hexdigest(),
            )
        ],
    )
    atomic_write_json(chap_dir / "manifest.json", manifest)

    script = RecapScript(
        project_slug=slug,
        chapter_number=chapter_num,
        segments=[
            ScriptSegment(
                seq_id="001",
                panel_ids=["panel_001"],
                evidence_panel_ids=["panel_001"],
                narrator_text="Mở đầu chương một với nhân vật chính quan sát toàn bộ khung cảnh.",
                voice_id="Ngoc Huyen",
                emotion="neutral",
            )
        ],
    )
    atomic_write_json(chap_dir / "script.json", script)
    atomic_write_json(
        chap_dir / "visual_plan.json",
        {"schema_version": "1.0.0", "project_slug": slug, "chapter_number": chapter_num, "shots": []},
    )


def test_command_registry_exposes_canonical_and_compat_commands():
    parser = build_parser()
    subparsers_action = next(
        a for a in parser._actions if getattr(a, "choices", None) is not None
    )
    registered_cmds = set(subparsers_action.choices.keys())
    for expected in (
        "prepare",
        "preview",
        "approve",
        "produce",
        "publish",
        "setup",
        "crawl",
        "segment",
        "repack",
        "decide",
        "inspect",
        "validate",
        "status",
        "tts",
        "render",
        "compile",
        "batch",
        "youtube",
        "clean",
        "cloud",
        "review-pack",
        "review-approve",
        "archive",
    ):
        assert expected in registered_cmds
        assert expected in COMMAND_REGISTRY


def test_prepare_creates_ingest_artifacts_never_script_and_reports_status(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("DATA_OUTPUT_DIR", str(tmp_path / "output"))
    proj_dir = _create_test_project(tmp_path)
    import_dir = _create_raw_import_dir(tmp_path, count=2)

    res = prepare_chapter(
        "wf-series",
        "1",
        import_dir=str(import_dir),
        skip_decide=True,
    )

    chap_dir = proj_dir / "chapters" / "1"
    assert res["next_action"] == "author_script"
    assert (chap_dir / "manifest.json").is_file()
    assert not (chap_dir / "script.json").exists()

    status = get_project_status("wf-series", chapter="1")
    assert status["next_action"] == "author_script"
    assert "script.json" in (status["blocker"] or "")


def test_preview_fails_without_script_and_stages_review_pack_with_mocked_media(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("DATA_OUTPUT_DIR", str(tmp_path / "output"))
    proj_dir = _create_test_project(tmp_path)
    chap_dir = init_chapter_layout("wf-series", "1")

    with pytest.raises(DomainValidationError, match="Missing script.json"):
        preview_chapter("wf-series", "1")

    with pytest.raises(DomainValidationError, match="dedicated to Chapter 1"):
        preview_chapter("wf-series", "2")

    _write_minimal_script_and_manifest(chap_dir)

    def _fake_synth(*args, **kwargs):
        audio_dir = chap_dir / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        wav_path = audio_dir / "seg_001.wav"
        wav_path.write_bytes(b"RIFF" + b"\x00" * 64)
        return {
            "project_slug": "wf-series",
            "chapter_number": "1",
            "total_duration": 3.0,
            "audio_files": [str(wav_path)],
        }

    def _fake_render(*args, **kwargs):
        vid_dir = chap_dir / "video"
        sub_dir = chap_dir / "subtitles"
        vid_dir.mkdir(parents=True, exist_ok=True)
        sub_dir.mkdir(parents=True, exist_ok=True)
        vid = vid_dir / "preview.mp4"
        srt = sub_dir / "chapter_1.srt"
        vid.write_bytes(b"FAKE_PREVIEW_MP4")
        srt.write_text("1\n00:00:00,000 --> 00:00:03,000\nTest\n", encoding="utf-8")
        return {"video_path": str(vid), "subtitle_path": str(srt)}

    monkeypatch.setattr("src.core.workflow.synthesize_chapter_tts", _fake_synth)
    monkeypatch.setattr("src.core.workflow.render_chapter", _fake_render)
    monkeypatch.setattr("src.core.workflow.validate_narration_evidence", lambda *a, **k: [])

    out = preview_chapter("wf-series", "1")
    assert out["next_action"] == "review_approve_chapter_1"
    assert Path(out["review_pack"]["generated_files"]["json"]).is_file()

    status = get_project_status("wf-series", chapter="1")
    assert status["next_action"] == "review_approve_chapter_1"


def test_approve_and_produce_gate_and_compute_routing(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("DATA_OUTPUT_DIR", str(tmp_path / "output"))
    proj_dir = _create_test_project(tmp_path, compute="local")
    ch1_dir = proj_dir / "chapters" / "1"
    _write_minimal_script_and_manifest(ch1_dir, chapter_num="1")
    preview_mp4 = ch1_dir / "video" / "preview.mp4"
    preview_srt = ch1_dir / "subtitles" / "preview.srt"
    preview_mp4.parent.mkdir(parents=True, exist_ok=True)
    preview_srt.parent.mkdir(parents=True, exist_ok=True)
    preview_mp4.write_bytes(b"APPROVED_PREVIEW_BYTES")
    preview_srt.write_text("1\n00:00:00,000 --> 00:00:03,000\nApproved\n", encoding="utf-8")

    # Blocked before human preview approval
    with pytest.raises(DomainValidationError):
        produce_chapters("wf-series", chapters="1")

    artifact_hash = hashlib.sha256(preview_mp4.read_bytes()).hexdigest()
    cfg_fp = review_fingerprint(proj_dir)
    rc = main(
        [
            "approve",
            "-p",
            "wf-series",
            "--reviewer",
            "qa-user",
            "--config-fingerprint",
            cfg_fp,
            "--artifact-hash",
            artifact_hash,
            "--json",
        ]
    )
    assert rc == 0

    # Local routing invokes batch_chapters with kaggle=False
    fake_batch = MagicMock(return_value={"project_slug": "wf-series", "completed_chapters": ["1"]})
    monkeypatch.setattr("src.core.workflow.batch_chapters", fake_batch)
    monkeypatch.setattr("src.core.workflow.validate_narration_evidence", lambda *a, **k: [])
    res_local = produce_chapters("wf-series", chapters="1")
    assert res_local["completed_chapters"] == ["1"]
    assert fake_batch.call_args.kwargs["kaggle"] is False

    # Missing script fails fast before starting compute
    ch2_dir = proj_dir / "chapters" / "2"
    ch2_dir.mkdir(parents=True, exist_ok=True)
    with pytest.raises(DomainValidationError, match="missing script.json"):
        produce_chapters("wf-series", chapters="2")

    # Switch Q02 to kaggle and re-approve updated setup fingerprint -> routes to KaggleCloudService
    answers = _make_valid_answers_dict()
    answers["Q02"]["user_answer"] = "kaggle"
    record_series_setup("wf-series", answers, reviewer="tester", project_dir=proj_dir)
    main(
        [
            "approve",
            "-p",
            "wf-series",
            "--reviewer",
            "qa-user",
            "--config-fingerprint",
            review_fingerprint(proj_dir),
            "--artifact-hash",
            artifact_hash,
            "--json",
        ]
    )
    fake_cloud_svc = MagicMock()
    fake_cloud_svc.is_authenticated = True
    fake_cloud_svc.dispatch_series_job_with_fallback.return_value = {"status": "completed"}
    monkeypatch.setattr("src.cloud.kaggle_service.KaggleCloudService", lambda: fake_cloud_svc)

    res_cloud = produce_chapters("wf-series", chapters="1")
    assert res_cloud["compute"] == "kaggle"
    fake_cloud_svc.dispatch_series_job_with_fallback.assert_called_once()


def test_publish_command_enforces_approval_and_delegates_to_youtube(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("DATA_OUTPUT_DIR", str(tmp_path / "output"))
    proj_dir = _create_test_project(tmp_path)

    # Upload blocked without Chapter 1 approval
    upload_args = SimpleNamespace(
        project="wf-series",
        chapter="1",
        publish_action="upload",
    )
    with pytest.raises(DomainValidationError):
        execute_subcommand("publish", upload_args)

    # Metadata action succeeds with mocked handler
    fake_yt = MagicMock(return_value={"status": "metadata_ok"})
    monkeypatch.setattr("src.cli.youtube_cmd.handle_youtube", fake_yt)
    meta_args = SimpleNamespace(
        project="wf-series",
        chapter="1",
        publish_action="metadata",
    )
    res = execute_subcommand("publish", meta_args)
    assert res == {"status": "metadata_ok"}
    assert meta_args.youtube_action == "metadata"
