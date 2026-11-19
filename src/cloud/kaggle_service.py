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
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from src.cloud.account_fallback import with_account_fallback
from src.core.logger import logger
from src.core.schemas import RecapScript
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

    def package_chapter_payload(
        self,
        project_slug: str,
        chapter_number: str,
        out_tar_path: Union[str, Path],
        job_type: str = "produce",
    ) -> Path:
        from src.cloud.payload import package_payload
        path, digest = package_payload(get_project_dir(project_slug), chapter_number, out_tar_path, job_type)
        self._payload_manifest_hash = digest
        with tarfile.open(path) as archive:
            manifest = json.load(archive.extractfile("payload_manifest.json"))
        self._dispatch_input_hashes = {relative.removeprefix("project/"): value
            for relative, value in manifest["files"].items() if relative.startswith("project/")}
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
        # Bootstrap uses only stdlib until source inventory has been verified.
        return "\n".join([
            "import os, sys, json, hashlib, tarfile, shutil, subprocess",
            "from pathlib import Path",
            "sys.dont_write_bytecode = True",
            "root = Path('/kaggle/working/runtime')",
            "if root.exists(): raise RuntimeError('Runtime must be fresh')",
            "root.mkdir()",
            "payloads = list(Path('/kaggle/input').rglob('payload.tar.gz'))",
            "if len(payloads) == 1:",
            "    with tarfile.open(payloads[0]) as archive:",
            "        for entry in archive.getmembers():",
            "            target = (root / entry.name).resolve()",
            "            if not target.is_relative_to(root) or not entry.isfile(): raise RuntimeError('Unsafe payload member')",
            "        archive.extractall(root, filter='data')",
            "else:",
            "    manifests = list(Path('/kaggle/input').rglob('payload_manifest.json'))",
            "    if len(manifests) != 1: raise RuntimeError('Expected one payload archive or extracted manifest')",
            "    src_root = manifests[0].parent",
            "    for item in src_root.iterdir():",
            "        dst = root / item.name",
            "        if item.is_dir(): shutil.copytree(item, dst)",
            "        else: shutil.copy2(item, dst)",
            f"expected = {digest!r}",
            "manifest_path = root / 'payload_manifest.json'",
            "if hashlib.sha256(manifest_path.read_bytes()).hexdigest() != expected: raise RuntimeError('Manifest changed')",
            "manifest = json.loads(manifest_path.read_text())",
            "files = {p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file() and p != manifest_path}",
            "if files != set(manifest['files']): raise RuntimeError('Payload inventory mismatch')",
            "for name, digest in manifest['files'].items():",
            "    if hashlib.sha256((root / name).read_bytes()).hexdigest() != digest: raise RuntimeError('Payload hash mismatch')",
            "subprocess.run([sys.executable, '-m', 'pip', 'install', '-r', str(root / 'requirements.txt')], check=True)",
            "sys.path.insert(0, str(root))",
            "from src.cloud.canonical_worker import run",
            f"run(root, expected, Path('/kaggle/working/results'), encoder={encoder!r})",
            "shutil.rmtree(root, ignore_errors=True)",
        ])

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
    ) -> Dict[str, Any]:
        """
        Dispatches a chapter production job to Kaggle Cloud, monitors progress, and returns result.
        Enforces:
        - True job separation ('tts', 'render', 'produce').
        - Non-CC0 metadata license for unconfirmed manga assets.
        - Strict timeout handling (including fast queue_timeout_seconds on QUEUED stalls).
        - No credentials exposure.
        """
        from src.core.series_setup import require_series_setup
        require_series_setup(project_slug)
        if str(chapter_number) != "1":
            from src.core.human_review import require_preview_approval
            require_preview_approval(get_project_dir(project_slug))
        if not self._api:
            raise RuntimeError("Kaggle API is not authenticated.")

        if job_type not in {"tts", "render", "produce"}:
            raise ValueError("Unknown cloud job type")
        chapter = get_chapter_dir(project_slug, chapter_number)
        if job_type == "render":
            from src.render.artifact_validation import validate_audio
            from src.render.chapter_renderer import resolve_audio
            script = RecapScript.model_validate(read_json(chapter / "script.json"))
            if not script.segments:
                raise ValueError("Cannot render an empty chapter")
            for segment in script.segments:
                report = validate_audio(resolve_audio(chapter, segment))
                if segment.checksum != report["sha256"] or segment.duration is None or abs(segment.duration - report["duration"]) > 0.001:
                    raise ValueError("Render-only audio metadata mismatch")

        if gpu:
            quota = self.get_gpu_quota_status()
            if quota and quota.get("exhausted"):
                raise KaggleQuotaExhaustedError(
                    f"Kaggle account '{getattr(self, 'account_name', 'unknown')}' ({self.get_username()}) "
                    f"has exhausted its weekly GPU quota ({quota['used_sec']:.1f}s / {quota['allowed_sec']:.1f}s used, "
                    f"refreshes {quota.get('quota_refresh_time') or 'next cycle'})."
                )

        kernel_slug = (kernel_slug or f"mr-{project_slug[:12]}-c{chapter_number}-{int(time.time())%100000}".lower().replace("_", "-"))[:45].rstrip("-")
        staging_dir = Path(f"/tmp/kaggle_job_{project_slug}_{chapter_number}_{job_type}").resolve()
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        staging_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"KaggleCloudService: Packaging chapter payload in {staging_dir} (job_type={job_type})...")
        payload_tar = staging_dir / "payload.tar.gz"
        self.package_chapter_payload(project_slug, chapter_number, payload_tar, job_type=job_type)

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
        ready = False
        started = time.monotonic()
        while time.monotonic() - started < 600:
            time.sleep(5)
            try:
                status = self._api.dataset_status(ds_ref)
            except Exception as err:
                logger.info(f"Waiting for Kaggle dataset creation to propagate ({ds_ref}): {err}")
                continue
            status = getattr(status, "status", status)
            if isinstance(status, dict):
                status = status.get("status", "")
            if str(status).strip().lower().split(".")[-1] == "ready":
                try:
                    entries = getattr(self._api.dataset_list_files(ds_ref), "dataset_files", [])
                    if any(entry.name == "payload.tar.gz" for entry in entries) or entries:
                        ready = True
                        break
                except Exception:
                    pass
            if str(status).strip().lower() in {"error", "failed"}:
                raise RuntimeError(f"Kaggle payload dataset failed: {status}")
        if not ready:
            raise TimeoutError("Kaggle payload dataset did not become ready; kernel not launched")

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
        )
        worker_py.write_text(worker_code, encoding="utf-8")

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

        job_state_path = chapter / "cloud_job.json"
        job_meta = {
            "project_slug": project_slug,
            "chapter_number": str(chapter_number),
            "job_type": job_type,
            "kernel_ref": target_kernel_ref,
            "dataset_ref": ds_ref,
            "account_name": getattr(self, "account_name", "unknown"),
            "username": self.get_username(),
            "gpu": gpu,
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

        # Download outputs
        output_dir = Path(f"/tmp/kaggle_out_{project_slug}_{chapter_number}_{job_type}").resolve()
        if output_dir.exists():
            shutil.rmtree(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"Downloading outputs from '{target_kernel_ref}' to {output_dir}...")
        self._api.kernels_output(target_kernel_ref, str(output_dir))

        # Unpack back into project directory with strict validation
        sync_report = self.sync_results(project_slug, chapter_number, output_dir, job_type=job_type)
        return sync_report

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

    def sync_results(
        self,
        project_slug: str,
        chapter_number: str,
        downloaded_dir: Path,
        job_type: str = "produce",
    ) -> Dict[str, Any]:
        from src.cloud.result_sync import sync_result
        root = Path(downloaded_dir)
        if (root / "results").is_dir():
            root = root / "results"
        return sync_result(get_project_dir(project_slug), chapter_number, root, job_type,
                           getattr(self, "_payload_manifest_hash", None),
                           getattr(self, "_dispatch_input_hashes", None))

    # =========================================================================
    # Multi-Chapter Dual-T4 Single-Session Series Production Methods (P8)
    # =========================================================================

    def check_chapter_1_preview_approval(
        self, project_slug: str, skip_preview: bool = False
    ) -> Tuple[bool, Optional[str]]:
        """
        Enforces Chapter 1 human preview approval (Gate G6) fail-closed.
        `skip_preview` is retained for signature compatibility only and NEVER waives G6.
        """
        _ = skip_preview
        from src.core.human_review import require_preview_approval
        try:
            require_preview_approval(get_project_dir(project_slug))
            return True, None
        except Exception as exc:
            return False, str(exc)

    def package_series_payload(
        self,
        project_slug: str,
        chapters: List[str],
        out_tar_path: Union[str, Path],
        *,
        skip_preview: bool = False,
        profile: str = "B4",
    ) -> Path:
        from src.cloud.series_payload import package_series_payload

        path, digest = package_series_payload(
            get_project_dir(project_slug),
            chapters,
            out_tar_path,
            skip_preview=False,
            profile=profile,
        )
        self._series_payload_manifest_hash = digest
        with tarfile.open(path) as archive:
            manifest = json.load(archive.extractfile("payload_manifest.json"))
        self._series_dispatch_input_hashes = {
            relative.removeprefix("project/"): value
            for relative, value in manifest["files"].items()
            if relative.startswith("project/")
        }
        self._series_chapters = list(manifest.get("chapters", []))
        return path

    def generate_series_worker_script(
        self,
        project_slug: str,
        chapters: List[str],
        render_workers: int = 2,
        encoder_preference: str = "auto",
        compile_feature: bool = True,
        profile: str = "B4",
        autotune: bool = False,
        video_mode: Optional[str] = None,
        burn_subtitles: Optional[bool] = None,
        fps: Optional[int] = None,
    ) -> str:
        from src.cloud.series_payload import generate_series_worker_script

        digest = getattr(self, "_series_payload_manifest_hash", None)
        if not digest:
            raise ValueError("Package and hash the complete series runtime before generating a worker")
        return generate_series_worker_script(
            project_slug,
            chapters,
            payload_manifest_hash=digest,
            project_dir=get_project_dir(project_slug),
            render_workers=render_workers,
            encoder_preference=encoder_preference,
            compile_feature=compile_feature,
            profile=profile,
            autotune=autotune,
            video_mode=video_mode,
            burn_subtitles=burn_subtitles,
            fps=fps,
        )

    def dispatch_series_job(
        self,
        project_slug: str,
        chapters: List[str],
        *,
        kernel_slug: Optional[str] = None,
        gpu: bool = True,
        wait: bool = True,
        poll_interval: int = 6,
        timeout_seconds: int = 3600,
        skip_preview: bool = False,
        compile_feature: bool = True,
        render_workers: int = 2,
        encoder_preference: str = "auto",
        profile: str = "B4",
        autotune: bool = False,
        video_mode: Optional[str] = None,
        burn_subtitles: Optional[bool] = None,
        fps: Optional[int] = None,
        queue_timeout_seconds: Optional[float] = 300.0,
    ) -> Dict[str, Any]:
        from src.core.human_review import require_voice_approval
        from src.core.series_setup import require_series_setup

        _ = skip_preview  # Never waives Gate G6.
        proj_dir = get_project_dir(project_slug)
        require_series_setup(project_slug, project_dir=proj_dir)
        require_voice_approval(proj_dir)
        ok, error = self.check_chapter_1_preview_approval(project_slug, skip_preview=False)
        if not ok:
            raise ValueError(error or "Chapter 1 human preview approval is required (G6)")
        if not self._api:
            raise RuntimeError("Kaggle API is not authenticated.")

        if gpu:
            quota = self.get_gpu_quota_status()
            if quota and quota.get("exhausted"):
                raise KaggleQuotaExhaustedError(
                    f"Kaggle account '{getattr(self, 'account_name', 'unknown')}' ({self.get_username()}) "
                    f"has exhausted its weekly GPU quota ({quota['used_sec']:.1f}s / {quota['allowed_sec']:.1f}s used, "
                    f"refreshes {quota.get('quota_refresh_time') or 'next cycle'})."
                )

        norm_chapters = [str(c).strip() for c in chapters if str(c).strip()]
        if not norm_chapters:
            raise ValueError("No valid chapters specified for series cloud production")

        kernel_slug = (
            kernel_slug
            or f"mr-{project_slug[:12]}-series-{int(time.time()) % 100000}".lower().replace("_", "-")
        )[:45].rstrip("-")
        staging_dir = Path(f"/tmp/kaggle_series_{project_slug}").resolve()
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        staging_dir.mkdir(parents=True, exist_ok=True)

        payload_tar = staging_dir / "payload.tar.gz"
        self.package_series_payload(
            project_slug,
            norm_chapters,
            payload_tar,
            skip_preview=False,
            profile=profile,
        )

        dataset_slug = f"mr-series-{self._series_payload_manifest_hash[:12]}-{time.time_ns() % 1000000000}"
        ds_staging = staging_dir / "dataset"
        ds_staging.mkdir()
        shutil.copy2(payload_tar, ds_staging / "payload.tar.gz")
        ds_ref = f"{self._username}/{dataset_slug}"
        atomic_write_json(
            ds_staging / "dataset-metadata.json",
            {"title": dataset_slug, "id": ds_ref, "licenses": [{"name": "other"}]},
        )
        self._api.dataset_create_new(str(ds_staging), public=False, quiet=True)
        self._wait_for_dataset_ready(ds_ref, expected_file="payload.tar.gz", timeout_sec=600.0)

        worker_py = staging_dir / "worker.py"
        worker_code = self.generate_series_worker_script(
            project_slug=project_slug,
            chapters=norm_chapters,
            render_workers=render_workers,
            encoder_preference=encoder_preference,
            compile_feature=compile_feature,
            profile=profile,
            autotune=autotune,
            video_mode=video_mode,
            burn_subtitles=burn_subtitles,
            fps=fps,
        )
        worker_py.write_text(worker_code, encoding="utf-8")

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
            "dataset_sources": [ds_ref],
            "competition_sources": [],
            "kernel_sources": [],
            "model_sources": [],
        }
        (staging_dir / "kernel-metadata.json").write_text(
            json.dumps(metadata, indent=2), encoding="utf-8"
        )

        push_res = self._api.kernels_push(str(staging_dir))
        self._require_kernel_submission(push_res)
        assigned_ref = getattr(push_res, "ref", None)
        target_kernel_ref = (
            assigned_ref.replace("/code/", "").strip()
            if assigned_ref
            else f"{self._username}/{kernel_slug}"
        )

        if not wait:
            return {
                "status": "dispatched",
                "kernel_ref": target_kernel_ref,
                "staging_dir": str(staging_dir),
                "job_type": "produce_series",
                "chapters": norm_chapters,
                "payload_manifest_sha256": self._series_payload_manifest_hash,
            }

        self._poll_kernel_until_complete(
            target_kernel_ref,
            timeout_seconds=timeout_seconds,
            queue_timeout_seconds=queue_timeout_seconds if gpu else None,
            poll_interval=poll_interval,
            label="Series",
        )

        output_dir = Path(f"/tmp/kaggle_series_out_{project_slug}").resolve()
        if output_dir.exists():
            shutil.rmtree(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        self._api.kernels_output(target_kernel_ref, str(output_dir))
        return self.sync_series_results(project_slug, norm_chapters, output_dir)

    @with_account_fallback
    def dispatch_series_job_with_fallback(self, *args, **kwargs) -> Dict[str, Any]:
        """Dispatches a series job, rotating across configured Kaggle accounts on quota/queue failure."""
        return self.dispatch_series_job(*args, **kwargs)

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

    def dispatch_benchmark_matrix_job(
        self,
        project_slug: str,
        chapters: List[str],
        *,
        variants: Tuple[str, ...] = ("B0", "B1", "B2", "B3", "B4", "B5"),
        repeats: int = 1,
        warmup: bool = False,
        kernel_slug: Optional[str] = None,
        output_dir: Optional[Union[str, Path]] = None,
        wait: bool = True,
        poll_interval: int = 6,
        timeout_seconds: int = 5400,
        encoder_preference: str = "auto",
        compile_feature: bool = False,
        dataset_poll_interval: float = 5.0,
    ) -> Dict[str, Any]:
        """
        Dispatches ONE self-contained Dual-T4 benchmark matrix kernel (`B0`..`B5`) on a pinned
        series payload, separating dataset upload, queue, boot, warmup/model load, compute,
        and download timings, and verifying SHA-256 receipts + `all_valid` before returning.
        """
        from src.cloud.series_payload import (
            generate_benchmark_worker_script,
            package_series_payload,
        )
        from src.core.human_review import require_voice_approval
        from src.core.series_setup import require_series_setup
        from src.render.artifact_validation import fingerprint

        proj_dir = get_project_dir(project_slug)
        require_series_setup(project_slug, project_dir=proj_dir)
        require_voice_approval(proj_dir)
        ok, error = self.check_chapter_1_preview_approval(project_slug, skip_preview=False)
        if not ok:
            raise ValueError(error or "Chapter 1 human preview approval is required (G6)")
        if not self._api:
            raise RuntimeError("Kaggle API is not authenticated.")

        norm_chapters = [str(c).strip() for c in chapters if str(c).strip()]
        norm_variants = [str(v).strip().upper() for v in variants if str(v).strip()]
        if not norm_chapters or not norm_variants:
            raise ValueError("Benchmark dispatch requires non-empty chapters and variants")

        kernel_slug = (
            kernel_slug
            or f"mr-{project_slug[:10]}-p9bench-{int(time.time()) % 100000}".lower().replace("_", "-")
        )[:45].rstrip("-")
        staging_dir = Path(f"/tmp/kaggle_bench_{project_slug}").resolve()
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        staging_dir.mkdir(parents=True, exist_ok=True)

        t_upload0 = time.monotonic()
        payload_tar = staging_dir / "payload.tar.gz"
        _, digest = package_series_payload(
            proj_dir,
            norm_chapters,
            payload_tar,
            skip_preview=False,
            profile="B4",
            job_type="benchmark_matrix",
            include_benchmark_harness=True,
            extra_metadata={
                "variants": norm_variants,
                "repeats": int(repeats),
                "warmup": bool(warmup),
            },
        )
        self._series_payload_manifest_hash = digest

        dataset_slug = f"mr-bench-{digest[:12]}-{time.time_ns() % 1000000000}"
        ds_staging = staging_dir / "dataset"
        ds_staging.mkdir()
        shutil.copy2(payload_tar, ds_staging / "payload.tar.gz")
        ds_ref = f"{self._username}/{dataset_slug}"
        atomic_write_json(
            ds_staging / "dataset-metadata.json",
            {"title": dataset_slug, "id": ds_ref, "licenses": [{"name": "other"}]},
        )
        self._api.dataset_create_new(str(ds_staging), public=False, quiet=True)
        self._wait_for_dataset_ready(
            ds_ref,
            expected_file="payload.tar.gz",
            timeout_sec=600.0,
            poll_interval=dataset_poll_interval,
        )
        dataset_upload_sec = round(max(0.0, time.monotonic() - t_upload0), 4)

        worker_code = generate_benchmark_worker_script(
            project_slug=project_slug,
            chapters=norm_chapters,
            payload_manifest_hash=digest,
            project_dir=proj_dir,
            variants=norm_variants,
            repeats=repeats,
            warmup=warmup,
            encoder_preference=encoder_preference,
            compile_feature=compile_feature,
        )
        (staging_dir / "worker.py").write_text(worker_code, encoding="utf-8")

        metadata = {
            "id": f"{self._username}/{kernel_slug}",
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
        (staging_dir / "kernel-metadata.json").write_text(
            json.dumps(metadata, indent=2), encoding="utf-8"
        )

        push_res = self._api.kernels_push(str(staging_dir))
        self._require_kernel_submission(push_res)
        assigned_ref = getattr(push_res, "ref", None)
        target_kernel_ref = (
            assigned_ref.replace("/code/", "").strip()
            if assigned_ref
            else f"{self._username}/{kernel_slug}"
        )

        if not wait:
            return {
                "status": "dispatched",
                "kernel_ref": target_kernel_ref,
                "dataset_ref": ds_ref,
                "staging_dir": str(staging_dir),
                "job_type": "benchmark_matrix",
                "chapters": norm_chapters,
                "variants": norm_variants,
                "payload_manifest_sha256": digest,
                "dataset_upload_sec": dataset_upload_sec,
            }

        poll_metrics = self._poll_kernel_until_complete(
            target_kernel_ref,
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
            label="Benchmark",
        )

        dl_dir = (
            Path(output_dir).resolve()
            if output_dir is not None
            else Path(f"/tmp/kaggle_bench_out_{project_slug}").resolve()
        )
        if dl_dir.exists():
            shutil.rmtree(dl_dir)
        dl_dir.mkdir(parents=True, exist_ok=True)

        t_dl0 = time.monotonic()
        self._api.kernels_output(target_kernel_ref, str(dl_dir))
        results_root = (dl_dir / "results") if (dl_dir / "results").is_dir() else dl_dir

        manifest_path = results_root / "dual_t4_result_manifest.json"
        if not manifest_path.is_file():
            raise ValueError("Missing downloaded benchmark result manifest: dual_t4_result_manifest.json")
        receipt = read_json(manifest_path)
        if (
            receipt.get("payload_manifest_sha256") != digest
            or receipt.get("project_slug") != project_slug
            or receipt.get("job_type") != "benchmark_matrix"
        ):
            raise ValueError("Downloaded benchmark result does not match dispatched benchmark job")

        actual_files = set()
        for p in results_root.rglob("*"):
            if p.is_symlink():
                raise ValueError("Symlink in downloaded benchmark results")
            if p.is_file() and p != manifest_path:
                actual_files.add(p.relative_to(results_root).as_posix())
        expected_files = dict(receipt.get("files") or {})
        if actual_files != set(expected_files):
            raise ValueError("Downloaded benchmark result inventory mismatch")
        for rel, file_digest in expected_files.items():
            p = (results_root / rel).resolve()
            if not p.is_relative_to(results_root) or fingerprint(p) != file_digest:
                raise ValueError(f"Downloaded benchmark result hash mismatch: {rel}")

        bench_json_path = results_root / "benchmark_matrix_results.json"
        if not bench_json_path.is_file():
            raise ValueError("Missing benchmark_matrix_results.json in downloaded bundle")
        bench_data = read_json(bench_json_path)

        download_sec = round(max(0.0, time.monotonic() - t_dl0), 4)
        phase_timings = dict(bench_data.get("phase_timings") or {})
        phase_timings["dataset_upload_sec"] = dataset_upload_sec
        phase_timings["queue_sec"] = poll_metrics["queue_sec"]
        phase_timings["download_sec"] = download_sec
        bench_data["phase_timings"] = phase_timings
        bench_data["kernel_ref"] = target_kernel_ref
        bench_data["dataset_ref"] = ds_ref

        runs = list(bench_data.get("runs") or [])
        if not runs or not bench_data.get("all_valid") or any(not r.get("all_valid") for r in runs):
            raise RuntimeError("Benchmark matrix aborted: one or more variants failed artifact validation")

        atomic_write_json(bench_json_path, bench_data)
        if results_root != dl_dir:
            atomic_write_json(dl_dir / "benchmark_matrix_results.json", bench_data)
        return bench_data

    def produce_series_job(self, project_slug, chapters, **kwargs):
        return self.dispatch_series_job(project_slug, chapters, **kwargs)

    def sync_series_results(
        self,
        project_slug: str,
        chapters: List[str],
        downloaded_dir: Union[str, Path],
    ) -> Dict[str, Any]:
        from src.cloud.series_payload import sync_series_results

        root = Path(downloaded_dir)
        if (root / "results").is_dir():
            root = root / "results"
        return sync_series_results(
            get_project_dir(project_slug),
            chapters,
            root,
            getattr(self, "_series_payload_manifest_hash", None),
            getattr(self, "_series_dispatch_input_hashes", None),
        )
