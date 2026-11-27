"""Tests for V7 CLI cloud commands, `--chunked`, and `status` hardware/telemetry reporting."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from src.cli.main import main
from src.core.series_setup import record_series_setup
from src.core.storage import atomic_write_json, read_json
from tests.workflow.test_canonical_workflow import unit_project  # noqa: F401


def test_cli_cloud_dispatch_reconcile_and_chunked_options(unit_project, monkeypatch, capsys):
    project, _ = unit_project
    calls = []

    fake_svc = MagicMock()
    fake_svc.is_authenticated = True
    fake_svc.check_chapter_1_preview_approval.return_value = (True, None)

    def _rec(kind, **kwargs):
        calls.append((kind, kwargs))
        return {"status": "success"}

    fake_svc.dispatch_chapter_job.side_effect = lambda **kw: _rec("run", **kw)
    fake_svc.dispatch_series_job.side_effect = lambda **kw: _rec("series", **kw)
    fake_svc.dispatch_series_chunks.side_effect = lambda **kw: _rec("chunks", **kw)
    fake_svc.reconcile_job.return_value = {"status": "RUNNING", "kernel_ref": "u1/job"}
    monkeypatch.setattr("src.cli.cloud_cmd.KaggleCloudService", lambda: fake_svc)

    assert (
        main([
            "cloud",
            "run",
            "--project",
            "unit",
            "--chapter",
            "1",
            "--json",
        ])
        == 0
    )
    capsys.readouterr()
    assert calls[-1][0] == "run"
    assert calls[-1][1]["gpu"] is True

    # Set Q02="kaggle" BEFORE approving Chapter 1 preview so Gate G6 fingerprint stays valid across cloud & produce
    saved = read_json(project / "series_setup_answers.json")["answers"]
    saved["Q02"]["user_answer"] = "kaggle"
    record_series_setup("unit", saved, project_dir=project)

    # Approve Chapter 1 preview cleanly before `cloud produce-series` / `produce` (Gate G6)
    from src.core.human_review import approve_preview, review_fingerprint
    from src.core.storage import calculate_sha256, load_manifest, load_recap_script
    from src.render.visual_policy import assemble_visual_plan, iter_segment_shots

    ch1 = project / "chapters" / "1"
    cfg = read_json(project / "project.json")["render_settings"]
    m1 = load_manifest(ch1)
    s1 = load_recap_script(ch1)
    shots = [sh for grp in iter_segment_shots(ch1, m1, s1, cfg["target_width"], cfg["target_height"], False) for sh in grp]
    assemble_visual_plan(
        m1, s1, shots,
        target_width=cfg["target_width"], target_height=cfg["target_height"],
        fps=cfg["fps"], motion_enabled=False,
    ).save(ch1 / "visual_plan.json")
    (ch1 / "video").mkdir(exist_ok=True)
    (ch1 / "subtitles").mkdir(exist_ok=True)
    (ch1 / "video" / "preview.mp4").write_bytes(b"preview mp4")
    (ch1 / "subtitles" / "preview.srt").write_text("1\n00:00:00,000 --> 00:00:00,500\nok\n", encoding="utf-8")
    approve_preview(project, "tester", review_fingerprint(project), calculate_sha256(ch1 / "video" / "preview.mp4"))

    assert (
        main([
            "cloud",
            "produce-series",
            "--project",
            "unit",
            "--chapters",
            "1",
            "--chunked",
            "--json",
        ])
        == 0
    )
    capsys.readouterr()
    assert calls[-1][0] == "chunks"
    assert calls[-1][1]["gpu"] is True

    monkeypatch.setattr("src.cloud.kaggle_service.KaggleCloudService", lambda: fake_svc)
    assert (
        main([
            "produce",
            "--project",
            "unit",
            "--chapters",
            "1",
            "--json",
        ])
        == 0
    )
    capsys.readouterr()
    assert calls[-1][0] == "chunks"

    assert main(["cloud", "reconcile", "--project", "unit", "--chapter", "1", "--json"]) == 0
    rec_payload = json.loads(capsys.readouterr().out)
    assert rec_payload["data"]["status"] == "RUNNING"
    fake_svc.reconcile_job.assert_called_once_with("unit", "1")


def test_status_reports_cloud_lifecycle_hardware_policy_and_chunk_progress(unit_project, capsys):
    project, ch1 = unit_project
    atomic_write_json(
        project / "cloud_job.json",
        {
            "project_slug": "unit",
            "chapter_number": "1",
            "job_type": "produce",
            "kernel_ref": "u1/mr-unit-c1",
            "account_name": "acct_1",
            "status": "RUNNING",
            "total_elapsed_sec": 124.5,
        },
    )
    atomic_write_json(
        project / "cloud_series_plan.json",
        {
            "status": "running",
            "skipped_verified": ["1"],
            "chunks": [
                {"chunk_index": 0, "chapters": ["2"], "status": "synced"},
                {"chunk_index": 1, "chapters": ["3"], "status": "running"},
            ],
            "updated_at": "2026-11-26T00:00:00Z",
        },
    )
    atomic_write_json(
        project / "cloud_telemetry.json",
        {
            "policy": "dual_tts_shared_encode",
            "hardware": {
                "devices": [{"index": 0, "name": "Tesla T4"}, {"index": 1, "name": "Tesla T4"}],
                "usable_nvenc_devices": [0, 1],
                "scheduler_policy": {"policy": "dual_tts_shared_encode", "tts_devices": [0, 1], "encode_devices": [0, 1]},
            },
            "timing": {"wall_makespan_sec": 45.2, "stages": {}},
        },
    )
    atomic_write_json(
        ch1 / "cloud_receipt.json",
        {"job_type": "produce", "quality": "final", "verified_at": "2026-11-26T00:00:00Z"},
    )

    assert main(["status", "--project", "unit", "--json"]) == 0
    proj_payload = json.loads(capsys.readouterr().out)["data"]
    assert proj_payload["cloud"]["job"]["status"] == "RUNNING"
    assert proj_payload["cloud"]["series_plan"]["completed_chunks"] == 1
    assert proj_payload["cloud"]["series_plan"]["total_chunks"] == 2
    assert proj_payload["cloud"]["telemetry"]["usable_nvenc_devices"] == [0, 1]

    assert main(["status", "--project", "unit", "--chapter", "1", "--json"]) == 0
    ch_payload = json.loads(capsys.readouterr().out)["data"]
    assert ch_payload["cloud"]["receipt"]["quality"] == "final"
    assert ch_payload["cloud"]["telemetry"]["policy"] == "dual_tts_shared_encode"
