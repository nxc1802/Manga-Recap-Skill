"""
src/cloud/kaggle_service.py
Kaggle Cloud adapter for canonical single-chapter and Dual-T4 series execution.
"""

from __future__ import annotations

import json
import contextlib
import io
import threading
import os
import re
import shutil
import tarfile
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from src.cloud.account_fallback import with_account_fallback
from src.core.logger import logger
from src.core.storage import atomic_write_json, get_chapter_dir, get_project_dir, read_json


class KaggleQueueTimeoutError(TimeoutError):
    """Raised when a Kaggle kernel stays QUEUED past queue_timeout_seconds."""


class KaggleQuotaExhaustedError(RuntimeError):
    """Raised when a Kaggle account has exhausted its weekly GPU quota."""


def _parse_kaggle_duration_seconds(raw: Any) -> float:
    """Parses Kaggle timedelta objects or duration strings into seconds."""
    if raw is None:
        return 0.0
    if isinstance(raw, timedelta):
        return max(0.0, float(raw.total_seconds()))
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return max(0.0, float(raw))
    match = re.match(r"^\s*(\d+(?:\.\d+)?)", str(raw))
    return max(0.0, float(match.group(1))) if match else 0.0



def safe_extract_tar(tar: tarfile.TarFile, target_dir: Union[str, Path]) -> None:
    """
    Safely extracts a tar archive preventing directory traversal (zip-slip / tar-slip).
    Rejects absolute member names and any path navigating outside target_dir.
    """
    resolved_target = Path(target_dir).resolve()
    seen = set()
    for member in tar.getmembers():
        # Python 3.10 lacks the data extraction filter. Reject links and special
        # entries before extraction on every supported Python version.
        if not (member.isfile() or member.isdir()):
            raise ValueError(f"Security error: Unsupported tar member: '{member.name}'")
        normalized = (resolved_target / member.name).resolve()
        if normalized in seen:
            raise ValueError(f"Security error: Duplicate tar member: '{member.name}'")
        seen.add(normalized)
        # Prevent absolute paths or path traversal via ../
        if member.name.startswith("/") or ".." in member.name.split("/"):
            raise ValueError(
                f"Security error: Attempted Path Traversal in Tar File: '{member.name}'"
            )
        member_path = (resolved_target / member.name).resolve()
        try:
            member_path.relative_to(resolved_target)
        except ValueError:
            raise ValueError(
                f"Security error: Attempted Path Traversal in Tar File: '{member.name}'"
            )
    try:
        tar.extractall(path=resolved_target, filter="data")
    except TypeError:
        tar.extractall(path=resolved_target)


