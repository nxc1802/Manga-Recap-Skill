"""
tests/unit/test_phase_b.py
Comprehensive Unit Test Suite for Phase B:
- PR-04: Universal / Agentic Manga Crawler
- PR-05: YouTube Chapters Timeline
- PR-06: Thumbnail Director
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from src.cli.compile_cmd import handle_compile
from src.cli.crawl_cmd import handle_crawl
from src.cli.exit_codes import CliValidationError
from src.cli.main import build_parser, execute_subcommand
from src.cli.youtube_cmd import (
    build_youtube_chapter_markers,
    build_youtube_description,
    format_seconds_to_timestamp,
    handle_youtube_metadata,
)
from src.core.schemas import (
    ChapterMarker,
    ChapterTimelineItem,
    FeatureTimeline,
    YouTubeMetadata,
)
from src.core.storage import atomic_write_json, get_output_dir, get_project_dir, init_chapter_layout, init_project_layout, read_json

from src.crawler.errors import CrawlerError, CrawlerErrorCode
from src.crawler.extractor import ChapterExtractor
from src.crawler.registry import SiteAdapterRegistry
from src.crawler.resolver import SeriesResolver
from src.crawler.schemas import (
    ChapterExtractorConfig,
    SeriesResolverConfig,
    SourceProfile,
    URLClassification,
)
from src.crawler.verifier import ImageVerificationReport, ImageVerifier


# ==============================================================================
# 1. PR-04: SeriesResolver Tests
# ==============================================================================

class TestSeriesResolver:
    def setup_method(self):
        self.resolver = SeriesResolver()

    def test_classify_url_types(self):
        # Direct images
        assert self.resolver.classify_url("https://example.com/comics/page1.jpg") == URLClassification.DIRECT_IMAGE
        assert self.resolver.classify_url("https://example.com/img/002.PNG") == URLClassification.DIRECT_IMAGE
        assert self.resolver.classify_url("https://example.com/asset.webp") == URLClassification.DIRECT_IMAGE

        # Chapter reader URLs
        assert self.resolver.classify_url("https://www.webtoons.com/en/fantasy/tower/episode-1/viewer?title_no=95&episode_no=1") == URLClassification.CHAPTER
        assert self.resolver.classify_url("https://truyenqq.com/truyen-tranh/dai-quan-gia-chap-100.html") == URLClassification.CHAPTER
        assert self.resolver.classify_url("https://example.com/manga/title/chapter-5") == URLClassification.CHAPTER

        # Series landing page URLs
        assert self.resolver.classify_url("https://truyenqq.com/truyen-tranh/lam-nong-dan-trong-toa-thap-14069") == URLClassification.SERIES
        assert self.resolver.classify_url("https://example.com/title/solo-leveling") == URLClassification.SERIES

        # Fallback unknown
        assert self.resolver.classify_url("https://example.com/about-us") == URLClassification.UNKNOWN

    def test_resolve_chapters_from_html(self):
        html = """
        <html>
            <body>
                <div class="works-chapter-list">
                    <a href="/truyen/dai-quan-gia/chap-3.html">Tập 3: Đại Chiến</a>
                    <a href="/truyen/dai-quan-gia/chap-1.html">Tập 1: Xuất Thế</a>
                    <a href="/truyen/dai-quan-gia/chap-2.html">Tập 2: Đột Phá</a>
                </div>
            </body>
        </html>
        """
        chapters = self.resolver.resolve_chapters(html, "https://truyenqq.com/truyen-tranh/dai-quan-gia")
        assert len(chapters) == 3
        # Should be sorted ascending by chapter number
        assert chapters[0]["chapter_number"] == "1"
        assert chapters[0]["url"] == "https://truyenqq.com/truyen/dai-quan-gia/chap-1.html"
        assert chapters[1]["chapter_number"] == "2"
        assert chapters[2]["chapter_number"] == "3"

    def test_resolve_chapters_empty_raises_error(self):
        html = "<html><body><div>Không có chương nào</div></body></html>"
        with pytest.raises(CrawlerError) as exc_info:
            self.resolver.resolve_chapters(html, "https://example.com/series/empty")
        assert exc_info.value.code == CrawlerErrorCode.NO_CHAPTERS_FOUND

    def test_get_chapter_url_matching(self):
        chapters = [
            {"chapter_number": "1", "title": "Tập 1", "url": "https://example.com/ch-1"},
            {"chapter_number": "2", "title": "Tập 2", "url": "https://example.com/ch-2"},
            {"chapter_number": "10.5", "title": "Tập 10.5", "url": "https://example.com/ch-10-5"},
        ]
        # Match string, int, 0-padded
        assert self.resolver.get_chapter_url(chapters, "1") == "https://example.com/ch-1"
        assert self.resolver.get_chapter_url(chapters, 1) == "https://example.com/ch-1"
        assert self.resolver.get_chapter_url(chapters, "01") == "https://example.com/ch-1"
        assert self.resolver.get_chapter_url(chapters, 2) == "https://example.com/ch-2"
        assert self.resolver.get_chapter_url(chapters, "10.5") == "https://example.com/ch-10-5"
        assert self.resolver.get_chapter_url(chapters, "99") is None


# ==============================================================================
# 2. PR-04: ChapterExtractor Tests
# ==============================================================================

class TestChapterExtractor:
    def setup_method(self):
        self.extractor = ChapterExtractor()

    def test_extract_webtoons_script_regex(self):
        html = """
        <html>
            <script>
                var imageList = [
                    {url: "https://webtoon-phinf.pstatic.net/img_001.jpg"},
                    {url: "https://webtoon-phinf.pstatic.net/img_002.jpg"},
                    {url: "https://webtoon-phinf.pstatic.net/banner_ad.jpg"}
                ];
            </script>
        </html>
        """
        urls = self.extractor.extract_image_urls(html, "https://www.webtoons.com/viewer")
        assert len(urls) == 2
        assert "img_001.jpg" in urls[0]
        assert "img_002.jpg" in urls[1]
        assert not any("banner" in u for u in urls)

    def test_extract_dom_attribute_precedence(self):
        html = """
        <html>
            <div class="reading-content">
                <img data-url="https://cdn.example.com/page1_hires.jpg" data-src="https://cdn.example.com/page1_med.jpg" src="placeholder.gif"/>
                <img data-original="https://cdn.example.com/page2_hires.png" src="loading.jpg"/>
                <img src="https://cdn.example.com/page3.webp"/>
                <img src="https://cdn.example.com/logo.png"/>
            </div>
        </html>
        """
        urls = self.extractor.extract_image_urls(html, "https://example.com/chapter-1")
        assert len(urls) == 3
        assert urls[0] == "https://cdn.example.com/page1_hires.jpg"
        assert urls[1] == "https://cdn.example.com/page2_hires.png"
        assert urls[2] == "https://cdn.example.com/page3.webp"
        assert not any("logo" in u for u in urls)

    def test_extract_zero_images_raises_error(self):
        html = "<html><body><div>Chỉ có nội dung văn bản</div></body></html>"
        with pytest.raises(CrawlerError) as exc_info:
            self.extractor.extract_image_urls(html, "https://example.com/novels/chapter-1")
        assert exc_info.value.code == CrawlerErrorCode.NO_IMAGES_FOUND


# ==============================================================================
# 3. PR-04: SiteAdapterRegistry Tests
# ==============================================================================

class TestSiteAdapterRegistry:
    def test_builtin_profiles_loaded(self):
        registry = SiteAdapterRegistry()
        profile = registry.get_profile("https://www.webtoons.com/viewer")
        assert profile.domain == "webtoons.com"
        assert profile.confidence >= 0.8
        assert "webtoons.com" in profile.headers.get("Referer", "")

    def test_generic_fallback(self):
        registry = SiteAdapterRegistry()
        profile = registry.get_profile("https://unfamiliar-manga-site.xyz/read/chap-1")
        assert profile.domain == "unfamiliar-manga-site.xyz"
        assert profile.confidence <= 0.8

    def test_save_and_retrieve_profile(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        registry = SiteAdapterRegistry()
        custom = SourceProfile(
            domain="custom-manga.org",
            name="Custom Manga",
            confidence=0.99,
            headers={"User-Agent": "CustomBot"},
        )
        saved_path = registry.save_profile(custom)
        assert saved_path.exists()

        loaded = registry.get_profile("custom-manga.org")
        assert loaded.domain == "custom-manga.org"
        assert loaded.confidence == 0.99
        assert loaded.headers["User-Agent"] == "CustomBot"


# ==============================================================================
# 4. PR-04: ImageVerifier Tests
# ==============================================================================

class TestImageVerifier:
    def test_non_strict_backward_compatibility(self, tmp_path):
        dummy_file = tmp_path / "page_001.jpg"
        dummy_file.write_bytes(b"dummy image bytes for test fixture")
        ok, err = ImageVerifier.verify_image(dummy_file, strict=False)
        assert ok is True
        assert err is None

    def test_empty_file_rejected(self, tmp_path):
        empty_file = tmp_path / "empty.jpg"
        empty_file.write_bytes(b"")
        ok, err = ImageVerifier.verify_image(empty_file, strict=False)
        assert ok is False
        assert "empty" in err.lower()

    def test_strict_html_challenge_rejected(self, tmp_path):
        html_file = tmp_path / "page_001.jpg"
        html_file.write_text("<!DOCTYPE html><html><body>Cloudflare 403 Forbidden challenge</body></html>")
        ok, err = ImageVerifier.verify_image(html_file, strict=True)
        assert ok is False
        assert "error page" in err.lower()

    def test_strict_valid_image(self, tmp_path):
        valid_img = tmp_path / "valid.jpg"
        # Create real 200x200 colorful image
        img = Image.new("RGB", (200, 200), (255, 100, 50))
        # Add pattern for variance
        for x in range(50):
            for y in range(50):
                img.putpixel((x, y), (0, 255, 0))
        img.save(valid_img, format="JPEG")

        ok, err = ImageVerifier.verify_image(valid_img, strict=True)
        assert ok is True
        assert err is None

    def test_verify_chapter_raw_images_continuity_and_duplicates(self, tmp_path):
        raw_dir = tmp_path / "raw"
        raw_dir.mkdir()

        # Create page 1 and page 3 (missing page 2)
        img = Image.new("RGB", (200, 200), (120, 150, 180))
        img.save(raw_dir / "page_001.jpg", format="JPEG")
        img.save(raw_dir / "page_003.jpg", format="JPEG")

        report = ImageVerifier.verify_chapter_raw_images(raw_dir, strict=False)
        assert report.total_pages == 2
        assert 2 in report.missing_indices
        assert report.is_valid is False


# ==============================================================================
# 5. PR-05: YouTube Chapters Timeline Tests
# ==============================================================================

class TestYouTubeChaptersTimeline:
    def test_build_youtube_chapter_markers_three_or_more_chapters(self):
        timeline = FeatureTimeline(
            project_slug="warrior_grandpa",
            compiled_video_path="/path/to/final.mp4",
            total_duration_seconds=950.0,
            chapters_count=3,
            chapters=[
                ChapterTimelineItem(
                    chapter_number="1",
                    title="Tấn Dương Phủ",
                    start_seconds=0.0,
                    end_seconds=300.0,
                    duration_seconds=300.0,
                    start_timestamp="00:00",
                    end_timestamp="05:00",
                    source_video_path="/path/1.mp4",
                ),
                ChapterTimelineItem(
                    chapter_number="2",
                    title="Thức Tỉnh Chân Khí",
                    start_seconds=300.0,
                    end_seconds=620.0,
                    duration_seconds=320.0,
                    start_timestamp="05:00",
                    end_timestamp="10:20",
                    source_video_path="/path/2.mp4",
                ),
                ChapterTimelineItem(
                    chapter_number="3",
                    title="Đại Náo Huyết Kiếm Môn",
                    start_seconds=620.0,
                    end_seconds=950.0,
                    duration_seconds=330.0,
                    start_timestamp="10:20",
                    end_timestamp="15:50",
                    source_video_path="/path/3.mp4",
                ),
            ],
        )

        markers = build_youtube_chapter_markers(timeline)
        assert len(markers) == 3
        # Strict YouTube rule 1: First marker starts at 00:00
        assert markers[0]["time"] == "00:00"
        assert "Tập 1" in markers[0]["label"]
        assert markers[1]["time"] == "05:00"
        assert "Tập 2" in markers[1]["label"]
        assert markers[2]["time"] == "10:20"
        assert "Tập 3" in markers[2]["label"]

    def test_build_youtube_chapter_markers_fallback_less_than_three_chapters(self):
        # 1 chapter feature needs at least 3 markers to be valid YouTube chapters
        timeline = FeatureTimeline(
            project_slug="warrior_grandpa",
            compiled_video_path="/path/to/final.mp4",
            total_duration_seconds=360.0,
            chapters_count=1,
            chapters=[
                ChapterTimelineItem(
                    chapter_number="1",
                    title="Tập 1: Tấn Dương Phủ",
                    start_seconds=0.0,
                    end_seconds=360.0,
                    duration_seconds=360.0,
                    start_timestamp="00:00",
                    end_timestamp="06:00",
                    source_video_path="/path/1.mp4",
                    sub_markers=[
                        ChapterMarker(time="00:00", label="Mở đầu", seconds=0.0),
                        ChapterMarker(time="02:00", label="Giao chiến", seconds=120.0),
                        ChapterMarker(time="04:30", label="Tuyệt kỹ", seconds=270.0),
                    ],
                )
            ],
        )

        markers = build_youtube_chapter_markers(timeline)
        # Guarantees >= 3 markers starting at 00:00
        assert len(markers) >= 3
        assert markers[0]["time"] == "00:00"
        assert markers[1]["time"] == "02:00"
        assert markers[2]["time"] == "04:30"

    def test_feature_timeline_persistence_in_compile(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        proj_slug = "test_compile_proj"
        init_project_layout(proj_slug, title=proj_slug)

        chap1_dir = init_chapter_layout(proj_slug, 1)
        chap2_dir = init_chapter_layout(proj_slug, 2)

        # Create dummy chapter MP4 files
        (chap1_dir / "video").mkdir(parents=True, exist_ok=True)
        (chap2_dir / "video").mkdir(parents=True, exist_ok=True)
        mp1 = chap1_dir / "video" / "chapter_001_final.mp4"
        mp2 = chap2_dir / "video" / "chapter_002_final.mp4"
        mp1.write_bytes(b"dummy mp4 1")
        mp2.write_bytes(b"dummy mp4 2")

        # Mock ffmpeg execution
        with patch("src.cli.compile_cmd.run_ffmpeg") as mock_ffmpeg, \
             patch("shutil.which", return_value="/usr/local/bin/ffmpeg"):
            # Set fake output file creation on run_ffmpeg
            def side_effect(cmd, **kwargs):
                out_p = Path(cmd[-1])
                out_p.parent.mkdir(parents=True, exist_ok=True)
                out_p.write_bytes(b"compiled video content")

            mock_ffmpeg.side_effect = side_effect

            args = MagicMock(
                project=proj_slug,
                chapters="1,2",
                output=None,
                extract_audio=False,
                approved=True,
            )
            res = handle_compile(args)
            assert res["project_slug"] == proj_slug
            assert "timeline_path" in res
            assert Path(res["timeline_path"]).exists()

            timeline_data = read_json(Path(res["timeline_path"]))
            assert timeline_data["chapters_count"] == 2
            assert len(timeline_data["chapters"]) == 2


# ==============================================================================
# 6. YouTube Thumbnail Integration Tests (AI Generated Thumbnails)
# ==============================================================================

class TestYouTubeThumbnailIntegration:
    def test_youtube_metadata_detects_ai_thumbnail(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        proj_slug = "meta_thumb_proj"
        init_project_layout(proj_slug, title="Meta Thumb Proj")
        chap_dir = init_chapter_layout(proj_slug, 1)

        # Stage an AI thumbnail in output directory
        out_dir = get_output_dir(proj_slug)
        out_dir.mkdir(parents=True, exist_ok=True)
        thumb_path = out_dir / "chapter_1_thumbnail.jpg"
        thumb_path.write_bytes(b"mock_jpeg_bytes")

        # Create basic script
        script_data = {
            "schema_version": "3.0.0",
            "segments": [
                {"seq_id": "001", "panel_ids": ["panel_001"], "narrator_text": "Mở đầu trận chiến kịch tính."}
            ]
        }
        atomic_write_json(chap_dir / "script.json", script_data)

        args = MagicMock(project=proj_slug, chapter="1", title=None, description=None, tags=None, privacy="private")
        res = handle_youtube_metadata(args)
        assert res["status"] == "success"

        meta = read_json(Path(res["metadata_path"]))
        assert meta["thumbnail_concept"] == "AI Generated Thumbnail tại chapter_1_thumbnail.jpg"

    def test_youtube_metadata_detects_feature_thumbnail(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        proj_slug = "feature_thumb_proj"
        init_project_layout(proj_slug, title="Feature Thumb Proj")

        out_dir = get_output_dir(proj_slug)
        out_dir.mkdir(parents=True, exist_ok=True)
        feat_thumb = out_dir / "feature_thumbnail.jpg"
        feat_thumb.write_bytes(b"mock_feature_jpeg_bytes")

        args = MagicMock(project=proj_slug, chapter="feature", title=None, description=None, tags=None, privacy="private")
        res = handle_youtube_metadata(args)
        assert res["status"] == "success"

        meta = read_json(Path(res["metadata_path"]))
        assert "feature_thumbnail.jpg" in meta["thumbnail_concept"]


# ==============================================================================
# 7. End-to-End CLI Subcommand Integration Tests
# ==============================================================================

class TestPhaseBCLIIntegration:
    def test_crawl_verify_flag(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        proj_slug = "test_crawl_verify"
        init_project_layout(proj_slug, title=proj_slug)

        # Import directory with real images
        import_dir = tmp_path / "imports"
        import_dir.mkdir()
        for idx in range(1, 3):
            img = Image.new("RGB", (200, 200), (100 * idx, 50, 50))
            for x in range(30):
                img.putpixel((x, x), (255, 255, 255))
            img.save(import_dir / f"page_{idx:03d}.jpg", format="JPEG")

        parser = build_parser()
        args = parser.parse_args([
            "crawl",
            "-p", proj_slug,
            "-c", "1",
            "--import-dir", str(import_dir),
            "--verify",
        ])
        res = execute_subcommand("crawl", args)
        assert res["status"] == "completed"
        assert res["verification"]["is_valid"] is True

    def test_youtube_metadata_feature_with_timeline(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        proj_slug = "test_yt_feature"
        init_project_layout(proj_slug, title=proj_slug)


        # Save feature_timeline.json
        timeline = FeatureTimeline(
            project_slug=proj_slug,
            compiled_video_path="/path/to/final.mp4",
            total_duration_seconds=600.0,
            chapters_count=3,
            chapters=[
                ChapterTimelineItem(
                    chapter_number="1",
                    title="Khởi Đầu",
                    start_seconds=0.0,
                    end_seconds=200.0,
                    duration_seconds=200.0,
                    start_timestamp="00:00",
                    end_timestamp="03:20",
                    source_video_path="/1.mp4",
                ),
                ChapterTimelineItem(
                    chapter_number="2",
                    title="Giao Đấu",
                    start_seconds=200.0,
                    end_seconds=400.0,
                    duration_seconds=200.0,
                    start_timestamp="03:20",
                    end_timestamp="06:40",
                    source_video_path="/2.mp4",
                ),
                ChapterTimelineItem(
                    chapter_number="3",
                    title="Kết Thúc",
                    start_seconds=400.0,
                    end_seconds=600.0,
                    duration_seconds=200.0,
                    start_timestamp="06:40",
                    end_timestamp="10:00",
                    source_video_path="/3.mp4",
                ),
            ],
        )
        proj_dir = get_project_dir(proj_slug)
        atomic_write_json(proj_dir / "feature_timeline.json", timeline)


        parser = build_parser()
        args = parser.parse_args([
            "youtube",
            "metadata",
            "-p", proj_slug,
            "-c", "feature",
        ])
        res = execute_subcommand("youtube", args)
        assert res["status"] == "success"
        assert res["chapter_number"] == "feature"
        assert res["timestamps_count"] == 3

        meta_file = Path(res["metadata_path"])
        assert meta_file.exists()
        meta = YouTubeMetadata.model_validate(read_json(meta_file))
        assert meta.timestamps[0]["time"] == "00:00"
        assert "00:00 - Tập 1" in meta.description
