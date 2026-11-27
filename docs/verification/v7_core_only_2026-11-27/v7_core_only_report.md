# V7 Core-Only Architecture — Final Implementation & Verification Report

**Repository:** `nxc1802/Manga-Recap-Skill` (`main`)  
**Authoritative Plan:** [`docs/V7_CORE_ONLY_IMPLEMENTATION_PLAN.md`](../../V7_CORE_ONLY_IMPLEMENTATION_PLAN.md)  
**Baseline Commit SHA (P0):** `457f51d679be56ec60175a5833fea23e4c870deb`  
**V7 Implementation SHA (P4 Code Complete):** `d2003685f4a4a9fd3965cfc99581130893ea96dd`  
**Audit Date:** 2026-11-27  

---

## 1. Executive Summary

All 66 user-approved architectural decisions (`A01–H05`: 44 reduction/consolidation/relocation actions and 22 retained capabilities) and all 4 Section 4 correctness fixes (`#1–#4`) have been implemented across Phases **P0–P5** without modifying `data/projects/*`.

Key outcomes:
- **Deterministic Core:** Production runtime (`src/`) now enforces one canonical configuration (`series_setup_answers.json` `Q01–Q05, Q08–Q10` + `voice_selection.json`), closed-set `gemini-decision-1` routing with fixed 4-sided adaptive edge trims (`0/10/20/30/40%`), VieNeu-TTS v3 Turbo single-narrator synthesis, 16:9 `landscape_fit` FFmpeg rendering (`static`, `slow_pan_down`, `static_focus`; sentence-aware subtitles), private HF dataset archival/verification, and explicit YouTube multi-channel upload/verification on canonical paths only.
- **Hardened Kaggle Dual-T4 Path:** Removed Policy A (`baseline_split`), NVENC-to-CPU fallback, single-GPU/CPU cloud execution, automatic account rotation, timeout auto-cancellation, and CLI policy/batch overrides. Retained **Policy B (`dual_tts_shared_encode`)** with strict hardware preflight (2× Tesla T4; dual `h264_nvenc` for render/produce), pinned single account, fixed batches (`bs=16` produce/render, `bs=32` standalone TTS), Chunk Planner resume, and durable lock/reconciliation (`cloud reconcile` / `cloud pull`).
- **Four Correctness Fixes Delivered:** Resolved panel identity collision after repack (`#1`), actual image SHA-256 & geometry verification on Decision cache hits (`#2`), duplicate-job/lock preservation without unreliable SDK cancel (`#3`), and HTTP 401/403/429/4xx/5xx/empty/transport exception classification (`#4`).
- **Offline Verification Passed:** Full test suite passed (`555 passed in 53.81s` at `d200368`, re-verified after docs/report changes), static checks passed, offline FFmpeg parity/scheduler tests passed, and offline Dual-T4 simulation and standalone migration/devtools utilities ran cleanly.
- **Live Cloud/API Caveat:** Live Gemini API, live Kaggle Dual-T4 execution, live Hugging Face pushes, and live YouTube uploads were **NOT RUN** in this offline workspace.

---

## 2. Phase Commit History

| Phase | Commit SHA | Summary | Diffstat (`--stat`) |
| --- | --- | --- | --- |
| **P0** | `fd62eff81560ff701e4667ea216156188e0ee496` | Baseline metrics & dependency audit (`p0_baseline_and_audit.md`) | `1 file, +121` |
| **P1** | `c696659fe6e076c3fec4de685d8f98cec85d93ad` | Setup consolidation, legacy migration relocation, crawler/decision/script fallback removals, Fixes #1 & #2 | `31 files, +1251/-1615` |
| **P2** | `0587d877824ea0c4491ee1fb7bf31338756b734f` | Remove HTML review pack, runtime mock/benchmark, legacy video/camera/subtitle modes, and approval bypass flags | `49 files, +708/-1336` |
| **P3** | `3874ba01552f4e112dfc64d2338e1126b3061ec3` | Enforce Kaggle Policy B Dual-T4, remove Policy A/CPU/single-GPU/account fallback/cancel, Fixes #3 & #4 | `23 files, +878/-1290` |
| **P4** | `d2003685f4a4a9fd3965cfc99581130893ea96dd` | Prune YouTube overrides, relocate HF restore, simplify CLI (`allow_abbrev=False`), condense Skill & docs | `26 files, +305/-485` |
| **Combined (`457f51d..d200368`)** | — | Full V7 code & doc transition across workspace | `103 files, +3261/-4724` |

