"""Centralized chapter checkpoint and status transition state machine."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Union

from src.core.errors import InvalidCheckpointTransitionError
from src.core.project import load_chapter_metadata
from src.core.schemas import (
    ChapterMetadata,
    CheckpointStatus,
    HumanReviewStatus,
    OPERATIONAL_STAGES,
    StageCheckpoint,
    StageType,
    utc_iso_now,
)
from src.core.storage import acquire_lock, atomic_write_json, read_json

VALID_TRANSITIONS: Dict[CheckpointStatus, List[CheckpointStatus]] = {
    CheckpointStatus.PENDING: [CheckpointStatus.RUNNING, CheckpointStatus.COMPLETED],
    CheckpointStatus.RUNNING: [
        CheckpointStatus.COMPLETED,
        CheckpointStatus.FAILED,
        CheckpointStatus.NEEDS_REVIEW,
        CheckpointStatus.RUNNING,
        CheckpointStatus.PENDING,
    ],
    CheckpointStatus.FAILED: [CheckpointStatus.RUNNING, CheckpointStatus.PENDING],
    CheckpointStatus.NEEDS_REVIEW: [CheckpointStatus.RUNNING, CheckpointStatus.COMPLETED],
    CheckpointStatus.COMPLETED: [CheckpointStatus.RUNNING],  # Allowed for force re-runs
}

STAGE_EXECUTION_ORDER: List[StageType] = [
    StageType.CRAWL,
    StageType.SEGMENT,
    StageType.SCRIPT,
    StageType.TTS,
    StageType.RENDER,
    StageType.FINALIZE,
]

OPERATIONAL_STAGE_ORDER: List[StageType] = list(OPERATIONAL_STAGES)


def _chapter_file(chapter_path: Path) -> Path:
    p = Path(chapter_path)
    return p if p.suffix == ".json" else p / "chapter.json"


def update_checkpoint(
    chapter_path: Path,
    stage: Union[str, StageType],
    status: Union[str, CheckpointStatus],
    error: Optional[str] = None,
    artifacts: Optional[List[str]] = None,
    checksums: Optional[Dict[str, str]] = None,
    human_review_status: Optional[Union[str, HumanReviewStatus]] = None,
) -> ChapterMetadata:
    """Thread- and process-safe atomic update to chapter.json checkpoints."""
    chap_file = _chapter_file(chapter_path)
    target_stage = StageType(stage) if isinstance(stage, str) else stage
    target_status = CheckpointStatus(status) if isinstance(status, str) else status

    with acquire_lock(chap_file):
        meta = ChapterMetadata.model_validate(read_json(chap_file))
        stage_cp = meta.stages.get(target_stage)
        if not stage_cp:
            stage_cp = StageCheckpoint(stage=target_stage, status=CheckpointStatus.PENDING)
            meta.stages[target_stage] = stage_cp

        allowed = VALID_TRANSITIONS.get(stage_cp.status, [])
        if target_status not in allowed:
            raise InvalidCheckpointTransitionError(
                f"Illegal transition for stage '{target_stage.value}': "
                f"'{stage_cp.status.value}' -> '{target_status.value}'"
            )

        now_str = utc_iso_now()
        stage_cp.status = target_status

        if target_status == CheckpointStatus.RUNNING:
            stage_cp.started_at = now_str
            stage_cp.error_message = None
            meta.current_stage = target_stage
            meta.overall_status = CheckpointStatus.RUNNING
        elif target_status == CheckpointStatus.COMPLETED:
            stage_cp.completed_at = now_str
            stage_cp.error_message = None
            if artifacts:
                stage_cp.artifacts = sorted(set(stage_cp.artifacts + artifacts))
            if checksums:
                stage_cp.checksums.update(checksums)

            legacy_complete = all(
                meta.stages.get(s) and meta.stages[s].status == CheckpointStatus.COMPLETED
                for s in STAGE_EXECUTION_ORDER
            )
            op_complete = all(
                meta.stages.get(s) and meta.stages[s].status == CheckpointStatus.COMPLETED
                for s in [StageType.INGEST, StageType.AGENT, StageType.AUDIO, StageType.RENDER]
            )
            if legacy_complete or op_complete or target_stage in (StageType.FINALIZE, StageType.EXPORT):
                meta.overall_status = CheckpointStatus.COMPLETED
        elif target_status == CheckpointStatus.FAILED:
            stage_cp.error_message = error or "Unspecified error occurred"
            stage_cp.retry_count += 1
            meta.overall_status = CheckpointStatus.FAILED
        elif target_status == CheckpointStatus.NEEDS_REVIEW:
            stage_cp.error_message = error
            meta.overall_status = CheckpointStatus.NEEDS_REVIEW

        meta.technical_status = meta.overall_status
        if human_review_status is not None:
            meta.human_review_status = (
                HumanReviewStatus(human_review_status)
                if isinstance(human_review_status, str)
                else human_review_status
            )
        meta.updated_at = now_str

        atomic_write_json(chap_file, meta)
        return meta


def reset_stage_checkpoint(
    chapter_path: Path,
    stage: Union[str, StageType],
) -> ChapterMetadata:
    """Resets a stage checkpoint back to PENDING and clears its artifacts and checksums."""
    chap_file = _chapter_file(chapter_path)
    target_stage = StageType(stage) if isinstance(stage, str) else stage

    with acquire_lock(chap_file):
        meta = ChapterMetadata.model_validate(read_json(chap_file))
        stage_cp = meta.stages.get(target_stage)
        if not stage_cp:
            stage_cp = StageCheckpoint(stage=target_stage, status=CheckpointStatus.PENDING)
            meta.stages[target_stage] = stage_cp
        else:
            stage_cp.status = CheckpointStatus.PENDING
            stage_cp.started_at = None
            stage_cp.completed_at = None
            stage_cp.error_message = None
            stage_cp.artifacts = []
            stage_cp.checksums = {}

        if meta.overall_status == CheckpointStatus.COMPLETED:
            meta.overall_status = CheckpointStatus.PENDING
        meta.technical_status = meta.overall_status
        meta.human_review_status = HumanReviewStatus.UNREVIEWED
        meta.updated_at = utc_iso_now()

        atomic_write_json(chap_file, meta)
        return meta


def get_resumable_stage(chapter_path: Path) -> Optional[StageType]:
    """Inspects chapter.json to find the first stage requiring execution."""
    chap_file = _chapter_file(chapter_path)
    meta = ChapterMetadata.model_validate(read_json(chap_file))

    if meta.overall_status == CheckpointStatus.COMPLETED:
        return None

    has_op = any(
        meta.stages.get(s) and meta.stages[s].status != CheckpointStatus.PENDING
        for s in (StageType.INGEST, StageType.AGENT, StageType.AUDIO)
    )
    order_to_check = OPERATIONAL_STAGE_ORDER if has_op else STAGE_EXECUTION_ORDER

    for stage in order_to_check:
        cp = meta.stages.get(stage)
        if not cp or cp.status in (
            CheckpointStatus.PENDING,
            CheckpointStatus.FAILED,
            CheckpointStatus.NEEDS_REVIEW,
            CheckpointStatus.RUNNING,
        ):
            return stage
    return None


def record_chapter_render_result(
    chapter_path: Path,
    *,
    quality: str,
    promote: bool,
    video_path: Union[str, Path],
    duration_seconds: float,
) -> ChapterMetadata:
    """Atomically updates chapter status and render stats after verified rendering."""
    chap_file = _chapter_file(chapter_path)
    with acquire_lock(chap_file):
        meta = load_chapter_metadata(chap_file)
        meta.technical_status = CheckpointStatus.COMPLETED
        meta.overall_status = (
            CheckpointStatus.NEEDS_REVIEW if quality == "preview" else CheckpointStatus.COMPLETED
        )
        meta.human_review_status = (
            HumanReviewStatus.APPROVED if promote else HumanReviewStatus.UNREVIEWED
        )
        meta.stats.video_path = str(video_path)
        meta.stats.final_video_duration_seconds = duration_seconds
        meta.updated_at = utc_iso_now()
        atomic_write_json(chap_file, meta)
        return meta


def mark_chapter_human_approved(chapter_path: Path) -> Optional[ChapterMetadata]:
    """Updates chapter.json human_review_status to APPROVED if chapter.json exists."""
    chap_file = _chapter_file(chapter_path)
    if not chap_file.is_file():
        return None
    with acquire_lock(chap_file):
        meta = load_chapter_metadata(chap_file)
        meta.human_review_status = HumanReviewStatus.APPROVED
        if meta.technical_status == CheckpointStatus.COMPLETED:
            meta.overall_status = CheckpointStatus.COMPLETED
        meta.updated_at = utc_iso_now()
        atomic_write_json(chap_file, meta)
        return meta
