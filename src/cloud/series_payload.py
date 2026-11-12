"""
src/cloud/series_payload.py
Self-contained Dual-T4 series payload packaging, stdlib-first bootstrap worker script,
single-session container entrypoint, and G8 Dual-T4 receipt/semantic-parity verification.
"""

from __future__ import annotations

import contextlib
import csv
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from src.core.human_review import require_preview_approval, require_voice_approval
from src.core.schemas import CheckpointStatus, HumanReviewStatus, StageType
from src.core.series_setup import require_series_setup
from src.core.storage import (
    acquire_lock,
    atomic_write_json,
    load_chapter_metadata,
    load_manifest,
    load_recap_script,
    read_json,
)
from src.core.visual_contract import VisualPlan
from src.render.artifact_validation import fingerprint, validate_audio, validate_final_artifact
from src.render.chapter_renderer import resolve_audio
from src.render.visual_policy import resolve_visual_plan, validate_narration_evidence


SERIES_PAYLOAD_SCHEMA_VERSION = "4.2.0"

G8_REQUIRED_RECEIPTS: Tuple[str, ...] = (
    "dual_t4_environment.json",
    "dual_t4_gpu_trace.csv",
    "dual_t4_worker_log.jsonl",
    "dual_t4_artifact_report.json",
    "dual_t4_result_manifest.json",
)


def _normalize_chapters(chapters: Sequence[Union[str, int]]) -> List[str]:
    cleaned: List[str] = []
    seen = set()
    for ch in chapters:
        s = str(ch).strip()
        if s and s not in seen:
            seen.add(s)
            cleaned.append(s)
    if not cleaned:
        raise ValueError("Series chapters list cannot be empty")
    return cleaned


def verify_series_payload_manifest(root: Union[str, Path], expected_manifest_hash: str) -> Dict[str, Any]:
    """Fail-closed SHA-256 verification of extracted Dual-T4 series runtime payload."""
    root = Path(root).resolve()
    path = root / "payload_manifest.json"
    if not expected_manifest_hash or not path.is_file():
        raise ValueError("Missing series payload manifest or expected digest")
    if fingerprint(path) != expected_manifest_hash:
        raise ValueError("Series payload manifest hash mismatch")

    manifest = read_json(path)
    if manifest.get("schema_version") != SERIES_PAYLOAD_SCHEMA_VERSION or not manifest.get("files"):
        raise ValueError("Invalid series payload manifest")
    if not manifest.get("chapters"):
        raise ValueError("Series payload manifest has no chapters")

    actual = set()
    for entry in root.rglob("*"):
        if entry.is_symlink():
            raise ValueError("Series payload symlinks are forbidden")
        if entry.is_file() and entry != path:
            actual.add(entry.relative_to(root).as_posix())
    if actual != set(manifest["files"]):
        raise ValueError("Series payload inventory differs from manifest")

    for relative, digest in manifest["files"].items():
        target = (root / relative).resolve()
        if not target.is_relative_to(root) or not target.is_file() or fingerprint(target) != digest:
            raise ValueError(f"Missing, unsafe, or altered series payload file: {relative}")

    required_assets = (
        "src/core/visual_contract.py",
        "src/render/visual_plan_renderer.py",
        "src/cloud/cuda_worker_pool.py",
        "src/cloud/elastic_scheduler.py",
        "src/cloud/dual_t4_orchestrator.py",
        "src/cloud/series_payload.py",
        "requirements.txt",
        "project/project.json",
        "project/series_setup_answers.json",
        "project/voice_selection.json",
        "project/preview_approval.json",
    )
    for req in required_assets:
        if req not in actual:
            raise ValueError(f"Required series runtime asset is absent: {req}")
    if manifest.get("job_type") == "benchmark_matrix":
        if "scripts/benchmark/run_dual_t4_matrix.py" not in actual:
            raise ValueError("Required benchmark harness is absent: scripts/benchmark/run_dual_t4_matrix.py")
    return manifest


