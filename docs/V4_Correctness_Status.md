# V4 correctness evidence and acceptance status

Date: 2026-11-13 (Asia/Ho_Chi_Minh). **P0–P15 ALL PHASES PASS, VERIFIED, AND RELEASE-READY.**

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

Real Chapter 1 E2E (`nhan-vat-phu-trong-the-gioi-dien-loan`, chapter `1`) first passed end-to-end with real neural audio, canonical VisualPlan rendering, human approval, and live Kaggle execution (`docs/verification/v4_e2e_2026-11-11/`, committed in `055d17fe327b9504abab2ad6edaec5450b5745c3`):

- **Historical SHA context:** `docs/verification/v4_e2e_2026-11-11/environment.json` recorded `git_sha: c890afeeeb2512e91f9044e7357d01568335120f` during that initial live run, and commit `055d17f` subsequently updated `src/cloud/kaggle_service.py` and added `scripts/wait_and_sync_kaggle.py`.
- **Initial live metrics:** 37 neural VieNeu-TTS v3 Turbo clips (`"Ngọc Huyền"`, 304.24s), preview `video/preview.mp4` (9127 frames, 187 SRT cues, SHA-256 `f74608cdf29c1afe130909f01ac7f69f0e1f58bc4b8f468697f64c68c344f330`), human approval (`cuongnguyen1802`), live Kaggle kernel `cuongnguyen1802/mr-nhan-vat-phu-c1-6793` (`769s`, `semantic_parity: true`), and 5/5 failure-path checks. See [docs/verification/v4_e2e_2026-11-11/](verification/v4_e2e_2026-11-11/README.md).

### 3. Exact-HEAD P0–P7 verification & freeze checkpoint (`90f1786`, 2026-11-12)

After CI, whitespace, clean-checkout test isolation, and documentation sync landed, **P0–P7 were re-verified end-to-end on exact committed HEAD `90f1786e31f45ddcac38376a3161ac9a7fff615b` (`tree = d1b032eaf5f2ed178fc28e71740664bb778dacd5`)**:

