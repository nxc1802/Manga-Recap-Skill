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
    expected_path = Path("data") / "ong_vo_gia_va_chau_chi_ton"
    assert get_project_data_dir("Ông Vô Giá Và Cháu Chí Tôn") == expected_path