---

## 3. Codebase Size & Relocation Accounting

Measurements below compare Git tree `457f51d` (P0 baseline) against `d200368` (P4 code complete) via UTF-8 byte counts and non-blank LOC. Relocation is separated explicitly from net runtime deletion.

### 3.1 Summary by Repository Area

| Scope (Filter) | P0 Files | P0 Bytes | P0 Non-blank LOC | V7 Files | V7 Bytes | V7 Non-blank LOC | Delta Bytes | Delta LOC |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `src/` (`.py` only) | 100 | 1,126,271 | 24,057 | 93 | 998,175 | 21,507 | `-128,096` (`-11.37%`) | `-2,550` |
| `src/` (all tracked, incl. `.json` profiles) | 103 | 1,129,111 | 24,140 | 95 | 1,000,133 | 21,565 | `-128,978` (`-11.42%`) | `-2,575` |
| `scripts/` (`.py` only, standalone & devtools) | 10 | 79,203 | 1,614 | 13 | 131,732 | 2,754 | `+52,529` (`+66.32%`) | `+1,140` |
| `tests/` (`.py` only) | 70 | 683,663 | 14,115 | 70 | 686,250 | 14,138 | `+2,587` (`+0.38%`) | `+23` |
| `  └─ tests/fixtures/` (`.py` subset) | 1 | 18,366 | 434 | 2 | 27,448 | 664 | `+9,082` (`+49.45%`) | `+230` |
| `.agents/skills/manga-recap/` (all 6 files) | 6 | 24,488 | 320 | 6 | 20,221 | 291 | `-4,267` (`-17.42%`) | `-29` |

### 3.2 `src/` (`.py`) Breakdown by Domain

| Domain | P0 Files / Bytes / LOC | V7 Files / Bytes / LOC | Delta Bytes | Delta LOC | Notes |
| --- | ---: | ---: | ---: | ---: | --- |
| `tts` | 6 / 114,236 / 2,679 | 5 / 83,474 / 1,998 | `-30,762` (`-26.9%`) | `-681` | Removed mock engine (`D04`) & benchmark CLI (`D05`) |
| `cloud` | 12 / 201,031 / 4,387 | 11 / 176,677 / 3,864 | `-24,354` (`-12.1%`) | `-523` | Deleted `account_fallback.py`, Policy A, sampler, cancel |
| `core` | 24 / 287,498 / 6,034 | 23 / 264,303 / 5,603 | `-23,195` (`-8.1%`) | `-431` | Deleted `config.py`, HTML pack, verbose pacing diagnostics |
| `cli` | 21 / 88,621 / 1,658 | 19 / 69,084 / 1,360 | `-19,537` (`-22.0%`) | `-298` | Deleted `batch_cmd.py`, `tts_benchmark_cmd.py`, retired flags |
| `render` | 8 / 148,576 / 3,028 | 8 / 134,865 / 2,779 | `-13,711` (`-9.2%`) | `-249` | Landscape-only camera, sentence-aware subtitles, pruned substitution |
| `compat` | 2 / 12,011 / 268 | 0 / 0 / 0 | `-12,011` (`-100.0%`) | `-268` | Deleted `src/compat/`; moved to `scripts/migrate_legacy_project.py` |
| `services` | 2 / 25,972 / 566 | 2 / 20,798 / 451 | `-5,174` (`-19.9%`) | `-115` | Pruned legacy script parsers, non-single-narrator paths |
| `archive` | 2 / 15,373 / 322 | 2 / 12,408 / 262 | `-2,965` (`-19.3%`) | `-60` | Relocated `archive_restore` to `scripts/restore_archive.py` |
| `youtube` | 4 / 70,045 / 1,460 | 4 / 68,413 / 1,424 | `-1,632` (`-2.3%`) | `-36` | Deleted interactive selector & custom upload overrides |
| `crawler` | 7 / 26,656 / 637 | 7 / 26,526 / 629 | `-130` (`-0.5%`) | `-8` | Pruned generic fallback (`generic.json` `-882 B` non-py) |
| `production` | 2 / 10,236 / 269 | 2 / 10,236 / 269 | `0` | `0` | Unchanged |
| `media` | 2 / 4,605 / 125 | 2 / 4,605 / 125 | `0` | `0` | Unchanged |
| `(root)` | 1 / 64 / 1 | 1 / 64 / 1 | `0` | `0` | Unchanged |
| `ingest` | 2 / 69,777 / 1,440 | 2 / 71,813 / 1,490 | `+2,036` (`+2.9%`) | `+50` | Net growth from Fix #1 & Fix #2 hardening despite B03–B08 deletions |
| `providers` | 5 / 51,570 / 1,183 | 5 / 54,909 / 1,252 | `+3,339` (`+6.5%`) | `+69` | Net growth from Fix #4 HTTP error classification |

