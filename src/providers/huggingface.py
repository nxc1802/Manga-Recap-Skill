"""Hugging Face Hub transport client decoupled from archival whitelist/receipt policy."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Optional

from huggingface_hub import HfApi, hf_hub_download

from src.core.errors import DomainValidationError, ProviderIntegrityError
from src.core.settings import resolve_hf_token as _resolve_hf_token_setting

DEFAULT_HF_REPO_ID = "Cuong2004/manga-recap-archive"
FALLBACK_HF_TOKEN = ""
_GIT_SHA40_RE = re.compile(r"^[a-f0-9]{40}$")


def resolve_hf_token(token: Optional[str] = None) -> str:
    """Resolves Hugging Face API token from explicit argument or runtime environment."""
    return (token or _resolve_hf_token_setting() or FALLBACK_HF_TOKEN).strip()


def get_hf_api(token: Optional[str] = None) -> HfApi:
    """Creates an authenticated Hugging Face Hub API client."""
    return HfApi(token=resolve_hf_token(token) or None)


def require_revision_sha(revision: Optional[str]) -> str:
    """Validates a 40-character lowercase Git commit SHA revision."""
    if not _GIT_SHA40_RE.fullmatch(revision or ""):
        raise DomainValidationError("Provide the 40-character commit SHA from the archive receipt")
    return str(revision)


class HuggingFaceClient:
    """Transport adapter for private Hugging Face dataset archive repositories."""

    def __init__(
        self,
        *,
        token: Optional[str] = None,
        api_client: Optional[Any] = None,
        download_fn: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.token = resolve_hf_token(token) or None
        self._api = api_client
        self._download_fn = download_fn or hf_hub_download

    @property
    def api(self) -> Any:
        if self._api is None:
            self._api = get_hf_api(self.token)
        return self._api

    def ensure_private_repo(self, repo_id: str = DEFAULT_HF_REPO_ID) -> str:
        """Ensures the dataset archive repository exists and rejects non-private repos."""
        self.api.create_repo(repo_id=repo_id, repo_type="dataset", private=True, exist_ok=True)
        info = self.api.repo_info(repo_id=repo_id, repo_type="dataset")
        if getattr(info, "private", None) is not True:
            raise DomainValidationError(f"Archive repository {repo_id} must be private")
        return repo_id

    def upload_folder(
        self,
        *,
        folder_path: Path,
        path_in_repo: str,
        repo_id: str = DEFAULT_HF_REPO_ID,
        commit_message: str,
    ) -> str:
        """Uploads staged folder to dataset repo and validates returned commit SHA."""
        commit = self.api.upload_folder(
            folder_path=str(folder_path),
            path_in_repo=path_in_repo,
            repo_id=repo_id,
            repo_type="dataset",
            commit_message=commit_message,
        )
        oid = getattr(commit, "oid", None)
        if not _GIT_SHA40_RE.fullmatch(oid or ""):
            raise ProviderIntegrityError(
                "Hugging Face archive push did not return a valid 40-character commit SHA",
                category="invalid_response",
                provider="huggingface",
            )
        return str(oid)

    def download_file(
        self,
        *,
        repo_id: str,
        revision: str,
        filename: str,
    ) -> Path:
        """Downloads a single file pinned to an exact Git commit revision."""
        return Path(
            self._download_fn(
                repo_id=repo_id,
                revision=revision,
                filename=filename,
                repo_type="dataset",
                token=self.token,
            )
        )
