# Project: Manga-Recap-Skill V4 Production Rollout

## Architecture
- **Cognition & Vision**: Agent-Native execution (visual OCR inspection, character consistency, storytelling recap scripting, camera direction). Zero cloud LLM/VLM APIs in Python core.
- **Series Setup & Runtime Governance**: Strict Form Mode enforcing 10 explicit user answers (Q01–Q10) via `ask_question`. Zero implicit defaults. Cryptographic snapshot validation via `series_setup_answers.json`.
- **Motion & Framing Engine**: Canonical `VisualPlan` and `Shot` engine. Strictly monotonic top-to-bottom motion (`y(t+dt) >= y(t)`), velocity capped (`<= 120 px/s`), zero reverse rubber-banding. Subject-centric focus boxes prioritizing characters over speech bubbles. Tall panel auto-splitting (`H/W > 2.0`).
- **Cloud Execution**: One-Session Kaggle Dual-T4 Engine (`machine_shape="NvidiaTeslaT4"`). GPU 0 dedicated to VieNeu-TTS v3 Turbo batch synthesis (PyTorch CUDA device 0); GPU 1 dedicated to FFmpeg hardware NVENC encoding (`cuda` device 1). Single payload upload -> single Kaggle execution -> single bundle download. Chapter 1 preview gate mandatory before batch run.
- **Data Persistence & Archive**: Self-contained local JSON (`data/projects/<slug>/`). Offsite private Hugging Face Dataset Archive (`Cuong2004/manga-recap-archive`) with recursive SHA-256 manifest and verified sandbox restore. Safe local cleanup permitted ONLY after both YouTube publication and HF archive restore are verified.