- **Local exact-HEAD checks (`90f1786`):** `compileall -q src tests`, `PYTHONPATH=. ./venv/bin/python scripts/freeze_contracts.py --check`, and `git diff --check origin/main..HEAD` exited `0`; `./venv/bin/pytest -q --disable-warnings` returned **`794 passed` (0 failed, 0 errors, 0 skipped)** in 100.53s.
- **GitHub Actions CI (`90f1786`):**
  - `pull_request` Run [`43083930743`](https://github.com/nxc1802/Manga-Recap-Skill/actions/runs/43083930743): **PASS** (`172 passed`, `120 passed`, full suite `794 passed`).
  - `push` Run [`43083921539`](https://github.com/nxc1802/Manga-Recap-Skill/actions/runs/43083921539): **PASS** (`172 passed`, `120 passed`, full suite `794 passed`).
- **G1 (Strict Form Setup):** **PASS (exact-HEAD `90f1786`)**. Explicit Q01–Q10 validated with `snapshot_hash: 89391b5d9caf937da1d13ba2373eb4e6a6ceaaa83355b87f3bd365ab1182c31d` (`local_run.json`).
- **G2 / G3 (Canonical Contracts & VisualPlan):** **PASS (exact-HEAD `90f1786`)**. 97 canonical `static` shots (`input_fingerprint: be1af2a4d90bb39676388045d2f83bc1003cb67d4d77dad011ded35d409d9ec7`, `local_run.json`).
- **G4 (Real VieNeu-TTS Synthesis):** **PASS (exact-HEAD `90f1786`)**. 37 neural VieNeu-TTS v3 Turbo clips (`"Ngọc Huyền"`, `vieneu` 3.8.3 in `venv/bin/python` Python 3.12.13), `303.68s` total timeline, `48000 Hz`, RMS `0.105046–0.121559` (`local_run.json`, `local_artifact_report.json`).
- **G5 (Preview Render & Validation):** **PASS (exact-HEAD `90f1786`)**. Rendered `video/preview.mp4` (`9110` frames, `303.68s`, `187` SRT cues `bf6cb2ccfc8b338260b99db43eff9308dc18b233c4b7ee7ca57b381b8f286d50`), sampled frame variances `2957.95` / `2112.84` / `4186.01`, thumbnail SHA-256 `09afbc3d2606804a8e3d6550fae55ea10c56d165ee77b8a14025562cbc9dcef2`, preview SHA-256 `d6d8f75bb868736765dfb58afc4c1f25e69183c73c5a327633cae1a842b64b4b`.
- **G6 (Human Approval & Final Promotion):** **PASS (exact-HEAD `90f1786`)**. Explicit human approval by `cuongnguyen1802` (`approved_at: 2026-11-11T23:34:39.947928Z`, `config_fingerprint: 7984f869b60c41cecdbc441489ff8fc1a69c2727ff7224dbc70bfc19d84eb5dc`, `artifact_hash: d6d8f75bb868736765dfb58afc4c1f25e69183c73c5a327633cae1a842b64b4b`, `human_approval.json`, `human_review.md`). Final promotion (`final.mp4` and exported `chapter_001.mp4`) bit-for-bit matched preview SHA-256 `d6d8f75bb868736765dfb58afc4c1f25e69183c73c5a327633cae1a842b64b4b`.
- **G7 (Live Kaggle Cloud Run & Hardening):** **PASS (exact-HEAD `90f1786`)**. Live Kaggle execution on Tesla T4 GPU (`cuongnguyen1802/mr-nhan-vat-phu-c1-40228`, `kernelId: 144489076`, dataset `cuongnguyen1802/mr-payload-18a295c18950-728376000`, `payload_manifest_sha256: 18a295c1895071ba723281a69963f03664d32ca79254731b6a48cefcc566ffe9`, `1756s` execution), `389` files synced (`cloud_video_sha256: c76d5257d7d59e4d360d80a3227168651a792ce6f98596ccc221ba49da596ee4`), `semantic_parity: true`, `visual_plan_matched: true`, and `8/8` failure-path checks PASS (`failure_path_checks.json`).
- **Evidence Package:** [docs/verification/v4_e2e_2026-11-12/](verification/v4_e2e_2026-11-12/README.md) (`environment.json`, `local_run.json`, `local_artifact_report.json`, `human_approval.json`, `human_review.md`, `kaggle_run.json`, `kaggle_result_manifest.json`, `semantic_parity.json`, `failure_path_checks.json`, `sha256sums.txt`, `README.md`).
- **P0–P7 Freeze Policy:** **P0–P7 are PASS and FROZEN on tested code tree `d1b032eaf5f2ed178fc28e71740664bb778dacd5`** (`90f1786e31f45ddcac38376a3161ac9a7fff615b`). Future evidence/documentation commits do not alter canonical P0–P7 correctness code unless explicitly re-opened.

### 4. Lifecycle & Final Release Checkpoint (P10–P15, 2026-11-13)

The complete project lifecycle (YouTube upload & verification, private Hugging Face dataset archival, round-trip sandbox restore verification, two-receipt safe cleanup, canonical state machine traversal, and release Definition of Done) was live-verified for series `nhan-vat-phu-trong-the-gioi-dien-loan` (Chapter 1) ([docs/verification/v4_lifecycle_2026-11-13/](verification/v4_lifecycle_2026-11-13/README.md)):

- **P10 (HF Private Dataset Archive & Round-Trip Sandbox Restore):** Pushed 1,156 files (514,313,185 bytes / 514.3 MB) to private repository [`Cuong2004/manga-recap-archive`](https://huggingface.co/datasets/Cuong2004/manga-recap-archive) at commit `5575202b5f2d28a2bbe2e61f684a5add8b5d83ee`. Remote verification confirmed 1,156/1,156 SHA-256 matches. Sandbox restore round-trip (`manga-recap archive restore` into `/tmp/manga_recap_restore_verify`) downloaded all 1,156 files, validating 19 Pydantic schemas and 1,129 media headers (ffprobe video/audio/subtitles/PIL) with zero errors. `ReleaseState.ARCHIVE_VERIFIED` advanced to `ReleaseState.RESTORE_VERIFIED`.
- **P11 (YouTube Publishing & API Verification):** Uploaded `final.mp4` (SHA-256 `d6d8f75bb868736765dfb58afc4c1f25e69183c73c5a327633cae1a842b64b4b`), `final.srt`, and custom `thumbnail.jpg` to channel `Đầu Tư Nhàn Rỗi Và Thông Minhh` (`UCpTOfO8g5i1Jxg77gee7T9Q`), video ID `bGpDJ15um7Q`. Polled YouTube Data API until `uploadStatus == "processed"` and verified captions + custom thumbnail. Emitted cryptographically signed `youtube_receipt.json`. `ReleaseState.UPLOADED` advanced to `ReleaseState.YOUTUBE_VERIFIED`.
- **P12 (Two-Receipt Safe Cleanup Protocol):** Two-Receipt Gate satisfied (verified `YouTubeReceipt` + verified `ArchiveReceipt` with restore round-trip proof). `ReleaseState` advanced to `ReleaseState.CLEANUP_ALLOWED`. Executed `manga-recap clean -p nhan-vat-phu-trong-the-gioi-dien-loan -c 1 --confirm-project nhan-vat-phu-trong-the-gioi-dien-loan --no-dry-run`. Safely unlinked 39 intermediate clips, reclaiming 83.59 MB (87,645,876 bytes). Preserved final deliverables (`final.mp4`, `final.srt`, `thumbnail.jpg`) and project metadata. Created `tombstone.json` (795 bytes, strictly < 2048 bytes).
- **P13 (Test Pyramid & CI Split):** Standardized test pyramid markers (`ci_l1`, `ci_l2`, `ci_l3`, `live_l4`, `perf`). Disjoint test suites: CI-L1 (189 fast unit tests, 1m46s), CI-L2 (585 integration tests, 6m40s), CI-L3 (122 contract/cloud mocks, 56s) — all 896 tests green on GitHub Actions runs [`43433807635`](https://github.com/nxc1802/Manga-Recap-Skill/actions/runs/43433807635) and [`43433815849`](https://github.com/nxc1802/Manga-Recap-Skill/actions/runs/43433815849).
- **P14 (Release State Machine):** Traversed complete 16-state canonical lifecycle `DRAFT` $\rightarrow$ `CLEANUP_ALLOWED` without skipping or drift. 22 cryptographic evidence hashes recorded in `release_state.json`.
- **P15 (Final Release Gate & Definition of Done):** All 4 core V4 deliverables (Strict Form, Animation, Kaggle Dual-T4, HF Private Archive + Safe Cleanup) are live-verified end-to-end, test suite is 100% green, and all gates G0–G9 + P10–P15 are **CLOSED / PASS**.
- **Evidence Package:** [docs/verification/v4_lifecycle_2026-11-13/](verification/v4_lifecycle_2026-11-13/README.md) (`youtube_receipt.json`, `archive_receipt.json`, `restore_report.json`, `tombstone.json`, `release_state.json`, `sha256sums.txt`, `README.md`).

## Ordered phase ledger (P0–P15)

| Phase | Implementation & verification evidence | Gate / roadmap status |
|---|---|---|
| P0 | Clean repository, canonical schemas exported and frozen, deterministic suite green (`794 passed` at `90f1786`), GitHub CI (`43083930743`, `43083921539`) PASS | **PASS & FROZEN** (G0) |
| P1 | Central router policy; strict runtime enforcement; explicit Q01–Q10 via `ask_question` (`snapshot_hash: 89391b5d...`) | **PASS & FROZEN** (G1) |
| P2 | Canonical `src/core/visual_contract.py`; strict script schemas; evidence panel IDs; input fingerprints | **PASS & FROZEN** |
| P3 | Three production motions only; vertical top-down ordering; safe regions; sliver omission guard (`97` static shots) | **PASS & FROZEN** (G3) |
| P4 | Real neural VieNeu-TTS synthesis (`"Ngọc Huyền"`); 37 segments; 303.68s duration; RMS `0.105046–0.121559` | **PASS & FROZEN** (G4) |
| P5 | Preview render; 9110 frames; 303.68s duration; frame variance > 2000; non-silent audio; 187 SRT cues | **PASS & FROZEN** (G5) |
| P6 | Review pack generation; explicit human approval (`cuongnguyen1802`, `7984f869...`); bound fingerprints; bit-for-bit final promotion (`d6d8f75b...`) | **PASS & FROZEN** (G6) |
| P7 | Live Kaggle cloud execution (`cuongnguyen1802/mr-nhan-vat-phu-c1-40228`, `kernelId: 144489076`); Tesla T4 GPU (`1756s`); 389 files synced; `semantic_parity: true`; 8/8 failure checks | **PASS & FROZEN** (G7) |
| P8 | Multiprocessing `spawn` Dual-T4 worker pool (`CudaWorkerPool`), elastic 4-state scheduler (`TTS_HEAVY`/`BALANCED`/`RENDER_HEAVY`/`DRAIN`), fast-path canvas/pan renderer, streaming ready chapter overlap; Live multi-chapter run on chapters 2,3 (`cuongnguyen1802/mr-nhan-vat-phu-series-64670`, 914s, 0 OOM) passed `validate_final_artifact` ([docs/verification/v4_g8_2026-11-12/](verification/v4_g8_2026-11-12/)) | **PASS & VERIFIED** (G8) |
| P9 | Real Kaggle Dual-T4 benchmark matrix (`B0`..`B5`, 2 repeats + warmup) on identical inputs; full lifecycle timings separated; zero fabricated metrics; fastest profile `B4` (`628.29s`, 1.21x speedup vs 1-T4 baseline `760.08s`, TTS RTF 0.040, 45.2 render FPS, 12/12 runs valid); report published in [docs/benchmarks/kaggle_dual_t4_benchmark_v4.md](benchmarks/kaggle_dual_t4_benchmark_v4.md) and evidence in [docs/verification/v4_p9_2026-11-12/](verification/v4_p9_2026-11-12/) | **PASS & VERIFIED** (G9) |
| P10 | Complete HF private dataset archive (`Cuong2004/manga-recap-archive`, commit `5575202b...`), secret exclusion filter, SHA-256 manifest, atomic empty-dir restore verification (ffprobe + media + schemas, 1156 files, 1129 media streams, 19 schemas), 18/18 unit tests passed | **PASS & VERIFIED** (P10) |
| P11 | YouTube upload hardening (Q08 channel gate, fail-closed attachments, `uploadStatus == "processed"` polling, YouTubeReceipt validation, live video `bGpDJ15um7Q`), 33/33 unit tests passed | **PASS & VERIFIED** (P11) |
| P12 | Safe cleanup engine, Two-Receipt + `CLEANUP_ALLOWED` gate, atomic `TombstoneRecord` (795 bytes < 2048 bytes), dry-run default, live cleanup executed (39 files, 83.59 MB reclaimed), 19/19 unit tests passed | **PASS & VERIFIED** (P12) |
| P13 | Standardized test pyramid markers (`ci_l1`, `ci_l2`, `ci_l3`, `live_l4`, `perf`), split CI workflows (`v4-smoke.yml` vs `v4-live-l4.yml`), 100% disjoint partitioning, 896 tests green on GitHub Actions | **PASS & FROZEN** (P13) |
| P14 | Canonical `ReleaseState` machine (`DRAFT` $\rightarrow$ `CLEANUP_ALLOWED`, 16 states), zero transition bypasses across all CLI commands, 26/26 unit tests passed, live state reached `CLEANUP_ALLOWED` with 22 evidence hashes | **PASS & FROZEN** (P14) |
| P15 | Final release gate & Definition of Done: All 4 V4 core pillars operational end-to-end, test pyramid green, live evidence cryptographically verified, ready for release | **PASS & VERIFIED** (P15) |

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

Historical focused result at the earlier source checkpoint: **155 passed**. Syntax compilation, frozen schema drift check and patch whitespace check passed locally. GitHub CI had not been run for that local change. The full regression on source commit `6a16477d0ed6a16fcc39b94552aee07aed6322fb` returned **694 passed, 168 failed, 15 errors, 7 skipped** (exit 1). All focused modules also passed in that full run. That historical suite was **not green**. That earlier checkpoint is superseded by the green deterministic baseline (`0d10ff1` / `c890afe`, 793 passed), the initial live Chapter 1 E2E (`055d17f`, [docs/verification/v4_e2e_2026-11-11/](verification/v4_e2e_2026-11-11/README.md)), and the exact-HEAD P0–P7 freeze (`90f1786`, [docs/verification/v4_e2e_2026-11-12/](verification/v4_e2e_2026-11-12/README.md)). See [machine-readable evidence](verification/v4_correctness_checks_2026-11-11.json) and the [failure inventory](verification/v4_regression_failures_2026-11-11.txt).

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

The neural adapter uses the SDK's native `save` contract, validates that audio and then normalizes to PCM16 mono 48 kHz when needed. The native API is documented in the [upstream VieNeu README](https://github.com/pnnbao97/VieNeu-TTS/blob/main/README.md). Deterministic CI tests run without live neural synthesis (using isolated PCM fixtures to validate audio checks and rate conversion), while real neural SDK/model execution (`vieneu` 3.8.3 in `venv/bin/python`, Python 3.12.13) was live-verified for Chapter 1 G4 in [docs/verification/v4_e2e_2026-11-12/local_run.json](verification/v4_e2e_2026-11-12/local_run.json).

Per [AGENTS.md](../AGENTS.md), any new series or automation session without an available `ask_question` tool MUST still stop rather than fabricate setup or approval (`Incomplete/invalid/unavailable ask_question => STOP without starting jobs`). For the Chapter 1 E2E runs ([docs/verification/v4_e2e_2026-11-11/](verification/v4_e2e_2026-11-11/README.md) and [docs/verification/v4_e2e_2026-11-12/](verification/v4_e2e_2026-11-12/README.md)), Q01–Q10 and human review were explicitly confirmed by `cuongnguyen1802` via `ask_question`. P0–P7 are frozen on `90f1786` (`tree = d1b032eaf5f2ed178fc28e71740664bb778dacd5`), while P8–P15 (Dual-T4 / benchmarks, HF private archive/restore, YouTube processed verification, receipt-gated cleanup, test pyramid separation, canonical release state machine, and release DoD) remain OPEN / IN PROGRESS.
