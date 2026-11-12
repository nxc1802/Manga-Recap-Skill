"""
tests/unit/test_p8_tts_optimization.py
Unit and L2 tests for P8.3 TTS optimization:
- Runtime probing (`probe_vieneu_runtime`) and true vs simulated batch detection
- Voice/speed/length bucketing and deterministic `seq_id` result ordering
- Dynamic OOM halving backoff without silence/placeholder fallback
- Decoupled CPU post-processing and FFmpeg fast-path skip at 48 kHz speed=1.0
- Real FFmpeg L2 normalization for non-1.0 speeds and non-48 kHz native WAVs
- `VieNeuCUDAWorker` load/VRAM/unload lifecycle
- Real `multiprocessing.get_context("spawn")` `tts_worker_loop` isolation
- `NeuralTTSBackend.synthesize_batch_native` and `VieNeuEngine.synthesize_batch_native`
"""

from __future__ import annotations

import gc
import math
import multiprocessing as mp
import os
import subprocess
import sys
import threading
import time
import types
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pytest

from src.render.artifact_validation import ArtifactValidationError, validate_audio
from src.tts.cuda_worker import (
    VieNeuCUDAWorker,
    get_length_bucket,
    group_and_bucket_tasks,
    probe_vieneu_runtime,
    tts_worker_loop,
)
from src.tts.engine import EngineMode, NeuralTTSBackend, VieNeuEngine


def _sine_wave(duration_sec: float = 0.25, sample_rate: int = 48000, freq: float = 440.0) -> np.ndarray:
    n = int(round(duration_sec * sample_rate))
    t = np.arange(n, dtype=np.float32) / float(sample_rate)
    return (0.25 * np.sin(2.0 * math.pi * freq * t)).astype(np.float32)


class _FakeBatchSDK:
    """Fake SDK supporting genuine native `infer_batch`."""

    sample_rate = 48000
    backend = "pytorch"

    def __init__(self, fail_batch_gt: Optional[int] = None, delay: float = 0.0):
        self.fail_batch_gt = fail_batch_gt
        self.delay = delay
        self.batch_calls: List[Dict[str, Any]] = []
        self.single_calls: List[Dict[str, Any]] = []
        self.closed = False

    def infer_batch(self, texts: List[str], voice: Optional[str] = None, batch_size: Optional[int] = None) -> List[np.ndarray]:
        self.batch_calls.append({"texts": list(texts), "voice": voice, "batch_size": batch_size})
        if self.delay > 0:
            time.sleep(self.delay)
        if self.fail_batch_gt is not None and len(texts) > self.fail_batch_gt:
            raise RuntimeError(f"CUDA out of memory while allocating batch size {len(texts)}")
        return [_sine_wave(0.2 + 0.02 * i) for i, _ in enumerate(texts)]

    def infer(self, text: str, voice: Optional[str] = None) -> np.ndarray:
        self.single_calls.append({"text": text, "voice": voice})
        return _sine_wave(0.25)

    def close(self) -> None:
        self.closed = True


class _FakeSingleOnlySDK:
    """Fake SDK lacking `infer_batch` (must never be reported as neural batching)."""

    sample_rate = 48000

    def __init__(self):
        self.single_calls: List[Dict[str, Any]] = []

    def infer(self, text: str, voice: Optional[str] = None) -> np.ndarray:
        self.single_calls.append({"text": text, "voice": voice})
        return _sine_wave(0.22)


class _FakeOnnxSequentialSDK:
    """Mirrors `V3TurboVieNeuTTS` on CPU/ONNX: method exists, backend='onnx' runs sequentially."""

    sample_rate = 48000
    backend = "onnx"

    class _FakeOnnxEngine:
        device = type("_Dev", (), {"type": "cpu"})()
        dtype = None

    engine = _FakeOnnxEngine()

    def __init__(self):
        self.batch_calls = 0
        self.single_calls = 0

    def infer_batch(self, texts: List[str], voice: Optional[str] = None, batch_size: Optional[int] = None) -> List[np.ndarray]:
        self.batch_calls += 1
        return [_sine_wave(0.2) for _ in texts]

    def infer(self, text: str, voice: Optional[str] = None) -> np.ndarray:
        self.single_calls += 1
        return _sine_wave(0.2)