## Feature Inventory
| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| 1 | R1.1: Strict Form 10/10 Questions Schema | `SeriesSetupAnswers`, `QuestionId` enum Q01..Q10, user provenance, snapshot hash in `src/core/series_setup.py` | M1 (PR-01) | ORIGINAL_REQUEST §R1, docs/V4_Plan §PR-01 |
| 2 | R1.2: Setup CLI & Runtime Validator | `manga-recap setup [validate\|record\|show\|migrate]` subcommands, `require_series_setup` gate at pipeline entry points | M1 (PR-01) | ORIGINAL_REQUEST §R1, docs/V4_Plan §PR-01 |
| 3 | R1.3: Elimination of Implicit Defaults | Remove default fallbacks in `ProjectConfig`, `crawl`, `batch`, `render`, `cloud`; fail closed on missing answers | M1 (PR-01) | ORIGINAL_REQUEST §R1, docs/V4_Plan §PR-01 |
| 4 | R1.4: Cloud Import Lint Fix | Convert static `googleapiclient.http` import in `src/cli/thumbnail_cmd.py` to dynamic import | M1 (PR-01) | explorer_survey_1 handoff |
| 5 | R2.1: Canonical VisualPlan & Shot Models | `VisualPlan`, `Shot`, and `visual_role` in `src/render/visual_policy.py` shared across local and Kaggle | M2 (PR-02) | ORIGINAL_REQUEST §R2, docs/V4_Plan §PR-02 |
| 6 | R2.2: Monotonic Top-Down Motion & Anti-Jitter | Clamp progress `p = min(1, max(0, t/T))`, eliminate cosine reverse rebound, cap speed <= 120 px/s, alias PAN_UP | M2 (PR-02) | ORIGINAL_REQUEST §R2, docs/V4_Plan §PR-02 |
| 7 | R2.3: Subject-Centric Framing & Text Avoidance | Enforce `FocusBox` dimensions (w>=0.35, h>=0.30), safe zone clamping (fy>=0.30), speech bubble suppression | M2 (PR-02) | ORIGINAL_REQUEST §R2, docs/V4_Plan §PR-02 |
| 8 | R2.4: Tall Panel Auto-Splitting & Sliver Filter | Split panels with H/W > 2.0 into overlapping sub-panels, filter blank/low-variance slivers < 80px | M2 (PR-02) | ORIGINAL_REQUEST §R2, docs/V4_Plan §PR-02 |
| 9 | R3.1: Dual-T4 GPU Allocation & Isolation | GPU 0 dedicated to VieNeu-TTS v3 Turbo; GPU 1 dedicated to FFmpeg NVENC hardware video render | M3 (PR-03) | ORIGINAL_REQUEST §R3, docs/V4_Plan §PR-03 |
| 10 | R3.2: One-Session Multi-Chapter Lifecycle | Chapter 1 preview gate -> single upload `series_payload.tar.gz` -> single session -> single download | M3 (PR-03) | ORIGINAL_REQUEST §R3, docs/V4_Plan §PR-03 |
| 11 | R3.3: Pipelined Overlap & Micro-Batching | Group segments by voice/length, dynamic batch size backoff, overlap TTS(N+1) with Render(N) | M3 (PR-03) | ORIGINAL_REQUEST §R3, docs/V4_Plan §PR-03 |
| 12 | R3.4: Kaggle Machine Shape Specification | Set `"machine_shape": "NvidiaTeslaT4"` in `kernel-metadata.json` for guaranteed 2x T4 GPU hardware | M3 (PR-03) | ORIGINAL_REQUEST §R3, docs/V4_Plan §PR-03 |
| 13 | R4.1: Private Hugging Face Dataset Archive | `src/archive/hf_archive.py` with `huggingface_hub.HfApi`, private dataset `Cuong2004/manga-recap-archive` | M4 (PR-04) | ORIGINAL_REQUEST §R4, docs/V4_Plan §PR-04 |
| 14 | R4.2: Cryptographic Archive Manifest & Receipts | Recursive SHA-256 whitelist manifest, exclude secrets/tokens, store immutable local archive receipt | M4 (PR-04) | ORIGINAL_REQUEST §R4, docs/V4_Plan §PR-04 |
| 15 | R4.3: Independent Verification & Restore Test | `manga-recap archive verify`, `manga-recap youtube verify`, sandbox round-trip test restore before cleanup | M4 (PR-04) | ORIGINAL_REQUEST §R4, docs/V4_Plan §PR-04 |
| 16 | R4.4: Fail-Closed Safe Cleanup & Tombstone | Enforce two-receipt gate (YouTube + HF), require `--confirm-project`, leave persistent `tombstone.json` | M4 (PR-04) | ORIGINAL_REQUEST §R4, docs/V4_Plan §PR-04 |
| 17 | R5.1: End-to-End Regression Test Suite | 100% pass on all existing suites (649 tests) plus new test suites for PR-01 through PR-04 | M5 (PR-05) | ORIGINAL_REQUEST §R5, docs/V4_Plan §PR-05 |
| 18 | R5.2: Empirical Dual-T4 Benchmark Report | Measure VRAM allocation, TTS throughput, NVENC FPS, wall-clock speedup on multi-chapter workload | M5 (PR-05) | ORIGINAL_REQUEST §R5, docs/V4_Plan §PR-05 |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| M1 | PR-01: Strict Form Runtime Validation | Features 1, 2, 3, 4 (`src/core/series_setup.py`, `src/cli/setup_cmd.py`, `thumbnail_cmd.py`) | Survey complete | DONE |
| M2 | PR-02: Visual-First Motion & Framing | Features 5, 6, 7, 8 (`src/render/visual_policy.py`, `camera.py`, `video_engine.py`, `streaming_segmenter.py`) | M1 (contracts frozen) | DONE |
| M3 | PR-03: Kaggle Dual-T4 Pipeline Engine | Features 9, 10, 11, 12 (`src/cloud/kaggle_service.py`, `dual_t4_orchestrator.py`, `cloud_cmd.py`) | M2 (VisualPlan parity) | DONE |
| M4 | PR-04: Private HF Archive & Safe Cleanup | Features 13, 14, 15, 16 (`src/archive/hf_archive.py`, `archive_cmd.py`, `clean_cmd.py`, `youtube_cmd.py`) | M1 (contracts frozen) | DONE |
| M5 | PR-05: E2E Acceptance & Dual-T4 Benchmark | Features 17, 18 (Full test suite 100% pass, Kaggle benchmark, release report) | M1, M2, M3, M4 | DONE |

