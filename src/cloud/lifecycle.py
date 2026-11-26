"""Cloud job lifecycle states, workload-derived budgets, duplicate guards, and resource checks."""

from __future__ import annotations

import contextlib
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional, Tuple

from src.cloud.jobs import CloudJob, load_cloud_job_state, write_cloud_job_state
from src.core.errors import ProviderIntegrityError
from src.core.logger import logger
from src.core.storage import FileLock, read_json
from src.providers.kaggle import (
    PROVIDER_NAME,
    KaggleAuthError,
    KaggleExecutionError,
    KaggleSubmitAmbiguousError,
)


class LifecycleState:
    """Explicit persisted cloud lifecycle states for ``cloud_job.json``."""

    SUBMITTING = "SUBMITTING"
    SUBMIT_UNKNOWN = "SUBMIT_UNKNOWN"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    REMOTE_COMPLETE = "REMOTE_COMPLETE"
    SYNCED = "synced"
    DISPATCHED = "dispatched"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    QUEUE_TIMEOUT = "QUEUE_TIMEOUT"
    CLIENT_TIMEOUT = "CLIENT_TIMEOUT"


ACTIVE_JOB_STATES = {
    LifecycleState.SUBMITTING,
    LifecycleState.SUBMIT_UNKNOWN,
    LifecycleState.QUEUED,
    LifecycleState.RUNNING,
    "DISPATCHED",
    LifecycleState.REMOTE_COMPLETE,
    LifecycleState.QUEUE_TIMEOUT,
    LifecycleState.CLIENT_TIMEOUT,
}

TERMINAL_REMOTE_FAILURE_STATES = {
    "FAILED",
    "ERROR",
    "CANCEL",
    "CANCELLED",
    "CANCEL_ACKNOWLEDGED",
}

SAFE_PRE_SUBMIT_FALLBACK_CATEGORIES = {
    "auth_failed",
    "submission_rejected",
}

NON_RETRYABLE_FALLBACK_CATEGORIES = {
    "remote_failed",
    "submit_unknown",
    "active_job_exists",
    "client_poll_timeout",
    "queue_timeout",
    "quota_exhausted",
    "invalid_response",
    "dataset_failed",
    "manifest_mismatch",
    "corrupt_artifact",
}

MAX_KAGGLE_GPU_SESSION_SEC = 32400  # 9h Kaggle interactive/batch GPU session ceiling


def _utc_iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class WorkloadBudget:
    """Separated deadlines and operational thresholds derived from workload size."""

    queue_notice_sec: float
    queue_timeout_sec: Optional[float]
    execution_budget_sec: int
    client_poll_timeout_sec: int
    dataset_transfer_timeout_sec: float
    segment_count: int
    chapter_count: int

    def as_dict(self) -> Dict[str, Any]:
        return {
            "queue_notice_sec": self.queue_notice_sec,
            "queue_timeout_sec": self.queue_timeout_sec,
            "execution_budget_sec": self.execution_budget_sec,
            "client_poll_timeout_sec": self.client_poll_timeout_sec,
            "dataset_transfer_timeout_sec": self.dataset_transfer_timeout_sec,
            "segment_count": self.segment_count,
            "chapter_count": self.chapter_count,
        }


def _count_segments(project_dir: Path, chapters: Iterable[str]) -> int:
    total = 0
    for ch in chapters:
        script_path = project_dir / "chapters" / str(ch) / "script.json"
        if script_path.is_file():
            try:
                data = read_json(script_path)
                segs = data.get("segments") if isinstance(data, dict) else None
                if isinstance(segs, list):
                    total += len(segs)
            except Exception:
                total += 25
    return total