### 3.3 Relocated Maintenance/Test Code vs. Net Deletion

| Relocated Destination | P0 Size | V7 Size | Delta | Origin in `src/` |
| --- | ---: | ---: | ---: | --- |
| `scripts/devtools/tts_benchmark.py` | `0` | `23,445 B` / `511 LOC` | `+23,445 B` | `src/tts/benchmark.py` (`D05`, `H03`) |
| `scripts/migrate_legacy_project.py` | `0` | `19,489 B` / `423 LOC` | `+19,489 B` | `src/compat/migrate.py` + legacy parsers (`A02`, `C01`, `C02`) |
| `scripts/restore_archive.py` | `0` | `5,443 B` / `111 LOC` | `+5,443 B` | `src/archive/hf_archive.py` (`G09`) |
| `scripts/devtools/cloud_policy_benchmark.py` | `10,190 B` / `213 LOC` | `14,370 B` / `308 LOC` | `+4,180 B` | `src/cloud/telemetry.py` (`ResourceSampler`) + `BASELINE_SPLIT_POLICY` (`E10`) |
| `tests/fixtures/mock_tts.py` | `0` | `9,067 B` / `230 LOC` | `+9,067 B` | `src/tts/engine.py` mock synthesizer (`D04`) |

- **Raw `src/` `.py` reduction:** `-128,096 B` (`-2,550 LOC`, `-11.37%`) plus `-882 B` from `src/crawler/profiles/generic.json`.
- **Moved outside runtime (`scripts/` delta + `mock_tts.py`):** `+61,596 B` (`+1,370 LOC`).
- **True net Python deletion (`src/` + `scripts/` + `mock_tts.py`):** `-66,500 B` (`-1,180 LOC`) net removed, alongside explicit security/correctness additions in `ingest` and `providers` (`+5,375 B`).

**Explanation vs. Section 7 Size Direction (`650–800 KB`):** Section 7 explicitly noted `650–800 KB` was an aspirational direction and prohibited deleting protected core functionality or minifying code to chase a byte target. After removing all 44 targeted legacy/fallback branches, `~998 KB` remains in `src/` because the 22 mandatory `KEEP` paths and Section 5 invariants—site extractors, VCS seam/crop/contact sheets, guarded Decision pipeline + Fixes #1–#2, VieNeu v3 Turbo runtime + 25-voice catalog + audio cache, FFmpeg parallel clip/subtitle fastpaths + camera trajectory checks + multi-encoder local rendering + master WAV, Kaggle Dual-T4 Policy B scheduler/worker/ChunkPlanner/durable lifecycle + Fixes #3–#4, multi-account YouTube upload/verify/resume, and private HF archival—constitute irreducible core product logic.

---

## 4. Complete 66-Decision Register & Correctness Fixes

### Group A — Setup (3/3 Complete)

| ID | Action | Status | Concrete Change & Evidence |
| --- | --- | --- | --- |
| `A01` | SIMPLIFY | DONE | Removed per-command voice/speed/BGM/subtitle overrides; confirmed `series_setup_answers.json` + `voice_selection.json` govern execution. |
| `A02` | MOVE | DONE | Deleted `src/compat/` (`__init__.py`, `migrate.py`); isolated V3/V4/V5 migration into `scripts/migrate_legacy_project.py`. Rejected implicit migration at runtime. |
| `A03` | SIMPLIFY | DONE | Deleted `src/core/config.py`; consolidated canonical defaults across `src/core/settings.py`, `src/core/schemas.py`, `src/core/series_setup.py`. |