def _install_fake_torch(monkeypatch, *, cuda_available: bool = True, bf16: bool = True):
    """Installs a lightweight fake `torch` module into `sys.modules` for unit tests."""

    class _FakeOOMError(RuntimeError):
        pass

    class _FakeCuda:
        OutOfMemoryError = _FakeOOMError

        def __init__(self):
            self.empty_cache_calls = 0
            self.ipc_collect_calls = 0
            self.set_device_calls: List[int] = []

        def is_available(self) -> bool:
            return cuda_available

        def device_count(self) -> int:
            return 1 if cuda_available else 0

        def current_device(self) -> int:
            return 0

        def set_device(self, idx: int) -> None:
            self.set_device_calls.append(idx)

        def is_bf16_supported(self) -> bool:
            return bf16

        def get_device_name(self, idx: int = 0) -> str:
            return "Tesla T4"

        def mem_get_info(self, idx: int = 0):
            return (13912 * 1024 * 1024, 14911 * 1024 * 1024)

        def memory_allocated(self, idx: int = 0) -> int:
            return int(401.1 * 1024 * 1024)

        def max_memory_allocated(self, idx: int = 0) -> int:
            return int(475.1 * 1024 * 1024)

        def memory_reserved(self, idx: int = 0) -> int:
            return int(704.0 * 1024 * 1024)

        def empty_cache(self) -> None:
            self.empty_cache_calls += 1

        def ipc_collect(self) -> None:
            self.ipc_collect_calls += 1

    fake_torch = types.ModuleType("torch")
    fake_torch.__version__ = "2.11.0+cu130"
    fake_torch.cuda = _FakeCuda()
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    return fake_torch


class _SpawnTestWorker:
    """Picklable minimal worker used inside real `spawn` child process test."""

    def __init__(self, gpu_id: int = 0, mode: str = "v3turbo", precision: str = "auto", preload: bool = False):
        # Verify CUDA_VISIBLE_DEVICES was set BEFORE torch or vieneu were imported.
        self.env_at_init = os.environ.get("CUDA_VISIBLE_DEVICES")
        self.torch_already_imported = "torch" in sys.modules
        self.vieneu_already_imported = "vieneu" in sys.modules
        self.gpu_id = gpu_id
        self.mode = mode
        self.precision = precision
        self.utterance_count = 0
        self.oom_events_count = 0
        self.unloaded = False
        self._sdk = _FakeBatchSDK()

    def load(self, precision: Optional[str] = None) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "provider": "pytorch_cuda",
            "supports_infer_batch": True,
            "neural_batching": True,
            "env_at_init": self.env_at_init,
            "torch_already_imported_at_init": self.torch_already_imported,
            "vieneu_already_imported_at_init": self.vieneu_already_imported,
        }

    def probe_runtime(self) -> Dict[str, Any]:
        return self.load()

    def unload(self) -> None:
        self.unloaded = True

    def vram_usage_mb(self) -> Dict[str, float]:
        return {"allocated_mb": 401.1, "peak_allocated_mb": 475.1, "reserved_mb": 704.0, "free_mb": 13912.0, "total_mb": 14911.0}

    def synthesize_batch_tasks(
        self,
        tasks: List[Dict[str, Any]],
        *,
        batch_size: int = 8,
        precision: str = "auto",
        cpu_workers: int = 4,
    ) -> List[Dict[str, Any]]:
        inner = VieNeuCUDAWorker(gpu_id=self.gpu_id, mode=self.mode, precision=precision, preload=False)
        inner._sdk = self._sdk
        res = inner.synthesize_batch_tasks(
            tasks, batch_size=batch_size, precision=precision, cpu_workers=cpu_workers
        )
        self.utterance_count += len(res)
        return res


def _spawn_test_worker_factory(**kwargs: Any) -> _SpawnTestWorker:
    return _SpawnTestWorker(**kwargs)