def estimate_workload_budgets(
    project_dir: Path,
    chapters: Iterable[str],
    job_type: str,
    *,
    gpu: bool = True,
    max_session_sec: int = MAX_KAGGLE_GPU_SESSION_SEC,
    queue_notice_sec: float = 300.0,
    queue_timeout_sec: Optional[float] = 900.0,
    dataset_transfer_timeout_sec: float = 600.0,
) -> WorkloadBudget:
    """Computes realistic separated timeouts from chapter/segment volume."""
    chapter_list = [str(c) for c in chapters]
    ch_count = max(1, len(chapter_list))
    seg_count = _count_segments(project_dir, chapter_list)

    # Conservative seconds per segment by mode, floored for single-chapter runs.
    sec_per_seg = {"tts": 4.0, "render": 10.0, "produce": 14.0}.get(job_type, 14.0)
    bootstrap_overhead_sec = 420.0
    raw_exec_sec = int(round(bootstrap_overhead_sec + max(1, seg_count) * sec_per_seg * 1.5))
    execution_budget_sec = max(1800, min(int(max_session_sec), raw_exec_sec))
    client_poll_timeout_sec = min(int(max_session_sec) + 300, execution_budget_sec + 300)

    return WorkloadBudget(
        queue_notice_sec=float(queue_notice_sec),
        queue_timeout_sec=(float(queue_timeout_sec) if (gpu and queue_timeout_sec is not None) else None),
        execution_budget_sec=execution_budget_sec,
        client_poll_timeout_sec=client_poll_timeout_sec,
        dataset_transfer_timeout_sec=float(dataset_transfer_timeout_sec),
        segment_count=seg_count,
        chapter_count=ch_count,
    )


def normalize_remote_status(raw_status: str) -> str:
    """Maps raw Kaggle status string to normalized canonical lifecycle token."""
    norm = str(raw_status or "").strip().upper().split(".")[-1]
    if norm in {"COMPLETE", "COMPLETED"}:
        return "COMPLETE"
    if norm in TERMINAL_REMOTE_FAILURE_STATES:
        return "FAILED"
    return norm or "UNKNOWN"