def package_series_payload(
    project_dir: Union[str, Path],
    chapters: Sequence[Union[str, int]],
    output: Union[str, Path],
    *,
    skip_preview: bool = False,
    profile: str = "B5",
    job_type: str = "produce_series",
    include_benchmark_harness: bool = False,
    extra_metadata: Optional[Dict[str, Any]] = None,
) -> Tuple[Path, str]:
    """
    Packages a self-contained Dual-T4 series payload enforcing Q01-Q10 setup,
    voice approval, Chapter 1 human preview approval (G6; skip_preview cannot waive),
    narration evidence validation across every chapter, and SHA-256 inventory manifest.
    """
    _ = skip_preview  # Compatibility parameter only; G6 preview approval is NEVER waived.
    root = Path(project_dir).resolve()
    config = read_json(root / "project.json") if (root / "project.json").is_file() else {}
    slug = str(config.get("slug") or root.name)

    require_series_setup(slug, project_dir=root)
    require_voice_approval(root)
    require_preview_approval(root)

    target_chapters = _normalize_chapters(chapters)
    source_root = Path(__file__).resolve().parents[2]

    chapters_to_validate = list(target_chapters)
    all_pack_chapters = ["1"] + [c for c in target_chapters if c != "1"]

    for ch_num in chapters_to_validate:
        chap = root / "chapters" / ch_num
        if not chap.is_dir():
            raise ValueError(f"Missing series chapter directory: {ch_num}")
        for req_file in ("chapter.json", "script.json", "manifest.json"):
            if not (chap / req_file).is_file():
                raise ValueError(f"Missing required chapter {ch_num} payload: {req_file}")
        script, source_manifest = load_recap_script(chap), load_manifest(chap)
        validate_narration_evidence(chap, script, source_manifest)
        if script.project_slug != slug or script.chapter_number != ch_num:
            raise ValueError(f"Series payload chapter/project identity mismatch on chapter {ch_num}")

    with tempfile.TemporaryDirectory(prefix="manga-series-payload-") as temp:
        stage = Path(temp)
        files: Dict[str, Path] = {}

        for src_file in (source_root / "src").rglob("*.py"):
            if "__pycache__" in src_file.parts:
                continue
            files[src_file.relative_to(source_root).as_posix()] = src_file
        for meta_name in ("requirements.txt", "pyproject.toml"):
            files[meta_name] = source_root / meta_name
        if include_benchmark_harness or job_type == "benchmark_matrix":
            harness_path = source_root / "scripts" / "benchmark" / "run_dual_t4_matrix.py"
            if not harness_path.is_file():
                raise ValueError(f"Missing benchmark matrix harness: {harness_path}")
            files["scripts/benchmark/run_dual_t4_matrix.py"] = harness_path

        for proj_meta in ("project.json", "series_setup_answers.json", "voice_selection.json", "preview_approval.json"):
            p = root / proj_meta
            if p.is_file():
                files[f"project/{proj_meta}"] = p

        allowed_roots = {"raw", "panels", "audio", "subtitles", "contact_sheets"}
        allowed_files = {
            "chapter.json",
            "script.json",
            "manifest.json",
            "visual_plan.json",
            "thumbnail.jpg",
            "thumbnail.png",
            "thumbnail_selection.json",
        }
        for ch_num in all_pack_chapters:
            chap = root / "chapters" / ch_num
            if not chap.is_dir():
                raise ValueError(f"Missing chapter payload: {ch_num}")
            for p in chap.rglob("*"):
                rel = p.relative_to(chap)
                if p.is_symlink():
                    raise ValueError(f"Symlink in series payload: {p.name}")
                if p.is_file() and (
                    rel.parts[0] in allowed_roots
                    or rel.as_posix() in allowed_files
                    or (ch_num == "1" and rel.as_posix() == "video/preview.mp4")
                ):
                    if p.suffix in {".lock", ".tmp"}:
                        continue
                    files[f"project/chapters/{ch_num}/{rel.as_posix()}"] = p

        bgm = config.get("audio_settings", {}).get("bgm_path")
        if bgm:
            bgm_path = root / bgm
            if Path(bgm).is_absolute() or not bgm_path.resolve().is_relative_to(root):
                raise ValueError("Cloud BGM must be a portable project-relative asset")
            if not bgm_path.is_file():
                raise ValueError(f"Configured BGM asset is missing: {bgm}")
            files[f"project/{bgm}"] = bgm_path

        for relative, src_path in files.items():
            if src_path.is_symlink() or not src_path.is_file():
                raise ValueError(f"Missing/unsafe series runtime asset: {relative}")
            dest = stage / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_path, dest)

        git_commit = "unknown"
        try:
            res = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(source_root),
                capture_output=True,
                text=True,
                timeout=3.0,
                check=False,
            )
            if res.returncode == 0 and res.stdout.strip():
                git_commit = res.stdout.strip()
        except Exception:
            pass

        manifest = {
            "schema_version": SERIES_PAYLOAD_SCHEMA_VERSION,
            "project_slug": slug,
            "chapters": target_chapters,
            "job_type": job_type,
            "profile": str(profile),
            "git_commit": git_commit,
            "files": {
                rel: fingerprint(stage / rel) for rel in sorted(files)
            },
        }
        if extra_metadata:
            manifest["benchmark"] = dict(extra_metadata)
        atomic_write_json(stage / "payload_manifest.json", manifest)
        digest = fingerprint(stage / "payload_manifest.json")
        verify_series_payload_manifest(stage, digest)

        out_path = Path(output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(out_path, "w:gz") as archive:
            for p in sorted(stage.rglob("*")):
                if p.is_file():
                    archive.add(p, arcname=p.relative_to(stage).as_posix())
    return out_path, digest


def _build_bootstrap_header_lines() -> List[str]:
    """
    Returns stdlib-only module top-level statements safe when Python `spawn`
    re-imports the Kaggle kernel script as `__mp_main__`.
    """
    return [
        "import os, sys, json, hashlib, tarfile, shutil, subprocess, time",
        "from pathlib import Path",
        "sys.dont_write_bytecode = True",
        "_KAGGLE_RUNTIME = '/tmp/mr_runtime'",
        "if os.path.isdir(_KAGGLE_RUNTIME) and _KAGGLE_RUNTIME not in sys.path:",
        "    sys.path.insert(0, _KAGGLE_RUNTIME)",
    ]


def _build_bootstrap_main_lines(payload_manifest_hash: str) -> List[str]:
    """
    Returns `main()`-scoped Kaggle container bootstrap statements that extract & verify
    the payload SHA-256 inventory BEFORE running `pip install` or importing `src.*`.
    Keeps runtime, caches, and temp working dirs under `/tmp/` so `/kaggle/working/`
    contains ONLY `results/`.
    Records `_boot_t0` and `_boot_sec` around extraction + verification + pip install.
    """
    return [
        "_boot_t0 = time.monotonic()",
        "working_dir = Path('/kaggle/working')",
        "os.environ.setdefault('HF_HOME', '/tmp/mr_hf_cache')",
        "os.environ.setdefault('TORCH_HOME', '/tmp/mr_torch_cache')",
        "os.environ.setdefault('TMPDIR', '/tmp/mr_tmp')",
        "os.environ.setdefault('MR_WORK_DIR', '/tmp/mr_work')",
        "Path(os.environ['TMPDIR']).mkdir(parents=True, exist_ok=True)",
        "Path(os.environ['MR_WORK_DIR']).mkdir(parents=True, exist_ok=True)",
        "root = Path(_KAGGLE_RUNTIME)",
        "if root.exists(): raise RuntimeError('Runtime must be fresh')",
        "root.mkdir(parents=True)",
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
        "        if item.is_symlink(): raise RuntimeError('Symlinks are forbidden in series payload')",
        "        if item.is_dir(): shutil.copytree(item, dst, symlinks=False)",
        "        else: shutil.copy2(item, dst)",
        f"expected = {payload_manifest_hash!r}",
        "manifest_path = root / 'payload_manifest.json'",
        "if hashlib.sha256(manifest_path.read_bytes()).hexdigest() != expected: raise RuntimeError('Payload manifest changed')",
        "manifest = json.loads(manifest_path.read_text())",
        "files = {p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file() and p != manifest_path}",
        "if files != set(manifest['files']): raise RuntimeError('Payload inventory mismatch')",
        "for name, digest in manifest['files'].items():",
        "    target = (root / name).resolve()",
        "    if not target.is_relative_to(root) or hashlib.sha256(target.read_bytes()).hexdigest() != digest:",
        "        raise RuntimeError(f'Payload hash mismatch: {name}')",
        "subprocess.run([sys.executable, '-m', 'pip', 'install', '-q', '-r', str(root / 'requirements.txt')], check=True)",
        "if str(root) not in sys.path:",
        "    sys.path.insert(0, str(root))",
        "_cur_py = os.environ.get('PYTHONPATH', '')",
        "_py_parts = [p for p in _cur_py.split(os.pathsep) if p] if _cur_py else []",
        "if str(root) not in _py_parts:",
        "    os.environ['PYTHONPATH'] = os.pathsep.join([str(root), *_py_parts])",
        "_boot_sec = max(0.0, time.monotonic() - _boot_t0)",
    ]


def _wrap_kaggle_bootstrap_script(
    payload_manifest_hash: str,
    job_lines: Sequence[str],
) -> str:
    """
    Wraps bootstrap + job execution inside `if __name__ == '__main__':` so Python
    `multiprocessing.spawn` child workers (`run_name='__mp_main__'`) never re-run
    runtime extraction, SHA-256 verification, `pip install`, or worker entrypoints.
    """
    lines = _build_bootstrap_header_lines()
    lines.append("")
    lines.append("def main() -> None:")
    for stmt in _build_bootstrap_main_lines(payload_manifest_hash):
        lines.append(f"    {stmt}")
    for stmt in job_lines:
        lines.append(f"    {stmt}")
    lines.append("")
    lines.append("if __name__ == '__main__':")
    lines.append("    main()")
    return "\n".join(lines)


def generate_series_worker_script(
    project_slug: str,
    chapters: Sequence[Union[str, int]],
    *,
    payload_manifest_hash: str,
    project_dir: Optional[Path] = None,
    render_workers: int = 2,
    encoder_preference: str = "auto",
    compile_feature: bool = True,
    profile: str = "B5",
    autotune: bool = True,
    video_mode: Optional[str] = None,
    burn_subtitles: Optional[bool] = None,
    fps: Optional[int] = None,
) -> str:
    """
    Generates a stdlib-only single-extraction bootstrap script for Kaggle `/tmp/mr_runtime`.
    Supports both `payload.tar.gz` and Kaggle auto-extracted `payload_manifest.json`.
    Verifies SHA-256 inventory BEFORE running `pip install` or importing any `src.*` module.
    """
    from src.core.storage import get_project_dir

    proj_dir = Path(project_dir) if project_dir is not None else get_project_dir(project_slug)
    require_series_setup(project_slug, project_dir=proj_dir)
    require_voice_approval(proj_dir)
    require_preview_approval(proj_dir)

    target_chapters = _normalize_chapters(chapters)
    if not payload_manifest_hash:
        raise ValueError("Package and hash the complete series payload before generating worker script")

    settings = read_json(proj_dir / "project.json")["render_settings"]
    for key, val in {
        "target_height": None,
        "fps": fps,
        "burn_subtitles": burn_subtitles,
        "video_mode": video_mode,
    }.items():
        if val is not None and val != settings[key]:
            raise ValueError(f"Cloud override conflicts with confirmed setup: {key}")

    encoder = "h264_nvenc" if encoder_preference in {"auto", "nvenc", "h264_nvenc"} else encoder_preference
    if encoder not in {"libx264", "h264_nvenc"}:
        raise ValueError(f"Unsupported cloud encoder: {encoder_preference}")

    job_lines = [
        "from src.cloud.series_payload import run_series_worker",
        "run_series_worker(",
        "    root,",
        "    expected,",
        "    working_dir / 'results',",
        f"    chapters={target_chapters!r},",
        f"    encoder={encoder!r},",
        f"    render_workers={int(render_workers)!r},",
        f"    compile_feature={bool(compile_feature)!r},",
        f"    profile={str(profile)!r},",
        f"    autotune={bool(autotune)!r},",
        ")",
    ]
    return _wrap_kaggle_bootstrap_script(payload_manifest_hash, job_lines)


def generate_benchmark_worker_script(
    project_slug: str,
    chapters: Sequence[Union[str, int]],
    *,
    payload_manifest_hash: str,
    project_dir: Optional[Path] = None,
    variants: Sequence[str] = ("B0", "B1", "B2", "B3", "B4", "B5"),
    repeats: int = 1,
    warmup: bool = False,
    encoder_preference: str = "auto",
    compile_feature: bool = False,
) -> str:
    """
    Generates a stdlib-only Kaggle Dual-T4 bootstrap script that verifies payload SHA-256
    inventory first, times kernel boot separately, and runs the P9 B0..B5 benchmark matrix
    on the pinned series payload inside `/kaggle/working/results`.
    """
    from src.core.storage import get_project_dir

    proj_dir = Path(project_dir) if project_dir is not None else get_project_dir(project_slug)
    require_series_setup(project_slug, project_dir=proj_dir)
    require_voice_approval(proj_dir)
    require_preview_approval(proj_dir)

    target_chapters = _normalize_chapters(chapters)
    if not payload_manifest_hash:
        raise ValueError("Package and hash the benchmark payload before generating worker script")

    encoder = "h264_nvenc" if encoder_preference in {"auto", "nvenc", "h264_nvenc"} else encoder_preference
    if encoder not in {"libx264", "h264_nvenc"}:
        raise ValueError(f"Unsupported cloud encoder: {encoder_preference}")

    norm_variants = [str(v).strip().upper() for v in variants if str(v).strip()]
    if not norm_variants:
        raise ValueError("Benchmark variants cannot be empty")

    job_lines = [
        "cmd = [",
        "    sys.executable,",
        "    str(root / 'scripts' / 'benchmark' / 'run_dual_t4_matrix.py'),",
        "    '--in-container',",
        "    '--runtime-root', str(root),",
        "    '--manifest-hash', expected,",
        "    '--output-dir', str(working_dir / 'results'),",
        f"    '--chapters', {','.join(target_chapters)!r},",
        f"    '--variants', {','.join(norm_variants)!r},",
        f"    '--repeats', str({max(1, int(repeats))}),",
        f"    '--encoder', {encoder!r},",
        "    '--boot-sec', f'{_boot_sec:.4f}',",
        "]",
    ]
    if warmup:
        job_lines.append("cmd.append('--warmup')")
    if not compile_feature:
        job_lines.append("cmd.append('--no-compile')")
    job_lines.append("subprocess.run(cmd, cwd=str(root), check=True)")
    return _wrap_kaggle_bootstrap_script(payload_manifest_hash, job_lines)


def _export_lean_chapter_outputs(src_chap: Path, dst_chap: Path) -> None:
    """
    Exports ONLY produced chapter metadata & media outputs (`results/chapters/<N>/`),
    excluding redundant `raw/`, `panels/`, `shots/`, `visual.mp4`, `speech_master.wav`,
    and `cache/` so Kaggle `kernels_output` downloads stay lean.
    """
    dst_chap.mkdir(parents=True, exist_ok=True)
    for fname in (
        "script.json",
        "chapter.json",
        "manifest.json",
        "visual_plan.json",
        "artifact_validation.json",
    ):
        src_file = src_chap / fname
        if src_file.is_file():
            shutil.copy2(src_file, dst_chap / fname)

    clips_dir = src_chap / "audio" / "clips"
    if clips_dir.is_dir():
        dst_clips = dst_chap / "audio" / "clips"
        dst_clips.mkdir(parents=True, exist_ok=True)
        for wav in sorted(clips_dir.glob("*.wav")):
            if wav.is_file():
                shutil.copy2(wav, dst_clips / wav.name)

    mixed_m4a = src_chap / "audio" / "mixed.m4a"
    if mixed_m4a.is_file():
        (dst_chap / "audio").mkdir(parents=True, exist_ok=True)
        shutil.copy2(mixed_m4a, dst_chap / "audio" / "mixed.m4a")

    sub_dir = src_chap / "subtitles"
    if sub_dir.is_dir():
        dst_sub = dst_chap / "subtitles"
        dst_sub.mkdir(parents=True, exist_ok=True)
        for srt in sorted(sub_dir.glob("*.srt")):
            if srt.is_file():
                shutil.copy2(srt, dst_sub / srt.name)

    video_dir = src_chap / "video"
    if video_dir.is_dir():
        for vname in ("final.mp4", "preview.mp4"):
            vfile = video_dir / vname
            if vfile.is_file():
                (dst_chap / "video").mkdir(parents=True, exist_ok=True)
                shutil.copy2(vfile, dst_chap / "video" / vname)


def run_series_worker(
    root: Union[str, Path],
    manifest_hash: str,
    output_dir: Union[str, Path],
    *,
    chapters: Optional[Sequence[Union[str, int]]] = None,
    encoder: str = "auto",
    render_workers: int = 2,
    compile_feature: bool = True,
    profile: str = "B5",
    autotune: bool = True,
    orchestrator_factory: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """
    In-container single-session Dual-T4 series execution entrypoint.
    Verifies SHA-256 payload manifest before running, preserves single-session
    model lifecycle, writes G8 receipts + lean chapter outputs + `dual_t4_result_manifest.json`.
    """
    from src.cloud.dual_t4_orchestrator import DualT4Orchestrator

    root_p = Path(root).resolve()
    out_p = Path(output_dir).resolve()
    manifest = verify_series_payload_manifest(root_p, manifest_hash)

    slug = str(manifest["project_slug"])
    target_chapters = _normalize_chapters(chapters or manifest["chapters"])

    out_p.mkdir(parents=True, exist_ok=True)
    prev_data_dir = os.environ.get("MANGA_RECAP_DATA_DIR")
    prev_pythonpath = os.environ.get("PYTHONPATH")
    mr_work = Path(os.environ.get("MR_WORK_DIR", "/tmp/mr_work"))
    mr_work.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="p8-series-", dir=str(mr_work)) as session_tmp:
        session_dir = Path(session_tmp)
        runtime_data = session_dir / "data"
        (runtime_data / "projects").mkdir(parents=True, exist_ok=True)
        destination = runtime_data / "projects" / slug
        shutil.copytree(root_p / "project", destination)
        work_dir = session_dir / "work"
        work_dir.mkdir(parents=True, exist_ok=True)

        os.environ["MANGA_RECAP_DATA_DIR"] = str(runtime_data)
        py_parts = [p for p in (prev_pythonpath or "").split(os.pathsep) if p]
        if str(root_p) not in py_parts:
            os.environ["PYTHONPATH"] = os.pathsep.join([str(root_p), *py_parts])
        try:
            require_series_setup(slug, project_dir=destination)
            require_voice_approval(destination)
            require_preview_approval(destination)

            if orchestrator_factory is not None:
                orch = orchestrator_factory(
                    workspace_dir=destination,
                    chapters=target_chapters,
                    render_workers=render_workers,
                    compile_feature=compile_feature,
                    profile=profile,
                    encoder=encoder,
                    autotune=autotune,
                    work_dir=work_dir,
                    receipts_dir=out_p,
                )
            else:
                orch = DualT4Orchestrator(
                    workspace_dir=destination,
                    chapters=target_chapters,
                    render_workers=render_workers,
                    compile_feature=compile_feature,
                    profile=profile,
                    encoder=encoder,
                    autotune=autotune,
                    work_dir=work_dir,
                    receipts_dir=out_p,
                    enforce_setup=True,
                )
            tel = orch.run_pipeline()

            for ch_num in target_chapters:
                src_chap = destination / "chapters" / ch_num
                dst_chap = out_p / "chapters" / ch_num
                _export_lean_chapter_outputs(src_chap, dst_chap)

            feat_src = destination / "outputs" / "full_recap_feature.mp4"
            if feat_src.is_file():
                feat_dst = out_p / "outputs" / "full_recap_feature.mp4"
                feat_dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(feat_src, feat_dst)

            for req in (
                "dual_t4_environment.json",
                "dual_t4_gpu_trace.csv",
                "dual_t4_worker_log.jsonl",
                "dual_t4_artifact_report.json",
            ):
                if not (out_p / req).is_file():
                    raise RuntimeError(f"Missing required G8 receipt after run: {req}")

            result_manifest_path = out_p / "dual_t4_result_manifest.json"
            files_inventory = {
                p.relative_to(out_p).as_posix(): fingerprint(p)
                for p in sorted(out_p.rglob("*"))
                if p.is_file() and p != result_manifest_path
            }
            receipt = {
                "schema_version": SERIES_PAYLOAD_SCHEMA_VERSION,
                "project_slug": slug,
                "chapters": target_chapters,
                "job_type": "produce_series",
                "payload_manifest_sha256": manifest_hash,
                "telemetry": tel.to_dict() if hasattr(tel, "to_dict") else dict(tel),
                "files": files_inventory,
            }
            atomic_write_json(result_manifest_path, receipt)
            return receipt
        finally:
            if prev_data_dir is None:
                os.environ.pop("MANGA_RECAP_DATA_DIR", None)
            else:
                os.environ["MANGA_RECAP_DATA_DIR"] = prev_data_dir
            if prev_pythonpath is None:
                os.environ.pop("PYTHONPATH", None)
            else:
                os.environ["PYTHONPATH"] = prev_pythonpath


def verify_g8_dual_t4_evidence(
    downloaded_dir: Union[str, Path],
    chapters: Sequence[Union[str, int]],
) -> Dict[str, Any]:
    """
    Verifies G8 Dual-T4 telemetry & artifact receipts:
    - All 5 G8 files exist with zero symlinks.
    - `dual_t4_environment.json` proves `gpu_count >= 2`, distinct GPU0/GPU1, and `mock_mode is False`.
    - `dual_t4_gpu_trace.csv` contains trace rows for BOTH GPU0 and GPU1.
    - `dual_t4_worker_log.jsonl` records real completed tasks on BOTH GPU0 and GPU1.
    - `dual_t4_artifact_report.json` confirms BOTH GPU0 and GPU1 performed real neural/render work.
    """
    root = Path(downloaded_dir).resolve()
    target_chapters = _normalize_chapters(chapters)

    for fname in G8_REQUIRED_RECEIPTS:
        p = root / fname
        if p.is_symlink() or not p.is_file() or p.stat().st_size == 0:
            raise ValueError(f"G8 evidence receipt missing or empty: {fname}")

    env_doc = read_json(root / "dual_t4_environment.json")
    if env_doc.get("mock_mode") is not False:
        raise ValueError("G8 evidence rejected: mock_mode must be False")
    gpus = env_doc.get("gpus") or []
    if int(env_doc.get("gpu_count", len(gpus))) < 2 or len(gpus) < 2:
        raise ValueError("G8 evidence requires at least 2 physical GPUs (GPU0 and GPU1)")
    uuids = {str(g.get("gpu_uuid") or g.get("gpu_id")) for g in gpus}
    if len(uuids) < 2:
        raise ValueError("G8 evidence requires 2 distinct physical GPU UUIDs")

    # Parse CSV trace
    trace_gpus = set()
    trace_row_count = 0
    with (root / "dual_t4_gpu_trace.csv").open("r", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            trace_row_count += 1
            if "gpu_id" in row:
                trace_gpus.add(int(float(row["gpu_id"])))
    if trace_row_count == 0 or not {0, 1}.issubset(trace_gpus):
        raise ValueError("G8 GPU trace must contain telemetry samples for both GPU 0 and GPU 1")

    # Parse worker JSONL log
    completed_by_gpu: Dict[int, int] = {0: 0, 1: 0}
    for line in (root / "dual_t4_worker_log.jsonl").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        evt = json.loads(line)
        if evt.get("event") in {"tts_batch_completed", "render_shot_completed"}:
            gid = int(evt.get("gpu_id", -1))
            if gid in completed_by_gpu:
                completed_by_gpu[gid] += 1
    if completed_by_gpu[0] <= 0 or completed_by_gpu[1] <= 0:
        raise ValueError(
            f"G8 worker log lacks completed GPU work on both GPUs (gpu0={completed_by_gpu[0]}, gpu1={completed_by_gpu[1]})"
        )

    art_doc = read_json(root / "dual_t4_artifact_report.json")
    usage = art_doc.get("gpu_usage_evidence") or {}
    for gid_key in ("0", "1"):
        g_info = usage.get(gid_key) or {}
        real_work = int(g_info.get("tts_items", 0)) + int(g_info.get("render_shots", 0))
        if real_work <= 0:
            raise ValueError(f"G8 artifact report shows zero real work on GPU {gid_key}")

    per_chap = art_doc.get("per_chapter") or {}
    for ch in target_chapters:
        if ch not in per_chap:
            raise ValueError(f"G8 artifact report missing chapter {ch}")

    return {
        "valid": True,
        "gpu_count": len(gpus),
        "trace_rows": trace_row_count,
        "completed_events_by_gpu": completed_by_gpu,
        "gpu_usage_evidence": usage,
    }


def sync_series_results(
    project: Union[str, Path],
    chapters: Sequence[Union[str, int]],
    downloaded: Union[str, Path],
    expected_manifest_hash: str,
    expected_inputs: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """
    Fail-closed validation & publication of Dual-T4 cloud series outputs:
    - Verifies dispatch manifest hash & local input snapshot immutability.
    - Enforces Q01-Q10 setup, voice approval, and Chapter 1 G6 preview approval.
    - Verifies `dual_t4_result_manifest.json` file inventory + SHA-256 checksums.
    - Verifies G8 Dual-T4 evidence (all 5 receipts + real work on BOTH GPU0 & GPU1).
    - Verifies semantic script parity, `validate_audio`, `VisualPlan` parity,
      Chapter 1 `final == preview` hash parity, and `validate_final_artifact` for EVERY chapter
      BEFORE atomically publishing any state or artifacts.
    """
    if not expected_manifest_hash:
        raise ValueError("Missing trusted series dispatch manifest fingerprint")
    project_dir = Path(project).resolve()
    downloaded_dir = Path(downloaded).resolve()
    target_chapters = _normalize_chapters(chapters)

    required_inputs = {
        "project.json",
        "series_setup_answers.json",
        "voice_selection.json",
        "preview_approval.json",
    }
    for ch in target_chapters:
        required_inputs.add(f"chapters/{ch}/script.json")
        required_inputs.add(f"chapters/{ch}/manifest.json")

    if not expected_inputs or not required_inputs.issubset(expected_inputs):
        raise ValueError("Missing trusted series dispatch input snapshot")

    def _check_inputs() -> None:
        for rel, digest in expected_inputs.items():
            p = (project_dir / rel).resolve()
            if not p.is_relative_to(project_dir) or not p.is_file() or fingerprint(p) != digest:
                raise ValueError(f"Local input changed while series cloud job ran: {rel}")

    _check_inputs()
    config = read_json(project_dir / "project.json")
    slug = config["slug"]
    require_series_setup(slug, project_dir=project_dir)
    require_voice_approval(project_dir)
    approval = require_preview_approval(project_dir)

    result_manifest_path = downloaded_dir / "dual_t4_result_manifest.json"
    if not result_manifest_path.is_file():
        raise ValueError("Missing G8 receipt: dual_t4_result_manifest.json")
    receipt = read_json(result_manifest_path)

    if (
        receipt.get("payload_manifest_sha256") != expected_manifest_hash
        or receipt.get("project_slug") != slug
        or receipt.get("job_type") != "produce_series"
        or list(receipt.get("chapters") or []) != target_chapters
    ):
        raise ValueError("Cloud series result does not match dispatched series job")

    actual_files = set()
    for p in downloaded_dir.rglob("*"):
        if p.is_symlink():
            raise ValueError("Symlink in cloud series results")
        if p.is_file() and p != result_manifest_path:
            actual_files.add(p.relative_to(downloaded_dir).as_posix())
    expected_files = dict(receipt.get("files") or {})
    if actual_files != set(expected_files):
        raise ValueError("Cloud series result inventory mismatch")
    for rel, digest in expected_files.items():
        p = (downloaded_dir / rel).resolve()
        if not p.is_relative_to(downloaded_dir) or fingerprint(p) != digest:
            raise ValueError(f"Cloud series output hash mismatch: {rel}")

    g8_report = verify_g8_dual_t4_evidence(downloaded_dir, target_chapters)

    settings = config["render_settings"]
    validated_chapters: Dict[str, Dict[str, Any]] = {}

    for ch in target_chapters:
        local_chap = project_dir / "chapters" / ch
        incoming_chap = downloaded_dir / "chapters" / ch
        if not incoming_chap.is_dir():
            raise ValueError(f"Missing downloaded chapter directory: {ch}")

        local_script = load_recap_script(local_chap)
        incoming_script = load_recap_script(incoming_chap)
        if not incoming_script.segments or len(incoming_script.segments) != len(local_script.segments):
            raise ValueError(f"Cloud script segment mismatch on chapter {ch}")

        for old_seg, new_seg in zip(local_script.segments, incoming_script.segments):
            for key in (
                "seq_id",
                "evidence_panel_ids",
                "narrator_text",
                "camera_motion",
                "focus_box",
                "emotion",
                "emotion_cue",
            ):
                if getattr(old_seg, key) != getattr(new_seg, key):
                    raise ValueError(f"Cloud changed source narration/planning on chapter {ch}: {key}")

        audio_paths = [resolve_audio(incoming_chap, seg) for seg in incoming_script.segments]
        for seg, ap in zip(incoming_script.segments, audio_paths):
            a_rep = validate_audio(ap)
            if (
                seg.checksum != a_rep["sha256"]
                or seg.duration is None
                or abs(seg.duration - a_rep["duration"]) > 0.001
            ):
                raise ValueError(f"Cloud audio metadata mismatch on chapter {ch} segment {seg.seq_id}")

        local_manifest = load_manifest(local_chap)
        expected_plan = resolve_visual_plan(
            local_chap,
            local_manifest,
            incoming_script,
            target_width=settings["target_width"],
            target_height=settings["target_height"],
            fps=settings["fps"],
            motion_enabled=settings["render_pan_mode"] == "animation",
        )
        plan = VisualPlan.load(incoming_chap / "visual_plan.json")
        if expected_plan != plan:
            raise ValueError(f"Cloud VisualPlan semantic parity failed on chapter {ch}")

        quality = "final"
        video_file = incoming_chap / "video" / f"{quality}.mp4"
        srt_file = incoming_chap / "subtitles" / f"{quality}.srt"
        if ch == "1":
            if not video_file.is_file() or fingerprint(video_file) != approval.artifact_hash:
                raise ValueError("Chapter 1 final video differs from approved preview artifact hash")

        report = validate_final_artifact(
            video_file,
            srt_file,
            audio_paths,
            plan,
            incoming_script,
            local_manifest,
            ch,
            chapter_dir=local_chap,
        )
        validated_chapters[ch] = {
            "incoming": incoming_chap,
            "local": local_chap,
            "script": incoming_script,
            "plan": plan,
            "report": report,
            "quality": quality,
        }

    # Publish all validated chapters only after EVERY chapter & G8 receipt passes.
    # Acquire all chapter locks in canonical order so input verification holds across the whole commit.
    published: Dict[str, Any] = {}
    with contextlib.ExitStack() as stack:
        for ch in sorted(validated_chapters):
            stack.enter_context(acquire_lock(validated_chapters[ch]["local"] / "chapter.json"))
        _check_inputs()
        for ch, bundle in validated_chapters.items():
            local_chap = bundle["local"]
            incoming_chap = bundle["incoming"]
            metadata = load_chapter_metadata(local_chap)
            metadata.overall_status = metadata.technical_status = CheckpointStatus.RUNNING
            atomic_write_json(local_chap / "chapter.json", metadata)
            for subdir in ("audio", "video", "subtitles"):
                if (incoming_chap / subdir).exists():
                    shutil.copytree(incoming_chap / subdir, local_chap / subdir, dirs_exist_ok=True)
            atomic_write_json(local_chap / "script.json", bundle["script"])
            atomic_write_json(local_chap / "visual_plan.json", bundle["plan"])
            atomic_write_json(local_chap / "artifact_validation.json", bundle["report"])
            metadata.stages[StageType.AUDIO].status = CheckpointStatus.COMPLETED
            metadata.stages[StageType.RENDER].status = CheckpointStatus.COMPLETED
            metadata.technical_status = CheckpointStatus.COMPLETED
            metadata.overall_status = CheckpointStatus.COMPLETED
            metadata.human_review_status = (
                HumanReviewStatus.APPROVED if ch == "1" else HumanReviewStatus.UNREVIEWED
            )
            atomic_write_json(local_chap / "chapter.json", metadata)

            published[ch] = {
                "video_path": str(local_chap / "video" / f"{bundle['quality']}.mp4"),
                "subtitle_path": str(local_chap / "subtitles" / f"{bundle['quality']}.srt"),
                "validation": bundle["report"],
                "semantic_parity": True,
            }

    receipts_dest = project_dir / "cloud_receipts" / "dual_t4"
    receipts_dest.mkdir(parents=True, exist_ok=True)
    for fname in G8_REQUIRED_RECEIPTS:
        shutil.copy2(downloaded_dir / fname, receipts_dest / fname)

    feat_src = downloaded_dir / "outputs" / "full_recap_feature.mp4"
    compiled_out: Optional[str] = None
    if feat_src.is_file():
        feat_dst = project_dir / "outputs" / "full_recap_feature.mp4"
        feat_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(feat_src, feat_dst)
        compiled_out = str(feat_dst)

    return {
        "status": "success",
        "job_type": "produce_series",
        "project_slug": slug,
        "chapters": target_chapters,
        "published": published,
        "g8_evidence": g8_report,
        "compiled_video": compiled_out,
        "receipts_dir": str(receipts_dest),
    }
