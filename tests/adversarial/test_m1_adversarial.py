"""
tests/adversarial/test_m1_adversarial.py
Adversarial empirical stress testing suite for Milestone M1 (PR-00 & PR-01).

Objectives:
1. Concurrency stress testing (multi-threading, multi-processing, FileLock mutual exclusion, deadlock freedom).
2. Crash consistency / atomic write challenge (interrupted writes, serialization failures, simulated I/O errors, malformed payloads).
3. Checkpoint state machine validation (exhaustive transition matrix, illegal jumps, type enforcement, aggregate overall status).
"""

import json
import multiprocessing as mp
import os
from pathlib import Path
import threading
import time
from unittest.mock import patch
import pytest

from src.core.schemas import (
    ChapterMetadata,
    CheckpointStatus,
    ProjectConfig,
    StageCheckpoint,
    StageType,
)
from src.core.storage import (
    FileLock,
    InvalidCheckpointTransitionError,
    STAGE_EXECUTION_ORDER,
    StorageCorruptError,
    StorageFileNotFoundError,
    StorageLockTimeoutError,
    StorageWriteError,
    acquire_lock,
    atomic_write_json,
    get_resumable_stage,
    init_chapter_layout,
    read_json,
    update_checkpoint,
)


# ==============================================================================
# Helper worker functions for multiprocessing (top-level pickleable)
# ==============================================================================

def _mp_worker_counter_increment(file_path_str: str, num_increments: int, worker_id: int):
    """Multiprocessing worker that increments a counter in a locked JSON file."""
    file_path = Path(file_path_str)
    for _ in range(num_increments):
        with acquire_lock(file_path, timeout=10.0, retry_interval=0.01):
            data = read_json(file_path)
            data["counter"] += 1
            data["history"].append({"worker": worker_id, "counter": data["counter"]})
            atomic_write_json(file_path, data)
        time.sleep(0.001)


def _mp_worker_checkpoint_contention(chap_file_str: str, worker_id: int, iterations: int):
    """Multiprocessing worker that acquires lock and records worker heartbeats in chapter.json."""
    chap_file = Path(chap_file_str)
    for _ in range(iterations):
        with acquire_lock(chap_file, timeout=10.0, retry_interval=0.01):
            data = read_json(chap_file)
            meta = ChapterMetadata.model_validate(data)
            meta.stats.total_segments += 1
            atomic_write_json(chap_file, meta)
        time.sleep(0.001)


# ==============================================================================
# 1. Concurrency Stress Testing
# ==============================================================================

