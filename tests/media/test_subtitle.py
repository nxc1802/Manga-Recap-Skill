"""
tests/unit/test_subtitle.py
Unit test suite for SubtitleGenerator and SRT helpers (PR-07).
"""

import pytest
from src.core.schemas import RecapScript, ScriptSegment
from src.render.subtitle import (
    SubtitleCue,
    SubtitleGenerator,
    format_srt_time,
    parse_srt_time,
    sanitize_subtitle_text,
    wrap_subtitle_lines,
)


class TestSubtitleHelpers:
    """Tests for subtitle time formatting, parsing, sanitization, and wrapping."""

    def test_format_srt_time_zero(self):
        assert format_srt_time(0.0) == "00:00:00,000"

    def test_format_srt_time_seconds_and_millis(self):
        assert format_srt_time(1.234) == "00:00:01,234"
        assert format_srt_time(65.789) == "00:01:05,789"
        assert format_srt_time(3661.050) == "01:01:01,050"

    def test_format_srt_time_negative_clamped_to_zero(self):
        assert format_srt_time(-5.0) == "00:00:00,000"

    def test_parse_srt_time_comma(self):
        assert pytest.approx(parse_srt_time("00:01:05,789"), 0.001) == 65.789

    def test_parse_srt_time_dot(self):
        assert pytest.approx(parse_srt_time("01:00:00.500"), 0.001) == 3600.500

    def test_parse_srt_time_invalid_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_srt_time("invalid_timecode")

    def test_sanitize_subtitle_text_strips_emotion_cues(self):
        raw = "[thở dài] Đã ba năm trôi qua kể từ ngày gia tộc sụp đổ..."
        cleaned = sanitize_subtitle_text(raw)
        assert cleaned == "Đã ba năm trôi qua kể từ ngày gia tộc sụp đổ..."

    def test_sanitize_subtitle_text_multiple_cues_and_spaces(self):
        raw = "[cười]   [hắng giọng]  Thật thú vị!  [nghiêm túc]   "
        assert sanitize_subtitle_text(raw) == "Thật thú vị!"

    def test_wrap_subtitle_lines_short_text(self):
        text = "Một câu ngắn gọn."
        assert wrap_subtitle_lines(text, max_chars_per_line=42) == text

    def test_wrap_subtitle_lines_multiline(self):
        text = "Đây là một câu rất dài nhằm mục đích kiểm tra khả năng ngắt dòng tự động của bộ sinh phụ đề tiếng Việt chuẩn."
        wrapped = wrap_subtitle_lines(text, max_chars_per_line=42, max_lines=2)
        lines = wrapped.split("\n")
        assert len(lines) == 2
        assert len(lines[0]) <= 42


class TestSubtitleGenerator:
    """Tests for SubtitleGenerator file writing, parsing, and sync validation."""

    def setup_method(self):
        self.generator = SubtitleGenerator(max_chars_per_line=42, max_lines=2)

    def test_build_cues_from_segments(self):
        segments = [
            ScriptSegment(seq_id="001", narrator_text="[hồi tưởng] Đoạn một.", duration=2.5),
            ScriptSegment(seq_id="002", narrator_text="Đoạn hai tiếp nối.", duration=3.0),
        ]
        cues = self.generator.build_cues_from_segments(segments)
        assert len(cues) == 2
        assert cues[0].index == 1
        assert cues[0].start_seconds == 0.0
        assert cues[0].end_seconds == 2.5
        assert cues[0].text == "Đoạn một."

        assert cues[1].index == 2
        assert cues[1].start_seconds == 2.5
        assert cues[1].end_seconds == 5.5
        assert cues[1].text == "Đoạn hai tiếp nối."

    def test_generate_and_parse_srt_file(self, tmp_path):
        srt_file = tmp_path / "test.srt"
        script = RecapScript(
            project_slug="test_proj",
            chapter_number="1",
            title="Episode 1",
            segments=[
                ScriptSegment(seq_id="1", narrator_text="Xin chào các bạn.", duration=2.0),
                ScriptSegment(seq_id="2", narrator_text="Chào mừng đến với Manga Review.", duration=3.5),
            ],
            total_duration_seconds=5.5,
        )

        out_path = self.generator.generate_srt_file(script, srt_file)
        assert out_path.exists()
        content = out_path.read_text(encoding="utf-8")
        assert "00:00:00,000 --> 00:00:02,000" in content
        assert "Xin chào các bạn." in content
        assert "00:00:02,000 --> 00:00:05,500" in content

        # Parse back
        parsed_cues = self.generator.parse_srt(out_path)
        assert len(parsed_cues) == 2
        assert parsed_cues[0].text == "Xin chào các bạn."
        assert parsed_cues[1].text == "Chào mừng đến với Manga Review."

    def test_validate_srt_sync_passes(self, tmp_path):
        srt_file = tmp_path / "valid.srt"
        segments = [
            ScriptSegment(seq_id="1", narrator_text="Lời thoại 1", duration=4.0),
            ScriptSegment(seq_id="2", narrator_text="Lời thoại 2", duration=3.0),
        ]
        self.generator.generate_srt_file(segments, srt_file)
        assert self.generator.validate_srt_sync(srt_file, expected_total_duration=7.0, tolerance_seconds=0.1)

    def test_validate_srt_sync_fails_on_duration_mismatch(self, tmp_path):
        srt_file = tmp_path / "mismatch.srt"
        segments = [
            ScriptSegment(seq_id="1", narrator_text="Lời thoại 1", duration=2.0),
        ]
        self.generator.generate_srt_file(segments, srt_file)
        # Expected duration is 10s, actual is 2s -> should fail
        assert not self.generator.validate_srt_sync(srt_file, expected_total_duration=10.0, tolerance_seconds=0.2)

    def test_build_subtitle_filter_escapes_paths(self):
        filter_str = self.generator.build_subtitle_filter("/path/to/sub:file.srt", font_size=24, margin_v=40)
        assert "subtitles=" in filter_str
        assert "FontSize=24" in filter_str
        assert "MarginV=40" in filter_str
        assert "\\:" in filter_str  # Colon is properly escaped
