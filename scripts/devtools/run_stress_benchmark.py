"""
tests/adversarial/run_stress_benchmark.py
Empirical Stress & Concurrency Benchmark Runner for Milestone M1 (PR-00 & PR-01).
Generates detailed execution telemetry, latency profiling, and formal challenge evidence.
"""

import datetime
import json
import multiprocessing as mp
import os
from pathlib import Path
import statistics
import sys
import tempfile
import threading
import time
from unittest.mock import patch

# Ensure project root in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.core.schemas import (
    ChapterMetadata,
    CheckpointStatus,
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
    VALID_TRANSITIONS,
    acquire_lock,
    atomic_write_json,
    read_json,
    update_checkpoint,
)


def _mp_worker(target_path_str: str, worker_id: int, iterations: int, output_queue: mp.Queue):
    """Worker process performing locked read-increment-write cycles."""
    target = Path(target_path_str)
    latencies = []
    errors = []

    for i in range(iterations):
        t0 = time.perf_counter()
        try:
            with acquire_lock(target, timeout=15.0, retry_interval=0.01):
                data = read_json(target)
                data["counter"] += 1
                data["worker_updates"][str(worker_id)] = data["worker_updates"].get(str(worker_id), 0) + 1
                atomic_write_json(target, data)
            t1 = time.perf_counter()
            latencies.append(t1 - t0)
        except Exception as exc:
            errors.append(f"Worker {worker_id} iteration {i}: {type(exc).__name__}: {exc}")

    output_queue.put({
        "worker_id": worker_id,
        "latencies": latencies,
        "errors": errors
    })


