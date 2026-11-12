"""Unit and L2 tests for P14 Canonical ReleaseState Machine and bypass elimination."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
from PIL import Image

from tests.unit.test_series_setup import _make_valid_answers_dict
from src.cli.batch_cmd import handle_batch
from src.cli.compile_cmd import handle_compile
from src.cli.exit_codes import CliValidationError
from src.cli.render_cmd import handle_render
from src.cli.validate_cmd import handle_validate
from src.core.contracts import (
    ArchiveReceipt,
    ReleaseRecord,
    ReleaseState,
    YouTubeReceipt,
)
from src.core.human_review import (
    HumanApproval,
    approve_preview,
    require_preview_approval,
    review_fingerprint,
)
from src.core.schemas import (
    CheckpointStatus,
    HumanReviewStatus,
    PanelItem,
    PanelManifest,
    RecapScript,
    ScriptSegment,
    StageType,
)
from src.core.series_setup import record_series_setup
from src.core.state_machine import (
    ALLOWED_TRANSITIONS,
    STATE_ORDER,
    StateTransitionError,
    TransitionResult,
    advance_release_state,
    can_transition,
    load_release_record,
    transition,
)
from src.core.storage import (
    atomic_write_json,
    calculate_sha256,
    init_chapter_layout,
    init_project_layout,
    load_chapter_metadata,
    read_json,
)
from src.render.artifact_validation import fingerprint
from src.render.chapter_renderer import render_chapter


def _hex64(tag: str) -> str:
    return hashlib.sha256(tag.encode("utf-8")).hexdigest()


def _hex40(tag: str) -> str:
    return hashlib.sha1(tag.encode("utf-8")).hexdigest()


def _make_in_memory_evidence() -> dict[ReleaseState, dict]:
    setup_hash = _hex64("setup")
    manifest_hash = _hex64("manifest")
    panel_hash = _hex64("panel_1")
    script_hash = _hex64("script")
    clip_hash = _hex64("clip_1")
    input_fp = _hex64("visual_plan_inputs")
    plan_hash = _hex64("visual_plan")
    video_hash = _hex64("video")
    sub_hash = _hex64("subtitle")
    av_hash = _hex64("artifact_validation")
    cfg_fp = _hex64("review_cfg")
    commit_sha = _hex40("hf_commit")
    archive_manifest_sha = _hex64("archive_manifest")

    script_obj = RecapScript(
        project_slug="sm_unit",
        chapter_number="1",
        title="Chapter 1",
        segments=[
            ScriptSegment(
                seq_id="001",
                panel_ids=["p1"],
                narrator_text="Valid Vietnamese narration for panel one.",
            )
        ],
    )
    av_report = {
        "valid": True,
        "schema_version": "4.1.0",
        "chapter_number": "1",
        "video": {"valid": True, "sha256": video_hash, "duration": 0.5},
        "subtitles": {"valid": True, "sha256": sub_hash, "cue_count": 1},
        "plan_fingerprint": _hex64("plan_model"),
        "script_fingerprint": _hex64("script_model"),
        "evidence_fingerprint": _hex64("manifest_model"),
    }
    approval = HumanApproval(
        human_review_status="approved",
        project_slug="sm_unit",
        chapter_number="1",
        approved_at="2026-11-12T00:00:00Z",
        reviewer="qa_reviewer",
        config_fingerprint=cfg_fp,
        artifact_hash=video_hash,
    )
    yt_uploaded = YouTubeReceipt(
        project_slug="sm_unit",
        chapter_number="1",
        video_id="yt_vid_001",
        video_url="https://youtu.be/yt_vid_001",
        account="main",
        channel_title="Main Channel",
        channel_id="UC123456",
        privacy_status="private",
        captions_uploaded=True,
        thumbnail_uploaded=True,
        captions_required=True,
        thumbnail_required=True,
        uploaded_at="2026-11-12T00:05:00Z",
        status="uploaded",
    )
    yt_verified = YouTubeReceipt(
        receipt_type="youtube_verification",
        project_slug="sm_unit",
        chapter_number="1",
        video_id="yt_vid_001",
        video_url="https://youtu.be/yt_vid_001",
        account="main",
        channel_title="Main Channel",
        channel_id="UC123456",
        upload_status="processed",
        has_captions=True,
        has_custom_thumbnail=True,
        verified_at="2026-11-12T00:06:00Z",
        privacy_status="private",
        captions_uploaded=True,
        thumbnail_uploaded=True,
        captions_required=True,
        thumbnail_required=True,
        uploaded_at="2026-11-12T00:05:00Z",
        status="verified",
    )
    arch_uploaded = ArchiveReceipt(
        project_slug="sm_unit",
        repo_id="org/manga-recap-archive",
        commit_sha=commit_sha,
        remote_url=f"https://huggingface.co/datasets/org/manga-recap-archive/tree/{commit_sha}",
        created_at="2026-11-12T00:10:00Z",
        verified_file_count=0,
        total_bytes=1024,
        manifest_sha256=archive_manifest_sha,
        status="uploaded",
    )
    arch_verified = ArchiveReceipt(
        project_slug="sm_unit",
        repo_id="org/manga-recap-archive",
        commit_sha=commit_sha,
        remote_url=f"https://huggingface.co/datasets/org/manga-recap-archive/tree/{commit_sha}",
        created_at="2026-11-12T00:10:00Z",
        verified_file_count=5,
        total_bytes=1024,
        manifest_sha256=archive_manifest_sha,
        status="verified",
        verification_revision=commit_sha,
        last_verified_at="2026-11-12T00:11:00Z",
    )
    arch_restored = ArchiveReceipt(
        project_slug="sm_unit",
        repo_id="org/manga-recap-archive",
        commit_sha=commit_sha,
        remote_url=f"https://huggingface.co/datasets/org/manga-recap-archive/tree/{commit_sha}",
        created_at="2026-11-12T00:10:00Z",
        verified_file_count=5,
        total_bytes=1024,
        manifest_sha256=archive_manifest_sha,
        status="verified",
        verification_revision=commit_sha,
        last_verified_at="2026-11-12T00:11:00Z",
        restore_verified_at="2026-11-12T00:12:00Z",
        restored_revision=commit_sha,
        restored_verified_files=5,
    )

    return {
        ReleaseState.SETUP_VALID: {"setup_snapshot_hash": setup_hash},
        ReleaseState.SOURCE_READY: {
            "manifest_sha256": manifest_hash,
            "panel_hashes": {"p1": panel_hash},
        },
        ReleaseState.SCRIPT_READY: {
            "script_sha256": script_hash,
            "script": script_obj,
            "panel_hashes": {"p1": panel_hash},
        },
        ReleaseState.AUDIO_VALID: {
            "audio_clips": [{"seq_id": "001", "sha256": clip_hash, "duration": 0.5, "valid": True}],
        },
        ReleaseState.VISUAL_PLAN_VALID: {
            "input_fingerprint": input_fp,
            "visual_plan_sha256": plan_hash,
        },
        ReleaseState.RENDER_VALID: {
            "video_sha256": video_hash,
            "subtitle_sha256": sub_hash,
        },
        ReleaseState.TECHNICALLY_VALID: {
            "artifact_validation": av_report,
            "artifact_validation_sha256": av_hash,
        },
        ReleaseState.HUMAN_APPROVED: {
            "preview_approval": approval,
        },
        ReleaseState.PRODUCTION_READY: {
            "final_video_sha256": video_hash,
            "artifact_validation_valid": True,
        },
        ReleaseState.UPLOADED: {
            "youtube_receipt": yt_uploaded,
        },
        ReleaseState.YOUTUBE_VERIFIED: {
            "youtube_receipt": yt_verified,
        },
        ReleaseState.ARCHIVED: {
            "archive_receipt": arch_uploaded,
        },
        ReleaseState.ARCHIVE_VERIFIED: {
            "archive_receipt": arch_verified,
        },
        ReleaseState.RESTORE_VERIFIED: {
            "archive_receipt": arch_restored,
        },
        ReleaseState.CLEANUP_ALLOWED: {
            "archive_receipt": arch_restored,
            "youtube_receipt": yt_verified,
        },
    }


@pytest.fixture
def project_l2(tmp_path, monkeypatch):
    """Isolated project fixture with real PNG panel, PCM WAV, and confirmed Q01-Q10 setup."""
    monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MANGA_RECAP_OUTPUT_DIR", str(tmp_path / "output"))
    init_project_layout("sm_proj", "State Machine Fixture")
    init_chapter_layout("sm_proj", "1")
    root = tmp_path / "data" / "projects" / "sm_proj"
    chapter = root / "chapters" / "1"

    answers = _make_valid_answers_dict()
    for key, value in dict(
        Q02="local",
        Q05=False,
        Q07=False,
        Q08={"no_upload": True},
        Q09="preview",
        Q10=False,
    ).items():
        answers[key]["user_answer"] = value
    record_series_setup("sm_proj", answers, project_dir=root)
    atomic_write_json(
        root / "voice_selection.json",
        {
            "project_slug": "sm_proj",
            "primary_voice": "Ngọc Huyền",
            "reviewer": "l2_fixture",
            "status": "approved",
            "speed": 1.0,
        },
    )

    panel_path = chapter / "panels" / "p1.png"
    Image.fromarray(np.tile(np.arange(200, dtype=np.uint8), (150, 1))).convert("RGB").save(panel_path)
    panel = PanelItem(
        panel_id="p1",
        sequence_number=1,
        filename="p1.png",
        relative_path="panels/p1.png",
        source_image="page_1.png",
        width=200,
        height=150,
        aspect_ratio=200 / 150,
        crop_box=(0, 0, 200, 150),
        sha256=calculate_sha256(panel_path),
        evidence_metadata={"text_detection_uncertain": False},
    )
    atomic_write_json(
        chapter / "manifest.json",
        PanelManifest(project_slug="sm_proj", chapter_number="1", panels=[panel]),
    )

    audio_path = chapter / "audio" / "clips" / "seg_001.wav"
    with wave.open(str(audio_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(48000)
        wf.writeframes((np.sin(np.arange(24000) / 10) * 2000).astype("<i2").tobytes())

    script = RecapScript(
        project_slug="sm_proj",
        chapter_number="1",
        title="Chapter 1",
        segments=[
            ScriptSegment(
                seq_id="001",
                evidence_panel_ids=["p1"],
                narrator_text="L2 fixture narration matching panel one.",
                duration=0.5,
                audio_path="audio/clips/seg_001.wav",
                checksum=calculate_sha256(audio_path),
            )
        ],
    )
    atomic_write_json(chapter / "script.json", script)
    return root, chapter


class TestCanonicalStateMachineUnit:
    def test_state_order_and_allowed_transitions_cover_all_16_states(self):
        assert len(STATE_ORDER) == 16
        assert list(STATE_ORDER) == list(ReleaseState)
        for idx, state in enumerate(STATE_ORDER):
            if idx + 1 < len(STATE_ORDER):
                assert ALLOWED_TRANSITIONS[state] == (STATE_ORDER[idx + 1],)
                assert can_transition(state, STATE_ORDER[idx + 1]) is True
            else:
                assert ALLOWED_TRANSITIONS[state] == ()
                assert can_transition(state, ReleaseState.DRAFT) is False

    def test_can_transition_rejects_skips_backwards_self_and_unknown(self):
        for i, src in enumerate(STATE_ORDER):
            for j, dst in enumerate(STATE_ORDER):
                expected = j == i + 1
                assert can_transition(src, dst) is expected
                assert can_transition(src.value, dst.value) is expected
        assert can_transition("BOGUS", ReleaseState.SETUP_VALID) is False
        assert can_transition(ReleaseState.DRAFT, "BOGUS") is False

    def test_full_in_memory_transition_chain_draft_to_cleanup_allowed(self):
        evidence_by_target = _make_in_memory_evidence()
        current = ReleaseState.DRAFT
        accumulated_hashes: dict[str, str] = {}

        for target in STATE_ORDER[1:]:
            res = transition(current, target, evidence=evidence_by_target[target])
            assert isinstance(res, TransitionResult)
            assert res.previous_state == current
            assert res.target_state == target
            assert res.state == target
            assert res.current_state == target
            assert res.evidence_hashes
            accumulated_hashes.update(res.evidence_hashes)
            current = res.state

        assert current == ReleaseState.CLEANUP_ALLOWED
        assert "setup_snapshot_hash" in accumulated_hashes
        assert "cleanup_gate_digest" in accumulated_hashes

    def test_all_invalid_skips_and_backwards_transitions_raise_state_transition_error(self):
        evidence_by_target = _make_in_memory_evidence()
        for i, src in enumerate(STATE_ORDER):
            for j, dst in enumerate(STATE_ORDER):
                if j == i + 1:
                    continue
                ev = evidence_by_target.get(dst, {})
                with pytest.raises(StateTransitionError) as exc_info:
                    transition(src, dst, evidence=ev)
                assert isinstance(exc_info.value, ValueError)

    @pytest.mark.parametrize("idx", range(1, len(STATE_ORDER)))
    def test_every_transition_rejects_missing_or_corrupt_evidence(self, idx):
        src = STATE_ORDER[idx - 1]
        dst = STATE_ORDER[idx]
        with pytest.raises(StateTransitionError):
            transition(src, dst, evidence=None)
        with pytest.raises(StateTransitionError):
            transition(src, dst, evidence={})

    def test_in_memory_validators_reject_specific_bad_evidence(self):
        ev = _make_in_memory_evidence()

        # Bad SHA256
        with pytest.raises(StateTransitionError):
            transition(ReleaseState.DRAFT, ReleaseState.SETUP_VALID, {"setup_snapshot_hash": "not-sha256"})

        # Script with needs_review / [CAN_XAC_MINH
        bad_script = RecapScript(
            project_slug="sm_unit",
            chapter_number="1",
            segments=[ScriptSegment(seq_id="001", panel_ids=["p1"], narrator_text="[CAN_XAC_MINH: blurry]")],
        )
        with pytest.raises(StateTransitionError):
            transition(
                ReleaseState.SOURCE_READY,
                ReleaseState.SCRIPT_READY,
                {"script_sha256": _hex64("s"), "script": bad_script, "panel_hashes": {"p1": _hex64("p")}},
            )

        # Zero duration audio
        with pytest.raises(StateTransitionError):
            transition(
                ReleaseState.SCRIPT_READY,
                ReleaseState.AUDIO_VALID,
                {"audio_clips": [{"seq_id": "001", "sha256": _hex64("a"), "duration": 0.0}]},
            )

        # artifact_validation valid=False
        bad_av = dict(ev[ReleaseState.TECHNICALLY_VALID]["artifact_validation"], valid=False)
        with pytest.raises(StateTransitionError):
            transition(
                ReleaseState.RENDER_VALID,
                ReleaseState.TECHNICALLY_VALID,
                {"artifact_validation": bad_av},
            )

        # YouTube uploaded cannot satisfy YOUTUBE_VERIFIED
        with pytest.raises(StateTransitionError):
            transition(
                ReleaseState.UPLOADED,
                ReleaseState.YOUTUBE_VERIFIED,
                {"youtube_receipt": ev[ReleaseState.UPLOADED]["youtube_receipt"]},
            )

        # Archive without restore_verified_at cannot satisfy RESTORE_VERIFIED
        with pytest.raises(StateTransitionError):
            transition(
                ReleaseState.ARCHIVE_VERIFIED,
                ReleaseState.RESTORE_VERIFIED,
                {"archive_receipt": ev[ReleaseState.ARCHIVE_VERIFIED]["archive_receipt"]},
            )


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg/FFprobe required")
class TestCanonicalStateMachineL2AndBypasses:
    def test_full_project_bound_lifecycle_and_atomic_roundtrip(self, project_l2):
        root, chapter = project_l2
        initial = load_release_record(root)
        assert initial.state == ReleaseState.DRAFT
        assert initial.evidence_hashes == {}

        # 1. SETUP_VALID
        rec = advance_release_state(root, ReleaseState.SETUP_VALID)
        assert rec.state == ReleaseState.SETUP_VALID
        assert rec.evidence_hashes["setup_snapshot_hash"] == read_json(root / "series_setup_answers.json")["snapshot_hash"]

        # 2. SOURCE_READY
        rec = advance_release_state(root, ReleaseState.SOURCE_READY, chapter_number="1")
        assert rec.state == ReleaseState.SOURCE_READY
        assert rec.evidence_hashes["manifest_sha256"] == calculate_sha256(chapter / "manifest.json")

        # 3. SCRIPT_READY
        rec = advance_release_state(root, ReleaseState.SCRIPT_READY, chapter_number="1")
        assert rec.state == ReleaseState.SCRIPT_READY
        assert rec.evidence_hashes["script_sha256"] == calculate_sha256(chapter / "script.json")

        # 4. AUDIO_VALID
        rec = advance_release_state(root, ReleaseState.AUDIO_VALID, chapter_number="1")
        assert rec.state == ReleaseState.AUDIO_VALID
        assert "audio_manifest_sha256" in rec.evidence_hashes

        # Render preview produces visual_plan.json, preview.mp4, preview.srt, artifact_validation.json
        preview_res = render_chapter(root, "1", quality="preview")
        assert preview_res["validation"]["valid"] is True

        # 5. VISUAL_PLAN_VALID
        rec = advance_release_state(root, ReleaseState.VISUAL_PLAN_VALID, chapter_number="1")
        assert rec.state == ReleaseState.VISUAL_PLAN_VALID

        # 6. RENDER_VALID
        rec = advance_release_state(root, ReleaseState.RENDER_VALID, chapter_number="1")
        assert rec.state == ReleaseState.RENDER_VALID

        # 7. TECHNICALLY_VALID
        rec = advance_release_state(root, ReleaseState.TECHNICALLY_VALID, chapter_number="1")
        assert rec.state == ReleaseState.TECHNICALLY_VALID

        # 8. HUMAN_APPROVED via approve_preview (also verifies chapter.json + release_state.json sync)
        approval = approve_preview(
            root,
            "qa_human",
            review_fingerprint(root),
            fingerprint(chapter / "video" / "preview.mp4"),
        )
        assert approval.human_review_status == "approved"
        rec = load_release_record(root)
        assert rec.state == ReleaseState.HUMAN_APPROVED
        assert load_chapter_metadata(chapter).human_review_status == HumanReviewStatus.APPROVED

        # 9. PRODUCTION_READY after promoting final.mp4
        final_res = render_chapter(root, "1", quality="final")
        assert Path(final_res["video_path"]).is_file()
        rec = advance_release_state(root, ReleaseState.PRODUCTION_READY, chapter_number="1")
        assert rec.state == ReleaseState.PRODUCTION_READY

        # 10. UPLOADED
        yt_upload = YouTubeReceipt(
            project_slug="sm_proj",
            chapter_number="1",
            video_id="yt_l2_123",
            video_url="https://youtu.be/yt_l2_123",
            account="main",
            channel_title="Channel One",
            channel_id="UC_MAIN",
            privacy_status="private",
            captions_uploaded=True,
            thumbnail_uploaded=False,
            captions_required=True,
            thumbnail_required=False,
            uploaded_at="2026-11-12T01:00:00Z",
            status="uploaded",
        )
        atomic_write_json(chapter / "youtube_receipt.json", yt_upload)
        rec = advance_release_state(root, ReleaseState.UPLOADED, chapter_number="1")
        assert rec.state == ReleaseState.UPLOADED

        # 11. YOUTUBE_VERIFIED
        yt_verified = yt_upload.model_copy(
            update={
                "receipt_type": "youtube_verification",
                "status": "verified",
                "upload_status": "processed",
                "has_captions": True,
                "verified_at": "2026-11-12T01:02:00Z",
            }
        )
        atomic_write_json(chapter / "youtube_receipt.json", yt_verified)
        rec = advance_release_state(root, ReleaseState.YOUTUBE_VERIFIED, chapter_number="1")
        assert rec.state == ReleaseState.YOUTUBE_VERIFIED

        # 12. ARCHIVED -> 13. ARCHIVE_VERIFIED -> 14. RESTORE_VERIFIED -> 15. CLEANUP_ALLOWED
        archive_manifest = {"project_slug": "sm_proj", "files": [{"path": "project.json"}]}
        atomic_write_json(root / "archive_manifest.json", archive_manifest)
        manifest_sha = calculate_sha256(root / "archive_manifest.json")
        commit_sha = _hex40("l2_archive_commit")

        arch_up = ArchiveReceipt(
            project_slug="sm_proj",
            repo_id="org/archive",
            commit_sha=commit_sha,
            remote_url=f"https://huggingface.co/datasets/org/archive/tree/{commit_sha}",
            created_at="2026-11-12T01:05:00Z",
            verified_file_count=0,
            total_bytes=2048,
            manifest_sha256=manifest_sha,
            status="uploaded",
        )
        atomic_write_json(root / "archive_receipt.json", arch_up)
        rec = advance_release_state(root, ReleaseState.ARCHIVED, chapter_number="1")
        assert rec.state == ReleaseState.ARCHIVED

        arch_ver = arch_up.model_copy(
            update={
                "status": "verified",
                "verified_file_count": 1,
                "verification_revision": commit_sha,
                "last_verified_at": "2026-11-12T01:06:00Z",
            }
        )
        atomic_write_json(root / "archive_receipt.json", arch_ver)
        rec = advance_release_state(root, ReleaseState.ARCHIVE_VERIFIED, chapter_number="1")
        assert rec.state == ReleaseState.ARCHIVE_VERIFIED

        arch_res = arch_ver.model_copy(
            update={
                "restore_verified_at": "2026-11-12T01:07:00Z",
                "restored_revision": commit_sha,
                "restored_verified_files": 1,
            }
        )
        atomic_write_json(root / "archive_receipt.json", arch_res)
        rec = advance_release_state(root, ReleaseState.RESTORE_VERIFIED, chapter_number="1")
        assert rec.state == ReleaseState.RESTORE_VERIFIED

        rec = advance_release_state(root, ReleaseState.CLEANUP_ALLOWED, chapter_number="1")
        assert rec.state == ReleaseState.CLEANUP_ALLOWED

        # Atomic round-trip check against frozen ReleaseRecord contract
        persisted = ReleaseRecord.model_validate(read_json(root / "release_state.json"))
        assert persisted == rec
        assert load_release_record(root) == rec

    def test_validate_cmd_is_strictly_read_only(self, project_l2):
        root, chapter = project_l2
        before = (chapter / "chapter.json").read_bytes()
        res = handle_validate(argparse.Namespace(project="sm_proj", chapter="1", strict=False))
        assert res["valid"] is True
        assert (chapter / "chapter.json").read_bytes() == before
        assert not (root / "release_state.json").exists()

    def test_batch_cmd_does_not_fabricate_ingest_or_render_failure(self, project_l2):
        root, chapter = project_l2
        preview_res = render_chapter(root, "1", quality="preview")
        approve_preview(
            root,
            "qa_human",
            review_fingerprint(root),
            fingerprint(preview_res["video_path"]),
        )
        # Create chapter 2 with manifest.json and an invalid panel reference so handle_validate fails inside batch
        chap2 = init_chapter_layout("sm_proj", "2")
        shutil.copytree(chapter / "panels", chap2 / "panels", dirs_exist_ok=True)
        m2 = read_json(chapter / "manifest.json")
        m2["chapter_number"] = "2"
        atomic_write_json(chap2 / "manifest.json", m2)
        bad_script = read_json(chapter / "script.json")
        bad_script["chapter_number"] = "2"
        bad_script["segments"][0]["panel_ids"] = ["nonexistent_panel.png"]
        bad_script["segments"][0]["evidence_panel_ids"] = ["nonexistent_panel.png"]
        atomic_write_json(chap2 / "script.json", bad_script)

        result = handle_batch(
            argparse.Namespace(project="sm_proj", chapters="2", approved=True, skip_preview=True)
        )
        assert result["success"] is False
        assert len(result["failed"]) == 1

        meta = load_chapter_metadata(chap2)
        assert meta.stages[StageType.INGEST].status == CheckpointStatus.PENDING
        assert meta.stages[StageType.AGENT].status == CheckpointStatus.FAILED
        assert meta.stages[StageType.RENDER].status == CheckpointStatus.PENDING

    def test_render_cmd_requires_preview_approval_before_marking_ch1_approved(self, project_l2):
        root, chapter = project_l2
        preview_res = render_chapter(root, "1", quality="preview")
        approve_preview(
            root,
            "qa_human",
            review_fingerprint(root),
            fingerprint(preview_res["video_path"]),
        )
        # Tamper preview_approval.json artifact_hash and reset chapter review status
        pa = read_json(root / "preview_approval.json")
        pa["artifact_hash"] = _hex64("tampered")
        atomic_write_json(root / "preview_approval.json", pa)
        ch_data = read_json(chapter / "chapter.json")
        ch_data["human_review_status"] = "unreviewed"
        atomic_write_json(chapter / "chapter.json", ch_data)

        fake_engine_res = {
            "video_path": str(chapter / "video" / "preview.mp4"),
            "subtitle_path": str(chapter / "subtitles" / "preview.srt"),
        }
        with patch("src.cli.render_cmd.enforce_execution_policy"), patch(
            "src.cli.render_cmd.VideoEngine.render_chapter", return_value=fake_engine_res
        ):
            with pytest.raises(CliValidationError, match="Gate G6"):
                handle_render(argparse.Namespace(project="sm_proj", chapter="1", quality="final"))

        assert load_chapter_metadata(chapter).human_review_status == HumanReviewStatus.UNREVIEWED
        # Ensure render_cmd succeeds cleanly and advances release_state when approval is valid
        approve_preview(
            root,
            "qa_human",
            review_fingerprint(root),
            fingerprint(preview_res["video_path"]),
        )
        res = handle_render(argparse.Namespace(project="sm_proj", chapter="1", quality="final"))
        assert Path(res["video_path"]).is_file()
        assert load_chapter_metadata(chapter).human_review_status == HumanReviewStatus.APPROVED
        assert load_release_record(root).state == ReleaseState.PRODUCTION_READY

    def test_compile_cmd_rejects_v3_preview_approval_and_accepts_human_approval(self, project_l2, tmp_path):
        root, chapter = project_l2
        preview_res = render_chapter(root, "1", quality="preview")
        approve_preview(
            root,
            "qa_human",
            review_fingerprint(root),
            fingerprint(preview_res["video_path"]),
        )
        render_chapter(root, "1", quality="final")

        # Replace canonical HumanApproval with legacy V3 PreviewApproval payload
        saved_canonical = read_json(root / "preview_approval.json")
        atomic_write_json(
            root / "preview_approval.json",
            {
                "schema_version": "3.0.0",
                "project_slug": "sm_proj",
                "chapter_number": "1",
                "reviewer": "legacy_user",
                "status": "approved",
            },
        )
        with pytest.raises(CliValidationError, match="Gate G6"):
            handle_compile(
                argparse.Namespace(
                    project="sm_proj",
                    chapters="1",
                    output=str(tmp_path / "compiled_fail.mp4"),
                    approved=False,
                )
            )

        # Restore canonical HumanApproval -> compile passes
        atomic_write_json(root / "preview_approval.json", saved_canonical)
        res = handle_compile(
            argparse.Namespace(
                project="sm_proj",
                chapters="1",
                output=str(tmp_path / "compiled_ok.mp4"),
                approved=False,
            )
        )
        assert Path(res["compiled_video"]).is_file()
