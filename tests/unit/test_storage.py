"""
tests/unit/test_storage.py
Comprehensive unit test suite for src/core/storage.py.
"""

import json
import os
import threading
import time
from pathlib import Path
import pytest

from src.core.schemas import (
    ChapterMetadata,
    CheckpointStatus,
    ProjectConfig,
    StageType,
)
from src.core.storage import (
    FileLock,
    InvalidCheckpointTransitionError,
    StorageCorruptError,
    StorageFileNotFoundError,
    StorageLockTimeoutError,
    acquire_lock,
    atomic_write_json,
    calculate_sha256,
    get_chapter_dir,
    get_project_dir,
    get_resumable_stage,
    init_chapter_layout,
    init_project_layout,
    read_json,
    update_checkpoint,
    verify_checksum,
)


class TestAtomicWriteJson:
    def test_atomic_write_creates_file(self, tmp_path):
        target = tmp_path / "subdir" / "test.json"
        data = {"hello": "world", "number": 42}
        atomic_write_json(target, data)

        assert target.exists()
        loaded = json.loads(target.read_text(encoding="utf-8"))
        assert loaded == data

    def test_atomic_write_pydantic_model(self, tmp_path):
        target = tmp_path / "project.json"
        cfg = ProjectConfig(project_id="p1", slug="s1", title="Title")
        atomic_write_json(target, cfg)

        assert target.exists()
        loaded = json.loads(target.read_text(encoding="utf-8"))
        assert loaded["slug"] == "s1"
        assert loaded["schema_version"] == "3.0.0"

    def test_atomic_write_overwrites_cleanly(self, tmp_path):
        target = tmp_path / "state.json"
        atomic_write_json(target, {"version": 1})
        atomic_write_json(target, {"version": 2})

        loaded = json.loads(target.read_text(encoding="utf-8"))
        assert loaded["version"] == 2


class TestReadJson:
    def test_read_json_success(self, tmp_path):
        target = tmp_path / "test.json"
        target.write_text('{"status": "ok"}', encoding="utf-8")
        assert read_json(target) == {"status": "ok"}

    def test_read_json_missing_raises_filenotfound(self, tmp_path):
        target = tmp_path / "nonexistent.json"
        with pytest.raises(StorageFileNotFoundError):
            read_json(target)

    def test_read_json_corrupt_raises_corrupterror(self, tmp_path):
        target = tmp_path / "bad.json"
        target.write_text('{invalid_json: true,', encoding="utf-8")
        with pytest.raises(StorageCorruptError):
            read_json(target)


