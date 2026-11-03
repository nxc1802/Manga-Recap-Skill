import pytest
from pathlib import Path
from src.core.utils import format_sequence_id, slugify_project_title, get_project_data_dir

def test_format_sequence_id():
    assert format_sequence_id("5") == "005"
    assert format_sequence_id("5_1") == "005_1"
    assert format_sequence_id("12") == "012"
    assert format_sequence_id("123_abc") == "123_abc"
    assert format_sequence_id("") == "000"
    assert format_sequence_id(None) == "000"

def test_slugify_project_title():
    # Vietnamese accents to ASCII, lowercased, spaces replaced by underscores
    assert slugify_project_title("Ông Vô Giá Và Cháu Chí Tôn") == "ong_vo_gia_va_chau_chi_ton"
    assert slugify_project_title("Warrior Grandpa & His Supreme Granddaughter") == "warrior_grandpa_his_supreme_granddaughter"
    assert slugify_project_title("  Simple   Title  ") == "simple_title"

def test_get_project_data_dir():
    from src.core.config import config
    # 1. Default canonical path when directory does not exist
    expected_canonical = config.storage_dir / "ong_vo_gia_va_chau_chi_ton"
    assert get_project_data_dir("Ông Vô Giá Và Cháu Chí Tôn") == expected_canonical

    # 2. Legacy fallback when data/{slug} exists and data/projects/{slug} does not
    legacy_dir = config.data_dir / "legacy_test_project"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    try:
        assert get_project_data_dir("legacy_test_project") == legacy_dir
    finally:
        if legacy_dir.exists():
            legacy_dir.rmdir()
