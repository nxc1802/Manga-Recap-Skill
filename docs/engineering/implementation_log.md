# Engineering Implementation Log

## Phase 1 — P0 Cloud Pipeline Correctness
- **Owner**: worker_a1 (A1: Cloud Fixer)
- **Component**: `src/cloud/kaggle_service.py`
- **Date**: 2026-11-08T20:25:00Z
- **Baseline Git Commit**: `a9920a53b54e6aba30bcba3d3ac60323d74c9336`

### Summary of Changes

1. **Multi-Panel Rendering & Timeline (`process_segment_render`)**:
   - Eliminated the single-panel assumption (`p_ids[0]`).
   - In the generated Kaggle kernel worker script, every panel in `panel_ids` is now rendered in order for each segment.
   - For multi-panel segments ($M > 1$), frames are saved as `frame_{seq_id}_{p_idx}.jpg` and displayed for `duration / M` seconds.
   - Total segment display duration strictly equals the segment's synthesized audio duration; audio plays continuously without repeating or dropping panels.

2. **Fail-Fast on Missing or Corrupted Assets**:
   - Completely removed silent fallback to `avail_panels[0]`.
   - Missing panels in `panel_ids` immediately raise an explicit `FileNotFoundError` detailing stage (`[Stage: render]`), `chapter_number`, `seq_id`, and exact asset path.
   - Corrupted panel images raise `RuntimeError` on open/verify.
   - Missing or corrupted audio clips in audio concatenation immediately raise `FileNotFoundError` or `RuntimeError` detailing stage (`[Stage: audio_merge]`), `chapter_number`, `seq_id`, and clip path.
   - Asset substitution is strictly forbidden.

3. **Per-Segment TTS Parameters**:
   - Eliminated hardcoded `target_voice = segments[0]...`.
   - Segments are grouped by `voice_id` to maximize inference batching while ensuring each utterance receives its configured voice.
   - Emotion cues (`[cười]`, `[thở dài]`, `[hắng giọng]`) are formatted per segment.
   - Segment `speed` adjustments are applied individually via FFmpeg `atempo` filter.
   - Strict batch count check: if any audio clip fails to generate or batch count mismatches, `RuntimeError` is raised immediately. Silent truncation or clipping is strictly prohibited.
   - Pre-existing valid audio clips on disk/cloud are detected and reused, avoiding redundant neural TTS inference during render-only cloud executions.

4. **Opaque `seq_id` Preservation**:
   - Replaced all occurrences of `f"{idx:03d}"` with actual opaque `seg["seq_id"]` strings (e.g., `"002_1"`).
   - All audio clips (`audio_{chapter}_{seq_id}.wav`), frame paths, and concatenation commands preserve the exact script sequence ID.

5. **Atomic Status Validation & Timeout Handling**:
   - In `dispatch_chapter_job`: If polling reaches `timeout_seconds`, an explicit `TimeoutError` is raised; output download is aborted to prevent downloading corrupt/partial artifacts.
   - In `sync_results`: Strict artifact validation confirms that `final.mp4` (>0 bytes), `chapter_{chapter_number}.srt` (>0 bytes), `script.json`, and all segment audio clips `audio_{chapter}_{seq_id}.wav` (>0 bytes) exist on disk.
   - `chapter.json` `overall_status` is ONLY updated to `"completed"` after all artifact checks pass. Failed stages are never marked `"completed"`.

6. **Security & Licensing**:
   - Added `safe_extract_tar` protecting both local archive extraction and Kaggle VM archive extraction against directory traversal (zip-slip / tar-slip). Rejects absolute paths and paths containing navigation outside destination.
   - Updated Kaggle dataset metadata license to `"licenses": [{"name": "other"}]`, removing hardcoded CC0 license for manga assets with unconfirmed copyright.
   - Sanitized exception logs to redact authentication tokens (`api_token`).

## Phase 2 — P1 Unified Local & Cloud Contracts
- **Status**: PASS
- **Agents Used / Max Concurrency**: 2 (worker_a3: Core/Local Engineer, worker_a4: Cloud Adapter Engineer)
- **Component**: `src/core/schemas.py`, `src/core/storage.py`, `src/core/config.py`, `src/cli/main.py`, `src/cli/validate_cmd.py`, `src/cli/tts_cmd.py`, `src/cli/render_cmd.py`, `src/production/runner.py`, `src/cloud/kaggle_service.py`
- **Date**: 2026-11-08T21:10:00Z
- **Baseline Git Commit**: `a9920a53b54e6aba30bcba3d3ac60323d74c9336`

### Summary of Changes

