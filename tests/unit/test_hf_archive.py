"""Hugging Face archive filtering and lifecycle tests."""
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.archive.hf_archive import (
    FALLBACK_HF_TOKEN, archive_push, archive_verify, archive_restore, is_whitelisted_archive_path, resolve_hf_token,
)
from src.cli.exit_codes import CliValidationError


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
            (Path("chapters/1/chapter.json"), "chapter_metadata"),
            (Path("chapters/1/script.json"), "script"),
            (Path("chapters/1/manifest.json"), "chapter_manifest"),
            (Path("chapters/1/visual_plan.json"), "visual_plan"),
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




def test_archive_push_verify_restore(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / "project.json").write_text('{"slug":"series"}')
    output = tmp_path / "output"
    output.mkdir()
    (output / "chapter_001.mp4").write_bytes(b"test fixture")
    (project / ".env").write_text("DO_NOT_UPLOAD=true")
    remote = tmp_path / "dataset"
    revision = "a" * 40

    class FakeApi:
        def create_repo(self, **kwargs):
            assert kwargs["private"]

        def repo_info(self, **kwargs):
            return SimpleNamespace(private=True)

        def upload_folder(self, folder_path, path_in_repo, **kwargs):
            import shutil
            shutil.copytree(folder_path, remote / path_in_repo)
            return SimpleNamespace(oid=revision)

    monkeypatch.setattr("src.archive.hf_archive.hf_hub_download",
                        lambda filename, **kwargs: remote / filename)
    pushed = archive_push("series", project_dir=project, output_dir=output, api_client=FakeApi())
    assert pushed["files_uploaded"] == 2
    assert not (remote / "series" / "series" / ".env").exists()
    verified = archive_verify("series", project_dir=project)
    assert verified["verified_file_count"] == 2
    target = tmp_path / "restored"
    archive_restore("series", project_dir=project, target_dir=target)
    assert (target / "output" / "chapter_001.mp4").read_bytes() == b"test fixture"
    (remote / "series" / "series" / "project.json").write_text("{}")
    with pytest.raises(CliValidationError, match="integrity"):
        archive_restore("series", project_dir=project, target_dir=tmp_path / "damaged")
    assert not (tmp_path / "damaged").exists()
