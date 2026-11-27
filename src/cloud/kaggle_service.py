"""
src/cloud/kaggle_service.py
Kaggle Cloud job lifecycle orchestrator for single-chapter and Dual-T4 series execution.
Delegates SDK transport to ``src.providers.kaggle`` and bootstrap/verification to ``src.cloud.bootstrap``.
"""

from __future__ import annotations

import json
import shutil
import tarfile
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.cloud.bootstrap import render_worker_stub, safe_extract_tar
from src.cloud.jobs import load_cloud_job_state, write_cloud_job_state
from src.cloud.lifecycle import (
    LifecycleState,
    check_no_duplicate_active_job,
    estimate_workload_budgets,
    normalize_remote_status,
    persist_submit_intent,
    reconcile_submit_unknown,
    release_pre_submit_reservation,
    reserve_cloud_job_slot,
)
from src.core.logger import logger
from src.core.project import get_chapter_dir, get_project_dir
from src.core.storage import atomic_write_json, read_json
from src.providers.kaggle import (
    KaggleAuthError,
    KaggleClient,
    KaggleClientPollTimeoutError,
    KaggleExecutionError,
    KaggleQueueTimeoutError,
    KaggleQuotaExhaustedError,
    KaggleSubmitAmbiguousError,
    authenticate_kaggle_token,
    parse_kaggle_duration_seconds as _parse_kaggle_duration_seconds,
)

__all__ = [
    "KaggleCloudService",
    "KaggleAuthError",
    "KaggleClientPollTimeoutError",
    "KaggleQueueTimeoutError",
    "KaggleQuotaExhaustedError",
    "KaggleExecutionError",
    "KaggleSubmitAmbiguousError",
    "safe_extract_tar",
    "_parse_kaggle_duration_seconds",
]


