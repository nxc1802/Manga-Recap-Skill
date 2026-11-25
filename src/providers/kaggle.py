"""Kaggle SDK provider transport wrapper decoupled from cloud lifecycle orchestration."""

from __future__ import annotations

import contextlib
import io
import os
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from src.core.errors import (
    ProviderAuthError,
    ProviderExecutionError,
    ProviderQuotaError,
    ProviderTimeoutError,
)
from src.core.logger import logger

PROVIDER_NAME = "kaggle"
_AUTH_LOCK = threading.RLock()


class KaggleAuthError(ProviderAuthError, RuntimeError):
    """Raised when Kaggle authentication fails or requested token cannot be bound."""


class KaggleQuotaExhaustedError(ProviderQuotaError, RuntimeError):
    """Raised when a Kaggle account has exhausted its weekly GPU quota."""


class KaggleQueueTimeoutError(ProviderTimeoutError, TimeoutError):
    """Raised when a Kaggle kernel stays QUEUED past queue_timeout_seconds."""


class KaggleClientPollTimeoutError(ProviderTimeoutError, TimeoutError):
    """Raised when local polling times out while a remote Kaggle kernel may still be running."""


class KaggleExecutionError(ProviderExecutionError, RuntimeError):
    """Raised when Kaggle rejects submission or fails remotely."""


class KaggleSubmitAmbiguousError(ProviderExecutionError, RuntimeError):
    """Raised when kernel submission outcome is transport-ambiguous (SUBMIT_UNKNOWN)."""


def parse_kaggle_duration_seconds(raw: Any) -> float:
    """Parses Kaggle timedelta objects or duration strings into seconds."""
    if raw is None:
        return 0.0
    if isinstance(raw, timedelta):
        return max(0.0, float(raw.total_seconds()))
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return max(0.0, float(raw))
    match = re.match(r"^\s*(\d+(?:\.\d+)?)", str(raw))
    return max(0.0, float(match.group(1))) if match else 0.0


def parse_quota_statistics(
    raw: Any,
    *,
    account_name: str,
    username: str,
    min_required_sec: float = 120.0,
) -> Optional[Dict[str, Any]]:
    """Extracts normalized GPU quota metrics from Kaggle SDK statistics objects."""

    def _pick(obj: Any, *keys: str) -> Any:
        for key in keys:
            val = obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)
            if val is not None:
                return val
        return None

    gpu_q = _pick(raw, "gpu_quota", "gpuQuota")
    if gpu_q is None:
        return None

    raw_allowed = _pick(gpu_q, "total_time_allowed", "totalTimeAllowed")
    raw_used = _pick(gpu_q, "time_used", "timeUsed")
    raw_reserved = _pick(gpu_q, "time_reserved", "timeReserved")
    duration_types = (timedelta, str, int, float)
    if not isinstance(raw_allowed, duration_types) or isinstance(raw_allowed, bool):
        return None
    if not isinstance(raw_used, duration_types) or isinstance(raw_used, bool):
        return None

    allowed_sec = parse_kaggle_duration_seconds(raw_allowed)
    used_sec = parse_kaggle_duration_seconds(raw_used)
    reserved_sec = (
        parse_kaggle_duration_seconds(raw_reserved)
        if isinstance(raw_reserved, duration_types) and not isinstance(raw_reserved, bool)
        else 0.0
    )
    remaining_sec = max(0.0, allowed_sec - used_sec - reserved_sec)
    refresh_time = _pick(raw, "quota_refresh_time", "quotaRefreshTime")
    if isinstance(refresh_time, datetime):
        refresh_str: Optional[str] = refresh_time.isoformat()
    elif isinstance(refresh_time, str):
        refresh_str = refresh_time
    else:
        refresh_str = None

    return {
        "account_name": account_name,
        "username": username,
        "allowed_sec": round(allowed_sec, 2),
        "used_sec": round(used_sec, 2),
        "reserved_sec": round(reserved_sec, 2),
        "remaining_sec": round(remaining_sec, 2),
        "remaining_hours": round(remaining_sec / 3600.0, 2),
        "exhausted": allowed_sec > 0 and remaining_sec < float(min_required_sec),
        "quota_refresh_time": refresh_str,
    }


