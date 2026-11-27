"""
src/cli/youtube_cmd.py
Thin CLI subcommand handler: manga-recap youtube (metadata, upload, verify, auth, accounts).
Delegates Data API transport to ``src.providers.youtube`` and domain publishing logic to
``src.youtube.publisher``.
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

from src.cli.exit_codes import CliValidationError
from src.youtube.account_manager import YOUTUBE_SCOPES, YouTubeAccountManager, account_manager
from src.youtube.metadata import (
    build_youtube_chapter_markers,
    build_youtube_description,
    format_seconds_to_timestamp,
    generate_timestamps_from_script,
    handle_youtube_metadata,
)
from src.youtube.publisher import publish_chapter_video, verify_uploaded_video

__all__ = [
    "YOUTUBE_SCOPES",
    "YouTubeAccountManager",
    "account_manager",
    "build_youtube_chapter_markers",
    "build_youtube_description",
    "format_seconds_to_timestamp",
    "generate_timestamps_from_script",
    "get_youtube_oauth_credentials",
    "handle_youtube_accounts",
    "handle_youtube_auth",
    "handle_youtube_metadata",
    "handle_youtube_upload",
    "handle_youtube_verify",
    "handle_youtube",
]


def get_youtube_oauth_credentials(
    account_name: Optional[str] = None,
    force_reauth: bool = False,
    *,
    allow_interactive: bool = False,
):
    """Retrieves and refreshes YouTube OAuth2 credentials for specified or default account."""
    creds, _ = account_manager.get_credentials(
        account_name=account_name,
        force_reauth=force_reauth,
        allow_interactive=allow_interactive,
    )
    return creds


def handle_youtube_accounts(args: argparse.Namespace) -> Dict[str, Any]:
    """Manages multi-channel YouTube accounts (list, add, remove, default)."""
    action = getattr(args, "account_action", None) or "list"
    account_name = getattr(args, "account", None)

    if action == "list":
        accounts = account_manager.list_accounts()
        reg = account_manager.load_registry()
        return {
            "status": "success",
            "command": "youtube",
            "action": "accounts",
            "subaction": "list",
            "default_account": reg.default_account,
            "accounts_count": len(accounts),
            "accounts": accounts,
        }

    if action == "add":
        if not account_name:
            raise CliValidationError("--account is required to add an account (e.g. --account review_truyen).")
        acc = account_manager.add_or_update_account(
            account_name=account_name,
            client_secrets_file=getattr(args, "secrets", None),
            set_default=getattr(args, "set_default", False),
            allow_interactive=not (getattr(args, "json", False) or getattr(args, "json_mode", False)),
        )
        reg = account_manager.load_registry()
        return {
            "status": "success",
            "command": "youtube",
            "action": "accounts",
            "subaction": "add",
            "account": {
                "account_name": acc.account_name,
                "channel_title": acc.channel_title,
                "channel_id": acc.channel_id,
                "token_file": acc.token_file,
                "is_default": (acc.account_name == reg.default_account),
            },
        }

    if action == "remove":
        if not account_name:
            raise CliValidationError("--account is required to remove an account.")
        reg = account_manager.remove_account(account_name, delete_token=getattr(args, "delete_token", True))
        return {
            "status": "success",
            "command": "youtube",
            "action": "accounts",
            "subaction": "remove",
            "removed_account": account_name,
            "default_account": reg.default_account,
            "remaining_accounts_count": len(reg.accounts),
        }

    if action == "default":
        if not account_name:
            raise CliValidationError("--account is required to set default account.")
        reg = account_manager.set_default_account(account_name)
        return {
            "status": "success",
            "command": "youtube",
            "action": "accounts",
            "subaction": "default",
            "default_account": reg.default_account,
        }

    raise CliValidationError(f"Unknown account action '{action}'. Available: list, add, remove, default.")


def handle_youtube_auth(args: argparse.Namespace) -> Dict[str, Any]:
    """Tests and verifies YouTube OAuth credentials for specified or default account."""
    account_name = getattr(args, "account", None)
    force = getattr(args, "force", False)
    creds = get_youtube_oauth_credentials(account_name=account_name, force_reauth=force)

    legacy_token_env = os.getenv("YOUTUBE_CREDENTIALS_FILE")
    if legacy_token_env and not account_name:
        token_file = str(Path(legacy_token_env).resolve())
        acc_name, c_title, c_id = "default", "", ""
    else:
        try:
            acc_info = account_manager.get_account(account_name)
            token_file = str(Path(acc_info.token_file).resolve())
            acc_name = acc_info.account_name
            c_title, c_id = acc_info.channel_title, acc_info.channel_id
            if not c_title and creds and getattr(creds, "valid", False):
                c_title, c_id = account_manager.fetch_channel_info(creds)
                if c_title:
                    reg = account_manager.load_registry()
                    if acc_info.account_name in reg.accounts:
                        reg.accounts[acc_info.account_name].channel_title = c_title
                        reg.accounts[acc_info.account_name].channel_id = c_id
                        account_manager.save_registry(reg)
        except Exception:
            token_file = str(Path(legacy_token_env or ".youtube_token.json").resolve())
            acc_name, c_title, c_id = account_name or "default", "", ""

    return {
        "status": "success",
        "valid": bool(creds and getattr(creds, "valid", False)),
        "account": acc_name,
        "channel_title": c_title,
        "channel_id": c_id,
        "token_file": str(Path(token_file).resolve()),
    }


def handle_youtube_upload(args: argparse.Namespace) -> Dict[str, Any]:
    """Uploads chapter or feature compilation video, captions, and custom thumbnail."""
    return publish_chapter_video(
        args,
        account_mgr=account_manager,
        credentials_fn=get_youtube_oauth_credentials,
        sleep_fn=time.sleep,
    )


def handle_youtube_verify(args: argparse.Namespace) -> Dict[str, Any]:
    """Polls YouTube Data API and verifies uploaded video, captions, thumbnail, and receipt."""
    return verify_uploaded_video(
        args,
        account_mgr=account_manager,
        credentials_fn=get_youtube_oauth_credentials,
        sleep_fn=time.sleep,
    )


def handle_youtube(args: argparse.Namespace) -> Dict[str, Any]:
    """Top-level dispatch router for ``manga-recap youtube`` subcommands."""
    from src.core.execution_policy import enforce_execution_policy

    enforce_execution_policy("youtube", args)
    yt_action = getattr(args, "youtube_action", "metadata")
    if yt_action == "metadata":
        return handle_youtube_metadata(args)
    if yt_action == "upload":
        return handle_youtube_upload(args)
    if yt_action == "verify":
        return handle_youtube_verify(args)
    if yt_action == "auth":
        return handle_youtube_auth(args)
    if yt_action == "accounts":
        return handle_youtube_accounts(args)

    raise CliValidationError(
        f"Unknown YouTube action '{yt_action}'. Available actions: metadata, upload, verify, auth, accounts."
    )