### Group B — Data Preparation (9/9 Complete)

| ID | Action | Status | Concrete Change & Evidence |
| --- | --- | --- | --- |
| `B01` | REMOVE | DONE | Deleted `src/crawler/profiles/generic.json` and generic crawler fallback in `src/crawler/registry.py`; unknown sites raise explicit `CrawlerError`. |
| `B02` | KEEP | DONE | Retained multi-site profile registry and extractor pipeline in `src/crawler/`. |
| `B03` | REMOVE | DONE | Removed `--skip-decide` / `skip_decide` manual VLM bypass; Decision failures/missing keys stop prepare cleanly. |
| `B04` | REMOVE | DONE | Removed `--reapply-policy` runtime branch and offline replay preflight machinery in `src/ingest/decision.py` and `src/cli/segment_cmd.py`. |
| `B05` | REMOVE | DONE | Removed Decision `--dry-run` option and dry-run report generation while preserving `clean --dry-run`. |
| `B06` | SIMPLIFY | DONE | Replaced duplicate refresh options with a single `--refresh` flag (`refresh: bool`) across `src/ingest/decision.py` and CLI. |
| `B07` | REMOVE | DONE | Removed legacy 9-preset crop runtime compatibility; legacy crops fail cleanly with migration instructions. |
| `B08` | SIMPLIFY | DONE | Fixed 4-sided adaptive settings (`edge trims 0/10/20/30/40%`, H/W `2.2` threshold, `verify_merges` fixed on); removed ablation switches. |
| `B09` | KEEP | DONE | Retained explicit `repack` and `panel-inspect` Agent repair commands routed through core functions. |

### Group C — Script (7/7 Complete)

| ID | Action | Status | Concrete Change & Evidence |
| --- | --- | --- | --- |
| `C01` | MOVE | DONE | Removed legacy CSV script parser from runtime (`src/services/tts_service.py` / `src/core/schemas.py`); retained `parse_legacy_script_csv` in `scripts/migrate_legacy_project.py`. |
| `C02` | MOVE | DONE | Removed legacy JSON script auto-normalization from runtime; retained `normalize_legacy_script_data` in `scripts/migrate_legacy_project.py`. |
| `C03` | REMOVE | DONE | Removed non-`single_narrator` voice strategy branches; enforced single approved narrator across schemas and TTS service. |
| `C04` | REMOVE | DONE | Removed automatic missing-panel substitution in `src/render/visual_policy.py` and `src/core/workflow.py`; invalid evidence fails validation. |
| `C05` | SIMPLIFY | DONE | Simplified `src/core/pacing.py` while keeping essential WPM/reading-duration pacing checks. |
| `C06` | KEEP | DONE | Retained `needs_review` and `[CAN_XAC_MINH: ...]` uncertainty gates blocking preview/produce until resolved or human-reviewed. |
| `C07` | KEEP | DONE | Retained persistent `characters.json` and `story_summary.md` continuity state. |

### Group D — Preview & TTS Utilities (8/8 Complete)

| ID | Action | Status | Concrete Change & Evidence |
| --- | --- | --- | --- |
| `D01` | REMOVE | DONE | Removed HTML Review Pack renderer and template from `src/core/review_pack.py`. |
| `D02` | KEEP | DONE | Retained concise `review_pack.md` summary for human gate review. |
| `D03` | KEEP | DONE | Retained `review_pack.json` and `preview_approval.json` with exact `config_fingerprint` and `artifact_hash`. |
| `D04` | MOVE | DONE | Removed mock TTS backend from `src/tts/engine.py` (`src/tts/__init__.py`); moved mock synthesizer to `tests/fixtures/mock_tts.py`. |
| `D05` | MOVE | DONE | Deleted `src/cli/tts_benchmark_cmd.py`; relocated `TTSBenchmarkRunner` from `src/tts/benchmark.py` to `scripts/devtools/tts_benchmark.py`. |
| `D06` | REMOVE | DONE | Removed `--skip-review`, `--approved`, `--allow-placeholders`, and approval bypass aliases across CLI and workflow. |
| `D07` | KEEP | DONE | Retained verified exact-byte Chapter 1 `preview.mp4` promotion when target `Q09` and fingerprints match; invalidates on change. |
| `D08` | KEEP | DONE | Retained Local and Kaggle preview execution honoring `Q02` backend and `Q09` target (`720p` / `1080p`). |

