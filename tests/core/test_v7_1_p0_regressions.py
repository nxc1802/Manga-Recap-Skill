"""Focused V7.1 P0 regression tests for BUG-01 through BUG-08."""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PIL import Image

from src.cli.main import build_parser, main
from src.cloud.jobs import write_cloud_job_state
from src.cloud.lifecycle import (
    LifecycleState,
    check_no_duplicate_active_job,
    reconcile_submit_unknown,
    reserve_cloud_job_slot,
)
from src.core.panel_repack import (
    PanelPlan,
    PanelPlanItem,
    PanelPlanOp,
    apply_panel_plan,
)
from src.core.project import init_chapter_layout, init_project_layout
from src.core.human_review import review_fingerprint
from src.core.schemas import (
    PanelItem,
    PanelManifest,
    PanelStatus,
    RecapScript,
    ScriptSegment,
    VoiceSelectionApproval,
)
from src.core.series_setup import record_series_setup
from src.core.storage import atomic_write_json, calculate_sha256, load_manifest
from src.core.version import APP_VERSION
from src.core.workflow import approve_chapter_preview, preview_chapter, produce_chapters
from src.ingest.decision import run_decision_pipeline
from src.providers.kaggle import KaggleExecutionError, KaggleSubmitAmbiguousError


def _valid_answers(compute: str = "local") -> dict:
    return {
        "Q01": {"user_answer": {"chapters": "1-2", "source": "https://nettruyen.example/test"}, "answer_source": "user"},
        "Q02": {"user_answer": compute, "answer_source": "user"},
        "Q03": {"user_answer": "Ngọc Huyền", "answer_source": "user"},
        "Q04": {"user_answer": 1.0, "answer_source": "user"},
        "Q05": {"user_answer": True, "answer_source": "user"},
        "Q08": {"user_answer": {"no_upload": True}, "answer_source": "user"},
        "Q09": {"user_answer": "preview", "answer_source": "user"},
        "Q10": {"user_answer": {"none": True}, "answer_source": "user"},
    }


def _make_project(tmp_path: Path, slug: str = "p0-proj", compute: str = "local") -> Path:
    proj_dir = init_project_layout(slug, "P0 Test")
    record_series_setup(slug, _valid_answers(compute=compute), reviewer="qa", project_dir=proj_dir)
    atomic_write_json(
        proj_dir / "voice_selection.json",
        VoiceSelectionApproval(
            project_slug=slug,
            approved_strategy="single_narrator",
            primary_voice="Ngọc Huyền",
            reviewer="qa",
            status="approved",
        ),
    )
    return proj_dir


def _write_chapter_evidence(chap_dir: Path, slug: str = "p0-proj", ch: str = "1") -> None:
    panels_dir = chap_dir / "panels"
    panels_dir.mkdir(parents=True, exist_ok=True)
    img_path = panels_dir / "panel_001.jpg"
    Image.new("RGB", (600, 800), (120, 140, 160)).save(img_path, "JPEG")
    sha = calculate_sha256(img_path)
    manifest = PanelManifest(
        project_slug=slug,
        chapter_number=ch,
        panels=[
            PanelItem(
                panel_id="panel_001",
                sequence_number=1,
                filename="panel_001.jpg",
                relative_path="panels/panel_001.jpg",
                source_image="page_001.jpg",
                crop_box=[0, 0, 600, 800],
                width=600,
                height=800,
                aspect_ratio=0.75,
                sha256=sha,
                vcs_start=0,
                vcs_end=800,
                status=PanelStatus.ACTIVE,
            )
        ],
    )
    atomic_write_json(chap_dir / "manifest.json", manifest)
    script = RecapScript(
        project_slug=slug,
        chapter_number=ch,
        voice_strategy="single_narrator",
        segments=[
            ScriptSegment(
                seq_id="001",
                panel_ids=["panel_001"],
                narrator_text="Nhân vật chính bước vào đại điện và nhìn thấy bí mật ẩn giấu.",
            )
        ],
    )
    atomic_write_json(chap_dir / "script.json", script)


