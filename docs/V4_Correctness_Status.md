# V4 correctness evidence and acceptance status

Date: 2026-11-11 (Asia/Ho_Chi_Minh). **Not release ready (P0–P7 initial live E2E verified; exact-HEAD P0–P7 re-pin/freeze and P8–P15 pending).**

This document is the **canonical gate ledger** and single source of truth for V4 gate and phase status. The controlling plan is [V4_Correctness_Improvement_Plan.md](V4_Correctness_Improvement_Plan.md); the older [implementation plan](V4_Implementation_Plan.md) is historical. A local test, an implemented function, and a real/live acceptance gate are distinct evidence levels.

## Verification checkpoints

### 1. Deterministic baseline checkpoint (`0d10ff1` / `c890afe`, 2026-11-11)

Pulled `ba11e81ac64733327c4bfc1dbe2bb9845ac6b87c`. Tested implementation contents are committed as `0d10ff13b18313f76eddb9b81271208ab68a5dcd`, and the baseline verification & blocker record is committed as `c890afeeeb2512e91f9044e7357d01568335120f`.

- **R1 fixed:** authored tall-panel crops retain the full per-panel canonical region list; arbitrary regions and reversed y-min/y-max progression are rejected, including nonconsecutive static shots. Canonical order, a single safe crop, and a full static hold have explicit adversarial coverage.
- **R2 frozen:** height `<80 px` means `short_sliver`; `80 px` is not a sliver. The argument affects both source metadata and actual image inputs.
- **R3 deterministic checks green:** `python3 -m pytest -q --disable-warnings` (with JUnit output) returned **793 passed, 0 failed, 0 errors, 0 skipped** in 90.62s. All 183 failed/error nodes from the initial run are classified in the [migration ledger](verification/v4_regression_migration.json). Legacy scenarios were consolidated with explicit replacements, fixtures migrated, and deferred/live specifications moved to [acceptance scope](../acceptance/README.md). Their removed/skipped counts are not reported as passing acceptance.
- **R4/R5 & hardening fixes:** production TTS parsers only accept neural; retained compatibility flags cannot bypass approval/receipts; missing project config is no longer swallowed by inspection or status; reading a missing setup creates no project directory; missing render source metadata fails explicitly; tar links/devices/duplicate members are rejected before extraction on all supported Python versions.
- **Checkpoint gate state at `c890afe`:** local compilation, frozen schema check, and local diff whitespace check passed; **G4–G7 remained OPEN** pending real Chapter 1 E2E execution (recorded in [V4_Remaining_Fixes_Result.md](verification/V4_Remaining_Fixes_Result.md) and [v4_remaining_fixes_2026-11-11.json](verification/v4_remaining_fixes_2026-11-11.json)).

### 2. Initial live E2E checkpoint (`055d17f`, 2026-11-11; runtime `environment.json` recorded `c890afe`)

Real Chapter 1 E2E (`nhan-vat-phu-trong-the-gioi-dien-loan`, chapter `1`) executed end-to-end with real neural audio, canonical VisualPlan rendering, human approval, and live Kaggle execution (`docs/verification/v4_e2e_2026-11-11/`, committed in `055d17fe327b9504abab2ad6edaec5450b5745c3`).

- **Source SHA note:** `docs/verification/v4_e2e_2026-11-11/environment.json` recorded `git_sha: c890afeeeb2512e91f9044e7357d01568335120f` during the live run, and commit `055d17f` subsequently updated `src/cloud/kaggle_service.py` (dataset auto-extraction handling, dataset readiness polling, timeout increased to 1800s) and added `scripts/wait_and_sync_kaggle.py`.
- **G1 (Strict Form Setup):** **PASS (initial live E2E)**. Collected Q01–Q10 explicitly via `ask_question`. `setup validate` passed with `snapshot_hash: 89391b5d9caf937da1d13ba2373eb4e6a6ceaaa83355b87f3bd365ab1182c31d` (`local_run.json`).
- **G2 / G3 (Canonical Contracts & VisualPlan):** **PASS (initial live E2E)**. Generated and verified 97 canonical shots (`motions: ["static"]`, `input_fingerprint: 29b17ad895197d858933d91b60d732a1a7c9ff04971058088e869f51fc62dec9`, `local_run.json`).
- **G4 (Real VieNeu-TTS Synthesis):** **PASS (initial live E2E)**. 37 segments synthesized with neural VieNeu-TTS v3 Turbo (`"Ngọc Huyền"`, `vieneu` 3.8.3 in `venv/bin/python` Python 3.12.13), 100% non-silent, RMS > 0.10, total timeline 304.24s (`local_run.json`, `local_artifact_report.json`).
- **G5 (Preview Render & Validation):** **PASS (initial live E2E)**. Rendered `video/preview.mp4` (9127 frames, 304.24s, 187 cues SRT). Sampled frame variance > 2000, audio RMS validated, SHA-256: `f74608cdf29c1afe130909f01ac7f69f0e1f58bc4b8f468697f64c68c344f330` (`local_artifact_report.json`).
- **G6 (Human Approval Gate):** **PASS (initial live E2E)**. Human review approval confirmed by reviewer `cuongnguyen1802` at `2026-11-11T13:55:16.427010Z` (`config_fingerprint: 6155808a9e2ea78e2333e6b819a22d77bcc3c5267b2af4eb2a91486e788cda7c`, `human_approval.json`, `human_review.md`). Final promoted artifact bit-for-bit matched preview (`final_sha256 == preview_sha256 == f74608cdf29c1afe130909f01ac7f69f0e1f58bc4b8f468697f64c68c344f330`).
- **G7 (Live Kaggle Cloud Run):** **PASS (initial live E2E)**. Live Kaggle cloud execution completed on Tesla T4 GPU (`cuongnguyen1802/mr-nhan-vat-phu-c1-6793`, duration 769s, payload manifest SHA `aef89865028f0e4c387da49284707f46c03f9c72d66afe5786139411101bdbf5`). Downloaded outputs verified, `sync_result` executed, `semantic_parity: true` (`kaggle_run.json`, `semantic_parity.json`), and 5/5 failure-path checks passed (`failure_path_checks.json`).
- **Evidence Package:** Committed in [docs/verification/v4_e2e_2026-11-11/](verification/v4_e2e_2026-11-11/README.md).

