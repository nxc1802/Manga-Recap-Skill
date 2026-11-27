# V7.1 — End-to-End Production Readiness Report

**Repository:** `nxc1802/Manga-Recap-Skill` (`main`)  
**Plan:** [`docs/V7_1_PRODUCTION_READINESS_PLAN.md`](../../V7_1_PRODUCTION_READINESS_PLAN.md)  
**Baseline SHA (V7):** `4bd550eef9f3405c3f6cd250337edb736a8bfc4f`  
**Release Candidate Code SHA:** `5d8ca077a4517d76395d22e19abfb8a39eb0c85b`  
**Audit Date:** 2026-11-27  
**Environment:** `macOS-26.6-arm64` (Apple Silicon), Python `3.13.14`, `manga-recap 7.1.0`, FFmpeg (`libx264`, `h264_videotoolbox`; no local `h264_nvenc`), Kaggle Dual Tesla T4 (`CUDA 13.0`, `PyTorch 2.11.0+cu130`, `VieNeu 3.8.5`, `FFmpeg 6.1.1` dual `h264_nvenc`)  

---

## 1. Release Gate Verdict: **`NOT READY`** (Core Pipeline Verified; Blocked on External Credentials & Long-Series Live Run)

All core Skill stages (**P0–P6**, **P7.1 Local 2-ch**, **P7.2 Kaggle Dual-T4 2-ch + Standalone TTS/Render + Verified Chunk Resume**, **P8.1 Final Media/Thumbnails**, **P8.4 Explicit Cleanup Safety**) **PASSED LIVE** on fresh isolated projects (`v71-accept-local` and `v71-accept-kaggle`) without touching any pre-existing user project in `data/projects/*`.

However, per Section P9 of the V7.1 Plan, **READY** requires every mandatory external gate to pass on the final candidate. Three gates remain blocked or unexecuted:

1. **`P8.2` YouTube Live Upload/Verify (`BLOCKED`):** `cho_review` OAuth refresh token expired (`YouTube token refresh failed for cho_review`); requires interactive OAuth re-authorization (`manga-recap youtube accounts --account-action add --account cho_review`). Note: `.[youtube]` extras were installed cleanly (`manga-recap==7.1.0`).
2. **`P8.3` Hugging Face Private Archive Live Push/Verify/Restore (`BLOCKED`):** `HF_TOKEN` in `.env` returned HTTP `401 Unauthorized` (`Invalid username or password`) from `https://huggingface.co/api/repos/create`; requires refreshing `HF_TOKEN`.
3. **`P7.2` Representative 5-Chapter & 10–20-Chapter Live Multi-Chunk Series (`NOT RUN`):** Live Kaggle Dual-T4 Policy B was verified end-to-end on a 2-chapter series (single chunk + verified resume `skipped_verified=["1","2"], dispatched_chunks=[]`) plus standalone remote TTS (`bs=32`) and standalone remote Render (`dual h264_nvenc`, zero TTS init). Multi-chunk 5-ch / 10–20-ch live Kaggle workloads were not executed in this pass.

---

## 2. Phase-by-Phase Execution Summary

