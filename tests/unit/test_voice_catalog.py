"""
tests/unit/test_voice_catalog.py
Unit tests for 25-voice preset catalog and alias resolution.
"""

import pytest

from src.tts.catalog import (
    CATALOG_VOICES,
    VoiceGender,
    VoiceRegion,
    get_voice_profile,
    resolve_voice_id,
)


def test_catalog_voice_count_and_recommendations():
    assert len(CATALOG_VOICES) == 25, "Catalog must define exactly 25 preset voices"

    top10 = [v for v in CATALOG_VOICES.values() if v.is_recommended]
    assert len(top10) == 10, "Catalog must designate 10 recommended voices"

    # Verify primary defaults
    assert "Trúc Ly" in CATALOG_VOICES
    assert CATALOG_VOICES["Trúc Ly"].gender == VoiceGender.FEMALE
    assert CATALOG_VOICES["Trúc Ly"].region == VoiceRegion.NORTH
    assert CATALOG_VOICES["Trúc Ly"].is_recommended is True

    assert "Hải Đăng" in CATALOG_VOICES
    assert CATALOG_VOICES["Hải Đăng"].gender == VoiceGender.MALE
    assert CATALOG_VOICES["Hải Đăng"].is_recommended is True


def test_resolve_voice_id():
    # Exact match
    assert resolve_voice_id("Trúc Ly") == "Trúc Ly"
    assert resolve_voice_id("Hải Đăng") == "Hải Đăng"

    # None or empty fallback
    assert resolve_voice_id(None) == "Trúc Ly"
    assert resolve_voice_id("") == "Trúc Ly"

    # Aliases
    assert resolve_voice_id("default") == "Trúc Ly"
    assert resolve_voice_id("female_narrator") == "Trúc Ly"
    assert resolve_voice_id("male_narrator") == "Hải Đăng"
    assert resolve_voice_id("truc_ly") == "Trúc Ly"
    assert resolve_voice_id("hai_dang") == "Hải Đăng"

    # Legacy Cloud Aliases
    assert resolve_voice_id("edge-tts-female") == "Trúc Ly"
    assert resolve_voice_id("edge-tts-male") == "Hải Đăng"
    assert resolve_voice_id("gemini-2.5-flash") == "Trúc Ly"
    assert resolve_voice_id("openai-tts-nova") == "Trúc Ly"

    # Unknown voice fallback
    assert resolve_voice_id("non_existent_voice_xyz") == "Trúc Ly"


def test_get_voice_profile():
    p = get_voice_profile("Hải Đăng")
    assert p.name == "Hải Đăng"
    assert p.gender == VoiceGender.MALE

    # Profile via alias
    p_alias = get_voice_profile("edge-tts-female")
    assert p_alias.name == "Trúc Ly"