### Group E — Kaggle Cloud Execution (13/13 Complete)

| ID | Action | Status | Concrete Change & Evidence |
| --- | --- | --- | --- |
| `E01` | REMOVE | DONE | Removed Policy A (`baseline_split`) and policy selector from `src/cloud/scheduler.py` & `dual_gpu_worker.py`; enforced Policy B `dual_tts_shared_encode`. |
| `E02` | REMOVE | DONE | Removed Kaggle NVENC-to-`libx264` CPU fallback; unusable dual `h264_nvenc` fails `render`/`produce` preflight. |
| `E03` | REMOVE | DONE | Removed single-GPU Kaggle fallback; `_validate_worker_hardware` and scheduler require `>= 2` Tesla T4 CUDA devices. |
| `E04` | REMOVE | DONE | Removed CPU-only Kaggle execution fallback (`gpu=False` rejected in `kaggle_service.py` / `payload.py`). |
| `E05` | REMOVE | DONE | Deleted `src/cloud/account_fallback.py`; removed automatic account rotation while retaining original job `account_name` binding for `pull`/`reconcile`. |
| `E06` | KEEP | DONE | Retained standalone remote `tts` mode using both T4 GPUs without requiring `h264_nvenc`, fixed `batch_size=32`. |
| `E07` | KEEP | DONE | Retained standalone remote `render` mode using dual `h264_nvenc` without initializing VieNeu TTS models. |
| `E08` | SIMPLIFY | DONE | Removed `--cloud-policy` / `--cloud-batch-size` overrides; fixed Policy B produce batch size to `16`. |
| `E09` | SIMPLIFY | DONE | Consolidated queue/transfer/poll limits while retaining workload estimates for Chunk Planner (`E11`). |
| `E10` | MOVE | DONE | Relocated `ResourceSampler` and `BASELINE_SPLIT_POLICY` from `src/cloud/` to `scripts/devtools/cloud_policy_benchmark.py`; kept minimal timings. |
| `E11` | KEEP | DONE | Retained automatic Chunk Planner, verified chapter/chunk receipts, and completed-chapter resume. |
| `E12` | REMOVE | DONE | Deleted `cancel_kernel` (`src/providers/kaggle.py`), `cancel_job` (`kaggle_service.py`), and timeout auto-cancel (`lifecycle.py`); added explicit `cloud reconcile`. |
| `E13` | SIMPLIFY | DONE | Pruned speculative recovery transitions; kept durable intent reservation (`SUBMIT_UNKNOWN`, `QUEUE_TIMEOUT`, `CLIENT_TIMEOUT`, `REMOTE_COMPLETE`) and safe retry. |

### Group F — Rendering (10/10 Complete)

| ID | Action | Status | Concrete Change & Evidence |
| --- | --- | --- | --- |
| `F01` | REMOVE | DONE | Deleted `portrait_vertical`, `portrait_padded`, and portrait rendering paths in `src/render/camera.py` & `chapter_renderer.py`. |
| `F02` | REMOVE | DONE | Removed `auto` video mode; fixed canonical `landscape_fit` (16:9). |
| `F03` | REMOVE | DONE | Removed `fixed-chars` subtitle chunking from `src/render/subtitle.py`; kept `sentence-aware` chunking only. |
| `F04` | SIMPLIFY | DONE | Fixed canonical subtitle styling defaults while preserving burn on/off (`Q05`). |
| `F05` | REMOVE | DONE | Deleted legacy camera motion aliases in `src/render/camera.py` & schemas. |
| `F06` | KEEP | DONE | Retained `static`, `slow_pan_down`, and `static_focus` camera motions with safe crop bounds & FFmpeg fastpaths. |
| `F07` | SIMPLIFY | DONE | Removed `--keep-temp` overrides; intermediate clips clean automatically only after verified final output, keeping restart evidence on failure. |
| `F08` | REMOVE | DONE | Deleted non-VieNeu production TTS fallbacks. |
| `F09` | KEEP | DONE | Retained local encoder selection (`libx264`, NVENC, VideoToolbox) without silent cloud degradation. |
| `F10` | KEEP | DONE | Retained compiled `master.wav` export alongside compiled series MP4/SRT. |

