"""
tests/unit/test_hf_archive.py
Unit and L2 integration tests for Private Hugging Face Dataset Archive & Restoration Engine (V4).
Covers:
- Secret exclusion filter tests (.env, OAuth tokens, client_secrets, kaggle tokens, symlinks, traversal).
- V4 whitelist expansion (data/projects/<slug>/ + output/<slug>/).
- Recursive SHA-256 manifest calculation.
- ArchiveReceipt 40-hex commit_sha enforcement and ReleaseState transitions.
- Remote verification logic with mocked HfApi.
- Atomic empty-dir sandbox restore verification (all SHA-256, missing/extra files,
  ffprobe/validate_video, validate_audio, validate_subtitles, PIL, and V4 Pydantic schemas).
"""

import hashlib
import json
import os
import shutil
import subprocess
import wave
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest
from PIL import Image
from unit.test_series_setup import _make_valid_answers_dict

from src.archive.hf_archive import (
    DEFAULT_HF_REPO_ID,
    FALLBACK_HF_TOKEN,
    archive_push,
    archive_restore,
    archive_verify,
    compute_file_sha256,
    generate_archive_manifest,
    is_whitelisted_archive_path,
    resolve_hf_token,
    verify_media_file_header,
)
from src.cli.exit_codes import CliValidationError
from src.cli.main import main
from src.core.contracts import ArchiveReceipt, ReleaseState, YouTubeReceipt
from src.core.human_review import HumanApproval, approve_preview, review_fingerprint
from src.core.schemas import (
    PanelItem,
    PanelManifest,
    RecapScript,
    ScriptSegment,
)
from src.core.series_setup import record_series_setup
from src.core.state_machine import advance_release_state, load_release_record
from src.core.storage import (
    atomic_write_json,
    calculate_sha256,
    init_chapter_layout,
    init_project_layout,
    read_json,
)
from src.core.visual_contract import ProductionMotion, Shot, VisualPlan

SHA40_A = "a1b2c3d4e5f67890abcdef1234567890abcdef12"
SHA40_B = "b2c3d4e5f67890abcdef1234567890abcdef1234"


def _write_real_wav(path: Path, duration: float = 0.25, rate: int = 48000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    n_samples = int(duration * rate)
    samples = (np.sin(np.arange(n_samples) / 10.0) * 3000).astype("<i2")
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(samples.tobytes())


def _write_real_mp4(path: Path, duration: float = 0.25, width: int = 160, height: int = 120, fps: int = 30) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=duration={duration}:size={width}x{height}:rate={fps}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={duration}",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ],
        check=True,
    )


