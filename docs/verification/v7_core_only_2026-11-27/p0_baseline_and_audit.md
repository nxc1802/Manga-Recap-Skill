# V7 Phase P0 — Baseline Metrics & Dependency Audit

**Plan:** `docs/V7_CORE_ONLY_IMPLEMENTATION_PLAN.md`  
**Baseline Commit SHA:** `457f51d679be56ec60175a5833fea23e4c870deb` (`main`, clean working tree)  
**Audit Timestamp:** 2026-11-27T14:15:00+07:00

---

## 1. Baseline Repository Metrics (`457f51d`)

Measured using `.venv/bin/python` (UTF-8 file bytes, non-blank LOC, `.py`/`.md`/`.json`/`.html`/`.txt`):

| Scope | Files | Total Bytes | Non-blank LOC |
| --- | ---: | ---: | ---: |
| `src/` | 100 | 1,126,271 | 24,057 |
| `tests/` | 70 | 683,663 | 14,115 |
| `.agents/skills/manga-recap/` | 6 | 24,488 | 320 |

### `src/` Breakdown by Domain

| Domain | Files | Bytes | LOC |
| --- | ---: | ---: | ---: |
| `core` | 24 | 287,498 | 6,034 |
| `cloud` | 12 | 201,031 | 4,387 |
| `render` | 8 | 148,576 | 3,028 |
| `tts` | 6 | 114,236 | 2,679 |
| `cli` | 21 | 88,621 | 1,658 |
| `youtube` | 4 | 70,045 | 1,460 |
| `ingest` | 2 | 69,777 | 1,440 |
| `providers` | 5 | 51,570 | 1,183 |
| `crawler` | 7 | 26,656 | 637 |
| `services` | 2 | 25,972 | 566 |
| `archive` | 2 | 15,373 | 322 |
| `compat` | 2 | 12,011 | 268 |
| `production` | 2 | 10,236 | 269 |
| `media` | 2 | 4,605 | 125 |
| `(root)` (`__init__.py`) | 1 | 64 | 1 |

### Baseline Verification Checks (`457f51d`)

- `.venv/bin/python -m compileall -q src tests scripts`: **PASS** (exit 0)
- `.venv/bin/python scripts/sync_requirements.py --check`: **PASS** (exit 0)
- `.venv/bin/python -m pytest -q`: **`585 passed in 56.26s`**

---

## 2. Existing Project Contract & Q09 Compatibility Audit (Read-Only)

Inspected `data/projects/` without modification:

1. `data/projects/bac-si-hoi-quy-chi-muon-song-yen-binh`:
   - `project.json` & `series_setup_answers.json`: `schema_version = "5.0.0"`, `Q01–Q05, Q08–Q10` (`Q09 = "preview"`).
   - `render_settings`: `target_width=1280, target_height=720`, `burn_subtitles=True`, `video_mode="auto"`, `render_pan_mode="animation"`, `subtitle={max_lines:1, font_size:30, chunking:"sentence-aware", max_chars_per_line:82}`.
   - `preview_approval.json`: `schema_version="5.0.0"`, `human_review_status="approved"`, `artifact_path="video/preview.mp4"`.
   - `voice_selection.json`: `status="approved"`, `primary_voice="Hải Đăng"`.
2. `data/projects/nhan-vat-phu-trong-the-gioi-dien-loan`:
   - `project.json` & `series_setup_answers.json`: `schema_version = "4.0.0"` (contains legacy `Q06`, `Q07`), `Q09 = "final"`, `render_settings`: `1920x1080`, `burn_subtitles=False`, `video_mode="auto"`, `render_pan_mode="static"`, `subtitle={max_lines:1, font_size:28, chunking:"sentence-aware", max_chars_per_line:42}`.
   - `preview_approval.json`: `schema_version="4.1.0"`, `human_review_status="approved"`.
   - `voice_selection.json`: `schema_version="3.0.0"`, `status="approved"`, `voice_strategy="single_narrator"`, `primary_voice="Ngọc Huyền"`.
3. `data/projects/tro-thanh-rong-nho`:
   - `project.json` & `series_setup_answers.json`: `schema_version = "4.0.0"` (contains legacy `Q06`, `Q07`), `Q09 = "final"`, `render_settings`: `1920x1080`, `burn_subtitles=True`, `video_mode="auto"`, `render_pan_mode="animation"`, `subtitle={max_lines:1, font_size:30, chunking:"sentence-aware", max_chars_per_line:82}`.
   - `preview_approval.json`: `schema_version="4.1.0"`, `human_review_status="approved"`.
   - `voice_selection.json`: `schema_version="3.0.0"`, `status="approved"`, `voice_strategy="single_narrator"`, `primary_voice="Ngọc Huyền"`.

### Resolution for D07 / D08 (720p vs 1080p Quality Contract & Chapter 1 Promote)

