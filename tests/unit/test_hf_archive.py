"""
tests/unit/test_hf_archive.py
Unit tests for Private Hugging Face Dataset Archive & Restoration Engine (Manga Recap V4).
Tests:
- Secret exclusion filter tests (.env, OAuth tokens, client_secrets, kaggle tokens).
- Recursive SHA-256 manifest calculation.
- Push and receipt emission logic.
- Remote verification logic with mocked HfApi.
- Round-trip sandbox restore verification (RecapScript schema, media headers).
"""

import hashlib
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

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
from src.core.schemas import RecapScript, ScriptSegment
from src.core.storage import atomic_write_json, init_chapter_layout, init_project_layout


@pytest.fixture
def mock_archive_project(tmp_path, monkeypatch):
    """Creates a sample project layout with valid assets and planted secrets."""
    data_dir = tmp_path / "data"
    output_dir = tmp_path / "output"
    monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(data_dir))
    monkeypatch.setenv("MANGA_RECAP_OUTPUT_DIR", str(output_dir))

    slug = "test_archive_series"
    proj_dir = init_project_layout(slug, "Test Archive Series")
    chap_dir = init_chapter_layout(slug, 1)

    # 1. Project metadata
    story_summary = proj_dir / "story_summary.md"
    story_summary.write_text("# Story Summary\nHào khí kiếm hiệp giang hồ.", encoding="utf-8")

    # 2. Valid RecapScript in chapter 1
    script_obj = RecapScript(
        project_slug=slug,
        chapter_number="1",
        title="Chapter 1 Opening",
        segments=[
            ScriptSegment(
                seq_id="001",
                panel_ids=["panel_001.jpg"],
                narrator_text="Tại vùng biên ải xa xôi, thiếu niên kiếm khách bắt đầu hành trình.",
            )
        ],
    )
    atomic_write_json(chap_dir / "script.json", script_obj)

    # 3. Chapter media assets with genuine headers
    raw_img = chap_dir / "raw" / "page_001.jpg"
    raw_img.parent.mkdir(parents=True, exist_ok=True)
    raw_img.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 200)

    panel_img = chap_dir / "panels" / "panel_001.jpg"
    panel_img.parent.mkdir(parents=True, exist_ok=True)
    panel_img.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 150)

    audio_clip = chap_dir / "audio" / "clips" / "audio_1_001.wav"
    audio_clip.parent.mkdir(parents=True, exist_ok=True)
    # Valid RIFF WAVE header: RIFF (4 bytes), size (4 bytes), WAVE (4 bytes)
    audio_clip.write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 50)

    video_final = chap_dir / "video" / "final.mp4"
    video_final.parent.mkdir(parents=True, exist_ok=True)
    # Valid MP4 header: 4-byte box size + b"ftypisom"
    video_final.write_bytes(b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2mp41" + b"\x00" * 1000)

    subtitles = chap_dir / "subtitles" / "chapter_1.srt"
    subtitles.parent.mkdir(parents=True, exist_ok=True)
    subtitles.write_text("1\n00:00:00,000 --> 00:00:05,000\nLời dẫn mở đầu.\n", encoding="utf-8")

    # 4. Planted forbidden secrets that MUST be filtered out
    (proj_dir / ".env").write_text("HF_TOKEN=secret_hf_key\nOPENAI_API_KEY=sk-dummy", encoding="utf-8")
    (proj_dir / ".env.production").write_text("SECRET=production_password", encoding="utf-8")
    (proj_dir / "client_secrets.json").write_text('{"installed": {"client_id": "secret_id"}}', encoding="utf-8")
    (proj_dir / ".youtube_token.json").write_text('{"token": "oauth2_refresh_token"}', encoding="utf-8")
    (proj_dir / "kaggle.json").write_text('{"username": "test", "key": "kaggle_key"}', encoding="utf-8")
    (proj_dir / "token.pickle").write_bytes(b"PICKLE_SECRET_TOKEN")
    (chap_dir / "temp_cache.tmp").write_bytes(b"TEMP_CACHE")
    (chap_dir / "run.lock").write_bytes(b"LOCK")

    return {
        "slug": slug,
        "proj_dir": proj_dir,
        "chap_dir": chap_dir,
        "output_dir": output_dir,
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

        # Forbidden secret files
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
        ]

        for p in forbidden:
            full = root / p
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_bytes(b"SECRET")
            allowed, reason = is_whitelisted_archive_path(p, root)
            assert not allowed, f"Security breach: {p} was allowed! Reason: {reason}"

    def test_whitelisted_files_pass_filter(self, tmp_path):
        root = tmp_path / "project"
        root.mkdir()

        whitelisted = [
            (Path("project.json"), "metadata"),
            (Path("characters.json"), "metadata"),
            (Path("story_summary.md"), "metadata"),
            (Path("series_setup_answers.json"), "metadata"),
            (Path("chapters/1/chapter.json"), "chapter_metadata"),
            (Path("chapters/1/script.json"), "script"),
            (Path("chapters/1/manifest.json"), "chapter_manifest"),
            (Path("chapters/1/youtube_metadata.json"), "youtube_metadata"),
            (Path("chapters/1/youtube_receipt.json"), "youtube_receipt"),
            (Path("chapters/1/raw/001.jpg"), "raw_image"),
            (Path("chapters/1/panels/panel_001.png"), "panel_image"),
            (Path("chapters/1/contact_sheets/sheet_001.jpg"), "contact_sheet"),
            (Path("chapters/1/audio/final.wav"), "audio"),
            (Path("chapters/1/audio/clips/001.wav"), "audio_clip"),
            (Path("chapters/1/video/final.mp4"), "video"),
            (Path("chapters/1/subtitles/chapter_1.srt"), "subtitles"),
            (Path("chapters/1/thumbnail.jpg"), "thumbnail"),
        ]

        for p, expected_role in whitelisted:
            full = root / p
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_bytes(b"VALID_CONTENT")
            allowed, role = is_whitelisted_archive_path(p, root)
            assert allowed, f"Whitelisted file was rejected: {p} ({role})"
            assert role == expected_role

    def test_symlink_escape_rejection(self, tmp_path):
        root = tmp_path / "project"
        root.mkdir()
        outside_file = tmp_path / "outside_secret.txt"
        outside_file.write_text("OUTSIDE_LEAK", encoding="utf-8")

        symlink_path = root / "chapters" / "1" / "leak_link.txt"
        symlink_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.symlink(outside_file, symlink_path)
            allowed, reason = is_whitelisted_archive_path(symlink_path.relative_to(root), root)
            assert not allowed, "Symlink escape outside project root must be rejected"
        except (OSError, NotImplementedError):
            pass


class TestManifestGeneration:
    """Tests recursive SHA-256 manifest computation and exclusion accuracy."""

    def test_manifest_calculation_and_secret_filtering(self, mock_archive_project):
        slug = mock_archive_project["slug"]
        proj_dir = mock_archive_project["proj_dir"]

        manifest = generate_archive_manifest(slug, project_dir=proj_dir)

        assert manifest["schema_version"] == "4.0.0"
        assert manifest["project_slug"] == slug
        assert manifest["repo_id"] == DEFAULT_HF_REPO_ID
        assert manifest["total_files"] >= 6
        assert manifest["excluded_files_count"] >= 5

        rel_paths = [f["relative_path"] for f in manifest["files"]]
        # Verify valid assets included
        assert "project.json" in rel_paths
        assert "story_summary.md" in rel_paths
        assert "chapters/1/script.json" in rel_paths
        assert "chapters/1/video/final.mp4" in rel_paths
        assert "chapters/1/audio/clips/audio_1_001.wav" in rel_paths

        # Verify NO secret files leaked into manifest
        for secret_name in (".env", "client_secrets.json", ".youtube_token.json", "kaggle.json", "token.pickle"):
            assert secret_name not in rel_paths, f"CRITICAL LEAK: {secret_name} found in archive manifest!"

        # Verify exact SHA-256
        for item in manifest["files"]:
            file_on_disk = proj_dir / item["relative_path"]
            assert file_on_disk.exists()
            assert item["sha256"] == compute_file_sha256(file_on_disk)
            assert item["size_bytes"] == file_on_disk.stat().st_size
            assert item["repo_path"] == f"series/{slug}/{item['relative_path']}"


class TestArchivePushAndVerify:
    """Tests archive push and remote verification with mocked HfApi."""

    def test_archive_push_emits_receipt(self, mock_archive_project):
        slug = mock_archive_project["slug"]
        proj_dir = mock_archive_project["proj_dir"]

        mock_api = MagicMock()
        mock_commit_info = MagicMock()
        mock_commit_info.oid = "a1b2c3d4e5f67890abcdef1234567890abcdef12"
        mock_commit_info.commit_url = "https://huggingface.co/datasets/Cuong2004/manga-recap-archive/commit/a1b2c3"
        mock_api.upload_folder.return_value = mock_commit_info

        res = archive_push(slug, project_dir=proj_dir, api_client=mock_api)

        assert res["status"] == "success"
        assert res["commit_sha"] == "a1b2c3d4e5f67890abcdef1234567890abcdef12"
        assert res["files_uploaded"] >= 6
        assert Path(res["receipt_path"]).exists()

        mock_api.create_repo.assert_called_once_with(
            repo_id=DEFAULT_HF_REPO_ID,
            repo_type="dataset",
            private=True,
            exist_ok=True,
        )

        receipt = json.loads(Path(res["receipt_path"]).read_text(encoding="utf-8"))
        assert receipt["schema_version"] == "4.0.0"
        assert receipt["receipt_type"] == "huggingface_archive"
        assert receipt["status"] == "verified"
        assert receipt["commit_sha"] == "a1b2c3d4e5f67890abcdef1234567890abcdef12"

    def test_archive_verify_success(self, mock_archive_project, monkeypatch):
        slug = mock_archive_project["slug"]
        proj_dir = mock_archive_project["proj_dir"]

        # First generate manifest and receipt
        mock_api = MagicMock()
        mock_commit_info = MagicMock()
        mock_commit_info.oid = "commithash12345"
        mock_api.upload_folder.return_value = mock_commit_info
        archive_push(slug, project_dir=proj_dir, api_client=mock_api)

        manifest = json.loads((proj_dir / "archive_manifest.json").read_text(encoding="utf-8"))

        # Build mock remote tree matching manifest
        remote_tree = []
        for f in manifest["files"]:
            tree_item = MagicMock()
            tree_item.path = f["repo_path"]
            tree_item.size = f["size_bytes"]
            remote_tree.append(tree_item)

        mock_api.list_repo_tree.return_value = remote_tree

        # Mock hf_hub_download to return local copy for sha256 check
        def mock_download(repo_id, repo_type, filename, revision, token):
            rel_in_proj = filename.replace(f"series/{slug}/", "")
            return str(proj_dir / rel_in_proj)

        monkeypatch.setattr("src.archive.hf_archive.hf_hub_download", mock_download)

        v_res = archive_verify(slug, project_dir=proj_dir, api_client=mock_api)
        assert v_res["status"] == "success"
        assert v_res["verified"] is True
        assert v_res["files_verified"] == len(manifest["files"])

    def test_archive_verify_detects_missing_remote_file(self, mock_archive_project):
        slug = mock_archive_project["slug"]
        proj_dir = mock_archive_project["proj_dir"]

        mock_api = MagicMock()
        mock_commit_info = MagicMock()
        mock_commit_info.oid = "commithash12345"
        mock_api.upload_folder.return_value = mock_commit_info
        archive_push(slug, project_dir=proj_dir, api_client=mock_api)

        # Return empty remote tree
        mock_api.list_repo_tree.return_value = []

        with pytest.raises(CliValidationError) as exc:
            archive_verify(slug, project_dir=proj_dir, api_client=mock_api)
        assert "Remote verification failed" in str(exc.value)


class TestMediaHeaderVerification:
    """Tests media header format validation (MP4, WAV, JPEG, PNG)."""

    def test_valid_and_invalid_media_headers(self, tmp_path):
        # Valid MP4
        mp4_valid = tmp_path / "valid.mp4"
        mp4_valid.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 20)
        ok, msg = verify_media_file_header(mp4_valid)
        assert ok
        assert msg == "video/mp4"

        # Corrupted MP4
        mp4_bad = tmp_path / "corrupt.mp4"
        mp4_bad.write_bytes(b"CORRUPTED_VIDEO_BYTES_WITHOUT_FTYP")
        ok, msg = verify_media_file_header(mp4_bad)
        assert not ok
        assert "Invalid MP4 header" in msg

        # Valid WAV
        wav_valid = tmp_path / "valid.wav"
        wav_valid.write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt ")
        ok, msg = verify_media_file_header(wav_valid)
        assert ok
        assert msg == "audio/wav"

        # Corrupted WAV
        wav_bad = tmp_path / "corrupt.wav"
        wav_bad.write_bytes(b"NOT_A_WAV_HEADER_DATA")
        ok, msg = verify_media_file_header(wav_bad)
        assert not ok
        assert "Invalid WAV header" in msg


