"""
src/cloud/kaggle_service.py
Single-session Kaggle adapter for the canonical Local/Cloud runtime.
Real-artifact/live acceptance is tracked separately; Dual-T4 dispatch is disabled.
"""

from __future__ import annotations

import json
import os
import shutil
import tarfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from src.core.logger import logger
from src.core.schemas import RecapScript
from src.core.storage import atomic_write_json, get_chapter_dir, get_project_dir, read_json



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

    def __init__(self, api_token: Optional[str] = None):
        self.api_token = api_token or os.environ.get("KAGGLE_API_TOKEN")
        self._api = None
        self._username = None
        self._authenticate()

    def __repr__(self) -> str:
        return f"<KaggleCloudService username='{self._username}' authenticated={self.is_authenticated}>"

    def _authenticate(self) -> None:
        """Authenticates with the Kaggle API without logging sensitive tokens."""
        try:
            from kaggle.api.kaggle_api_extended import KaggleApi

            if self.api_token:
                os.environ["KAGGLE_API_TOKEN"] = self.api_token

            api = KaggleApi()
            api.authenticate()
            self._api = api
            # Introspect username
            self._username = api.config_values.get(api.CONFIG_NAME_USER, "kaggle_user")
            logger.info(f"KaggleCloudService: Authenticated as '{self._username}'")
        except Exception as e:
            err_msg = str(e)
            if self.api_token and self.api_token in err_msg:
                err_msg = err_msg.replace(self.api_token, "[REDACTED_API_TOKEN]")
            logger.warning(f"KaggleCloudService: Kaggle API authentication failed: {err_msg}")
            self._api = None

    @property
    def is_authenticated(self) -> bool:
        return self._api is not None

    def get_username(self) -> str:
        return self._username or "unknown"

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
            "import os, sys, json, hashlib, tarfile, subprocess",
            "from pathlib import Path",
            "sys.dont_write_bytecode = True",
            "root = Path('/kaggle/working/runtime')",
            "if root.exists(): raise RuntimeError('Runtime must be fresh')",
            "root.mkdir()",
            "payloads = list(Path('/kaggle/input').rglob('payload.tar.gz'))",
            "if len(payloads) != 1: raise RuntimeError('Expected one exact payload')",
            "with tarfile.open(payloads[0]) as archive:",
            "    for entry in archive.getmembers():",
            "        target = (root / entry.name).resolve()",
            "        if not target.is_relative_to(root) or not entry.isfile(): raise RuntimeError('Unsafe payload member')",
            "    archive.extractall(root, filter='data')",
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
        ])

    def dispatch_chapter_job(
        self,
        project_slug: str,
        chapter_number: str,
        kernel_slug: Optional[str] = None,
        gpu: bool = True,
        wait: bool = True,
        poll_interval: int = 6,
        timeout_seconds: int = 900,
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
        - Strict timeout handling (raising TimeoutError, aborting corrupt downloads).
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
        if job_type == "render":
            from src.render.artifact_validation import validate_audio
            from src.render.chapter_renderer import resolve_audio
            chapter = get_chapter_dir(project_slug, chapter_number)
            script = RecapScript.model_validate(read_json(chapter / "script.json"))
            if not script.segments:
                raise ValueError("Cannot render an empty chapter")
            for segment in script.segments:
                report = validate_audio(resolve_audio(chapter, segment))
                if segment.checksum != report["sha256"] or segment.duration is None or abs(segment.duration - report["duration"]) > 0.001:
                    raise ValueError("Render-only audio metadata mismatch")

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
        while time.monotonic() - started < 180:
            status = self._api.dataset_status(ds_ref)
            status = getattr(status, "status", status)
            if isinstance(status, dict):
                status = status.get("status", "")
            if str(status).strip().lower().split(".")[-1] == "ready":
                entries = getattr(self._api.dataset_list_files(ds_ref), "dataset_files", [])
                if any(entry.name == "payload.tar.gz" for entry in entries):
                    ready = True
                    break
            if str(status).strip().lower() in {"error", "failed"}:
                raise RuntimeError(f"Kaggle payload dataset failed: {status}")
            time.sleep(5)
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
        logger.info(f"KaggleCloudService: Kernel push result: {push_res}")

        # Resolve actual slug assigned by Kaggle
        assigned_ref = getattr(push_res, "ref", None)
        if assigned_ref:
            target_kernel_ref = assigned_ref.replace("/code/", "").strip()
        else:
            target_kernel_ref = f"{self._username}/{kernel_slug}"

        if not wait:
            return {
                "status": "dispatched",
                "kernel_ref": target_kernel_ref,
                "staging_dir": str(staging_dir),
                "job_type": job_type,
            }

        logger.info(f"KaggleCloudService: Waiting for cloud execution of '{target_kernel_ref}'...")
        t0 = time.time()
        last_status = "QUEUED"
        completed_cleanly = False

        while time.time() - t0 < timeout_seconds:
            time.sleep(poll_interval)
            status_obj = self._api.kernels_status(target_kernel_ref)
            current_status = getattr(status_obj, "status", str(status_obj))
            if isinstance(current_status, dict):
                current_status = current_status.get("status", "UNKNOWN")

            if current_status != last_status:
                logger.info(f"Kaggle Cloud Status [{int(time.time() - t0)}s]: {current_status}")
                last_status = current_status

            normalized_status = str(current_status).strip().upper().split(".")[-1]
            if normalized_status in {"COMPLETE", "COMPLETED"}:
                logger.info(f"Kaggle execution COMPLETE in {int(time.time() - t0)}s!")
                completed_cleanly = True
                break
            elif any(err_kw in str(current_status).upper() for err_kw in ["FAILED", "ERROR"]):
                fail_msg = getattr(status_obj, "failureMessage", "Unknown cloud error")
                try:
                    err_dir = staging_dir / "error_out"
                    err_dir.mkdir(parents=True, exist_ok=True)
                    self._api.kernels_output(target_kernel_ref, str(err_dir))
                    for log_f in err_dir.glob("*.log"):
                        logger.error(f"Kaggle Cloud Error Log:\n{log_f.read_text(errors='ignore')[-3000:]}")
                except Exception as log_err:
                    logger.warning(f"Could not retrieve error log: {log_err}")
                raise RuntimeError(f"Kaggle Cloud job failed: {fail_msg}")

        if not completed_cleanly:
            raise TimeoutError(
                f"Kaggle Cloud job '{target_kernel_ref}' timed out after {timeout_seconds}s "
                f"with status '{last_status}'. Output download aborted."
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
    # Multi-Chapter Dual-T4 Single-Session Series Production Methods (PR-03)
    # =========================================================================

    def check_chapter_1_preview_approval(
        self, project_slug: str, skip_preview: bool = False
    ) -> Tuple[bool, Optional[str]]:
        from src.core.human_review import require_preview_approval
        try:
            require_preview_approval(get_project_dir(project_slug))
            return True, None
        except Exception as exc:
            return False, str(exc)

    def package_series_payload(self, *args, **kwargs):
        raise RuntimeError("Dual-T4 series payload is disabled pending G7 real cloud correctness and G8")

    def generate_series_worker_script(self, *args, **kwargs):
        raise RuntimeError("Dual-T4 worker is disabled pending G7 real cloud correctness and G8")

    def dispatch_series_job(self, project_slug, chapters, **kwargs):
        from src.core.series_setup import require_series_setup
        require_series_setup(project_slug)
        ok, error = self.check_chapter_1_preview_approval(project_slug)
        if not ok:
            raise ValueError(error)
        raise RuntimeError("Dual-T4 series production is disabled pending G7 real cloud correctness and G8")

    def produce_series_job(self, project_slug, chapters, **kwargs):
        return self.dispatch_series_job(project_slug, chapters, **kwargs)

    def sync_series_results(self, *args, **kwargs):
        raise RuntimeError("Dual-T4 series synchronization is disabled pending G7 and G8")
