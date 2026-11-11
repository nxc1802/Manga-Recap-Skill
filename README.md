# Manga Recap Agentic Skill V3

> V4 correctness work is **draft**, with real chapter and live Kaggle gates pending. See [current evidence and blockers](docs/V4_Correctness_Status.md). Unit fixtures and test counts are not production verification.

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Architecture](https://img.shields.io/badge/architecture-Agent--Native%20Decoupled-green.svg)](docs/V4_Correctness_Improvement_Plan.md)
[![Audio](https://img.shields.io/badge/TTS-VieNeu--TTS%20v3%20Turbo%20Local-orange.svg)](docs/lab/index.html)
[![Storage](https://img.shields.io/badge/storage-Local%20Atomic%20JSON-purple.svg)](src/core/storage.py)
[![Tests](https://img.shields.io/badge/tests-306%20passed-brightgreen.svg)](tests/)

An autonomous, agent-native manga and webtoon recap video creation pipeline. Engineered around a **strict separation of intelligence and execution**: AI coding agents handle vision, visual OCR, narrative recap scripting, and continuity; deterministic offline Python CLI tools handle media ingestion, long-strip streaming segmentation, local VieNeu-TTS v3 Turbo synthesis, and cinematic FFmpeg video rendering.

---

## 🌟 Core Architectural Principles

1. **Decoupled Agent-Native Cognition**:
   - The core Python codebase contains **zero direct calls to external Cloud LLM/VLM APIs** (OpenAI, Gemini, OpenRouter) and **zero Cloud TTS APIs** (Edge-TTS, Cloud REST).
   - Visual interpretation of manga panels, character tracking, and Vietnamese narration scripts are executed directly by Coding Agents (Antigravity, OpenCode, Claude Code) using native vision/reasoning.
2. **Local Atomic JSON Storage**:
   - **Zero Supabase or database dependencies**: All project states, manifests, scripts, and checkpoints are stored locally in self-contained directories under `data/projects/<project_slug>/`.
   - Concurrency is governed by cross-platform single-writer `FileLock` (POSIX `fcntl` on macOS/Linux, `msvcrt` on Windows), atomic file replacement (`.tmp` write + `os.fsync` + atomic rename), and SHA-256 fingerprinting.
3. **Local VieNeu-TTS v3 Turbo Audio Engine**:
   - Master narration synthesized offline at uncompressed **48 kHz 16-bit PCM WAV** with SHA-256 utterance-level caching (`src/tts/`).
   - Hardware-accelerated local neural execution (Apple Silicon MPS / NVIDIA CUDA / CPU ONNX) delivering high throughput ($RTF \approx 0.197$, ~5.1x faster than real-time speech).
4. **Cinematic Video Engine V2**:
   - 8 dynamic camera motion modes (`static`, `pan_left`, `pan_right`, `pan_up`, `pan_down`, `zoom_in`, `zoom_out`, `focus_crop`, `vertical_scroll`).
   - Motion speeds and clip durations strictly calculated from exact binary audio waveform headers.
   - Dynamic FFmpeg `sidechaincompress` BGM volume ducking and millisecond-synchronized `.srt` subtitles.
5. **Streamlined 5-Stage Architecture & Consolidated Review Gates**:
   - Streamlined into 5 operational stages: **Ingest** $\to$ **Agent** $\to$ **Audio** $\to$ **Render** $\to$ **Export**.
   - **Gate 0 (Video Config)**: V4 Agent policy requires the user to explicitly answer **all 10 setup questions** via `ask_question` once per new series, without implicit defaults; validated selections then carry forward to later chapters. This is currently an **Agent instruction, not a CLI-level runtime guard**.
   - **Gate 1 (Voice Strategy)**: Approved **ONCE** per series in `voice_selection.json` and inherited by all chapters.
   - **Gate 2 (Preview Approval)**: V4 series policy requires a real **Chapter 1 preview approval** before full-series final processing. Later chapters can skip additional previews while retaining `unreviewed` review status; legacy V3 CLI still permits `--skip-preview` without this safeguard.
   - **Batch Mode**: Multi-chapter sequential execution (`manga-recap batch`) with chapter error isolation and single-chapter retry (`--retry-chapter`).

---

> **V4 implementation status:** Strict Form is currently documented in `.agents/skills/manga-recap/SKILL.md`; Python enforcement, visual-only top-down motion, single-session dual-T4 Kaggle jobs, and verified Hugging Face archiving/cleanup are planned in [the V4 implementation plan](docs/V4_Implementation_Plan.md) and are **not** existing runtime features.

## 🏗️ Repository Layout

```text
├── .agents/
│   └── skills/
│       └── manga-recap/
│           ├── SKILL.md                 # Agentic Skill entrypoint and 10-stage execution protocol
│           ├── rules/                   # Specialized rulebooks (narrative, vision, voice, review gates)
│           └── templates/               # Pydantic-validated JSON templates (script.json, project.json)
├── AGENTS.md                            # Global instructions and behavioral contracts for coding agents
├── docs/
│   ├── plan.md                          # Comprehensive roadmap and architectural specifications (PR-00 to PR-09)
│   └── lab/
│       ├── index.html                   # Zero-CDN standalone TTS evaluation lab interface
│       ├── benchmark_data.js            # Pre-compiled screening and emotion benchmark data
│       └── samples/                     # 74 master 48 kHz WAV sample clips across 25 preset voices
├── src/
│   ├── cli/                             # Unified CLI entry point (`manga-recap`) and subcommands
│   │   ├── main.py                      # Subcommand router, POSIX exit codes, structured JSON envelopes
│   │   ├── crawl_cmd.py                 # Webtoon crawler and local directory image importer
│   │   ├── segment_cmd.py               # Long-strip streaming segmenter & 4x4 contact sheet builder
│   │   ├── inspect_cmd.py               # Chapter and project assets inspector
│   │   ├── validate_cmd.py              # Schema conformance and file existence validator
│   │   ├── status_cmd.py                # Resumable stage and checkpoint status reporter
│   │   ├── tts_cmd.py                   # Local VieNeu-TTS utterance synthesis runner
│   │   ├── tts_benchmark_cmd.py         # Voice screening and emotion benchmark generator
│   │   ├── render_cmd.py                # Chapter video renderer (preview/final) with subtitles & motion
│   │   └── compile_cmd.py               # Feature video concatenation and master audio extraction
│   ├── core/                            # Storage engine, schemas, and media utilities
│   │   ├── schemas.py                   # Strict Pydantic v2 data models for all JSON state files
│   │   ├── storage.py                   # Atomic file writer, POSIX locking, checkpoints, and cleanups
│   │   ├── streaming_segmenter.py       # Memory-safe streaming long-strip gutter analyzer
│   │   ├── contact_sheet.py             # 1920x1920 4x4 image grid generator with crimson badges
│   │   └── utils.py                     # Media fingerprinting and FFmpeg subprocess wrappers
│   ├── tts/                             # Local VieNeu-TTS v3 Turbo engine
│   │   ├── engine.py                    # VieNeu offline engine runner, atomic WAV synthesis & checksums
│   │   ├── catalog.py                   # Catalog of 25 Vietnamese preset voices and regional profiles
│   │   └── benchmark.py                 # Screening runner, emotion suite, and hardware telemetry
│   └── render/                          # Cinematic Video Engine V2
│       ├── camera.py                    # Camera motion math, coordinate translation, 16:9 crop preservation
│       ├── subtitle.py                  # Millisecond-accurate SRT generator and text wrapping
│       ├── audio_mixer.py               # PCM binary speech concatenation & sidechain BGM ducking
│       ├── validator.py                 # FFprobe stream inspection (h264, aac, 720p/30fps, duration sync)
│       └── video_engine.py              # VideoEngine chapter rendering and assembly coordinator
└── tests/
    ├── adversarial/                     # Stress testing, JSON stdout purity, and memory bounds
    ├── unit/                            # Component unit test suites
    └── e2e/                             # End-to-end full chapter verification suite
```

---

## ⚡ Quick Start

### 1. Prerequisites
- **Python 3.10+** (tested on 3.12)
- **FFmpeg & FFprobe** (installed and available on system `PATH`)
  - macOS: `brew install ffmpeg`
  - Linux: `sudo apt install ffmpeg`
  - Windows: `winget install Gyan.FFmpeg`

### 2. Installation
Clone the repository and run the setup script:

```bash
# macOS / Linux
git clone <repo-url>
cd "Manga Review"
chmod +x setup.sh
./setup.sh
source venv/bin/activate
```

---

## 🛠️ Unified Deterministic CLI Reference

The pipeline is invoked via `manga-recap <subcommand>` or `python -m src.cli.main <subcommand>`. Always pass `--json` when invoking commands from automated agents.

```bash
# 1. Check pipeline and chapter checkpoints
manga-recap status --project <slug> [--chapter <num>] --json

# 2. Ingest online webtoon or import local panel directory
manga-recap crawl --project <slug> --chapter <num> --import-dir <path> --json

# 3. Stream-segment tall pages into panels and 4x4 contact sheets
manga-recap segment --project <slug> --chapter <num> --json

# 4. Inspect chapter assets, panel dimensions, and audio status
manga-recap inspect --project <slug> --chapter <num> --json

# 5. Validate script.json and state conformance against schemas
manga-recap validate --project <slug> --chapter <num> --strict --json

# 6. Synthesize local VieNeu-TTS 48 kHz audio utterances
manga-recap tts --project <slug> --chapter <num> [--force] --json

# 7. Chapter 1: preview and obtain explicit human approval before final production
manga-recap render --project <slug> --chapter 1 --quality preview --json
# After Chapter 1 approval only (runtime guard planned for V4):
manga-recap render --project <slug> --chapter <num> --quality final --skip-preview --json

# 8. Legacy V3 sequential multi-chapter batch (only after Chapter 1 human approval in V4 workflow)
manga-recap batch --project <slug> [--chapters 1-5] [--retry-chapter <num>] [--compile] --json

# 9. Concatenate multiple chapter videos into a continuous feature recap
manga-recap compile --project <slug> --chapters 1,2 --output <path> --json

# 10. Run isolated TTS voice screening and emotion evaluation benchmark
manga-recap tts_benchmark --out-dir data/benchmarks/vieneu_v3_turbo/ --json
```

### Standard JSON Output Contract
All CLI subcommands emit a strictly machine-parseable JSON envelope on stdout:
```json
{
  "status": "success",
  "command": "render",
  "exit_code": 0,
  "data": {
    "project_slug": "warrior_grandpa",
    "chapter_number": "2",
    "quality": "preview",
    "resolution": "1280x720",
    "fps": 30.0,
    "video_path": ".../chapters/2/video/preview.mp4",
    "duration_diff_seconds": 0.02
  },
  "error": null,
  "duration_seconds": 1.42
}
```

### POSIX Exit Codes
- `0` (`SUCCESS`): Subcommand completed successfully.
- `1` (`GENERAL_ERROR`): Unhandled runtime error.
- `2` (`VALIDATION_ERROR`): Schema mismatch or invalid parameters in `script.json` / `project.json`.
- `3` (`IO_ERROR`): File not found, permission denied, or locked resource.
- `4` (`DEPENDENCY_ERROR`): Missing system dependency (`ffmpeg`, `ffprobe`).

---

## 🎧 TTS Voice Evaluation Lab & Human Review Gates

VieNeu-TTS v3 Turbo supports 25 preset Vietnamese voices and distinct emotional nuances. To ensure optimal narration quality, the repository includes a standalone local listening evaluation lab.

### Running the Evaluation Lab
1. Generate or update benchmark samples:
   ```bash
   manga-recap tts_benchmark --copy-to-lab --json
   ```
2. Open [`docs/lab/index.html`](docs/lab/index.html) in any modern web browser (zero external internet or CDN required).
3. **Audition Voices**: Compare the Top 10 recommended voices (*Trúc Ly*, *Adam bựa*, *Thiện Minh*, *Mai Anh*, *Hải Đăng*, *Thùy Dung*, etc.) across neutral and dramatic narrative hooks.
4. **Rate & Score**: Grade voices across the 5-dimension rubric (*Naturalness*, *Intelligibility*, *Emotion*, *Consistency*, *Audio Quality*).
5. **Human Review Gate 1**: Click **Export voice_selection.json** to save your approved voice profile into the project root before final video generation.

---

## 🤖 Agentic Skill Workflow for Coding Agents

When a coding agent receives a Manga Recap task:
1. **Discover Skill**: Consult `.agents/skills/manga-recap/SKILL.md` and repository guidelines in `AGENTS.md`.
2. **Read Contact Sheets**: Inspect downsampled 4x4 image grids (`contact_sheets/sheet_*.jpg`) to visually analyze character actions and dialogue.
3. **5-Panel Sliding Window**: Script narration in batches of 5 panels with 2 panels of narrative overlap to maintain continuous emotional pacing.
4. **Zero Hallucination Rule**: If panel imagery is ambiguous or text bubbles are obscured, mark `[CAN_XAC_MINH]` and set checkpoint status to `needs_review`.
5. **Deterministic Execution**: Invoke the CLI suite (`manga-recap tts` and `manga-recap render`) to execute local audio synthesis and video rendering.

---

## 🧪 Automated Test Suite

The repository includes a comprehensive 4-tier automated test suite:
- **Unit Tests**: Schema validation, atomic storage locks, camera motion math, and subtitle wrapping.
- **Adversarial Tests**: CLI stdout JSON purity, broken parameter handling, and memory-bounded webtoon slicing ($1080 \times 130,000$ px with $<25$ MB peak RAM).
- **Integration Tests**: CLI subcommands and defect remediation tests.
- **End-to-End Tests**: Full 6-step lifecycle on Warrior Grandpa and His Supreme Granddaughter Episode 2.

Run the test suite:
```bash
pytest
```
*Current test suite status: **306/306 passed** in ~80 seconds (100% pass rate).*

---

## 📄 License
Internal engineering project for Manga Recap automation. Model weights and webtoon source imagery are governed by their respective licenses and terms of service.
