# V6.1 — De-engineering & Stabilization Plan

**Repository:** nxc1802/Manga-Recap-Skill  
**Reviewed baseline:** `fba554f5522cce345212eefb4b3bb2079dcb8b75` (V6 main)  
**Status:** Proposed implementation plan — not an implementation or a new benchmark result.

## 1. Objective and scope

Consolidate V6 into a lean, reliable manga recap Skill. Preserve the demonstrated advantages of Dual-T4 Policy B and 4-sided adaptive Decision, while removing experimental branches, unnecessary compatibility layers, redundant work, and misplaced responsibilities. Fix correctness risks before deleting code.

This plan supersedes the expansion-oriented portions of the previous three plans for the stabilization phase. Read [V6 master report](V6_MASTER_UPGRADE_AND_BENCHMARK_REPORT.md), [refactor plan](REFACTOR_PLAN.md), [Kaggle plan](KAGGLE_PRODUCTION_OPTIMIZATION_PLAN.md), and [Decision plan](GEMINI_DECISION_OPTIMIZATION_PLAN.md) as historical context, not as instructions to reintroduce complexity.

**Do not** add new models, services, frameworks, execution policies, databases, generalized provider abstractions, or a new state machine unless a demonstrated correctness issue cannot be solved by existing primitives. Avoid a wholesale rewrite, mass file renaming, or arbitrary line-count targets. Prefer deletion, direct functions, and reuse of existing tested code.

### Baseline evidence

| Metric | V5 (6da4699) | V6 (fba554f) |
|---|---:|---:|
| Python files under src/ | 74 | 101 |
| Source bytes | 864,223 | 1,141,485 (+32.1%) |
| CLI bytes | 230,203 | 89,042 |
| Cloud bytes | 77,228 | 208,702 |
| Skill tree bytes | 40,018 | 24,523 |

V6 report records 583 passing tests, Dual-T4 Policy B produce makespan 179.04 s (bs=16) vs Policy A 210.18 s (bs=12), and Decision held-out 206,578 vs 282,114 input tokens (118 raw panels). These are repository-reported historical measurements, not independently rerun results. Single-run cold Kaggle measurements are not sufficient to claim universal optimality.

## 2. Non-negotiable contracts

- Retain eight explicit human setup answers, approved single narrator, voice/speed/BGM/subtitles/quality settings, and Chapter 1 preview approval bound to exact evidence and artifact hashes. Do not silently rerender or alter an approved preview, bypass approval, or change confirmed output settings.
- Preserve visual/OCR evidence, ACTIVE / CONTEXT_ONLY / SKIP / NEEDS_REVIEW semantics, safe split/merge, source lineage, narration/character continuity, and render-only crop. No loss of raw story evidence.
- Keep local execution and Kaggle standalone TTS/render/produce, full-series compilation, verified resume, payload integrity, fallback when hardware is degraded, and actual dual-GPU concurrency.
- Keep existing provider boundaries, explicit publish confirmation, resumable YouTube publishing, private HF archival and verified archive-gated cleanup. Default cleanup remains dry-run.
- Preserve existing project data and supported project schema through tested migrations. Do not erase user projects, receipts, archives, or historical evidence. Separate temporary benchmark fixtures from production data.
- Preserve reliable error reporting, fail-closed gates, bounded queues, heartbeat, timeout handling, checkpointing, checksum verification, and meaningful telemetry. Simplify implementation, not safety.
- Never work around provider limits using account rotation. No live upload/publish/delete or billable cloud/API runs without appropriate authorization.

## 3. Execution order

### S0 — Establish baseline and deletion inventory (P0)

- [ ] Inspect current main and existing changes before editing; record exact starting SHA.
- [ ] Run existing full test suite, compileall, dependency sync check and CLI help. Record pre-existing failures separately.
- [ ] Inventory production entrypoints, imports, compatibility aliases, persisted schemas, feature flags, policies, benchmark-only helpers, and actual callers. Use current code, tests, Skill and docs; do not classify a feature as dead merely because a grep misses dynamic callers.
- [ ] Capture source file count, bytes and nonblank LOC for src/, per-domain sizes, Skill docs, test count, and representative feature/golden fixtures.
- [ ] Record retain / consolidate / move to devtools / remove decisions with evidence. Keep this inventory short; avoid inventing new runtime machinery for the audit.