class KaggleCloudService:
    """
    Manages remote batch rendering and TTS synthesis lifecycle on Kaggle Cloud.
    Supports clean job separation:
    - 'tts': Audio synthesis only, syncing audio clips and script without video rendering.
    - 'render': Hardware video rendering only, reusing pre-existing audio cache and skipping TTS.
    - 'produce': Full end-to-end production (TTS + Render).
    """

    DEFAULT_KERNEL_SLUG = "manga-recap-cloud-runner"
    _auth_lock = threading.RLock()

    @staticmethod
    def _require_kernel_submission(result: Any) -> None:
        error = result.get("error") if isinstance(result, dict) else getattr(result, "error", None)
        if not result or (isinstance(error, (str, bool)) and error):
            err_text = str(error or "").strip()
            if "quota" in err_text.lower():
                raise KaggleQuotaExhaustedError(
                    f"Kaggle rejected kernel submission (quota): {err_text}",
                    category="quota_exhausted",
                    provider="kaggle",
                )
            raise KaggleExecutionError(
                f"Kaggle rejected kernel submission: {err_text}" if err_text else "Kaggle rejected kernel submission",
                category="submission_rejected",
                provider="kaggle",
            )

    def __init__(self, api_token: Optional[str] = None, account_name: Optional[str] = None):
        from src.cloud.account_manager import KaggleAccountManager

        self._accounts = (
            [(account_name or "explicit", api_token)]
            if api_token
            else KaggleAccountManager().candidates()
        )
        self._api = None
        self._username = None
        self._account_index = 0
        self.account_name, self.api_token = "unconfigured", None
        if not self._accounts:
            return
        if account_name and not api_token:
            for index, (cand_name, _) in enumerate(self._accounts):
                if cand_name == account_name:
                    self._select_account(index)
                    return
            raise KaggleAuthError(
                f"Configured Kaggle account '{account_name}' not found",
                category="auth_failed",
                provider="kaggle",
            )
        self._select_account(0)

    def _select_account(self, index: int) -> None:
        self._account_index = index
        self.account_name, self.api_token = self._accounts[index]
        self._api = None
        self._username = None
        self._authenticate()

    def __repr__(self) -> str:
        return f"<KaggleCloudService account='{self.account_name}' authenticated={self.is_authenticated}>"

    def _authenticate(self) -> None:
        """Bind a separate API client to this token; restore process credentials."""
        with self._auth_lock:
            api, username = authenticate_kaggle_token(
                self.api_token,
                account_name=self.account_name,
            )
            self._api = api
            self._username = username

    def _client(self) -> KaggleClient:
        return KaggleClient(
            api=getattr(self, "_api", None),
            username=getattr(self, "_username", None),
            account_name=getattr(self, "account_name", "unknown"),
        )

    def bind_job_account(
        self, account_name: Optional[str], username: Optional[str] = None
    ) -> tuple[Optional[KaggleClient], Optional[Any]]:
        """Temporarily binds the original account for reconciliation and returns ``(client, restore_fn)``."""
        target_acct = (account_name or "").strip()
        target_user = (username or "").strip()
        curr_acct = getattr(self, "account_name", "unknown")
        curr_user = getattr(self, "_username", None)
        matches_current = (
            (target_acct in ("", "unknown") or target_acct == curr_acct)
            and (target_user in ("", "unknown") or target_user == curr_user)
        )
        if matches_current:
            client = self._client()
            return (client if client.is_authenticated else None), None

        accounts = getattr(self, "_accounts", None) or []
        orig_index = getattr(self, "_account_index", 0)
        for idx, (acct_name, _) in enumerate(accounts):
            if target_acct not in ("", "unknown") and acct_name == target_acct:
                self._select_account(idx)
                client = self._client()
                def _restore() -> None:
                    if 0 <= orig_index < len(accounts):
                        self._select_account(orig_index)
                if client.is_authenticated and (target_user in ("", "unknown") or client.username == target_user):
                    return client, _restore
                _restore()
                return None, None
        return None, None

    @property
    def is_authenticated(self) -> bool:
        return self._api is not None

    def get_username(self) -> str:
        return self._username or "unknown"

    def get_gpu_quota_status(self, min_required_sec: float = 120.0) -> Optional[Dict[str, Any]]:
        """Queries Kaggle weekly GPU accelerator quota when supported by the client."""
        return self._client().get_gpu_quota_status(min_required_sec=min_required_sec)

    def list_accounts_quota(self, min_required_sec: float = 120.0) -> List[Dict[str, Any]]:
        """Returns authenticated status and GPU quota summary across all configured accounts."""
        accounts = getattr(self, "_accounts", None) or []
        if not accounts:
            quota = self.get_gpu_quota_status(min_required_sec=min_required_sec)
            return [quota] if quota else []
        orig_index = getattr(self, "_account_index", 0)
        results: List[Dict[str, Any]] = []
        try:
            for idx in range(len(accounts)):
                self._select_account(idx)
                if not self.is_authenticated:
                    results.append({
                        "account_name": self.account_name,
                        "authenticated": False,
                        "exhausted": True,
                    })
                    continue
                quota = self.get_gpu_quota_status(min_required_sec=min_required_sec)
                if quota:
                    quota["authenticated"] = True
                    results.append(quota)
                else:
                    results.append({
                        "account_name": self.account_name,
                        "username": self.get_username(),
                        "authenticated": True,
                        "exhausted": False,
                    })
        finally:
            if 0 <= orig_index < len(accounts):
                self._select_account(orig_index)
        return results

    @staticmethod
    def _write_cloud_job_state(state_path: Optional[Path], payload: Dict[str, Any]) -> None:
        write_cloud_job_state(state_path, payload)

    def package_chapter_payload(
        self,
        project_slug: str,
        chapter_number: Any,
        out_tar_path: Path,
        job_type: str = "produce",
        *,
        series: bool = False,
    ) -> Path:
        from src.cloud.payload import package_payload

        path, self._payload_manifest_hash = package_payload(
            get_project_dir(project_slug),
            chapter_number,
            out_tar_path,
            job_type,
            series=series,
        )
        with tarfile.open(path) as archive:
            manifest = json.load(archive.extractfile("payload_manifest.json"))
        self._dispatch_input_hashes = {
            relative.removeprefix("project/"): value
            for relative, value in manifest["files"].items()
            if relative.startswith("project/") and not relative.endswith("/chapter.json")
        }
        return path

    def generate_worker_script(
        self,
        project_slug: str,
        chapter_number: str,
        render_workers: int = 4,
        encoder_preference: str = "auto",
        job_type: str = "produce",
        compile_feature: bool = False,
    ) -> str:
        from src.core.series_setup import require_series_setup

        require_series_setup(project_slug)
        if job_type not in {"tts", "render", "produce"}:
            raise ValueError("Unknown cloud job type")
        digest = getattr(self, "_payload_manifest_hash", None)
        if not digest:
            raise ValueError("Package and hash the complete runtime before generating a worker")
        norm_pref = str(encoder_preference or "auto").strip().lower()
        if norm_pref in {"auto", "nvenc", "h264_nvenc"}:
            encoder = "h264_nvenc"
        elif job_type == "tts" and norm_pref == "libx264":
            encoder = "h264_nvenc"
        else:
            raise ValueError(
                f"Unsupported cloud encoder '{encoder_preference}'. V7 Kaggle execution requires h264_nvenc."
            )

        return render_worker_stub(
            expected_hash=digest,
            encoder=encoder,
            render_workers=render_workers,
            compile_feature=compile_feature,
        )

    def dispatch_chapter_job(
        self,
        project_slug: str,
        chapter_number: str,
        kernel_slug: Optional[str] = None,
        gpu: bool = True,
        wait: bool = True,
        poll_interval: int = 6,
        timeout_seconds: Optional[int] = None,
        queue_timeout_seconds: Optional[float] = None,
        queue_notice_seconds: float = 300.0,
        dataset_timeout_seconds: float = 600.0,
        job_type: str = "produce",
        render_workers: int = 4,
        encoder_preference: str = "auto",
        series_chapters: Optional[List[str]] = None,
        compile_feature: bool = False,
    ) -> Dict[str, Any]:
        """Dispatches a chapter or series job to Kaggle Dual-T4 Cloud, monitors progress, and syncs outputs."""
        if job_type not in {"tts", "render", "produce"}:
            raise ValueError("Unknown cloud job type")
        if not gpu:
            raise ValueError("CPU-only Kaggle execution was removed in V7; gpu=True (Dual T4) is required.")
        client = self._client()
        if not client.is_authenticated:
            raise RuntimeError("Kaggle API is not authenticated")

        series = series_chapters is not None
        chapters = list(series_chapters) if series else [str(chapter_number)]
        project = get_project_dir(project_slug)
        chapter = project / "chapters" / str(chapter_number)
        job_state_path = (project if series else chapter) / "cloud_job.json"

        resolved_queue_timeout: Optional[float] = 900.0 if queue_timeout_seconds is None else (
            None if float(queue_timeout_seconds) <= 0 else float(queue_timeout_seconds)
        )
        budget = estimate_workload_budgets(
            project,
            chapters,
            job_type,
            gpu=True,
            queue_notice_sec=queue_notice_seconds,
            queue_timeout_sec=resolved_queue_timeout,
            dataset_transfer_timeout_sec=dataset_timeout_seconds,
        )
        effective_timeout = int(timeout_seconds) if timeout_seconds is not None else budget.client_poll_timeout_sec

        kernel_slug = (
            kernel_slug
            or f"mr-{project_slug[:12]}-c{chapter_number}-{int(time.time()) % 100000}".lower().replace("_", "-")
        )[:45].rstrip("-")
        expected_kernel_ref = f"{self._username}/{kernel_slug}"
        base_meta: Dict[str, Any] = {
            "project_slug": project_slug,
            "chapter_number": str(chapter_number),
            "job_type": job_type,
            "kernel_ref": expected_kernel_ref,
            "account_name": getattr(self, "account_name", "unknown"),
            "username": self.get_username(),
            "gpu": True,
            "series": series,
            "chapters": chapters,
            "budget": budget.as_dict(),
        }
        reservation_id, prior_state = reserve_cloud_job_slot(
            project,
            job_state_path,
            chapters,
            base_meta,
            client=client,
            service=self,
        )

        staging_dir = Path(tempfile.mkdtemp(prefix="manga-kaggle-"))
        payload_tar = staging_dir / "payload.tar.gz"
        host_timing: Dict[str, float] = {}
        try:
            quota = self.get_gpu_quota_status()
            if quota and quota.get("exhausted"):
                raise KaggleQuotaExhaustedError(
                    f"Kaggle account '{getattr(self, 'account_name', 'unknown')}' ({self.get_username()}) "
                    f"has exhausted its weekly GPU quota ({quota['used_sec']:.1f}s / {quota['allowed_sec']:.1f}s used, "
                    f"refreshes {quota.get('quota_refresh_time') or 'next cycle'}).",
                    category="quota_exhausted",
                    provider="kaggle",
                )

            t_pack = time.monotonic()
            self.package_chapter_payload(
                project_slug,
                chapters if series else chapter_number,
                payload_tar,
                job_type=job_type,
                series=series,
            )
            host_timing["package_sec"] = round(time.monotonic() - t_pack, 4)

            worker_py = staging_dir / "worker.py"
            worker_code = self.generate_worker_script(
                project_slug=project_slug,
                chapter_number=chapter_number,
                render_workers=render_workers,
                encoder_preference=encoder_preference,
                job_type=job_type,
                compile_feature=compile_feature,
            )
            worker_py.write_text(worker_code, encoding="utf-8")

            dataset_slug = f"mr-payload-{self._payload_manifest_hash[:12]}-{time.time_ns() % 1000000000}"
            ds_staging = staging_dir / "dataset"
            ds_staging.mkdir()
            shutil.copy2(payload_tar, ds_staging / "payload.tar.gz")
            ds_ref = f"{self._username}/{dataset_slug}"
            atomic_write_json(
                ds_staging / "dataset-metadata.json",
                {"title": dataset_slug, "id": ds_ref, "licenses": [{"name": "other"}]},
            )
            t_ds_create = time.monotonic()
            client.create_private_dataset(ds_staging)
            host_timing["dataset_create_sec"] = round(time.monotonic() - t_ds_create, 4)
            t_ds_ready = time.monotonic()
            self._wait_for_dataset_ready(
                ds_ref,
                expected_file="payload.tar.gz",
                timeout_sec=budget.dataset_transfer_timeout_sec,
            )
            host_timing["dataset_ready_sec"] = round(time.monotonic() - t_ds_ready, 4)
        except Exception:
            release_pre_submit_reservation(project, job_state_path, reservation_id, prior_state)
            raise

        metadata = {
            "id": expected_kernel_ref,
            "title": kernel_slug,
            "code_file": "worker.py",
            "language": "python",
            "kernel_type": "script",
            "is_private": True,
            "enable_gpu": True,
            "enable_tpu": False,
            "enable_internet": True,
            "machine_shape": "NvidiaTeslaT4",
            "dataset_sources": [ds_ref],
            "competition_sources": [],
            "kernel_sources": [],
            "model_sources": [],
        }
        (staging_dir / "kernel-metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")

        job_meta = {
            **base_meta,
            "dataset_ref": ds_ref,
            "payload_manifest_sha256": self._payload_manifest_hash,
            "input_hashes": self._dispatch_input_hashes,
            "host_timing": host_timing,
            "reservation_id": reservation_id,
        }
        persist_submit_intent(job_state_path, job_meta)

        logger.info(
            f"KaggleCloudService: Pushing kernel '{expected_kernel_ref}' (Dual-T4 Policy B, Job: {job_type})..."
        )
        t_push = time.monotonic()
        target_kernel_ref = expected_kernel_ref
        reconciled_state: Optional[Dict[str, Any]] = None
        try:
            for attempt in range(4):
                try:
                    target_kernel_ref = client.push_kernel(staging_dir, expected_kernel_ref)
                    break
                except KaggleExecutionError as exc:
                    if getattr(exc, "category", None) == "dataset_propagating" and attempt < 3:
                        logger.info(
                            "Kaggle dataset still propagating for kernel '%s' (attempt %d/4); retrying in 6s...",
                            expected_kernel_ref,
                            attempt + 1,
                        )
                        time.sleep(6)
                        continue
                    raise
        except (KaggleAuthError, KaggleExecutionError, KaggleQuotaExhaustedError):
            release_pre_submit_reservation(project, job_state_path, reservation_id, prior_state)
            raise
        except Exception as exc:
            ambiguous = {
                **job_meta,
                "status": LifecycleState.SUBMIT_UNKNOWN,
                "error": f"Push terminated before confirmation: {exc}",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            self._write_cloud_job_state(job_state_path, ambiguous)
            reconciled_state = reconcile_submit_unknown(client, job_state_path, ambiguous)
            target_kernel_ref = str(reconciled_state.get("kernel_ref") or expected_kernel_ref)
            if reconciled_state["status"] == LifecycleState.FAILED:
                raise KaggleExecutionError(
                    f"Kaggle {job_type} job failed after ambiguous push: {reconciled_state.get('error')}",
                    category="remote_failed",
                    provider="kaggle",
                )
        host_timing["kernel_push_sec"] = round(time.monotonic() - t_push, 4)
        job_meta["kernel_ref"] = target_kernel_ref

        if not wait:
            self._write_cloud_job_state(
                job_state_path,
                {
                    **job_meta,
                    "status": (
                        reconciled_state["status"]
                        if reconciled_state is not None
                        else LifecycleState.DISPATCHED
                    ),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            return {
                "status": "dispatched",
                "kernel_ref": target_kernel_ref,
                "staging_dir": str(staging_dir),
                "job_type": job_type,
                "budget": budget.as_dict(),
            }

        logger.info(f"KaggleCloudService: Waiting for cloud execution of '{target_kernel_ref}'...")
        self._poll_kernel_until_complete(
            target_kernel_ref,
            timeout_seconds=effective_timeout,
            queue_timeout_seconds=budget.queue_timeout_sec,
            queue_notice_seconds=budget.queue_notice_sec,
            poll_interval=poll_interval,
            label=f"Chapter-{chapter_number} {job_type}",
            error_download_dir=staging_dir / "error_out",
            job_state_path=job_state_path,
            job_metadata=job_meta,
        )
        self._write_cloud_job_state(
            job_state_path,
            {
                **job_meta,
                "status": LifecycleState.REMOTE_COMPLETE,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )

        with tempfile.TemporaryDirectory(prefix="manga-kaggle-output-") as download:
            client.download_kernel_output(target_kernel_ref, Path(download))
            if series:
                result = self.sync_series_results(project_slug, chapters, Path(download), job_type=job_type)
            else:
                result = self.sync_results(project_slug, chapter_number, Path(download), job_type=job_type)
        self._write_cloud_job_state(job_state_path, {**job_meta, "status": "synced", "result": result})
        return result

    def dispatch_tts_job(self, project_slug: str, chapter_number: str, **kwargs) -> Dict[str, Any]:
        return self.dispatch_chapter_job(project_slug, chapter_number, job_type="tts", **kwargs)

    def dispatch_render_job(self, project_slug: str, chapter_number: str, **kwargs) -> Dict[str, Any]:
        return self.dispatch_chapter_job(project_slug, chapter_number, job_type="render", **kwargs)

    def dispatch_produce_job(self, project_slug: str, chapter_number: str, **kwargs) -> Dict[str, Any]:
        return self.dispatch_chapter_job(project_slug, chapter_number, job_type="produce", **kwargs)

    def sync_results(
        self,
        project_slug: str,
        chapter_number: Any,
        downloaded_dir: Path,
        job_type: str = "produce",
    ) -> Dict[str, Any]:
        from src.cloud.payload import sync_results

        return sync_results(
            get_project_dir(project_slug),
            [str(chapter_number)],
            downloaded_dir,
            job_type,
            getattr(self, "_payload_manifest_hash", None),
            getattr(self, "_dispatch_input_hashes", None),
        )

    def sync_series_results(
        self,
        project_slug: str,
        chapters: List[str],
        downloaded_dir: Path,
        job_type: str = "produce",
    ) -> Dict[str, Any]:
        from src.cloud.payload import sync_results

        return sync_results(
            get_project_dir(project_slug),
            chapters,
            downloaded_dir,
            job_type,
            getattr(self, "_payload_manifest_hash", None),
            getattr(self, "_dispatch_input_hashes", None),
            series=True,
        )

    def check_chapter_1_preview_approval(self, project_slug: str):
        from src.core.human_review import require_preview_approval

        try:
            require_preview_approval(get_project_dir(project_slug))
            return True, None
        except Exception as exc:
            return False, str(exc)

    def dispatch_series_job(
        self,
        project_slug: str,
        chapters: List[Any],
        **kwargs,
    ) -> Dict[str, Any]:
        numbers = [str(number).strip() for number in chapters]
        if not numbers:
            raise ValueError("Expected at least one chapter")
        return self.dispatch_chapter_job(project_slug, numbers[0], series_chapters=numbers, **kwargs)

    def dispatch_series_chunks(
        self,
        project_slug: str,
        chapters: List[Any],
        *,
        job_type: str = "produce",
        max_chunk_runtime_sec: float = 25200.0,
        max_chunk_payload_mb: float = 15360.0,
        max_chapters_per_chunk: Optional[int] = None,
        compile_feature: bool = False,
        **kwargs,
    ) -> Dict[str, Any]:
        """Executes a multi-chapter series in workload-sized chunks with verified per-chapter resume."""
        from src.cloud.chunking import filter_pending_chapters, plan_series_chunks, write_series_chunk_plan

        numbers = [str(number).strip() for number in chapters]
        if not numbers:
            raise ValueError("Expected at least one chapter")
        project = get_project_dir(project_slug)
        plan_state_path = project / "cloud_series_plan.json"

        verified_done, pending = filter_pending_chapters(project, numbers, job_type, series=True)
        chunks = (
            plan_series_chunks(
                project,
                pending,
                job_type,
                max_chunk_runtime_sec=max_chunk_runtime_sec,
                max_chunk_payload_mb=max_chunk_payload_mb,
                max_chapters_per_chunk=max_chapters_per_chunk,
            )
            if pending
            else []
        )

        chunk_records = [
            {
                **chunk.as_dict(),
                "status": "pending",
            }
            for chunk in chunks
        ]
        write_series_chunk_plan(
            plan_state_path,
            {
                "project_slug": project_slug,
                "job_type": job_type,
                "all_chapters": numbers,
                "skipped_verified": verified_done,
                "chunks": chunk_records,
                "status": "running" if chunks else "synced",
            },
        )

        published: Dict[str, Any] = {}
        for idx, chunk in enumerate(chunks):
            chunk_records[idx]["status"] = "running"
            write_series_chunk_plan(
                plan_state_path,
                {
                    "project_slug": project_slug,
                    "job_type": job_type,
                    "all_chapters": numbers,
                    "skipped_verified": verified_done,
                    "chunks": chunk_records,
                    "status": "running",
                },
            )
            chunk_res = self.dispatch_chapter_job(
                project_slug,
                chunk.chapters[0],
                job_type=job_type,
                series_chapters=list(chunk.chapters),
                wait=True,
                compile_feature=False,
                **kwargs,
            )
            chunk_records[idx]["status"] = "synced"
            if isinstance(chunk_res, dict) and isinstance(chunk_res.get("published"), dict):
                published.update(chunk_res["published"])
            write_series_chunk_plan(
                plan_state_path,
                {
                    "project_slug": project_slug,
                    "job_type": job_type,
                    "all_chapters": numbers,
                    "skipped_verified": verified_done,
                    "chunks": chunk_records,
                    "status": "running",
                },
            )

        verified_after, remaining = filter_pending_chapters(project, numbers, job_type, series=True)
        feature: Dict[str, Any] = {}
        compiled_video: Optional[str] = None
        if compile_feature and job_type != "tts" and not remaining:
            from src.core.workflow import compile_project_chapters

            compile_res = compile_project_chapters(project_slug, chapters=",".join(numbers), output=None)
            feature = {
                key: str(val)
                for key, val in (compile_res or {}).items()
                if isinstance(val, str)
            }
            compiled_video = feature.get("compiled_video") or feature.get("export_video_path")

        write_series_chunk_plan(
            plan_state_path,
            {
                "project_slug": project_slug,
                "job_type": job_type,
                "all_chapters": numbers,
                "skipped_verified": verified_done,
                "verified_after": verified_after,
                "chunks": chunk_records,
                "feature": feature,
                "status": "synced" if not remaining else "partial",
            },
        )
        return {
            "status": "success" if not remaining else "partial",
            "job_type": f"{job_type}_series",
            "project_slug": project_slug,
            "chapters": numbers,
            "skipped_verified": verified_done,
            "dispatched_chunks": [chunk.chapters for chunk in chunks],
            "published": published,
            "feature": feature,
            "compiled_video": compiled_video,
            "plan_path": str(plan_state_path),
        }

    def resume_series_chunks(self, project_slug: str, chapters: Optional[List[Any]] = None, **kwargs) -> Dict[str, Any]:
        """Resumes an interrupted chunked series run from ``cloud_series_plan.json``."""
        project = get_project_dir(project_slug)
        plan_state_path = project / "cloud_series_plan.json"
        if chapters is None:
            saved = read_json(plan_state_path)
            chapters = list(saved["all_chapters"])
            kwargs.setdefault("job_type", saved.get("job_type", "produce"))
        return self.dispatch_series_chunks(project_slug, chapters, **kwargs)

    def pull(self, project_slug: str, chapter_number: Optional[str] = None) -> Dict[str, Any]:
        """Resumes a dispatched cloud job, awaits completion, downloads, validates, and syncs outputs."""
        target_dir = get_chapter_dir(project_slug, chapter_number) if chapter_number else get_project_dir(project_slug)
        state_path = target_dir / "cloud_job.json"
        state = load_cloud_job_state(state_path)
        if state.project_slug != project_slug:
            raise ValueError("Cloud job project mismatch")
        if state.status == "synced" and state.result is not None:
            return state.result

        client, restore_fn = self.bind_job_account(state.account_name, state.username)
        if client is None or state.username not in ("", "unknown", None) and self.get_username() != state.username:
            if callable(restore_fn):
                restore_fn()
            raise RuntimeError(
                f"Authenticate the Kaggle account '{state.account_name}' ({state.username}) that dispatched this job"
            )
        try:
            self._payload_manifest_hash = state.payload_manifest_sha256
            self._dispatch_input_hashes = state.input_hashes
            job_dict = state.model_dump(mode="json")

            if str(state.status or "").upper() == LifecycleState.SUBMIT_UNKNOWN:
                job_dict = reconcile_submit_unknown(self._client(), state_path, job_dict)
                if job_dict["status"] == LifecycleState.FAILED:
                    raise KaggleExecutionError(
                        f"Kaggle job '{state.kernel_ref}' failed: {job_dict.get('error')}",
                        category="remote_failed",
                        provider="kaggle",
                    )

            saved_budget = job_dict.get("budget") or {}
            if saved_budget.get("client_poll_timeout_sec"):
                poll_timeout = int(saved_budget["client_poll_timeout_sec"])
                queue_notice = float(saved_budget.get("queue_notice_sec", 300.0))
                queue_timeout = saved_budget.get("queue_timeout_sec")
            else:
                budget = estimate_workload_budgets(
                    get_project_dir(project_slug),
                    state.chapters or [state.chapter_number or "1"],
                    state.job_type,
                    gpu=True,
                )
                poll_timeout = budget.client_poll_timeout_sec
                queue_notice = budget.queue_notice_sec
                queue_timeout = budget.queue_timeout_sec

            self._poll_kernel_until_complete(
                str(job_dict.get("kernel_ref") or state.kernel_ref),
                timeout_seconds=poll_timeout,
                queue_timeout_seconds=queue_timeout,
                queue_notice_seconds=queue_notice,
                poll_interval=6,
                job_state_path=state_path,
                job_metadata=job_dict,
            )
            self._write_cloud_job_state(
                state_path,
                {
                    **job_dict,
                    "status": LifecycleState.REMOTE_COMPLETE,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            with tempfile.TemporaryDirectory(prefix="manga-kaggle-output-") as download:
                self._client().download_kernel_output(
                    str(job_dict.get("kernel_ref") or state.kernel_ref),
                    Path(download),
                )
                if state.series:
                    result = self.sync_series_results(project_slug, state.chapters, Path(download), job_type=state.job_type)
                else:
                    result = self.sync_results(project_slug, state.chapter_number, Path(download), state.job_type)
            self._write_cloud_job_state(state_path, {**job_dict, "status": "synced", "result": result})
            return result
        finally:
            if callable(restore_fn):
                restore_fn()

    def reconcile_job(self, project_slug: str, chapter_number: Optional[str] = None) -> Dict[str, Any]:
        """Refreshes ``cloud_job.json`` status from remote Kaggle kernel status via its original account."""
        target_dir = get_chapter_dir(project_slug, chapter_number) if chapter_number else get_project_dir(project_slug)
        state_path = target_dir / "cloud_job.json"
        state = load_cloud_job_state(state_path)
        if state.project_slug != project_slug:
            raise ValueError("Cloud job project mismatch")
        client, restore_fn = self.bind_job_account(state.account_name, state.username)
        if client is None:
            raise RuntimeError(
                f"Authenticate the Kaggle account '{state.account_name}' ({state.username}) that owns job '{state.kernel_ref}'"
            )
        try:
            raw_status, norm, fail_msg = client.get_kernel_status(state.kernel_ref)
        finally:
            if callable(restore_fn):
                restore_fn()
        mapped = normalize_remote_status(norm or raw_status)
        if mapped == "COMPLETE":
            status_val = (
                LifecycleState.SYNCED
                if str(state.status or "") == LifecycleState.SYNCED
                else LifecycleState.REMOTE_COMPLETE
            )
        elif mapped == "FAILED":
            status_val = LifecycleState.FAILED
        elif mapped in {"QUEUED", "RUNNING"}:
            status_val = mapped
        else:
            status_val = state.status
        updated = {
            **state.model_dump(mode="json"),
            "status": status_val,
            "last_remote_status": mapped,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if fail_msg:
            updated["error"] = str(fail_msg)
        elif status_val == LifecycleState.FAILED:
            updated["error"] = f"status={raw_status}"
        self._write_cloud_job_state(state_path, updated)
        return updated

    def _wait_for_dataset_ready(
        self,
        ds_ref: str,
        *,
        expected_file: str = "payload.tar.gz",
        timeout_sec: float = 600.0,
        poll_interval: float = 5.0,
    ) -> None:
        self._client().wait_for_dataset_ready(
            ds_ref,
            expected_file=expected_file,
            timeout_sec=timeout_sec,
            poll_interval=poll_interval,
            sleep_fn=time.sleep,
        )

    def _poll_kernel_until_complete(
        self,
        target_kernel_ref: str,
        *,
        timeout_seconds: int = 3600,
        queue_timeout_seconds: Optional[float] = 300.0,
        queue_notice_seconds: Optional[float] = 300.0,
        poll_interval: int = 6,
        heartbeat_interval_sec: float = 30.0,
        label: str = "Series",
        error_download_dir: Optional[Path] = None,
        job_state_path: Optional[Path] = None,
        job_metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, float]:
        client = self._client()
        t0 = time.monotonic()
        queued_since_iso = datetime.now(timezone.utc).isoformat()
        started_running_iso: Optional[str] = None
        last_status: Optional[str] = None
        last_norm: str = "QUEUED"
        last_heartbeat = t0
        left_queue_at: Optional[float] = None
        queue_notice_emitted = False
        completed_cleanly = False
        base_meta = dict(job_metadata or {})

        def _persist(phase: str, extra: Optional[Dict[str, Any]] = None) -> None:
            now_mono = time.monotonic()
            queue_elapsed = (left_queue_at if left_queue_at is not None else now_mono) - t0
            payload = {
                **base_meta,
                "kernel_ref": target_kernel_ref,
                "account_name": getattr(self, "account_name", base_meta.get("account_name", "unknown")),
                "status": phase,
                "queued_since": queued_since_iso,
                "started_running_at": started_running_iso,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "queue_elapsed_sec": round(max(0.0, queue_elapsed), 2),
                "total_elapsed_sec": round(max(0.0, now_mono - t0), 2),
            }
            if extra:
                payload.update(extra)
            self._write_cloud_job_state(job_state_path, payload)

        _persist("QUEUED")

        while time.monotonic() - t0 < timeout_seconds:
            if poll_interval > 0:
                time.sleep(poll_interval)
            current_status, norm, failure_message = client.get_kernel_status(target_kernel_ref)

            now = time.monotonic()
            elapsed = now - t0
            last_norm = normalize_remote_status(norm or current_status)

            if current_status != last_status:
                logger.info(f"Kaggle {label} Status [{int(elapsed)}s]: {current_status}")
                last_status = current_status
                last_heartbeat = now
                if left_queue_at is None and norm != "QUEUED":
                    left_queue_at = now
                    started_running_iso = datetime.now(timezone.utc).isoformat()
                _persist(norm)
            elif now - last_heartbeat >= heartbeat_interval_sec:
                if norm == "QUEUED" and queue_timeout_seconds:
                    logger.info(
                        f"Kaggle {label} still QUEUED [{int(elapsed)}s/{int(queue_timeout_seconds)}s] "
                        f"({target_kernel_ref}, account={getattr(self, 'account_name', 'unknown')})"
                    )
                else:
                    logger.info(f"Kaggle {label} Status [{int(elapsed)}s]: {current_status}")
                last_heartbeat = now
                _persist(norm)

            if left_queue_at is None and norm != "QUEUED":
                left_queue_at = now
                started_running_iso = started_running_iso or datetime.now(timezone.utc).isoformat()

            if norm in {"COMPLETE", "COMPLETED"}:
                logger.info(f"Kaggle {label} COMPLETE in {int(elapsed)}s!")
                completed_cleanly = True
                _persist(LifecycleState.REMOTE_COMPLETE, {"last_remote_status": "COMPLETE"})
                break
            if any(err_kw in current_status.upper() for err_kw in ("FAILED", "ERROR", "CANCEL")):
                fail_msg = failure_message or f"status={current_status}"
                if error_download_dir is not None:
                    try:
                        client.download_kernel_output(target_kernel_ref, error_download_dir)
                        for log_f in error_download_dir.glob("*.log"):
                            logger.error(f"Kaggle Cloud Error Log:\n{log_f.read_text(errors='ignore')[-3000:]}")
                    except Exception as log_err:
                        logger.warning(f"Could not retrieve error log: {log_err}")
                _persist("FAILED", {"error": str(fail_msg)})
                raise KaggleExecutionError(
                    f"Kaggle {label} job failed: {fail_msg}",
                    category="remote_failed",
                    provider="kaggle",
                )

            if (
                norm == "QUEUED"
                and queue_notice_seconds is not None
                and float(queue_notice_seconds) > 0
                and elapsed >= float(queue_notice_seconds)
                and not queue_notice_emitted
            ):
                queue_notice_emitted = True
                logger.warning(
                    f"Kaggle {label} queue stall notice [{int(elapsed)}s]: '{target_kernel_ref}' still QUEUED "
                    f"(account={getattr(self, 'account_name', 'unknown')})"
                )
                _persist("QUEUED", {"queue_notice_emitted": True})

            if (
                norm == "QUEUED"
                and queue_timeout_seconds is not None
                and float(queue_timeout_seconds) > 0
                and elapsed >= float(queue_timeout_seconds)
            ):
                _persist(
                    LifecycleState.QUEUE_TIMEOUT,
                    {
                        "queue_timeout_seconds": float(queue_timeout_seconds),
                        "queue_notice_emitted": queue_notice_emitted,
                        "last_remote_status": last_norm,
                    },
                )
                raise KaggleQueueTimeoutError(
                    f"Kaggle Cloud job '{target_kernel_ref}' (account '{getattr(self, 'account_name', 'unknown')}') "
                    f"remained QUEUED for {int(elapsed)}s (limit={int(queue_timeout_seconds)}s). "
                    "Remote kernel may still be queued/running; cancel via Kaggle UI and reconcile, or pull before resubmitting.",
                    category="queue_timeout",
                    provider="kaggle",
                )

        if not completed_cleanly:
            _persist(
                LifecycleState.CLIENT_TIMEOUT,
                {
                    "error": f"Client poll timed out after {timeout_seconds}s with status '{last_status}'",
                    "last_remote_status": last_norm,
                },
            )
            raise KaggleClientPollTimeoutError(
                f"Kaggle Cloud job '{target_kernel_ref}' client poll timed out after {timeout_seconds}s "
                f"while remote status was '{last_status}'. Use 'cloud pull' or 'reconcile' to resume.",
                category="client_poll_timeout",
                provider="kaggle",
            )

        t_done = time.monotonic()
        queue_sec = max(0.0, (left_queue_at if left_queue_at is not None else t_done) - t0)
        return {
            "queue_sec": round(queue_sec, 4),
            "remote_wall_sec": round(t_done - t0, 4),
        }
