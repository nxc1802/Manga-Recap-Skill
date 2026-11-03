# Project: Manga Recap Agentic Skill V3

## Architecture
The system follows a strict decoupled architecture:
1. **Agentic Skill Layer**: Coding agents (`.agents/skills/manga-recap/`, `AGENTS.md`) provide intelligent orchestration, visual OCR/panel understanding, character identification, and narrative recap scripting. Direct LLM/VLM calls are zero in the Python codebase.
2. **Deterministic Offline Tooling Layer**: Standalone Python CLI tools (`crawl`, `inspect`, `validate`, `tts`, `tts_benchmark`, `render`, `compile`, `status`) provide reliable, offline utilities.
3. **Local JSON & File System Storage**: Self-contained per-chapter storage (`project.json`, `chapter.json`, `script.json`, `characters.json`, `story_summary.md`, `manifest.json`), using atomic writes (`atomic_write_json`), single-writer file locking (`FileLock`), content hashing (`SHA-256`), and stage checkpoints (`pending`, `running`, `completed`, `failed`, `needs_review`). Zero Supabase/SQL dependencies.
4. **Local Audio Engine**: VieNeu-TTS v3 Turbo running locally via Python SDK / ONNX / PyTorch, producing 48 kHz uncompressed WAV master audio with utterance-level SHA-256 caching.
5. **Video Engine V2**: 7 cinematic camera motion modes (static, pan_left, pan_right, pan_up, pan_down, zoom_in, zoom_out, focus_crop) calculated from actual measured waveform durations, dynamic FFmpeg filter chains, BGM ducking, and synchronized SRT subtitles.
6. **E2E Testing & Quality Framework**: Independent dual-track opaque-box E2E test harness covering Tiers 1–4, plus Tier 5 adversarial testing.

```
+-------------------------------------------------------------------------------+
|                       Agentic Skill (Coding Agent)                            |
|          Vision / OCR / Scripting / Narrative Quality / Human Gates           |
+------------------------------------+------------------------------------------+
                                     | CLI Invocation (JSON I/O & Exit Codes)
                                     v
+-------------------------------------------------------------------------------+
|                      Deterministic Python CLI Suite                           |
|  crawl | inspect | validate | tts | tts_benchmark | render | compile | status   |
+---------+-------------------+------+-----------------+--------+---------------+
          |                   |                        |        |
          v                   v                        v        v
   [Crawler V2]     [Local JSON Store]         [VieNeu-TTS v3] [Video Engine V2]
   Long-strip       project.json               Offline SDK     Camera motions
   streaming        chapter.json               48 kHz WAV      Audio-synced SRT
   Contact sheets   script.json                SHA-256 cache   BGM ducking
                    manifest.json
+-------------------------------------------------------------------------------+
```