### 3. Pending exact-HEAD P0–P7 re-pin & freeze

- **Why re-pin is pending:** Because `055d17f` modified `src/cloud/kaggle_service.py` after the `c890afe` E2E snapshot (and CI/whitespace/doc cleanup commits are landing on top of `055d17f`), an **exact-HEAD P0–P7 E2E re-pin** is scheduled once CI passes on the cleaned branch HEAD.
- **Freeze boundary:** P8–P9 (Dual-T4 / benchmarks) and P10–P15 (HF archive/restore, YouTube verification, receipt-gated cleanup, test pyramid separation, canonical release state machine, release DoD) remain gated behind that exact-HEAD P0–P7 freeze.

## Ordered phase ledger (P0–P15)

| Phase | Implementation & verification evidence | Gate / roadmap status |
|---|---|---|
| P0 | Clean repository, canonical schemas exported and frozen, deterministic suite green (`793 passed` at `0d10ff1` / `c890afe`) | **PASS (baseline; CI & exact-HEAD freeze pending)** |
| P1 | Central router policy; strict runtime enforcement; explicit Q01–Q10 via `ask_question` (`snapshot_hash: 89391b5d...`) | **PASS** (G1, initial live E2E) |
| P2 | Canonical `src/core/visual_contract.py`; strict script schemas; evidence panel IDs; input fingerprints | **PASS** (initial live E2E) |
| P3 | Three production motions only; vertical top-down ordering; safe regions; sliver omission guard | **PASS** (G3, initial live E2E) |
| P4 | Real neural VieNeu-TTS synthesis (`"Ngọc Huyền"`); 37 segments; 304.24s duration; measured durations in script | **PASS** (G4, initial live E2E) |
| P5 | Preview render; 9127 frames; 304.24s duration; frame variance > 2000; non-silent audio; 187 SRT cues | **PASS** (G5, initial live E2E) |
| P6 | Review pack generation; explicit human approval (`cuongnguyen1802`); bound fingerprints; bit-for-bit final promotion | **PASS** (G6, initial live E2E) |
| P7 | Live Kaggle cloud execution (`cuongnguyen1802/mr-nhan-vat-phu-c1-6793`); Tesla T4 GPU; result sync; `semantic_parity: true`; 5/5 failure checks | **PASS** (G7, initial live E2E at `c890afe`; exact-HEAD re-pin pending after `055d17f` `src/cloud/kaggle_service.py` update) |
| P8–P9 | Dual-T4 multiprocessing architecture & reproducible real benchmarks | **GATED / NOT STARTED** (disabled until exact-HEAD P0–P7 freeze; standalone probe prep only; no performance report published) |
| P10–P12 | Complete HF private archive/restore round-trip, YouTube processed verification, receipt-gated cleanup | **GATED / NOT COMPLETE** (schemas frozen; live lifecycle round-trip & integration verification remain gated behind exact-HEAD P0–P7 freeze and P8–P9) |
| P13–P15 | Full CI (L1–L3) vs LIVE-L4 pyramid, canonical release state machine (`DRAFT` -> `CLEANUP_ALLOWED`), final release gate / DoD | **GATED / OPEN** (branch is not release ready) |

## Canonical contracts

Reviewable JSON Schema snapshots live in `schemas/v4/`. Regenerate intentionally and check drift:

```bash
PYTHONPATH=. python scripts/freeze_contracts.py
PYTHONPATH=. python scripts/freeze_contracts.py --check
```

- Explicit series setup: 4.0.0; project revision and setup snapshot must match.
- Script: new documents use 4.1.0; explicitly identified 3.0.0 documents remain readable only where their fields meet the strict schema. `panel_ids` is a checked evidence alias, not shot instructions. Conflicting aliases and embedded legacy shot plans fail closed.
- VisualPlan/Shot: 4.1.0, unknown fields and legacy motions rejected. A stale/missing input fingerprint on an existing production plan requires regeneration or an explicit authored-plan update.
- Human approval: 4.1.0; old status-only approvals do not authorize production.
- Archive/YouTube receipts: frozen 4.0.0 shapes. Completing their lifecycle validator integration remains P10/P11 work.
- Release record: frozen 4.1.0 shape only. This is not the P14 transition engine and must not be used to assert release readiness.

