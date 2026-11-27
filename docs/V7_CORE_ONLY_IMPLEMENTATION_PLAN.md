# V7 — Core-only Architecture: Implementation Plan

**Repository:** nxc1802/Manga-Recap-Skill  
**Baseline:** `297d36378bb2063912a510ae87ca3ab5a7c482bc` (V6.1, main)  
**Status:** User-approved functional scope; implementation pending.  
**Scope:** 66 decisions: 44 remove/consolidate/relocate, 22 retain.

## 1. Mission

Build a focused manga recap Skill, not a general-purpose production framework. This is a deliberate reduction of supported functionality, not another architecture-only refactor. Delete unnecessary runtime branches and their configuration, CLI, schema, tests and documentation together. Preserve all approved capabilities and measured performance of the retained paths.

**Python executes deterministic core operations. The Coding Agent orchestrates, interprets visual evidence, authors narration, and handles exceptions. The human approves configuration, preview and consequential external actions.**

When an unsupported condition occurs, fail clearly and stop. Do not silently change model, account, hardware, crop method, output settings or execution policy. Report stage, error, reason, affected artifact/job and actionable next step. The Agent may explicitly fix the issue and resume; no new automatic fallback framework.

This plan supersedes conflicting feature-retention requirements of [V6.1 plan](V6_1_DEENGINEERING_STABILIZATION_PLAN.md), [V6 refactor plan](REFACTOR_PLAN.md), [Kaggle plan](KAGGLE_PRODUCTION_OPTIMIZATION_PLAN.md), and [Decision plan](GEMINI_DECISION_OPTIMIZATION_PLAN.md) for the 66 decisions below. Earlier benchmark and verification reports remain historical evidence. Do not resurrect retired functionality simply to satisfy old tests.

## 2. Canonical workflow

| Stage | Input / action | Required output / gate |
|---|---|---|
| 1. Setup | Human answers the existing 8 questions Q01–Q05, Q08–Q10; select configured backend and narrator | Valid canonical project settings, explicit approved voice, no hidden override |
| 2. Prepare Chapter 1 | Supported website profile or local images → ingest → VCS → Gemini Decision → verified structural repack → Pass 2 on new children → contact sheets | Raw evidence, panels, manifest, contact sheets; resolve needs_review before authoring |
| 3. Author Chapter 1 | Agent sees sheets + high-resolution evidence, reads OCR/dialogue, tracks characters and continuity, writes Vietnamese single-narrator script | Valid script.json, characters.json, story_summary.md; preserve N:M panel mapping |
| 4. Preview | Selected Local or Kaggle backend, VieNeu v3 Turbo → visual plan → FFmpeg | preview MP4/SRT, concise review_pack.md and review_pack.json |
| 5. Human approval | User watches exact preview and explicitly approves | Fingerprint and artifact hash bound to reviewed media/config; stale approval blocks execution |
| 6. Prepare and author remaining chapters | Repeat stages 2–3; inherit confirmed series settings | All requested chapter scripts/evidence validated |
| 7. Produce | Local native path or Kaggle Dual-T4 Policy B; TTS → render → verify → compile | Verified chapter MP4/SRT, compiled feature MP4/SRT and master WAV |
| 8. Package and release | Agent authors clean 16:9 thumbnail and Vietnamese metadata; save locally or explicitly confirmed YouTube upload; optional private HF archive | Standard output paths, verified upload/archive receipts when requested |

Cleanup is a separate explicit action, never automatic raw-data deletion. Granular commands support deliberate repair/resume; they must use the same core functions as the high-level workflow.

**Ownership:** Agent owns Vision/OCR, narrative, character continuity, creative framing and thumbnail artwork. Gemini Decision is closed-set classification/structure/crop only. Python must not invent story content or substitute missing evidence.

## 3. Exact approved decision register