# ---------------------------------------------------------------------------
# BUG-01: write_cloud_job_state must propagate persistence failures
# ---------------------------------------------------------------------------


def test_bug01_write_cloud_job_state_propagates_write_and_missing_dir_errors(tmp_path, monkeypatch):
    missing_path = tmp_path / "does_not_exist" / "cloud_job.json"
    with pytest.raises(FileNotFoundError):
        write_cloud_job_state(missing_path, {"project_slug": "p0", "status": "SUBMITTING"})

    state_path = tmp_path / "cloud_job.json"
    monkeypatch.setattr(
        "src.cloud.jobs.atomic_write_json",
        MagicMock(side_effect=PermissionError("disk read-only")),
    )
    with pytest.raises(PermissionError, match="disk read-only"):
        write_cloud_job_state(state_path, {"project_slug": "p0", "status": "SUBMITTING"})

    with pytest.raises(PermissionError, match="disk read-only"):
        reserve_cloud_job_slot(
            tmp_path,
            state_path,
            ["1"],
            {"project_slug": "p0", "chapter_number": "1", "chapters": ["1"]},
        )


# ---------------------------------------------------------------------------
# BUG-02: unrecognized remote status must remain active/unresolved (fail-closed)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw_status",
    ["UNKNOWN", "INITIALIZING", "PENDING_RETRY", "QUEUE_TIMEOUT", "CLIENT_TIMEOUT", "REMOTE_COMPLETE"],
)
def test_bug02_unrecognized_or_unresolved_status_blocks_duplicate_submit(tmp_path, raw_status):
    state_path = tmp_path / "cloud_job.json"
    write_cloud_job_state(
        state_path,
        {
            "project_slug": "p0",
            "chapter_number": "1",
            "chapters": ["1"],
            "kernel_ref": "user/kern-1",
            "account_name": "acct1",
            "username": "user",
            "status": raw_status,
        },
    )
    with pytest.raises(KaggleExecutionError):
        check_no_duplicate_active_job(state_path)


def test_bug02_reconcile_unfamiliar_remote_enum_stays_submit_unknown(tmp_path):
    state_path = tmp_path / "cloud_job.json"
    fake_client = MagicMock()
    fake_client.get_kernel_status.return_value = ("KernelWorkerStatus.INITIALIZING", "INITIALIZING", None)
    updated = reconcile_submit_unknown(
        fake_client,
        state_path,
        {
            "project_slug": "p0",
            "chapter_number": "1",
            "chapters": ["1"],
            "kernel_ref": "user/kern-1",
            "account_name": "acct1",
            "username": "user",
            "status": LifecycleState.SUBMIT_UNKNOWN,
        },
    )
    assert updated["status"] == LifecycleState.SUBMIT_UNKNOWN
    assert updated["last_remote_status"] == "INITIALIZING"
    with pytest.raises(KaggleSubmitAmbiguousError):
        check_no_duplicate_active_job(state_path)


def test_bug02_confirmed_terminal_failure_allows_retry(tmp_path):
    state_path = tmp_path / "cloud_job.json"
    write_cloud_job_state(
        state_path,
        {
            "project_slug": "p0",
            "chapter_number": "1",
            "chapters": ["1"],
            "kernel_ref": "user/kern-1",
            "status": LifecycleState.FAILED,
        },
    )
    res = check_no_duplicate_active_job(state_path)
    assert res is not None and res.status == LifecycleState.FAILED


