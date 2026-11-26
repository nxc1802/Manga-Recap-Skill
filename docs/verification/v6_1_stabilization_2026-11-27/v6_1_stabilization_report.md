# V6.1 De-engineering & Stabilization Report (`61a1705` → V6.1)

**Date:** `2026-11-27`  
**Branch:** `main`  
**Baseline Rollback Point:** `61a1705799f77e42627399ecff00f1f309dbbe13` (`61a1705`)  
**Verified Code Commit (S1–S5):** `1d90132` (`HEAD` before final docs commit)

---

## 1. Executive Summary

V6.1 executes [V6_1_DEENGINEERING_STABILIZATION_PLAN.md](../../V6_1_DEENGINEERING_STABILIZATION_PLAN.md) (`S0`–`S6`) with **fix-P0-before-simplify** ordering and **zero new features or speculative abstractions**:

1. **S1 (Kaggle Lifecycle P0 Fixed):** Closed timeout duplicate-dispatch holes (`QUEUE_TIMEOUT` & `CLIENT_TIMEOUT` added to `ACTIVE_JOB_STATES`), enforced original persisted `account_name`/`username` binding during `reconcile`/`cancel`, serialized cross-scope chapter/series/chunk reservations via project-level `FileLock` (`cloud_dispatch.lock`), preserved unresolved status (`cancelled=False, resolved=False`) on failed/unsupported cancellation, and restricted account fallback strictly to safe pre-submit `auth_failed` / `submission_rejected` errors (never quota, timeout, `SUBMIT_UNKNOWN`, or arbitrary exceptions).
2. **S2 (Decision P0 Fixed):** Enforced strict offline `--reapply-policy` preflight (`sha256`, exact `(width, height)`, `cache_version`, non-empty `raw_answers` or `blocked` marker, and child-own Pass 2 entries) before any API/key lookup or file mutation; prevented parent-answer fallback onto `split_at`/`merge` children; ensured VCS contiguity never triggers merges without semantic verification; prevented missing edge/group confidence from passing guardrails; and made unchanged reruns byte-idempotent on `manifest.json` so valid Chapter 1 `preview_approval.json` stays intact.
3. **S3 (Kaggle Runtime Consolidated):** Retained **Policy B** (`dual_tts_shared_encode`) default + **Policy A** (`baseline_split`) fallback (`produce bs=16`, `tts bs=32`); retired **Policy C**, **Policy D**, and CLI/scheduler ablation branches; relocated `src/cloud/benchmark.py` out of runtime `src/` to `scripts/devtools/cloud_policy_benchmark.py`.
4. **S4 (Decision Runtime Simplified):** Standardized on canonical **`4sided`** adaptive (`medium` regular / `high` tall `H/W >= 2.2`, 0..40% guarded edge trims); retired `legacy_9preset`, `9preset_safe`, `4sided_with_motion`, unused `Policy E` crop-verify branches, and `--decision-policy`; preserved conservative full-image `Region()` fallback when reading legacy `legacy_9preset`/`center_crop_preset` metadata.
5. **S5 (Workflow & Docs Streamlined):** Relocated `compile_project_chapters` (`220` lines) from `src/core/workflow.py` to `src/render/chapter_renderer.py` (preserving public re-exports for compatibility), deduplicated panel-lookup/cleanup/final-path helpers in `workflow.py` and `tts_service.py`, removed stale `VOICE_ALIASES`, and synced `SKILL.md` (`6.1.0`) and `README.md` with V6.1 CLI contracts.

---

## 2. Quantitative V6 Baseline vs. V6.1 Metrics

Measured with the same non-blank line & byte inventory method at `61a1705` (S0) vs `1d90132` (after S1–S5):

| Metric | V6 Baseline (`61a1705`) | V6.1 (`1d90132`) | Delta |
|---|---:|---:|---:|
| `src/` Python files | `101` | `100` | `-1` file (`benchmark.py` moved to `scripts/devtools/`) |
| `src/` total bytes | `1,141,485 B` | `1,126,271 B` | **`-15,214 B` (`-1.33%`)** |
| `src/` non-blank LOC | `24,312` | `24,057` | **`-255 LOC`** (even after S1/S2 P0 guards added) |
| `src/core/workflow.py` | `91,981 B` (`1,935 LOC`) | `80,894 B` (`1,730 LOC`) | **`-11,087 B` (`-205 LOC`)** |
| `src/ingest/decision.py` | `77,052 B` (`1,573 LOC`) | `69,709 B` (`1,439 LOC`) | **`-7,343 B` (`-134 LOC`)** |
| `src/cloud/dual_gpu_worker.py` | `56,661 B` (`1,238 LOC`) | `52,640 B` (`1,159 LOC`) | **`-4,021 B` (`-79 LOC`)** |
| `src/cloud/` domain total | `13` files, `208,702 B` (`4,525 LOC`) | `12` files, `201,031 B` (`4,387 LOC`) | `-1` file, `-7,671 B` (`-138 LOC`) |
| `src/render/` domain total | `8` files, `136,951 B` (`2,792 LOC`) | `8` files, `148,576 B` (`3,028 LOC`) | `+11,625 B` (owns `compile_project_chapters`) |
| `.agents/skills/manga-recap/` (`SKILL.md`) | `6` files, `24,523 B` (`9,831 B`) | `6` files, `24,488 B` (`9,796 B`) | `-35 B` (`v6.1.0` synced) |
| `tests/` Python files | `71` (`672,813 B`, `13,960 LOC`) | `70` (`683,663 B`, `14,115 LOC`) | `+S1/S2 fault-injection`, `-A–D bench test` |
| Full `pytest -q` | `583 passed in 69.73s` (`0` fail/skip) | **`585 passed in 57.38s`** (`0` fail/skip) | **`+2 tests`, `-12.35s` (`-17.7%`)** |