### Group G — Release Services (11/11 Complete)

| ID | Action | Status | Concrete Change & Evidence |
| --- | --- | --- | --- |
| `G01` | KEEP | DONE | Retained YouTube upload, privacy/account enforcement, and live verification polling. |
| `G02` | KEEP | DONE | Retained multi-channel registry (`youtube accounts`) and explicit `--account` routing. |
| `G03` | REMOVE | DONE | Deleted `prompt_interactive_channel_selection` and `-i/--interactive` prompts (`src/youtube/publisher.py`, `src/cli/youtube_cmd.py`). |
| `G04` | REMOVE | DONE | Deleted `--video`, `--subtitles`, `--thumbnail` custom upload path overrides (`resolve_upload_*_path` enforce canonical chapter/feature outputs). |
| `G05` | KEEP | DONE | Retained both single-chapter and compiled-feature upload paths. |
| `G06` | KEEP | DONE | Retained durable upload resume/verification (`upload_receipt.json`) preventing duplicate uploads. |
| `G07` | KEEP | DONE | Retained private HF dataset archival (`archive push`). |
| `G08` | KEEP | DONE | Retained HF checksum & revision verification (`archive verify`). |
| `G09` | MOVE | DONE | Removed `archive restore` from `src/archive/` & CLI; moved `archive_restore` to standalone `scripts/restore_archive.py`. |
| `G10` | REMOVE | DONE | Ensured zero automatic raw-panel deletion across pipeline runs; explicit `clean` only. |
| `G11` | KEEP | DONE | Retained `clean` safe allowlist, `--dry-run` default, `--confirm-project <slug>`, and verified-archive gate before `--purge-raw`. |

### Group H — CLI & Documentation (5/5 Complete)

| ID | Action | Status | Concrete Change & Evidence |
| --- | --- | --- | --- |
| `H01` | REMOVE | DONE | Deleted `batch` (`src/cli/batch_cmd.py`), `review-approve`, and redundant top-level/subcommand aliases (`prepare` alias on `youtube`, etc.). |
| `H02` | KEEP | DONE | Retained 11 canonical high-level commands (`setup, prepare, validate, preview, approve, produce, compile, publish, archive, clean, status`) + 12 granular tools (`crawl, segment, repack, panel-inspect, decide, tts, visual-plan, render, review-pack, inspect, cloud, youtube`). |
| `H03` | MOVE | DONE | Removed `tts-benchmark` from runtime CLI dispatch; moved to `scripts/devtools/tts_benchmark.py`. |
| `H04` | REMOVE | DONE | Removed retired flags and enabled `allow_abbrev=False` in `JsonAwareArgumentParser` so retired flags (e.g., `--video` near `--video-id`) fail with exit code 2 instead of prefix-matching. |
| `H05` | SIMPLIFY | DONE | Simplified `status`/`inspect` outputs (`_summarize_cloud_state` pruned stale `resources`), condensed `.agents/skills/manga-recap/SKILL.md` (`version: "7.0.0"`, `112` lines), updated `AGENTS.md`, `README.md`, `references/`. |

### Section 4 Correctness Fixes (4/4 Verified in Offline Tests)