def _write_real_jpeg(path: Path, size=(160, 120)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    arr = np.tile(np.arange(size[0], dtype=np.uint8), (size[1], 1))
    Image.fromarray(arr).convert("RGB").save(path, format="JPEG")


@pytest.fixture
def mock_archive_project(tmp_path, monkeypatch):
    """Creates a realistic V4 project + output layout with real media and planted secrets."""
    data_dir = tmp_path / "data"
    output_dir = tmp_path / "output"
    monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(data_dir))
    monkeypatch.setenv("MANGA_RECAP_OUTPUT_DIR", str(output_dir))

    slug = "test_archive_series"
    proj_dir = init_project_layout(slug, "Test Archive Series")
    chap_dir = init_chapter_layout(slug, 1)

    # Full V4 setup + voice approval
    answers = _make_valid_answers_dict()
    answers["Q02"]["user_answer"] = "local"
    answers["Q07"]["user_answer"] = False
    answers["Q08"]["user_answer"] = {"account": "review_truyen", "privacy": "private"}
    answers["Q09"]["user_answer"] = "final"
    answers["Q10"]["user_answer"] = False
    record_series_setup(slug, answers, project_dir=proj_dir)

    atomic_write_json(
        proj_dir / "voice_selection.json",
        {
            "schema_version": "3.0.0",
            "project_slug": slug,
            "voice_strategy": "single_narrator",
            "primary_voice": "Ngọc Huyền",
            "speed": 1.0,
            "reviewer": "human_tester",
            "status": "approved",
        },
    )

    story_summary = proj_dir / "story_summary.md"
    story_summary.write_text("# Story Summary\nHào khí kiếm hiệp giang hồ.", encoding="utf-8")

    # Real images & PanelManifest
    raw_img = chap_dir / "raw" / "page_001.jpg"
    _write_real_jpeg(raw_img)

    panel_img = chap_dir / "panels" / "panel_001.jpg"
    _write_real_jpeg(panel_img)
    panel_item = PanelItem(
        panel_id="panel_001",
        sequence_number=1,
        filename="panel_001.jpg",
        relative_path="panels/panel_001.jpg",
        source_image="page_001.jpg",
        width=160,
        height=120,
        aspect_ratio=160 / 120,
        crop_box=(0, 0, 160, 120),
        sha256=calculate_sha256(panel_img),
    )
    atomic_write_json(
        chap_dir / "manifest.json",
        PanelManifest(project_slug=slug, chapter_number="1", panels=[panel_item]),
    )

    # Real WAV + RecapScript
    audio_clip = chap_dir / "audio" / "clips" / "audio_1_001.wav"
    _write_real_wav(audio_clip, duration=0.25)

    script_obj = RecapScript(
        project_slug=slug,
        chapter_number="1",
        title="Chapter 1 Opening",
        segments=[
            ScriptSegment(
                seq_id="001",
                evidence_panel_ids=["panel_001"],
                narrator_text="Tại vùng biên ải xa xôi, thiếu niên kiếm khách bắt đầu hành trình.",
                duration=0.25,
                audio_path="audio/clips/audio_1_001.wav",
                checksum=calculate_sha256(audio_clip),
            )
        ],
    )
    atomic_write_json(chap_dir / "script.json", script_obj)

    # Real render + HumanApproval via canonical renderer
    from src.render.artifact_validation import fingerprint
    from src.render.chapter_renderer import render_chapter

    preview_res = render_chapter(proj_dir, "1", quality="preview")
    cfg_fp = review_fingerprint(proj_dir)
    approve_preview(proj_dir, "human_tester", cfg_fp, fingerprint(preview_res["video_path"]))
    final_res = render_chapter(proj_dir, "1", quality="final")
    video_final = Path(final_res["video_path"])
    subtitles = Path(final_res["subtitle_path"])

    # Output staged files (output/<slug>/...)
    out_proj_dir = output_dir / slug
    out_proj_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(video_final, out_proj_dir / "chapter_001.mp4")
    shutil.copy2(subtitles, out_proj_dir / "chapter_001.srt")

    yt_receipt = YouTubeReceipt(
        receipt_type="youtube_verification",
        project_slug=slug,
        chapter_number="1",
        video_id="yt_arch_vid_1",
        video_url="https://www.youtube.com/watch?v=yt_arch_vid_1",
        account="review_truyen",
        channel_title="Review Truyen",
        channel_id="UC_ARCH_123",
        upload_status="processed",
        has_captions=True,
        has_custom_thumbnail=False,
        verified_at="2026-11-12T10:00:00Z",
        privacy_status="private",
        captions_uploaded=True,
        thumbnail_uploaded=False,
        captions_required=True,
        thumbnail_required=False,
        uploaded_at="2026-11-12T09:55:00Z",
        status="verified",
    )
    atomic_write_json(chap_dir / "youtube_receipt.json", yt_receipt)
    atomic_write_json(out_proj_dir / "chapter_001.youtube_receipt.json", yt_receipt)

    # Planted forbidden secrets that MUST be filtered out in both proj_dir and out_dir
    (proj_dir / ".env").write_text("HF_TOKEN=secret_hf_key\nOPENAI_API_KEY=sk-dummy", encoding="utf-8")
    (proj_dir / ".env.production").write_text("SECRET=production_password", encoding="utf-8")
    (proj_dir / "client_secrets.json").write_text('{"installed": {"client_id": "secret_id"}}', encoding="utf-8")
    (proj_dir / ".youtube_token.json").write_text('{"token": "oauth2_refresh_token"}', encoding="utf-8")
    (proj_dir / "kaggle.json").write_text('{"username": "test", "key": "kaggle_key"}', encoding="utf-8")
    (proj_dir / "token.pickle").write_bytes(b"PICKLE_SECRET_TOKEN")
    (chap_dir / "temp_cache.tmp").write_bytes(b"TEMP_CACHE")
    (chap_dir / "run.lock").write_bytes(b"LOCK")
    (out_proj_dir / ".env").write_text("OUTPUT_SECRET=1", encoding="utf-8")
    (out_proj_dir / "oauth_token.json").write_text('{"token": "leak"}', encoding="utf-8")

    return {
        "slug": slug,
        "proj_dir": proj_dir,
        "chap_dir": chap_dir,
        "out_proj_dir": out_proj_dir,
    }