Legend: **REMOVE** = delete production branch and public option; **SIMPLIFY** = retain necessary behavior with one canonical implementation; **MOVE** = isolate a genuinely retained maintenance/development utility outside runtime, not a way to hide dead code; **KEEP** = preserve feature and regression coverage. The 44 non-KEEP decisions are authorized reductions; all 22 KEEP decisions are mandatory.

### A — Setup (3 decisions)

| ID | Decision | Implementation contract |
|---|---|---|
| A01 | SIMPLIFY | Remove per-command voice/speed/BGM/quality/output-setting overrides; read confirmed series settings. Explicit setup changes invalidate affected artifacts/approval. Necessary repair identifiers, paths for source import and account selection are not configuration overrides. |
| A02 | MOVE | Extract legacy V3/V4/V5 migration into standalone explicit utility. No implicit migration when loading or executing current projects. Reject unsupported schema with migration instructions; preserve old artifacts and backups. |
| A03 | SIMPLIFY | One authoritative confirmed series configuration; one location for technical defaults. Eliminate competing defaults across CLI, config, service and schema. |

Retain 8 explicit questions: source + chapter range, backend, narrator, speed, subtitle burn, publication destination/privacy, quality, BGM/gains. Retain required voice approval; avoid duplicating settings in independently editable sources. Do not add mandatory setup questions without approval.

### B — Data Preparation (9 decisions)

| ID | Decision | Implementation contract |
|---|---|---|
| B01 | REMOVE | Generic crawler fallback for unknown sites. Return unsupported-site error; Agent may use supported profile or import local images. |
| B02 | KEEP | Multi-site profiles and supported-site extraction. Keep registry only as needed for real profiles. |
| B03 | REMOVE | Skip-Decision / manual VLM replacement when Gemini unavailable. Missing API key or failed request blocks prepare. |
| B04 | REMOVE | Offline --reapply-policy runtime, interpretation-only replay branch and dedicated cache-preflight machinery used solely by it. Ordinary validated cache/resume remains. |
| B05 | REMOVE | Decision-specific --dry-run and dry-run report path. Does NOT remove safe cleanup dry-run. |
| B06 | SIMPLIFY | Replace duplicate --refresh-decisions / --reclassify-all with one explicit refresh option; refresh correct source/child decisions without corrupting topology. |
| B07 | REMOVE | Legacy 9-preset crop reader/compatibility in production after explicit migration. Incompatible data must be migrated or re-prepared, never guessed. |
| B08 | SIMPLIFY | Fixed benchmarked 4-sided adaptive settings, no policy/resolution/threshold ablation switches in runtime. |
| B09 | KEEP | Manual Repack and Panel Inspection as explicit Agent repair tools; not a Decision bypass. |

**Canonical Decision:** six questions (classification, structure, four edges); edge options exactly 0/10/20/30/40 percent; medium/high adaptive input resolution using existing H/W threshold 2.2; preserve measured guardrails, no_split, semantic merge verification, deterministic motion, and metadata-only repack where structure unchanged. Cache keys must bind exact content and raw question/model/preprocess contract. Keep final contact sheet generation and child-only Pass 2. Do not reduce API quality to meet size goals.

### C — Script (7 decisions)

| ID | Decision | Implementation contract |
|---|---|---|
| C01 | MOVE | Remove legacy CSV parser from runtime; keep only in explicit migration utility as necessary. |
| C02 | MOVE | Remove legacy JSON parser/implicit normalization from runtime; migrate offline. |
| C03 | REMOVE | Multi-voice strategy and legacy voice strategy branches; single approved narrator only. |
| C04 | REMOVE | Automatic panel substitution when evidence missing/invalid; fail validation, Agent repairs script. |
| C05 | SIMPLIFY | Keep essential pacing checks; remove elaborate redundant diagnostics/heuristics. |
| C06 | KEEP | needs_review status and [CAN_XAC_MINH: ...] uncertainty evidence; do not render unresolved required evidence. |
| C07 | KEEP | Character dictionary and story summary, updated consistently across chapters. |

Single canonical script schema. Do not discard legitimate existing data silently; migration utility should preserve source files and report incompatible fields. Validate story coverage and source lineage, panel IDs, ordering, durations and required assets. Python does not generate creative narration.