def test_cuda_worker_module_has_zero_eager_heavy_imports():
    """Importing `src.tts.cuda_worker` in a fresh Python process must not load torch/vieneu/ort/numpy."""
    repo_root = Path(__file__).resolve().parents[2]
    code = (
        "import sys, src.tts.cuda_worker; "
        "leaked = [m for m in ('torch', 'vieneu', 'onnxruntime', 'numpy') if m in sys.modules]; "
        "assert not leaked, f'Unexpected top-level imports: {leaked}'"
    )
    proc = subprocess.run([sys.executable, "-c", code], cwd=str(repo_root), capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_probe_vieneu_runtime_graceful_when_optional_deps_missing(monkeypatch):
    """Simulates lightweight CI environment lacking torch, onnxruntime, and vieneu."""
    for mod_name in ("torch", "onnxruntime", "vieneu"):
        monkeypatch.setitem(sys.modules, mod_name, None)
    info = probe_vieneu_runtime(mode="v3turbo")
    assert info["torch_installed"] is False
    assert info["ort_installed"] is False
    assert info["vieneu_installed"] is False
    assert info["supports_infer_batch"] is False
    assert info["neural_batching"] is False
    assert info["provider"] == "unavailable"
    assert info["vram_free_mb"] == 0.0
    assert info["vram_allocated_mb"] == 0.0


def test_probe_vieneu_runtime_distinguishes_true_batch_vs_single_and_onnx(monkeypatch):
    fake_torch = _install_fake_torch(monkeypatch, cuda_available=True, bf16=True)

    class _FakeModel:
        def parameters(self):
            yield types.SimpleNamespace(device="cuda:0", dtype="torch.bfloat16")

    sdk_cuda = _FakeBatchSDK()
    sdk_cuda.engine = types.SimpleNamespace(device="cuda", dtype="torch.bfloat16", model=_FakeModel())

    info_cuda = probe_vieneu_runtime(mode="v3turbo", sdk=sdk_cuda)
    assert info_cuda["provider"] == "pytorch_cuda"
    assert info_cuda["supports_infer_batch"] is True
    assert info_cuda["neural_batching"] is True
    assert info_cuda["batching_mode"] == "native_infer_batch"
    assert info_cuda["dtype"] == "torch.bfloat16"
    assert info_cuda["vram_allocated_mb"] == pytest.approx(401.1, abs=0.2)
    assert info_cuda["vram_free_mb"] == pytest.approx(13912.0, abs=1.0)
    assert fake_torch.cuda.is_available()

    # SDK missing `infer_batch`: MUST NOT claim neural batching!
    sdk_single = _FakeSingleOnlySDK()
    info_single = probe_vieneu_runtime(mode="v3turbo", sdk=sdk_single)
    assert info_single["supports_infer_batch"] is False
    assert info_single["neural_batching"] is False
    assert "neural_batch" not in info_single["batching_mode"]

    # ONNX CPU SDK (method exists on wrapper, backend=='onnx' runs sequentially):
    sdk_onnx = _FakeOnnxSequentialSDK()
    info_onnx = probe_vieneu_runtime(mode="v3turbo", sdk=sdk_onnx)
    assert info_onnx["provider"] == "onnx_cpu"
    assert info_onnx["supports_infer_batch"] is False
    assert info_onnx["neural_batching"] is False
    assert info_onnx["batching_mode"] == "onnx_cpu_sequential"


def test_length_bucketing_and_voice_speed_grouping_and_deterministic_order(tmp_path):
    assert get_length_bucket("a" * 10) == 0
    assert get_length_bucket("a" * 50) == 0
    assert get_length_bucket("a" * 51) == 1
    assert get_length_bucket("a" * 120) == 1
    assert get_length_bucket("a" * 121) == 2
    assert get_length_bucket("a" * 250) == 2
    assert get_length_bucket("a" * 251) == 3

    worker = VieNeuCUDAWorker(gpu_id=0, preload=False)
    fake_sdk = _FakeBatchSDK()
    worker._sdk = fake_sdk

    # Interleaved voices, speeds, and lengths
    tasks = [
        {"seq_id": "001", "text": "x" * 260, "voice": "Ngọc Huyền", "speed": 1.0, "output_path": tmp_path / "001.wav"},
        {"seq_id": "002", "text": "y" * 20, "voice": "Hải Đăng", "speed": 1.0, "output_path": tmp_path / "002.wav"},
        {"seq_id": "003", "text": "x" * 18, "voice": "Ngọc Huyền", "speed": 1.0, "output_path": tmp_path / "003.wav"},
        {"seq_id": "004", "text": "x" * 95, "voice": "Ngọc Huyền", "speed": 1.2, "emotion_cue": "[thở dài]", "output_path": tmp_path / "004.wav"},
        {"seq_id": "005", "text": "x" * 40, "voice": "Ngọc Huyền", "speed": 1.0, "output_path": tmp_path / "005.wav"},
        {"seq_id": "006", "text": "x" * 12, "voice": "Ngọc Huyền", "speed": 1.0, "output_path": tmp_path / "006.wav"},
    ]

    results = worker.synthesize_batch_tasks(tasks, batch_size=8, cpu_workers=2)
    assert [r["seq_id"] for r in results] == ["001", "002", "003", "004", "005", "006"]
    assert all(r["valid"] is True and r["batch_used"] is True for r in results)

    # Verify bucket 0 for ("Ngọc Huyền", 1.0) batched [12, 18, 40] sorted by length,
    # separate from bucket 3 (260), ("Hải Đăng", 1.0), and ("Ngọc Huyền", 1.2).
    call_shapes = [(c["voice"], [len(t) for t in c["texts"]]) for c in fake_sdk.batch_calls]
    assert ("Ngọc Huyền", [12, 18, 40]) in call_shapes
    assert ("Ngọc Huyền", [260]) in call_shapes
    assert ("Hải Đăng", [20]) in call_shapes
    assert any(c["voice"] == "Ngọc Huyền" and c["texts"] == ["[thở dài] " + "x" * 95] for c in fake_sdk.batch_calls)


def test_synthesize_batch_tasks_validates_speed_and_output_path(tmp_path):
    worker = VieNeuCUDAWorker(preload=False)
    sdk = _FakeBatchSDK()
    worker._sdk = sdk

    bad_tasks = [
        {"seq_id": "1", "text": "Xin chào", "voice": "Ngọc Huyền", "speed": 0.4, "output_path": tmp_path / "a.wav"},
        {"seq_id": "1", "text": "Xin chào", "voice": "Ngọc Huyền", "speed": 2.1, "output_path": tmp_path / "b.wav"},
        {"seq_id": "1", "text": "Xin chào", "voice": "Ngọc Huyền", "speed": float("nan"), "output_path": tmp_path / "c.wav"},
        {"seq_id": "1", "text": "Xin chào", "voice": "Ngọc Huyền", "speed": None, "output_path": tmp_path / "d.wav"},
        {"seq_id": "1", "text": "Xin chào", "voice": "Ngọc Huyền", "speed": 1.0, "output_path": ""},
        {"seq_id": "1", "text": "Xin chào", "voice": "Ngọc Huyền", "speed": 1.0, "output_path": tmp_path},
    ]
    for bad in bad_tasks:
        with pytest.raises(ValueError):
            worker.synthesize_batch_tasks([bad])

    assert sdk.batch_calls == []
    assert sdk.single_calls == []


def test_single_only_and_onnx_sdks_never_claim_batch_used(tmp_path):
    for sdk_obj in (_FakeSingleOnlySDK(), _FakeOnnxSequentialSDK()):
        worker = VieNeuCUDAWorker(preload=False)
        worker._sdk = sdk_obj
        tasks = [
            {"seq_id": "001", "text": "Câu một.", "voice": "Ngọc Huyền", "speed": 1.0, "output_path": tmp_path / f"{id(sdk_obj)}_1.wav"},
            {"seq_id": "002", "text": "Câu hai.", "voice": "Ngọc Huyền", "speed": 1.0, "output_path": tmp_path / f"{id(sdk_obj)}_2.wav"},
        ]
        results = worker.synthesize_batch_tasks(tasks, batch_size=4)
        assert len(results) == 2
        assert all(r["batch_used"] is False for r in results)
        assert all(r["batching_mode"] != "native_infer_batch" for r in results)
        assert all(r["valid"] is True for r in results)


def test_oom_batch_halving_retry_and_no_placeholder_on_bs1_oom(tmp_path, monkeypatch):
    fake_torch = _install_fake_torch(monkeypatch, cuda_available=True)
    gc_calls = []
    orig_collect = gc.collect
    monkeypatch.setattr(gc, "collect", lambda *a, **kw: (gc_calls.append(1), orig_collect(*a, **kw))[1])

    # Fails on batch > 2, succeeds on <= 2
    sdk = _FakeBatchSDK(fail_batch_gt=2)
    worker = VieNeuCUDAWorker(preload=False)
    worker._sdk = sdk

    tasks = [
        {"seq_id": f"{i:03d}", "text": f"Câu ngắn {i}", "voice": "Ngọc Huyền", "speed": 1.0, "output_path": tmp_path / f"oom_{i}.wav"}
        for i in range(6)
    ]
    results = worker.synthesize_batch_tasks(tasks, batch_size=6, cpu_workers=2)

    assert [r["seq_id"] for r in results] == [f"{i:03d}" for i in range(6)]
    assert all(r["valid"] and r["rms"] > 0.01 for r in results)
    assert worker.oom_events_count >= 1
    assert fake_torch.cuda.empty_cache_calls >= 1
    assert len(gc_calls) >= 1
    # Verify halving progression: attempted 6 -> halved to 3 -> halved to 2/1
    attempted_sizes = [len(c["texts"]) for c in sdk.batch_calls]
    assert attempted_sizes[0] == 6
    assert 3 in attempted_sizes
    assert any(sz <= 2 for sz in attempted_sizes)

    # Unrecoverable OOM at batch_size == 1 MUST fail loudly and NEVER overwrite existing file with silence
    fatal_sdk = _FakeBatchSDK(fail_batch_gt=0)
    worker2 = VieNeuCUDAWorker(preload=False)
    worker2._sdk = fatal_sdk
    preserved = tmp_path / "preserved.wav"
    preserved.write_bytes(b"original-non-placeholder")

    with pytest.raises(RuntimeError, match="CUDA out of memory"):
        worker2.synthesize_batch_tasks(
            [{"seq_id": "999", "text": "Thất bại OOM", "voice": "Ngọc Huyền", "speed": 1.0, "output_path": preserved}],
            batch_size=1,
        )
    assert preserved.read_bytes() == b"original-non-placeholder"


def test_ffmpeg_fastpath_skip_at_48khz_speed_1_and_l2_ffmpeg_atempo_resample(tmp_path, monkeypatch):
    worker = VieNeuCUDAWorker(preload=False)
    worker._sdk = _FakeBatchSDK()

    ffmpeg_calls: List[List[str]] = []
    real_run = subprocess.run

    def _spy_run(cmd, *args, **kwargs):
        if cmd and "ffmpeg" in str(cmd[0]):
            ffmpeg_calls.append(list(cmd))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _spy_run)

    # 1. Native 48 kHz + speed=1.0 -> ZERO FFmpeg subprocess calls!
    out_fast = tmp_path / "fast_48k.wav"
    res_fast = worker.synthesize_batch_tasks(
        [{"seq_id": "001", "text": "Đường dẫn nhanh không qua FFmpeg.", "voice": "Ngọc Huyền", "speed": 1.0, "output_path": out_fast}],
        cpu_workers=2,
    )
    assert ffmpeg_calls == []
    assert res_fast[0]["sample_rate"] == 48000
    assert res_fast[0]["valid"] is True
    assert validate_audio(out_fast)["sha256"] == res_fast[0]["sha256"]

    # 2. Speed != 1.0 -> Real FFmpeg atempo execution (L2 check)!
    out_fast_125 = tmp_path / "speed_125.wav"
    res_125 = worker.synthesize_batch_tasks(
        [{"seq_id": "002", "text": "Tốc độ nhanh hơn.", "voice": "Ngọc Huyền", "speed": 1.25, "output_path": out_fast_125}],
        cpu_workers=2,
    )
    assert len(ffmpeg_calls) == 1
    assert any("atempo=1.25" in arg for arg in ffmpeg_calls[0])
    rep_125 = validate_audio(out_fast_125)
    assert rep_125["sample_rate"] == 48000
    assert rep_125["duration"] < res_fast[0]["duration"]
    assert res_125[0]["duration"] == rep_125["duration"]

    # 3. Custom sdk.save outputting 24 kHz WAV at speed=1.0 -> Real FFmpeg resample to 48 kHz!
    class _Fixture24kSDK:
        def infer_batch(self, texts, voice=None, batch_size=None):
            return [{"token": i} for i in range(len(texts))]

        def save(self, obj, path):
            wav24 = _sine_wave(0.25, sample_rate=24000)
            with wave.open(str(path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(24000)
                wf.writeframes((wav24 * 32767.0).astype("<i2").tobytes())

    ffmpeg_calls.clear()
    worker_24k = VieNeuCUDAWorker(preload=False)
    worker_24k._sdk = _Fixture24kSDK()
    out_24k = tmp_path / "resampled_48k.wav"
    res_24k = worker_24k.synthesize_batch_tasks(
        [{"seq_id": "003", "text": "Chuyển đổi tần số lấy mẫu.", "voice": "Ngọc Huyền", "speed": 1.0, "output_path": out_24k}],
        cpu_workers=2,
    )
    assert len(ffmpeg_calls) == 1
    assert validate_audio(out_24k)["sample_rate"] == 48000
    assert res_24k[0]["sample_rate"] == 48000
    assert not list(tmp_path.glob("*.native.*"))
    assert not list(tmp_path.glob(".*.tmp.*"))


def test_cpu_postprocess_runs_in_bounded_threadpool(tmp_path, monkeypatch):
    worker = VieNeuCUDAWorker(preload=False)
    worker._sdk = _FakeBatchSDK()

    seen_threads: List[str] = []
    main_thread = threading.current_thread().name
    orig_post = __import__("src.tts.cuda_worker", fromlist=["_postprocess_cpu_item"])._postprocess_cpu_item

    def _wrapped_post(**kwargs):
        seen_threads.append(threading.current_thread().name)
        time.sleep(0.02)
        return orig_post(**kwargs)

    monkeypatch.setattr("src.tts.cuda_worker._postprocess_cpu_item", _wrapped_post)

    tasks = [
        {"seq_id": f"{i:03d}", "text": f"Câu {i}", "voice": "Ngọc Huyền", "speed": 1.0, "output_path": tmp_path / f"pool_{i}.wav"}
        for i in range(6)
    ]
    results = worker.synthesize_batch_tasks(tasks, batch_size=6, cpu_workers=3)
    assert len(results) == 6
    assert any(t != main_thread for t in seen_threads)


def test_worker_load_vram_and_unload_lifecycle(tmp_path, monkeypatch):
    fake_torch = _install_fake_torch(monkeypatch, cuda_available=True, bf16=True)
    fake_sdk = _FakeBatchSDK()
    init_calls: List[Dict[str, Any]] = []

    fake_vieneu_mod = types.ModuleType("vieneu")

    def _fake_vieneu_factory(mode="v3turbo", **kwargs):
        init_calls.append({"mode": mode, **kwargs})
        return fake_sdk

    fake_vieneu_mod.Vieneu = _fake_vieneu_factory
    monkeypatch.setitem(sys.modules, "vieneu", fake_vieneu_mod)

    gc_calls = []
    orig_collect = gc.collect
    monkeypatch.setattr(gc, "collect", lambda *a, **kw: (gc_calls.append(1), orig_collect(*a, **kw))[1])

    worker = VieNeuCUDAWorker(gpu_id=0, mode="v3turbo", precision="bf16", preload=False)
    assert worker._sdk is None

    info = worker.load()
    assert init_calls[0]["mode"] == "v3turbo"
    assert init_calls[0]["dtype"] == "bfloat16"
    assert info["provider"] == "pytorch_cuda"
    assert info["supports_infer_batch"] is True

    vram = worker.vram_usage_mb()
    assert vram["allocated_mb"] == pytest.approx(401.1, abs=0.2)
    assert vram["peak_allocated_mb"] == pytest.approx(475.1, abs=0.2)
    assert vram["reserved_mb"] == pytest.approx(704.0, abs=0.2)

    worker.unload()
    assert fake_sdk.closed is True
    assert worker._sdk is None
    assert len(gc_calls) >= 1
    assert fake_torch.cuda.empty_cache_calls >= 1


def test_spawn_tts_worker_loop_cuda_isolation_and_queue_protocol(tmp_path):
    ctx = mp.get_context("spawn")
    task_q = ctx.Queue()
    result_q = ctx.Queue()

    proc = ctx.Process(
        target=tts_worker_loop,
        args=(1, task_q, result_q),
        kwargs={
            "mode": "v3turbo",
            "precision": "auto",
            "batch_size": 4,
            "cpu_workers": 2,
            "preload": True,
            "heartbeat_interval_sec": 0.2,
            "worker_factory": _spawn_test_worker_factory,
        },
    )
    proc.start()

    events: List[Dict[str, Any]] = []
    try:
        # 1. Wait for ready event
        ready_msg = result_q.get(timeout=20)
        events.append(ready_msg)
        assert ready_msg["event"] == "ready"
        assert ready_msg["physical_gpu_id"] == "1"
        assert ready_msg["cuda_visible_devices"] == "1"
        assert ready_msg["runtime"]["env_at_init"] == "1"
        assert ready_msg["runtime"]["torch_already_imported_at_init"] is False
        assert ready_msg["runtime"]["vieneu_already_imported_at_init"] is False

        # 2. Submit a batch job
        out1 = tmp_path / "spawn_001.wav"
        out2 = tmp_path / "spawn_002.wav"
        task_q.put(
            {
                "job_id": "ch1",
                "tasks": [
                    {"seq_id": "001", "text": "Xin chào từ GPU 1.", "voice": "Ngọc Huyền", "speed": 1.0, "output_path": str(out1)},
                    {"seq_id": "002", "text": "Tiếp tục tổng hợp hàng loạt.", "voice": "Ngọc Huyền", "speed": 1.0, "output_path": str(out2)},
                ],
                "batch_size": 4,
            }
        )

        # Drain heartbeats until result arrives
        result_msg = None
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            msg = result_q.get(timeout=10)
            events.append(msg)
            if msg["event"] == "result":
                result_msg = msg
                break
            assert msg["event"] in {"heartbeat", "ready"}

        assert result_msg is not None
        assert result_msg["ok"] is True
        assert result_msg["job_id"] == "ch1"
        assert result_msg["cuda_visible_devices"] == "1"
        assert [r["seq_id"] for r in result_msg["results"]] == ["001", "002"]
        assert validate_audio(out1)["sample_rate"] == 48000
        assert validate_audio(out2)["sample_rate"] == 48000

        # 3. Shutdown cleanly
        task_q.put(None)
        stopped_msg = None
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            msg = result_q.get(timeout=5)
            events.append(msg)
            if msg["event"] == "stopped":
                stopped_msg = msg
                break
        assert stopped_msg is not None
        assert stopped_msg["jobs_completed"] == 1
        assert stopped_msg["utterance_count"] == 2
        proc.join(timeout=10)
        assert proc.exitcode == 0
    finally:
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=5)


def test_engine_synthesize_batch_native_neural_and_mock_compat(tmp_path):
    # 1. NeuralTTSBackend.synthesize_batch_native using object.__new__ (matches existing test pattern)
    backend = object.__new__(NeuralTTSBackend)
    backend.mode = "v3turbo"
    backend._sdk = _FakeBatchSDK()
    backend.total_inference_seconds = 0.0
    backend.utterance_count = 0

    neural_tasks = [
        {"seq_id": "001", "text": "Đoạn một.", "voice": "Ngọc Huyền", "speed": 1.0, "output_path": tmp_path / "n1.wav"},
        {"seq_id": "002", "text": "Đoạn hai.", "voice": "Ngọc Huyền", "speed": 1.0, "output_path": tmp_path / "n2.wav"},
    ]
    neural_res = backend.synthesize_batch_native(neural_tasks, batch_size=4, cpu_workers=2)
    assert len(neural_res) == 2
    assert all(r["valid"] and r["batch_used"] for r in neural_res)
    assert backend.utterance_count == 2

    # 2. VieNeuEngine(MOCK).synthesize_batch_native compatibility
    engine = VieNeuEngine(force_mode=EngineMode.MOCK)
    mock_tasks = [
        {"text": "Kiểm tra mock batch một.", "voice": "Trúc Ly", "speed": 1.0, "output_path": tmp_path / "m1.wav"},
        {"text": "Kiểm tra mock batch hai.", "voice": "Hải Đăng", "speed": 1.2, "output_path": tmp_path / "m2.wav"},
    ]
    mock_res = engine.synthesize_batch_native(mock_tasks, batch_size=4, cpu_workers=2)
    assert [r["seq_id"] for r in mock_res] == ["000", "001"]
    assert all(r["valid"] and r["sample_rate"] == 48000 for r in mock_res)

    # 3. Silent waveform from SDK must raise ArtifactValidationError and never leave output file
    class _SilentSDK:
        sample_rate = 48000
        def infer_batch(self, texts, voice=None, batch_size=None):
            return [np.zeros(4800, dtype=np.float32) for _ in texts]

    bad_worker = VieNeuCUDAWorker(preload=False)
    bad_worker._sdk = _SilentSDK()
    silent_dest = tmp_path / "silent.wav"
    with pytest.raises(ArtifactValidationError):
        bad_worker.synthesize_batch_tasks(
            [{"seq_id": "001", "text": "Im lặng.", "voice": "Ngọc Huyền", "speed": 1.0, "output_path": silent_dest}]
        )
    assert not silent_dest.exists()
