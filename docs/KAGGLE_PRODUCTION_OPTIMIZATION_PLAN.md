# Kaggle Production Optimization Plan — Manga Recap Skill

**Repository:** `nxc1802/Manga-Recap-Skill`  
**Baseline inspected:** `f242338af23cfe47c7bfa3b4924ebdbc5be2c455` (main at previous audit)  
**Relationship:** Supplements `REFACTOR_PLAN.md`, particularly Phase 5 (Kaggle) and Phase 8 (Media). Performance-critical implementation should be benchmarked against this pinned baseline before removal/refactor.  
**Scope:** Full-series production after verified Chapter 1 Preview approval. No repository changes or live Kaggle jobs have been performed in preparing this plan.

## 1. Objective and non-goals

Optimize approved series production for **end-to-end makespan** and **useful output per Kaggle session/quota hour**, not time to produce a single Preview or superficial GPU utilization. Keep the solution compact: one worker entry, one media contract, bounded queues, a small number of measurable execution policies, no external scheduler/database.

- Preview: favor reliable output and human review; do not optimize at the expense of review integrity. Reuse the exact approved Chapter 1 MP4/SRT in production; do not re-synthesize or re-encode it. Do not silently alter its quality/resolution. If the approved Preview is genuinely lower resolution than the desired final deliverable, surface the conflict separately rather than breaking approval.
- Full production: use one Dual-T4 session for a normal-sized ready batch; shard only when runtime, disk, quota, output-size or failure-recovery limits warrant it. Do not dispatch one job per chapter by default.
- Preserve narration, voice/speed, shot selection/order, frame schedule, subtitle timing/style, motion, watermark, BGM and all review gates. No mock TTS, missing shots, silent quality downgrade or fake success. Keep chapter exports and optional feature compilation.
- Do not change model family, quantization/precision defaults, render preset, resolution or other creative settings just to win a benchmark. Do not introduce DDP, NCCL, Ray, Celery, Redis or a generic DAG framework. Independent utterances do not need distributed training/model parallelism.
- Existing local/single-GPU/CPU behavior remains supported; actual runtime hardware and capability reports must be explicit. No account rotation to evade Kaggle limits; only authorized compliant use.

## 2. Current-state audit (verified source)

| Area | Evidence | Assessment |
|---|---|---|
| Accelerator | `src/cloud/kaggle_service.py:431–446`, `dual_gpu_worker.py:340–364` | Requests `NvidiaTeslaT4` (Kaggle documents T4 x2), probes visible CUDA devices and falls back from NVENC if smoke test fails. Actual allocation must be recorded and verified per GPU. |
| Full-series dispatch | `kaggle_service.py:556–564`, `cli/cloud_cmd.py:151–190` | One series job and enforced Preview gate: good baseline. |
| Dual GPU | `dual_gpu_worker.py:233–252,270–337` | GPU0 owns one persistent VieNeu worker and emits shots; GPU1 encodes concurrently. GPU0 joins encode pool only after all TTS/producer work ends. No second TTS worker. |
| TTS | `dual_gpu_worker.py:32–73,116–122`; `tts/cuda_worker.py:507–529,634–799,952–980` | Fingerprint/checksum cache, native length bucketing, OOM backoff, CPU postprocessing. Outer contiguous chunks default to 12 segments and call a new batch routine per chunk, so global batch packing and GPU1 TTS are not exploited. Default is not a measured universal optimum. |
| Render | `dual_gpu_worker.py:128–197,200–231`; `render/fast_renderer.py:576–902` | Streams shots where possible, preserves subtitle overlap at chunk boundary, caches canvas and uses per-shot FFmpeg/NVENC. Existing visual-plan path waits for chapter audio before queuing all shots. Render-only mode also has a producer phase before GPU0 joins encoding. |
| Finalization | `dual_gpu_worker.py:255–267,304–326` | Parent finalizes chapter synchronously (SRT, audio mix, concat/mux), blocking event handling during that step. No evidence yet that this dominates runtime. |
| Queue and worker safety | `dual_gpu_worker.py:284–337` | Multiprocessing spawn and basic error propagation; unbounded task/event queues and temporary shot directory. No explicit per-worker heartbeat in this pipeline; the separate TTS worker-loop heartbeat is not used here. |
| Transfer and bootstrap | `payload.py:36–117`; `kaggle_service.py:274–351,397–429` | New private dataset per job, package full runtime and allowed roots (raw/panels/audio/subtitles/contact sheets), copy/hash/compress/upload, install runtime. Safe isolation, potentially redundant data and repeated setup. |
| Recovery | `payload.py:120–163`; `dual_gpu_worker.py:362–364` | Results exported after entire pipeline returns; script/checkpoints in remote runtime alone do not guarantee durable progress after session loss. No demonstrated chapter-level cross-session resume. |
| Timeout and retry | `kaggle_service.py:352–361,495–513,566–590,628–740`; `account_fallback.py:8–35` | Default wait timeout 1800s and queue timeout 300s; full series inherits same default. Broad exception fallback can resubmit despite ambiguous/running prior jobs. Client timeout does not prove remote cancellation. |
| Chapter 1 | `dual_gpu_worker.py:89–94`; `chapter_renderer.py:72–98` | Already promotes exact approved Preview: retain, avoid transferring/copying more than integrity gate requires. |