### D — Preview & TTS development utilities (8 decisions)

| ID | Decision | Implementation contract |
|---|---|---|
| D01 | REMOVE | HTML Review Pack, duplicated HTML templates and callers. |
| D02 | KEEP | Concise Markdown Review Pack; include useful human review evidence and unresolved issues. |
| D03 | KEEP | JSON approval metadata, exact config fingerprint and artifact hash. |
| D04 | MOVE | Mock TTS out of production runtime into test fixtures; no silent mock backend. |
| D05 | MOVE | Voice/emotion benchmark out of runtime into devtools, still usable for selecting narrator samples. |
| D06 | REMOVE | Deprecated flags --skip-review, --approved, --allow-placeholders and equivalent no-op approval bypass aliases. |
| D07 | KEEP | Verified exact approved preview reuse; invalidate approval when relevant inputs change. |
| D08 | KEEP | Preview both Local and Kaggle, respecting explicit confirmed backend and canonical quality settings. |

Artifacts: preview.mp4, preview.srt, review_pack.md, review_pack.json. Retain actual approval record as needed. Resolve the existing 720p preview vs 1080p final contract without silently upscaling or changing approved bytes: produce at the selected final target for exact reuse, or require explicit human re-review of changed media if a different target is requested. Confirm existing projects' semantics before changing the quality contract; if incompatible, report the issue for user decision rather than silently redefine Q09.

### E — Kaggle (13 decisions)

| ID | Decision | Implementation contract |
|---|---|---|
| E01 | REMOVE | Policy A baseline fallback and production policy selector; Policy B only. Historical benchmark evidence stays in Git. |
| E02 | REMOVE | Kaggle NVENC-to-CPU encoder fallback; fail if required NVENC unusable. |
| E03 | REMOVE | Single-GPU Kaggle execution; fail hardware preflight. |
| E04 | REMOVE | CPU-only Kaggle execution; no accidental local/CPU substitution. |
| E05 | REMOVE | Automatic account fallback; explicit configured account, no rotation. Keep account ownership on reconcile/pull. |
| E06 | KEEP | Standalone remote TTS, shared dual-GPU TTS path, fixed batch size 32. |
| E07 | KEEP | Standalone remote render, shared dual NVENC render path; no model initialization for render-only. |
| E08 | SIMPLIFY | Remove policy and batch size CLI overrides; full produce uses benchmarked batch size 16. |
| E09 | SIMPLIFY | Clear fixed/central technical queue, transfer and poll limits; keep essential chunk-size/workload estimates for E11. |
| E10 | MOVE | Detailed profiling/benchmark harness to devtools; runtime keeps meaningful job/stage status and minimal timings. |
| E11 | KEEP | Automatic Chunk Planner, verified chunk receipts, resume already completed chapters; simplify only redundant estimates. |
| E12 | REMOVE | Unreliable SDK cancellation command and timeout auto-cancel. Do not claim remote job stopped. User/Agent may cancel via provider UI; reconcile actual terminal status before new submit. |
| E13 | SIMPLIFY | Remove speculative automatic recovery transitions; keep minimal durable job intent, identity, remote status, receipts, explicit pull/reconcile and safe retry. |

#### Kaggle execution contract