## Interface Contracts

### 1. Series Setup Contract (PR-01)
- Storage document: `data/projects/<slug>/series_setup_answers.json`
- Model: `SeriesSetupAnswers`
- Required keys: `Q01` through `Q10`, all with `answer_source == "user"`
- Snapshot hash: `SHA-256` of canonical sorted JSON of answers
- Sync revision: `setup_revision: int` matching `ProjectConfig.setup_revision`
- Fail-closed gate: `require_series_setup(project_slug: str)` raises `CliValidationError` if incomplete

### 2. VisualPlan & Motion Contract (PR-02)
- Engine: `src/render/visual_policy.py` -> `VisualPlan`, `Shot`
- Motion constraint: $y(t + \Delta t) \ge y(t)$ at all frames; $v \le 120\text{ px/s}$
- Time progression: $p(t) = \min(1.0, \max(0.0, t / T))$ with smoothstep easing $p^2(3 - 2p)$
- Focus box: $x_{max} - x_{min} \ge 0.35$, $y_{max} - y_{min} \ge 0.30$, $f_y \ge 0.30$ on portrait panels
- Panel splitting: Panels with height/width $> 2.0$ auto-split into overlapping segments with $0.65 \le W/H \le 2.0$

### 3. Kaggle Dual-T4 Pipeline Contract (PR-03)
- CLI command: `manga-recap cloud produce-series --project <slug> --chapters <ch_list> --json`
- Machine shape: `"machine_shape": "NvidiaTeslaT4"`
- Process 0 (TTS): `CUDA_VISIBLE_DEVICES=0`, `Vieneu(mode="v3turbo")`, emits `chapter_{N}_audio_ready.json`
- Process 1 (Render): `CUDA_VISIBLE_DEVICES=1`, polls readiness, renders with `h264_nvenc`
- Chapter 1 Gate: Refuses series batch unless Chapter 1 has `human_review_status == "approved"`

### 4. Hugging Face Archive & Safe Cleanup Contract (PR-04)
- Repository: Private dataset `Cuong2004/manga-recap-archive` via `huggingface_hub.HfApi`
- Manifest: `archive_manifest.json` containing recursive SHA-256 of all series assets (no secrets)
- Receipts:
  1. `archive_receipt.json`: Commit SHA, timestamp, verified file count
  2. `youtube_verified_receipt.json`: Verified YouTube video ID, status via `videos.list`
- Cleanup rule: `manga-recap archive finalize --project <slug>` strictly blocked unless BOTH receipts are verified and round-trip test restore passes
- Tombstone: Persistent `tombstone.json` left in local project folder after cleanup

## Code Layout
- `src/core/series_setup.py`: Pydantic models and validator for 10-question setup
- `src/cli/setup_cmd.py`: CLI handlers for `manga-recap setup`
- `src/render/visual_policy.py`: Shared canonical `VisualPlan` and `Shot` generator
- `src/render/camera.py`: Monotonic camera motion and Ken Burns interpolation
- `src/render/video_engine.py`: Single-pass canvas rendering and video muxing
- `src/segment/streaming_segmenter.py`: Tall panel auto-splitting and blank panel filtering
- `src/cloud/kaggle_service.py`: Kaggle cloud API client and session orchestrator
- `src/cloud/dual_t4_orchestrator.py`: In-container multi-chapter dual-GPU supervisor
- `src/archive/hf_archive.py`: Hugging Face dataset upload, verification, and restore engine
- `src/cli/archive_cmd.py`: CLI handlers for `manga-recap archive`
- `src/cli/clean_cmd.py`: Safe cleanup with two-receipt verification and tombstone
- `src/cli/youtube_cmd.py`: YouTube publishing and independent post-upload verification
- `tests/unit/`: Unit tests for setup, visual policy, cloud, archive, cleanup
- `tests/e2e/`: End-to-end integration test suites