The old refactor plan explicitly preserves current scheduling and calls for benchmark/regression checks, but does not specify adaptive allocation, timeout estimation, backpressure, partial persistence or a comparative execution-policy benchmark. This plan adds those requirements. No live utilization, runtime profile or end-to-end speedup was measured during this audit.

## 3. Why the current fixed split may be suboptimal

Current timeline (conceptual, not measured):

```text
GPU0 CUDA: [load model][TTS ch2][TTS ch3][TTS ch4][unload]------------
GPU0 ENC : --------------------------------------------[encode tail]
GPU1 CUDA: mostly not used for TTS ---------------------------------
GPU1 ENC : -----------[shots ch2][shots ch3][shots ch4][encode tail]
CPU      : prep/cues/FFmpeg filters/postprocessing/mix/concat/export
```

NVENC is separate hardware from CUDA cores (with shared memory, power, CPU/I/O and some CUDA-assisted encoder operations). Thus an encoder running on GPU1 does not imply that GPU1's CUDA compute is saturated. Independent TTS inference on GPU1 and NVENC encoding on either GPU may overlap, but VRAM pressure, CPU filters, subprocess overhead and thermal/power constraints can negate the gain. Measure before choosing.

Idealized lower bounds (ignoring setup, dependency bubbles and contention): with aggregate single-device TTS time `A` and one-device encoding time `B`, perfectly overlapped two TTS workers plus two encoders could approach `max(A/2,B/2)`. The current one-TTS policy has a TTS lower bound of `A`; it can overlap some encoding and drain the render tail on both devices. This is not a prediction of real speedup. If CPU filters or transfer dominate, adding GPU work does not necessarily help.

## 4. Target architecture — minimal changes

Keep the existing `dual_gpu_worker.py` entry point (or move it once according to the main refactor, with compatibility entry), reuse the canonical TTS backend and shot renderer. Separate only the responsibilities that require independent lifecycles:

```text
cloud/
  kaggle_service.py     # thin SDK/job transport after main refactor
  payload.py            # verified immutable inputs and result contracts
  worker.py             # bootstrap and one execution entry (rename only once)
  scheduler.py          # ready queues, hardware policy, supervision
  [checkpoint.py]       # optional only if existing storage helpers insufficient
media/                  # existing/reorganized TTS, visual plan, FFmpeg, audio mix
```

Logical data flow:

```text
Local: validate all ready scripts + approvals → freeze input revision → plan chunk(s)
  → upload verified private payload → submit and persist job ID → monitor
Kaggle: capability probe → promote approved Chapter 1 if requested
  → TTS ready batches ──→ verified audio/ordered durations
  → canonical shot plan ──→ bounded ready-shot queue → NVENC workers
  → chapter finalize/mux → validated chapter receipt → optional feature compile
  → verified result manifest → download → safe local sync
```

