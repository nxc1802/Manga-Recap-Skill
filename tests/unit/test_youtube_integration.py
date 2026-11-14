"""
tests/unit/test_youtube_integration.py
Unit tests for YouTube metadata generation, packaging, timestamps, and upload command.
"""

from __future__ import annotations

from v4_fixtures import init_confirmed_project
from unit.test_series_setup import _make_valid_answers_dict

import argparse
import shutil
import sys
import wave
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest
from PIL import Image

from src.cli.exit_codes import CliValidationError
from src.cli.youtube_cmd import (
    build_youtube_description,
    format_seconds_to_timestamp,
    generate_timestamps_from_script,
    handle_youtube,
    handle_youtube_metadata,
    handle_youtube_upload,
    handle_youtube_verify,
)
from src.core.contracts import ReleaseState, YouTubeReceipt
from src.core.human_review import approve_preview, review_fingerprint
from src.core.schemas import PanelItem, PanelManifest, RecapScript, ScriptSegment, YouTubeMetadata
from src.core.series_setup import record_series_setup
from src.core.state_machine import advance_release_state, load_release_record
from src.core.storage import (
    StorageFileNotFoundError,
    atomic_write_json,
    calculate_sha256,
    get_chapter_dir,
    get_output_dir,
    get_project_dir,
    init_chapter_layout,
    init_project_layout,
    read_json,
)
from src.render.artifact_validation import fingerprint
from src.render.chapter_renderer import render_chapter


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