## Code Layout
```
/Volumes/WorkSpace/Factory/Manga Review/
├── .agents/
│   ├── skills/
│   │   └── manga-recap/
│   │       ├── SKILL.md
│   │       ├── rules/
│   │       └── templates/
│   │           ├── script.json.template
│   │           └── project.json.template
│   └── teamwork/                    # Orchestrator & agent metadata ONLY
├── AGENTS.md                        # Root agent guidelines and skill discovery
├── config.yaml                      # Local engine defaults and render profiles
├── data/
│   └── projects/
│       └── <project_slug>/
│           ├── project.json
│           ├── characters.json
│           ├── story_summary.md
│           └── chapters/
│               └── <chapter_num>/
│                   ├── chapter.json
│                   ├── manifest.json
│                   ├── script.json
│                   ├── raw/
│                   ├── panels/
│                   ├── contact_sheets/
│                   ├── audio/
│                   ├── subtitles/
│                   └── video/
├── docs/
│   ├── plan.md
│   ├── ARCHITECTURE.md
│   └── lab/
│       └── index.html               # TTS evaluation lab interface
├── src/
│   ├── cli/
│   │   ├── __init__.py
│   │   ├── main.py                  # Unified CLI entry point
│   │   ├── crawl.py
│   │   ├── inspect_cmd.py
│   │   ├── validate_cmd.py
│   │   ├── tts_cmd.py
│   │   ├── tts_benchmark_cmd.py
│   │   ├── render_cmd.py
│   │   ├── compile_cmd.py
│   │   └── status_cmd.py
│   ├── core/
│   │   ├── __init__.py
│   │   ├── config.py                # Pydantic settings without Supabase
│   │   ├── storage.py               # Local JSON atomic storage & file locks
│   │   ├── schemas.py               # Pydantic models for all state files
│   │   └── utils.py                 # Media & hashing utilities
│   ├── crawler/
│   │   ├── __init__.py
│   │   ├── crawler.py
│   │   └── segmenter.py             # Memory-safe long-strip segmentation V2
│   ├── tts/
│   │   ├── __init__.py
│   │   ├── engine.py                # VieNeu-TTS v3 Turbo local runner
│   │   ├── cache.py                 # SHA-256 utterance cache
│   │   └── lab.py                   # Screening & emotion evaluation lab
│   └── render/
│       ├── __init__.py
│       ├── camera.py                # Camera motion math & crop calculations
│       ├── video_engine.py          # Video Engine V2 with FFmpeg filterchains
│       ├── subtitle.py              # Word/chunk subtitle timing & SRT generator
│       └── audio_mixer.py           # Audio concatenation & BGM ducking
├── tests/
│   ├── conftest.py
│   ├── unit/
│   ├── integration/
│   └── e2e/
│       ├── harness.py
│       └── test_suites/
└── pyproject.toml / requirements.txt
```

