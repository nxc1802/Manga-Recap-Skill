"""
src/youtube/account_manager.py
Multi-account management, token resolution, and channel profiling for YouTube Data API v3.
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from src.cli.exit_codes import CliValidationError
from src.core.logger import logger
from src.core.schemas import YouTubeAccountInfo, YouTubeAccountsRegistry, utc_iso_now
from src.core.storage import (
    StorageFileNotFoundError,
    atomic_write_json,
    get_youtube_accounts_dir,
    read_json,
)
from src.core.utils import DependencyNotFoundError

YOUTUBE_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]


class YouTubeAccountManager:
    """
    Manages multiple YouTube channel accounts, isolated token storage,
    OAuth credential refreshing, and channel profile resolution.
    """

    def __init__(self, storage_dir: Optional[Union[str, Path]] = None):
        self._explicit_storage_dir = Path(storage_dir) if storage_dir else None

    @property
    def storage_dir(self) -> Path:
        """Dynamically resolves storage directory, respecting environment overrides."""
        if self._explicit_storage_dir:
            return self._explicit_storage_dir
        return get_youtube_accounts_dir()

    @property
    def registry_file(self) -> Path:
        return self.storage_dir / "accounts.json"

    @property
    def tokens_dir(self) -> Path:
        return self.storage_dir / "tokens"

    def _ensure_initialized(self) -> None:
        """Initializes directory structure and auto-migrates legacy single .youtube_token.json if present."""
        self.tokens_dir.mkdir(parents=True, exist_ok=True)
        if not self.registry_file.exists():
            env_token_override = os.getenv("YOUTUBE_CREDENTIALS_FILE")
            is_custom_env = bool(os.getenv("YOUTUBE_ACCOUNTS_DIR") or os.getenv("MANGA_RECAP_DATA_DIR") or self._explicit_storage_dir)
            legacy_token = Path(env_token_override or ".youtube_token.json").resolve()
            if legacy_token.exists() and not env_token_override and not is_custom_env:
                try:
                    logger.info(f"Auto-migrating legacy YouTube credentials from {legacy_token}")
                    migrated_token = self.tokens_dir / "default.json"
                    shutil.copy2(legacy_token, migrated_token)

                    default_acc = YouTubeAccountInfo(
                        account_name="default",
                        channel_title="Default Channel",
                        channel_id="",
                        token_file=str(migrated_token),
                        client_secrets_file=os.getenv("YOUTUBE_CLIENT_SECRETS_FILE", "client_secrets.json"),
                        created_at=utc_iso_now(),
                    )
                    reg = YouTubeAccountsRegistry(
                        default_account="default",
                        accounts={"default": default_acc},
                    )
                    atomic_write_json(self.registry_file, reg)
                except Exception as e:
                    logger.warning(f"Failed to auto-migrate legacy token: {e}")

    def load_registry(self) -> YouTubeAccountsRegistry:
        """Loads and validates accounts.json registry."""
        self._ensure_initialized()
        if not self.registry_file.exists():
            return YouTubeAccountsRegistry()
        try:
            data = read_json(self.registry_file)
            return YouTubeAccountsRegistry.model_validate(data)
        except Exception as e:
            logger.warning(f"Failed to load accounts registry from {self.registry_file}: {e}")
            return YouTubeAccountsRegistry()

    def save_registry(self, registry: YouTubeAccountsRegistry) -> None:
        """Persists accounts registry atomically."""
        self.tokens_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.registry_file, registry)

    def list_accounts(self) -> List[Dict[str, Any]]:
        """Returns all registered YouTube accounts with channel info and active status."""
        reg = self.load_registry()
        res: List[Dict[str, Any]] = []
        for name, acc in reg.accounts.items():
            token_p = Path(acc.token_file)
            token_exists = token_p.exists()
            res.append({
                "account_name": name,
                "channel_title": acc.channel_title or "(Chưa đặt tên)",
                "channel_id": acc.channel_id or "(Chưa xác định)",
                "is_default": (name == reg.default_account),
                "token_file": str(token_p),
                "client_secrets_file": acc.client_secrets_file,
                "valid": token_exists and acc.is_active,
                "created_at": acc.created_at,
                "last_used_at": acc.last_used_at,
            })
        return res

    def get_account(self, account_name: Optional[str] = None) -> YouTubeAccountInfo:
        """
        Resolves an account by name, or falls back to the default account.
        Raises CliValidationError if account cannot be resolved.
        """
        reg = self.load_registry()

        # Legacy environment override fallback
        env_token_override = os.getenv("YOUTUBE_CREDENTIALS_FILE")
        if env_token_override and (not account_name or account_name == "default") and (not reg.accounts or not self.registry_file.exists()):
            return YouTubeAccountInfo(
                account_name="default",
                channel_title="Default Channel",
                channel_id="",
                token_file=str(Path(env_token_override).resolve()),
                client_secrets_file=os.getenv("YOUTUBE_CLIENT_SECRETS_FILE", "client_secrets.json"),
            )

        if not reg.accounts:
            # Fallback check for legacy .youtube_token.json on disk
            legacy_token = Path(env_token_override or ".youtube_token.json").resolve()
            if legacy_token.exists():
                return YouTubeAccountInfo(
                    account_name="default",
                    channel_title="Default Channel",
                    channel_id="",
                    token_file=str(legacy_token),
                    client_secrets_file=os.getenv("YOUTUBE_CLIENT_SECRETS_FILE", "client_secrets.json"),
                )
            raise CliValidationError(
                "No YouTube accounts registered.\n"
                "Please run 'manga-recap youtube accounts add --account <name>' or "
                "'python scripts/youtube_auth.py --account <name>' to authenticate a channel."
            )

        if account_name:
            target = account_name.strip()
            if target in reg.accounts:
                return reg.accounts[target]
            # Flexible match: case-insensitive alias or channel_title
            for name, acc in reg.accounts.items():
                if name.lower() == target.lower():
                    return acc
            for name, acc in reg.accounts.items():
                if acc.channel_title and acc.channel_title.strip().lower() == target.lower():
                    return acc
            available = list(reg.accounts.keys())
            raise CliValidationError(
                f"YouTube account '{target}' not found. Available accounts: {available}.\n"
                f"Run 'manga-recap youtube accounts add --account {target}' to add it."
            )

        # Fallback to default
        if reg.default_account and reg.default_account in reg.accounts:
            return reg.accounts[reg.default_account]

        # Or first available account
        first_key = next(iter(reg.accounts.keys()))
        return reg.accounts[first_key]

    def set_default_account(self, account_name: str) -> YouTubeAccountsRegistry:
        """Sets the specified account as the default upload account."""
        reg = self.load_registry()
        target = account_name.strip()
        matched_key = target if target in reg.accounts else None
        if not matched_key:
            for name, acc in reg.accounts.items():
                if name.lower() == target.lower() or (acc.channel_title and acc.channel_title.strip().lower() == target.lower()):
                    matched_key = name
                    break
        if not matched_key:
            available = list(reg.accounts.keys())
            raise CliValidationError(f"Cannot set default: account '{target}' not found. Available: {available}")
        reg.default_account = matched_key
        self.save_registry(reg)
        logger.info(f"Set '{matched_key}' as default YouTube upload account.")
        return reg

    def remove_account(self, account_name: str, delete_token: bool = False) -> YouTubeAccountsRegistry:
        """Removes an account from the registry and optionally deletes its token file."""
        reg = self.load_registry()
        target = account_name.strip()
        if target not in reg.accounts:
            raise CliValidationError(f"Account '{target}' not found in registry.")

        acc = reg.accounts.pop(target)
        if delete_token:
            token_p = Path(acc.token_file)
            if token_p.exists():
                try:
                    token_p.unlink()
                except OSError:
                    pass

        if reg.default_account == target:
            reg.default_account = next(iter(reg.accounts.keys())) if reg.accounts else None

        self.save_registry(reg)
        logger.info(f"Removed YouTube account '{target}' from registry.")
        return reg

    def fetch_channel_info(self, creds: Any) -> Tuple[str, str]:
        """
        Queries YouTube Data API v3 to retrieve the channel title and ID for given credentials.
        Returns (channel_title, channel_id).
        """
        try:
            discovery_mod = importlib.import_module("googleapiclient.discovery")
            build = discovery_mod.build
            service = build("youtube", "v3", credentials=creds)
            res = service.channels().list(mine=True, part="snippet").execute()
            items = res.get("items", [])
            if items:
                title = items[0].get("snippet", {}).get("title", "")
                c_id = items[0].get("id", "")
                return title, c_id
        except Exception as e:
            logger.debug(f"Could not fetch channel info: {e}")
        return "", ""

    def get_credentials(
        self,
        account_name: Optional[str] = None,
        force_reauth: bool = False,
    ) -> Tuple[Any, YouTubeAccountInfo]:
        """
        Retrieves, validates, and refreshes OAuth2 credentials for the target account.
        Returns (credentials, account_info).
        """
        try:
            req_mod = importlib.import_module("google.auth.transport.requests")
            creds_mod = importlib.import_module("google.oauth2.credentials")
            flow_mod = importlib.import_module("google_auth_oauthlib.flow")
            Request = req_mod.Request
            Credentials = creds_mod.Credentials
            InstalledAppFlow = flow_mod.InstalledAppFlow
        except ImportError as e:
            raise DependencyNotFoundError(
                "Missing Google API client dependencies for YouTube.\n"
                "Please install with: pip install google-api-python-client google-auth-oauthlib google-auth-httplib2"
            ) from e

        acc = self.get_account(account_name)
        token_path = Path(acc.token_file).resolve()
        secrets_path = Path(acc.client_secrets_file).resolve()

        creds = None
        if not force_reauth and token_path.exists():
            try:
                logger.info(f"Loading YouTube credentials for account '{acc.account_name}' from {token_path}")
                creds = Credentials.from_authorized_user_file(str(token_path), YOUTUBE_SCOPES)
            except Exception as e:
                logger.warning(f"Failed to load token file for '{acc.account_name}': {e}")
                creds = None

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                logger.info(f"Credentials for '{acc.account_name}' expired. Refreshing token...")
                try:
                    creds.refresh(Request())
                except Exception as e:
                    logger.error(f"Failed to refresh YouTube credentials: {e}")
                    creds = None

            if not creds:
                if not secrets_path.exists():
                    # Fallback check for client_secrets in credentials/ or root
                    candidates = [
                        Path("credentials/client_secrets_adroit.json").resolve(),
                        Path("credentials/client_secrets.json").resolve(),
                        Path("client_secrets.json").resolve(),
                    ]
                    found = next((c for c in candidates if c.exists()), None)
                    if found:
                        secrets_path = found
                    else:
                        raise StorageFileNotFoundError(
                            f"OAuth client secrets file not found at: {secrets_path}\n"
                            "Please place your OAuth 2.0 Client credentials (client_secrets.json) "
                            "in 'credentials/' or in the project root."
                        )

                if os.environ.get("PYTEST_CURRENT_TEST") and not os.environ.get("ALLOW_INTERACTIVE_OAUTH"):
                    raise CliValidationError("Interactive OAuth flow blocked in automated test runner.")

                logger.info(f"Launching local browser OAuth flow for account '{acc.account_name}'...")
                flow = InstalledAppFlow.from_client_secrets_file(str(secrets_path), YOUTUBE_SCOPES)
                if getattr(flow, "client_type", None) == "web":
                    logger.warning(
                        f"Note: '{secrets_path.name}' is of type 'Web application'. If Google displays "
                        "'Error 400: redirect_uri_mismatch', create a 'Desktop app' OAuth Client ID in Google Cloud Console."
                    )
                creds = flow.run_local_server(port=0)

            # Persist authorized credentials
            try:
                token_path.parent.mkdir(parents=True, exist_ok=True)
                with open(token_path, "w", encoding="utf-8") as f:
                    f.write(creds.to_json())
                logger.info(f"YouTube credentials for '{acc.account_name}' saved to {token_path}")
            except Exception as e:
                logger.warning(f"Could not persist credentials to {token_path}: {e}")

        # Update last_used_at and channel title if missing
        try:
            reg = self.load_registry()
            if acc.account_name in reg.accounts:
                reg.accounts[acc.account_name].last_used_at = utc_iso_now()
                if not reg.accounts[acc.account_name].channel_title or not reg.accounts[acc.account_name].channel_id:
                    c_title, c_id = self.fetch_channel_info(creds)
                    if c_title:
                        reg.accounts[acc.account_name].channel_title = c_title
                    if c_id:
                        reg.accounts[acc.account_name].channel_id = c_id
                self.save_registry(reg)
                acc = reg.accounts[acc.account_name]
        except Exception:
            pass

        return creds, acc

    def add_or_update_account(
        self,
        account_name: str,
        client_secrets_file: Optional[Union[str, Path]] = None,
        token_file: Optional[Union[str, Path]] = None,
        set_default: bool = False,
        force_auth: bool = False,
    ) -> YouTubeAccountInfo:
        """
        Authorizes, tests, and registers a new or existing YouTube channel account.
        """
        clean_name = account_name.strip()
        if not clean_name:
            raise CliValidationError("Account name cannot be empty.")

        sec_file = str(Path(client_secrets_file).resolve()) if client_secrets_file else "client_secrets.json"
        target_token_file = self.tokens_dir / f"{clean_name}.json"
        target_token_file.parent.mkdir(parents=True, exist_ok=True)

        if token_file and Path(token_file).exists():
            shutil.copy2(token_file, target_token_file)

        # Register placeholder
        reg = self.load_registry()
        is_first = len(reg.accounts) == 0
        new_acc = YouTubeAccountInfo(
            account_name=clean_name,
            channel_title="",
            channel_id="",
            token_file=str(target_token_file),
            client_secrets_file=sec_file,
            created_at=utc_iso_now(),
        )
        reg.accounts[clean_name] = new_acc
        if set_default or is_first or not reg.default_account:
            reg.default_account = clean_name
        self.save_registry(reg)

        # Authenticate and probe channel details
        try:
            creds, updated_acc = self.get_credentials(account_name=clean_name, force_reauth=force_auth)
            c_title, c_id = self.fetch_channel_info(creds)
            if c_title or c_id:
                reg = self.load_registry()
                if clean_name in reg.accounts:
                    if c_title:
                        reg.accounts[clean_name].channel_title = c_title
                    if c_id:
                        reg.accounts[clean_name].channel_id = c_id
                    self.save_registry(reg)
                    updated_acc = reg.accounts[clean_name]
            return updated_acc
        except Exception as e:
            logger.debug(f"Online authentication probe note for '{clean_name}': {e}")
            return new_acc


# Global manager instance
account_manager = YouTubeAccountManager()