class TestSecurityWhitelistFilter:
    """Tests strict isolation and exclusion of secrets, tokens, credentials, and temp files."""

    def test_token_resolution(self, monkeypatch):
        monkeypatch.delenv("HF_TOKEN", raising=False)
        assert resolve_hf_token(None) == FALLBACK_HF_TOKEN

        monkeypatch.setenv("HF_TOKEN", "hf_custom_env_token_12345")
        assert resolve_hf_token(None) == "hf_custom_env_token_12345"
        assert resolve_hf_token("hf_explicit_override") == "hf_explicit_override"

    def test_secret_exclusion_filter_cases(self, tmp_path):
        root = tmp_path / "project"
        root.mkdir()
        out_root = tmp_path / "output"
        out_root.mkdir()

        forbidden = [
            Path(".env"),
            Path(".env.production"),
            Path("client_secrets.json"),
            Path("client_secret_123.json"),
            Path("credentials.json"),
            Path(".youtube_token.json"),
            Path("token.pickle"),
            Path("oauth_token.json"),
            Path("kaggle.json"),
            Path("kaggle_token.txt"),
            Path("id_rsa"),
            Path("secret.key"),
            Path("chapters/1/cache.tmp"),
            Path("chapters/1/process.lock"),
            Path("chapters/1/__pycache__/cache.pyc"),
            Path(".DS_Store"),
            Path("../escape.json"),
            Path("output/.env"),
            Path("output/oauth_token.json"),
            Path("output/secret_dir/chapter_001.mp4"),
        ]

        for p in forbidden:
            if not str(p).startswith(".."):
                base = out_root if p.parts[0] == "output" else root
                sub = Path(*p.parts[1:]) if p.parts[0] == "output" else p
                full = base / sub
                full.parent.mkdir(parents=True, exist_ok=True)
                full.write_bytes(b"SECRET")
            allowed, reason = is_whitelisted_archive_path(
                p, root, output_root=out_root if p.parts and p.parts[0] == "output" else None
            )
            assert not allowed, f"Security breach: {p} was allowed! Reason: {reason}"

    def test_whitelisted_files_pass_filter(self, tmp_path):
        root = tmp_path / "project"
        root.mkdir()
        out_root = tmp_path / "output"
        out_root.mkdir()

        whitelisted = [
            (Path("project.json"), "metadata"),
            (Path("characters.json"), "metadata"),
            (Path("story_summary.md"), "metadata"),
            (Path("series_setup_answers.json"), "metadata"),
            (Path("voice_selection.json"), "metadata"),
            (Path("preview_approval.json"), "metadata"),
            (Path("feature.youtube_receipt.json"), "youtube_receipt"),
            (Path("production_job.json"), "metadata"),
            (Path("release_state.json"), "metadata"),
            (Path("chapters/1/chapter.json"), "chapter_metadata"),
            (Path("chapters/1/script.json"), "script"),
            (Path("chapters/1/manifest.json"), "chapter_manifest"),
            (Path("chapters/1/visual_plan.json"), "visual_plan"),
            (Path("chapters/1/artifact_validation.json"), "artifact_validation"),
            (Path("chapters/1/thumbnail_selection.json"), "thumbnail_selection"),
            (Path("chapters/1/youtube_metadata.json"), "youtube_metadata"),
            (Path("chapters/1/youtube_receipt.json"), "youtube_receipt"),
            (Path("chapters/1/raw/001.jpg"), "raw_image"),
            (Path("chapters/1/panels/panel_001.png"), "panel_image"),
            (Path("chapters/1/contact_sheets/sheet_001.jpg"), "contact_sheet"),
            (Path("chapters/1/audio/final.wav"), "audio"),
            (Path("chapters/1/audio/mixed.m4a"), "audio"),
            (Path("chapters/1/audio/clips/001.wav"), "audio_clip"),
            (Path("chapters/1/video/final.mp4"), "video"),
            (Path("chapters/1/subtitles/chapter_1.srt"), "subtitles"),
            (Path("chapters/1/thumbnail.jpg"), "thumbnail"),
            (Path("output/chapter_001.mp4"), "output_video"),
            (Path("output/chapter_001.srt"), "output_subtitles"),
            (Path("output/chapter_001.youtube_receipt.json"), "youtube_receipt"),
            (Path("output/feature.youtube_receipt.json"), "youtube_receipt"),
            (Path("output/export_bundle/chapter_001.mp4"), "output_video"),
        ]

        for p, expected_role in whitelisted:
            is_out = p.parts[0] == "output"
            base = out_root if is_out else root
            sub = Path(*p.parts[1:]) if is_out else p
            full = base / sub
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_bytes(b"VALID_CONTENT")
            allowed, role = is_whitelisted_archive_path(p, root, output_root=out_root if is_out else None)
            assert allowed, f"Whitelisted file was rejected: {p} ({role})"
            assert role == expected_role

    def test_symlink_escape_rejection(self, tmp_path):
        root = tmp_path / "project"
        root.mkdir()
        outside_file = tmp_path / "outside_secret.txt"
        outside_file.write_text("OUTSIDE_LEAK", encoding="utf-8")

        symlink_path = root / "chapters" / "1" / "raw" / "leak.jpg"
        symlink_path.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(outside_file, symlink_path)
        allowed, _ = is_whitelisted_archive_path(symlink_path.relative_to(root), root)
        assert not allowed, "Symlink escape outside project root must be rejected"