def run_benchmark():
    print("=" * 80)
    print("M1 ADVERSARIAL STRESS & EMPIRICAL BENCHMARK SUITE")
    print(f"Timestamp: {datetime.datetime.now(datetime.timezone.utc).isoformat()}")
    print("=" * 80)

    # --------------------------------------------------------------------------
    # Benchmark 1: Multi-Process FileLock & Atomic Write Stress
    # --------------------------------------------------------------------------
    print("\n[BENCHMARK 1] Multi-Process FileLock & Atomic Write Concurrency Stress")
    num_processes = 8
    iterations_per_proc = 25
    total_expected = num_processes * iterations_per_proc

    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "mp_benchmark.json"
        atomic_write_json(target, {"counter": 0, "worker_updates": {}})

        ctx = mp.get_context("spawn")
        queue = ctx.Queue()
        processes = [
            ctx.Process(target=_mp_worker, args=(str(target), p_id, iterations_per_proc, queue))
            for p_id in range(num_processes)
        ]

        t_start = time.perf_counter()
        for p in processes:
            p.start()

        results = []
        for _ in range(num_processes):
            results.append(queue.get())

        for p in processes:
            p.join(timeout=30.0)
            assert not p.is_alive()
        t_total = time.perf_counter() - t_start

        # Analyze results
        all_latencies = [lat for r in results for lat in r["latencies"]]
        all_errors = [err for r in results for err in r["errors"]]

        final_data = read_json(target)
        final_counter = final_data["counter"]

        print(f"- Processes: {num_processes}")
        print(f"- Total Iterations: {total_expected}")
        print(f"- Total Wall Time: {t_total:.4f}s")
        print(f"- Throughput: {total_expected / t_total:.2f} locked atomic writes/sec")
        print(f"- Mean Latency: {statistics.mean(all_latencies)*1000:.2f}ms")
        print(f"- Median Latency: {statistics.median(all_latencies)*1000:.2f}ms")
        print(f"- 95th Percentile: {sorted(all_latencies)[int(len(all_latencies)*0.95)]*1000:.2f}ms")
        print(f"- Max Latency: {max(all_latencies)*1000:.2f}ms")
        print(f"- Errors: {len(all_errors)}")
        print(f"- Final Counter: {final_counter} (Expected: {total_expected})")
        assert final_counter == total_expected, f"Lost updates! {final_counter} != {total_expected}"
        assert len(all_errors) == 0, f"Errors encountered: {all_errors}"
        print("-> VERDICT: PASS (Zero lost updates, zero corruption, zero deadlocks)")

    # --------------------------------------------------------------------------
    # Benchmark 2: Concurrent Multi-Threaded Reader-Writer Hammering
    # --------------------------------------------------------------------------
    print("\n[BENCHMARK 2] Concurrent Reader-Writer Hammering (Zero Partial Reads)")
    num_writers = 8
    writes_per_thread = 50
    stop_event = threading.Event()
    read_counts = [0]
    read_errors = []

    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "hammer_test.json"
        atomic_write_json(target, {"initial": True, "seq": 0})

        def reader():
            while not stop_event.is_set():
                try:
                    data = read_json(target)
                    assert isinstance(data, dict)
                    read_counts[0] += 1
                except Exception as exc:
                    read_errors.append(f"{type(exc).__name__}: {exc}")
                time.sleep(0.0005)

        def writer(w_id: int):
            for i in range(writes_per_thread):
                payload = {
                    "writer": w_id,
                    "seq": i,
                    "payload": "x" * (100 * (i % 10 + 1)),
                    "nested": {"array": list(range(20))}
                }
                atomic_write_json(target, payload)
                time.sleep(0.001)

        t_r = threading.Thread(target=reader, daemon=True)
        t_r.start()

        t_start = time.perf_counter()
        writers = [threading.Thread(target=writer, args=(w,)) for w in range(num_writers)]
        for w in writers:
            w.start()
        for w in writers:
            w.join()
        stop_event.set()
        t_r.join(timeout=1.0)
        t_elapsed = time.perf_counter() - t_start

        print(f"- Writers: {num_writers} threads x {writes_per_thread} writes = {num_writers * writes_per_thread} writes")
        print(f"- Concurrent Reads Completed: {read_counts[0]}")
        print(f"- Read Errors (Partial/Corrupt JSON reads): {len(read_errors)}")
        print(f"- Elapsed Time: {t_elapsed:.4f}s")
        assert len(read_errors) == 0, f"Corrupted read detected: {read_errors}"
        print("-> VERDICT: PASS (Atomic rename guarantees readers never observe partial writes)")

    # --------------------------------------------------------------------------
    # Benchmark 3: Crash Consistency & Interrupted Write Simulation
    # --------------------------------------------------------------------------
    print("\n[BENCHMARK 3] Crash Consistency & Fault Injection")
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "crash_test.json"
        pristine = {"system_state": "healthy", "version": 42}
        atomic_write_json(target, pristine)

        # 3.1 Unserializable payload
        try:
            atomic_write_json(target, {"invalid": object()})
            assert False, "Should have raised TypeError"
        except TypeError:
            pass
        assert read_json(target) == pristine
        assert len(list(Path(td).glob(".*tmp*"))) == 0
        print("- [3.1] Unserializable object abort: Target intact, 0 leaked temp files.")

        # 3.2 Circular reference
        circ = {"a": 1}
        circ["loop"] = circ
        try:
            atomic_write_json(target, circ)
            assert False, "Should have raised ValueError"
        except ValueError:
            pass
        assert read_json(target) == pristine
        assert len(list(Path(td).glob(".*tmp*"))) == 0
        print("- [3.2] Circular reference abort: Target intact, 0 leaked temp files.")

        # 3.3 Disk full during open/write
        with patch("builtins.open", side_effect=IOError("Simulated disk full")):
            try:
                atomic_write_json(target, {"system_state": "overwritten"})
                assert False, "Should have raised StorageWriteError"
            except StorageWriteError:
                pass
        assert read_json(target) == pristine
        assert len(list(Path(td).glob(".*tmp*"))) == 0
        print("- [3.3] Simulated disk full: Target intact, temp file cleaned up.")

        # 3.4 Replace failure (e.g. cross-device or permission error)
        with patch("os.replace", side_effect=OSError("Simulated rename error")):
            try:
                atomic_write_json(target, {"system_state": "overwritten"})
                assert False, "Should have raised StorageWriteError"
            except StorageWriteError:
                pass
        assert read_json(target) == pristine
        assert len(list(Path(td).glob(".*tmp*"))) == 0
        print("- [3.4] Simulated rename failure: Target intact, temp file cleaned up.")

        # 3.5 Corrupt payload handling
        corrupt_file = Path(td) / "corrupt.json"
        corrupt_file.write_text("{'invalid_single_quotes': 1}", encoding="utf-8")
        try:
            read_json(corrupt_file)
            assert False, "Should have raised StorageCorruptError"
        except StorageCorruptError:
            pass
        print("- [3.5] Corrupt JSON detection: StorageCorruptError successfully raised.")
        print("-> VERDICT: PASS (All crash/abort paths preserve data integrity and clean up)")

    # --------------------------------------------------------------------------
    # Benchmark 4: Exhaustive Checkpoint State Machine Transition Matrix
    # --------------------------------------------------------------------------
    print("\n[BENCHMARK 4] Checkpoint State Machine Transition Matrix Validation")
    all_statuses = list(CheckpointStatus)
    total_transitions_tested = 0
    illegal_transitions_rejected = 0
    legal_transitions_accepted = 0

    with tempfile.TemporaryDirectory() as td:
        chap_file = Path(td) / "matrix_chapter.json"

        for start_status in all_statuses:
            for target_status in all_statuses:
                total_transitions_tested += 1
                # Setup chapter with start_status
                meta = ChapterMetadata(
                    chapter_id="chap_test",
                    chapter_number="1",
                    project_slug="matrix_test",
                    title="Matrix Test"
                )
                meta.stages[StageType.CRAWL].status = start_status
                atomic_write_json(chap_file, meta)

                is_allowed = target_status in VALID_TRANSITIONS.get(start_status, [])

                if is_allowed:
                    try:
                        updated = update_checkpoint(chap_file, StageType.CRAWL, target_status)
                        assert updated.stages[StageType.CRAWL].status == target_status
                        legal_transitions_accepted += 1
                    except Exception as e:
                        raise AssertionError(f"Expected legal transition {start_status.value} -> {target_status.value} to succeed, failed with {e}")
                else:
                    try:
                        update_checkpoint(chap_file, StageType.CRAWL, target_status)
                        raise AssertionError(f"Illegal transition {start_status.value} -> {target_status.value} should have raised InvalidCheckpointTransitionError!")
                    except InvalidCheckpointTransitionError:
                        illegal_transitions_rejected += 1

        print(f"- Total Transitions Tested: {total_transitions_tested} ({len(all_statuses)}x{len(all_statuses)})")
        print(f"- Legal Transitions Accepted: {legal_transitions_accepted}")
        print(f"- Illegal Transitions Blocked: {illegal_transitions_rejected}")
        assert legal_transitions_accepted + illegal_transitions_rejected == total_transitions_tested
        print("-> VERDICT: PASS (Exhaustive state machine matrix 100% compliant with VALID_TRANSITIONS)")

    print("\n" + "=" * 80)
    print("ALL 4 EMPIRICAL ADVERSARIAL BENCHMARKS PASSED SUCCESSFULLY")
    print("=" * 80)


if __name__ == "__main__":
    run_benchmark()