class TestArchiveRestoreRoundTrip:
    """Tests sandbox round-trip restoration and schema validation."""

    def test_archive_restore_success(self, mock_archive_project, tmp_path, monkeypatch):
        slug = mock_archive_project["slug"]
        proj_dir = mock_archive_project["proj_dir"]

        # Push to generate manifest
        mock_api = MagicMock()
        mock_commit_info = MagicMock()
        mock_commit_info.oid = "commithash12345"
        mock_api.upload_folder.return_value = mock_commit_info
        archive_push(slug, project_dir=proj_dir, api_client=mock_api)

        # Mock snapshot_download by populating local_dir from proj_dir
        def mock_snapshot(repo_id, repo_type, revision, allow_patterns, local_dir, token):
            dest = Path(local_dir) / "series" / slug
            dest.mkdir(parents=True, exist_ok=True)
            manifest_p = proj_dir / "archive_manifest.json"
            manifest = json.loads(manifest_p.read_text(encoding="utf-8"))
            # Copy manifest
            (dest / "archive_manifest.json").write_text(manifest_p.read_text(encoding="utf-8"), encoding="utf-8")
            for item in manifest["files"]:
                src = proj_dir / item["relative_path"]
                dst = dest / item["relative_path"]
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(src.read_bytes())

        monkeypatch.setattr("src.archive.hf_archive.snapshot_download", mock_snapshot)

        sandbox_dir = tmp_path / "sandbox_restore"
        res = archive_restore(slug, target_dir=sandbox_dir, project_dir=proj_dir)

        assert res["status"] == "success"
        assert res["restored"] is True
        assert res["files_validated"] >= 6
        assert res["schemas_validated"] >= 1  # RecapScript validated
        assert res["media_headers_validated"] >= 2  # MP4 and WAV headers validated
        assert (sandbox_dir / "chapters" / "1" / "script.json").exists()

    def test_archive_restore_fails_on_corrupt_schema(self, mock_archive_project, tmp_path, monkeypatch):
        slug = mock_archive_project["slug"]
        proj_dir = mock_archive_project["proj_dir"]

        mock_api = MagicMock()
        mock_commit_info = MagicMock()
        mock_commit_info.oid = "commithash12345"
        mock_api.upload_folder.return_value = mock_commit_info
        archive_push(slug, project_dir=proj_dir, api_client=mock_api)

        # Mock snapshot_download corrupting script.json
        def mock_snapshot(repo_id, repo_type, revision, allow_patterns, local_dir, token):
            dest = Path(local_dir) / "series" / slug
            dest.mkdir(parents=True, exist_ok=True)
            manifest_p = proj_dir / "archive_manifest.json"
            (dest / "archive_manifest.json").write_text(manifest_p.read_text(encoding="utf-8"), encoding="utf-8")
            # Write invalid script schema
            script_dest = dest / "chapters" / "1" / "script.json"
            script_dest.parent.mkdir(parents=True, exist_ok=True)
            script_dest.write_text('{"invalid_schema": true}', encoding="utf-8")

        monkeypatch.setattr("src.archive.hf_archive.snapshot_download", mock_snapshot)

        sandbox_dir = tmp_path / "sandbox_restore_fail"
        with pytest.raises(CliValidationError) as exc:
            archive_restore(slug, target_dir=sandbox_dir, project_dir=proj_dir)
        assert "Restore verification failed" in str(exc.value)


class TestArchiveCliInvocation:
    """Tests CLI entrypoint integration for `manga-recap archive`."""

    def test_archive_cli_help(self):
        code = main(["archive", "--help"])
        assert code == 0

    def test_archive_cli_missing_project(self):
        # Missing required --project returns validation error exit code 2
        code = main(["archive", "push", "--json"])
        assert code == 2