class TestManifestGeneration:
    """Tests recursive SHA-256 manifest computation across project + output dirs."""

    def test_manifest_includes_project_and_output_and_filters_secrets(self, mock_archive_project):
        slug = mock_archive_project["slug"]
        proj_dir = mock_archive_project["proj_dir"]
        out_dir = mock_archive_project["out_proj_dir"]

        manifest = generate_archive_manifest(slug, project_dir=proj_dir, output_dir=out_dir)

        assert manifest["schema_version"] == "4.0.0"
        assert manifest["project_slug"] == slug
        assert manifest["repo_id"] == DEFAULT_HF_REPO_ID
        assert manifest["total_files"] >= 10
        assert manifest["excluded_files_count"] >= 7

        rel_paths = [f["relative_path"] for f in manifest["files"]]
        assert "project.json" in rel_paths
        assert "series_setup_answers.json" in rel_paths
        assert "voice_selection.json" in rel_paths
        assert "preview_approval.json" in rel_paths
        assert "chapters/1/script.json" in rel_paths
        assert "chapters/1/visual_plan.json" in rel_paths
        assert "chapters/1/video/final.mp4" in rel_paths
        assert "output/chapter_001.mp4" in rel_paths
        assert "output/chapter_001.srt" in rel_paths
        assert "output/chapter_001.youtube_receipt.json" in rel_paths

        for secret_name in (
            ".env",
            "client_secrets.json",
            ".youtube_token.json",
            "kaggle.json",
            "token.pickle",
            "output/.env",
            "output/oauth_token.json",
        ):
            assert secret_name not in rel_paths, f"CRITICAL LEAK: {secret_name} in manifest!"

        for item in manifest["files"]:
            rel = Path(item["relative_path"])
            file_on_disk = (out_dir / Path(*rel.parts[1:])) if rel.parts[0] == "output" else (proj_dir / rel)
            assert file_on_disk.exists()
            assert item["sha256"] == compute_file_sha256(file_on_disk)
            assert item["size_bytes"] == file_on_disk.stat().st_size
            assert item["repo_path"] == f"series/{slug}/{item['relative_path']}"