Use typed small task records (chapter ID + segment/shot ID + revision/fingerprint + path + estimated cost), not large image/audio blobs over IPC. Keep per-chapter state in one coordinator; workers write unique output files atomically and emit result events. Workers never concurrently rewrite the same `script.json`. Preserve canonical order at finalize, irrespective of out-of-order completion. Use spawn and device mapping before importing/initializing CUDA in workers. FFmpeg GPU mapping must be independently verified (physical vs logical device numbering). Do not assume the parent CUDA probe configures child isolation.

## 5. Phase K0 — Instrument and freeze baseline (P0)

- [ ] Pin the baseline and dependency/environment versions. Record actual GPU model/count/VRAM, CUDA, VieNeu model version/precision, FFmpeg build and smoke test for each NVENC device; model cache status, CPU cores/RAM/disk.
- [ ] Add lightweight structured timing/event records and periodic resource samples (~1–2 s initially; low overhead). Capture CPU utilization/load, process/thread counts, disk usage, per-GPU SM utilization, memory use, NVENC utilization where supported (NVML/nvidia-smi), active processes, OOM count, batch occupancy, audio seconds generated, queue depth/wait, shot frames, render FPS, finalization time.
- [ ] Measure pack/hash/compression/upload, dataset ready, queue wait, dependency/model setup, TTS, shot preparation, encode, audio mix/concat, export/compile, download/sync separately; report overlap, not simply the sum of parallel task durations.
- [ ] Use `job_id`, `chapter_id`, `segment_id`, `shot_id`, `device_id`, monotonic duration and timestamps. Keep reports compact; no raw tokens, private texts or credentials in telemetry.
- [ ] Use one short multi-chapter fixture and one representative long/mixed series (short and long narration; static/pan; subtitles on/off as distinct controlled workloads). Benchmark approved Chapter 1 reuse separately. Use existing project assets with authorized rights and exact same input revisions.

**Acceptance:** A timeline and stage totals identify bottlenecks; real hardware and actual worker device mapping are recorded. Baseline output passes media/regression tests. Do not change scheduling before collecting baseline, except correctness fixes independently measured.

## 6. Phase K1 — Reliability and long-run lifecycle (P0)

- [ ] Separate queue deadline, remote execution budget, client observation deadline, and transfer deadline. Replace fixed 1800s full-series budget with explicit bounded workload-derived limits (from calibrated upper estimates + margin + provider caps); use the actual permitted Kaggle session/quota limits, not hardcoded claims. 300s queue stall can still notify user; it does not imply cancel/resubmit.
- [ ] Persist immutable job intent, payload digest, unique kernel/dataset ID, revision, account identity, chunk list and submission state *before* attempting submit. Reconcile unknown submit result with remote status; distinguish `SUBMIT_UNKNOWN`, `QUEUED`, `RUNNING`, `REMOTE_COMPLETE`, `SYNCED`, `FAILED`, `CANCELLED` (or equivalent). Reattach/pull without new compute after local timeout/disconnect.
- [ ] Narrow fallback to known eligible failures; no automatic whole-series replay after deterministic worker failure, artifact mismatch, network ambiguity or timeout. Do not launch replacement while prior job may still run. Require confirmed cancellation/terminal state and compliance with provider terms.
- [ ] Add worker heartbeats, progress counters and bounded task queues. Watch for alive-but-stalled children, process failures, failed FFmpeg, disk low-water marks and shutdown; drain error/control events promptly. Terminate child subprocess trees safely. Bounded queues must not deadlock: for single-GPU sequential path use direct per-stage draining/spooling or an active consumer; never block the only producer awaiting its own future consumer.
- [ ] Validate job type and approval on host and worker. Maintain safe extraction, path whitelist, symlink rejection, input hash/revision checks, atomic sync and exact chapter order. Incomplete work cannot be reported as success.

**Acceptance:** Mock tests for unknown submit, queued timeout then starts, polling network failure, local restart, confirmed cancel, deterministic failure, queue saturation, encoder crash/hang, no GPU, quota exhaustion. No duplicate active jobs for same revision/chunk. No task lost or duplicated in scheduler; no indefinite wait on queue.