class KaggleCloudService:
    """
    Manages remote batch rendering and TTS synthesis execution on Kaggle Cloud.
    Supports clean job separation:
    - 'tts': Audio synthesis only, syncing audio clips and script without video rendering.
    - 'render': Hardware video rendering only, reusing pre-existing audio cache and skipping TTS.
    - 'produce': Full end-to-end production (TTS + Render).
    """

    DEFAULT_KERNEL_SLUG = "manga-recap-cloud-runner"

    @staticmethod
    def _require_kernel_submission(result):
        error = result.get("error") if isinstance(result, dict) else getattr(result, "error", None)
        if not result or (isinstance(error, (str, bool)) and error):
            raise RuntimeError("Kaggle rejected kernel submission")

    _auth_lock = threading.RLock()

    def __init__(self, api_token: Optional[str] = None):
        from src.cloud.account_manager import KaggleAccountManager
        self._accounts = [("explicit", api_token)] if api_token else KaggleAccountManager().candidates()
        self._api = None
        self._username = None
        self._account_index = 0
        self.account_name, self.api_token = "unconfigured", None
        for index in range(len(self._accounts)):
            self._select_account(index)
            if self.is_authenticated:
                break

    def _select_account(self, index):
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
            previous = os.environ.get("KAGGLE_API_TOKEN")
            try:
                if self.api_token:
                    os.environ["KAGGLE_API_TOKEN"] = self.api_token
                # Kaggle package import also authenticates its global client.
                # Keep SDK diagnostics off CLI stdout and out of secret logs.
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    from kaggle.api.kaggle_api_extended import KaggleApi
                    api = KaggleApi()
                    api.authenticate()
                if self.api_token and api.config_values.get(api.CONFIG_NAME_TOKEN) != self.api_token:
                    raise RuntimeError("Requested token was not authenticated")
                username = api.config_values.get(api.CONFIG_NAME_USER)
                if not username:
                    raise RuntimeError("Kaggle username unavailable")
                self._api, self._username = api, username
                logger.info("Kaggle account '%s' authenticated", self.account_name)
            except (Exception, SystemExit):
                self._api = None
                logger.warning("Kaggle account '%s' could not authenticate", self.account_name)
            finally:
                if previous is None:
                    os.environ.pop("KAGGLE_API_TOKEN", None)
                else:
                    os.environ["KAGGLE_API_TOKEN"] = previous

    @property
    def is_authenticated(self) -> bool:
        return self._api is not None

    def get_username(self) -> str:
        return self._username or "unknown"

    def get_gpu_quota_status(self, min_required_sec: float = 120.0) -> Optional[Dict[str, Any]]:
        """Queries Kaggle weekly GPU accelerator quota when supported by the client."""
        if not self._api or not hasattr(self._api, "build_kaggle_client"):
            return None
        try:
            with self._api.build_kaggle_client() as client:
                raw = client.kernels.kernels_api_client.get_accelerator_quota_statistics()
        except Exception:
            return None

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
        allowed_sec = _parse_kaggle_duration_seconds(raw_allowed)
        used_sec = _parse_kaggle_duration_seconds(raw_used)
        reserved_sec = (
            _parse_kaggle_duration_seconds(raw_reserved)
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
            "account_name": getattr(self, "account_name", "unknown"),
            "username": self.get_username(),
            "allowed_sec": round(allowed_sec, 2),
            "used_sec": round(used_sec, 2),
            "reserved_sec": round(reserved_sec, 2),
            "remaining_sec": round(remaining_sec, 2),
            "remaining_hours": round(remaining_sec / 3600.0, 2),
            "exhausted": allowed_sec > 0 and remaining_sec < float(min_required_sec),
            "quota_refresh_time": refresh_str,
        }

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
        if state_path is None:
            return
        try:
            if state_path.parent.is_dir():
                atomic_write_json(state_path, payload)
        except Exception as exc:
            logger.debug("Could not persist cloud_job.json (%s): %s", state_path, exc)

    def package_chapter_payload(self, project_slug, chapter_number, out_tar_path, job_type="produce", *, series=False):
        from src.cloud.payload import package_payload

        path, self._payload_manifest_hash = package_payload(
            get_project_dir(project_slug), chapter_number, out_tar_path, job_type, series=series,
        )
        with tarfile.open(path) as archive:
            manifest = json.load(archive.extractfile("payload_manifest.json"))
        self._dispatch_input_hashes = {relative.removeprefix("project/"): value
            for relative, value in manifest["files"].items()
            if relative.startswith("project/") and not relative.endswith("/chapter.json")}
        return path

    def generate_worker_script(
        self,
        project_slug: str,
        chapter_number: str,
        render_workers: int = 4,
        encoder_preference: str = "auto",
        job_type: str = "produce",
        video_mode: Optional[str] = None,
        burn_subtitles: Optional[bool] = None,
        fps: Optional[int] = None,
        target_width: Optional[int] = None,
        target_height: Optional[int] = None,
        compile_feature: bool = False,
    ) -> str:
        from src.core.series_setup import require_series_setup
        require_series_setup(project_slug)
        if job_type not in {"tts", "render", "produce"}:
            raise ValueError("Unknown cloud job type")
        settings = read_json(get_project_dir(project_slug) / "project.json")["render_settings"]
        for key, value in {"target_width": target_width, "target_height": target_height, "fps": fps,
                           "burn_subtitles": burn_subtitles, "video_mode": video_mode}.items():
            if value is not None and value != settings[key]:
                raise ValueError(f"Cloud override conflicts with confirmed setup: {key}")
        digest = getattr(self, "_payload_manifest_hash", None)
        if not digest:
            raise ValueError("Package and hash the complete runtime before generating a worker")
        encoder = "h264_nvenc" if encoder_preference in {"auto", "nvenc", "h264_nvenc"} else encoder_preference
        if encoder not in {"libx264", "h264_nvenc"}:
            raise ValueError("Unsupported cloud encoder")
        lines = [
            "import sys, json, hashlib, tarfile, shutil, subprocess",
            "from pathlib import Path",
            "",
            "def main():",
            "    sys.dont_write_bytecode = True",
            "    root = Path('/kaggle/working/runtime')",
            "    root.mkdir()",
            "    try:",
            "        payload, = Path('/kaggle/input').rglob('payload.tar.gz')",
            "        with tarfile.open(payload) as archive:",
            "            seen = set()",
            "            for entry in archive.getmembers():",
            "                target = (root / entry.name).resolve()",
            "                if not target.is_relative_to(root) or not entry.isfile() or target in seen:",
            "                    raise RuntimeError('Unsafe payload member')",
            "                seen.add(target)",
            "            archive.extractall(root)",
            f"        expected = {digest!r}",
            "        manifest_path = root / 'payload_manifest.json'",
            "        if hashlib.sha256(manifest_path.read_bytes()).hexdigest() != expected:",
            "            raise RuntimeError('Manifest changed')",
            "        manifest = json.loads(manifest_path.read_text())",
            "        files = {p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file() and p != manifest_path}",
            "        if files != set(manifest['files']): raise RuntimeError('Payload inventory mismatch')",
            "        for name, digest in manifest['files'].items():",
            "            with (root / name).open('rb') as f:",
            "                checksum = hashlib.sha256()",
            "                while block := f.read(1 << 20): checksum.update(block)",
            "            if checksum.hexdigest() != digest: raise RuntimeError('Payload hash mismatch')",
            "        subprocess.run([sys.executable, '-m', 'pip', 'install', '-r', str(root / 'requirements.txt')], check=True)",
            "        sys.path.insert(0, str(root))",
            "        from src.cloud.dual_gpu_worker import run",
            f"        run(root, expected, Path('/kaggle/working/results'), encoder={encoder!r},",
            f"            render_workers={render_workers!r}, compile_feature={compile_feature!r})",
            "    finally:",
            "        shutil.rmtree(root, ignore_errors=True)",
            "",
            "if __name__ == '__main__':",
            "    main()",
        ]
        return "\n".join(lines) + "\n"

    def dispatch_chapter_job(
        self,
        project_slug: str,
        chapter_number: str,
        kernel_slug: Optional[str] = None,
        gpu: bool = True,
        wait: bool = True,
        poll_interval: int = 6,
        timeout_seconds: int = 1800,
        queue_timeout_seconds: Optional[float] = 300.0,
        job_type: str = "produce",
        render_workers: int = 4,
        encoder_preference: str = "auto",
        video_mode: Optional[str] = None,
        burn_subtitles: Optional[bool] = None,
        fps: Optional[int] = None,
        target_width: Optional[int] = None,
        target_height: Optional[int] = None,
        series_chapters: Optional[List[str]] = None,
        compile_feature: bool = False,
    ) -> Dict[str, Any]:
        """
        Dispatches a chapter production job to Kaggle Cloud, monitors progress, and returns result.
        Enforces:
        - True job separation ('tts', 'render', 'produce').
        - Non-CC0 metadata license for unconfirmed manga assets.
        - Strict timeout handling (including fast queue_timeout_seconds on QUEUED stalls).
        - No credentials exposure.
        """
        if job_type not in {"tts", "render", "produce"}:
            raise ValueError("Unknown cloud job type")
        if not self._api:
            raise RuntimeError("Kaggle API is not authenticated")
        series = series_chapters is not None
        chapters = list(series_chapters) if series else [str(chapter_number)]
        chapter = get_chapter_dir(project_slug, chapter_number)
        if gpu:
            quota = self.get_gpu_quota_status()
            if quota and quota.get("exhausted"):
                raise KaggleQuotaExhaustedError(
                    f"Kaggle account '{getattr(self, 'account_name', 'unknown')}' ({self.get_username()}) "
                    f"has exhausted its weekly GPU quota ({quota['used_sec']:.1f}s / {quota['allowed_sec']:.1f}s used, "
                    f"refreshes {quota.get('quota_refresh_time') or 'next cycle'})."
                )

        kernel_slug = (kernel_slug or f"mr-{project_slug[:12]}-c{chapter_number}-{int(time.time())%100000}".lower().replace("_", "-"))[:45].rstrip("-")
        staging_dir = Path(tempfile.mkdtemp(prefix="manga-kaggle-"))
        payload_tar = staging_dir / "payload.tar.gz"
        self.package_chapter_payload(project_slug, chapters if series else chapter_number,
                                     payload_tar, job_type=job_type, series=series)

        worker_py = staging_dir / "worker.py"
        worker_code = self.generate_worker_script(
            project_slug=project_slug,
            chapter_number=chapter_number,
            render_workers=render_workers,
            encoder_preference=encoder_preference,
            job_type=job_type,
            video_mode=video_mode,
            burn_subtitles=burn_subtitles,
            fps=fps,
            target_width=target_width,
            target_height=target_height,
            compile_feature=compile_feature,
        )
        worker_py.write_text(worker_code, encoding="utf-8")

        # A fresh private dataset per job avoids racing a mutable shared payload.
        dataset_slug = f"mr-payload-{self._payload_manifest_hash[:12]}-{time.time_ns() % 1000000000}"
        ds_staging = staging_dir / "dataset"
        ds_staging.mkdir()
        shutil.copy2(payload_tar, ds_staging / "payload.tar.gz")
        ds_ref = f"{self._username}/{dataset_slug}"
        atomic_write_json(ds_staging / "dataset-metadata.json", {
            "title": dataset_slug, "id": ds_ref, "licenses": [{"name": "other"}],
        })
        self._api.dataset_create_new(str(ds_staging), public=False, quiet=True)
        self._wait_for_dataset_ready(ds_ref, expected_file="payload.tar.gz", timeout_sec=300.0)

        metadata = {
            "id": f"{self._username}/{kernel_slug}",
            "title": kernel_slug,
            "code_file": "worker.py",
            "language": "python",
            "kernel_type": "script",
            "is_private": True,
            "enable_gpu": gpu,
            "enable_tpu": False,
            "enable_internet": True,
            "machine_shape": "NvidiaTeslaT4" if gpu else "None",
            "dataset_sources": [f"{self._username}/{dataset_slug}"],
            "competition_sources": [],
            "kernel_sources": [],
            "model_sources": [],
        }

        metadata_file = staging_dir / "kernel-metadata.json"
        metadata_file.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

        logger.info(f"KaggleCloudService: Pushing kernel '{self._username}/{kernel_slug}' (GPU: {gpu}, Job: {job_type})...")
        push_res = self._api.kernels_push(str(staging_dir))
        self._require_kernel_submission(push_res)
        logger.info(f"KaggleCloudService: Kernel push result: {push_res}")

        # Resolve actual slug assigned by Kaggle
        assigned_ref = getattr(push_res, "ref", None)
        if assigned_ref:
            target_kernel_ref = assigned_ref.replace("/code/", "").strip()
        else:
            target_kernel_ref = f"{self._username}/{kernel_slug}"

        project = get_project_dir(project_slug)
        job_state_path = (project if series else chapter) / "cloud_job.json"
        job_meta = {
            "project_slug": project_slug,
            "chapter_number": str(chapter_number),
            "job_type": job_type,
            "kernel_ref": target_kernel_ref,
            "dataset_ref": ds_ref,
            "account_name": getattr(self, "account_name", "unknown"),
            "username": self.get_username(),
            "gpu": gpu,
            "series": series,
            "chapters": chapters,
            "payload_manifest_sha256": self._payload_manifest_hash,
            "input_hashes": self._dispatch_input_hashes,
        }
        if not wait:
            self._write_cloud_job_state(
                job_state_path,
                {
                    **job_meta,
                    "status": "dispatched",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            return {
                "status": "dispatched",
                "kernel_ref": target_kernel_ref,
                "staging_dir": str(staging_dir),
                "job_type": job_type,
            }

        logger.info(f"KaggleCloudService: Waiting for cloud execution of '{target_kernel_ref}'...")
        self._poll_kernel_until_complete(
            target_kernel_ref,
            timeout_seconds=timeout_seconds,
            queue_timeout_seconds=queue_timeout_seconds if gpu else None,
            poll_interval=poll_interval,
            label=f"Chapter-{chapter_number} {job_type}",
            error_download_dir=staging_dir / "error_out",
            job_state_path=job_state_path,
            job_metadata=job_meta,
        )

        with tempfile.TemporaryDirectory(prefix="manga-kaggle-output-") as download:
            self._api.kernels_output(target_kernel_ref, download)
            if series:
                result = self.sync_series_results(project_slug, chapters, Path(download))
            else:
                result = self.sync_results(project_slug, chapter_number, Path(download), job_type=job_type)
        self._write_cloud_job_state(job_state_path, {**job_meta, "status": "synced", "result": result})
        return result

    @with_account_fallback
    def dispatch_chapter_job_with_fallback(self, *args, **kwargs) -> Dict[str, Any]:
        """Dispatches a chapter job, rotating across configured Kaggle accounts on quota/queue failure."""
        return self.dispatch_chapter_job(*args, **kwargs)

    def dispatch_tts_job(self, project_slug: str, chapter_number: str, **kwargs) -> Dict[str, Any]:
        """Dispatches a dedicated cloud TTS synthesis job."""
        return self.dispatch_chapter_job(project_slug, chapter_number, job_type="tts", **kwargs)

    def dispatch_render_job(self, project_slug: str, chapter_number: str, **kwargs) -> Dict[str, Any]:
        """Dispatches a dedicated cloud video render job reusing pre-existing audio cache."""
        return self.dispatch_chapter_job(project_slug, chapter_number, job_type="render", **kwargs)

    def dispatch_produce_job(self, project_slug: str, chapter_number: str, **kwargs) -> Dict[str, Any]:
        """Dispatches a full cloud production job (TTS + Render)."""
        return self.dispatch_chapter_job(project_slug, chapter_number, job_type="produce", **kwargs)

    def sync_results(self, project_slug, chapter_number, downloaded_dir, job_type="produce"):
        from src.cloud.payload import sync_results

        return sync_results(get_project_dir(project_slug), [str(chapter_number)], downloaded_dir, job_type,
                            getattr(self, "_payload_manifest_hash", None),
                            getattr(self, "_dispatch_input_hashes", None))

    def sync_series_results(self, project_slug, chapters, downloaded_dir):
        from src.cloud.payload import sync_results

        return sync_results(get_project_dir(project_slug), chapters, downloaded_dir, "produce",
                            getattr(self, "_payload_manifest_hash", None),
                            getattr(self, "_dispatch_input_hashes", None), series=True)

    def check_chapter_1_preview_approval(self, project_slug, skip_preview=False):
        from src.core.human_review import require_preview_approval

        try:
            require_preview_approval(get_project_dir(project_slug))
            return True, None
        except Exception as exc:
            return False, str(exc)

    def dispatch_series_job(self, project_slug, chapters, *, skip_preview=False, **kwargs):
        numbers = [str(number).strip() for number in chapters]
        if not numbers:
            raise ValueError("Expected at least one chapter")
        return self.dispatch_chapter_job(project_slug, numbers[0], series_chapters=numbers, **kwargs)

    @with_account_fallback
    def dispatch_series_job_with_fallback(self, *args, **kwargs):
        return self.dispatch_series_job(*args, **kwargs)

    def pull(self, project_slug, chapter_number=None):
        chapter = get_chapter_dir(project_slug, chapter_number) if chapter_number else get_project_dir(project_slug)
        state = read_json(chapter / "cloud_job.json")
        if state["project_slug"] != project_slug:
            raise ValueError("Cloud job project mismatch")
        if state.get("status") == "synced":
            return state["result"]
        if state["username"] != self.get_username():
            for index, account in enumerate(self._accounts):
                if account[0] == state["account_name"]:
                    self._select_account(index)
                    break
        if state["username"] != self.get_username() or not self.is_authenticated:
            raise RuntimeError("Authenticate the Kaggle account that dispatched this job")
        self._payload_manifest_hash = state["payload_manifest_sha256"]
        self._dispatch_input_hashes = state["input_hashes"]
        self._poll_kernel_until_complete(state["kernel_ref"], timeout_seconds=1800,
                                         queue_timeout_seconds=300, poll_interval=6)
        with tempfile.TemporaryDirectory(prefix="manga-kaggle-output-") as download:
            self._api.kernels_output(state["kernel_ref"], download)
            if state["series"]:
                result = self.sync_series_results(project_slug, state["chapters"], Path(download))
            else:
                result = self.sync_results(project_slug, state["chapter_number"], Path(download), state["job_type"])
        self._write_cloud_job_state(chapter / "cloud_job.json", {**state, "status": "synced", "result": result})
        return result

    def _wait_for_dataset_ready(
        self,
        ds_ref: str,
        *,
        expected_file: str = "payload.tar.gz",
        timeout_sec: float = 600.0,
        poll_interval: float = 5.0,
    ) -> None:
        ready = False
        started = time.monotonic()
        while time.monotonic() - started < timeout_sec:
            if poll_interval > 0:
                time.sleep(poll_interval)
            try:
                status = self._api.dataset_status(ds_ref)
            except Exception as err:
                logger.info(f"Waiting for Kaggle series dataset propagation ({ds_ref}): {err}")
                continue
            status = getattr(status, "status", status)
            if isinstance(status, dict):
                status = status.get("status", "")
            norm = str(status).strip().lower().split(".")[-1]
            if norm == "ready":
                try:
                    entries = getattr(self._api.dataset_list_files(ds_ref), "dataset_files", [])
                    if any(entry.name == expected_file for entry in entries) or entries:
                        ready = True
                        break
                except Exception:
                    pass
            if str(status).strip().lower() in {"error", "failed"}:
                raise RuntimeError(f"Kaggle series payload dataset failed: {status}")
        if not ready:
            raise TimeoutError("Kaggle series payload dataset did not become ready; kernel not launched")

    def _poll_kernel_until_complete(
        self,
        target_kernel_ref: str,
        *,
        timeout_seconds: int = 3600,
        queue_timeout_seconds: Optional[float] = 300.0,
        poll_interval: int = 6,
        heartbeat_interval_sec: float = 30.0,
        label: str = "Series",
        error_download_dir: Optional[Path] = None,
        job_state_path: Optional[Path] = None,
        job_metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, float]:
        t0 = time.monotonic()
        queued_since_iso = datetime.now(timezone.utc).isoformat()
        started_running_iso: Optional[str] = None
        last_status: Optional[str] = None
        last_heartbeat = t0
        left_queue_at: Optional[float] = None
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
            status_obj = self._api.kernels_status(target_kernel_ref)
            current_status = getattr(status_obj, "status", str(status_obj))
            if isinstance(current_status, dict):
                current_status = current_status.get("status", "UNKNOWN")

            now = time.monotonic()
            elapsed = now - t0
            norm = str(current_status).strip().upper().split(".")[-1]

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
                _persist("COMPLETE")
                break
            if any(err_kw in str(current_status).upper() for err_kw in ("FAILED", "ERROR", "CANCEL")):
                fail_msg = getattr(status_obj, "failureMessage", None) or f"status={current_status}"
                if error_download_dir is not None:
                    try:
                        error_download_dir.mkdir(parents=True, exist_ok=True)
                        self._api.kernels_output(target_kernel_ref, str(error_download_dir))
                        for log_f in error_download_dir.glob("*.log"):
                            logger.error(f"Kaggle Cloud Error Log:\n{log_f.read_text(errors='ignore')[-3000:]}")
                    except Exception as log_err:
                        logger.warning(f"Could not retrieve error log: {log_err}")
                _persist("FAILED", {"error": str(fail_msg)})
                raise RuntimeError(f"Kaggle {label} job failed: {fail_msg}")

            if (
                norm == "QUEUED"
                and queue_timeout_seconds is not None
                and float(queue_timeout_seconds) > 0
                and elapsed >= float(queue_timeout_seconds)
            ):
                _persist("QUEUE_TIMEOUT", {"queue_timeout_seconds": float(queue_timeout_seconds)})
                raise KaggleQueueTimeoutError(
                    f"Kaggle Cloud job '{target_kernel_ref}' (account '{getattr(self, 'account_name', 'unknown')}') "
                    f"remained QUEUED for {int(elapsed)}s (limit={int(queue_timeout_seconds)}s). "
                    "Switch Kaggle account or fallback to local execution (--local / h264_videotoolbox)."
                )

        if not completed_cleanly:
            _persist("TIMEOUT", {"error": f"Timed out after {timeout_seconds}s with status '{last_status}'"})
            raise TimeoutError(
                f"Kaggle Cloud job '{target_kernel_ref}' timed out after {timeout_seconds}s "
                f"with status '{last_status}'. Output download aborted."
            )

        t_done = time.monotonic()
        queue_sec = max(0.0, (left_queue_at if left_queue_at is not None else t_done) - t0)
        return {
            "queue_sec": round(queue_sec, 4),
            "remote_wall_sec": round(t_done - t0, 4),
        }