class TestConcurrencyStress:

    def test_concurrent_threads_atomic_write_no_corruption(self, tmp_path):
        """
        Stress-test atomic_write_json under heavy concurrent multi-threaded writes and reads.
        Verifies that readers never read corrupt or half-written JSON data.
        """
        target = tmp_path / "shared_data.json"
        atomic_write_json(target, {"initial": True, "count": 0})

        num_writers = 10
        writes_per_thread = 30
        stop_reader = threading.Event()
        read_errors = []
        successful_reads = [0]

        def reader_worker():
            while not stop_reader.is_set():
                try:
                    data = read_json(target)
                    assert isinstance(data, dict)
                    assert "count" in data or "initial" in data
                    successful_reads[0] += 1
                except Exception as exc:
                    read_errors.append(exc)
                time.sleep(0.001)

        def writer_worker(thread_id: int):
            for i in range(writes_per_thread):
                payload = {
                    "thread_id": thread_id,
                    "iteration": i,
                    "count": thread_id * 1000 + i,
                    "items": list(range(50)),
                    "nested": {"status": "ok", "timestamp": time.time()}
                }
                atomic_write_json(target, payload)
                time.sleep(0.001)

        reader_thread = threading.Thread(target=reader_worker, daemon=True)
        reader_thread.start()

        threads = [threading.Thread(target=writer_worker, args=(t,)) for t in range(num_writers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        stop_reader.set()
        reader_thread.join(timeout=1.0)

        # Reader must never observe corrupted JSON
        assert len(read_errors) == 0, f"Observed read errors during concurrent write: {read_errors}"
        assert successful_reads[0] > 0

        # Final file must be valid JSON
        final_data = read_json(target)
        assert "thread_id" in final_data
        assert "items" in final_data
        assert len(final_data["items"]) == 50

    def test_concurrent_threads_filelock_mutual_exclusion(self, tmp_path):
        """
        Verifies that FileLock prevents lost updates under concurrent read-modify-write.
        10 threads each incrementing a counter 20 times must reach exactly 200.
        """
        target = tmp_path / "counter.json"
        atomic_write_json(target, {"counter": 0})

        num_threads = 10
        increments_per_thread = 20

        def increment_worker():
            for _ in range(increments_per_thread):
                with acquire_lock(target, timeout=10.0, retry_interval=0.01):
                    data = read_json(target)
                    data["counter"] += 1
                    atomic_write_json(target, data)
                time.sleep(0.001)

        threads = [threading.Thread(target=increment_worker) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        final_data = read_json(target)
        expected = num_threads * increments_per_thread
        assert final_data["counter"] == expected, f"Lost updates! Expected {expected}, got {final_data['counter']}"

    def test_multiprocess_filelock_mutual_exclusion(self, tmp_path):
        """
        Verifies that FileLock enforces true mutual exclusion across independent OS processes.
        4 processes each performing 15 atomic locked increments = exactly 60.
        """
        target = tmp_path / "mp_counter.json"
        atomic_write_json(target, {"counter": 0, "history": []})

        num_processes = 4
        increments_per_proc = 15

        ctx = mp.get_context("spawn")
        processes = [
            ctx.Process(target=_mp_worker_counter_increment, args=(str(target), increments_per_proc, pid))
            for pid in range(num_processes)
        ]

        for p in processes:
            p.start()
        for p in processes:
            p.join(timeout=30.0)
            assert not p.is_alive(), f"Process {p.pid} hung or deadlocked!"
            assert p.exitcode == 0, f"Process {p.pid} exited with error code {p.exitcode}"

        final_data = read_json(target)
        expected = num_processes * increments_per_proc
        assert final_data["counter"] == expected, f"MP Lost updates: expected {expected}, got {final_data['counter']}"
        assert len(final_data["history"]) == expected

    def test_multiprocess_checkpoint_contention(self, tmp_path, monkeypatch):
        """
        Verifies concurrent processes reading and updating chapter.json under FileLock
        produces valid ChapterMetadata without corruption or deadlocks.
        """
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path))
        chap_dir = init_chapter_layout("mp_test_slug", 1, "Multiprocess Test")
        chap_file = chap_dir / "chapter.json"

        num_processes = 4
        iterations_per_proc = 10

        ctx = mp.get_context("spawn")
        processes = [
            ctx.Process(target=_mp_worker_checkpoint_contention, args=(str(chap_file), pid, iterations_per_proc))
            for pid in range(num_processes)
        ]

        for p in processes:
            p.start()
        for p in processes:
            p.join(timeout=30.0)
            assert not p.is_alive(), f"Process {p.pid} timed out / deadlocked"
            assert p.exitcode == 0

        # Validate final metadata
        data = read_json(chap_file)
        meta = ChapterMetadata.model_validate(data)
        expected_segments = num_processes * iterations_per_proc
        assert meta.stats.total_segments == expected_segments

    def test_filelock_timeout_under_contention(self, tmp_path):
        """
        Verifies that acquire_lock raises StorageLockTimeoutError when another holder
        does not release within timeout.
        """
        res = tmp_path / "locked_resource.json"
        lock1 = acquire_lock(res, timeout=5.0)
        lock1.acquire()

        try:
            lock2 = acquire_lock(res, timeout=0.1, retry_interval=0.02)
            with pytest.raises(StorageLockTimeoutError) as exc_info:
                lock2.acquire()
            assert "Timed out after 0.1s" in str(exc_info.value)
        finally:
            lock1.release()

        # After lock1 releases, lock2 should acquire immediately
        lock2 = acquire_lock(res, timeout=1.0)
        lock2.acquire()
        lock2.release()

    def test_filelock_exception_in_context_manager_releases(self, tmp_path):
        """
        Verifies that exceptions raised inside `with acquire_lock` block cleanly
        release the lock file descriptor, preventing deadlocks for subsequent callers.
        """
        res = tmp_path / "exceptional.json"

        with pytest.raises(RuntimeError):
            with acquire_lock(res, timeout=1.0):
                raise RuntimeError("Simulated crash inside critical section")

        # Must be able to immediately re-acquire
        with acquire_lock(res, timeout=0.2):
            pass


# ==============================================================================
# 2. Crash Consistency & Atomic Write Challenge
# ==============================================================================

class TestCrashConsistencyAndAtomicWrite:

    def test_atomic_write_unserializable_payload_leaves_target_intact(self, tmp_path):
        """
        Verifies that an un-serializable payload (e.g. object() or circular reference)
        fails BEFORE modifying the target file, leaves the existing target file intact,
        and does not leak temporary files.
        """
        target = tmp_path / "important_state.json"
        original_data = {"status": "pristine", "version": 1}
        atomic_write_json(target, original_data)

        # 1. Unserializable object() without __dict__ raises TypeError
        with pytest.raises(TypeError):
            atomic_write_json(target, {"status": "corrupt", "raw_obj": object()})

        # Target file must be unchanged
        assert read_json(target) == original_data

        # 2. Circular reference raises ValueError
        circular = {"status": "corrupt"}
        circular["self"] = circular
        with pytest.raises(ValueError):
            atomic_write_json(target, circular)

        assert read_json(target) == original_data

        # No temp files leaked in directory
        tmp_files = list(tmp_path.glob(".*tmp*"))
        assert len(tmp_files) == 0, f"Leaked temp files: {tmp_files}"

    def test_atomic_write_simulated_io_failure_cleans_up_temp_file(self, tmp_path):
        """
        Verifies that an I/O failure during file writing raises StorageWriteError,
        unlinks the temporary file, and leaves the original file untouched.
        """
        target = tmp_path / "state_file.json"
        original_data = {"status": "original"}
        atomic_write_json(target, original_data)

        # Monkeypatch open to simulate write error (e.g. disk full)
        with patch("builtins.open", side_effect=IOError("Simulated disk full")):
            with pytest.raises(StorageWriteError) as exc_info:
                atomic_write_json(target, {"status": "new_state"})
            assert "Failed to atomically write JSON" in str(exc_info.value)

        # Original file preserved
        assert read_json(target) == original_data

        # Temp file cleaned up
        tmp_files = list(tmp_path.glob(".*tmp*"))
        assert len(tmp_files) == 0

    def test_atomic_write_simulated_replace_failure(self, tmp_path):
        """
        Verifies that an error during os.replace raises StorageWriteError,
        cleans up the temporary file, and preserves the original file.
        """
        target = tmp_path / "replace_target.json"
        original_data = {"intact": True}
        atomic_write_json(target, original_data)

        with patch("os.replace", side_effect=OSError("Simulated filesystem rename failure")):
            with pytest.raises(StorageWriteError) as exc_info:
                atomic_write_json(target, {"intact": False})
            assert "Failed to atomically write JSON" in str(exc_info.value)

        # Original file preserved
        assert read_json(target) == original_data

        # Temp files removed
        tmp_files = list(tmp_path.glob(".*tmp*"))
        assert len(tmp_files) == 0

    def test_read_json_corrupted_payloads_raise_storage_corrupt_error(self, tmp_path):
        """
        Verifies that read_json cleanly catches truncated, malformed, or non-UTF-8 files
        and raises StorageCorruptError (subclass of ValueError and StorageError).
        """
        bad_json = tmp_path / "truncated.json"
        bad_json.write_text('{"key": "value", "truncated": ', encoding="utf-8")

        with pytest.raises(StorageCorruptError):
            read_json(bad_json)
        assert issubclass(StorageCorruptError, ValueError)

        # Non-UTF8 binary file
        binary_file = tmp_path / "binary.json"
        binary_file.write_bytes(b"\x80\xff\xfe\x00\x01\x02")

        with pytest.raises(StorageCorruptError):
            read_json(binary_file)

    def test_read_json_nonexistent_file_raises_storage_file_not_found(self, tmp_path):
        """
        Verifies that read_json raises StorageFileNotFoundError (subclass of FileNotFoundError).
        """
        missing = tmp_path / "does_not_exist.json"
        with pytest.raises(StorageFileNotFoundError):
            read_json(missing)
        assert issubclass(StorageFileNotFoundError, FileNotFoundError)

    def test_atomic_write_creates_deep_nested_directories(self, tmp_path):
        """
        Verifies atomic_write_json transparently creates parent directories.
        """
        target = tmp_path / "level1" / "level2" / "level3" / "deep.json"
        atomic_write_json(target, {"level": 3})
        assert target.exists()
        assert read_json(target) == {"level": 3}


# ==============================================================================
# 3. Checkpoint State Machine Validation
# ==============================================================================

class TestCheckpointStateMachineValidation:

    @pytest.fixture
    def isolated_chapter_file(self, tmp_path, monkeypatch) -> Path:
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path))
        # Use unique subpath for every test
        chap_dir = tmp_path / "chapters" / "1"
        chap_dir.mkdir(parents=True, exist_ok=True)
        chap_file = chap_dir / "chapter.json"
        meta = ChapterMetadata(
            chapter_id="test_slug_chap_1",
            chapter_number="1",
            project_slug="test_slug",
            title="State Machine Test"
        )
        atomic_write_json(chap_file, meta)
        return chap_file

    def test_all_valid_checkpoint_lifecycle_transitions(self, isolated_chapter_file):
        """
        Verifies the full set of legal transitions defined in VALID_TRANSITIONS.
        """
        chapter_file = isolated_chapter_file

        # 1. PENDING -> RUNNING
        meta = update_checkpoint(chapter_file, StageType.CRAWL, CheckpointStatus.RUNNING)
        assert meta.stages[StageType.CRAWL].status == CheckpointStatus.RUNNING
        assert meta.stages[StageType.CRAWL].started_at is not None
        assert meta.overall_status == CheckpointStatus.RUNNING

        # 2. RUNNING -> FAILED
        meta = update_checkpoint(chapter_file, StageType.CRAWL, CheckpointStatus.FAILED, error="Network timeout")
        assert meta.stages[StageType.CRAWL].status == CheckpointStatus.FAILED
        assert meta.stages[StageType.CRAWL].error_message == "Network timeout"
        assert meta.stages[StageType.CRAWL].retry_count == 1
        assert meta.overall_status == CheckpointStatus.FAILED

        # 3. FAILED -> RUNNING (retry)
        meta = update_checkpoint(chapter_file, StageType.CRAWL, CheckpointStatus.RUNNING)
        assert meta.stages[StageType.CRAWL].status == CheckpointStatus.RUNNING
        assert meta.stages[StageType.CRAWL].error_message is None

        # 4. RUNNING -> NEEDS_REVIEW
        meta = update_checkpoint(chapter_file, StageType.CRAWL, CheckpointStatus.NEEDS_REVIEW, error="Uncertain panels")
        assert meta.stages[StageType.CRAWL].status == CheckpointStatus.NEEDS_REVIEW
        assert meta.overall_status == CheckpointStatus.NEEDS_REVIEW

        # 5. NEEDS_REVIEW -> RUNNING
        meta = update_checkpoint(chapter_file, StageType.CRAWL, CheckpointStatus.RUNNING)
        assert meta.stages[StageType.CRAWL].status == CheckpointStatus.RUNNING

        # 6. RUNNING -> COMPLETED
        meta = update_checkpoint(chapter_file, StageType.CRAWL, CheckpointStatus.COMPLETED, artifacts=["raw/001.jpg"])
        assert meta.stages[StageType.CRAWL].status == CheckpointStatus.COMPLETED
        assert "raw/001.jpg" in meta.stages[StageType.CRAWL].artifacts

        # 7. COMPLETED -> RUNNING (force rerun)
        meta = update_checkpoint(chapter_file, StageType.CRAWL, CheckpointStatus.RUNNING)
        assert meta.stages[StageType.CRAWL].status == CheckpointStatus.RUNNING
        assert meta.overall_status == CheckpointStatus.RUNNING

        # 8. Re-complete
        meta = update_checkpoint(chapter_file, StageType.CRAWL, CheckpointStatus.COMPLETED)
        assert meta.stages[StageType.CRAWL].status == CheckpointStatus.COMPLETED

    def test_transition_pending_to_completed_allowed(self, isolated_chapter_file):
        """
        Verifies that transitioning directly from PENDING to COMPLETED is permitted.
        """
        meta = update_checkpoint(isolated_chapter_file, StageType.FINALIZE, CheckpointStatus.COMPLETED)
        assert meta.stages[StageType.FINALIZE].status == CheckpointStatus.COMPLETED

    def test_illegal_jump_pending_to_failed_raises(self, isolated_chapter_file):
        """
        Verifies that jumping directly from PENDING to FAILED raises InvalidCheckpointTransitionError.
        """
        with pytest.raises(InvalidCheckpointTransitionError):
            update_checkpoint(isolated_chapter_file, StageType.CRAWL, CheckpointStatus.FAILED)

    def test_illegal_jump_pending_to_needs_review_raises(self, isolated_chapter_file):
        """
        Verifies that jumping directly from PENDING to NEEDS_REVIEW raises InvalidCheckpointTransitionError.
        """
        with pytest.raises(InvalidCheckpointTransitionError):
            update_checkpoint(isolated_chapter_file, StageType.SEGMENT, CheckpointStatus.NEEDS_REVIEW)

    def test_illegal_duplicate_completed_to_completed_raises(self, isolated_chapter_file):
        """
        Verifies that transitioning from COMPLETED to COMPLETED raises InvalidCheckpointTransitionError.
        """
        update_checkpoint(isolated_chapter_file, StageType.CRAWL, CheckpointStatus.RUNNING)
        update_checkpoint(isolated_chapter_file, StageType.CRAWL, CheckpointStatus.COMPLETED)

        with pytest.raises(InvalidCheckpointTransitionError):
            update_checkpoint(isolated_chapter_file, StageType.CRAWL, CheckpointStatus.COMPLETED)

    def test_illegal_completed_to_failed_or_needs_review_raises(self, isolated_chapter_file):
        """
        Verifies that COMPLETED cannot directly transition to FAILED or NEEDS_REVIEW.
        """
        update_checkpoint(isolated_chapter_file, StageType.CRAWL, CheckpointStatus.RUNNING)
        update_checkpoint(isolated_chapter_file, StageType.CRAWL, CheckpointStatus.COMPLETED)

        with pytest.raises(InvalidCheckpointTransitionError):
            update_checkpoint(isolated_chapter_file, StageType.CRAWL, CheckpointStatus.FAILED)

        with pytest.raises(InvalidCheckpointTransitionError):
            update_checkpoint(isolated_chapter_file, StageType.CRAWL, CheckpointStatus.NEEDS_REVIEW)

        with pytest.raises(InvalidCheckpointTransitionError):
            update_checkpoint(isolated_chapter_file, StageType.CRAWL, CheckpointStatus.PENDING)

    def test_invalid_stage_or_status_strings_raise_value_error(self, isolated_chapter_file):
        """
        Verifies that unparseable stage or status string values raise ValueError.
        """
        with pytest.raises(ValueError):
            update_checkpoint(isolated_chapter_file, "bogus_stage", CheckpointStatus.RUNNING)

        with pytest.raises(ValueError):
            update_checkpoint(isolated_chapter_file, StageType.CRAWL, "bogus_status")

    def test_overall_status_only_completed_when_all_stages_completed(self, isolated_chapter_file):
        """
        Verifies that overall_status becomes COMPLETED if and only if ALL 6 stages
        in STAGE_EXECUTION_ORDER are COMPLETED.
        """
        chapter_file = isolated_chapter_file

        # Execute first 5 stages to COMPLETED
        for stage in STAGE_EXECUTION_ORDER[:-1]:
            update_checkpoint(chapter_file, stage, CheckpointStatus.RUNNING)
            meta = update_checkpoint(chapter_file, stage, CheckpointStatus.COMPLETED)
            assert meta.overall_status == CheckpointStatus.RUNNING, f"Stage {stage} prematurely marked overall COMPLETED"

        # Execute final stage FINALIZE
        update_checkpoint(chapter_file, StageType.FINALIZE, CheckpointStatus.RUNNING)
        meta = update_checkpoint(chapter_file, StageType.FINALIZE, CheckpointStatus.COMPLETED)

        # Now all stages completed
        assert meta.overall_status == CheckpointStatus.COMPLETED

    def test_resumable_stage_tracking(self, isolated_chapter_file):
        """
        Verifies get_resumable_stage returns first incomplete stage in execution order.
        """
        chapter_file = isolated_chapter_file

        # Initially all pending -> CRAWL
        assert get_resumable_stage(chapter_file) == StageType.CRAWL

        # Crawl completed -> SEGMENT
        update_checkpoint(chapter_file, StageType.CRAWL, CheckpointStatus.RUNNING)
        update_checkpoint(chapter_file, StageType.CRAWL, CheckpointStatus.COMPLETED)
        assert get_resumable_stage(chapter_file) == StageType.SEGMENT

        # Segment failed -> still SEGMENT
        update_checkpoint(chapter_file, StageType.SEGMENT, CheckpointStatus.RUNNING)
        update_checkpoint(chapter_file, StageType.SEGMENT, CheckpointStatus.FAILED)
        assert get_resumable_stage(chapter_file) == StageType.SEGMENT

        # Complete segment
        update_checkpoint(chapter_file, StageType.SEGMENT, CheckpointStatus.RUNNING)
        update_checkpoint(chapter_file, StageType.SEGMENT, CheckpointStatus.COMPLETED)

        for s in [StageType.SCRIPT, StageType.TTS, StageType.RENDER, StageType.FINALIZE]:
            update_checkpoint(chapter_file, s, CheckpointStatus.RUNNING)
            update_checkpoint(chapter_file, s, CheckpointStatus.COMPLETED)

        assert get_resumable_stage(chapter_file) is None

    def test_missing_stage_in_partial_stages_dict_safely_handled(self, tmp_path):
        """
        Verifies that when chapter.json contains a partial stages dictionary,
        update_checkpoint to COMPLETED does NOT raise KeyError and safely updates.
        """
        chap_file = tmp_path / "chapter_partial.json"
        raw = {
            "chapter_id": "c1",
            "chapter_number": "1",
            "project_slug": "p1",
            "title": "Partial Test",
            "stages": {
                "crawl": {"stage": "crawl", "status": "running"}
            }
        }
        atomic_write_json(chap_file, raw)

        meta = update_checkpoint(chap_file, StageType.CRAWL, CheckpointStatus.COMPLETED)
        assert meta.stages[StageType.CRAWL].status == CheckpointStatus.COMPLETED
        assert meta.overall_status != CheckpointStatus.COMPLETED