- In current `src/render/chapter_renderer.py`, `promote = quality == "final" and str(chapter_num) == "1"` copies approved `video/preview.mp4` and `subtitles/preview.srt` byte-for-byte to `video/final.mp4` / `subtitles/final.srt`, while `series_setup.py` sets `target_width x target_height` directly from `Q09` (`720p`/`preview` -> `1280x720`, `1080p`/`final` -> `1920x1080`).
- Therefore there is **no silent upscaling** in `chapter_renderer.py`: preview and promoted/subsequent finals share the project's confirmed Q09 target dimensions (`720p` or `1080p`).
- V7 will lock `RenderSettings` to explicit landscape target resolution (`1280x720` or `1920x1080`, confirmed in setup Q09) and require exact byte promotion only when target matches confirmed setup; changing resolution changes `project.json`, invalidating `review_fingerprint` and forcing fresh render + explicit re-review (`D07`, `D08`).
- Existing `4.0.0` setups in `data/projects/` will be migrated only via explicit standalone migration (`scripts/migrate_legacy_project.py`), never silently rewritten on load (`A02`).

---

## 3. Caller & Boundary Audit (P1–P4 Execution Map)

### Phase P1 — Setup, Decision, Crawler, Script (`A01–A03`, `B01–B09`, `C01–C07` + Fixes #1 & #2)

1. **Setup & Config (`A01–A03`):**
   - Consolidate competing defaults (`src/core/config.py`, `src/core/schemas.py`, `src/core/series_setup.py`, `src/core/workflow.py`, `src/cli/*`): remove per-command `voice`/`speed`/`bgm`/`video_mode`/`subtitle_*` overrides and legacy `config.yaml` / `execution_policy.resolve_legacy_chapter` runtime fallbacks.
   - Move `src/compat/migrate.py` + `normalize_stage_type` / `from_legacy_dict` / `parse_legacy_script_csv` / `normalize_legacy_script_data` / `migrate_project_artifacts` out of runtime `src/` paths into standalone `scripts/migrate_legacy_project.py` (with explicit backup), keeping zero silent legacy normalization in runtime loaders (`A02`).
2. **Crawler (`B01–B02`):**
   - Remove `src/crawler/profiles/generic.json` (if present) and generic fallback in `src/crawler/registry.py` + `src/crawler/extractor.py` + `src/crawler/resolver.py`; raise explicit `CrawlerError` when a URL does not match a known site profile while keeping all multi-site profiles (`B02`).