class TestArchivePushAndVerify:
    """Tests archive push, ArchiveReceipt validation, remote verify, and ReleaseState wiring."""

    def test_archive_push_rejects_non_40hex_commit_sha(self, mock_archive_project):
        slug = mock_archive_project["slug"]
        proj_dir = mock_archive_project["proj_dir"]

        mock_api = MagicMock()
        mock_api.repo_info.return_value.private = True
        mock_commit_info = MagicMock()
        mock_commit_info.oid = "main"
        mock_api.upload_folder.return_value = mock_commit_info
        mock_api.list_repo_commits.return_value = []

        with pytest.raises(CliValidationError, match="40-char hex"):
            archive_push(slug, project_dir=proj_dir, api_client=mock_api)

    def test_archive_push_emits_validated_receipt_and_advances_release_state(self, mock_archive_project):
        slug = mock_archive_project["slug"]
        proj_dir = mock_archive_project["proj_dir"]

        # Seed release_state at YOUTUBE_VERIFIED
        atomic_write_json(
            proj_dir / "release_state.json",
            {
                "schema_version": "4.1.0",
                "project_slug": slug,
                "chapter_number": "1",
                "state": "YOUTUBE_VERIFIED",
                "evidence_hashes": {"youtube_video_id": "yt_arch_vid_1"},
            },
        )

        mock_api = MagicMock()
        mock_api.repo_info.return_value.private = True
        mock_commit_info = MagicMock()
        mock_commit_info.oid = SHA40_A
        mock_api.upload_folder.return_value = mock_commit_info

        res = archive_push(slug, project_dir=proj_dir, api_client=mock_api)

        assert res["status"] == "success"
        assert res["commit_sha"] == SHA40_A
        receipt = ArchiveReceipt.model_validate(read_json(Path(res["receipt_path"])))
        assert receipt.status == "uploaded"
        assert receipt.commit_sha == SHA40_A
        assert load_release_record(proj_dir).state == ReleaseState.ARCHIVED

    def test_archive_verify_success_and_advances_release_state(self, mock_archive_project, tmp_path, monkeypatch):
        slug = mock_archive_project["slug"]
        proj_dir = mock_archive_project["proj_dir"]
        out_dir = mock_archive_project["out_proj_dir"]
        remote_store = tmp_path / "hf_remote_store"

        atomic_write_json(
            proj_dir / "release_state.json",
            {
                "schema_version": "4.1.0",
                "project_slug": slug,
                "chapter_number": "1",
                "state": "YOUTUBE_VERIFIED",
                "evidence_hashes": {"youtube_video_id": "yt_arch_vid_1"},
            },
        )

        mock_api = MagicMock()
        mock_api.repo_info.return_value.private = True
        mock_commit_info = MagicMock()
        mock_commit_info.oid = SHA40_A

        def capture_upload(folder_path, path_in_repo, repo_id, repo_type, commit_message):
            shutil.copytree(folder_path, remote_store / path_in_repo, dirs_exist_ok=True)
            return mock_commit_info

        mock_api.upload_folder.side_effect = capture_upload
        archive_push(slug, project_dir=proj_dir, output_dir=out_dir, api_client=mock_api)

        manifest = read_json(proj_dir / "archive_manifest.json")
        remote_tree = []
        for f in manifest["files"]:
            tree_item = MagicMock()
            tree_item.path = f["repo_path"]
            tree_item.size = f["size_bytes"]
            remote_tree.append(tree_item)
        mock_api.list_repo_tree.return_value = remote_tree

        def mock_download(repo_id, repo_type, filename, revision, token):
            return str(remote_store / filename)

        monkeypatch.setattr("src.archive.hf_archive.hf_hub_download", mock_download)

        v_res = archive_verify(slug, project_dir=proj_dir, api_client=mock_api)
        assert v_res["status"] == "success"
        assert v_res["verified"] is True
        assert v_res["files_verified"] == len(manifest["files"])
        assert load_release_record(proj_dir).state == ReleaseState.ARCHIVE_VERIFIED

    def test_archive_verify_detects_missing_remote_file(self, mock_archive_project):
        slug = mock_archive_project["slug"]
        proj_dir = mock_archive_project["proj_dir"]

        mock_api = MagicMock()
        mock_api.repo_info.return_value.private = True
        mock_commit_info = MagicMock()
        mock_commit_info.oid = SHA40_A
        mock_api.upload_folder.return_value = mock_commit_info
        archive_push(slug, project_dir=proj_dir, api_client=mock_api)

        mock_api.list_repo_tree.return_value = []
        with pytest.raises(CliValidationError, match="Remote verification failed"):
            archive_verify(slug, project_dir=proj_dir, api_client=mock_api)