## 7. Phase K2 — Comparative Dual-T4 execution policies (P1, highest performance impact)

Implement behind a small execution policy switch for benchmarking; retain the baseline path as rollback until measured. Prefer fixed policy per job plus bounded queue backpressure over frequent model reloads or complex adaptive scheduling.

| Policy | GPU0 | GPU1 | Why test |
|---|---|---|---|
| A — baseline split | TTS, then render | Render | Current behavior and control group |
| B — dual TTS + shared encode | TTS instance + NVENC slots | TTS instance + NVENC slots | Potentially exploit both SM sets and both dedicated encode engines simultaneously |
| C — dual TTS then dual encode | TTS instance, unload, then encode | TTS instance, unload, then encode | Safer if concurrent TTS+NVENC contends for VRAM/CPU; may cost overlap/disk |
| D — one TTS + dual encode | TTS + NVENC slots | NVENC slots | Render-heavy workload, or second model fails memory/perf gate |

B is the main candidate for a TTS-heavy full series, not a guaranteed winner. A or D may be fastest for other workloads. Render-only should immediately allow both usable encoders without waiting for an artificial TTS producer barrier; TTS-only should benchmark two independent TTS workers instead of force-slicing devices to one. Single GPU: retain reliable sequential baseline; optional same-device overlap only when measured safe. NVENC failure: use an explicitly allowed CPU encoder policy, and do not automatically discard a healthy second CUDA device for TTS just because encoding is CPU-based. No GPU: report actual degraded execution and respect compute selection; stop if policy cannot be satisfied.

- [ ] One resident VieNeu instance per TTS device/process where memory and actual runtime allow; warm once, do not load per chapter. Reuse existing native `infer_batch`, length bucketing and CPU postprocessing. No DDP/model sharing across GPUs; independent batches are the unit of parallelism.
- [ ] Central pending-utterance queue by approved voice/speed and normalized text/token-length estimates. Use composite `(chapter, seq_id)` identity. Permit batching across chapters only if same approved synthesis settings and validated output mapping; use bounded lookahead and prioritize earliest ready chapter so feature dependencies are not starved. Compare against simpler chapter-level batches first; keep the smaller solution if gains are negligible.
- [ ] Assign next eligible batch to the next free TTS worker; estimate batch cost by normalized text length initially, refine only with measured data. Avoid rigid equal-chapter splits when chapters differ substantially. Preserve actual generated audio duration and checksum; aggregate results in coordinator; commit per chapter/micro-batch without concurrent metadata races.
- [ ] Compare batch caps 4/8/12/16 (and larger only if VRAM/profiling support them), separate long-utterance caps and effective OOM backoff. Treat these as experimental candidates, not universal defaults. Preserve model/voice/precision, check output quality; do not count cached audio as fresh inference throughput.
- [ ] Keep shot-level shared ready queue and per-GPU bounded encoder slots; start with 1 slot/device and test 2 then 4 only where measured. Avoid launching too many FFmpeg processes; account for filter/encoder/decoder threads and TTS CPU postprocess workers together under a global CPU budget. Preserve NVENC GPU binding, codec/preset/quality.
- [ ] Each ready shot must have actual audio durations, correct visual plan and subtitle timing. Preserve boundary cue logic; for existing authored plans whose validity depends on whole-chapter durations, wait for necessary data instead of overriding authored framing or rendering invalid partial plans. Stream only where the existing canonical policy supports it.
- [ ] Keep the model resident while useful TTS remains. Release models when exhausted; encoding drains using both GPUs. Only consider threshold-based switching if measurements show material gains over per-job policy; hysteresis/minimum residency and bounded memory required, no repeated reload thrashing.
- [ ] Use per-policy resource caps and explicit fallback on unrecoverable OOM or NVENC failure; reuse verified completed audio/shot outputs rather than silently rerunning all work. Distinguish task failure from policy/hardware capability issue.

**Acceptance:** Real Dual-T4 smoke validates one TTS process on each intended device, correct encoder device IDs, no hidden CPU fallback, no missing/duplicated/reordered output, bounded VRAM/disk/queues. Compare A–D on controlled fixture; adopt only winning policy(s) that preserve quality and stability. If B is slower or unstable, choose A/C/D as evidence dictates. Keep scheduler complexity proportional to measured benefit.