## Feature Inventory
| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| 1 | Baseline Git Tag & Snapshot | Create `v1.0-baseline` tag and manifest of pre-migration state | PR-00 | docs/plan.md § IV.PR-00 |
| 2 | Legacy Dependency Audit | Inventory all Supabase, Cloud TTS, and unpinned dependencies | PR-00 | docs/plan.md § IV.PR-00 |
| 3 | Rollback Safety Script | Document and test clean rollback procedure to baseline tag | PR-00 | docs/plan.md § IV.PR-00 |
| 4 | JSON Schema Specifications | Pydantic models for `project.json`, `chapter.json`, `script.json`, `characters.json`, `story_summary.md` | PR-01 | docs/plan.md § IV.PR-01 |
| 5 | Atomic File Writer | Safe atomic write with temporary file and atomic replace | PR-01 | docs/plan.md § IV.PR-01 |
| 6 | File Lock Concurrency | Single-writer file locking for JSON writes using FileLock | PR-01 | docs/plan.md § IV.PR-01 |
| 7 | Stage Checkpoints & Recovery | Checkpoint state tracking (`pending`, `running`, `completed`, `failed`, `needs_review`) | PR-01 | docs/plan.md § IV.PR-01 |
| 8 | Content Fingerprinting | SHA-256 content hashing on panel images and script text | PR-01 | docs/plan.md § IV.PR-01 |
| 9 | Supabase Layer Deletion | Completely remove `supabase_client.py` and all Supabase imports/keys | PR-01 | docs/plan.md § IV.PR-01 |
| 10 | Legacy Data Migration Utility | Optional migration tool to convert legacy Supabase records or wipe script | PR-01 | docs/plan.md § IV.PR-01 |
| 11 | Unified CLI Entry Point | Single CLI entry point `manga-recap` dispatching subcommands | PR-02 | docs/plan.md § IV.PR-02 |
| 12 | CLI Exit Codes | Standard POSIX exit codes (0=success, 1=generic, 2=validation, 3=IO, 4=dependency) | PR-02 | docs/plan.md § IV.PR-02 |
| 13 | Structured JSON CLI Output | `--json` flag on all subcommands emitting standardized JSON response | PR-02 | docs/plan.md § IV.PR-02 |
| 14 | Fix Cleanup Calls Defect | Eliminate nonexistent `cleanup_temp_files()` and implement proper cleanup | PR-02 | docs/plan.md § IV.PR-02 |
| 15 | FFmpeg Returncode Enforcement | Check `returncode` and raise/report error on all FFmpeg subprocess runs | PR-02 | docs/plan.md § IV.PR-02 |
| 16 | Output Path Versioning Fix | Fix `output_suffix` handling in video path resolution | PR-02 | docs/plan.md § IV.PR-02 |
| 17 | CLI Status Subcommand | `manga-recap status` returning complete stage and checkpoint state | PR-02 | docs/plan.md § IV.PR-02 |
| 18 | CLI Validate Subcommand | `manga-recap validate` verifying project and chapter integrity against schemas | PR-02 | docs/plan.md § IV.PR-02 |
| 19 | CLI Inspect Subcommand | `manga-recap inspect` reporting asset counts, dimensions, and audio durations | PR-02 | docs/plan.md § IV.PR-02 |
| 20 | Agentic Skill Specification | Standard `SKILL.md` under `.agents/skills/manga-recap/` following Agent Skill standard | PR-03 | docs/plan.md § IV.PR-03 |
| 21 | Root AGENTS.md | Project-level instructions for coding agents with skill index | PR-03 | docs/plan.md § IV.PR-03 |
| 22 | Vision Workflow Prompt Templates | Standard prompts for panel description, OCR verification, and recap scripting | PR-03 | docs/plan.md § IV.PR-03 |
| 23 | Script JSON Templates | Ready-to-populate template for `script.json` with emotion/motion hints | PR-03 | docs/plan.md § IV.PR-03 |
| 24 | Streaming Long-Strip Slicer | Page-by-page slice without full in-memory canvas concatenation | PR-04 | docs/plan.md § IV.PR-04 |
| 25 | Downsampled Gutter Detection | Fast whitespace/blackspace gutter detection using downsampled luminance | PR-04 | docs/plan.md § IV.PR-04 |
| 26 | Panel Manifest Generator | Emit `manifest.json` with panel bounding boxes, aspect ratios, and filenames | PR-04 | docs/plan.md § IV.PR-04 |
| 27 | Contact Sheet Grid Builder | Generate downscaled contact sheet images for multi-panel agent vision review | PR-04 | docs/plan.md § IV.PR-04 |
| 28 | Crawler Rate Limiting & Retry | Deterministic retry, exponential backoff, and polite delays for webtoon scraping | PR-04 | docs/plan.md § IV.PR-04 |
| 29 | VieNeu-TTS Python SDK Wrapper | Local runner wrapping `vieneu` library with PyTorch/ONNX support | PR-05 | docs/plan.md § IV.PR-05 |
| 30 | 48 kHz WAV Output | Master audio synthesized at 48,000 Hz 16-bit PCM uncompressed WAV | PR-05 | docs/plan.md § IV.PR-05 |
| 31 | Utterance SHA-256 Cache | Content-addressed caching keyed by `hash(text, voice_id, speed, sample_rate)` | PR-05 | docs/plan.md § IV.PR-05 |
| 32 | Waveform Duration Extraction | Measure exact duration from audio header rather than estimating | PR-05 | docs/plan.md § IV.PR-05 |
| 33 | Graceful Degrade / Fallback Voice | Fall back to default voice if requested preset fails or is missing | PR-05 | docs/plan.md § IV.PR-05 |
| 34 | Cloud TTS Elimination | Remove Edge-TTS, Gemini REST TTS, and OpenAI REST TTS from codebase | PR-05 | docs/plan.md § IV.PR-05 |
| 35 | Voice Screening Runner | Benchmark script generating samples for all 25 VieNeu preset voices | PR-06 | docs/plan.md § IV.PR-06 |
| 36 | Emotion Benchmark Suite | 8 emotion benchmark sentences synthesized and evaluated | PR-06 | docs/plan.md § IV.PR-06 |
| 37 | Hardware Performance Telemetry | Measure and log RTF (Real-Time Factor), latency, memory, and throughput | PR-06 | docs/plan.md § IV.PR-06 |
| 38 | Local HTML Evaluation Lab | Standalone HTML/JS interface for side-by-side listening and grading | PR-06 | docs/plan.md § IV.PR-06 |
| 39 | Evaluation Scoring Rubric | 5-dimension rubric (naturalness, intelligibility, emotion, consistency, audio quality) | PR-06 | docs/plan.md § IV.PR-06 |
| 40 | Camera Motion Modes (7 types) | Support `static`, `pan_left`, `pan_right`, `pan_up`, `pan_down`, `zoom_in`, `zoom_out`, `focus_crop` | PR-07 | docs/plan.md § IV.PR-07 |
| 41 | Audio-Duration Timeline Sync | Scale clip duration and pan/zoom speed exactly to measured utterance duration | PR-07 | docs/plan.md § IV.PR-07 |
| 42 | Focus Coordinate Math | Convert normalized `(focus_x, focus_y, crop_scale)` to dynamic FFmpeg crop filters | PR-07 | docs/plan.md § IV.PR-07 |
| 43 | Synchronized Subtitle Generator | Generate `.srt` subtitles perfectly aligned to utterance start/end times | PR-07 | docs/plan.md § IV.PR-07 |
| 44 | BGM Ducking & Audio Mixing | Automatic volume ducking during speech utterances and smooth crossfade | PR-07 | docs/plan.md § IV.PR-07 |
| 45 | FFmpeg Video Stream Validation | Validate resulting MP4 via `ffprobe` (audio/video codecs, resolution, sync) | PR-07 | docs/plan.md § IV.PR-07 |
| 46 | End-to-End Test Chapter Runner | Full workflow on Warrior Grandpa Ep 2 (crawl/import -> segment -> script -> tts -> render) | PR-08 | docs/plan.md § IV.PR-08 |
| 47 | Human Review Gate 1 Integration | Voice & emotion approval checkpoint before production generation | PR-08 | docs/plan.md § IV.PR-08 |
| 48 | Human Review Gate 2 Integration | Preview video review checkpoint before final high-res encode | PR-08 | docs/plan.md § IV.PR-08 |
| 49 | Artifact Verification Matrix | Verify presence and validity of all generated JSONs, WAVs, SRT, and MP4 | PR-08 | docs/plan.md § IV.PR-08 |
| 50 | Comprehensive README.md Rewrite | Documentation for installation, Agentic Skill usage, CLI reference, and architecture | PR-09 | docs/plan.md § IV.PR-09 |
| 51 | Legacy Cleanup & Pruning | Remove `wipe_db.py`, Supabase configs, unused scripts, and outdated docs | PR-09 | docs/plan.md § IV.PR-09 |
| 52 | Clean-Environment Verification | Verify complete pytest pass in pristine venv without legacy packages | PR-09 | docs/plan.md § IV.PR-09 |
| 53 | E2E Testing Harness & Runner | Opaque-box CLI test runner executing Tiers 1–4 tests with zero internal imports | E2E-TRACK | docs/plan.md § V & Pattern |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| M1 | Baseline & Local JSON Storage Migration | PR-00 (V1 snapshot, audit, rollback) + PR-01 (Local JSON engine, schemas, atomic writes, file locks, checkpoints, Supabase removal) | none | DONE |
| M2 | Deterministic CLI Suite & Agentic Skill | PR-02 (Unified CLI manga-recap, inspect, validate, tts, render, status, exit codes, bug fixes) + PR-03 (Agentic Skill SKILL.md, AGENTS.md, prompt & script templates) | M1 | DONE |
| M3 | Media Ingestion & VieNeu-TTS with Eval Lab | PR-04 (Streaming long-strip segmenter, manifest.json, contact sheets) + PR-05 (VieNeu-TTS local SDK, 48 kHz WAV, SHA-256 cache, cloud removal) + PR-06 (TTS Eval Lab, 25 voice screening, 8 emotion benchmarks, HTML interface) | M1, M2 | DONE |
| M4 | Cinematic Video Engine V2 | PR-07 (7 camera motion modes, duration timeline sync, focus coordinates, SRT subtitles, BGM ducking, ffprobe validation) | M3 | PLANNED |
| M5 | End-to-End Verification & Release Readiness | PR-08 (E2E run on Warrior Grandpa Ep 2, Human Gates 1 & 2) + PR-09 (README.md rewrite, dead code removal, pytest release verification) | M2, M3, M4, ME2E | PLANNED |
| ME2E | Opaque-Box E2E Testing Track | Requirement-driven opaque-box test infra, runner, and test suites across Tiers 1–4, publishing TEST_READY.md | M1 | PLANNED |