- Require actual two Tesla T4 CUDA devices. Verify usable NVENC on both for render/produce and remote preview rendering; TTS-only requires two usable T4 CUDA devices, not an unused video encoder. Reject unsupported hardware before costly processing when feasible.
- Single canonical Policy B: each GPU has its own persistent VieNeu model for TTS; native length-aware batching and shared NVENC encode queue; CPU postprocessing/finalizer overlaps where useful. Preserve bounded queues, worker isolation, result order, OOM batch-size backoff within the same model/device, input/output checksums and verified temporary cleanup.
- TTS-only and render-only are intentional job modes, not fallback policies. Reuse existing scheduler, do not clone worker pipelines.
- Keep explicit job intent/reservation across chapter/series/chunk scope under existing lock; ambiguous submit, queue/client timeout and unverified cancellation are NOT terminal. Reconcile original account/kernel before releasing reservation; completed remote job must be synced/verified rather than blindly resubmitted. A provider error or no response is not proof of rejection.
- Fixed timeouts must not prevent legitimate long chunks from finishing. Retain sufficient chunk budget estimation and safe poll/pull semantics; no new generalized state-machine framework.
- On failure report blocker and preserve verified prior chunks and receipts. No automatic retry of unknown-result writes. Short bounded retries for clearly transient, safe operations may remain; no account cycling or provider-limit circumvention.
- Compile completed chapter outputs using shared canonical compiler, not a second cloud-specific compiler implementation.

### F — Rendering (10 decisions)

| ID | Decision | Implementation contract |
|---|---|---|
| F01 | REMOVE | Portrait vertical/padded modes and branches. |
| F02 | REMOVE | Auto video-mode detection; fixed landscape 16:9. |
| F03 | REMOVE | fixed-chars subtitle chunking; sentence-aware only. |
| F04 | SIMPLIFY | Fixed canonical subtitle style/line-count/font settings; remove user overrides while keeping subtitle burn on/off. |
| F05 | REMOVE | Remaining unsupported legacy camera motion paths/aliases. |
| F06 | KEEP | Static and Slow Pan Down, safe framing, authored visual intent consistent with evidence. |
| F07 | SIMPLIFY | Remove keep-temp overrides; clean intermediate clips only after verified output; preserve needed restart evidence on failure. |
| F08 | REMOVE | Non-VieNeu production synthesis paths. |
| F09 | KEEP | Local multi-encoder support for supported hardware (e.g. libx264, NVENC, VideoToolbox); retain existing meaningful selection, no cloud hardware fallback. |
| F10 | KEEP | Master WAV export when compiling series. |

Keep 720p/1080p target profiles, MP4 H.264/AAC, SRT, confirmed speed and optional BGM/voice gain. Retain FFmpeg fast paths, audio/canvas caches, correct static/pan geometry, timing and native batching. No format/quality change for existing approved projects without explicit consent.

### G — Release services (11 decisions)

| ID | Decision | Implementation contract |
|---|---|---|
| G01 | KEEP | YouTube upload and verification, exact user-confirmed target/privacy. |
| G02 | KEEP | Multi-channel configuration and credential management; explicit account selection. |
| G03 | REMOVE | Separate interactive channel selector; use confirmed setup/explicit confirmation. |
| G04 | REMOVE | Custom upload media path overrides; standard chapter/feature paths only. |
| G05 | KEEP | Single-chapter as well as compiled-feature upload. |
| G06 | KEEP | Minimal durable upload resume/verification to avoid duplicate publication and finish attachments. |
| G07 | KEEP | Private HF Archive Push; only when authorized. |
| G08 | KEEP | HF Archive Verify, pinned revision and checksums. |
| G09 | MOVE | HF Restore as standalone explicit maintenance utility, not production workflow. |
| G10 | REMOVE | Automatic raw data cleanup. Explicit clean only; verified temporary media cleanup (F07) remains. |
| G11 | KEEP | Cleanup dry-run default, explicit project confirmation, archive receipt covering current files before raw purge, safe path allowlist. |

Do not add external services. Do not upload, archive, delete data or spend live API/GPU quota without appropriate user authorization. Never commit credentials, tokens, private project artifacts or copyrighted source panels.

### H — CLI (5 decisions)

| ID | Decision | Implementation contract |
|---|---|---|
| H01 | REMOVE | Duplicate high-level commands such as batch and review-approve; canonical produce/approve. Reuse needed implementation functions rather than remove functionality. |
| H02 | KEEP | Granular repair commands for crawl/import, segment, decide/refresh, repack/inspect, TTS, render, cloud pull/reconcile, plus needed account and metadata utilities. |
| H03 | MOVE | Benchmark CLI to devtools, not runtime dispatch. |
| H04 | REMOVE | Deprecated flags and compatibility aliases; unknown command/flag must fail clearly, not silently ignored. |
| H05 | SIMPLIFY | status/inspect report current stage, verified completion, actionable blocker and required job identity; remove verbose duplicated diagnostics. |