## 8. Phase K3 — CPU, render and finalization bottlenecks (P1/P2)

- [ ] Profile the real FFmpeg path: canvas prep, overlay/font, pan filters, frame transfer, subprocess launch, encoder and concat. A busy NVENC engine is not necessarily a busy CUDA engine; low SM utilization alone is not a failure criterion.
- [ ] Retain static/pan fast paths, frame schedule, canvas cache, per-shot subtitle alignment and stream-copy concat where compatible. Share immutable cached files safely between workers; bound cache and remove temporary shots after verified chapter finalization (not before final output is committed). Avoid repeated decoding/scaling or copies where proven redundant.
- [ ] If parent finalization stalls event draining or GPU work, introduce one bounded CPU finalizer thread/process for audio mix, SRT and concat/mux; coordinator keeps consuming worker events, waits for all finalizers before export. CPU budget must account for finalizer alongside filters/TTS postprocessing. Do not add a pool if measured parent overhead is negligible.
- [ ] Feature compile is optional and ordered, must not block remaining GPU tasks; run after required chapter receipts exist. Prefer existing safe stream-copy where parameters match; if normalized re-encode is needed, measure and report, preserve quality. Check whether compiling locally saves quota/transfer versus remote; do not automatically download duplicate huge feature + chapters without user/contract justification.

**Acceptance:** No GPU idle caused by blocked event processing when ready tasks exist; CPU/disk remain bounded. Existing golden visual/audio/subtitle checks pass including boundary cues, out-of-order completions and mixed chapter sizes. Quantify any optimization before making default.

## 9. Phase K4 — Payload, batching across sessions and durable recovery (P1)

Normal path: one job for all ready chapters if estimates safely fit. Large series: partition **contiguous chapter chunks** using measured seconds/utterances/frames, bytes and available runtime/disk/quota budget, not fixed arbitrary chapter count. Preserve final series order and Chapter 1 approval reference in every chunk. Chapter 1 is promoted once; if a chunk requires original Preview evidence for gate validation, include that evidence. Never remove evidence required by worker-side checks.

- [ ] Profile payload inventory and output sizes. Derive minimal allowlist from actual canonical dependency graph; do not indiscriminately include contact sheets, raw images, old audio/subtitles or all watermarks if not needed. Test extraction/worker preflight for every render path and review gate before excluding. Keep immutable digests and private dataset isolation.
- [ ] Reuse exact content-addressed private runtime/model/input artifacts only if provider supports reliable version pinning and authorized access. If using model/dataset cache, verify model/dependency version and license, no secrets. Do not make runtime non-reproducible. Avoid pip install/model download every chapter; one session bootstraps once. Measure cold vs warm starts separately.
- [ ] Estimate disk high-water mark (input archive/extract + runtime + model + WAVs + shots + output copies + feature) and reserve safety margin; use bounded lookahead and clean only committed intermediates. Avoid extra full copies if integrity and atomicity preserved. Clean local staging only after required diagnostic/state retention.
- [ ] Emit per-chapter receipt (input digest, output files/checksums, frame/duration/audio/subtitle checks, completion state) and atomically export each completed chapter to the output directory as soon as possible. This does NOT guarantee durable recovery if Kaggle session dies before outputs are committed or downloadable. Test actual provider behavior. Do not advertise resume across killed sessions until proven.
- [ ] Durable recovery default: sync verified outputs after each successfully committed chunk; next job contains only missing/stale chapter work plus necessary approval evidence. If already completed chunks exist locally, avoid re-synthesis/re-encode. For interrupted single session, only reuse remote artifacts demonstrably retrievable and verified. Optional private HF checkpoint transport only with explicit user authorization, provider policy, measured I/O benefit and existing archive safety; no mandatory extra service or credentials in payload.
- [ ] Keep final compile after all requested chunks sync; if a remote compile fails, do not invalidate verified chapter outputs or rerun TTS. Retry only incomplete release step. A series succeeds only when all requested chapter/feature contracts pass.

