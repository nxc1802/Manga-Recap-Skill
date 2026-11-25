"""Focused provider boundary tests for Hugging Face Archive and YouTube Data API publishing."""

import argparse
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.archive.hf_archive import archive_push, archive_restore, archive_verify
from src.core.cleanup import clean_project_chapters
from src.core.errors import DomainValidationError, ProviderIntegrityError
from src.core.storage import read_json
from src.providers.huggingface import HuggingFaceClient
from src.youtube.account_manager import YouTubeAccountInfo
from tests.unit.test_youtube_integration import _build_ready_youtube_project


def test_partial_youtube_upload_persists_video_id_and_resumes_without_duplicate_insert(
    tmp_path, monkeypatch
):
    proj = _build_ready_youtube_project(tmp_path, monkeypatch, q07=True)
    mock_creds = MagicMock(valid=True)
    monkeypatch.setattr("src.cli.youtube_cmd.get_youtube_oauth_credentials", lambda **kwargs: mock_creds)

    fake_acc = YouTubeAccountInfo(
        account_name="kênh_review_vip",
        channel_title="Review VIP",
        channel_id="UC_VIP_999",
        token_file=str(tmp_path / "tok.json"),
    )
    fake_mgr = MagicMock()
    fake_mgr.get_account.return_value = fake_acc
    monkeypatch.setattr("src.cli.youtube_cmd.account_manager", fake_mgr)

    mock_yt = MagicMock()
    mock_yt.videos.return_value.insert.return_value.next_chunk.return_value = (
        None,
        {"id": "yt_partial_777"},
    )
    mock_yt.captions.return_value.insert.return_value.execute.side_effect = [
        RuntimeError("transient 503"),
        {"id": "cap_ok"},
    ]
    mock_yt.thumbnails.return_value.set.return_value.execute.side_effect = [
        RuntimeError("thumbnail backend blip"),
        {"items": []},
    ]

    mock_discovery = SimpleNamespace(build=MagicMock(return_value=mock_yt))
    mock_http = SimpleNamespace(MediaFileUpload=MagicMock(side_effect=lambda *a, **kw: MagicMock()))
    monkeypatch.setitem(sys.modules, "googleapiclient.discovery", mock_discovery)
    monkeypatch.setitem(sys.modules, "googleapiclient.http", mock_http)

    from src.cli.youtube_cmd import handle_youtube_upload

    base_args = argparse.Namespace(
        project=proj["slug"],
        chapter="1",
        account="kênh_review_vip",
        privacy="unlisted",
        no_captions=False,
        json=True,
    )

    # Attempt 1: videos().insert succeeds, captions().insert fails -> evidence records video_id immediately
    with pytest.raises(DomainValidationError, match="Failed to upload closed captions"):
        handle_youtube_upload(base_args)

    ev_path = proj["chap_dir"] / "youtube_upload_evidence.json"
    assert ev_path.is_file()
    assert not (proj["chap_dir"] / "youtube_receipt.json").exists()
    ev1 = read_json(ev_path)
    assert ev1["video_id"] == "yt_partial_777"
    assert ev1["partial_upload"] is True
    assert ev1["captions_uploaded"] is False
    assert mock_yt.videos.return_value.insert.call_count == 1

    # Attempt 2: resumes same video_id WITHOUT duplicate videos().insert; captions succeed, thumbnail fails
    with pytest.raises(DomainValidationError, match="Failed to upload custom thumbnail"):
        handle_youtube_upload(base_args)

    assert mock_yt.videos.return_value.insert.call_count == 1
    assert mock_yt.captions.return_value.insert.call_count == 2
    ev2 = read_json(ev_path)
    assert ev2["captions_uploaded"] is True
    assert ev2["thumbnail_uploaded"] is False
    assert ev2["partial_upload"] is True

    # Attempt 3: resumes thumbnail ONLY without repeating video insert or caption upload
    res = handle_youtube_upload(base_args)
    assert mock_yt.videos.return_value.insert.call_count == 1
    assert mock_yt.captions.return_value.insert.call_count == 2
    assert mock_yt.thumbnails.return_value.set.call_count == 2
    assert res["video_id"] == "yt_partial_777"
    assert res["captions_uploaded"] is True
    assert res["thumbnail_uploaded"] is True
    assert (proj["chap_dir"] / "youtube_receipt.json").is_file()
    ev3 = read_json(ev_path)
    assert ev3["partial_upload"] is False


