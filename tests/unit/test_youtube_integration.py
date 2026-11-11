"""
tests/unit/test_youtube_integration.py
Unit tests for YouTube metadata generation, packaging, timestamps, and upload command.
"""

from __future__ import annotations

from v4_fixtures import init_confirmed_project

import argparse
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.cli.youtube_cmd import (
    build_youtube_description,
    format_seconds_to_timestamp,
    generate_timestamps_from_script,
    handle_youtube,
    handle_youtube_metadata,
    handle_youtube_upload,
)
from src.core.schemas import YouTubeMetadata
from src.core.storage import (
    StorageFileNotFoundError,
    atomic_write_json,
    get_chapter_dir,
    get_output_dir,
    get_project_dir,
    read_json,
)


class TestYouTubeHelpers:
    """Tests for formatting and helper functions."""

    def test_format_seconds_to_timestamp(self):
        assert format_seconds_to_timestamp(0) == "00:00"
        assert format_seconds_to_timestamp(45) == "00:45"
        assert format_seconds_to_timestamp(65) == "01:05"
        assert format_seconds_to_timestamp(3600) == "01:00:00"
        assert format_seconds_to_timestamp(3665) == "01:01:05"
        assert format_seconds_to_timestamp(-10) == "00:00"

    def test_generate_timestamps_from_empty_script(self):
        script = {"segments": []}
        ts = generate_timestamps_from_script(script)
        assert len(ts) == 1
        assert ts[0]["time"] == "00:00"
        assert "Bắt đầu" in ts[0]["label"]

    def test_generate_timestamps_from_rich_script(self):
        script = {
            "segments": [
                {"segment_id": 1, "duration": 5.0, "narrator_text": "Trời đổ mưa to tại làng Lá"},
                {"segment_id": 2, "duration": 25.0, "narrator_text": "Naruto bước vào quán mì Ichiraku"},
                {"segment_id": 3, "duration": 30.0, "character": "Sasuke", "narrator_text": "Ngươi vẫn chậm chạp như mọi khi"},
                {"segment_id": 4, "duration": 10.0, "narrator_text": "Kết thúc cuộc đối đầu"},
            ]
        }
        ts = generate_timestamps_from_script(script)
        assert len(ts) >= 3
        assert ts[0]["time"] == "00:00"
        assert "Trời đổ mưa to" in ts[0]["label"]
        # Marker created after elapsed time
        assert any("Sasuke" in m["label"] for m in ts)

    def test_build_youtube_description(self):
        timestamps = [
            {"time": "00:00", "label": "Mở đầu"},
            {"time": "00:30", "label": "Hành động"},
        ]
        desc = build_youtube_description(
            project_title="Chiến Binh Già",
            chapter_num="1",
            story_summary="Tóm tắt nội dung về một lão binh tái xuất giang hồ.",
            timestamps=timestamps,
            custom_hook="Hook mở đầu cực sốc!",
        )
        assert "Hook mở đầu cực sốc!" in desc
        assert "00:00 - Mở đầu" in desc
        assert "00:30 - Hành động" in desc
        assert "Tóm tắt nội dung về một lão binh" in desc
        assert "Copyright Disclaimer Under Section 107" in desc
        assert "#MangaRecap" in desc


class TestYouTubeMetadataSchema:
    """Tests Pydantic schema validation for YouTubeMetadata."""

    def test_valid_schema(self):
        meta = YouTubeMetadata(
            project_slug="test-manga",
            chapter_number="1",
            title="[Review] Siêu Phẩm - Tập 1",
            description="Mô tả chi tiết",
            tags=["Manga", "Recap"],
            privacy_status="unlisted",
            timestamps=[{"time": "00:00", "label": "Intro"}],
        )
        assert meta.title == "[Review] Siêu Phẩm - Tập 1"
        assert meta.privacy_status == "unlisted"
        assert meta.category_id == "24"
        assert meta.made_for_kids is False

    def test_title_max_length(self):
        with pytest.raises(Exception):
            YouTubeMetadata(
                project_slug="test-manga",
                chapter_number="1",
                title="A" * 105,  # Exceeds max 100
                description="Desc",
            )


