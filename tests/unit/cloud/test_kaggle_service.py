"""
tests/unit/cloud/test_kaggle_service.py
Unit tests for KaggleCloudService and CLI cloud integration.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.cloud.kaggle_service import KaggleCloudService


def test_kaggle_service_initialization():
    service = KaggleCloudService(api_token="KGAT_mock_token_12345")
    assert service.api_token == "KGAT_mock_token_12345"


def test_package_chapter_payload(tmp_path, monkeypatch):
    proj_dir = tmp_path / "test_proj"
    chap_dir = proj_dir / "chapters" / "1"
    chap_dir.mkdir(parents=True)
    (proj_dir / "project.json").write_text("{}", encoding="utf-8")
    (chap_dir / "chapter.json").write_text("{}", encoding="utf-8")
    (chap_dir / "script.json").write_text('{"segments": []}', encoding="utf-8")
    (chap_dir / "panels").mkdir()
    (chap_dir / "panels" / "panel_001.jpg").write_bytes(b"dummy")

    monkeypatch.setattr("src.cloud.kaggle_service.get_project_dir", lambda p: proj_dir)
    monkeypatch.setattr("src.cloud.kaggle_service.get_chapter_dir", lambda p, c: chap_dir)

    service = KaggleCloudService(api_token="dummy")
    out_tar = tmp_path / "payload.tar.gz"
    res = service.package_chapter_payload("test_proj", "1", out_tar)

    assert res.exists()
    assert res.stat().st_size > 0


def test_generate_worker_script():
    service = KaggleCloudService(api_token="dummy")
    script_str = service.generate_worker_script("my_proj", "1", render_workers=4)
    assert "Manga Recap Extreme-Speed Cloud Production Worker" in script_str
    assert "my_proj" in script_str
    assert "Vieneu" in script_str
    assert "ThreadPoolExecutor" in script_str


def test_cli_tts_kaggle_dispatch(tmp_path, monkeypatch):
    import argparse
    from src.cli.tts_cmd import handle_tts

    proj_dir = tmp_path / "test_proj"
    chap_dir = proj_dir / "chapters" / "1"
    chap_dir.mkdir(parents=True)
    (proj_dir / "project.json").write_text("{}", encoding="utf-8")
    chap_meta = {
        "chapter_id": "test_proj_ch1",
        "chapter_number": "1",
        "project_slug": "test_proj",
        "title": "Chapter 1",
        "stages": {},
    }
    (chap_dir / "chapter.json").write_text(json.dumps(chap_meta), encoding="utf-8")
    (chap_dir / "script.json").write_text('{"segments": []}', encoding="utf-8")

    monkeypatch.setattr("src.cli.tts_cmd.get_project_dir", lambda p: proj_dir)
    monkeypatch.setattr("src.cli.tts_cmd.get_chapter_dir", lambda p, c: chap_dir)

    mock_service = MagicMock()
    mock_service.is_authenticated = True
    mock_service.dispatch_chapter_job.return_value = {
        "status": "success",
        "final_video": str(chap_dir / "video" / "final.mp4"),
        "subtitles": str(chap_dir / "subtitles" / "chapter_1.srt"),
        "telemetry": {"gpu": "Tesla T4", "tts_seconds": 20.63},
    }

    with patch("src.cloud.kaggle_service.KaggleCloudService", return_value=mock_service):
        args = argparse.Namespace(project="test_proj", chapter="1", kaggle=True)
        res = handle_tts(args)

    assert res["status"] == "success"
    assert res["provider"] == "kaggle"
    assert res["mode"] == "cloud_gpu"
    mock_service.dispatch_chapter_job.assert_called_once_with(
        project_slug="test_proj",
        chapter_number="1",
        gpu=True,
        wait=True,
    )


def test_cli_render_kaggle_dispatch(tmp_path, monkeypatch):
    import argparse
    from src.cli.render_cmd import handle_render

    proj_dir = tmp_path / "test_proj"
    chap_dir = proj_dir / "chapters" / "1"
    chap_dir.mkdir(parents=True)

    monkeypatch.setattr("src.cli.render_cmd.get_project_dir", lambda p: proj_dir)
    monkeypatch.setattr("src.cli.render_cmd.get_chapter_dir", lambda p, c: chap_dir)

    mock_service = MagicMock()
    mock_service.is_authenticated = True
    mock_service.dispatch_chapter_job.return_value = {
        "status": "success",
        "final_video": str(chap_dir / "video" / "final.mp4"),
        "subtitles": str(chap_dir / "subtitles" / "chapter_1.srt"),
        "telemetry": {"gpu": "Tesla T4", "render_seconds": 82.30},
    }

    with patch("src.cloud.kaggle_service.KaggleCloudService", return_value=mock_service):
        args = argparse.Namespace(project="test_proj", chapter="1", kaggle=True)
        res = handle_render(args)

    assert res["status"] == "success"
    assert res["provider"] == "kaggle"
    assert res["mode"] == "cloud_nvenc"
    mock_service.dispatch_chapter_job.assert_called_once_with(
        project_slug="test_proj",
        chapter_number="1",
        gpu=True,
        wait=True,
    )


def test_cli_cloud_subcommands():
    import argparse
    from src.cli.cloud_cmd import handle_cloud

    mock_service = MagicMock()
    mock_service.is_authenticated = True
    mock_service.get_username.return_value = "cuongnguyen1802"
    mock_service.dispatch_chapter_job.return_value = {"status": "success"}

    with patch("src.cli.cloud_cmd.KaggleCloudService", return_value=mock_service):
        # 1. info
        info_args = argparse.Namespace(cloud_action="info")
        info_res = handle_cloud(info_args)
        assert info_res["status"] == "success"
        assert info_res["data"]["username"] == "cuongnguyen1802"

        # 2. run
        run_args = argparse.Namespace(
            cloud_action="run",
            project="warrior-grandpa",
            chapter="11",
            no_gpu=False,
        )
        run_res = handle_cloud(run_args)
        assert run_res["status"] == "success"
        mock_service.dispatch_chapter_job.assert_called_once_with(
            project_slug="warrior-grandpa",
            chapter_number="11",
            gpu=True,
            wait=True,
        )


def test_cli_batch_kaggle_dispatch(tmp_path, monkeypatch):
    import argparse
    from src.cli.batch_cmd import handle_batch

    proj_dir = tmp_path / "test_proj"
    chap_dir = proj_dir / "chapters" / "1"
    chap_dir.mkdir(parents=True)
    (proj_dir / "project.json").write_text("{}", encoding="utf-8")
    chap_meta = {
        "chapter_id": "test_proj_ch1",
        "chapter_number": "1",
        "project_slug": "test_proj",
        "title": "Chapter 1",
        "stages": {},
    }
    (chap_dir / "chapter.json").write_text(json.dumps(chap_meta), encoding="utf-8")
    valid_script = {
        "title": "Ch 1",
        "summary": "Summary",
        "segments": [
            {
                "seq_id": "001",
                "panel_ids": ["panel_001.jpg"],
                "narrator_text": "Bối cảnh giang hồ dậy sóng khắp nơi.",
                "duration": 5.0,
            }
        ],
    }
    (chap_dir / "script.json").write_text(json.dumps(valid_script), encoding="utf-8")
    (chap_dir / "panels").mkdir()
    (chap_dir / "panels" / "panel_001.jpg").write_bytes(b"dummy")

    monkeypatch.setattr("src.cli.batch_cmd.get_project_dir", lambda p: proj_dir)
    monkeypatch.setattr("src.cli.batch_cmd.get_chapter_dir", lambda p, c: chap_dir)
    monkeypatch.setattr("src.cli.validate_cmd.handle_validate", lambda args: {"valid": True, "errors": []})

    mock_service = MagicMock()
    mock_service.is_authenticated = True
    mock_service.dispatch_chapter_job.return_value = {
        "status": "success",
        "final_video": str(chap_dir / "video" / "final.mp4"),
        "subtitles": str(chap_dir / "subtitles" / "chapter_1.srt"),
        "telemetry": {"gpu": "Tesla T4", "render_seconds": 82.30},
    }

    with patch("src.cloud.kaggle_service.KaggleCloudService", return_value=mock_service):
        args = argparse.Namespace(
            project="test_proj",
            chapters="1",
            retry_chapter=None,
            skip_preview=True,
            compile=False,
            force=False,
            kaggle=True,
        )
        res = handle_batch(args)

    assert res["success"] is True
    assert len(res["completed"]) == 1
    mock_service.dispatch_chapter_job.assert_called_once_with(
        project_slug="test_proj",
        chapter_number="1",
        gpu=True,
        wait=True,
    )
