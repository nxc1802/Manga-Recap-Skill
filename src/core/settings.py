"""Centralized runtime settings and provider credential resolution."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from src.core.errors import ProviderAuthError


_DOTENV_LOADED = False


def load_runtime_dotenv(*, force: bool = False) -> None:
    """Load .env once at CLI/composition boundary without overriding explicit env vars."""
    global _DOTENV_LOADED
    if _DOTENV_LOADED and not force:
        return
    load_dotenv(override=False)
    _DOTENV_LOADED = True


@dataclass(frozen=True)
class RuntimeSettings:
    """Environment-derived operational paths, logging, and local engine settings."""

    data_dir: Path
    projects_dir: Path
    output_dir: Path
    workspace_dir: Path
    youtube_accounts_dir: Path
    kaggle_accounts_file: Path
    log_level: str
    vieneu_device: str
    vieneu_sample_rate: int
    vieneu_force_neural: bool
    hf_home: Path


def get_runtime_settings(*, load_env: bool = False) -> RuntimeSettings:
    """
    Resolves runtime settings cleanly from current process environment.
    Precedence: explicit environment variable > documented default.
    """
    if load_env:
        load_runtime_dotenv()

    env_data = os.getenv("MANGA_RECAP_DATA_DIR")
    data_dir = Path(env_data) if env_data else Path("data")
    projects_dir = data_dir / "projects"

    env_output = os.getenv("MANGA_RECAP_OUTPUT_DIR")
    output_dir = Path(env_output) if env_output else Path("output")

    env_workspace = os.getenv("MANGA_RECAP_WORKSPACE_DIR")
    workspace_dir = Path(env_workspace) if env_workspace else Path("workspace")

    env_yt_dir = os.getenv("YOUTUBE_ACCOUNTS_DIR")
    youtube_accounts_dir = Path(env_yt_dir) if env_yt_dir else (data_dir / "youtube_accounts")

    default_kaggle_file = (
        Path(env_data) / "credentials" / "kaggle_accounts.json"
        if env_data
        else Path("credentials") / "kaggle_accounts.json"
    )
    kaggle_accounts_file = Path(os.getenv("KAGGLE_ACCOUNTS_FILE") or default_kaggle_file)

    try:
        sample_rate = int(os.getenv("VIENEU_SAMPLE_RATE", "48000"))
    except ValueError:
        sample_rate = 48000

    return RuntimeSettings(
        data_dir=data_dir,
        projects_dir=projects_dir,
        output_dir=output_dir,
        workspace_dir=workspace_dir,
        youtube_accounts_dir=youtube_accounts_dir,
        kaggle_accounts_file=kaggle_accounts_file,
        log_level=(os.getenv("LOG_LEVEL") or "INFO").strip().upper(),
        vieneu_device=(os.getenv("VIENEU_DEVICE") or "mps").strip(),
        vieneu_sample_rate=sample_rate,
        vieneu_force_neural=os.getenv("VIENEU_FORCE_NEURAL") == "1",
        hf_home=Path(os.getenv("HF_HOME", str(Path.home() / ".cache" / "huggingface" / "hub"))),
    )


def resolve_gemini_api_key(explicit_key: Optional[str] = None, *, load_env: bool = False) -> str:
    """Resolve Gemini Decision API key from explicit argument, GEMINI_API_KEY, or GOOGLE_API_KEY."""
    if load_env:
        load_runtime_dotenv()
    key = explicit_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not key or not key.strip():
        raise ProviderAuthError(
            "Missing GEMINI_API_KEY in environment or .env for gemini-decision-1.",
            category="auth",
            provider="decision",
        )
    return key.strip()


def resolve_hf_token(explicit_token: Optional[str] = None) -> str:
    """Resolve Hugging Face token from explicit override or HF_TOKEN."""
    return (explicit_token or os.getenv("HF_TOKEN") or "").strip()


def resolve_kaggle_legacy_token() -> Optional[str]:
    """Resolve fallback Kaggle API token from KAGGLE_API_TOKEN or ~/.kaggle/access_token."""
    legacy = os.getenv("KAGGLE_API_TOKEN")
    if not legacy and not os.getenv("MANGA_RECAP_DATA_DIR"):
        path = Path(os.getenv("KAGGLE_CONFIG_DIR", str(Path.home() / ".kaggle"))) / "access_token"
        if path.is_file():
            legacy = path.read_text(encoding="utf-8").strip()
    return legacy.strip() if isinstance(legacy, str) and legacy.strip() else None