## Interface Contracts

### 1. Storage Contract (`src/core/storage.py` & `src/core/schemas.py`)
- `atomic_write_json(file_path: Path, data: dict | BaseModel) -> None`
  - Writes data to `.tmp.{pid}.{uuid}` then atomically renames to `file_path`.
- `read_json(file_path: Path) -> dict`
  - Reads and parses JSON file with error handling (`StorageFileNotFoundError`, `StorageCorruptError`).
- `acquire_lock(resource_path: Path) -> FileLock`
  - Acquires exclusive write lock via `.lock` file to prevent concurrent corruption.
- `update_checkpoint(chapter_path: Path, stage: str, status: CheckpointStatus, error: str | None = None) -> None`
  - Stages: `crawl`, `segment`, `script`, `tts`, `render`, `finalize`.
  - Statuses: `pending`, `running`, `completed`, `failed`, `needs_review`.

### 2. CLI Contract (`src/cli/main.py`)
- All commands support `--json` and `--verbose`.
- Standard output schema when `--json` is specified:
  ```json
  {
    "status": "success" | "error",
    "command": "<cmd_name>",
    "exit_code": 0,
    "data": { ... },
    "error": null | { "code": "<ERR_CODE>", "message": "<str>" },
    "duration_seconds": 1.23
  }
  ```