class TestFileLock:
    def test_file_lock_mutual_exclusion(self, tmp_path):
        resource = tmp_path / "shared.json"
        results = []

        def worker(worker_id):
            with acquire_lock(resource, timeout=2.0):
                results.append(f"start_{worker_id}")
                time.sleep(0.05)
                results.append(f"end_{worker_id}")

        t1 = threading.Thread(target=worker, args=(1,))
        t2 = threading.Thread(target=worker, args=(2,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert len(results) == 4
        # Verification of no interleaving inside lock
        assert (results[:2] == ["start_1", "end_1"] and results[2:] == ["start_2", "end_2"]) or \
               (results[:2] == ["start_2", "end_2"] and results[2:] == ["start_1", "end_1"])

    def test_file_lock_timeout(self, tmp_path):
        resource = tmp_path / "locked.json"
        lock1 = acquire_lock(resource, timeout=1.0)
        lock1.acquire()

        try:
            lock2 = acquire_lock(resource, timeout=0.1)
            with pytest.raises(StorageLockTimeoutError):
                lock2.acquire()
        finally:
            lock1.release()


class TestCheckpointTransitions:
    def test_valid_transitions_lifecycle(self, tmp_path):
        chap_file = tmp_path / "chapter.json"
        meta = ChapterMetadata(
            chapter_id="c1",
            chapter_number="1",
            project_slug="slug1",
            title="Title"
        )
        atomic_write_json(chap_file, meta)

        # 1. PENDING -> RUNNING
        updated = update_checkpoint(chap_file, StageType.CRAWL, CheckpointStatus.RUNNING)
        assert updated.stages[StageType.CRAWL].status == CheckpointStatus.RUNNING
        assert updated.stages[StageType.CRAWL].started_at is not None

        # 2. RUNNING -> COMPLETED
        updated = update_checkpoint(
            chap_file,
            StageType.CRAWL,
            CheckpointStatus.COMPLETED,
            artifacts=["raw/001.jpg"]
        )
        assert updated.stages[StageType.CRAWL].status == CheckpointStatus.COMPLETED
        assert "raw/001.jpg" in updated.stages[StageType.CRAWL].artifacts

    def test_invalid_transition_raises(self, tmp_path):
        chap_file = tmp_path / "chapter.json"
        meta = ChapterMetadata(
            chapter_id="c1",
            chapter_number="1",
            project_slug="slug1",
            title="Title"
        )
        atomic_write_json(chap_file, meta)

        # Attempt illegal PENDING -> FAILED directly
        with pytest.raises(InvalidCheckpointTransitionError):
            update_checkpoint(chap_file, StageType.CRAWL, CheckpointStatus.FAILED)

    def test_transition_pending_to_completed(self, tmp_path):
        """Test direct PENDING -> COMPLETED transition."""
        chap_file = tmp_path / "chapter.json"
        meta = ChapterMetadata(
            chapter_id="c1",
            chapter_number="1",
            project_slug="slug1",
            title="Title"
        )
        atomic_write_json(chap_file, meta)

        updated = update_checkpoint(
            chap_file,
            StageType.CRAWL,
            CheckpointStatus.COMPLETED,
            artifacts=["raw/001.jpg"]
        )
        assert updated.stages[StageType.CRAWL].status == CheckpointStatus.COMPLETED
        assert updated.stages[StageType.CRAWL].completed_at is not None
        assert "raw/001.jpg" in updated.stages[StageType.CRAWL].artifacts

    def test_transition_running_to_running_and_pending(self, tmp_path):
        """Test RUNNING -> RUNNING and RUNNING -> PENDING transitions."""
        chap_file = tmp_path / "chapter.json"
        meta = ChapterMetadata(
            chapter_id="c1",
            chapter_number="1",
            project_slug="slug1",
            title="Title"
        )
        atomic_write_json(chap_file, meta)

        # 1. PENDING -> RUNNING
        update_checkpoint(chap_file, StageType.CRAWL, CheckpointStatus.RUNNING)

        # 2. RUNNING -> RUNNING (re-entrant update / progress checkpoint)
        updated = update_checkpoint(chap_file, StageType.CRAWL, CheckpointStatus.RUNNING)
        assert updated.stages[StageType.CRAWL].status == CheckpointStatus.RUNNING

        # 3. RUNNING -> PENDING (reset / retry)
        updated_pending = update_checkpoint(chap_file, StageType.CRAWL, CheckpointStatus.PENDING)
        assert updated_pending.stages[StageType.CRAWL].status == CheckpointStatus.PENDING

    def test_resume_interrupted_running_stage(self, tmp_path):
        """Test resuming an interrupted RUNNING stage with get_resumable_stage()."""
        chap_file = tmp_path / "chapter.json"
        meta = ChapterMetadata(
            chapter_id="c1",
            chapter_number="1",
            project_slug="slug1",
            title="Title"
        )
        atomic_write_json(chap_file, meta)

        # Complete CRAWL
        update_checkpoint(chap_file, StageType.CRAWL, CheckpointStatus.COMPLETED)
        # Start SEGMENT which was interrupted while RUNNING
        update_checkpoint(chap_file, StageType.SEGMENT, CheckpointStatus.RUNNING)

        # get_resumable_stage must return SEGMENT
        resumable = get_resumable_stage(chap_file)
        assert resumable == StageType.SEGMENT

    def test_partial_stages_dict_handling_no_key_error(self, tmp_path):
        """Test updating to COMPLETED with a partial stages dictionary does not raise KeyError."""
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

        # Updating crawl to COMPLETED must safely evaluate STAGE_EXECUTION_ORDER without KeyError
        updated = update_checkpoint(chap_file, StageType.CRAWL, CheckpointStatus.COMPLETED)
        assert updated.stages[StageType.CRAWL].status == CheckpointStatus.COMPLETED
        assert updated.overall_status != CheckpointStatus.COMPLETED


class TestFingerprinting:
    def test_calculate_sha256_string_and_bytes(self):
        s_hash = calculate_sha256("test_string")
        b_hash = calculate_sha256(b"test_string")
        assert s_hash == b_hash
        assert len(s_hash) == 64

    def test_calculate_sha256_file(self, tmp_path):
        f = tmp_path / "sample.txt"
        f.write_text("sample content", encoding="utf-8")
        h = calculate_sha256(f)
        assert verify_checksum(f, h) is True
        assert verify_checksum(f, "bad_hash") is False