class TestMediaHeaderVerification:
    """Tests binary header helper."""

    def test_valid_and_invalid_media_headers(self, tmp_path):
        mp4_valid = tmp_path / "valid.mp4"
        mp4_valid.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 20)
        ok, msg = verify_media_file_header(mp4_valid)
        assert ok and msg == "video/mp4"

        mp4_bad = tmp_path / "corrupt.mp4"
        mp4_bad.write_bytes(b"CORRUPTED_VIDEO_BYTES_WITHOUT_FTYP")
        ok, msg = verify_media_file_header(mp4_bad)
        assert not ok and "Invalid MP4 header" in msg

        wav_valid = tmp_path / "valid.wav"
        wav_valid.write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt ")
        ok, msg = verify_media_file_header(wav_valid)
        assert ok and msg == "audio/wav"


def _setup_verified_archive_and_snapshot(mock_archive_project, tmp_path, monkeypatch, mutator=None):
    slug = mock_archive_project["slug"]
    proj_dir = mock_archive_project["proj_dir"]
    out_dir = mock_archive_project["out_proj_dir"]
    remote_store = tmp_path / "hf_remote_store"

    atomic_write_json(
        proj_dir / "release_state.json",
        {
            "schema_version": "4.1.0",
            "project_slug": slug,
            "chapter_number": "1",
            "state": "YOUTUBE_VERIFIED",
            "evidence_hashes": {"youtube_video_id": "yt_arch_vid_1"},
        },
    )

    mock_api = MagicMock()
    mock_api.repo_info.return_value.private = True
    mock_commit_info = MagicMock()
    mock_commit_info.oid = SHA40_A

    def capture_upload(folder_path, path_in_repo, repo_id, repo_type, commit_message):
        shutil.copytree(folder_path, remote_store / path_in_repo, dirs_exist_ok=True)
        return mock_commit_info

    mock_api.upload_folder.side_effect = capture_upload
    archive_push(slug, project_dir=proj_dir, output_dir=out_dir, api_client=mock_api)

    manifest = read_json(proj_dir / "archive_manifest.json")
    remote_tree = []
    for f in manifest["files"]:
        tree_item = MagicMock()
        tree_item.path = f["repo_path"]
        tree_item.size = f["size_bytes"]
        remote_tree.append(tree_item)
    mock_api.list_repo_tree.return_value = remote_tree

    def remote_download(repo_id, repo_type, filename, revision, token):
        return str(remote_store / filename)

    monkeypatch.setattr("src.archive.hf_archive.hf_hub_download", remote_download)
    archive_verify(slug, project_dir=proj_dir, api_client=mock_api)

    def mock_snapshot(repo_id, repo_type, revision, allow_patterns, local_dir, token):
        dest = Path(local_dir) / "series" / slug
        shutil.copytree(remote_store / "series" / slug, dest, dirs_exist_ok=True)
        m_data = read_json(dest / "archive_manifest.json")
        if mutator is not None:
            mutator(dest, m_data)

    monkeypatch.setattr("src.archive.hf_archive.snapshot_download", mock_snapshot)
    return slug, proj_dir


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required for L2 media validation")
class TestArchiveRestoreRoundTrip:
    """Tests atomic empty-dir sandbox round-trip restoration, real media, and schema validation."""

    def test_archive_restore_success_with_output_and_release_state(self, mock_archive_project, tmp_path, monkeypatch):
        slug, proj_dir = _setup_verified_archive_and_snapshot(mock_archive_project, tmp_path, monkeypatch)

        sandbox_dir = tmp_path / "sandbox_restore"
        res = archive_restore(slug, target_dir=sandbox_dir, project_dir=proj_dir)

        assert res["status"] == "success"
        assert res["restored"] is True
        assert res["files_validated"] >= 10
        assert res["schemas_validated"] >= 9
        assert res["media_headers_validated"] >= 6
        assert (sandbox_dir / "chapters" / "1" / "script.json").exists()
        assert (sandbox_dir / "output" / "chapter_001.mp4").exists()

        updated_receipt = ArchiveReceipt.model_validate(read_json(proj_dir / "archive_receipt.json"))
        assert updated_receipt.restore_verified_at is not None
        assert updated_receipt.restored_revision == SHA40_A
        assert updated_receipt.restored_verified_files == res["files_validated"]
        assert load_release_record(proj_dir).state == ReleaseState.RESTORE_VERIFIED

    def test_archive_restore_fails_on_non_empty_target_dir(self, mock_archive_project, tmp_path, monkeypatch):
        slug, proj_dir = _setup_verified_archive_and_snapshot(mock_archive_project, tmp_path, monkeypatch)

        non_empty = tmp_path / "non_empty_restore"
        non_empty.mkdir()
        sentinel = non_empty / "existing_user_file.txt"
        sentinel.write_text("KEEP_ME", encoding="utf-8")

        with pytest.raises(CliValidationError, match="not empty"):
            archive_restore(slug, target_dir=non_empty, project_dir=proj_dir)
        assert sentinel.read_text(encoding="utf-8") == "KEEP_ME"

    def test_archive_restore_fails_on_extra_unlisted_file_without_touching_dest(self, mock_archive_project, tmp_path, monkeypatch):
        def add_extra(dest: Path, _manifest):
            (dest / "chapters" / "1" / "unexpected_extra.txt").write_text("EXTRA", encoding="utf-8")

        slug, proj_dir = _setup_verified_archive_and_snapshot(mock_archive_project, tmp_path, monkeypatch, mutator=add_extra)

        sandbox_dir = tmp_path / "restore_extra_fail"
        with pytest.raises(CliValidationError, match="Extra unlisted files"):
            archive_restore(slug, target_dir=sandbox_dir, project_dir=proj_dir)
        assert not sandbox_dir.exists()

    def test_archive_restore_fails_on_corrupt_media_ffprobe(self, mock_archive_project, tmp_path, monkeypatch):
        def corrupt_mp4(dest: Path, _manifest):
            bad = dest / "output" / "chapter_001.mp4"
            bad_bytes = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 500
            bad.write_bytes(bad_bytes)
            m_path = dest / "archive_manifest.json"
            m_data = read_json(m_path)
            for item in m_data["files"]:
                if item["relative_path"] == "output/chapter_001.mp4":
                    item["size_bytes"] = len(bad_bytes)
                    item["sha256"] = hashlib.sha256(bad_bytes).hexdigest()
            m_path.write_text(json.dumps(m_data), encoding="utf-8")

        slug, _proj_dir = _setup_verified_archive_and_snapshot(mock_archive_project, tmp_path, monkeypatch, mutator=corrupt_mp4)
        sandbox_dir = tmp_path / "restore_bad_media"
        cold_proj = tmp_path / "cold_proj_unused"
        with pytest.raises(CliValidationError, match="Media validation failed"):
            archive_restore(slug, revision=SHA40_A, target_dir=sandbox_dir, project_dir=cold_proj)
        assert not sandbox_dir.exists()

    def test_archive_restore_fails_on_corrupt_schema(self, mock_archive_project, tmp_path, monkeypatch):
        def corrupt_script(dest: Path, _manifest):
            script_dest = dest / "chapters" / "1" / "script.json"
            script_dest.write_text('{"invalid_schema": true}', encoding="utf-8")

        slug, proj_dir = _setup_verified_archive_and_snapshot(mock_archive_project, tmp_path, monkeypatch, mutator=corrupt_script)
        sandbox_dir = tmp_path / "restore_bad_schema"
        with pytest.raises(CliValidationError, match="Restore verification failed"):
            archive_restore(slug, target_dir=sandbox_dir, project_dir=proj_dir)
        assert not sandbox_dir.exists()

    def test_archive_restore_cold_requires_40hex_revision(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "empty_data"))
        with pytest.raises(CliValidationError, match="40-char hex"):
            archive_restore("nonexistent_series", revision="main", target_dir=tmp_path / "cold")


class TestArchiveCliInvocation:
    """Tests CLI entrypoint integration for `manga-recap archive`."""

    def test_archive_cli_help(self):
        code = main(["archive", "--help"])
        assert code == 0

    def test_archive_cli_missing_project(self):
        code = main(["archive", "push", "--json"])
        assert code == 2