def test_huggingface_provider_rejects_public_repo_bad_commit_revision_and_unverified_cleanup(
    tmp_path, monkeypatch
):
    public_api = MagicMock()
    public_api.repo_info.return_value = SimpleNamespace(private=False)
    with pytest.raises(DomainValidationError, match="must be private"):
        HuggingFaceClient(api_client=public_api).ensure_private_repo("user/public-repo")

    bad_commit_api = MagicMock()
    bad_commit_api.upload_folder.return_value = SimpleNamespace(oid="not-a-sha")
    with pytest.raises(ProviderIntegrityError, match="40-character commit SHA"):
        HuggingFaceClient(api_client=bad_commit_api).upload_folder(
            folder_path=tmp_path,
            path_in_repo="series/x",
            repo_id="user/priv",
            commit_message="test",
        )

    # Archive verify rejects wrong revision and raw cleanup denies unverified upload
    monkeypatch.setenv("DATA_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("DATA_OUTPUT_DIR", str(tmp_path / "output"))
    proj_dir = tmp_path / "projects" / "hf-series"
    chap_dir = proj_dir / "chapters" / "1"
    (chap_dir / "raw").mkdir(parents=True)
    (chap_dir / "video").mkdir(parents=True)
    (chap_dir / "subtitles").mkdir(parents=True)
    (proj_dir / "project.json").write_text('{"slug":"hf-series"}', encoding="utf-8")
    (chap_dir / "chapter.json").write_text(
        '{"schema_version":"3.0.0","chapter_id":"ch1","chapter_number":"1","overall_status":"completed","stages":{}}',
        encoding="utf-8",
    )
    (chap_dir / "raw" / "001.png").write_bytes(b"raw")
    (chap_dir / "video" / "final.mp4").write_bytes(b"mp4")
    (chap_dir / "subtitles" / "final.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nx\n", encoding="utf-8")
    out_dir = tmp_path / "output" / "hf-series"
    out_dir.mkdir(parents=True)

    rev_a = "a" * 40
    rev_b = "b" * 40

    class _FakeHfApi:
        def create_repo(self, **kwargs):
            return None

        def repo_info(self, **kwargs):
            return SimpleNamespace(private=True)

        def upload_folder(self, **kwargs):
            return SimpleNamespace(oid=rev_a)

    pushed = archive_push("hf-series", project_dir=proj_dir, output_dir=out_dir, api_client=_FakeHfApi())
    assert pushed["commit_sha"] == rev_a

    with pytest.raises(DomainValidationError, match="must match the upload receipt"):
        archive_verify("hf-series", revision=rev_b, project_dir=proj_dir)

    # Restore refuses non-empty directory unless allow_non_empty=True
    existing_target = tmp_path / "nonempty"
    existing_target.mkdir()
    (existing_target / "keep.txt").write_text("existing", encoding="utf-8")
    with pytest.raises(DomainValidationError, match="empty --target-dir"):
        archive_restore("hf-series", revision=rev_a, project_dir=proj_dir, target_dir=existing_target)

    # Cleanup --purge-raw refuses when archive receipt is only 'uploaded' (not 'verified')
    with pytest.raises(DomainValidationError):
        clean_project_chapters(
            "hf-series",
            chapter="1",
            dry_run=False,
            confirm_project="hf-series",
            purge_raw=True,
        )