| Phase | Gate / Stage | Status | Evidence Summary |
| --- | --- | --- | --- |
| **P0** | Baseline & Known Correctness Fixes (`BUG-01..BUG-08` + live `BUG-09`) | **PASS** | Commits `4acfc6d` & `5d8ca07`; `572 passed` (re-verified post-docs); `compileall` & `sync_requirements.py --check` exit `0`; `manga-recap --version` -> `7.1.0` |
| **P1** | Stage 1: Series Setup (`SET-01..SET-08`) | **PASS** | Explicit user answers recorded via `setup record` + `voice_selection.json` (`Ngọc Huyền`, `1.0x`, subtitles `ON`, `720p` Local / `1080p` Kaggle, no BGM, pinned `existing` / `cho_review`) |
| **P2** | Stage 2: Prepare & Live Gemini Decision (`PRE-01..PRE-13`) | **PASS** | Live TruyenQQ crawl (`202` + `141` raw pages) + local import (`PRE-02`) + warm-cache `0` API requests without key (`PRE-10`) + manual `repack` (`PRE-12`). See [`decision_quality_report.md`](decision_quality_report.md) |
| **P3** | Stage 3: Author Chapter 1 (`SCR-01..SCR-09`) | **PASS** | Full contact-sheet & high-res panel inspection (`24` segments / `79` active panels, `230/230` checks, `0` errors); `characters.json` & `story_summary.md` maintained |
| **P4** | Stage 4: Real Chapter 1 Previews (Local & Kaggle) | **PASS** | Real Local `720p` (`90.38s`, `188.40s` audio, `decode_rc=0`) & real Kaggle Dual-T4 `1080p` (TTS `99s` + Render `185s` kernel / `217.27s` e2e, `187.84s` audio, `decode_rc=0`). See [`media_quality_report.md`](media_quality_report.md) |
| **P5** | Stage 5: Human Approval & Negative Gates (`APR-01..APR-06`) | **PASS** | `APR-01` blocked pre-approval (`rc=2`); genuine human approval recorded (`user`); `APR-03..05` script/preview/voice mutations blocked; `APR-06` exact SHA-256 reuse verified on both Local & Kaggle |
| **P6** | Stage 6: Prepare & Author Chapter 2 | **PASS** | Live Ch. 2 `prepare` (`structural_staged`: 2 splits, 3 merges, 7 Pass 2 children) + `C04` fail-closed caught 2 `blank_band` transition strips before compute + `validate --pacing` (`valid: true`) |
| **P7.1** | Stage 7: Local Full Production & Compile (Ch. 1–2) | **PASS** | `53.16s` total; Ch. 1 exact promotion `0.073s`; Ch. 2 `49.33s`; compiled `720p` feature `327.26s` + `48kHz` master WAV `327.34s`, all `decode_rc=0` |
| **P7.2** | Stage 7: Kaggle Live Dual-T4 Policy B & Standalone Modes | **PARTIAL (`NOT RUN` on 5/10–20 ch)** | 2-ch Policy B `produce --compile` (`156.93s` worker makespan / `293.76s` e2e), standalone TTS (`99s`), standalone Render (`185s`), verified chunk resume (`4.99s`, `0` jobs) **PASS**. 5-ch & 10–20-ch multi-chunk live runs **NOT RUN**. See [`kaggle_benchmark_report.md`](kaggle_benchmark_report.md) |
| **P8.1** | Stage 8: Final Media, 16:9 Thumbnails & Metadata | **PASS** | `youtube_metadata.json` generated (`7` timestamps, `8` tags); clean `1280x720` thumbnails verified |
| **P8.2** | Stage 8: YouTube Live Private Upload & Verify | **BLOCKED** | Expired OAuth refresh token for `cho_review` (`exit_code=4`) |
| **P8.3** | Stage 8: Hugging Face Live Push/Verify/Restore | **BLOCKED** | HTTP `401 Unauthorized` on `HF_TOKEN` (`exit_code=3`) |
| **P8.4** | Explicit Cleanup Safety | **PASS** | `--dry-run` `rc=0` (0 deletions); missing `--confirm-project` blocked (`rc=2`); `--purge-raw` without verified `archive_receipt.json` blocked (`rc=3`) |

---

## 3. Defects Reproduced, Fixed & Verified

All runtime code additions were strictly bounded to reproducible correctness defects (`+2,200 B` / `+41` non-blank LOC in `src/`):