@pytest.fixture
def mock_project(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    projects_dir = data_dir / "projects"
    output_dir = tmp_path / "output"
    projects_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)

    monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(data_dir))
    monkeypatch.setenv("MANGA_RECAP_OUTPUT_DIR", str(output_dir))

    slug = "test_yt_proj"
    chap = "1"
    p_dir = projects_dir / slug
    c_dir = p_dir / "chapters" / chap
    c_dir.mkdir(parents=True)

    init_confirmed_project(slug, "Đại Lão Trùng Sinh")
    (p_dir / "story_summary.md").write_text("# Đại Lão Trùng Sinh\n\nNội dung chương 1 diễn biến ly kỳ.", encoding="utf-8")

    script_data = {
        "schema_version": "3.0.0",
        "project_slug": slug,
        "chapter_number": chap,
        "segments": [
            {"segment_id": 1, "duration": 10.0, "narrator_text": "Trời xanh mây trắng"},
            {"segment_id": 2, "duration": 20.0, "narrator_text": "Nhân vật xuất hiện"},
            {"segment_id": 3, "duration": 15.0, "narrator_text": "Giao đấu nảy lửa"},
        ],
    }
    atomic_write_json(c_dir / "script.json", script_data)

    return {"slug": slug, "chapter": chap, "projects_dir": projects_dir, "output_dir": output_dir, "chap_dir": c_dir}


class TestYouTubeCliCommands:
    """Tests CLI execution of youtube metadata and upload."""

    def test_handle_youtube_metadata(self, mock_project):
        args = argparse.Namespace(
            youtube_action="metadata",
            project=mock_project["slug"],
            chapter=mock_project["chapter"],
            title=None,
            description=None,
            tags="Anime,Manga,Top1",
            privacy="unlisted",
        )

        res = handle_youtube(args)
        assert res["status"] == "success"
        assert res["project_slug"] == mock_project["slug"]
        assert res["tags_count"] == 3

        # Verify saved file in chapter dir
        chap_meta_path = mock_project["chap_dir"] / "youtube_metadata.json"
        assert chap_meta_path.exists()
        saved_meta = read_json(chap_meta_path)
        assert saved_meta["privacy_status"] == "unlisted"
        assert "Đại Lão Trùng Sinh" in saved_meta["title"]
        assert len(saved_meta["timestamps"]) >= 3

        # Verify staged file in output dir with zero-padded naming
        staged_path = mock_project["output_dir"] / mock_project["slug"] / "chapter_001.youtube.json"
        assert staged_path.exists()
        staged_data = read_json(staged_path)
        assert staged_data["title"] == saved_meta["title"]

    def test_handle_youtube_upload_missing_video_raises(self, mock_project):
        args = argparse.Namespace(
            youtube_action="upload",
            project=mock_project["slug"],
            chapter=mock_project["chapter"],
            title=None,
            description=None,
            tags=None,
            privacy="private",
            thumbnail=None,
            no_captions=False,
        )

        with pytest.raises(StorageFileNotFoundError) as exc_info:
            handle_youtube_upload(args)
        assert "Video not found" in str(exc_info.value)

    def test_handle_youtube_upload_success_mock(self, mock_project, monkeypatch, tmp_path):
        slug = mock_project["slug"]
        chap = mock_project["chapter"]
        out_proj_dir = mock_project["output_dir"] / slug
        out_proj_dir.mkdir(parents=True, exist_ok=True)

        # Create dummy video and srt
        vid_path = out_proj_dir / "chapter_001.mp4"
        vid_path.write_bytes(b"dummy mp4 video bytes")
        srt_path = out_proj_dir / "chapter_001.srt"
        srt_path.write_text("1\n00:00:00,000 --> 00:00:05,000\nXin chào\n", encoding="utf-8")

        # Provide isolated credentials file and mock OAuth credentials
        token_path = tmp_path / ".test_token.json"
        token_path.write_text('{"token": "mock_tok"}', encoding="utf-8")
        monkeypatch.setenv("YOUTUBE_CREDENTIALS_FILE", str(token_path))
        mock_creds = MagicMock()
        monkeypatch.setattr("src.cli.youtube_cmd.get_youtube_oauth_credentials", lambda account_name=None: mock_creds)

        # Mock Google API client
        mock_youtube_service = MagicMock()
        mock_videos = MagicMock()
        mock_insert = MagicMock()
        mock_insert.next_chunk.return_value = (None, {"id": "yt_video_12345"})
        mock_videos.insert.return_value = mock_insert
        mock_youtube_service.videos.return_value = mock_videos

        # Mock captions
        mock_captions = MagicMock()
        mock_cap_insert = MagicMock()
        mock_captions.insert.return_value = mock_cap_insert
        mock_youtube_service.captions.return_value = mock_captions

        # Mock importlib inside handle_youtube_upload
        mock_discovery = MagicMock()
        mock_discovery.build.return_value = mock_youtube_service
        mock_http = MagicMock()

        original_import = __import__

        def custom_import(name, *args, **kwargs):
            if name == "googleapiclient.discovery":
                return mock_discovery
            if name == "googleapiclient.http":
                return mock_http
            return original_import(name, *args, **kwargs)

        import sys
        monkeypatch.setitem(sys.modules, "googleapiclient.discovery", mock_discovery)
        monkeypatch.setitem(sys.modules, "googleapiclient.http", mock_http)

        args = argparse.Namespace(
            youtube_action="upload",
            project=slug,
            chapter=chap,
            title="Upload Test Title",
            description="Upload Test Description",
            tags="Test,Tag",
            privacy="unlisted",
            thumbnail=None,
            no_captions=False,
        )

        res = handle_youtube_upload(args)
        assert res["status"] == "success"
        assert res["video_id"] == "yt_video_12345"
        assert res["video_url"] == "https://www.youtube.com/watch?v=yt_video_12345"
        assert res["captions_uploaded"] is True

    def test_handle_youtube_auth(self, monkeypatch, tmp_path):
        from src.cli.youtube_cmd import handle_youtube_auth, handle_youtube
        token_path = tmp_path / ".test_token.json"
        monkeypatch.setenv("YOUTUBE_CREDENTIALS_FILE", str(token_path))

        mock_creds = MagicMock()
        mock_creds.valid = True
        mock_creds.expiry = None
        monkeypatch.setattr("src.cli.youtube_cmd.get_youtube_oauth_credentials", lambda: mock_creds)

        args = argparse.Namespace(
            youtube_action="auth",
            force=False,
        )
        res = handle_youtube(args)
        assert res["status"] == "success"
        assert res["valid"] is True
        assert res["token_file"] == str(token_path)


