"""
tests/unit/test_youtube_integration.py
Unit tests for YouTube metadata generation, packaging, timestamps, and upload command.
"""

from __future__ import annotations

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


class TestYouTubeCliCommands:
    """Tests CLI execution of youtube metadata and upload."""

    @pytest.fixture
    def mock_project(self, tmp_path, monkeypatch):
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

        atomic_write_json(p_dir / "project.json", {"project_slug": slug, "title": "Đại Lão Trùng Sinh"})
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

    def test_handle_youtube_upload_success_mock(self, mock_project, monkeypatch):
        slug = mock_project["slug"]
        chap = mock_project["chapter"]
        out_proj_dir = mock_project["output_dir"] / slug
        out_proj_dir.mkdir(parents=True, exist_ok=True)

        # Create dummy video and srt
        vid_path = out_proj_dir / "chapter_001.mp4"
        vid_path.write_bytes(b"dummy mp4 video bytes")
        srt_path = out_proj_dir / "chapter_001.srt"
        srt_path.write_text("1\n00:00:00,000 --> 00:00:05,000\nXin chào\n", encoding="utf-8")

        # Mock OAuth credentials
        mock_creds = MagicMock()
        monkeypatch.setattr("src.cli.youtube_cmd.get_youtube_oauth_credentials", lambda: mock_creds)

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
        sys.modules["googleapiclient.discovery"] = mock_discovery
        sys.modules["googleapiclient.http"] = mock_http

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