### S1 — Fix Kaggle lifecycle before simplifying (P0)

Evidence at reviewed baseline: [lifecycle.py:38–45](../src/cloud/lifecycle.py#L38-L45), [lifecycle.py:197–246](../src/cloud/lifecycle.py#L197-L246), [account_fallback.py:44–51](../src/cloud/account_fallback.py#L44-L51), [kaggle_service.py](../src/cloud/kaggle_service.py). Live report documents SDK cancellation limitations.

- [ ] Treat QUEUE_TIMEOUT and CLIENT_TIMEOUT as potentially active remote jobs, not terminal outcomes. Reconcile using persisted original account and kernel reference; block new submit until proven safe. Missing/ambiguous status and failed cancellation must remain unresolved, not successful cancellation.
- [ ] Persist intent and reserve overlapping chapter/series jobs atomically under an existing project-level lock; release lock after reservation, not after the entire remote job. Cover concurrent dispatch, restart, chunk dispatch and single-chapter vs series overlap.
- [ ] Restrict fallback to explicitly classified safe pre-submit failures. Remove broad retry-on-Exception and failed-result fallback if execution may have occurred. Reconcile ambiguous submit/transfer/poll before retry. Do not use fallback to evade quota restrictions.
- [ ] Preserve kernel/account identifiers, lifecycle state, receipts and retryable sync. If the provider cannot cancel reliably, report the blocker and use pull/reconcile; do not fake remote cancellation.
- [ ] Add tests for timeout with remote still queued/running, timeout after remote completion, unknown status, cancel returning false, concurrent reservation, ambiguous submit, sync failure, and safe pre-submit rejection. Assert no second submission until remote termination or verified sync.

### S2 — Fix Decision reapply/cache integrity (P0)

Evidence: [ingest/decision.py:1159–1196](../src/ingest/decision.py#L1159-L1196), [1328–1435](../src/ingest/decision.py#L1328-L1435), [1495–1557](../src/ingest/decision.py#L1495-L1557).

- [ ] Make `--reapply-policy` strictly offline: preflight full compatible cached evidence before writes; missing data returns actionable error without API calls, partial mutations or hidden refresh.
- [ ] Never treat VCS contiguity as semantic merge verification. Reuse only verified cached pair evidence with exact hashes, geometry and question/model version. Missing or uncertain evidence => retain structure or flag needs_review.
- [ ] Preserve validated split decisions on reapply; do not silently drop previously verified cuts or invent new ones. Reapply on the current final panel set by default, without structural mutation. If topology changes are explicitly supported, require exact child cache and transactional plan; otherwise require explicit fresh decision run.
- [ ] Do not apply parent crop/classification to split/merged children. Bind child raw answers and geometry to exact image hashes. Reinterpret compatible raw answers when thresholds change rather than reuse stale derived decisions.
- [ ] Distinguish raw API question/model/resolution fingerprint from local interpretation thresholds; reject incompatible cache schemas and never infer missing confidence as certainty. Keep actual source checksum/provenance and manual review state.
- [ ] Preserve transactional repack, metadata-only KEEP path, source evidence, one final contact sheet generation and static/motion crop parity. Do not invalidate valid approval for an unchanged semantic result.
- [ ] Tests: zero network calls with complete/incomplete cache, no writes on invalid preflight, cached split/merge accept/reject, child-specific crop, stale image/question/model cache, rerun idempotency, interrupted repack recovery, needs_review gating, approved evidence invalidation on meaningful change.

### S3 — Consolidate Kaggle runtime (P1)

- [ ] Production default: Dual-T4 Policy B (dual_tts_shared_encode) on verified dual CUDA + NVENC; one simple existing baseline/degraded fallback. Retain usable CUDA for TTS when NVENC is unavailable where existing tested behavior supports it.
- [ ] After parity and rollback verification, retire Policies C/D and experimental dispatch variants from runtime/CLI. Keep A only as practical baseline/fallback; preserve historical A–D experiments in Git and existing reports, not extra live production branches. Move useful benchmark harness into devtools if appropriate; do not duplicate scheduler code.
- [ ] Keep separate defaults: produce batch size 16, standalone TTS 32; retain existing bounded override if needed. No new autotuner, policy matrix or configuration taxonomy.
- [ ] Reuse a single scheduler/coordinator and finalizer where behavior is shared. Remove duplicate branches, wrappers and redundant telemetry calculations only after tests. Preserve native batching, OOM backoff, device isolation, ordered results, bounded queues, chapter overlap and verified temp cleanup.
- [ ] Keep chunk planning/resume based on real workload, storage and quota constraints. Do not claim partial artifacts on a killed remote session are durable until provider output is downloaded and verified. Preserve successful chunks on subsequent failure.
- [ ] Measure actual full-series makespan and quota efficiency separately. Historical bs=12/16/32 results: 183.89/179.04/190.57 s worker runtime and 227.93/245.89/216.72 s quota respectively; bs=16 is fastest in that run, not universally most quota-efficient.

### S4 — Consolidate Gemini Decision runtime (P1)

- [ ] Keep one canonical production policy: 4-sided adaptive, four edge choices each {0,10,20,30,40}, plus classification and structure action; deterministic motion. Keep medium/high adaptive resolution (current H/W threshold 2.2), calibrated confidence and geometric safety unless new evidence supports change.
- [ ] Preserve minimum retained width/height/area, conservative backoff, needs_review, no_split, semantic merge verification, child-only second pass, conditional high-risk verification if demonstrably useful, and versioned cache.
- [ ] Remove legacy 9-preset and intermediate ablation branches from runtime after migration/rollback/parity checks. Keep historical benchmark artifacts, not duplicate production implementations. Old projects must remain readable; migrate only when safe and explicitly required. Use full-image conservative fallback rather than guessing crop on incompatible old evidence.
- [ ] Consolidate question construction, raw answer validation, interpretation, structure resolution, cache, manifest update and telemetry. Provider handles HTTP/retry/encoding/response validation; ingest handles domain decisions; renderer handles geometry and render policy.
- [ ] Retain refresh and offline reapply only if semantics are clear and tested. No new AI requests for deterministic tasks, no repeated image encoding or redundant sheet generation, no unchecked default-to-ACTIVE.
- [ ] Verify both static and motion output using actual rendered frames, not JSON coordinates alone.

### S5 — Reduce workflow, compatibility and documentation complexity (P1)

- [ ] Slim core/workflow.py (2,123 lines at baseline): orchestrate existing domain functions; relocate actual business logic to existing owning modules instead of creating a new abstraction hierarchy. Apply the same principle to ingest/decision.py (1,684 lines) and cloud/dual_gpu_worker.py.
- [ ] Eliminate direct pass-through wrappers, duplicated validation/path/timestamp/FFmpeg logic and unused imports only after caller audit. Maintain dependency direction: CLI -> workflow/domain -> provider or media; no domain dependence on CLI.
- [ ] Keep canonical CLI entrypoints for setup, prepare, validate/status, preview, approve, produce, publish, archive, clean. Retain necessary granular commands for recovery/debug and supported integrations; retire aliases only with migration/help/docs updates and regression coverage.
- [ ] Keep settings centralized, provider-specific credentials isolated, pyproject dependency source and generated requirements sync. Do not introduce a global universal provider class or a redundant configuration layer.
- [ ] Keep 7 Skill stages, explicit human stop conditions, exact commands, input/output and recovery. Move compatibility commands, policy details and rare troubleshooting to existing references only where needed. Avoid duplicating rules across AGENTS, SKILL, README and references.
- [ ] Recheck actual archive/publish integration, data migration and legacy project fixtures before retiring compatibility paths. Do not delete tests merely to make the suite pass.

### S6 — Verification and release decision (P0)

Run regression tests incrementally after each phase and full suite at the end. New tests must reproduce each fixed bug and assert the intended behavior; do not weaken existing safety assertions. Use isolated fixtures and mocked providers for fault injection.

| Area | Required verification |
|---|---|
| Build/CLI | compileall src tests scripts, pytest -q, scripts/sync_requirements.py --check, canonical CLI --help and representative JSON errors |
| Setup/review | Missing answers/voice, stale fingerprints, exact approved preview reuse, no approval bypass |
| Decision | Original vs new output on frozen validation/held-out panels, 625 edge combinations, valid geometry, no story loss, offline cache 0 calls, full-resolution evidence and static/motion frame parity |
| Kaggle | Mocked lifecycle/concurrency faults; single/degraded GPU fallback; Dual-T4 Policy B media parity; chunks/resume/checksums; no duplicate submission |
| TTS/render | Narration order/voice/speed, WAV validity, subtitles timing, aspect ratio, FPS, audio/video duration, ffprobe and representative visual/audio inspection |
| Release | Destination confirmation, mock resumable upload and verification, private archive receipts, dry-run and cleanup gates |
| Compatibility | Existing V5/V6 project fixture load/migrate, no data loss, rollback documented |

**Performance verification (when authorized and resources available):**

1. Reuse the existing 71-segment / 167-shot Kaggle workload and frozen Decision held-out corpus for direct comparability. Compare V6 baseline to candidate with same settings, hardware, corpus and cache conditions. Record cold/warm separately.
2. Run at least three comparable trials for important performance claims if quota permits; report individual results and median/range. Test a representative 10–20 chapter workload to confirm long-run scheduling, queue bounds, resume and disk behavior; use offline simulation/fixtures first and live only when authorized.
3. Record worker makespan, end-to-end submit-to-sync time, quota consumed, video seconds per quota second, first encode start, stage overlaps, OOM and actual GPU/NVENC/CPU utilization. Do not interpret low VRAM as idle GPU.
4. Decision: measure total and text/image input tokens including split/merge/verification; latency, cache hits, calls on unchanged rerun, reviewed crop errors and false story drops. Use human-labeled cases across multiple series for classification/split/merge metrics; agreement with a historical manifest is not ground-truth accuracy. Report actual sample size and unresolved review cases.
5. Preserve benchmark inputs and machine-readable outputs without credentials or copyrighted source panels in public Git. If live tests cannot run, report them as NOT RUN, not PASS; avoid replacing real evidence with synthetic claims.

## 4. Acceptance criteria / Definition of Done

- All P0 defects above fixed and covered by reproducible tests; no unresolved known duplicate submit or unsafe reapply path.
- Existing functional contracts retained; no silent story/asset loss, incorrect approvals, fake publication/archive success or unexpected network calls.
- One canonical production Decision policy, one preferred dual-GPU execution path and one safe fallback. No production-only dependencies on retired experiments.
- Source code materially simpler: target 20–30% reduction versus 1,141,485-byte V6 baseline **only if justified by actual redundant code**. Report bytes, LOC, file count, deleted branches/wrappers and responsibility changes; never game metrics by minification, moving code outside src/, or deleting functionality/tests. If target is unattainable safely, explain why.
- No meaningful performance regression on comparable workload; provisional tolerance <=5% median makespan/total tokens against V6 with same conditions, or explain variance and obtain approval before accepting a trade-off. No categorical claims of optimality from a single run. Zero catastrophic crop errors on reviewed acceptance set (not a guarantee on unseen data).
- Full available test suite green, or explicitly document pre-existing failures and blockers. Live validation gaps remain open; do not mark release fully verified if required evidence is missing.
- Skill shorter and clearer, docs match actual CLI and default policy, rollback via pinned pre-change commit and supported project migration. Avoid long-lived duplicate implementation just for rollback.

## 5. Deliverables

- Small, reviewable commits per coherent change; prioritize S1/S2 fixes before removing runtime branches. Preserve unrelated user changes.
- Updated code, regression tests, concise Skill/README/reference updates, and a brief final report containing baseline/after metrics, retain/remove decisions, test commands/results, actual benchmark evidence, unresolved issues and rollback procedure.
- Keep the implementation simple: no new roadmap/framework for this stabilization. Stop once correctness, existing capabilities and measurable simplification are demonstrated.
