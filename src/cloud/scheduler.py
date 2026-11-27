"""Execution policies, isolated CUDA/NVENC device roles, and cost-aware TTS micro-batch dispatching."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from src.tts.cuda_worker import MAX_LONG_TEXT_BATCH_SIZE, get_length_bucket


class ExecutionPolicy:
    """Canonical V7 Kaggle Dual-T4 GPU execution policy (Policy B only)."""

    DUAL_TTS_SHARED_ENCODE = "dual_tts_shared_encode"


CANONICAL_POLICY = ExecutionPolicy.DUAL_TTS_SHARED_ENCODE
SUPPORTED_POLICIES = (ExecutionPolicy.DUAL_TTS_SHARED_ENCODE,)

MAX_CLOUD_TTS_BATCH_SIZE = 32
DEFAULT_PRODUCE_BATCH_SIZE = 16
SUPPORTED_BATCH_CAPS = (4, 8, 12, 16, 24, 32)
DEFAULT_LONG_UTTERANCE_CAP = 4


def default_cloud_batch_size(job: str = "produce") -> int:
    """Returns canonical V7 VieNeu CUDA batch size: 32 for TTS-only, 16 for produce/render."""
    if str(job).strip().lower() == "tts":
        return MAX_CLOUD_TTS_BATCH_SIZE
    return DEFAULT_PRODUCE_BATCH_SIZE


def normalize_policy(policy: Optional[str]) -> str:
    """Validates that cloud policy is either omitted or canonical dual_tts_shared_encode (Policy B)."""
    if policy is None or not str(policy).strip():
        return CANONICAL_POLICY
    raw = str(policy).strip().lower()
    if raw == CANONICAL_POLICY:
        return CANONICAL_POLICY
    raise ValueError(
        f"Unsupported cloud execution policy '{policy}'. V7 runs canonical Policy B ('{CANONICAL_POLICY}') only."
    )


@dataclass(frozen=True)
class DeviceRolePlan:
    """Resolved execution policy and non-conflicting CUDA TTS / NVENC encode device pools."""

    policy: str
    job: str
    encoder: str
    tts_devices: Tuple[int, ...]
    encode_devices: Tuple[int, ...]
    selection_reason: str

    @property
    def all_devices(self) -> Tuple[int, ...]:
        ordered: List[int] = []
        for idx in (*self.tts_devices, *self.encode_devices):
            if idx not in ordered:
                ordered.append(idx)
        return tuple(ordered)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "policy": self.policy,
            "job": self.job,
            "encoder": self.encoder,
            "tts_devices": list(self.tts_devices),
            "encode_devices": list(self.encode_devices),
            "selection_reason": self.selection_reason,
        }


def resolve_execution_policy(
    policy: Optional[str] = None,
    *,
    job: str,
    encoder: str,
    cuda_devices: Sequence[int],
    usable_nvenc_devices: Optional[Sequence[int]] = None,
) -> DeviceRolePlan:
    """Resolves canonical V7 Policy B and rejects single-GPU, CPU-only, or unusable-NVENC hardware."""
    canonical = normalize_policy(policy)
    job_norm = str(job).strip().lower()
    if job_norm not in {"tts", "render", "produce"}:
        raise ValueError(f"Unknown cloud job type: {job}")

    cuda_list = tuple(dict.fromkeys(int(d) for d in cuda_devices))
    if len(cuda_list) < 2:
        raise RuntimeError(
            f"Kaggle V7 requires dual Tesla T4 CUDA devices (>=2), got {list(cuda_list)}. "
            "Single-GPU and CPU fallbacks were removed in V7."
        )

    if job_norm == "tts":
        return DeviceRolePlan(
            policy=canonical,
            job="tts",
            encoder=encoder,
            tts_devices=cuda_list[:2],
            encode_devices=(),
            selection_reason="Policy B: dual CUDA T4 TTS pool (no NVENC required for tts-only)",
        )

    if encoder != "h264_nvenc":
        raise RuntimeError(
            f"Kaggle V7 requires encoder='h264_nvenc' for '{job_norm}' (got '{encoder}'). "
            "CPU libx264 fallback was removed in V7."
        )
    nvenc_list = tuple(dict.fromkeys(int(d) for d in usable_nvenc_devices)) if usable_nvenc_devices is not None else cuda_list
    if len(nvenc_list) < 2:
        raise RuntimeError(
            f"Kaggle V7 requires usable h264_nvenc on both GPUs for '{job_norm}' "
            f"(usable_nvenc={list(nvenc_list)}, cuda={list(cuda_list)})."
        )

    if job_norm == "render":
        return DeviceRolePlan(
            policy=canonical,
            job="render",
            encoder="h264_nvenc",
            tts_devices=(),
            encode_devices=nvenc_list[:2],
            selection_reason="Policy B: shared dual-NVENC render pool",
        )

    return DeviceRolePlan(
        policy=canonical,
        job="produce",
        encoder="h264_nvenc",
        tts_devices=cuda_list[:2],
        encode_devices=nvenc_list[:2],
        selection_reason="Policy B: dual CUDA TTS with concurrent shared NVENC encode",
    )


def estimate_utterance_cost(text: str) -> float:
    """Normalized relative synthesis cost score derived from character count and punctuation pauses."""
    stripped = str(text or "").strip()
    if not stripped:
        return 0.0
    punct = sum(stripped.count(ch) for ch in (",", ".", ";", "!", "?", "…", ":"))
    return float(len(stripped)) + punct * 6.0


@dataclass
class UtteranceItem:
    """Pending TTS utterance keyed unambiguously by composite ``(chapter_number, seq_id)``."""

    chapter_index: int
    chapter_number: str
    segment_index: int
    seq_id: str
    text: str
    voice: str
    speed: float
    emotion: Optional[str]
    emotion_cue: Optional[str]
    output_path: Path
    fingerprint: str
    cost: float = 0.0
    length_bucket: int = 0

    def __post_init__(self) -> None:
        if self.cost <= 0.0:
            self.cost = estimate_utterance_cost(self.text)
        self.length_bucket = int(get_length_bucket(self.text))

    @property
    def key(self) -> Tuple[str, str]:
        return (self.chapter_number, self.seq_id)

    def to_worker_task(self) -> Dict[str, Any]:
        return {
            "chapter_number": self.chapter_number,
            "seq_id": self.seq_id,
            "text": self.text,
            "voice": self.voice,
            "speed": self.speed,
            "emotion": self.emotion,
            "emotion_cue": self.emotion_cue,
            "output_path": str(self.output_path),
            "fingerprint": self.fingerprint,
        }


def pack_tts_batches(
    items: Sequence[UtteranceItem],
    *,
    max_batch_size: int = 16,
    long_utterance_cap: int = DEFAULT_LONG_UTTERANCE_CAP,
) -> List[List[UtteranceItem]]:
    """
    Groups pending utterances into padded-efficient micro-batches:
    same ``(voice, round(speed, 6))`` and length bucket, prioritizing earliest
    blocking ``(chapter_index, segment_index)`` groups first.
    """
    eff_max = max(1, min(int(max_batch_size), MAX_CLOUD_TTS_BATCH_SIZE))
    long_cap = max(1, min(eff_max, int(long_utterance_cap), MAX_LONG_TEXT_BATCH_SIZE))
    grouped: Dict[Tuple[str, float, int], List[UtteranceItem]] = {}
    for item in sorted(items, key=lambda u: (u.chapter_index, u.segment_index, len(u.text))):
        gkey = (item.voice, round(float(item.speed), 6), item.length_bucket)
        grouped.setdefault(gkey, []).append(item)

    # Order groups by their earliest segment so blocked chapters unblock without starvation.
    ordered_groups = sorted(
        grouped.values(),
        key=lambda grp: min((u.chapter_index, u.segment_index) for u in grp),
    )

    batches: List[List[UtteranceItem]] = []
    for grp in ordered_groups:
        # Within a compatible bucket group, sort by text length to curb padding waste.
        bucket = grp[0].length_bucket
        cap = long_cap if bucket >= 3 else eff_max
        # Keep earliest-chapter head items close to front while tightening padding variance.
        sorted_grp = sorted(grp, key=lambda u: (u.chapter_index, len(u.text), u.segment_index))
        for i in range(0, len(sorted_grp), cap):
            batches.append(sorted_grp[i : i + cap])
    return batches


@dataclass
class UtteranceDispatcher:
    """
    Central cost-aware micro-batch dispatcher across ready chapters.

    Prevents cross-chapter ``seq_id`` collisions via ``(chapter_number, seq_id)`` keys
    and tracks adaptive per-device batch caps after CUDA OOM backoff events.
    """

    max_batch_size: int = 16
    long_utterance_cap: int = DEFAULT_LONG_UTTERANCE_CAP
    _pending: Dict[Tuple[str, str], UtteranceItem] = field(default_factory=dict)
    _in_flight: Dict[Tuple[str, str], int] = field(default_factory=dict)
    _completed: set = field(default_factory=set)
    _device_caps: Dict[int, int] = field(default_factory=dict)
    _next_batch_id: int = 1

    def enqueue(self, item: UtteranceItem) -> None:
        key = item.key
        if key in self._completed or key in self._in_flight or key in self._pending:
            raise ValueError(f"Duplicate utterance key registered: {key}")
        self._pending[key] = item

    def enqueue_many(self, items: Iterable[UtteranceItem]) -> None:
        for item in items:
            self.enqueue(item)

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def in_flight_count(self) -> int:
        return len(self._in_flight)

    @property
    def is_drained(self) -> bool:
        return not self._pending and not self._in_flight

    def record_oom_backoff(self, device_id: int, effective_batch_size: int) -> None:
        cap = max(1, int(effective_batch_size))
        prev = self._device_caps.get(int(device_id), self.max_batch_size)
        self._device_caps[int(device_id)] = min(prev, cap)

    def effective_cap_for_device(self, device_id: Optional[int]) -> int:
        if device_id is None:
            return self.max_batch_size
        return max(1, min(self.max_batch_size, self._device_caps.get(int(device_id), self.max_batch_size)))

    def next_batch(self, *, device_id: Optional[int] = None) -> Optional[Tuple[int, List[UtteranceItem]]]:
        if not self._pending:
            return None
        cap = self.effective_cap_for_device(device_id)
        batches = pack_tts_batches(
            list(self._pending.values()),
            max_batch_size=cap,
            long_utterance_cap=self.long_utterance_cap,
        )
        if not batches:
            return None
        chosen = batches[0]
        batch_id = self._next_batch_id
        self._next_batch_id += 1
        for item in chosen:
            key = item.key
            self._pending.pop(key)
            self._in_flight[key] = batch_id
        return batch_id, chosen

    def mark_completed(self, keys: Iterable[Tuple[str, str]]) -> None:
        for key in keys:
            self._in_flight.pop(key, None)
            self._completed.add(key)

    def fail_batch(self, keys: Iterable[Tuple[str, str]]) -> None:
        for key in keys:
            self._in_flight.pop(key, None)