Canonical high-level CLI: setup, prepare, validate, preview, approve, produce, compile, publish, archive, clean, status. Use a single JSON response envelope and clear nonzero exit codes. Granular commands call the same implementations and must not bypass setup/approval. Remove flags at parser, dispatch, functions, schemas, config, tests and docs together. Do not add equivalent renamed aliases.

## 4. Correctness fixes required in retained paths

These are source-review findings at baseline, not independently reproduced failures. Start with small regression fixtures; fix only remaining code, not branches scheduled for deletion.

1. **Panel identity after repack (P0):** [ingest/decision.py:1391–1412](../src/ingest/decision.py#L1391-L1412), [1461–1495](../src/ingest/decision.py#L1461-L1495). Repack renumbers IDs; do not match a new panel to an old Pass 1 decision merely because IDs collide. Resolve source lineage + actual SHA and geometry; child split/merge must obtain its own exact Pass 2 decision/cache. Test split-first, merge-first, shifted KEEP/DROP, repeated prepare, and interrupted child classification. No wrong status, crop, provenance or cache entry.
2. **Actual cache integrity (P1):** [ingest/decision.py:816–870](../src/ingest/decision.py#L816-L870). Even though offline replay is removed, normal cache must verify real image bytes against manifest/evidence before using cached results. Reuse existing integrity helpers; stale bytes must fail or explicitly refresh after manifest repair. Do not accept incomplete raw answers or stale model/question/preprocess versions as valid.
3. **Cancellation/duplicate job (P0):** [providers/kaggle.py:335–355](../src/providers/kaggle.py#L335-L355), [kaggle_service.py:881–909](../src/cloud/kaggle_service.py#L881-L909). Remove unreliable SDK cancel per E12, preserve active intent after timeout/unknown status. Provider-side user cancellation is not proof until remote terminal status confirmed. Keep lock/reservation through unresolved state; test queue, client timeout, unknown and completed-unsynced.
4. **HTTP classification (P1):** [providers/kaggle.py:263–305](../src/providers/kaggle.py#L263-L305). Classify explicit rejection/auth, quota/rate limit, propagation error and ambiguous submission accurately. With E05 removed, never rotate account. Do not release intent on ambiguous response; retry only documented safe propagation failures. Test 429, 4xx, 5xx and network/empty response ambiguity.

Also audit cross-scope locking and terminal receipt transitions in surviving cloud paths. Prefer reusing existing locks/checksum helpers. Do not add features under the guise of bugfixes.

## 5. Mandatory invariants

These are not fallback features and are not authorized for deletion:

- Explicit human setup, single approved voice, preview approval with exact evidence/config/media hashes, publication destination confirmation.
- Canonical schema validation, valid story/evidence lineage, safe crop constraints, uncertainty gates and no automatic replacement for missing evidence.
- Atomic writes, minimum durable checkpoints, actual integrity verification, verified media/receipts, safe path boundaries and recoverable failures.
- Kaggle job reservation, original account identity, ambiguous-submit reconciliation, bounded queues, native batches, safe OOM backoff and verified chunk resume.
- No automatic raw deletion, no cleanup without required receipts/confirmation, no secrets in payload or logs.
- Existing fast paths and measured quality/performance for all retained functions.
- Regression coverage for retained functionality. Update tests asserting intentionally retired features; do not weaken tests for the retained invariants.

## 6. Implementation phases

Use small coherent commits; do not modify unrelated files or user project data. The register is authoritative: check every ID off with changed files and verification evidence. No new scope, service, plugin, model, abstraction layer or fallback policy.

### P0 — Baseline and dependency/deletion audit

- [ ] Record current starting SHA and actual repo status. Inventory src/ Python bytes, nonblank LOC, file count, per-domain sizes and Skill length.
- [ ] Run existing tests, compileall and requirements sync. V6.1 report states 585 passed; this is historical evidence, not a substitute for fresh baseline.
- [ ] Trace references/callers for each removed option, including dynamic imports, config, persisted schemas, worker payload/bootstrap, scripts, CLI and docs. Identify features already absent rather than reimplement to remove.
- [ ] Capture canonical fixtures for Local, dual-GPU produce/TTS/render, approved preview, Decision repack/cache, archive and YouTube receipts. Keep all user project data untouched.
- [ ] Establish migration/rollback route before removing implicit legacy support. Backup or copy test fixture data; migrate explicitly, validate, preserve source. No destructive reset or force-push.

### P1 — Setup, schema, Decision and script (A, B, C)

- [ ] Enforce one canonical settings and script load boundary. Isolate necessary migration utilities.
- [ ] Remove Decision replay/dry-run/legacy/ablation/skip paths and duplicate refresh flag; keep only canonical 4-sided method and explicit repair tools.
- [ ] Fix panel identity and actual image hash correctness; tests with renumbered panels and stale cache.
- [ ] Remove unsupported crawler fallback, narration strategies and automatic substitutions. Keep multi-site profiles, story state and uncertainty validation.
- [ ] Run ingest, compatibility/migration and script regression tests.

### P2 — Preview, media and Local (D, F)

- [ ] Replace review pack with concise MD + approval JSON; remove HTML generation and deprecated bypass aliases.
- [ ] Isolate Mock TTS and voice benchmark; one production VieNeu implementation.
- [ ] Fix landscape-only rendering, subtitle defaults, static/slow-pan, verified temp cleanup, keep Local encoder support and master WAV.
- [ ] Consolidate duplicate TTS/render/compile invocation logic; reuse existing core fast paths and renderer.
- [ ] Run actual offline FFmpeg fixture rendering, approval invalidation/reuse and media parity checks. Mock speech is test evidence only, never a production artifact.

### P3 — Kaggle strict production path (E)

- [ ] Remove Policy A, CPU/single GPU fallback, account auto-fallback, policy/batch overrides and cancel path; simplify role mapping and dispatch, retain intentional produce/TTS/render modes.
- [ ] Keep automatic chunk planner with shared minimal workload estimates, verified resume and durable intent. Simplify deadline/telemetry, no hidden retries on unknown writes.
- [ ] Fix cancellation/HTTP categorization correctness, run fault injection and dual-device scheduler tests. Test unsupported hardware fail-fast without silent fallback.
- [ ] Verify payload builds/imports after deletion, no retired modules included. Confirm unchanged approved chapter handling and final compile.
- [ ] Keep advanced benchmarks outside src/; no live jobs without authorization.

### P4 — Release, CLI and Skill (G, H)

- [ ] Preserve YouTube multi-channel and chapter/feature upload, verification/resume; remove redundant selector/custom paths.
- [ ] Preserve private HF push/verify, move restore to standalone maintenance utility, explicit clean only.
- [ ] Remove duplicate high-level commands, old flags/aliases, verbose diagnostics and runtime benchmark entrypoints.
- [ ] Rework SKILL.md into the 8-stage canonical workflow; concise commands, inputs, outputs, stop gates. Move rare repair/maintenance details to a short reference only where needed. Deduplicate AGENTS, README and references.
- [ ] Remove dead code, unused dependencies, stale config and tests exclusively covering removed behavior; regenerate lock/requirements only using canonical project tooling.
- [ ] Verify no surviving imports of removed runtime components and all supported granular commands route to the same core implementation.

### P5 — Final validation and release report

- [ ] Run full available test suite and static checks, CLI help/error contracts, migration utility and devtools smoke. Record actual commands and results.
- [ ] Complete offline end-to-end fixtures for Local and mocked Kaggle/provider operations with actual FFmpeg output checks.
- [ ] When authorized, compare live Kaggle and Gemini Decision with existing frozen V6 baseline workloads, plus long-series workload. If not authorized/unavailable, report NOT RUN and remaining release-validation gap, never fabricate.
- [ ] Audit all 66 decision IDs, deleted/retained code, size/performance, limitations, breaking changes and rollback. Stop once agreed scope is implemented; no follow-up speculative roadmap.

## 7. Verification and acceptance

### Required functional evidence

| Area | Acceptance |
|---|---|
| Setup | All 8 questions explicitly answered; settings inherited; removed overrides rejected; no implicit migration |
| Prepare | Supported site + local input; unknown site and missing Decision fail clearly; 6-question crop method, verified split/merge and child decisions; actual checksum validation |
| Script | Canonical single narrator, N:M evidence, uncertainty gate, characters/story continuity; no auto substitution |
| Preview | Local and Kaggle command routes; MP4/SRT/MD/JSON; exact approval, stale input rejection, unchanged preview reuse |
| Kaggle | Strict hardware rejection, dual-GPU scheduler for produce and standalone TTS/render, correct order, verified chunk resume, no duplicate dispatch on unknown/timeout/unsynced, no account rotation/cancel fallback |
| Render | Actual 16:9 output, 720p/1080p as confirmed, static/pan frames, correct crop, audio duration, BGM, subtitles, Local encoders, master WAV |
| Release | Explicit YouTube destination, chapter/feature, duplicate-safe resume, private HF push/verify, explicit safe cleanup |
| CLI/docs | 11 canonical high-level commands plus retained granular/maintenance tools; retired options not accepted; clear JSON failures; Skill matches reality |

### Performance and complexity

V6.1 baseline Git tree: 100 Python files, 1,126,271 source bytes; repo report: 24,057 nonblank LOC, 585 tests. V7 aspirational target: **650–800 KB** (roughly 29–42% smaller). This is a user-confirmed direction, not a guaranteed estimate of deletable code. Measure total runtime Python source and separate devtools/migration/fixtures footprint; relocation alone does not count as actual deletion. Report both net deletion and legitimate moved code. Never minify or remove retained behavior to hit a metric.

- Reference live V6 Kaggle: 71 segments / 167 shots, two T4, Policy B bs16 worker makespan 179.04s; historical single-run result, not V6.1 live measurement. Compare same inputs/settings/hardware and cold/warm conditions; report actual end-to-end vs worker timing, quota, output quality. Aim for no meaningful regression (provisional 5% tolerance on comparable medians, not absolute guarantee).
- Preserve Decision held-out adaptive baseline (118 raw panels, 206,578 input tokens, 241.29s historical run). Check actual output quality and story/crop correctness, not just tokens. Reuse frozen corpus when available; distinguish human-labeled accuracy from prior-manifest agreement.
- For long series, use representative 10–20 chapter fixtures/simulation, then live workload only when authorized; report chunk/resume correctness, resource usage and throughput. Do not optimize against simulation alone.
- For live performance claims use repeated comparable runs where quota permits; label variance, limitations and actual sample count.

### Definition of Done

All 66 decisions accounted for (44 reduction actions, 22 retained); no unauthorized scope expansion. All retained paths pass functional regression tests. Four reviewed correctness risks are resolved or demonstrably eliminated by removal. No hidden fallback, redundant old runtime or alternative production policy survives. Explicit migration/maintenance tools remain usable where approved. Skill is short and actionable, no repeated rules.

Final report must include: baseline and final SHA, per-ID outcome, actual bytes/LOC/files before/after including moved code, deleted feature list, breaking CLI/schema changes, commands/tests and actual results, benchmark evidence vs NOT RUN, known blockers, and non-destructive rollback/migration instructions. If the size target cannot be reached safely, explain why rather than delete protected code.

**Implementation boundary:** User authorized this functional scope and preparation of this Plan; external paid jobs, actual uploads/archive writes and destructive project operations still require separate authorization. Preserve user data and existing repository history.
