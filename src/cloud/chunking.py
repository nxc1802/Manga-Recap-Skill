"""Workload-sized session chunk planning, per-chapter verified receipts, and incremental resume."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from src.cloud.lifecycle import MAX_KAGGLE_GPU_SESSION_SEC, estimate_workload_budgets
from src.core.errors import ProviderIntegrityError
from src.core.logger import logger
from src.core.storage import atomic_write_json, calculate_sha256, load_recap_script, read_json
from src.core.utils import assert_file_nonempty
from src.providers.kaggle import PROVIDER_NAME


DEFAULT_CHUNK_RUNTIME_CAP_SEC = 25200  # 7h target ceiling (leaving 2h margin under Kaggle's 9h session cap)
DEFAULT_CHUNK_PAYLOAD_CAP_MB = 15360.0  # 15 GiB compressed payload ceiling


def _utc_iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ChapterWorkloadMetric:
    """Estimated runtime and payload/scratch resource footprint for one chapter."""

    chapter_number: str
    segment_count: int
    estimated_runtime_sec: float
    payload_bytes: int
    estimated_scratch_mb: float

    @property
    def payload_mb(self) -> float:
        return round(self.payload_bytes / (1024.0 * 1024.0), 3)


@dataclass(frozen=True)
class SeriesChunk:
    """Contiguous bounded slice of chapters planned for a single Kaggle session."""

    chunk_index: int
    total_chunks: int
    chapters: List[str]
    includes_preview_gate: bool
    estimated_runtime_sec: float
    estimated_payload_mb: float
    estimated_scratch_mb: float

    def as_dict(self) -> Dict[str, Any]:
        return {
            "chunk_index": self.chunk_index,
            "total_chunks": self.total_chunks,
            "chapters": list(self.chapters),
            "includes_preview_gate": self.includes_preview_gate,
            "estimated_runtime_sec": round(self.estimated_runtime_sec, 2),
            "estimated_payload_mb": round(self.estimated_payload_mb, 2),
            "estimated_scratch_mb": round(self.estimated_scratch_mb, 2),
        }


def _chapter_payload_size_bytes(chapter_dir: Path, job_type: str) -> int:
    total = 0
    for name in ("chapter.json", "script.json", "manifest.json", "visual_plan.json"):
        p = chapter_dir / name
        if p.is_file():
            total += p.stat().st_size
    if job_type != "tts":
        panels_dir = chapter_dir / "panels"
        if panels_dir.is_dir():
            for p in panels_dir.rglob("*"):
                if p.is_file():
                    total += p.stat().st_size
    audio_dir = chapter_dir / "audio"
    if audio_dir.is_dir():
        for p in audio_dir.rglob("*.wav"):
            if p.is_file():
                total += p.stat().st_size
    return total


def estimate_chapter_workload_metrics(
    project_dir: Path,
    chapters: Sequence[str],
    job_type: str,
) -> List[ChapterWorkloadMetric]:
    """Computes per-chapter runtime and storage estimates for chunk planning."""
    root = Path(project_dir)
    metrics: List[ChapterWorkloadMetric] = []
    sec_per_seg = {"tts": 4.0, "render": 10.0, "produce": 14.0}.get(job_type, 14.0)
    for num in chapters:
        ch_num = str(num)
        ch_dir = root / "chapters" / ch_num
        seg_count = 0
        script_path = ch_dir / "script.json"
        if script_path.is_file():
            try:
                data = read_json(script_path)
                segs = data.get("segments") if isinstance(data, dict) else None
                if isinstance(segs, list):
                    seg_count = len(segs)
            except Exception:
                seg_count = 20
        est_runtime = max(60.0, seg_count * sec_per_seg * 1.5)
        payload_bytes = _chapter_payload_size_bytes(ch_dir, job_type)
        # Active chapter shot scratch bounded by K3 per-chapter verified cleanup: ~3x chapter payload + headroom.
        scratch_mb = max(32.0, (payload_bytes / (1024.0 * 1024.0)) * 3.0 + seg_count * 1.5)
        metrics.append(
            ChapterWorkloadMetric(
                chapter_number=ch_num,
                segment_count=seg_count,
                estimated_runtime_sec=est_runtime,
                payload_bytes=payload_bytes,
                estimated_scratch_mb=round(scratch_mb, 2),
            )
        )
    return metrics


def estimate_disk_high_water_mb(metrics: Sequence[ChapterWorkloadMetric], *, base_mb: float = 2048.0) -> float:
    """
    With K3 immediate per-chapter scratch cleanup, peak scratch disk sits near
    base runtime + single worst active chapter * 2 + completed output accumulation.
    """
    if not metrics:
        return round(base_mb, 2)
    worst_scratch = max(m.estimated_scratch_mb for m in metrics)
    cumulative_outputs = sum(max(10.0, m.segment_count * 0.8) for m in metrics)
    return round(base_mb + worst_scratch * 1.5 + cumulative_outputs, 2)


def plan_series_chunks(
    project_dir: Path,
    chapters: Sequence[str],
    job_type: str,
    *,
    max_chunk_runtime_sec: float = DEFAULT_CHUNK_RUNTIME_CAP_SEC,
    max_chunk_payload_mb: float = DEFAULT_CHUNK_PAYLOAD_CAP_MB,
    max_chapters_per_chunk: Optional[int] = None,
    bootstrap_overhead_sec: float = 420.0,
) -> List[SeriesChunk]:
    """
    Partitions ``chapters`` into contiguous session-sized chunks respecting runtime,
    payload size, and optional chapter-count limits without splitting any single chapter.
    """
    ordered = [str(c).strip() for c in chapters]
    if not ordered:
        raise ValueError("Expected at least one chapter for chunk planning")

    runtime_budget = min(float(max_chunk_runtime_sec), float(MAX_KAGGLE_GPU_SESSION_SEC))
    metrics = estimate_chapter_workload_metrics(project_dir, ordered, job_type)
    groups: List[List[ChapterWorkloadMetric]] = []
    cur: List[ChapterWorkloadMetric] = []
    cur_runtime = float(bootstrap_overhead_sec)
    cur_payload_mb = 25.0  # shared source + watermark base overhead

    for metric in metrics:
        exceeds_count = max_chapters_per_chunk is not None and len(cur) >= int(max_chapters_per_chunk)
        exceeds_time = bool(cur) and (cur_runtime + metric.estimated_runtime_sec) > runtime_budget
        exceeds_size = bool(cur) and (cur_payload_mb + metric.payload_mb) > float(max_chunk_payload_mb)
        if exceeds_count or exceeds_time or exceeds_size:
            groups.append(cur)
            cur = [metric]
            cur_runtime = float(bootstrap_overhead_sec) + metric.estimated_runtime_sec
            cur_payload_mb = 25.0 + metric.payload_mb
        else:
            cur.append(metric)
            cur_runtime += metric.estimated_runtime_sec
            cur_payload_mb += metric.payload_mb
    if cur:
        groups.append(cur)

    total_chunks = len(groups)
    planned: List[SeriesChunk] = []
    for idx, grp in enumerate(groups):
        ch_nums = [m.chapter_number for m in grp]
        planned.append(
            SeriesChunk(
                chunk_index=idx,
                total_chunks=total_chunks,
                chapters=ch_nums,
                includes_preview_gate=any(c != "1" for c in ch_nums) or len(ordered) > 1,
                estimated_runtime_sec=round(float(bootstrap_overhead_sec) + sum(m.estimated_runtime_sec for m in grp), 2),
                estimated_payload_mb=round(25.0 + sum(m.payload_mb for m in grp), 3),
                estimated_scratch_mb=estimate_disk_high_water_mb(grp),
            )
        )
    return planned


def build_chapter_receipt(
    *,
    project_slug: str,
    chapter_number: str,
    job_type: str,
    quality: Optional[str],
    payload_manifest_sha256: str,
    exported_files: Iterable[Path],
    base_dir: Path,
    script_sha256: str,
) -> Dict[str, Any]:
    """Creates a per-chapter export receipt with exact file digests."""
    digests: Dict[str, str] = {}
    for file_path in exported_files:
        p = Path(file_path)
        assert_file_nonempty(p)
        rel = p.relative_to(base_dir).as_posix()
        digests[rel] = calculate_sha256(p)
    return {
        "schema_version": "1.0.0",
        "project_slug": project_slug,
        "chapter_number": str(chapter_number),
        "job_type": job_type,
        "quality": quality,
        "payload_manifest_sha256": payload_manifest_sha256,
        "script_sha256": script_sha256,
        "verified_at": _utc_iso_now(),
        "files": digests,
    }


def verify_chapter_receipt(chapter_root: Path, receipt_data: Dict[str, Any], *, expected_slug: str, expected_chapter: str) -> None:
    """Verifies every listed file inside ``chapter_root`` against ``receipt_data`` digests."""
    if str(receipt_data.get("project_slug")) != str(expected_slug) or str(receipt_data.get("chapter_number")) != str(expected_chapter):
        raise ProviderIntegrityError(
            f"Chapter receipt identity mismatch for chapter {expected_chapter}",
            category="manifest_mismatch",
            provider=PROVIDER_NAME,
        )
    files = receipt_data.get("files") or {}
    if not isinstance(files, dict) or not files:
        raise ProviderIntegrityError(
            f"Empty file manifest in chapter receipt for chapter {expected_chapter}",
            category="corrupt_artifact",
            provider=PROVIDER_NAME,
        )
    resolved_root = chapter_root.resolve()
    for rel, expected_sha in files.items():
        target = (resolved_root / rel).resolve()
        if not target.is_relative_to(resolved_root) or not target.is_file():
            raise ProviderIntegrityError(
                f"Receipt file missing or outside chapter root: {rel}",
                category="corrupt_artifact",
                provider=PROVIDER_NAME,
            )
        assert_file_nonempty(target)
        actual_sha = calculate_sha256(target)
        if actual_sha != expected_sha:
            raise ProviderIntegrityError(
                f"Checksum mismatch for '{rel}' in chapter {expected_chapter}: expected {expected_sha}, got {actual_sha}",
                category="corrupt_artifact",
                provider=PROVIDER_NAME,
            )


def is_chapter_verified_complete(
    project_dir: Path,
    chapter_number: str,
    job_type: str,
    *,
    series: bool = True,
) -> bool:
    """
    Returns True only when a local chapter already holds a verified ``cloud_receipt.json``
    matching its current on-disk ``script.json`` and non-empty deliverables for ``job_type``.
    """
    ch_dir = Path(project_dir) / "chapters" / str(chapter_number)
    receipt_path = ch_dir / "cloud_receipt.json"
    script_path = ch_dir / "script.json"
    if not receipt_path.is_file() or not script_path.is_file():
        return False
    try:
        slug = str(read_json(Path(project_dir) / "project.json")["slug"])
        receipt = read_json(receipt_path)
        if str(receipt.get("project_slug")) != slug or str(receipt.get("chapter_number")) != str(chapter_number):
            return False
        if receipt.get("script_sha256") and receipt.get("script_sha256") != calculate_sha256(script_path):
            return False
        # Require current narration input script hash when resuming same stage or render after tts
        if receipt.get("job_type") != job_type and not (job_type == "tts" and receipt.get("job_type") == "produce"):
            if job_type != "produce" or receipt.get("job_type") != "produce":
                return False
        quality = "preview" if (not series and str(chapter_number) == "1") else "final"
        if job_type != "tts":
            video_file = ch_dir / "video" / f"{quality}.mp4"
            sub_file = ch_dir / "subtitles" / f"{quality}.srt"
            plan_file = ch_dir / "visual_plan.json"
            if not (video_file.is_file() and sub_file.is_file() and plan_file.is_file()):
                return False
            assert_file_nonempty(video_file)
            assert_file_nonempty(sub_file)
            verify_chapter_receipt(ch_dir, receipt, expected_slug=slug, expected_chapter=str(chapter_number))
        else:
            script = load_recap_script(ch_dir)
            if not script.segments or any(s.status != "synthesized" or not s.audio_path for s in script.segments):
                return False
            verify_chapter_receipt(ch_dir, receipt, expected_slug=slug, expected_chapter=str(chapter_number))
        return True
    except Exception as exc:
        logger.debug("Chapter %s not yet verified complete: %s", chapter_number, exc)
        return False


def filter_pending_chapters(
    project_dir: Path,
    chapters: Sequence[str],
    job_type: str,
    *,
    series: bool = True,
) -> Tuple[List[str], List[str]]:
    """Splits ``chapters`` into ``(completed_verified, pending_chapters)`` preserving order."""
    done: List[str] = []
    pending: List[str] = []
    for raw in chapters:
        num = str(raw).strip()
        if is_chapter_verified_complete(project_dir, num, job_type, series=series):
            done.append(num)
        else:
            pending.append(num)
    return done, pending


def write_series_chunk_plan(state_path: Path, payload: Dict[str, Any]) -> None:
    atomic_write_json(state_path, {**payload, "updated_at": _utc_iso_now()})