1. **Canonical Schema Harmonization & Review Decoupling (`src/core/schemas.py`, `src/core/storage.py`)**:
   - Added `HumanReviewStatus` enum (`unreviewed`, `approved`, `changes_requested`).
   - Extended `ChapterMetadata` with independent `technical_status` (execution state) and `human_review_status` (editorial review state) with bidirectional compatibility validators.
   - Added default fallbacks (`project_slug`, `chapter_number`, `title`) to `RecapScript` for legacy fixture compatibility.
   - Updated `update_checkpoint()` to maintain synchronization between technical checkpoints and human review states.

2. **Validation Relaxation & Fail-Fast Integrity (`src/cli/validate_cmd.py`)**:
   - Relaxed validator to support N:M panel mapping: allowed panel reuse across segments and omission of non-essential manifest panels without validation error.
   - Enforced strict fail-fast validation: any panel referenced in `script.json` must exist on disk and resolve in `manifest.json`.
   - Validated opaque string `seq_id` (supporting non-sequential IDs like `"002_1"` or `"scene_01"`).

3. **Render/TTS Separation & CLI Option Precedence (`src/cli/render_cmd.py`, `src/cli/main.py`)**:
   - Fixed parser defaults in `main.py` so CLI flags do not unconditionally override series-level `project.json` settings.
   - Added pre-render audio clip inspection in `render_cmd.py`: if all segment WAV clips exist and are valid (> 44 bytes), render proceeds without invoking neural TTS.
   - Updated Gate 2 review semantics: when `--skip-preview` is used, `technical_status` is marked `completed` while `human_review_status` is strictly recorded as `unreviewed` (never auto-approved).

4. **Dedicated Cloud Job Separation & Option Parity (`src/cloud/kaggle_service.py`)**:
   - Factored worker generation into dedicated generators: `tts` (audio only, omitting panels), `render` (video only, zero `vieneu` code), and `produce` (full pipeline).
   - Added pre-flight check in `dispatch_chapter_job` verifying existing audio before dispatching render-only jobs.
   - Enforced option parity between Local and Cloud: canvas resolution, FPS, video modes (`auto`, `landscape_fit`, `portrait_padded`, `portrait_vertical`), subtitle font scaling, emotion cue sanitization (`[cười]`, `[thở dài]`, `[hắng giọng]`), and FFmpeg sidechain dynamic BGM ducking.
   - Updated `sync_results` to update `ChapterMetadata` canonical statuses (`technical_status = "completed"`, `human_review_status = "unreviewed"`).

### Test Evidence
- Command: `./venv/bin/pytest tests/unit/`
- Result: **450 passed in 46.30s** (100% pass, 0 regressions).
- Contract verification: `tests/unit/cloud/test_kaggle_service.py` (8 new contract and job separation tests passed).

---

## Phase 3 — P1 Human-Driven Agent QA
- **Status**: PASS
- **Agents Used / Max Concurrency**: 1 (worker_a5: Agent Workflow Engineer)
- **Component**: `.agents/skills/manga-recap/SKILL.md`, `rules/**`, `prompts/**`, `templates/**`, `src/cli/review_pack.py`, `src/cli/main.py`, `docs/human_review_guide.md`
- **Date**: 2026-11-08T21:40:00Z

### Summary of Changes

1. **Formalization of N:M Panel Mapping & Story Retention**:
   - Eliminated the rigid "1 Panel $\leftrightarrow$ 1 Segment" constraint across `SKILL.md` and rulebooks.
   - Formalized N:M mapping semantics (1:1, 1:N multi-panel combos, N:1 panel reuse, and selective omission of filler/scenery panels).
   - Enforced the **Mandatory Story Retention Invariant**: all panels containing essential story progression, critical dialogue, or action climaxes must be retained and referenced in the video.

2. **Faithful Storytelling & Zero Automated Benchmarks**:
   - In strict compliance with the Integrity Mandate, zero automated OCR benchmarks, accuracy scores, or LLM-as-judge prompts were built.
   - Established that the coding agent autonomously performs visual inspection and OCR without artificial confidence thresholds.
   - Enforced the **Uncertainty Protocol**: cut-off, blurred, or ambiguous panels are flagged with `status: "needs_review"` and `[CAN_XAC_MINH: <lý_do_chi_tiết>]`.

3. **Lightweight Human Review Pack (`src/cli/review_pack.py`)**:
   - Implemented `manga-recap review-pack` subcommand producing static, standalone inspection artifacts:
     - `review_pack.md`: Markdown summary with segment counts, omitted panel log, flagged uncertainty items, and alignment table.
     - `review_pack.html`: Responsive HTML gallery displaying 4x4 visual contact sheets, audio player controls, subtitle cues, and Ken Burns parameters.
     - `review_pack.json`: Machine-readable summary for editorial workflows.
   - Authored complete Human Review Guide in `docs/human_review_guide.md` with 5-point inspection protocol.