def _build_ready_youtube_project(
    tmp_path: Path,
    monkeypatch,
    *,
    slug: str = "yt_v4_proj",
    title: str = "Đại Lão Trùng Sinh",
    q07: bool = False,
    q08: Any = None,
) -> dict:
    """Create a real V4-compliant project advanced to ReleaseState.PRODUCTION_READY."""
    data_dir = tmp_path / "data"
    output_dir = tmp_path / "output"
    monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(data_dir))
    monkeypatch.setenv("MANGA_RECAP_OUTPUT_DIR", str(output_dir))

    init_project_layout(slug, title)
    init_chapter_layout(slug, "1")
    root = get_project_dir(slug)
    chap_dir = get_chapter_dir(slug, "1")
    out_proj_dir = get_output_dir(slug)
    out_proj_dir.mkdir(parents=True, exist_ok=True)

    answers = _make_valid_answers_dict()
    answers["Q02"]["user_answer"] = "local"
    answers["Q05"]["user_answer"] = False
    answers["Q07"]["user_answer"] = q07
    answers["Q08"]["user_answer"] = q08 if q08 is not None else {"account": "kênh_review_vip", "privacy": "unlisted"}
    answers["Q09"]["user_answer"] = "preview"
    answers["Q10"]["user_answer"] = False
    record_series_setup(slug, answers, project_dir=root)

    (root / "story_summary.md").write_text(f"# {title}\n\nNội dung chương 1 diễn biến ly kỳ.", encoding="utf-8")
    atomic_write_json(
        root / "voice_selection.json",
        {
            "project_slug": slug,
            "primary_voice": "Ngọc Huyền",
            "reviewer": "yt_test_reviewer",
            "status": "approved",
            "speed": 1.0,
        },
    )

    panel_path = chap_dir / "panels" / "p1.png"
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
        chap_dir / "manifest.json",
        PanelManifest(project_slug=slug, chapter_number="1", panels=[panel]),
    )

    audio_path = chap_dir / "audio" / "clips" / "seg_001.wav"
    with wave.open(str(audio_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(48000)
        wf.writeframes((np.sin(np.arange(24000) / 10) * 2000).astype("<i2").tobytes())

    script = RecapScript(
        project_slug=slug,
        chapter_number="1",
        title="Chapter 1",
        segments=[
            ScriptSegment(
                seq_id="001",
                evidence_panel_ids=["p1"],
                narrator_text="Trời xanh mây trắng, nhân vật chính xuất hiện.",
                duration=0.5,
                audio_path="audio/clips/seg_001.wav",
                checksum=calculate_sha256(audio_path),
            )
        ],
    )
    atomic_write_json(chap_dir / "script.json", script)

    if q07:
        thumb_path = chap_dir / "thumbnail.jpg"
        Image.new("RGB", (1280, 720), color=(210, 120, 40)).save(thumb_path, format="JPEG")
        shutil.copy2(thumb_path, out_proj_dir / "chapter_1_thumbnail.jpg")
        atomic_write_json(
            chap_dir / "thumbnail_selection.json",
            {
                "schema_version": "4.1.0",
                "selection_source": "explicit_cli_arguments",
                "panels": {"panels/p1.png": fingerprint(panel_path)},
                "title": "TEST",
                "subtitle": None,
                "badge": None,
                "has_text": True,
                "color_preset": "yellow_black",
                "font": None,
                "artifact_sha256": fingerprint(thumb_path),
            },
        )

    advance_release_state(root, ReleaseState.SETUP_VALID, chapter_number="1")
    advance_release_state(root, ReleaseState.SOURCE_READY, chapter_number="1")
    advance_release_state(root, ReleaseState.SCRIPT_READY, chapter_number="1")
    advance_release_state(root, ReleaseState.AUDIO_VALID, chapter_number="1")

    render_chapter(root, "1", quality="preview")
    advance_release_state(root, ReleaseState.VISUAL_PLAN_VALID, chapter_number="1")
    advance_release_state(root, ReleaseState.RENDER_VALID, chapter_number="1")
    advance_release_state(root, ReleaseState.TECHNICALLY_VALID, chapter_number="1")

    approve_preview(
        root,
        "qa_human",
        review_fingerprint(root),
        fingerprint(chap_dir / "video" / "preview.mp4"),
    )
    render_chapter(root, "1", quality="final")
    advance_release_state(root, ReleaseState.PRODUCTION_READY, chapter_number="1")

    return {
        "slug": slug,
        "chapter": "1",
        "project_dir": root,
        "chap_dir": chap_dir,
        "output_dir": output_dir,
        "out_proj_dir": out_proj_dir,
    }


def _install_mock_youtube(monkeypatch, *, account_name="kênh_review_vip", channel_title="Review Truyện VIP", channel_id="UC_VIP_999", video_id="yt_video_12345"):
    mock_creds = MagicMock()
    monkeypatch.setattr("src.cli.youtube_cmd.get_youtube_oauth_credentials", lambda account_name=None: mock_creds)

    mock_acc_info = MagicMock()
    mock_acc_info.account_name = account_name
    mock_acc_info.channel_title = channel_title
    mock_acc_info.channel_id = channel_id

    from src.youtube.account_manager import account_manager
    monkeypatch.setattr(
        account_manager,
        "get_account",
        lambda name=None: mock_acc_info if name == account_name else (_ for _ in ()).throw(CliValidationError(f"Account {name} not found")),
    )

    mock_youtube_service = MagicMock()
    mock_insert = MagicMock()
    mock_insert.next_chunk.return_value = (None, {"id": video_id})
    mock_youtube_service.videos.return_value.insert.return_value = mock_insert
    mock_youtube_service.captions.return_value.insert.return_value.execute.return_value = {"id": "cap_vi_1"}
    mock_youtube_service.thumbnails.return_value.set.return_value.execute.return_value = {"items": [{"default": {"url": "https://i.ytimg.com/vi/x/default.jpg"}}]}

    mock_discovery = MagicMock()
    mock_discovery.build.return_value = mock_youtube_service
    mock_http = MagicMock()
    monkeypatch.setitem(sys.modules, "googleapiclient.discovery", mock_discovery)
    monkeypatch.setitem(sys.modules, "googleapiclient.http", mock_http)
    return mock_youtube_service


class TestYouTubeCliCommands:
    """Tests CLI execution of youtube metadata, upload, verify, and auth."""

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

    def test_handle_youtube_upload_missing_video_raises(self, tmp_path, monkeypatch):
        proj = _build_ready_youtube_project(tmp_path, monkeypatch)
        _install_mock_youtube(monkeypatch)
        (proj["chap_dir"] / "video" / "final.mp4").unlink()

        args = argparse.Namespace(
            youtube_action="upload",
            project=proj["slug"],
            chapter=proj["chapter"],
            account="kênh_review_vip",
            title=None,
            description=None,
            tags=None,
            privacy="unlisted",
            thumbnail=None,
            no_captions=False,
        )

        with pytest.raises(StorageFileNotFoundError) as exc_info:
            handle_youtube_upload(args)
        assert "Video not found" in str(exc_info.value)

    def test_handle_youtube_upload_success_mock(self, tmp_path, monkeypatch):
        proj = _build_ready_youtube_project(tmp_path, monkeypatch)
        _install_mock_youtube(monkeypatch)

        args = argparse.Namespace(
            youtube_action="upload",
            project=proj["slug"],
            chapter=proj["chapter"],
            account="kênh_review_vip",
            title="Upload Test Title",
            description="Upload Test Description",
            tags="Test,Tag",
            privacy="unlisted",
            thumbnail=None,
            no_captions=False,
        )

        res = handle_youtube_upload(args)
        assert res["status"] == "success"
        assert res["verified"] is False
        assert res["video_id"] == "yt_video_12345"
        assert res["video_url"] == "https://www.youtube.com/watch?v=yt_video_12345"
        assert res["captions_uploaded"] is True
        assert res["video_sha256"] == calculate_sha256(proj["chap_dir"] / "video" / "final.mp4")
        assert res["subtitle_sha256"] == calculate_sha256(proj["chap_dir"] / "subtitles" / "final.srt")

        receipt_raw = read_json(Path(res["receipt_path"]))
        receipt = YouTubeReceipt.model_validate(receipt_raw)
        assert receipt.status == "uploaded"
        assert receipt.receipt_type == "youtube_upload"
        assert receipt.captions_required is True
        assert receipt.captions_uploaded is True
        assert load_release_record(proj["project_dir"]).state == ReleaseState.UPLOADED

    def test_handle_youtube_auth(self, monkeypatch, tmp_path):
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

        monkeypatch.setattr("src.cli.youtube_cmd.account_manager", mgr)
        monkeypatch.setattr(mgr, "get_credentials", lambda account_name=None, force_reauth=False: (
            mock_creds,
            mgr.get_account(account_name)
        ))
        monkeypatch.setattr(mgr, "fetch_channel_info", lambda creds: ("Channel Alpha", "UC_ALPHA"))

        # Pre-seed one account
        mgr.add_or_update_account("alpha", token_file=dummy_tok)

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

    def test_upload_with_selected_account(self, tmp_path, monkeypatch):
        proj = _build_ready_youtube_project(tmp_path, monkeypatch)
        _install_mock_youtube(monkeypatch, video_id="yt_video_multi_99")

        # 1. Upload with valid target account
        args = argparse.Namespace(
            youtube_action="upload",
            project=proj["slug"],
            chapter=proj["chapter"],
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
            project=proj["slug"],
            chapter=proj["chapter"],
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

    def test_upload_interactive_mode(self, tmp_path, monkeypatch):
        proj = _build_ready_youtube_project(tmp_path, monkeypatch)
        _install_mock_youtube(monkeypatch, video_id="yt_interactive_123")
        monkeypatch.setattr("src.cli.youtube_cmd.prompt_interactive_channel_selection", lambda mgr: "kênh_review_vip")

        args = argparse.Namespace(
            youtube_action="upload",
            project=proj["slug"],
            chapter=proj["chapter"],
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


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg/FFprobe required")
class TestYouTubeUploadV4Gates:
    """Tests Q08 enforcement, channel gate, readiness validation, feature upload, and fail-closed attachments."""

    def test_q08_no_upload_rejected(self, tmp_path, monkeypatch):
        proj = _build_ready_youtube_project(tmp_path, monkeypatch, q08={"no_upload": True})
        mock_yt = _install_mock_youtube(monkeypatch)

        args = argparse.Namespace(
            youtube_action="upload",
            project=proj["slug"],
            chapter="1",
            account="kênh_review_vip",
            privacy="unlisted",
            json=True,
        )
        with pytest.raises(CliValidationError, match="no_upload"):
            handle_youtube_upload(args)
        mock_yt.videos.return_value.insert.assert_not_called()

    def test_mandatory_channel_gate_rejects_missing_account_in_json_mode(self, tmp_path, monkeypatch):
        proj = _build_ready_youtube_project(tmp_path, monkeypatch)
        _install_mock_youtube(monkeypatch)

        args = argparse.Namespace(
            youtube_action="upload",
            project=proj["slug"],
            chapter="1",
            account=None,
            privacy="unlisted",
            json=True,
        )
        with pytest.raises(CliValidationError, match="Mandatory Channel Selection Gate"):
            handle_youtube_upload(args)

    def test_q08_account_and_privacy_mismatch_rejected_unless_reconfirmed(self, tmp_path, monkeypatch):
        proj = _build_ready_youtube_project(
            tmp_path,
            monkeypatch,
            q08={"account": "kênh_review_vip", "privacy": "unlisted"},
        )
        _install_mock_youtube(monkeypatch, account_name="other_channel")

        # Account differs from Q08 without reconfirmation
        with pytest.raises(CliValidationError, match="does not match Q08"):
            handle_youtube_upload(
                argparse.Namespace(
                    project=proj["slug"],
                    chapter="1",
                    account="other_channel",
                    privacy="unlisted",
                    json=True,
                )
            )

        # Privacy differs from Q08 without reconfirmation
        _install_mock_youtube(monkeypatch, account_name="kênh_review_vip")
        with pytest.raises(CliValidationError, match="does not match Q08"):
            handle_youtube_upload(
                argparse.Namespace(
                    project=proj["slug"],
                    chapter="1",
                    account="kênh_review_vip",
                    privacy="public",
                    json=True,
                )
            )

        # Explicitly reconfirmed gate allows override
        res = handle_youtube_upload(
            argparse.Namespace(
                project=proj["slug"],
                chapter="1",
                account="kênh_review_vip",
                privacy="public",
                gate_reconfirmed=True,
                json=True,
            )
        )
        assert res["privacy_status"] == "public"

    def test_readiness_rejects_missing_preview_approval_or_stale_state(self, tmp_path, monkeypatch):
        proj = _build_ready_youtube_project(tmp_path, monkeypatch)
        _install_mock_youtube(monkeypatch)

        (proj["project_dir"] / "preview_approval.json").unlink()
        with pytest.raises(CliValidationError, match="Human preview Gate G6"):
            handle_youtube_upload(
                argparse.Namespace(
                    project=proj["slug"],
                    chapter="1",
                    account="kênh_review_vip",
                    privacy="unlisted",
                    json=True,
                )
            )

    def test_captions_upload_failure_fails_closed(self, tmp_path, monkeypatch):
        proj = _build_ready_youtube_project(tmp_path, monkeypatch)
        mock_yt = _install_mock_youtube(monkeypatch)
        mock_yt.captions.return_value.insert.return_value.execute.side_effect = RuntimeError("quotaExceeded")

        with pytest.raises(CliValidationError, match="Failed to upload closed captions"):
            handle_youtube_upload(
                argparse.Namespace(
                    project=proj["slug"],
                    chapter="1",
                    account="kênh_review_vip",
                    privacy="unlisted",
                    no_captions=False,
                    json=True,
                )
            )
        assert not (proj["chap_dir"] / "youtube_receipt.json").exists()
        assert load_release_record(proj["project_dir"]).state == ReleaseState.PRODUCTION_READY

    def test_q07_thumbnail_failure_fails_closed_and_success_records_evidence(self, tmp_path, monkeypatch):
        proj = _build_ready_youtube_project(tmp_path, monkeypatch, q07=True)
        mock_yt = _install_mock_youtube(monkeypatch)

        # 1. Thumbnail upload exception fails closed (no receipt written)
        mock_yt.thumbnails.return_value.set.return_value.execute.side_effect = RuntimeError("thumb rejected")
        with pytest.raises(CliValidationError, match="Failed to upload custom thumbnail"):
            handle_youtube_upload(
                argparse.Namespace(
                    project=proj["slug"],
                    chapter="1",
                    account="kênh_review_vip",
                    privacy="unlisted",
                    json=True,
                )
            )
        assert not (proj["chap_dir"] / "youtube_receipt.json").exists()

        # 2. Successful thumbnail upload records exact sha256 + timestamp
        mock_yt.thumbnails.return_value.set.return_value.execute.side_effect = None
        mock_yt.thumbnails.return_value.set.return_value.execute.return_value = {"items": []}
        res = handle_youtube_upload(
            argparse.Namespace(
                project=proj["slug"],
                chapter="1",
                account="kênh_review_vip",
                privacy="unlisted",
                json=True,
            )
        )
        expected_thumb_sha = calculate_sha256(proj["chap_dir"] / "thumbnail.jpg")
        assert res["thumbnail_uploaded"] is True
        assert res["thumbnail_local_sha256"] == expected_thumb_sha
        assert res["thumbnail_upload_at"]

        ev = read_json(Path(res["evidence_path"]))
        assert ev["thumbnail_local_sha256"] == expected_thumb_sha
        assert ev["thumbnail_upload_at"] == res["thumbnail_upload_at"]
        assert ev["video_sha256"] == res["video_sha256"]
        assert ev["subtitle_sha256"] == res["subtitle_sha256"]

    def test_feature_upload_without_metadata_has_no_nameerror_or_hardcoded_titles(self, tmp_path, monkeypatch):
        proj = _build_ready_youtube_project(tmp_path, monkeypatch)
        _install_mock_youtube(monkeypatch, video_id="yt_feat_999")

        feat_vid = proj["out_proj_dir"] / f"{proj['slug']}_full_recap.mp4"
        feat_srt = proj["out_proj_dir"] / f"{proj['slug']}_full_recap.srt"
        shutil.copy2(proj["chap_dir"] / "video" / "final.mp4", feat_vid)
        shutil.copy2(proj["chap_dir"] / "subtitles" / "final.srt", feat_srt)
        assert not (proj["out_proj_dir"] / "feature.youtube.json").exists()

        res = handle_youtube_upload(
            argparse.Namespace(
                project=proj["slug"],
                chapter="feature",
                account="kênh_review_vip",
                privacy="unlisted",
                json=True,
            )
        )
        assert res["status"] == "success"
        assert res["video_id"] == "yt_feat_999"
        assert "Tập 1-10" not in res["title"]
        assert "Đại Lão Trùng Sinh" in res["title"]
        receipt = YouTubeReceipt.model_validate(read_json(Path(res["receipt_path"])))
        assert receipt.chapter_number == "feature"
        assert receipt.status == "uploaded"

    def test_upload_wait_flag_polls_and_advances_to_verified(self, tmp_path, monkeypatch):
        proj = _build_ready_youtube_project(tmp_path, monkeypatch, q07=True)
        mock_yt = _install_mock_youtube(monkeypatch, video_id="yt_wait_1")
        monkeypatch.setattr("time.sleep", lambda s: None)

        mock_yt.videos.return_value.list.return_value.execute.side_effect = [
            {"items": [{"status": {"uploadStatus": "uploaded"}, "snippet": {}}]},
            {
                "items": [
                    {
                        "status": {"uploadStatus": "processed", "privacyStatus": "unlisted"},
                        "snippet": {
                            "title": "Đại Lão Trùng Sinh",
                            "channelId": "UC_VIP_999",
                            "channelTitle": "Review Truyện VIP",
                            "thumbnails": {"maxres": {"url": "https://i.ytimg.com/vi/yt_wait_1/maxresdefault.jpg"}},
                        },
                    }
                ]
            },
        ]
        mock_yt.captions.return_value.list.return_value.execute.return_value = {
            "items": [{"snippet": {"language": "vi"}}]
        }

        res = handle_youtube_upload(
            argparse.Namespace(
                project=proj["slug"],
                chapter="1",
                account="kênh_review_vip",
                privacy="unlisted",
                wait=True,
                poll_interval=0.5,
                timeout=10.0,
                json=True,
            )
        )
        assert res["verified"] is True
        assert res["upload_status"] == "processed"
        assert res["poll_attempts"] == 2
        receipt = YouTubeReceipt.model_validate(read_json(Path(res["receipt_path"])))
        assert receipt.status == "verified"
        assert receipt.receipt_type == "youtube_verification"
        assert load_release_record(proj["project_dir"]).state == ReleaseState.YOUTUBE_VERIFIED


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg/FFprobe required")
class TestYouTubeVerify:
    """Tests bounded uploadStatus polling, channel/privacy/caption/thumbnail checks, and receipt lifecycle."""

    def _upload_chapter_one(self, tmp_path, monkeypatch, *, q07=True):
        proj = _build_ready_youtube_project(tmp_path, monkeypatch, q07=q07)
        mock_yt = _install_mock_youtube(monkeypatch, video_id="yt_ver_001")
        handle_youtube_upload(
            argparse.Namespace(
                project=proj["slug"],
                chapter="1",
                account="kênh_review_vip",
                privacy="unlisted",
                json=True,
            )
        )
        return proj, mock_yt

    def test_verify_polls_transient_statuses_to_processed(self, tmp_path, monkeypatch):
        proj, mock_yt = self._upload_chapter_one(tmp_path, monkeypatch, q07=True)
        sleeps = []
        monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))

        mock_yt.videos.return_value.list.return_value.execute.side_effect = [
            {"items": [{"status": {"uploadStatus": "uploaded"}, "snippet": {}}]},
            {"items": [{"status": {"uploadStatus": "processing"}, "snippet": {}}]},
            {
                "items": [
                    {
                        "status": {"uploadStatus": "processed", "privacyStatus": "unlisted"},
                        "snippet": {
                            "title": "Tập 1",
                            "channelId": "UC_VIP_999",
                            "channelTitle": "Review Truyện VIP",
                            "thumbnails": {"high": {"url": "https://i.ytimg.com/vi/yt_ver_001/hqdefault.jpg"}},
                        },
                    }
                ]
            },
        ]
        mock_yt.captions.return_value.list.return_value.execute.return_value = {
            "items": [{"snippet": {"language": "vi"}}]
        }

        res = handle_youtube_verify(
            argparse.Namespace(
                project=proj["slug"],
                chapter="1",
                poll_interval=1.0,
                timeout=10.0,
            )
        )
        assert res["verified"] is True
        assert res["upload_status"] == "processed"
        assert res["poll_attempts"] == 3
        assert sleeps == [1.0, 1.5]
        assert res["has_captions"] is True
        assert res["has_custom_thumbnail"] is True

        receipt = YouTubeReceipt.model_validate(read_json(proj["chap_dir"] / "youtube_receipt.json"))
        assert receipt.status == "verified"
        assert receipt.upload_status == "processed"
        assert (proj["out_proj_dir"] / "chapter_001.youtube_receipt.json").is_file()
        assert load_release_record(proj["project_dir"]).state == ReleaseState.YOUTUBE_VERIFIED

    def test_verify_timeout_rejected(self, tmp_path, monkeypatch):
        proj, mock_yt = self._upload_chapter_one(tmp_path, monkeypatch, q07=False)
        monkeypatch.setattr("time.sleep", lambda s: None)

        mock_yt.videos.return_value.list.return_value.execute.return_value = {
            "items": [{"status": {"uploadStatus": "uploaded"}, "snippet": {}}]
        }

        with pytest.raises(CliValidationError, match="Timed out"):
            handle_youtube_verify(
                argparse.Namespace(
                    project=proj["slug"],
                    chapter="1",
                    poll_interval=1.0,
                    timeout=2.5,
                )
            )
        assert read_json(proj["chap_dir"] / "youtube_receipt.json")["status"] == "uploaded"
        assert load_release_record(proj["project_dir"]).state == ReleaseState.UPLOADED

    @pytest.mark.parametrize("bad_status", ["rejected", "failed", "deleted"])
    def test_verify_terminal_failure_status_rejected(self, tmp_path, monkeypatch, bad_status):
        proj, mock_yt = self._upload_chapter_one(tmp_path, monkeypatch, q07=False)

        mock_yt.videos.return_value.list.return_value.execute.return_value = {
            "items": [{"status": {"uploadStatus": bad_status, "rejectionReason": "copyright"}, "snippet": {}}]
        }

        with pytest.raises(CliValidationError, match=bad_status):
            handle_youtube_verify(
                argparse.Namespace(project=proj["slug"], chapter="1", poll_interval=1.0, timeout=10.0)
            )
        assert mock_yt.videos.return_value.list.return_value.execute.call_count == 1

    def test_verify_wrong_channel_and_privacy_rejected(self, tmp_path, monkeypatch):
        proj, mock_yt = self._upload_chapter_one(tmp_path, monkeypatch, q07=False)

        mock_yt.videos.return_value.list.return_value.execute.return_value = {
            "items": [
                {
                    "status": {"uploadStatus": "processed", "privacyStatus": "unlisted"},
                    "snippet": {"channelId": "UC_WRONG_CHANNEL", "thumbnails": {}},
                }
            ]
        }
        with pytest.raises(CliValidationError, match="channel mismatch"):
            handle_youtube_verify(argparse.Namespace(project=proj["slug"], chapter="1"))

        mock_yt.videos.return_value.list.return_value.execute.return_value = {
            "items": [
                {
                    "status": {"uploadStatus": "processed", "privacyStatus": "public"},
                    "snippet": {"channelId": "UC_VIP_999", "thumbnails": {}},
                }
            ]
        }
        with pytest.raises(CliValidationError, match="privacyStatus mismatch"):
            handle_youtube_verify(argparse.Namespace(project=proj["slug"], chapter="1"))

    def test_verify_missing_captions_rejected(self, tmp_path, monkeypatch):
        proj, mock_yt = self._upload_chapter_one(tmp_path, monkeypatch, q07=False)

        mock_yt.videos.return_value.list.return_value.execute.return_value = {
            "items": [
                {
                    "status": {"uploadStatus": "processed", "privacyStatus": "unlisted"},
                    "snippet": {"channelId": "UC_VIP_999", "thumbnails": {}},
                }
            ]
        }
        mock_yt.captions.return_value.list.return_value.execute.return_value = {
            "items": [{"snippet": {"language": "en"}}]
        }
        with pytest.raises(CliValidationError, match="captions not verified"):
            handle_youtube_verify(argparse.Namespace(project=proj["slug"], chapter="1"))

    def test_verify_missing_thumbnail_evidence_rejected(self, tmp_path, monkeypatch):
        proj, mock_yt = self._upload_chapter_one(tmp_path, monkeypatch, q07=True)

        # Strip thumbnail_upload_at from both evidence and metadata sidecar
        ev_file = proj["chap_dir"] / "youtube_upload_evidence.json"
        ev_data = read_json(ev_file)
        ev_data.pop("thumbnail_upload_at", None)
        atomic_write_json(ev_file, ev_data)

        meta_file = proj["chap_dir"] / "youtube_metadata.json"
        meta_data = read_json(meta_file)
        meta_data.pop("thumbnail_upload_at", None)
        atomic_write_json(meta_file, meta_data)

        mock_yt.videos.return_value.list.return_value.execute.return_value = {
            "items": [
                {
                    "status": {"uploadStatus": "processed", "privacyStatus": "unlisted"},
                    "snippet": {
                        "channelId": "UC_VIP_999",
                        "thumbnails": {"default": {"url": "https://i.ytimg.com/vi/yt_ver_001/default.jpg"}},
                    },
                }
            ]
        }
        mock_yt.captions.return_value.list.return_value.execute.return_value = {
            "items": [{"snippet": {"language": "vi"}}]
        }

        with pytest.raises(CliValidationError, match="thumbnail upload evidence"):
            handle_youtube_verify(argparse.Namespace(project=proj["slug"], chapter="1"))

    def test_verify_missing_receipt_and_q08_no_upload_rejected(self, tmp_path, monkeypatch):
        proj = _build_ready_youtube_project(tmp_path, monkeypatch, q07=False)
        _install_mock_youtube(monkeypatch)

        with pytest.raises(CliValidationError, match="No YouTube upload receipt"):
            handle_youtube_verify(argparse.Namespace(project=proj["slug"], chapter="1"))

        proj_no_up = _build_ready_youtube_project(
            tmp_path / "no_up",
            monkeypatch,
            slug="no_up_proj",
            q08={"no_upload": True},
        )
        with pytest.raises(CliValidationError, match="no_upload"):
            handle_youtube_verify(argparse.Namespace(project=proj_no_up["slug"], chapter="1"))

    def test_thumbnail_cmd_has_no_dead_get_authenticated_service(self):
        thumb_src = Path("src/cli/thumbnail_cmd.py").read_text(encoding="utf-8")
        assert "get_authenticated_service" not in thumb_src