def authenticate_kaggle_token(
    api_token: Optional[str] = None,
    *,
    account_name: str = "explicit",
) -> Tuple[Optional[Any], Optional[str]]:
    """Authenticates a dedicated KaggleApi client while isolating KAGGLE_API_TOKEN."""
    with _AUTH_LOCK:
        previous = os.environ.get("KAGGLE_API_TOKEN")
        try:
            if api_token:
                os.environ["KAGGLE_API_TOKEN"] = api_token
            # Kaggle package import also authenticates its global client.
            # Keep SDK diagnostics off CLI stdout and out of secret logs.
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                from kaggle.api.kaggle_api_extended import KaggleApi

                api = KaggleApi()
                api.authenticate()
            if api_token and api.config_values.get(api.CONFIG_NAME_TOKEN) != api_token:
                raise KaggleAuthError(
                    "Requested token was not authenticated",
                    category="auth_failed",
                    provider=PROVIDER_NAME,
                )
            username = api.config_values.get(api.CONFIG_NAME_USER)
            if not username:
                raise KaggleAuthError(
                    "Kaggle username unavailable",
                    category="auth_failed",
                    provider=PROVIDER_NAME,
                )
            logger.info("Kaggle account '%s' authenticated", account_name)
            return api, str(username)
        except ModuleNotFoundError as exc:
            logger.warning(
                "Kaggle account '%s' could not authenticate: missing dependency '%s'",
                account_name,
                exc.name,
            )
            return None, None
        except (Exception, SystemExit):
            logger.warning("Kaggle account '%s' could not authenticate", account_name)
            return None, None
        finally:
            if previous is None:
                os.environ.pop("KAGGLE_API_TOKEN", None)
            else:
                os.environ["KAGGLE_API_TOKEN"] = previous


