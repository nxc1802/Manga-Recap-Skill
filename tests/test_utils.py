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

def test_get_project_data_dir(tmp_path, monkeypatch):
    data_dir = tmp_path / "env_data"
    monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(data_dir))

    # 1. Canonical path under MANGA_RECAP_DATA_DIR when directory does not exist
    expected_canonical = data_dir / "projects" / "ong_vo_gia_va_chau_chi_ton"
    assert get_project_data_dir("Ông Vô Giá Và Cháu Chí Tôn") == expected_canonical

    # 2. Legacy fallback under MANGA_RECAP_DATA_DIR/{slug} when projects/{slug} does not exist
    legacy_dir = data_dir / "legacy_test_project"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    assert get_project_data_dir("legacy_test_project") == legacy_dir


def test_get_project_data_dir_respects_env_override_without_repo_fallback(tmp_path, monkeypatch):
    from src.cli.exit_codes import ExitCode
    from src.cli.main import main
    from src.core.config import config

    repo_storage = tmp_path / "repo_data" / "projects"
    repo_data = tmp_path / "repo_data"
    leaked_dir = repo_storage / "leakedproj"
    leaked_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "storage_dir", repo_storage)
    monkeypatch.setattr(config, "data_dir", repo_data)

    isolated_dir = tmp_path / "isolated"
    monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(isolated_dir))
    assert get_project_data_dir("leakedproj") == isolated_dir / "projects" / "leakedproj"
    assert not get_project_data_dir("leakedproj").exists()

    for subcmd in ("validate", "inspect", "status"):
        assert main([subcmd, "-p", "leakedproj", "--json"]) == ExitCode.IO_ERROR

    monkeypatch.delenv("MANGA_RECAP_DATA_DIR", raising=False)
    assert get_project_data_dir("leakedproj") == leaked_dir