- Subcommands:
  - `crawl --project <p> --chapter <c> --url <u> [--json]`
  - `inspect --project <p> --chapter <c> [--json]`
  - `validate --project <p> [--chapter <c>] [--json]`
  - `tts --project <p> --chapter <c> [--force] [--json]`
  - `tts_benchmark [--out-dir <d>] [--voices <list>] [--json]`
  - `render --project <p> --chapter <c> [--quality preview|final] [--json]`
  - `compile --project <p> --chapters <c1,c2> [--output <path>] [--json]`
  - `status --project <p> [--chapter <c>] [--json]`

### 3. TTS Engine Contract (`src/tts/engine.py`)
- `VieNeuEngine`:
  - `synthesize_utterance(text: str, voice_id: str, speed: float = 1.0) -> UtteranceResult`
  - `UtteranceResult`: `{ "audio_path": Path, "duration_seconds": float, "sample_rate": 48000, "cached": bool, "checksum": str }`
  - Cache formula: `cache_key = sha256(f"{text}|{voice_id}|{speed}|{sample_rate}".encode('utf-8')).hexdigest()`

### 4. Video Engine V2 Contract (`src/render/video_engine.py`)
- `VideoEngine`:
  - `render_panel(panel_image: Path, motion_mode: str, focus_box: tuple[float, float, float, float] | None, duration: float, output_path: Path) -> Path`
  - `assemble_chapter(panels: list[PanelClipConfig], bgm_path: Path | None, srt_path: Path, output_path: Path) -> Path`
  - Validates output using `ffprobe`: checks video codec (`h264`), audio codec (`aac`), resolution (`1280x720`), frame rate (`30`), and duration matching audio sum within ±0.1s.

### 5. Agentic Skill Contract (`.agents/skills/manga-recap/SKILL.md`)
- Invocation: Coding agent reads contact sheets and OCR data, reasons about character actions and tone, generates `script.json` obeying the schema, assigns voice IDs and motion modes per segment, and invokes CLI tools for deterministic execution.