class KaggleClient:
    """Thin SDK wrapper around an authenticated KaggleApi instance."""

    def __init__(
        self,
        *,
        api: Optional[Any] = None,
        username: Optional[str] = None,
        account_name: str = "unknown",
    ) -> None:
        self.api = api
        self.username = username
        self.account_name = account_name

    @classmethod
    def from_token(
        cls,
        api_token: Optional[str] = None,
        *,
        account_name: str = "explicit",
    ) -> "KaggleClient":
        api, username = authenticate_kaggle_token(api_token, account_name=account_name)
        return cls(api=api, username=username, account_name=account_name)

    @property
    def is_authenticated(self) -> bool:
        return self.api is not None

    def get_gpu_quota_status(self, min_required_sec: float = 120.0) -> Optional[Dict[str, Any]]:
        """Queries Kaggle weekly GPU accelerator quota statistics."""
        if not self.api or not hasattr(self.api, "build_kaggle_client"):
            return None
        try:
            with self.api.build_kaggle_client() as client:
                raw = client.kernels.kernels_api_client.get_accelerator_quota_statistics()
        except Exception:
            return None
        return parse_quota_statistics(
            raw,
            account_name=self.account_name,
            username=self.username or "unknown",
            min_required_sec=min_required_sec,
        )

    def create_private_dataset(self, dataset_dir: Path) -> None:
        if not self.api:
            raise KaggleAuthError("Kaggle API is not authenticated", category="auth_failed", provider=PROVIDER_NAME)
        self.api.dataset_create_new(str(dataset_dir), public=False, quiet=True)

    def wait_for_dataset_ready(
        self,
        ds_ref: str,
        *,
        expected_file: str = "payload.tar.gz",
        timeout_sec: float = 600.0,
        poll_interval: float = 5.0,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        """Waits until a newly uploaded private dataset becomes ready for kernel attach."""
        if not self.api:
            raise KaggleAuthError("Kaggle API is not authenticated", category="auth_failed", provider=PROVIDER_NAME)
        ready = False
        started = time.monotonic()
        while time.monotonic() - started < timeout_sec:
            if poll_interval > 0:
                sleep_fn(poll_interval)
            try:
                status = self.api.dataset_status(ds_ref)
            except Exception as err:
                logger.info(f"Waiting for Kaggle series dataset propagation ({ds_ref}): {err}")
                continue
            status = getattr(status, "status", status)
            if isinstance(status, dict):
                status = status.get("status", "")
            norm = str(status).strip().lower().split(".")[-1]
            if norm == "ready":
                try:
                    entries = getattr(self.api.dataset_list_files(ds_ref), "dataset_files", [])
                    if any(entry.name == expected_file for entry in entries) or entries:
                        ready = True
                        break
                except Exception:
                    pass
            if norm in {"error", "failed"}:
                raise KaggleExecutionError(
                    f"Kaggle series payload dataset failed: {status}",
                    category="dataset_failed",
                    provider=PROVIDER_NAME,
                )
        if not ready:
            raise TimeoutError("Kaggle series payload dataset did not become ready; kernel not launched")

    def push_kernel(self, staging_dir: Path, default_ref: str) -> str:
        """Pushes kernel bundle and returns resolved Kaggle kernel reference."""
        if not self.api:
            raise KaggleAuthError("Kaggle API is not authenticated", category="auth_failed", provider=PROVIDER_NAME)
        push_res = self.api.kernels_push(str(staging_dir))
        error = push_res.get("error") if isinstance(push_res, dict) else getattr(push_res, "error", None)
        if not push_res or (isinstance(error, (str, bool)) and error):
            raise KaggleExecutionError(
                "Kaggle rejected kernel submission",
                category="submission_rejected",
                provider=PROVIDER_NAME,
            )
        assigned_ref = getattr(push_res, "ref", None)
        if isinstance(push_res, dict) and not assigned_ref:
            assigned_ref = push_res.get("ref")
        if assigned_ref:
            return str(assigned_ref).replace("/code/", "").strip()
        return default_ref

    def get_kernel_status(self, kernel_ref: str) -> Tuple[str, str, Optional[str]]:
        """Fetches kernel status and returns ``(raw_status, normalized_status, failure_message)``."""
        if not self.api:
            raise KaggleAuthError("Kaggle API is not authenticated", category="auth_failed", provider=PROVIDER_NAME)
        status_obj = self.api.kernels_status(kernel_ref)
        current_status = getattr(status_obj, "status", str(status_obj))
        if isinstance(current_status, dict):
            current_status = current_status.get("status", "UNKNOWN")
        raw_str = str(current_status)
        norm = raw_str.strip().upper().split(".")[-1]
        fail_msg = getattr(status_obj, "failureMessage", None)
        if isinstance(status_obj, dict) and fail_msg is None:
            fail_msg = status_obj.get("failureMessage")
        return raw_str, norm, fail_msg

    def download_kernel_output(self, kernel_ref: str, dest_dir: Path) -> None:
        """Downloads completed kernel outputs/logs into ``dest_dir``."""
        if not self.api:
            raise KaggleAuthError("Kaggle API is not authenticated", category="auth_failed", provider=PROVIDER_NAME)
        dest_dir.mkdir(parents=True, exist_ok=True)
        self.api.kernels_output(kernel_ref, str(dest_dir))

    def cancel_kernel(self, kernel_ref: str) -> bool:
        """Best-effort kernel cancellation when supported by Kaggle SDK client."""
        if not self.api:
            return False
        if hasattr(self.api, "kernels_cancel"):
            try:
                self.api.kernels_cancel(kernel_ref)
                return True
            except Exception as exc:
                logger.warning("Failed cancelling Kaggle kernel %s: %s", kernel_ref, exc)
                return False
        if hasattr(self.api, "build_kaggle_client"):
            try:
                with self.api.build_kaggle_client() as client:
                    cancel_fn = getattr(getattr(client, "kernels", None), "cancel_kernel", None)
                    if callable(cancel_fn):
                        cancel_fn(kernel_ref)
                        return True
            except Exception as exc:
                logger.warning("Failed cancelling Kaggle kernel %s via client: %s", kernel_ref, exc)
        return False