## Historical verification checkpoint (before the `0d10ff1` / `c890afe` handoff)

The focused L1/L2 suite uses isolated unit fixtures, including explicitly synthetic PCM/image inputs. Those fixtures test failure handling, actual encode/decode, N:M planning, approval binding and result import. **They do not prove real neural speech, actual human approval, a real manga chapter, or a live cloud run.** No fixture was inserted into a user's project to obtain production acceptance. An interrupted cloud artifact copy leaves the chapter running rather than completed; a changed local input snapshot rejects the result before publication.

Focused command:

```bash
python -m pytest -q --disable-warnings \
  tests/unit/test_series_setup.py \
  tests/unit/test_visual_policy.py \
  tests/unit/test_artifact_correctness.py \
  tests/unit/test_canonical_workflow.py \
  tests/unit/cloud/test_canonical_cloud.py \
  tests/unit/test_tts_engine.py \
  tests/unit/test_schemas.py
```

Historical focused result at the earlier source checkpoint: **155 passed**. Syntax compilation, frozen schema drift check and patch whitespace check passed locally. GitHub CI had not been run for that local change. The full regression on source commit `6a16477d0ed6a16fcc39b94552aee07aed6322fb` returned **694 passed, 168 failed, 15 errors, 7 skipped** (exit 1). All focused modules also passed in that full run. That historical suite was **not green**. That earlier checkpoint is superseded by the green deterministic baseline (`0d10ff1` / `c890afe`, 793 passed) and the subsequent initial live Chapter 1 E2E verification (`055d17f`, [docs/verification/v4_e2e_2026-11-11/](verification/v4_e2e_2026-11-11/README.md)); an exact-HEAD P0–P7 E2E re-pin remains pending after CI and documentation cleanup. See [machine-readable evidence](verification/v4_correctness_checks_2026-11-11.json) and the [failure inventory](verification/v4_regression_failures_2026-11-11.txt).

Earlier repository-wide runs were red, including tests that assert flag-based approval bypass, source-string details of the removed cloud renderer, status-only approval, mock production synthesis and fake/header-only artifact acceptance. Some failures also predate this work. The migration ledger above records their current dispositions and replacements. Deferred/live specifications are unaccepted; no skip/xfail is used to claim a passing real pipeline.

## Operational behavior and remaining limitations

After a real, explicitly confirmed setup and validated source/script:

```text
real neural TTS
-> visual-plan (or visual-plan --regenerate after edits)
-> render --quality preview for Chapter 1
-> technical validation + review pack
-> actual human review
-> review-approve with the exact review-pack fingerprints
-> final Chapter 1 promotion / later chapter production
```

`visual-plan --regenerate` preserves the prior plan under a content-addressed stale filename. Existing authored plans are validated and preserved, rather than silently replaced with one shot per evidence panel. Changing reviewed Chapter 1 source, narration, voice, setup, plan, audio, thumbnail or BGM invalidates approval. Unrelated chapter registration/bookkeeping does not. A replaced Chapter 1 final also blocks approval; generating a changed preview preserves the old final under a content-addressed stale filename. Final Chapter 1 promotes the exact reviewed preview bytes at the configured target resolution; there is no unreviewed high-resolution reframing after approval.

Canonical production currently supports auto/landscape-fit framing. Unsupported portrait settings fail closed. Cloud per-chapter execution produces Chapter 1 preview or later chapters final. Chapter 1 final promotion is local after download/review; `render --kaggle` rejects a contradictory quality selection. Encoder bytes need not match; the source/shot/timeline contract must match.

The neural adapter uses the SDK's native `save` contract, validates that audio and then normalizes to PCM16 mono 48 kHz when needed. The native API is documented in the [upstream VieNeu README](https://github.com/pnnbao97/VieNeu-TTS/blob/main/README.md). Deterministic CI tests run without live neural synthesis (using isolated PCM fixtures to validate audio checks and rate conversion), while real neural SDK/model execution (`vieneu` 3.8.3 in `venv/bin/python`, Python 3.12.13) was live-verified for Chapter 1 G4 in [local_run.json](verification/v4_e2e_2026-11-11/local_run.json).

Per [AGENTS.md](../AGENTS.md), any new series or automation session without an available `ask_question` tool MUST still stop rather than fabricate setup or approval (`Incomplete/invalid/unavailable ask_question => STOP without starting jobs`). For the initial Chapter 1 E2E run ([docs/verification/v4_e2e_2026-11-11/](verification/v4_e2e_2026-11-11/README.md)), Q01–Q10 and human review were explicitly confirmed by `cuongnguyen1802` via `ask_question`; full P10–P15 lifecycle verification (HF private archive/restore, YouTube processed verification, receipt-gated cleanup, and canonical release state machine) and P8–P9 Dual-T4 benchmarking remain gated pending the exact-HEAD P0–P7 freeze.