---

## 3. Architecture Simplification & Retain/Remove Summary

- **Retained Unchanged Contracts:** 8 human setup questions (`Q01–Q05, Q08–Q10`), confirmed single narrator (`voice_selection.json`), Chapter 1 `preview.mp4` + `approve` fingerprint binding, closed-set `ACTIVE`/`CONTEXT_ONLY`/`SKIP`/`NEEDS_REVIEW` + `[CAN_XAC_MINH: ...]` uncertainty gates, FFmpeg hardware/software rendering parity, Kaggle Policy B (`dual_tts_shared_encode`) & Policy A (`baseline_split`) with `bs=16`/`bs=32`, Hugging Face archive verification, dry-run `clean` / `--purge-raw` gates, and V3/V4/V5/V6 artifact schema compatibility (`compat/migrate.py`).
- **Removed / Relocated Complexity:** Policies `C` & `D`, `src/cloud/benchmark.py` (moved to `scripts/devtools/cloud_policy_benchmark.py` for Policy B vs A reference), `tests/providers/test_kaggle_policy_benchmark.py`, Decision `legacy_9preset`/`9preset_safe`/`4sided_with_motion`/Policy `E` crop-verify branches, `--decision-policy`, broad pre-submit fallback retries on `Exception`/falsy dicts/quotas/timeouts, duplicated workflow artifact resets & path probing, and stale `VOICE_ALIASES` duplication in `tts_service.py`.

---

## 4. Verification & Offline Benchmark Evidence (`1d90132`)

| Check | Command / Scope | Observed Result |
|---|---|---|
| Bytecode & dependency sync | `.venv/bin/python -m compileall -q src tests scripts && .venv/bin/python scripts/sync_requirements.py --check` | **PASS** (exit `0`) |
| Full test suite | `.venv/bin/python -m pytest -q` (includes all S1 lifecycle locks/reconcile/fallback, S2 offline reapply/child isolation/merge verify, S3 scheduler/worker/payload preflight, S4 4sided/legacy fallback, media parity, adversarial, integration) | **`585 passed in 57.38s`** (`0` failed, `0` skipped) |
| CLI contract & error JSON | `.venv/bin/manga-recap --help` & `status --project __nonexistent_s6__ --json` | Help exit `0`; error exit `3` (`IO_ERROR` valid JSON envelope) |
| Offline Decision reapply & render parity (isolated `/tmp` fixture, `MockTransport`) | 3-panel `800x1000`/`800x2200` initial `4sided` (`60.67 ms`) → `--reapply-policy` (`0.75 ms`, `requests={}`, `manifest_byte_idempotent=True`, `static_shot0_box=0.1..0.9`, tall `motion=slow_pan_down`, rendered center pixel `[20, 60, 220]`) | **PASS** (`0` network calls offline) |
| Offline Dual-T4 simulator (`scripts/devtools/cloud_policy_benchmark.py`, `repeats=5`) | `short_mixed` · `tts_heavy` · `render_heavy` (`Policy B` vs `Policy A`) | Policy B fastest across all 3: **`1.614x`** (`0.214s` vs `0.345s`), **`1.911x`** (`0.395s` vs `0.755s`), **`1.102x`** (`0.536s` vs `0.590s`) |

### Honest Verification Boundaries (Live vs. Offline)

- **Verified offline in V6.1:** All 585 pytest unit/integration/adversarial tests, mocked Kaggle multi-account fault injection, isolated FFmpeg render parity (`test_kaggle_media_parity.py`), Decision `--reapply-policy` / merge / child-cache tests (`MockTransport`), and simulated Dual-T4 timelines.
- **Not re-executed live against external cloud endpoints during V6.1:** Live Kaggle Dual-T4 sessions, live Gemini `gemini-decision-1` API calls, and live YouTube/Hugging Face uploads were **not re-run** during V6.1 stabilization (historical pre-V6.1 live baseline reports remain linked from [README.md](../../../README.md)). No project files in `data/projects/` were mutated during V6.1 verification.

---

## 5. Commit History & Rollback Reference

- **Pre-V6.1 baseline commit:** `61a1705` (reset point if rollback is ever required: `git reset --hard 61a1705`)
- `92512a3` — `fix(cloud): enforce Kaggle lifecycle locks, account binding, and pre-submit-only fallback` (**S0+S1**)
- `0d81fd3` — `fix(decision): enforce offline reapply preflight, child cache isolation, and verified merges` (**S2**)
- `06954cf` — `refactor(cloud): consolidate Kaggle runtime to Policy B default and Policy A fallback` (**S3**)
- `16a734b` — `refactor(decision): simplify Decision runtime to canonical 4sided adaptive policy` (**S4**)
- `1d90132` — `refactor(core): streamline workflow, move feature compiler to render, and sync V6.1 docs` (**S5**)
