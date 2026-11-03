# Repository Instructions for Coding Agents

Welcome to the **Manga Recap Agentic Skill V3** repository. This document defines the operating principles, architectural separation of responsibilities, and tool discovery for coding agents (OpenCode, Claude Code, Antigravity, etc.).

---

## 1. Core Architectural Contract: Decoupled Agent-Native Design

This project operates on a **strict separation of intelligence and execution**:

| Responsibility Domain | Owner | Description |
|-----------------------|-------|-------------|
| **Cognition & Vision** | Coding Agent | Reads panel imagery, executes visual OCR, interprets character actions, crafts Vietnamese narrative recap scripts, selects camera motions, and manages continuity. |
| **Deterministic Tooling**| Python CLI | Offline execution of webtoon scraping, panel slicing, Pydantic schema validation, local VieNeu-TTS synthesis, and FFmpeg video rendering. |
| **Human Quality Gates** | User / Human Reviewer | Listens to voice samples, validates narration emotion/strategy, and inspects video previews before final high-resolution encoding. |

### ⚠️ Zero Cloud LLM / Cloud TTS & Zero Supabase Rule
- **The core Python codebase contains zero direct calls to external LLM/VLM APIs (OpenAI, Gemini, OpenRouter, Anthropic) or Cloud TTS APIs (Edge-TTS, OpenAI TTS, Google Cloud TTS).**
- **Zero Supabase or database dependencies:** All persistence is self-contained local JSON under `data/projects/`.
- All reasoning and visual perception MUST be performed directly by you (the coding agent) using your native capabilities.
- Audio synthesis is performed locally and offline via the bundled VieNeu-TTS v3 Turbo engine.


---

## 2. Skill Discovery & Usage

The repository provides a standardized Agentic Skill located at:
```
.agents/skills/manga-recap/SKILL.md
```
Whenever a user requests processing a manga chapter, writing a recap script, evaluating voice samples, or rendering a recap video:
1. Read `.agents/skills/manga-recap/SKILL.md` to load the 10-stage execution workflow.
2. Consult the specialized rulebooks in `.agents/skills/manga-recap/rules/`:
   - `narrative_recap.md`: Guidelines for pacing, hooks, character voice, and Vietnamese prose.
   - `vision_ocr.md`: Guidelines for reading contact sheets, detecting crop anomalies, and camera focus.
   - `character_consistency.md`: Guidelines for character registry and voice mapping.
   - `human_review_gates.md`: Protocols for human checkpoints and uncertainty handling.
3. Use templates in `.agents/skills/manga-recap/templates/` for initial state documents.

---

## 3. Local Storage Architecture

All project assets and runtime states are stored in self-contained directories under `data/projects/`:
```
data/projects/<project_slug>/
├── project.json              # Project configuration, video/audio render settings
├── characters.json           # Character registry, visual features, assigned voices
├── story_summary.md          # Running lore summary and chapter progression
└── chapters/
    └── <chapter_num>/
        ├── chapter.json      # Stage checkpoints (pending, running, completed, failed, needs_review)
        ├── manifest.json     # Segmented panel coordinates, aspect ratios, checksums
        ├── script.json       # Utterance-level recap script with camera motions and audio metadata
        ├── raw/              # Scraped or imported full-page images
        ├── panels/           # Segmented panel crops (panel_001.jpg, etc.)
        ├── contact_sheets/   # 4x4 image grids for agent visual inspection
        ├── audio/clips/      # 48 kHz uncompressed WAV utterances
        ├── subtitles/        # Synchronized SRT subtitles
        └── video/            # Rendered chapter clips and final MP4 video
```

### Storage Safety Guarantees
- **No Supabase or External DB**: All state is strictly local JSON and Markdown. Zero Supabase or SQL dependencies.
- **Atomic Writes**: All state mutations use `atomic_write_json` (`.tmp` write followed by atomic rename) to eliminate corruption from interrupted processes.
- **File Locking**: Concurrency is governed by exclusive `FileLock` (`.lock`).
- **Resumability**: If your context is truncated or execution is interrupted, query `manga-recap status --project <slug> --chapter <num>` to discover the last completed stage and resume seamlessly.

---

## 4. Deterministic CLI Quick Reference

The CLI is invoked via `manga-recap <subcommand>` or `python -m src.cli.main <subcommand>`. Always pass `--json` when invoking tools from an agent session.

```bash
# 1. Check pipeline and chapter status
manga-recap status --project <slug> [--chapter <num>] --json

# 2. Ingest or crawl raw images
manga-recap crawl --project <slug> --chapter <num> --url "<url>" --json

# 3. Inspect chapter assets and manifest
manga-recap inspect --project <slug> --chapter <num> --json

# 4. Validate script and chapter schemas
manga-recap validate --project <slug> [--chapter <num>] --json

# 5. Synthesize local VieNeu-TTS audio
manga-recap tts --project <slug> --chapter <num> [--force] --json

# 6. Render preview or final video
manga-recap render --project <slug> --chapter <num> --quality preview --json

# 7. Compile multiple chapters into a feature video
manga-recap compile --project <slug> --chapters 1,2 --output <path> --json

# 8. Run TTS voice screening and emotion benchmarks
manga-recap tts_benchmark --out-dir data/benchmarks/vieneu_v3_turbo/ --json
```

### Standard JSON Response Envelope
```json
{
  "status": "success",
  "command": "validate",
  "exit_code": 0,
  "data": { "valid": true, "errors": [] },
  "error": null,
  "duration_seconds": 0.42
}
```

### Exit Codes & Handling Strategy
- `0` (Success): Proceed to next task.
- `1` (Generic Error): Review error payload in response, consult log files, and retry.
- `2` (Validation Error): Schema mismatch or invalid data in `script.json` / `project.json`. Read error details and repair JSON fields.
- `3` (IO / Storage Error): Missing file or locked resource. Confirm paths relative to project root.
- `4` (Dependency Error): Missing external system dependency (FFmpeg, local model weights). Notify user.

---

## 5. Coding Agent Behavioral Rules

1. **Mandatory User Configuration Gate (Stage 0)**: Before starting crawl, scripting, or rendering, the Agent MUST ask the User for the complete set of video configurations (Chapter, URL, Voice ID, Speed, Burn Subtitles, Frame Animation, Video Quality, BGM).
2. **Subject-Centric Framing & Chat Box Avoidance**: Never set the frame focal center on speech bubbles / chat boxes. Always focus on character eyes, facial expressions, or physical action. Enforce safe crop scale ($\ge 0.45$) and clamp Ken Burns zoom factor ($\le 1.15\times$) to prevent over-zooming. For tall continuous panels ($H > 2W$), prefer `static` dual-layer mode with blurred wings or `vertical_scroll`. If the user disables animation (`--no-animation`), render all frames in `static` mode.
3. **Look Before Scripting**: Never write narration from filenames (`panel_001.jpg`) or guesswork. Inspect contact sheets (`contact_sheets/sheet_*.jpg`) directly.
4. **5-Panel Sliding Window**: Script in batches of 5 panels with 2 panels of overlap to guarantee narrative coherence.
5. **No Hallucination**: If text in a speech bubble is unreadable, watermarked, or cut off, or if an action is ambiguous, mark `status: "needs_review"` and flag with `[CAN_XAC_MINH: <lý_do>]`. Never fabricate lore.
6. **Enforce Human Gates**: Never render final 1080p video before the user approves voice selection (Review Gate 1) and preview video (Review Gate 2).
7. **Atomic Checkpointing**: Update `script.json` after every scripting batch so progress is never lost.