**Acceptance:** Simulate failure halfway through long series, restart local controller, reconcile job, resume only unfinished chunks. Corrupt/stale receipts fail closed; no chapter reordering. Verify actual Kaggle output persistence behavior using a limited authorized test before claiming cross-session checkpointing. No unbounded storage growth or unnecessary datasets/jobs.

## 10. Phase K5 — Benchmark and choose the default (P0 gate)

Run performance experiments only with explicit authorization and a stated quota/workload budget. Unit and local mocked scheduler tests first. For live benchmark use same code inputs, voice, model version, hardware, FPS/resolution/preset/subtitles, cold/warm cache state and chapter set. Repeat promising policies (ideally 3 paired runs where budget permits), report individual samples/median/spread; separate Kaggle queue variability from worker compute. Report failures and fallback as failures/degraded runs, not exclude inconvenient results. Avoid a large factorial grid: A vs B first; C/D when bottleneck warrants; then tune batch/encoder slots around best candidate.

| Metric | Definition / purpose |
|---|---|
| End-to-end makespan | Local dispatch start → verified requested output synced (and compile if requested) |
| Worker makespan | Worker start → verified export ready; isolate bootstrap/model load |
| Throughput | Final valid video minutes / production wall-clock hour; also chapters/hour |
| TTS throughput | Newly synthesized audio seconds / TTS wall time; batch occupancy/OOM |
| Encode throughput | Valid frames or video seconds / elapsed encode time; distinguish per-GPU and aggregate |
| Resource balance | SM/NVENC utilization, VRAM, CPU, disk, ready queue depth/wait, idle gaps |
| Cloud cost | Report actual Kaggle quota consumption if available; session-hours and estimated device-hours separately (not assume provider charges 2x) |
| Reliability | Success rate, retry count, lost/repeated work, resume time, correct terminal state |
| Quality parity | Valid media, approved Chapter 1 exact checksum, canonical plan/order/cues, audio/visual quality and configured resolution/codec |

Proposed targets (not measured promises): >=20% lower median **worker makespan** on a representative TTS-heavy approved series compared with A, with end-to-end performance also reported and no material regression for render-heavy workloads. Treat ~10–20% threshold as decision guidance; refine after baseline and quota budget. A simpler policy with smaller gains can be chosen if robustness wins. If no candidate beats baseline beyond noise, retain A and ship lifecycle/recovery/observability improvements; do not force slower scheduler or claim speedup. No fixed 90% GPU utilization target and no theoretical 2x promise.

Validation gates:

- [ ] Exact approval checks/fingerprints and Chapter 1 MP4/SRT reuse; current final resolution semantics documented. No unapproved render or creative change.
- [ ] Unit tests for multi-GPU scheduling, composite IDs, ordering, cache validity, bounded queues, fairness, device mapping, worker failures, OOM and CPU fallback. Mock SDK tests for timeout/reconcile/retry/pull and receipts.
- [ ] Real media tests with static/pan/mixed shots, subtitle boundary across micro-batches, multiple chapter lengths, authored plans, BGM/watermark and output validation via ffprobe/decode. For non-deterministic neural audio or encoder output do not demand byte equality of regenerated media; require content/settings/timing and quality parity, exact bytes for promoted approved files.
- [ ] Limited authorized real Kaggle Dual-T4 smoke (2–3 chapters including approved ch1), then matched larger workload if budget permits. Demonstrate device use and actual performance; no claims based only on mocks or historical single-chapter timings.
- [ ] Failure recovery simulation; real provider persistence behavior verified separately when authorized. No silent incomplete success, no duplicate running job, no lost verified chunks.

## 11. Phase K6 — Integration with Skill and rollout