class TestYouTubeMultiAccountManagement:
    """Tests for multi-channel YouTube account registry, credential management, and CLI operations."""

    @pytest.fixture
    def account_setup(self, tmp_path, monkeypatch):
        yt_dir = tmp_path / "yt_accounts"
        yt_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("YOUTUBE_ACCOUNTS_DIR", str(yt_dir))

        from src.youtube.account_manager import YouTubeAccountManager
        mgr = YouTubeAccountManager(storage_dir=yt_dir)
        return {"dir": yt_dir, "manager": mgr}

    def test_account_manager_crud_and_default(self, account_setup, tmp_path, monkeypatch):
        mgr = account_setup["manager"]
        dummy_token_1 = tmp_path / "token1.json"
        dummy_token_1.write_text('{"token": "t1", "refresh_token": "r1"}', encoding="utf-8")
        dummy_token_2 = tmp_path / "token2.json"
        dummy_token_2.write_text('{"token": "t2", "refresh_token": "r2"}', encoding="utf-8")

        # Mock channel probing
        monkeypatch.setattr(mgr, "fetch_channel_info", lambda creds: ("Kênh Võ Thuật", "UC_VO_THUAT"))

        # Mock credentials resolution to avoid real network call
        mock_creds = MagicMock()
        mock_creds.valid = True
        monkeypatch.setattr(mgr, "get_credentials", lambda account_name, force_reauth=False: (
            mock_creds,
            mgr.get_account(account_name)
        ))

        # 1. Add first account (auto-becomes default)
        acc1 = mgr.add_or_update_account(
            account_name="main_channel",
            token_file=dummy_token_1,
        )
        assert acc1.account_name == "main_channel"
        assert acc1.channel_title == "Kênh Võ Thuật"
        assert acc1.channel_id == "UC_VO_THUAT"

        reg = mgr.load_registry()
        assert reg.default_account == "main_channel"

        # 2. Add second account
        monkeypatch.setattr(mgr, "fetch_channel_info", lambda creds: ("Kênh Review Truyện", "UC_REVIEW"))
        acc2 = mgr.add_or_update_account(
            account_name="review_channel",
            token_file=dummy_token_2,
        )
        assert acc2.account_name == "review_channel"

        # List accounts
        acc_list = mgr.list_accounts()
        assert len(acc_list) == 2
        acc_names = [a["account_name"] for a in acc_list]
        assert "main_channel" in acc_names
        assert "review_channel" in acc_names

        # 3. Switch default account
        mgr.set_default_account("review_channel")
        assert mgr.load_registry().default_account == "review_channel"
        assert mgr.get_account().account_name == "review_channel"

        # 4. Remove account
        mgr.remove_account("main_channel")
        remaining = mgr.list_accounts()
        assert len(remaining) == 1
        assert remaining[0]["account_name"] == "review_channel"
        assert mgr.load_registry().default_account == "review_channel"

    def test_cli_accounts_dispatch(self, account_setup, tmp_path, monkeypatch):
        mgr = account_setup["manager"]
        dummy_tok = tmp_path / "tok.json"
        dummy_tok.write_text('{"token": "xyz"}', encoding="utf-8")

        mock_creds = MagicMock()
        mock_creds.valid = True

        from src.youtube.account_manager import account_manager as global_account_mgr
        monkeypatch.setattr("src.cli.youtube_cmd.account_manager", mgr)
        monkeypatch.setattr(mgr, "get_credentials", lambda account_name=None, force_reauth=False: (
            mock_creds,
            mgr.get_account(account_name)
        ))
        monkeypatch.setattr(mgr, "fetch_channel_info", lambda creds: ("Channel Alpha", "UC_ALPHA"))

        # Pre-seed one account
        mgr.add_or_update_account("alpha", token_file=dummy_tok)

        from src.cli.youtube_cmd import handle_youtube

        # CLI: list
        args_list = argparse.Namespace(
            youtube_action="accounts",
            account_action="list",
            account=None,
        )
        res = handle_youtube(args_list)
        assert res["status"] == "success"
        assert res["accounts_count"] >= 1
        assert res["accounts"][0]["account_name"] == "alpha"

        # CLI: default switch
        mgr.add_or_update_account("beta", token_file=dummy_tok)
        args_def = argparse.Namespace(
            youtube_action="accounts",
            account_action="default",
            account="beta",
        )
        res_def = handle_youtube(args_def)
        assert res_def["status"] == "success"
        assert res_def["default_account"] == "beta"

    def test_upload_with_selected_account(self, mock_project, monkeypatch, tmp_path):
        from src.cli.youtube_cmd import handle_youtube_upload
        from src.cli.exit_codes import CliValidationError

        slug = mock_project["slug"]
        chap = mock_project["chapter"]
        out_proj_dir = mock_project["output_dir"] / slug
        out_proj_dir.mkdir(parents=True, exist_ok=True)

        vid_path = out_proj_dir / "chapter_001.mp4"
        vid_path.write_bytes(b"dummy mp4 video bytes")

        # Mock OAuth credentials
        mock_creds = MagicMock()
        monkeypatch.setattr("src.cli.youtube_cmd.get_youtube_oauth_credentials", lambda account_name=None: mock_creds)

        # Mock Account Manager
        mock_acc_info = MagicMock()
        mock_acc_info.account_name = "kênh_review_vip"
        mock_acc_info.channel_title = "Review Truyện VIP"
        mock_acc_info.channel_id = "UC_VIP_999"

        from src.youtube.account_manager import account_manager
        monkeypatch.setattr(account_manager, "get_account", lambda name: mock_acc_info if name == "kênh_review_vip" else (_ for _ in ()).throw(CliValidationError(f"Account {name} not found")))

        # Mock Google API client
        mock_youtube_service = MagicMock()
        mock_videos = MagicMock()
        mock_insert = MagicMock()
        mock_insert.next_chunk.return_value = (None, {"id": "yt_video_multi_99"})
        mock_videos.insert.return_value = mock_insert
        mock_youtube_service.videos.return_value = mock_videos

        mock_discovery = MagicMock()
        mock_discovery.build.return_value = mock_youtube_service
        mock_http = MagicMock()

        import sys
        monkeypatch.setitem(sys.modules, "googleapiclient.discovery", mock_discovery)
        monkeypatch.setitem(sys.modules, "googleapiclient.http", mock_http)

        # 1. Upload with valid target account
        args = argparse.Namespace(
            youtube_action="upload",
            project=slug,
            chapter=chap,
            account="kênh_review_vip",
            title="Upload Multi Account Test",
            description="Test Desc",
            tags="Test",
            privacy="unlisted",
            thumbnail=None,
            no_captions=True,
        )

        res = handle_youtube_upload(args)
        assert res["status"] == "success"
        assert res["account"] == "kênh_review_vip"
        assert res["channel_title"] == "Review Truyện VIP"
        assert res["channel_id"] == "UC_VIP_999"
        assert res["video_id"] == "yt_video_multi_99"

        # 2. Upload with non-existent account raises CliValidationError
        args_bad = argparse.Namespace(
            youtube_action="upload",
            project=slug,
            chapter=chap,
            account="kenh_khong_ton_tai",
            title=None,
            description=None,
            tags=None,
            privacy="unlisted",
            thumbnail=None,
            no_captions=True,
        )
        with pytest.raises(CliValidationError) as exc:
            handle_youtube_upload(args_bad)
        assert "not found" in str(exc.value)

    def test_interactive_channel_selection(self, account_setup, monkeypatch):
        from src.cli.youtube_cmd import prompt_interactive_channel_selection
        from src.cli.exit_codes import CliValidationError

        mgr = account_setup["manager"]
        dummy_tok = account_setup["dir"] / "dummy.json"
        dummy_tok.write_text("{}", encoding="utf-8")

        mgr.add_or_update_account("chan1", token_file=dummy_tok)
        mgr.add_or_update_account("chan2", token_file=dummy_tok)

        # 1. Select option 2
        monkeypatch.setattr("builtins.input", lambda prompt="": "2")
        selected = prompt_interactive_channel_selection(mgr)
        assert selected == "chan2"

        # 2. Select default (empty input)
        monkeypatch.setattr("builtins.input", lambda prompt="": "")
        selected_default = prompt_interactive_channel_selection(mgr)
        assert selected_default == mgr.load_registry().default_account

        # 3. Select 0 (cancel)
        monkeypatch.setattr("builtins.input", lambda prompt="": "0")
        with pytest.raises(CliValidationError) as exc:
            prompt_interactive_channel_selection(mgr)
        assert "hủy" in str(exc.value).lower()

    def test_upload_interactive_mode(self, mock_project, monkeypatch):
        from src.cli.youtube_cmd import handle_youtube_upload
        slug = mock_project["slug"]
        chap = mock_project["chapter"]
        out_proj_dir = mock_project["output_dir"] / slug
        out_proj_dir.mkdir(parents=True, exist_ok=True)
        vid_path = out_proj_dir / "chapter_001.mp4"
        vid_path.write_bytes(b"dummy mp4 video bytes")

        # Mock interactive channel selection returning 'kênh_review_vip'
        monkeypatch.setattr("src.cli.youtube_cmd.prompt_interactive_channel_selection", lambda mgr: "kênh_review_vip")
        mock_creds = MagicMock()
        monkeypatch.setattr("src.cli.youtube_cmd.get_youtube_oauth_credentials", lambda account_name=None: mock_creds)

        mock_acc_info = MagicMock()
        mock_acc_info.account_name = "kênh_review_vip"
        mock_acc_info.channel_title = "Review Truyện VIP"
        mock_acc_info.channel_id = "UC_VIP_999"

        from src.youtube.account_manager import account_manager
        monkeypatch.setattr(account_manager, "get_account", lambda name: mock_acc_info)

        mock_youtube_service = MagicMock()
        mock_insert = MagicMock()
        mock_insert.next_chunk.return_value = (None, {"id": "yt_interactive_123"})
        mock_youtube_service.videos().insert.return_value = mock_insert

        mock_discovery = MagicMock()
        mock_discovery.build.return_value = mock_youtube_service
        mock_http = MagicMock()
        import sys
        monkeypatch.setitem(sys.modules, "googleapiclient.discovery", mock_discovery)
        monkeypatch.setitem(sys.modules, "googleapiclient.http", mock_http)

        args = argparse.Namespace(
            youtube_action="upload",
            project=slug,
            chapter=chap,
            account=None,
            interactive=True,
            title="Interactive Test",
            description="Test Desc",
            tags="Test",
            privacy="unlisted",
            thumbnail=None,
            no_captions=True,
        )
        res = handle_youtube_upload(args)
        assert res["status"] == "success"
        assert res["account"] == "kênh_review_vip"
        assert res["video_id"] == "yt_interactive_123"