1. **Fix #1 (P0) — Panel Identity After Repack (`src/ingest/decision.py`):** Fixed `KEEP` / split / merge Pass 1 & Pass 2 identity matching to verify explicit source lineage (`source_raw_panels`, verified `source_raw_sha256s`, and image dimensions) before IDs, eliminating panel ID collision across renumbered/shifted repacks.
2. **Fix #2 (P1) — Cache Byte Integrity (`src/ingest/decision.py`):** Enforced on-disk file existence, recomputed byte SHA-256 verification, dimension checks, and 6-key answer completeness before reusing cached Pass 1 / Pass 2 / split / merge decisions.
3. **Fix #3 (P0) — Cancellation & Duplicate Job Prevention (`src/providers/kaggle.py`, `src/cloud/lifecycle.py`, `src/cloud/kaggle_service.py`):** Removed unreliable `cancel_kernel` and timeout auto-cancel; preserved active locks/reservations across `SUBMIT_UNKNOWN`, `QUEUE_TIMEOUT`, `CLIENT_TIMEOUT`, and `REMOTE_COMPLETE` until remote state is explicitly reconciled (`cloud reconcile` / `cloud pull`).
4. **Fix #4 (P1) — HTTP Classification (`src/providers/kaggle.py`):** Implemented `_extract_http_status_and_body` and `_classify_push_exception` cleanly distinguishing HTTP `401/403` auth, `429` rate limit, `400-404` non-retryable client errors, `5xx` propagation errors, and ambiguous empty/transport errors without rotating accounts or dropping active locks.

---

## 5. Breaking Changes Summary

- **Retired CLI Commands:** `batch`, `review-approve`, `tts-benchmark`, `archive restore`, `youtube prepare`.
- **Retired CLI Flags / Modes:** `-i/--interactive`, `--video`, `--subtitles`, `--thumbnail` (on `publish`/`youtube`), `--skip-review`, `--approved`, `--allow-placeholders`, `--skip-decide`, `--reapply-policy`, `--dry-run` (on `decide`), `--refresh-decisions` / `--reclassify-all` (use `--refresh`), `--cloud-policy`, `--cloud-batch-size`, `--video-mode`, `--hw-accel`, `--keep-temp`, `--answers` alias, `--suffix`, subtitle styling overrides. Partial flag prefix abbreviation is disabled (`allow_abbrev=False`).
- **Removed Runtime Support:** Legacy `V3/V4/V5` implicit loading (requires `scripts/migrate_legacy_project.py`), generic unknown-site crawling, runtime `MockTTSBackend`, HTML review packs, non-`single_narrator` narration, automatic missing-panel substitution, Kaggle Policy A / single-GPU / CPU / NVENC->CPU fallback / multi-account rotation / programmatic cancel, portrait/blurred video modes, `fixed-chars` subtitles, and non-VieNeu TTS paths.

---

## 6. Verification & Benchmark Evidence

### 6.1 Offline Verification Results (Actual Commands Executed)

All offline verification commands passed cleanly:

1. **Static & Full Regression Suite:**
   ```bash
   .venv/bin/python -m compileall -q src tests scripts
   .venv/bin/python scripts/sync_requirements.py --check
   .venv/bin/python -m pytest -q
   ```
   - Code freeze (`d200368`): **`555 passed in 53.81s`**, `compileall` exit `0`, `sync_requirements.py --check` exit `0`. *(Note: final run repeated after writing this report & `README.md` link.)*
2. **Offline FFmpeg Media Parity, Dual-GPU Scheduler & Decision Hardening Timing Slice:**
   ```bash
   .venv/bin/python -m pytest \
     tests/media/test_kaggle_media_parity.py \
     tests/media/test_render_fastpath.py \
     tests/providers/test_kaggle_scheduler.py \
     tests/ingest/test_decision_4sided_and_hardening.py \
     tests/ingest/test_decision_provider_and_ingest.py \
     tests/integration/test_synthetic_fixture.py -q --durations=12
   ```
   - Result: **`37 passed in 4.34s`** (slowest multi-chapter FFmpeg BGM/watermark/subtitle cloud-produce parity test: `0.64s`).
3. **CLI Contract & Fail-Closed Rejections:**
   - `--version` -> `manga-recap 5.0.0` (exit `0`).
   - Retired/removed forms rejected cleanly with JSON `VALIDATION_ERROR` and exit code `2`:
     `batch --json`, `review-approve --json`, `archive restore -p demo --json`, `publish -p demo -c 1 --video x.mp4 --json`, `produce -p demo -c 1 --cloud-policy baseline --json`, `render -p demo -c 1 --video-mode portrait --json`, `preview -p demo -c 1 --skip-review --json`, `decide -p demo -c 1 --reapply-policy --json`.