- [ ] Keep user-facing workflow simple: Preview Chapter 1 → human approval → `produce` (Kaggle, existing `cloud produce-series` compatibility) → status/resume if interrupted → compile/export. Agent does not manually allocate GPUs or loop through chapters. No new Stage 0 question for internal scheduler details.
- [ ] Internal policy default `auto` only after benchmark; deterministic selected policy in job manifest. Advanced debug override for baseline/other measured policies; `status` reports actual hardware, selected policy, progress and blocker. Preview remains simple and correctness-oriented. Keep preflight fail-closed; no `--skip-preview` bypass.
- [ ] Reuse the general refactor's provider boundary and canonical media functions. Implement each scheduler change in small reviewable commits, separate from broad folder renames/compat migration. Remove abandoned experimental policies only after evidence and tests; avoid two competing production pipelines. Document one rollback switch until stable.
- [ ] Update Skill/README once: no repeated manual dispatch instructions, no unsupported throughput claims; include user-approved series gate, job status/recovery and measured hardware defaults. Keep credentials out of logs/datasets.

## 12. Implementation sequence and definition of done

1. Baseline instrumentation and correctness/approval tests (K0).
2. Long-job lifecycle, timeout/reconciliation, worker supervision (K1).
3. Minimal A/B scheduler experiment, then C/D only when useful; resource tuning and integration (K2/K3).
4. Chunk planning, verified partial export and recovery (K4; implement earlier if long-run workload cannot complete reliably).
5. Controlled live validation and selection (K5), then merge into general refactor and simplify (K6).

Done when: the same approved multi-chapter workload is produced correctly, measured execution policy uses real allocated resources efficiently, job timeouts no longer force arbitrary 30-minute failures, queue and disk limits are bounded, safe recovery demonstrably avoids recomputing completed chunks, scheduler is explainable and tested, and the final report contains actual benchmark numbers, any regressions/limitations, chosen default and rationale. If a live run was not authorized or completed, explicitly mark performance and provider durability as **unverified**; do not label optimization proven.

## 13. Source references

Pinned source (repository inspected, not modified):

- [Dual GPU worker](https://github.com/nxc1802/Manga-Recap-Skill/blob/f242338af23cfe47c7bfa3b4924ebdbc5be2c455/src/cloud/dual_gpu_worker.py)
- [Kaggle service](https://github.com/nxc1802/Manga-Recap-Skill/blob/f242338af23cfe47c7bfa3b4924ebdbc5be2c455/src/cloud/kaggle_service.py)
- [Payload/result handling](https://github.com/nxc1802/Manga-Recap-Skill/blob/f242338af23cfe47c7bfa3b4924ebdbc5be2c455/src/cloud/payload.py)
- [Account fallback](https://github.com/nxc1802/Manga-Recap-Skill/blob/f242338af23cfe47c7bfa3b4924ebdbc5be2c455/src/cloud/account_fallback.py)
- [CUDA worker](https://github.com/nxc1802/Manga-Recap-Skill/blob/f242338af23cfe47c7bfa3b4924ebdbc5be2c455/src/tts/cuda_worker.py)
- [Fast renderer](https://github.com/nxc1802/Manga-Recap-Skill/blob/f242338af23cfe47c7bfa3b4924ebdbc5be2c455/src/render/fast_renderer.py)
- [Chapter renderer](https://github.com/nxc1802/Manga-Recap-Skill/blob/f242338af23cfe47c7bfa3b4924ebdbc5be2c455/src/render/chapter_renderer.py)
- [Cloud CLI](https://github.com/nxc1802/Manga-Recap-Skill/blob/f242338af23cfe47c7bfa3b4924ebdbc5be2c455/src/cli/cloud_cmd.py)
- Previous `REFACTOR_PLAN.md`, Phase 5 and Phase 8 (Phase 9 test cleanup also relevant).
- [Kaggle kernel metadata documentation](https://github.com/Kaggle/kaggle-cli/blob/main/docs/kernels_metadata.md) — `NvidiaTeslaT4` requests T4 x2; actual devices must be checked at runtime.
- [NVIDIA Video Codec SDK](https://developer.nvidia.com/video-codec-sdk) — hardware encode/decode engines are separate from CUDA cores.

**Decisions proposed without explicit user instruction:** optimize makespan and quota efficiency rather than instantaneous GPU utilization, default to one session when safe, prefer one small scheduler and measured policy selection, keep existing Preview promotion semantics, and use a 20% exploratory worker-time improvement target. These are subject to measured data; no live benchmark or repository modification has been made.
