"""Importable Kaggle runtime bootstrap and safe tar/payload verification."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any, Dict, Union


def safe_extract_tar(tar: tarfile.TarFile, target_dir: Union[str, Path]) -> None:
    """
    Safely extracts a tar archive preventing directory traversal (zip-slip / tar-slip).
    Rejects non-regular files (symlinks, devices), duplicates, and paths outside ``target_dir``.
    """
    resolved_target = Path(target_dir).resolve()
    seen = set()
    for member in tar.getmembers():
        if not (member.isfile() or member.isdir()):
            raise ValueError(f"Security error: Unsupported tar member: '{member.name}'")
        normalized = (resolved_target / member.name).resolve()
        if normalized in seen:
            raise ValueError(f"Security error: Duplicate tar member: '{member.name}'")
        seen.add(normalized)
        if member.name.startswith("/") or ".." in member.name.split("/"):
            raise ValueError(f"Security error: Attempted Path Traversal in Tar File: '{member.name}'")
        try:
            normalized.relative_to(resolved_target)
        except ValueError as exc:
            raise ValueError(f"Security error: Attempted Path Traversal in Tar File: '{member.name}'") from exc
    try:
        tar.extractall(path=resolved_target, filter="data")
    except TypeError:
        tar.extractall(path=resolved_target)


def unpack_input_payload(input_dir: Path, runtime_root: Path) -> None:
    """Unpacks single ``payload.tar.gz`` or copies unzipped dataset tree into ``runtime_root``."""
    tarballs = list(input_dir.rglob("payload.tar.gz"))
    if len(tarballs) == 1:
        with tarfile.open(tarballs[0]) as archive:
            safe_extract_tar(archive, runtime_root)
        return
    manifests = list(input_dir.rglob("payload_manifest.json"))
    if len(manifests) != 1:
        raise RuntimeError(
            f"Payload not found in {input_dir} (tarballs={len(tarballs)}, manifests={len(manifests)})"
        )
    shutil.copytree(manifests[0].parent, runtime_root, dirs_exist_ok=True)


def verify_runtime_files(runtime_root: Path, expected_manifest_hash: str) -> Dict[str, Any]:
    """Verifies ``payload_manifest.json`` SHA-256 and exact inventory/checksums of all runtime files."""
    manifest_path = runtime_root / "payload_manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError(f"Missing payload_manifest.json in {runtime_root}")
    if hashlib.sha256(manifest_path.read_bytes()).hexdigest() != expected_manifest_hash:
        raise RuntimeError("Manifest changed")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = {
        p.relative_to(runtime_root).as_posix()
        for p in runtime_root.rglob("*")
        if p.is_file() and p != manifest_path
    }
    expected_files = manifest.get("files") or {}
    if files != set(expected_files.keys()):
        raise RuntimeError("Payload inventory mismatch")

    for name, expected_digest in expected_files.items():
        checksum = hashlib.sha256()
        with (runtime_root / name).open("rb") as fh:
            while block := fh.read(1 << 20):
                checksum.update(block)
        if checksum.hexdigest() != expected_digest:
            raise RuntimeError(f"Payload hash mismatch for {name}")
    return manifest


def bootstrap_runtime(
    expected_manifest_hash: str,
    *,
    encoder: str = "h264_nvenc",
    render_workers: int = 2,
    compile_feature: bool = False,
    input_dir: Path = Path("/kaggle/input"),
    runtime_root: Path = Path("/kaggle/working/runtime"),
    output_dir: Path = Path("/kaggle/working/results"),
    install_deps: bool = True,
) -> None:
    """Importable Kaggle bootstrap verifying payload integrity and running dual-GPU pipeline."""
    sys.dont_write_bytecode = True
    runtime_root.mkdir(parents=True, exist_ok=True)
    try:
        unpack_input_payload(input_dir, runtime_root)
        verify_runtime_files(runtime_root, expected_manifest_hash)
        if install_deps:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r", str(runtime_root / "requirements.txt")],
                check=True,
            )
        root_str = str(runtime_root)
        if root_str not in sys.path:
            sys.path.insert(0, root_str)
        from src.cloud.dual_gpu_worker import run

        run(
            runtime_root,
            expected_manifest_hash,
            output_dir,
            encoder=encoder,
            render_workers=render_workers,
            compile_feature=compile_feature,
        )
    finally:
        shutil.rmtree(runtime_root, ignore_errors=True)


WORKER_STUB_TEMPLATE = """import sys, subprocess, shutil
from pathlib import Path

def main():
    sys.dont_write_bytecode = True
    root = Path('/kaggle/working/runtime')
    root.mkdir()
    try:
        tarballs = list(Path('/kaggle/input').rglob('payload.tar.gz'))
        if len(tarballs) == 1:
            import tarfile
            with tarfile.open(tarballs[0]) as archive:
                seen = set()
                for entry in archive.getmembers():
                    target = (root / entry.name).resolve()
                    if not target.is_relative_to(root) or not entry.isfile() or target in seen:
                        raise RuntimeError('Unsafe payload member')
                    seen.add(target)
                archive.extractall(root)
        else:
            manifests = list(Path('/kaggle/input').rglob('payload_manifest.json'))
            if len(manifests) != 1:
                raise RuntimeError(f'Payload not found in /kaggle/input (tarballs={len(tarballs)}, manifests={len(manifests)})')
            shutil.copytree(manifests[0].parent, root, dirs_exist_ok=True)
        sys.path.insert(0, str(root))
        from src.cloud.bootstrap import verify_runtime_files
        from src.cloud.dual_gpu_worker import run
        expected = __EXPECTED_HASH__
        verify_runtime_files(root, expected)
        subprocess.run([sys.executable, '-m', 'pip', 'install', '-r', str(root / 'requirements.txt')], check=True)
        run(root, expected, Path('/kaggle/working/results'), encoder=__ENCODER__,
            render_workers=__RENDER_WORKERS__, compile_feature=__COMPILE_FEATURE__)
    finally:
        shutil.rmtree(root, ignore_errors=True)

if __name__ == '__main__':
    main()
"""


def render_worker_stub(
    *,
    expected_hash: str,
    encoder: str,
    render_workers: int,
    compile_feature: bool,
) -> str:
    """Renders the minimal spawn-safe Kaggle worker launcher invoking bootstrap & dual-GPU runner."""
    return (
        WORKER_STUB_TEMPLATE
        .replace("__EXPECTED_HASH__", repr(expected_hash))
        .replace("__ENCODER__", repr(encoder))
        .replace("__RENDER_WORKERS__", repr(render_workers))
        .replace("__COMPILE_FEATURE__", repr(compile_feature))
    )