### Test Evidence
- Command: `./venv/bin/pytest tests/unit/test_synthetic_fixture.py tests/unit/test_cli.py tests/unit/test_review_pack.py -v`
- Result: **47 passed in 7.46s**.
- Full Unit Test Suite: **454 passed in 46.78s**.
- CLI Execution: `manga-recap review-pack -p synthetic_fixture -c 1 --json` verified.

---

## Phase 4 — P3 Performance Baseline
- **Status**: PASS
- **Agents Used / Max Concurrency**: 1 (worker_a6: Performance Profiler)
- **Component**: `docs/performance/baseline.md`, `docs/performance/baseline_data.json`, `docs/performance/baseline_data.csv`, `scripts/benchmark/profile_runner.py`, `scripts/benchmark/workload_generator.py`, `tests/performance/test_benchmark_harness.py`
- **Date**: 2026-11-08T22:35:00Z

### Summary of Changes

1. **Standardized Synthetic Workload Generator (`scripts/benchmark/workload_generator.py`)**:
   - Created reproducible synthetic workloads without copyrighted assets:
     - Small Workload: 35 segments, 20 panels (~2.5 min video).
     - Large Workload: 120 segments, 40 panels (~8.5 min video).
     - Batch Workload: 5 chapters (100 segments total, compiled into feature recap).

2. **Empirical Benchmarking & Profiling Harness (`scripts/benchmark/profile_runner.py`)**:
   - Measured cold start vs warm start, cache hits vs misses, hardware VideoToolbox vs CPU libx264, and peak RSS.
   - All measurements obtained from genuine execution without simulation or mock interpolation.

3. **Quantitative Bottleneck Ranking (Ranked 1 to 3)**:
   - **#1 Video Rendering (66.5% of Total E2E Wall Clock)**: Measured at 300.57s on Large Workload due to per-frame FFmpeg `boxblur=25:5` and filtergraph negotiations across 137 individual clips.
   - **#2 VieNeu-TTS Neural Audio Inference (33.5% of Total E2E Wall Clock)**: Measured at 151.55s (RTF 0.189x) due to sequential `_infer_lock` and file I/O roundtrips.
   - **#3 Container Packaging & Validation (< 0.1% of Total E2E Wall Clock)**: Negligible (< 0.01s).

4. **Hardware Discoveries**:
   - Apple Silicon VideoToolbox: Concurrent multi-threaded invocations (`workers=4`) crashed macOS `com.apple.coremedia.videoencoder` with `SIGKILL (-9)` from `launchd`. Serial dispatch or CPU libx264 operated cleanly.
   - Kaggle Cloud GPU: Probed 2x NVIDIA Tesla T4 GPUs, functional NVENC acceleration, and 52.34s cold boot download.

### Test Evidence
- Command: `./venv/bin/pytest tests/unit/ tests/performance/`
- Result: **461 passed in 52.18s** (0 regressions).
- Empirical Baseline Artifacts: `docs/performance/baseline.md`, `docs/performance/baseline_data.json`, `docs/performance/baseline_data.csv`.

---

## Phase 5 — P3 Targeted Performance Optimizations
- **Status**: PASS
- **Agents Used / Max Concurrency**: 2 (worker_a7: TTS Specialist, worker_a8: Render/Cloud Specialist)
- **Component**: `src/tts/catalog.py`, `src/tts/engine.py`, `src/render/video_engine.py`, `src/production/hardware.py`, `scripts/benchmark/measure_render_speedup.py`, `tests/unit/test_render_optimizations.py`, `docs/performance/render_optimization_metrics.json`
- **Date**: 2026-11-09T00:30:00Z

### Summary of Changes

1. **Rendering Bottleneck #1 Optimization (worker_a8)**:
   - **Single-Frame Static Motion Hold**: In `src/render/video_engine.py`, added `create_static_canvas()` compositing the 16:9 canvas (including background blur) once in-process via Pillow for static segments, feeding the canvas to FFmpeg via `-loop 1`. Bypassed the costly per-frame `split=2`, `scale`, and `boxblur=25:5` filtergraph completely.
   - **In-Memory Canvas Cache**: Added thread-safe `_canvas_cache` in `VideoEngine` keyed on image path, mtime, dimensions, video mode, motion type, and focus box, avoiding duplicate composites for repeated panels.
   - **VideoToolbox Mutex & Robust Fallback**: Serialized Apple VideoToolbox sessions via `_videotoolbox_lock` while allowing worker threads to prepare canvases in parallel; added automated fallback to `libx264` on runtime encoding exceptions.
   - **Measured Empirical Result**: Rendering time dropped from 28.121s to 9.031s on 20 clips (**3.11x speedup, 67.9% wall-clock reduction**).