| Bug ID | Root Cause | Minimal Fix | Commit | Regression Test |
| --- | --- | --- | --- | --- |
| **BUG-01 (P0)** | `src/cloud/jobs.py:write_cloud_job_state` swallowed persistence exceptions & missing parent dirs | Propagate `FileNotFoundError` / write exceptions; anchor `chapter` path to resolved `project` in `kaggle_service.py` | `4acfc6d` | `test_bug01_write_cloud_job_state_propagates_write_and_missing_dir_errors` |
| **BUG-02 (P0)** | `src/cloud/lifecycle.py` checked membership in `ACTIVE_JOB_STATES` (fail-open on unrecognized enums like `INITIALIZING`/`UNKNOWN`) | Fail-closed against explicit `TERMINAL_LOCAL_STATES` (`SYNCED`, `FAILED`, `CANCELLED`…); map unfamiliar remote enums to `SUBMIT_UNKNOWN` | `4acfc6d` | `test_bug02_unrecognized_or_unresolved_status_blocks_duplicate_submit`, `test_bug02_reconcile_unfamiliar_remote_enum_stays_submit_unknown`, `test_bug02_confirmed_terminal_failure_allows_retry` |
| **BUG-03 (P1)** | `src/core/workflow.py:produce_chapters` called `dispatch_series_job`, bypassing the automatic Chunk Planner | Route canonical Kaggle `produce` through `service.dispatch_series_chunks` | `4acfc6d` | `test_bug03_canonical_produce_routes_kaggle_via_chunk_planner` |
| **BUG-04 (P1)** | `preview`/`produce` exposed `--kaggle` overrides and `preview_chapter` defaulted `kaggle=False` instead of reading `Q02` | Derive `use_kaggle` strictly from confirmed `Q02` in `preview_chapter` & `produce_chapters`; remove `--kaggle` from `preview`/`produce` while keeping granular `tts`/`render --kaggle` | `4acfc6d` | `test_bug04_preview_uses_confirmed_q02_backend`, `test_bug03_and_bug04_cli_rejects_kaggle_override_on_preview_and_produce` |
| **BUG-05 (P1)** | `src/ingest/decision.py` resolved `GEMINI_API_KEY` eagerly only on Pass 1 miss | Lazy `_get_api_key()` invoked only when Pass 1, split, merge, or Pass 2 actually makes a remote call | `4acfc6d` | `test_bug05_lazy_api_key_resolves_on_later_phase_miss_and_skips_on_warm_cache` |
| **BUG-06** | `src/core/panel_repack.py` metadata-only & shifted `KEEP` branches overwrote `repack_op` and `source_raw_panels`/`source_raw_sha256s` | Preserve existing structural lineage & `repack_op` in `panel_repack.py` and resolve self-lineage cleanly in `decision.py:_resolve_post_repack_record` | `4acfc6d` | `test_bug06_metadata_only_repack_preserves_child_lineage` + `test_v7_panel_identity_after_repack_and_cache_integrity` |
| **BUG-07** | `SKILL.md` had 6 stages and both `SKILL.md` & `README.md` cited unsupported `publish --wait` | Aligned to exact 8 canonical stages + separate optional archive/cleanup; replaced `--wait` with `--action verify` | `4acfc6d` | Verified against CLI `--help` |
| **BUG-08** | `APP_VERSION` / `pyproject.toml` reported `5.0.0` vs `SKILL.md` `7.0.0` | Aligned `APP_VERSION`, `pyproject.toml`, and `SKILL.md` to `7.1.0` without changing persisted schema versions (`3.0.0`/`4.0.0`/`4.1.0`/`5.0.0`) | `4acfc6d` | `test_bug08_cli_version_matches_skill_and_pyproject` |
| **BUG-09 (Live P4)** | `sync_outputs` marked `StageType.TTS` `COMPLETED`, then `synthesize_chapter_tts(..., kaggle=True)` attempted a duplicate `completed -> completed` transition | Check current `TTS` checkpoint status before updating to `COMPLETED` and mark `FAILED` on dispatch exception in `src/core/workflow.py` | `5d8ca07` | `test_bug09_kaggle_tts_does_not_fail_when_sync_already_completed_checkpoint` |

---

## 4. Runtime Footprint Delta (`4bd550e` → `5d8ca07`)

| Scope (`.py`) | Baseline `4bd550e` | Candidate `5d8ca07` | Delta |
| --- | ---: | ---: | ---: |
| `src/` | 93 files · `998,175 B` · `21,507` LOC | 93 files · `1,000,375 B` · `21,548` LOC | `+2,200 B` (`+41` LOC) |
| `tests/` | 70 files · `686,250 B` · `14,138` LOC | 71 files · `705,972 B` · `14,564` LOC | `+19,722 B` (`+426` LOC) |
| `scripts/` | 13 files · `131,732 B` · `2,754` LOC | 13 files · `131,732 B` · `2,754` LOC | `0` |

Added file: `tests/core/test_v7_1_p0_regressions.py`. Zero new runtime modules, policies, or fallbacks added.

---

## 5. Actions Required to Certify **READY** & Rollback Guide

1. **Refresh Hugging Face token:** Update `HF_TOKEN` in `.env` and re-run `manga-recap archive push/verify -p v71-accept-kaggle --json` + `scripts/restore_archive.py` hash verification.
2. **Re-authorize YouTube channel:** Run `manga-recap youtube accounts --account-action add --account cho_review`, then execute `publish` (`-c 1` & `-c feature`, `--privacy private`) + `--action verify` + duplicate re-run check.
3. **Execute long-series Kaggle live workload:** Run a 5-chapter and 10–20-chapter multi-chunk `produce --compile` on Kaggle Dual-T4 to complete P7.2 long-series certification.
4. **Non-Destructive Rollback:** Pre-V7.1 baseline is `4bd550eef9f3405c3f6cd250337edb736a8bfc4f`; rollback cleanly via `git checkout -b rollback/pre-v7.1 4bd550eef9f3405c3f6cd250337edb736a8bfc4f` or `git revert 4bd550e..HEAD`. Acceptance projects (`data/projects/v71-accept-*`, `output/v71-accept-*`) are isolated and can be deleted independently without affecting user data.