3. **Decision Engine & Visual Policy (`B03–B09` + Fixes #1 & #2):**
   - `src/ingest/decision.py`, `src/core/workflow.py`, `src/cli/segment_cmd.py`, `src/cli/main.py`: remove `skip_decide`/`--skip-decide` (`B03`), `reapply_policy`/`--reapply-policy` (`B04`), `dry_run`/`--dry-run` on `decide` (`B05`), consolidate `reclassify_all` + `refresh_decisions` into a single `refresh: bool` / `--refresh` (`B06`), remove ablation config flags (`verify_merges` etc. -> fixed single 4-sided adaptive behavior, `B08`), remove `_is_incompatible_legacy_crop` silent fallback (`B07` — fail with explicit migration prompt if incompatible crop metadata is loaded).
   - **Correctness Fix #1 (P0 — Panel Identity After Repack):** In `src/ingest/decision.py:run_decision_pipeline`, resolve `KEEP`/repack Pass 1/Pass 2 reuse strictly by explicit lineage (`source_raw_panels` + verified `source_raw_sha256s` + dimensions) before ever consulting IDs, eliminating panel ID collision after split/drop renumbering. Child panels (`split_at` / `merge`) must resolve/run Pass 2 keyed by `(p.panel_id, p.sha256, width, height)`.
   - **Correctness Fix #2 (P1 — Cache Byte Integrity):** Before accepting any cached Pass 1 / Pass 2 / split / merge entry, verify on-disk file existence (`chap_dir / p.relative_path`), recompute actual byte SHA-256 (`calculate_sha256`), verify real image dimensions `(width, height)`, and reject incomplete model answers (all 6 keys required unless blocked).
4. **Script & Narrative (`C01–C07`):**
   - Remove runtime legacy CSV (`C01`) and legacy JSON (`C02`) script ingestion from `src/core/schemas.py` / `src/core/storage.py` / `src/services/tts_service.py` (move explicit converter to standalone migration script).
   - Remove `voice_strategy` non-single-narrator branches (`C03`, keep only canonical single-narrator flow + `VoiceSelectionApproval`).
   - Remove automatic `substitute_panel` fallback (`C04` in `src/render/visual_policy.py` — fail validation with clear error).
   - Trim verbose bloated diagnostics in `src/core/pacing.py` (`C05`), preserve `needs_review` / `[CAN_XAC_MINH: ...]` (`C06`) and `characters.json` + `story_summary.md` (`C07`).

### Phase P2 — Preview, TTS Utilities & Rendering (`D01–D08`, `F01–F10`)

- `src/core/review_pack.py`: delete HTML review pack rendering & HTML template (`D01`), keep concise `review_pack.md` + `review_pack.json` (`D02–D03`).
- `src/tts/engine.py`: remove `MockTTSBackend` / `EngineMode.MOCK` / `VIENEU_MOCK` from production runtime (`D04`, keep test mock helper in `tests/fixtures/` for unit tests only).
- `src/tts/benchmark.py` & `src/cli/tts_benchmark_cmd.py`: relocate out of runtime `src/` to `scripts/devtools/` (`D05`).
- `src/cli/*`, `src/core/workflow.py`, `src/render/chapter_renderer.py`: delete `--skip-review`, `--approved`, `--allow-placeholders` (`D06`).
- `src/core/schemas.py`, `src/render/camera.py`, `src/render/subtitle.py`, `src/render/fast_renderer.py`, `src/render/chapter_renderer.py`, `src/render/audio_mixer.py`: remove `portrait_vertical`, `portrait_padded`, `auto` (`F01–F02` -> 16:9 landscape fit only), remove `fixed-chars` chunking (`F03`) & subtitle style/line/font overrides while keeping burn on/off (`F04`), remove legacy motion aliases keeping `static` & `slow_pan_down` (`F05–F06`), remove `keep_temp` & non-VieNeu fallbacks (`F07–F08`), preserve encoders, BGM/speed/gains, verified temp cleanup, FFmpeg fast paths, and master WAV (`F09–F10`).

### Phase P3 — Kaggle Strict Production Path (`E01–E13` + Fixes #3 & #4)

- Delete `src/cloud/account_fallback.py` (`E05`).
- `src/cloud/scheduler.py` & `src/cloud/dual_gpu_worker.py` & `src/cloud/kaggle_service.py`: delete `BASELINE_SPLIT` / Policy A (`E01`), NVENC->CPU fallback (`E02`), single-GPU & CPU Kaggle modes (`E03–E04`), `cloud_policy`/`cloud_batch_size` overrides (`E08`, lock Policy B `dual_tts_shared_encode`, produce `bs=16`, standalone `tts` `bs=32`), speculative retry state transitions (`E13`), and all `cancel_kernel`/`cancel_job` calls (`E12`).
- `src/providers/kaggle.py` & `src/cloud/lifecycle.py`: enforce **Fix #3 (P0)** — keep durable intent lock (`SUBMITTING`, `SUBMIT_UNKNOWN`, `QUEUED`, `RUNNING`, `REMOTE_COMPLETE`, `QUEUE_TIMEOUT`, `CLIENT_TIMEOUT`) until explicit remote terminal state reconciled on original account; implement **Fix #4 (P1)** — distinguish HTTP 429/401/403/4xx/5xx/network/empty ambiguity cleanly.
- Move heavy telemetry/profiler utilities out of hot runtime if not needed at runtime (`E10`). Retain chunk planner (`E11`), bounded queues, OOM backoff, shared compile (`E06–E07, E09`).

### Phase P4 — Release Services, CLI, Cleanup & Skill (`G01–G11`, `H01–H05`)

- `src/youtube/publisher.py`, `src/cli/youtube_cmd.py`: delete interactive channel selector (`G03`) & custom media path overrides (`G04`); keep multi-account/channel support, chapter/feature upload, idempotent upload & verify (`G01–G02, G05–G06`).
- `src/archive/hf_archive.py` & `src/cli/archive_cmd.py`: relocate `archive_restore` (`G09`) to standalone `scripts/restore_archive.py`; keep HF `push`/`verify` (`G07–G08`) and explicit `clean --dry-run` safe removal (`G10–G11`).
- `src/cli/*`: expose canonical 11 high-level commands (`setup, prepare, validate, preview, approve, produce, compile, publish, archive, clean, status`) + retained granular repair/account tools (`H02`); remove `batch`, `review-approve`, `tts_benchmark` runtime CLI, deprecated flags/aliases, and redundant verbose diagnostics (`H01, H03–H05`).
- `.agents/skills/manga-recap/`: condense `SKILL.md` to canonical 8-stage workflow (`H05` + Section 6 P4), deduplicate `AGENTS.md`, `README.md`, `references/`, remove obsolete tests/dependencies.

---

## 4. Non-Destructive Rollback Plan

- Pre-V7 clean tag/commit: `457f51d679be56ec60175a5833fea23e4c870deb` (`docs/V7_CORE_ONLY_IMPLEMENTATION_PLAN.md` committed on `main`).
- Each V7 phase (`P0`, `P1`, `P2`, `P3`, `P4`, `P5`) is committed atomically without touching `data/projects/`.
- Non-destructive rollback at any point: `git revert <range>` or `git checkout -b rollback/pre-v7 457f51d679be56ec60175a5833fea23e4c870deb` (zero forced history rewrites).