2. **Neural TTS Bottleneck #2 Optimization (worker_a7)**:
   - **Voice Memoization**: Applied `@functools.lru_cache(maxsize=512)` to voice catalog resolvers in `src/tts/catalog.py`.
   - **Direct In-Memory PCM WAV Packaging**: Implemented `_encode_wav_16bit_pcm()` in `src/tts/engine.py` constructing 44-byte RIFF headers directly, computing duration and SHA-256 in memory without disk read passes.
   - **Streamed FFmpeg atempo**: Streamed raw PCM via stdin pipe directly to seekable destination files for speed adjustments, eliminating intermediate temporary files.
   - **Decoupled Inference Lock Scope**: Restricted `_infer_lock` strictly to the neural forward pass in `NeuralTTSBackend.synthesize`.
   - **Thread-Safe Audio Metric Cache**: Cached durations and checksums in `_AUDIO_METRIC_CACHE`.
   - **Batch Synthesis Pipeline**: Added `VieNeuEngine.synthesize_batch(tasks, workers=2)`.
   - **Measured Empirical Result**: Small workload TTS dropped from 44.3936s to 40.9408s (**7.8% latency reduction, 8.7% batch throughput speedup**).

### Test Evidence
- Combined Render Suite: **89 passed in 13.61s**.
- Full Test Suite: **590 passed in 119.97s (0 regressions)**.
- Empirical Metrics: `docs/performance/render_optimization_metrics.json`.

---

## Phase 6 — Independent Verification & Release Readiness
- **Status**: PASS (Technical Acceptance) / PENDING HUMAN REVIEW (Editorial Quality) / UNVERIFIED (Cloud Worker Dataset Defect)
- **Agents Used / Max Concurrency**: 1 (worker_a9: Independent Technical Verifier)
- **Component**: `docs/release_readiness.md`, `tests/verification/test_matrix_verification.py`, `docs/engineering/implementation_log.md`
- **Date**: 2026-11-09T06:21:00Z

### Summary of Changes

1. **Full Test Suite & Test Matrix Verification**:
   - Executed full workspace unit, adversarial, and performance test suites: **592 passed in 109.40s**.
   - Authored and executed dedicated acceptance test matrix suite `tests/verification/test_matrix_verification.py`: **12 passed in 5.91s**.
   - Verified scenarios T01 through T12 covering multi-panel rendering, omitted panels, missing panel fail-fast, per-segment voice/speed/emotion, opaque `seq_id` (`002_1`), batch TTS count integrity, corrupt artifact handling, cache hit reuse, render-only decoupling from TTS, Local vs Cloud parity, skip-preview unreviewed status, and stream integrity.
   - Total passing tests: **604 tests (100% green, 0 regressions)**.

2. **Kaggle Cloud GPU Telemetry & Live Job Probe**:
   - Verified live Kaggle API authentication as `cuongnguyen1802`.
   - Verified completed hardware probe kernels: `manga-recap-speedup-verify` (NVENC 1080p hardware encode verified on 2x Tesla T4) and `manga-recap-vieneu-test` (VieNeu v3 Turbo initialized on Linux 6.18, Python 3.13).
   - Executed live chapter production job `manga-recap-benchmark-small-35-ch1` version 2 on Kaggle Tesla T4.
   - Identified and isolated defect in `src/cloud/kaggle_service.py` (lines 672–684): when uncompressed datasets are detected in `/kaggle/input/`, script metadata is copied to workspace but `audio/clips/` directory copy is omitted, causing the audio cache verification step to fail fast. Documented root cause and recommended fix.

3. **Release Readiness Documentation**:
   - Published `docs/release_readiness.md` formally separating product readiness into:
     - `technical_ready`: **YES (PASSED)**.
     - `human_quality_approved`: **PENDING HUMAN REVIEW**.
     - `cloud_live_verified`: **UNVERIFIED / BLOCKED** (on cloud worker uncompressed audio copy defect).
   - Formally documented zero automated narrative quality or LLM-as-judge scoring in compliance with the Integrity Mandate.

### Acceptance Evidence
- Test Results: `tests/verification/test_matrix_verification.py` (12/12 passed), full pytest run (592/592 passed).
- Release Readiness Document: `docs/release_readiness.md`.
- Live Cloud Kernel Output: `cuongnguyen1802/manga-recap-speedup-verify` and `cuongnguyen1802/manga-recap-benchmark-small-35-ch1` logs.

### Remaining Risks & Recommended Next Steps
1. **Cloud Worker Audio Copy Patch**: Patch lines 672–684 in `src/cloud/kaggle_service.py` to copy `found_script.parent / "audio" / "clips"` into `chapter_dir / "audio" / "clips"` when uncompressed datasets are present.
2. **Editorial Sign-off**: Human Reviewer reviews generated `review_pack.html` for target chapters to provide editorial approval (`human_quality_approved`).
