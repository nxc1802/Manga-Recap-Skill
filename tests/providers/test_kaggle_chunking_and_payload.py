"""Tests for K4 minimal payload pruning, worker import preflight, session chunk planning, and incremental resume."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import wave
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.cloud.chunking import (
    filter_pending_chapters,
    is_chapter_verified_complete,
    plan_series_chunks,
)
from src.cloud.kaggle_service import KaggleCloudService
from src.cloud.payload import export_results, package_payload, sync_results
from src.core.errors import ProviderIntegrityError
from src.core.human_review import approve_preview, review_fingerprint
from src.core.storage import atomic_write_json, calculate_sha256, load_manifest, load_recap_script, read_json
from src.core.visual_contract import VisualPlan
from src.render.visual_policy import assemble_visual_plan, iter_segment_shots
from tests.workflow.test_canonical_workflow import unit_project  # noqa: F401


def _build_series_project(unit_project, num_chapters: int = 3):
    project, ch1 = unit_project
    cfg = read_json(project / "project.json")
    rs = cfg["render_settings"]
    m1 = load_manifest(ch1)
    s1 = load_recap_script(ch1)
    if not (ch1 / "visual_plan.json").exists():
        shots1 = [
            sh
            for grp in iter_segment_shots(ch1, m1, s1, rs["target_width"], rs["target_height"], False)
            for sh in grp
        ]
        assemble_visual_plan(
            m1, s1, shots1,
            target_width=rs["target_width"], target_height=rs["target_height"],
            fps=rs["fps"], motion_enabled=False,
        ).save(ch1 / "visual_plan.json")

    (ch1 / "video").mkdir(parents=True, exist_ok=True)
    (ch1 / "subtitles").mkdir(parents=True, exist_ok=True)
    (ch1 / "video" / "preview.mp4").write_bytes(b"ch1 approved preview bytes")
    (ch1 / "subtitles" / "preview.srt").write_text("1\n00:00:00,000 --> 00:00:00,500\nok\n", encoding="utf-8")
    p_hash = calculate_sha256(ch1 / "video" / "preview.mp4")
    approve_preview(project, "tester", review_fingerprint(project), p_hash)

    # Plant large redundant raw/, contact_sheets/, orphan panels, stale previous renders, unrelated watermark
    (ch1 / "raw").mkdir(exist_ok=True)
    (ch1 / "raw" / "raw_heavy.bin").write_bytes(b"RAW" * 1024)
    (ch1 / "contact_sheets").mkdir(exist_ok=True)
    (ch1 / "contact_sheets" / "sheet_01.jpg").write_bytes(b"SHEET" * 512)
    (ch1 / "panels" / "orphan_unused.png").write_bytes(b"ORPHAN" * 512)

    wm_dir = project.parent.parent / "watermarks"
    wm_dir.mkdir(exist_ok=True)
    (wm_dir / "default.png").write_bytes(b"default_wm")
    (wm_dir / "other_unrelated_channel.png").write_bytes(b"other_wm")

    for num in range(2, num_chapters + 1):
        ch_num = str(num)
        ch = project / "chapters" / ch_num
        shutil.copytree(ch1, ch)
        (ch / "visual_plan.json").unlink(missing_ok=True)
        (ch / "video").mkdir(exist_ok=True)
        (ch / "video" / "final.previous.mp4").write_bytes(b"STALE")
        m_data = read_json(ch / "manifest.json")
        m_data["chapter_number"] = ch_num
        atomic_write_json(ch / "manifest.json", m_data)
        c_data = read_json(ch / "chapter.json")
        c_data["chapter_number"] = ch_num
        atomic_write_json(ch / "chapter.json", c_data)
        script = load_recap_script(ch)
        s0 = script.segments[0]
        script = script.model_copy(
            update={
                "chapter_number": ch_num,
                "segments": [s0.model_copy(update={"seq_id": f"c{ch_num}_s1", "status": "pending", "duration": None})],
            }
        )
        atomic_write_json(ch / "script.json", script)

    return project


def test_minimal_payload_pruning_by_job_type_and_worker_import_preflight(unit_project, tmp_path):
    project = _build_series_project(unit_project, num_chapters=2)

    # 1. Produce series tarball skips raw/, contact_sheets/, orphan panels, cli/archive src, unrelated watermarks
    prod_tar, prod_hash = package_payload(
        project, ["1", "2"], tmp_path / "prod.tar.gz", "produce", series=True, cloud_policy="dual_tts_then_dual_encode"
    )
    with tarfile.open(prod_tar) as archive:
        names = set(archive.getnames())
        manifest = json.load(archive.extractfile("payload_manifest.json"))
        assert manifest["cloud_policy"] == "dual_tts_then_dual_encode"

    assert not any("/raw/" in n for n in names)
    assert not any("/contact_sheets/" in n for n in names)
    assert not any("orphan_unused.png" in n for n in names)
    assert not any("final.previous" in n for n in names)
    assert not any(n.startswith("src/cli/") or n.startswith("src/archive/") for n in names)
    assert "data/watermarks/default.png" in names
    assert "data/watermarks/other_unrelated_channel.png" not in names
    assert "project/chapters/1/video/preview.mp4" in names
    assert "project/chapters/2/panels/" in "\n".join(names)

    # Preflight check: extract pruned tarball & verify remote worker modules import without ModuleNotFoundError
    extracted = tmp_path / "preflight"
    with tarfile.open(prod_tar) as archive:
        archive.extractall(extracted, filter="data")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(extracted)
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import src.cloud.dual_gpu_worker, src.cloud.payload, src.cloud.scheduler, src.cloud.lifecycle, src.cloud.chunking",
        ],
        cwd=str(extracted),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr

    # 2. TTS-only payload skips active chapter panels/ as well
    tts_tar, _ = package_payload(project, "2", tmp_path / "tts.tar.gz", "tts", series=False)
    with tarfile.open(tts_tar) as archive:
        tts_names = set(archive.getnames())
    assert not any(n.startswith("project/chapters/2/panels/") for n in tts_names)
    assert "project/chapters/2/script.json" in tts_names


def test_export_receipt_verification_detects_tamper_before_local_write(unit_project, tmp_path):
    project = _build_series_project(unit_project, num_chapters=2)
    tar_path, manifest_hash = package_payload(project, "2", tmp_path / "render.tar.gz", "render", series=False)
    with tarfile.open(tar_path) as ar:
        manifest = json.load(ar.extractfile("payload_manifest.json"))
    expected_inputs = {
        k.removeprefix("project/"): v
        for k, v in manifest["files"].items()
        if k.startswith("project/") and not k.endswith("/chapter.json")
    }

    runtime = tmp_path / "runtime"
    shutil.copytree(project, runtime)
    ch2 = runtime / "chapters" / "2"
    cfg = read_json(runtime / "project.json")["render_settings"]
    script2 = load_recap_script(ch2)
    script2.segments[0].duration = 0.5
    script2.total_duration_seconds = 0.5
    atomic_write_json(ch2 / "script.json", script2)
    m2 = load_manifest(ch2)
    shots = [
        sh
        for grp in iter_segment_shots(ch2, m2, script2, cfg["target_width"], cfg["target_height"], False)
        for sh in grp
    ]
    assemble_visual_plan(
        m2, script2, shots,
        target_width=cfg["target_width"], target_height=cfg["target_height"],
        fps=cfg["fps"], motion_enabled=False,
    ).save(ch2 / "visual_plan.json")
    (ch2 / "video").mkdir(exist_ok=True)
    (ch2 / "subtitles").mkdir(exist_ok=True)
    (ch2 / "video" / "final.mp4").write_bytes(b"real output bytes")
    (ch2 / "subtitles" / "final.srt").write_text("1\n00:00:00,000 --> 00:00:00,500\nok\n", encoding="utf-8")

    # Also update local chapter 2 duration to match render-only expectation
    local_ch2 = project / "chapters" / "2"
    shutil.copy2(ch2 / "script.json", local_ch2 / "script.json")
    tar_path, manifest_hash = package_payload(project, "2", tmp_path / "render2.tar.gz", "render", series=False)
    with tarfile.open(tar_path) as ar:
        manifest = json.load(ar.extractfile("payload_manifest.json"))
    expected_inputs = {
        k.removeprefix("project/"): v
        for k, v in manifest["files"].items()
        if k.startswith("project/") and not k.endswith("/chapter.json")
    }

    out = tmp_path / "exported"
    export_results(runtime, manifest, manifest_hash, out)
    assert (out / "chapters" / "2" / "chapter_result_receipt.json").is_file()

    # Corrupt exported video without updating chapter_result_receipt.json -> sync_results must fail closed!
    (out / "chapters" / "2" / "video" / "final.mp4").write_bytes(b"corrupted!!")
    with pytest.raises(ProviderIntegrityError, match="Checksum mismatch"):
        sync_results(project, ["2"], out, "render", manifest_hash, expected_inputs, series=False)
    assert not (local_ch2 / "video" / "final.mp4").exists()

    # Restore valid video -> sync commits and persists local cloud_receipt.json
    (out / "chapters" / "2" / "video" / "final.mp4").write_bytes(b"real output bytes")
    sync_results(project, ["2"], out, "render", manifest_hash, expected_inputs, series=False)
    assert (local_ch2 / "cloud_receipt.json").is_file()
    assert is_chapter_verified_complete(project, "2", "render", series=False)

    # Editing script.json locally invalidates cached receipt cleanly
    s = load_recap_script(local_ch2)
    s.segments[0].narrator_text = "Edited text"
    atomic_write_json(local_ch2 / "script.json", s)
    assert not is_chapter_verified_complete(project, "2", "render", series=False)


def test_contiguous_chunk_planning_and_incremental_series_resume(unit_project, monkeypatch):
    project = _build_series_project(unit_project, num_chapters=4)
    chunks = plan_series_chunks(
        project,
        ["1", "2", "3", "4"],
        "produce",
        max_chapters_per_chunk=2,
    )
    assert [c.chapters for c in chunks] == [["1", "2"], ["3", "4"]]
    assert chunks[0].total_chunks == 2
    assert all(c.estimated_scratch_mb > 0 for c in chunks)

    # Mock chunked series dispatch: chunk 1 completes & syncs, chunk 2 raises, resume skips ["1", "2"] cleanly
    dispatched_batches = []
    compile_calls = []

    def _complete_local_chapter(ch_num: str):
        ch = project / "chapters" / ch_num
        cfg = read_json(project / "project.json")["render_settings"]
        s = load_recap_script(ch)
        wav = ch / "audio" / "clips" / f"audio_{ch_num}_{s.segments[0].seq_id}.wav"
        wav.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(48000)
            w.writeframes(b"\x00\x00" * 24000)
        s.segments[0].duration = 0.5
        s.segments[0].checksum = calculate_sha256(wav)
        s.segments[0].audio_path = wav.relative_to(ch).as_posix()
        s.segments[0].status = "synthesized"
        s.total_duration_seconds = 0.5
        atomic_write_json(ch / "script.json", s)
        m = load_manifest(ch)
        shots = [
            sh
            for grp in iter_segment_shots(ch, m, s, cfg["target_width"], cfg["target_height"], False)
            for sh in grp
        ]
        assemble_visual_plan(
            m, s, shots,
            target_width=cfg["target_width"], target_height=cfg["target_height"],
            fps=cfg["fps"], motion_enabled=False,
        ).save(ch / "visual_plan.json")
        (ch / "video").mkdir(parents=True, exist_ok=True)
        (ch / "subtitles").mkdir(parents=True, exist_ok=True)
        (ch / "video" / "final.mp4").write_bytes(f"mp4_{ch_num}".encode())
        (ch / "subtitles" / "final.srt").write_text("1\n00:00:00,000 --> 00:00:00,500\nok\n", encoding="utf-8")
        from src.cloud.chunking import build_chapter_receipt
        rec = build_chapter_receipt(
            project_slug="unit",
            chapter_number=ch_num,
            job_type="produce",
            quality="final",
            payload_manifest_sha256="a" * 64,
            exported_files=[ch / "script.json", ch / "visual_plan.json", ch / "video" / "final.mp4", ch / "subtitles" / "final.srt"],
            base_dir=ch,
            script_sha256=calculate_sha256(ch / "script.json"),
        )
        atomic_write_json(ch / "cloud_receipt.json", rec)

    fail_on_second = [True]

    def fake_dispatch(self, slug, ch, **kwargs):
        chs = list(kwargs.get("series_chapters") or [ch])
        dispatched_batches.append(chs)
        if fail_on_second[0] and chs == ["3", "4"]:
            raise RuntimeError("Session interrupted before chunk 2!")
        published = {}
        for c in chs:
            _complete_local_chapter(c)
            published[c] = {"chapter_number": c, "status": "success"}
        return {"status": "success", "published": published}

    monkeypatch.setattr(KaggleCloudService, "dispatch_chapter_job_with_fallback", fake_dispatch)
    monkeypatch.setattr(
        "src.core.workflow.compile_project_chapters",
        lambda slug, chapters=None, output=None: compile_calls.append(chapters) or {"compiled_video": "/tmp/full.mp4"},
    )

    svc = object.__new__(KaggleCloudService)
    with pytest.raises(RuntimeError, match="Session interrupted"):
        svc.dispatch_series_chunks(
            "unit", ["1", "2", "3", "4"], job_type="produce", max_chapters_per_chunk=2, compile_feature=True
        )
    # Chunk 1 succeeded, Chunk 2 failed -> feature compile must NOT run prematurely!
    assert dispatched_batches == [["1", "2"], ["3", "4"]]
    assert compile_calls == []
    done, rem = filter_pending_chapters(project, ["1", "2", "3", "4"], "produce", series=True)
    assert done == ["1", "2"] and rem == ["3", "4"]

    # Resume -> skips ["1", "2"], runs ONLY ["3", "4"], then compiles full feature ["1,2,3,4"]!
    fail_on_second[0] = False
    dispatched_batches.clear()
    resumed = svc.resume_series_chunks("unit", max_chapters_per_chunk=2, compile_feature=True)
    assert dispatched_batches == [["3", "4"]]
    assert compile_calls == ["1,2,3,4"]
    assert resumed["skipped_verified"] == ["1", "2"]
    assert resumed["compiled_video"] == "/tmp/full.mp4"