def reconcile_submit_unknown(
    client: Any,
    job_state_path: Optional[Path],
    state_payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Probes ``kernel_ref`` after ambiguous push to avoid duplicate blind submissions."""
    kernel_ref = str(state_payload.get("kernel_ref") or "").strip()
    if not kernel_ref:
        raise KaggleSubmitAmbiguousError(
            "Submission status is SUBMIT_UNKNOWN without kernel_ref; cannot safely resubmit",
            category="submit_unknown",
            provider=PROVIDER_NAME,
        )
    try:
        raw_status, norm, fail_msg = client.get_kernel_status(kernel_ref)
    except Exception as exc:
        updated = {
            **state_payload,
            "status": LifecycleState.SUBMIT_UNKNOWN,
            "error": f"Submission ambiguous and status probe failed: {exc}",
            "updated_at": _utc_iso_now(),
        }
        write_cloud_job_state(job_state_path, updated)
        raise KaggleSubmitAmbiguousError(
            f"Kaggle kernel push outcome ambiguous for '{kernel_ref}': status probe failed ({exc})",
            category="submit_unknown",
            provider=PROVIDER_NAME,
        ) from exc

    mapped = normalize_remote_status(norm or raw_status)
    updated = {
        **state_payload,
        "status": mapped,
        "updated_at": _utc_iso_now(),
    }
    if mapped == "FAILED":
        updated["error"] = str(fail_msg or f"status={raw_status}")
    write_cloud_job_state(job_state_path, updated)
    return updated


def _resolve_reconcile_client(
    state: CloudJob, *, client: Any = None, service: Any = None
) -> Tuple[Any, Optional[Callable[[], None]]]:
    """Returns `(client, restore_fn)` bound cleanly to the persisted original account if available."""
    if service is not None and hasattr(service, "bind_job_account"):
        return service.bind_job_account(state.account_name, state.username)
    if client is None or not getattr(client, "is_authenticated", False):
        return None, None
    acct = getattr(client, "account_name", "unknown")
    user = getattr(client, "username", None)
    orig_acct = state.account_name or "unknown"
    orig_user = state.username or "unknown"
    if orig_acct not in ("", "unknown") and acct not in ("", "unknown") and acct != orig_acct:
        if orig_user in ("", "unknown") or user != orig_user:
            return None, None
    return client, None


def check_no_duplicate_active_job(
    job_state_path: Path,
    *,
    client: Any = None,
    service: Any = None,
) -> Optional[CloudJob]:
    """Blocks new submission when an unresolved active, timed-out, or ambiguous local job exists."""
    if not job_state_path.exists():
        return None
    try:
        state = load_cloud_job_state(job_state_path)
    except Exception as exc:
        raise ProviderIntegrityError(
            f"Existing cloud_job.json is unreadable ({job_state_path}); resolve manually before resubmitting: {exc}",
            category="corrupt_artifact",
            provider=PROVIDER_NAME,
        ) from exc

    norm_status = str(state.status or "").strip()
    if norm_status.upper() not in {s.upper() for s in ACTIVE_JOB_STATES}:
        return state

    # Reconcile ambiguous/timed-out/active job via its original persisted account only.
    reconciled_client, restore_fn = _resolve_reconcile_client(state, client=client, service=service)
    if reconciled_client is not None and state.kernel_ref:
        try:
            raw_status, norm, fail_msg = reconciled_client.get_kernel_status(state.kernel_ref)
            remote_state = normalize_remote_status(norm or raw_status)
            if remote_state == "FAILED":
                updated = {
                    **state.model_dump(mode="json"),
                    "status": LifecycleState.FAILED,
                    "error": str(fail_msg or f"status={raw_status}"),
                    "updated_at": _utc_iso_now(),
                }
                write_cloud_job_state(job_state_path, updated)
                return CloudJob.model_validate(updated)
            if remote_state == "COMPLETE":
                updated = {
                    **state.model_dump(mode="json"),
                    "status": LifecycleState.REMOTE_COMPLETE,
                    "last_remote_status": "COMPLETE",
                    "updated_at": _utc_iso_now(),
                }
                write_cloud_job_state(job_state_path, updated)
                state = CloudJob.model_validate(updated)
                norm_status = LifecycleState.REMOTE_COMPLETE
            elif remote_state in {"QUEUED", "RUNNING"}:
                updated = {
                    **state.model_dump(mode="json"),
                    "status": remote_state,
                    "last_remote_status": remote_state,
                    "updated_at": _utc_iso_now(),
                }
                write_cloud_job_state(job_state_path, updated)
                state = CloudJob.model_validate(updated)
                norm_status = remote_state
        except Exception as exc:
            logger.warning(
                "Could not reconcile Kaggle job '%s' (account=%s): %s",
                state.kernel_ref,
                state.account_name,
                exc,
            )
        finally:
            if callable(restore_fn):
                restore_fn()

    if norm_status.upper() == LifecycleState.SUBMIT_UNKNOWN:
        raise KaggleSubmitAmbiguousError(
            f"Previous Kaggle job '{state.kernel_ref or 'unknown'}' (account={state.account_name}) is in SUBMIT_UNKNOWN; run 'cloud pull/reconcile' before resubmitting.",
            category="submit_unknown",
            provider=PROVIDER_NAME,
        )
    if norm_status.upper() == LifecycleState.REMOTE_COMPLETE:
        raise KaggleExecutionError(
            f"Remote Kaggle job '{state.kernel_ref}' (account={state.account_name}) already completed but is not yet synced. "
            "Run 'cloud pull' to verify and download outputs instead of resubmitting.",
            category="active_job_exists",
            provider=PROVIDER_NAME,
        )
    raise KaggleExecutionError(
        f"An active or unresolved Kaggle job '{state.kernel_ref or 'reservation'}' (account={state.account_name}) "
        f"is already recorded in state '{state.status}'. Reconcile, pull, or confirm termination before submitting.",
        category="active_job_exists",
        provider=PROVIDER_NAME,
    )


def _state_overlaps_chapters(state: CloudJob, target_chapters: set[str]) -> bool:
    recorded = {str(c) for c in (state.chapters or []) if str(c).strip()}
    if not recorded and state.chapter_number:
        recorded = {str(state.chapter_number).strip()}
    if not recorded:
        return True
    return bool(recorded & target_chapters)


def reserve_cloud_job_slot(
    project_dir: Path,
    job_state_path: Path,
    chapters: Iterable[str],
    job_meta: Dict[str, Any],
    *,
    client: Any = None,
    service: Any = None,
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Atomically checks chapter/series/chunk overlap under project lock and reserves ``SUBMITTING`` intent."""
    target_chapters = {str(c).strip() for c in chapters if str(c).strip()}
    lock = FileLock(project_dir / "cloud_dispatch.lock", timeout=10.0)
    with lock:
        candidate_paths = [project_dir / "cloud_job.json"]
        for ch in sorted(target_chapters):
            candidate_paths.append(project_dir / "chapters" / ch / "cloud_job.json")
        if job_state_path not in candidate_paths:
            candidate_paths.append(job_state_path)

        seen_paths: set[Path] = set()
        for path in candidate_paths:
            resolved = path.resolve()
            if resolved in seen_paths or not path.exists():
                continue
            seen_paths.add(resolved)
            try:
                existing = load_cloud_job_state(path)
            except Exception:
                check_no_duplicate_active_job(path, client=client, service=service)
                continue
            # Project-level cloud_job.json blocks only if its chapters overlap or if overwriting it directly.
            if resolved == (project_dir / "cloud_job.json").resolve() and resolved != job_state_path.resolve():
                if not _state_overlaps_chapters(existing, target_chapters):
                    continue
            check_no_duplicate_active_job(path, client=client, service=service)

        prior_state: Optional[Dict[str, Any]] = None
        if job_state_path.exists():
            prior_state = read_json(job_state_path)
        reservation_id = uuid.uuid4().hex
        intent = {
            **job_meta,
            "status": LifecycleState.SUBMITTING,
            "reservation_id": reservation_id,
            "submitted_at": _utc_iso_now(),
            "updated_at": _utc_iso_now(),
        }
        write_cloud_job_state(job_state_path, intent)
        return reservation_id, prior_state


def release_pre_submit_reservation(
    project_dir: Path,
    job_state_path: Path,
    reservation_id: str,
    prior_state: Optional[Dict[str, Any]],
) -> None:
    """Rolls back an unpushed ``SUBMITTING`` reservation cleanly on safe pre-submit rejection."""
    lock = FileLock(project_dir / "cloud_dispatch.lock", timeout=10.0)
    with lock:
        if not job_state_path.exists():
            return
        try:
            current = read_json(job_state_path)
        except Exception:
            return
        if (
            str(current.get("status") or "").upper() != LifecycleState.SUBMITTING
            or current.get("reservation_id") != reservation_id
        ):
            return
        if prior_state is not None:
            write_cloud_job_state(job_state_path, prior_state)
        else:
            with contextlib.suppress(Exception):
                job_state_path.unlink()


def persist_submit_intent(
    job_state_path: Optional[Path],
    job_meta: Dict[str, Any],
) -> Dict[str, Any]:
    """Writes immutable pre-submit intent before calling remote ``kernels_push``."""
    intent = {
        **job_meta,
        "status": LifecycleState.SUBMITTING,
        "submitted_at": _utc_iso_now(),
        "updated_at": _utc_iso_now(),
    }
    write_cloud_job_state(job_state_path, intent)
    return intent


def check_disk_low_water(path: Path, *, min_free_mb: int = 512) -> int:
    """Verifies sufficient free disk space; raises RuntimeError below threshold."""
    if min_free_mb <= 0:
        return 0
    target = path if path.exists() else path.parent
    usage = shutil.disk_usage(target)
    free_mb = int(usage.free // (1024 * 1024))
    if free_mb < min_free_mb:
        logger.error("Low disk space under %s: %d MiB free (< %d MiB required)", target, free_mb, min_free_mb)
        raise RuntimeError(f"Insufficient scratch disk space under {target}: {free_mb} MiB free < {min_free_mb} MiB")
    return free_mb