# ---------------------------------------------------------------------------
# BUG-03 & BUG-04: preview/produce follow Q02 & canonical produce uses chunk planner
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("compute", ["local", "kaggle"])
def test_bug04_preview_uses_confirmed_q02_backend(tmp_path, monkeypatch, compute):
    monkeypatch.setenv("DATA_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("DATA_OUTPUT_DIR", str(tmp_path / "output"))
    proj_dir = _make_project(tmp_path, slug=f"prv-{compute}", compute=compute)
    chap_dir = init_chapter_layout(f"prv-{compute}", "1")
    _write_chapter_evidence(chap_dir, slug=f"prv-{compute}", ch="1")

    seen = {}

    def _fake_tts(slug, ch, **kwargs):
        seen["tts_kaggle"] = kwargs.get("kaggle")
        return {"project_slug": slug, "chapter_number": ch}

    def _fake_render(slug, ch, **kwargs):
        seen["render_kaggle"] = kwargs.get("kaggle")
        return {"video_path": "v.mp4", "subtitle_path": "s.srt", "review_pack": {"ok": True}}

    monkeypatch.setattr("src.core.workflow.synthesize_chapter_tts", _fake_tts)
    monkeypatch.setattr("src.core.workflow.render_chapter_workflow", _fake_render)
    monkeypatch.setattr("src.core.workflow.validate_narration_evidence", lambda *a, **k: [])

    res = preview_chapter(f"prv-{compute}", "1")
    expected = compute == "kaggle"
    assert res["compute"] == compute
    assert seen["tts_kaggle"] is expected
    assert seen["render_kaggle"] is expected


def test_bug03_and_bug04_cli_rejects_kaggle_override_on_preview_and_produce(tmp_path, monkeypatch):
    # preview/produce no longer accept --kaggle override (Q02 governs backend)
    assert main(["preview", "-p", "demo", "-c", "1", "--kaggle", "--json"]) == 2
    assert main(["produce", "-p", "demo", "-c", "1", "--kaggle", "--json"]) == 2

    # Granular repair commands retain explicit --kaggle support
    parser = build_parser()
    ns_tts = parser.parse_args(["tts", "-p", "demo", "-c", "1", "--kaggle"])
    ns_render = parser.parse_args(["render", "-p", "demo", "-c", "1", "--kaggle"])
    assert ns_tts.kaggle is True
    assert ns_render.kaggle is True


def test_bug03_canonical_produce_routes_kaggle_via_chunk_planner(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("DATA_OUTPUT_DIR", str(tmp_path / "output"))
    proj_dir = _make_project(tmp_path, slug="prod-kag", compute="kaggle")
    ch1 = init_chapter_layout("prod-kag", "1")
    _write_chapter_evidence(ch1, slug="prod-kag", ch="1")
    atomic_write_json(ch1 / "visual_plan.json", {"schema_version": "4.1.0", "shots": []})
    prev_mp4 = ch1 / "video" / "preview.mp4"
    prev_srt = ch1 / "subtitles" / "preview.srt"
    prev_mp4.parent.mkdir(parents=True, exist_ok=True)
    prev_srt.parent.mkdir(parents=True, exist_ok=True)
    prev_mp4.write_bytes(b"PREVIEW_BYTES")
    prev_srt.write_text("1\n00:00:00,000 --> 00:00:03,000\nApproved\n", encoding="utf-8")
    approve_chapter_preview(
        "prod-kag",
        reviewer="qa",
        config_fingerprint=review_fingerprint(proj_dir),
        artifact_hash=hashlib.sha256(prev_mp4.read_bytes()).hexdigest(),
    )

    fake_svc = MagicMock()
    fake_svc.is_authenticated = True
    fake_svc.dispatch_series_chunks.return_value = {
        "status": "success",
        "dispatched_chunks": [["1"]],
        "skipped_verified": [],
    }
    monkeypatch.setattr("src.cloud.kaggle_service.KaggleCloudService", lambda: fake_svc)
    monkeypatch.setattr("src.core.workflow.validate_narration_evidence", lambda *a, **k: [])

    out = produce_chapters("prod-kag", chapters="1", compile_after=True)
    assert out["compute"] == "kaggle"
    fake_svc.dispatch_series_chunks.assert_called_once()
    fake_svc.dispatch_series_job.assert_not_called()


# ---------------------------------------------------------------------------
# BUG-05: lazy API key resolution when Pass 1 is cached but Pass 2/split misses
# ---------------------------------------------------------------------------


def test_bug05_lazy_api_key_resolves_on_later_phase_miss_and_skips_on_warm_cache(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    chap_dir = tmp_path / "ch1"
    panels_dir = chap_dir / "panels"
    raw_dir = chap_dir / "raw"
    panels_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)

    # Create a tall panel + matching raw image so split_at can extract child panels
    img_path = panels_dir / "panel_001.jpg"
    raw_path = raw_dir / "page_001.jpg"
    img = Image.new("RGB", (600, 1800), (200, 200, 200))
    img.save(img_path, "JPEG")
    img.save(raw_path, "JPEG")
    sha = calculate_sha256(img_path)

    manifest = PanelManifest(
        project_slug="bug05",
        chapter_number="1",
        panels=[
            PanelItem(
                panel_id="panel_001",
                sequence_number=1,
                filename="panel_001.jpg",
                relative_path="panels/panel_001.jpg",
                source_image="page_001.jpg",
                crop_box=[0, 0, 600, 1800],
                width=600,
                height=1800,
                aspect_ratio=600 / 1800,
                sha256=sha,
                vcs_start=0,
                vcs_end=1800,
                status=PanelStatus.ACTIVE,
            )
        ],
    )
    atomic_write_json(chap_dir / "manifest.json", manifest)

    # First run with explicit api_key populates Pass 1, split, and Pass 2 cache
    seen_keys = []

    def _fake_eval(path, w, h, key, client=None, config=None):
        seen_keys.append(key)
        is_tall = h > 1200
        return {
            "answers": {
                "panel_group": {"choice": "group_1_story_art", "confidence": 0.95},
                "structure_action": {"choice": "needs_split" if is_tall else "ok_single", "confidence": 0.95},
                "crop_top": {"choice": "0", "confidence": 0.95},
                "crop_bottom": {"choice": "0", "confidence": 0.95},
                "crop_left": {"choice": "0", "confidence": 0.95},
                "crop_right": {"choice": "0", "confidence": 0.95},
            },
            "blocked": False,
        }

    def _fake_cuts(ch_d, pid, ratio, key, client=None, config=None):
        seen_keys.append(key)
        return {"cuts": [0.5], "reason": "mid"}

    monkeypatch.setattr("src.ingest.decision._evaluate_panel_raw", _fake_eval)
    monkeypatch.setattr("src.ingest.decision._choose_cut_ratios_detailed", _fake_cuts)

    run_decision_pipeline(chap_dir, api_key="test-key-1")
    assert all(k == "test-key-1" for k in seen_keys)

    # Second run: complete warm cache -> zero requests, works even with NO GEMINI_API_KEY
    seen_keys.clear()
    run_decision_pipeline(chap_dir, api_key=None)
    assert seen_keys == []

    # Now invalidate only one Pass 2 child entry while Pass 1 stays cached
    cache_path = chap_dir / "decision_cache.json"
    import json
    cache_data = json.loads(cache_path.read_text(encoding="utf-8"))
    for k in list(cache_data.keys()):
        if k.startswith("__pass2_") or k == "panel_001":
            if k.startswith("__pass2_"):
                del cache_data[k]
    # Restore raw manifest to trigger Pass 1 warm hit + split warm hit + Pass 2 miss
    atomic_write_json(chap_dir / "manifest.json", manifest)
    img.save(img_path, "JPEG")
    atomic_write_json(cache_path, cache_data)

    monkeypatch.setenv("GEMINI_API_KEY", "resolved-late-key")
    seen_keys.clear()
    run_decision_pipeline(chap_dir, api_key=None)
    assert len(seen_keys) >= 1
    assert all(k == "resolved-late-key" for k in seen_keys)


# ---------------------------------------------------------------------------
# BUG-06: metadata-only repack & shifted KEEP must preserve child lineage
# ---------------------------------------------------------------------------


def test_bug06_metadata_only_repack_preserves_child_lineage(tmp_path):
    chap_dir = tmp_path / "ch1"
    panels_dir = chap_dir / "panels"
    panels_dir.mkdir(parents=True, exist_ok=True)
    p1_path = panels_dir / "panel_001.jpg"
    Image.new("RGB", (600, 600), (10, 20, 30)).save(p1_path, "JPEG")
    child_sha = calculate_sha256(p1_path)

    manifest = PanelManifest(
        project_slug="bug06",
        chapter_number="1",
        panels=[
            PanelItem(
                panel_id="panel_001",
                sequence_number=1,
                filename="panel_001.jpg",
                relative_path="panels/panel_001.jpg",
                source_image="page_001.jpg",
                crop_box=[0, 0, 600, 600],
                width=600,
                height=600,
                aspect_ratio=1.0,
                sha256=child_sha,
                vcs_start=0,
                vcs_end=600,
                status=PanelStatus.ACTIVE,
                evidence_metadata={
                    "repack_op": "split_at",
                    "source_raw_panels": ["panel_raw_001"],
                    "source_raw_sha256s": ["deadbeef" * 8],
                    "source_vcs_range": [0, 1200],
                },
            )
        ],
    )
    atomic_write_json(chap_dir / "manifest.json", manifest)

    plan = PanelPlan(
        project_slug="bug06",
        chapter_number="1",
        items=[
            PanelPlanItem(
                op=PanelPlanOp.KEEP,
                source_panel_ids=["panel_001"],
                group=PanelStatus.ACTIVE,
                reason="recheck",
            )
        ],
    )
    res = apply_panel_plan(chap_dir, plan, regenerate_sheets=False)
    assert res["fast_path"] == "metadata_only"

    reloaded = load_manifest(chap_dir)
    assert reloaded is not None
    meta = reloaded.panels[0].evidence_metadata
    assert meta["repack_op"] == "split_at"
    assert meta["source_raw_panels"] == ["panel_raw_001"]
    assert meta["source_raw_sha256s"] == ["deadbeef" * 8]
    assert meta["source_vcs_range"] == [0, 1200]


# ---------------------------------------------------------------------------
# BUG-08: CLI version aligns with release candidate 7.1.0
# ---------------------------------------------------------------------------


def test_bug08_cli_version_matches_skill_and_pyproject():
    repo = Path(__file__).resolve().parents[2]
    assert APP_VERSION == "7.1.0"
    assert 'version = "7.1.0"' in (repo / "pyproject.toml").read_text(encoding="utf-8")
    assert 'version: "7.1.0"' in (repo / ".agents/skills/manga-recap/SKILL.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# BUG-09 (found live during V7.1 P4 Kaggle preview):
# sync_outputs marks StageType.TTS COMPLETED; synthesize_chapter_tts must not
# fail with Illegal transition 'completed' -> 'completed'.
# ---------------------------------------------------------------------------


def test_bug09_kaggle_tts_does_not_fail_when_sync_already_completed_checkpoint(tmp_path, monkeypatch):
    from src.core.storage import load_chapter_metadata, update_checkpoint
    from src.core.schemas import CheckpointStatus, StageType
    from src.core.workflow import synthesize_chapter_tts

    monkeypatch.setenv("DATA_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("DATA_OUTPUT_DIR", str(tmp_path / "output"))
    _make_project(tmp_path, slug="bug09-proj", compute="kaggle")
    chap_dir = init_chapter_layout("bug09-proj", "1")
    _write_chapter_evidence(chap_dir, slug="bug09-proj", ch="1")

    def _fake_dispatch(**kwargs):
        # Emulate sync_outputs marking TTS COMPLETED before returning
        update_checkpoint(chap_dir, StageType.TTS, CheckpointStatus.COMPLETED)
        return {"status": "success", "telemetry": {"worker_sec": 12.3}}

    fake_svc = MagicMock()
    fake_svc.is_authenticated = True
    fake_svc.dispatch_chapter_job.side_effect = _fake_dispatch
    monkeypatch.setattr("src.cloud.kaggle_service.KaggleCloudService", lambda: fake_svc)

    res = synthesize_chapter_tts("bug09-proj", "1", kaggle=True)
    assert res["status"] == "success"
    assert res["provider"] == "kaggle"
    assert load_chapter_metadata(chap_dir).stages[StageType.TTS].status == CheckpointStatus.COMPLETED
