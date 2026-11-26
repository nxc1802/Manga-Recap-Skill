# V6.1 Stabilization — Phase S0 Baseline & Deletion Inventory

**Starting SHA:** `61a1705799f77e42627399ecff00f1f309dbbe13` (`main` / `origin/main`)  
**Date:** `2026-11-27`

## 1. Starting Verification & Size Baseline

| Metric | S0 Measured Value |
|---|---:|
| `src/` Python files | `101` |
| `src/` total bytes | `1,141,485` |
| `src/` non-blank LOC | `24,312` |
| `core/` (`24` files) | `298,643 B` (`6,241` LOC) |
| `cloud/` (`13` files) | `208,702 B` (`4,525` LOC) |
| `render/` (`8` files) | `136,951 B` (`2,792` LOC) |
| `tts/` (`6` files) | `114,236 B` (`2,679` LOC) |
| `cli/` (`21` files) | `89,042 B` (`1,660` LOC) |
| `ingest/` (`2` files) | `77,120 B` (`1,574` LOC) |
| `youtube/` (`4` files) | `70,045 B` (`1,460` LOC) |
| `providers/` (`5` files) | `51,175 B` (`1,176` LOC) |
| `.agents/skills/manga-recap/` (`6` files) | `24,523 B` (`221` LOC) |
| `tests/` Python files | `71` (`672,813 B`, `13,960` LOC) |
| `compileall -q src tests scripts` | `PASS` (exit 0) |
| `scripts/sync_requirements.py --check` | `PASS` (exit 0) |
| `pytest -q` | **`583 passed in 69.73s`** (`0` failed, `0` skipped) |

## 2. Retain / Consolidate / Move / Remove Inventory

| Category | Item / Module | S0 Decision | Rationale |
|---|---|---|---|
| **Kaggle Lifecycle** | `cloud/lifecycle.py`, `cloud/account_fallback.py`, `cloud/kaggle_service.py` | **Fix P0 (S1) → Retain** | `QUEUE_TIMEOUT`/`CLIENT_TIMEOUT` missing from active states; missing project-level reservation lock across chapter/series/chunks; overly broad `Exception`/dict/quota fallback |
| **Kaggle Runtime** | `cloud/scheduler.py`, `cloud/dual_gpu_worker.py` (`Policies C & D`) | **Consolidate (S3)** | Retain `Policy B` (`dual_tts_shared_encode`) default + `Policy A` (`baseline_split`) fallback; retire `Policy C`, `Policy D`, and `benchmark` production branches cleanly |
| **Kaggle Benchmark** | `src/cloud/benchmark.py` (`12,905 B`) | **Move to `scripts/devtools/` (S3)** | Offline/live ablation harness not needed in production runtime/Kaggle payload |
| **Decision Reapply** | `ingest/decision.py` (`--reapply-policy`, split/merge/cache) | **Fix P0 (S2) → Retain** | Offline reapply lacks full preflight, couples VCS contiguity with merge validity, can leak parent decision onto children |
| **Decision Runtime** | `ingest/decision.py` (`legacy_9preset`, `9preset_safe`, `4sided_with_motion`, `--decision-policy`) | **Remove runtime ablations (S4)** | Standardize on single canonical `4sided` adaptive policy; retain conservative full-image reader compatibility for legacy manifests/caches |
| **Core Facade** | `src/core/decision_engine.py` (`1,511 B`) + pass-through wrappers in `workflow.py` / `cli/` | **Audit & Consolidate (S4–S5)** | Inline trivial wrappers/aliases where no external contract requires indirection |
| **Migration / Compat** | `src/compat/migrate.py` (`11,931 B`), `src/core/config.py`, `src/core/execution_policy.py` | **Retain essential V3/V4 schema readers, trim dead helpers (S5)** | Keep `Q01–Q10` setup/schema migration & fail-closed gates intact while deduplicating wrappers |
| **Skill / Docs** | `SKILL.md`, `AGENTS.md`, `README.md`, `references/*` | **Deduplicate (S5)** | Single canonical 7-stage Skill flow, sync with simplified CLI/Policy B/4-sided defaults |