4. **Standalone Maintenance & Devtools Checks:**
   - `scripts/migrate_legacy_project.py --help` -> exit `0`; temp V4->V5 CLI smoke: `--dry-run` `planned_count=3`, `--apply` `applied_count=3` with `.bak` backups (`project.json.bak`, `series_setup_answers.json.bak`).
   - `scripts/restore_archive.py --help` -> exit `0` (plus mocked restore tests in `tests/providers/test_hf_archive.py`).
   - `scripts/devtools/tts_benchmark.py --mock --voices "Ngọc Huyền" --emotions neutral --out-dir <tmp> --json` -> exit `0` (`report.json`, `summary.md`, 48 kHz WAV generated).
5. **Offline Policy B Dual-T4 Simulation (`scripts/devtools/cloud_policy_benchmark.py`, 3 repeats):**
   - `short_mixed` (3 ch × 4 seg): historical `baseline_split` median `0.345s` vs V7 `dual_tts_shared_encode` `0.21375s` (`1.614x` simulated speedup).
   - `tts_heavy` (3 ch × 6 seg): `0.755s` vs `0.395s` (`1.911x` simulated speedup).
   - `render_heavy` (3 ch × 3 seg × 3 shots): `0.59s` vs `0.5355s` (`1.102x` simulated speedup).
   - Custom 15-chapter workload (`long_15ch`, 15 ch × 20 seg × 2 shots): `11.295s` vs `8.0325s` (`1.406x` simulated speedup).

### 6.2 Live / External Validation Status (`NOT RUN`)

No live quota-consuming or external state-mutating operations were run in this offline environment:
- **Live Gemini Decision benchmark (`gemini-decision-1`):** `NOT RUN` (historical V6 baseline reference: 118 panels, 206,578 tokens, 241.29s).
- **Live Kaggle Dual-T4 Policy B end-to-end (`bs=16` produce / `bs=32` TTS):** `NOT RUN` (no local CUDA/NVENC hardware; historical V6 single-run worker makespan: 179.04s on 71 segments / 167 shots).
- **Live Hugging Face `archive push` / `verify` / `restore`:** `NOT RUN`.
- **Live YouTube `publish` / `verify`:** `NOT RUN`.

---

## 7. Remaining Limitations & Operational Blockers

1. **Unverified Live Hardware/API Execution:** NVENC hardware initialization, Kaggle Dual-T4 vRAM/throughput behavior at `bs=16`/`bs=32`, and Gemini API latency remain verified only through mocked/synthetic fixtures and historical V6 data until an authorized live run executes.
2. **Existing Legacy Series in `data/projects/`:** Unmodified V4 projects (such as `4.0.0` `series_setup_answers.json` files) will be rejected at runtime with explicit migration guidance until `.venv/bin/python scripts/migrate_legacy_project.py --project-dir data/projects/<slug> --apply` is run by the operator.
3. **Codebase Size vs. Aspirational 650–800 KB Target:** Core runtime Python stands at `998,175 B` (`-11.37%`) because retained site extractors, VCS/crop/contact-sheet processing, VieNeu runtime, FFmpeg fastpaths, Dual-T4 scheduler/lifecycle, and OAuth/HF security gates are protected requirements.

---

## 8. Non-Destructive Rollback & Migration Instructions

- **User Data Protection:** Zero files inside `data/projects/*` were modified by V7 commits.
- **Migrating Existing V4/V5 Projects Forward (Preserves Backups):**
  ```bash
  .venv/bin/python scripts/migrate_legacy_project.py --project-dir data/projects/<slug>
  .venv/bin/python scripts/migrate_legacy_project.py --project-dir data/projects/<slug> --apply
  ```
- **Non-Destructive Git Rollback to Pre-V7 Baseline (`457f51d`):**
  ```bash
  # Inspect or test pre-V7 state on a separate branch (no force-push or reset):
  git checkout -b rollback/pre-v7 457f51d679be56ec60175a5833fea23e4c870deb

  # Or cleanly revert V7 commits on main:
  git revert --no-commit 457f51d679be56ec60175a5833fea23e4c870deb..HEAD
  ```
