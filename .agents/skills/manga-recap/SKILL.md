---
name: manga-recap
version: "3.0.0"
tags:
  - manga
  - recap
  - video
description: >
  Autonomous end-to-end Manga Recap workflow orchestrating visual panel OCR,
  character consistency, Vietnamese narrative recap scripting, local VieNeu-TTS
  synthesis, and cinematic video rendering via offline CLI tools. Use whenever
  a user requests creating, scripting, narrating, or rendering a manga/webtoon
  recap video from online URLs or local panel images.
metadata:
  version: "3.0.0"
  schema_version: "3.0.0"
  architecture: decoupled-agentic
  tts_engine: vieneu-v3-turbo-local
  target_language: vi
---

# Manga Recap Agentic Skill

## Overview

The `manga-recap` skill enables coding agents (OpenCode, Claude Code, Antigravity) to act as autonomous directors for high-quality Vietnamese Manga Recap videos. The agent handles multimodal vision, OCR, narrative recap scripting, and quality review gates, while driving a suite of offline, deterministic Python CLI tools for image processing, local TTS audio generation, and FFmpeg video assembly.

### Architectural Core Principles
- **Decoupled Intelligence**: All visual interpretation, character understanding, and creative scriptwriting reside in the coding agent. The core Python code contains **zero** direct calls to external LLM/VLM or Cloud TTS APIs.
- **Offline Determinism**: Media processing, audio synthesis via VieNeu-TTS v3 Turbo, video rendering, and state management run 100% locally and deterministically.
- **Strict Recoverability**: Every pipeline stage persists state atomically to JSON checkpoints (`data/projects/<slug>/chapters/<num>/chapter.json`). Any interrupted workflow can be safely resumed without duplicate work.
- **Evidence-Grounded Review**: The agent examines actual panel imagery and contact sheets before scripting. It never invents plot points or character actions from filenames. Ambiguous panels are flagged for human review.

## Prerequisites & System Requirements

1. **Python Environment**: Python 3.10+ with project dependencies installed (`pip install -e .` or active virtualenv in `./venv`).
2. **FFmpeg & FFprobe**: Installed and available on system `$PATH` with `libx264` and `aac` codecs.
3. **VieNeu-TTS Model Weights**: Downloaded and cached locally for VieNeu-TTS v3 Turbo (no network calls during synthesis).
4. **CLI Entrypoint**: `manga-recap` or `python -m src.cli.main`.

## Complete Workflow: Stage 0 to Stage 10

```
[0. User Config Gate] -> [1. Ingest/Crawl] -> [2. Segment/Inspect] -> [3. Character Sync]
         │                       │                    │                       │
         v                       v                    v                       v
   project.json             Raw Images         Panel Manifest           characters.json
                                                                              │
                                                                              v
[5. Validate Script] <───────────────────────────────────────── [4. 5/2 Window Scripting]
         │                                                      (Subject-Centric Framing)
         v
[6. Local VieNeu-TTS] -> [7. Review Gate 1] -> [8. Render Video] -> [9. Review Gate 2] -> [10. Compile]
```

### Stage 0: User Configuration & Onboarding Gate (BẮT BUỘC)
Before launching any pipeline execution, the Agent **MUST** ask the user or explicitly confirm all video configurations:
1. **Chapter & Source URL**: Chapter number and source URL (webtoon/manga reader link).
2. **Giọng đọc (Voice ID)**: Selected from 25 VieNeu-TTS local presets:
   - `Trúc Ly` (Nữ miền Bắc, truyền cảm, tự nhiên — Mặc định)
   - `Hải Đăng` (Nam miền Nam, dõng dạc, hành động)
   - `Thiền Tâm Đức` (Nam miền Bắc, trầm ấm, cổ phong kiếm hiệp)
   - `Thiện Minh` (Nam miền Bắc, hào sảng, kịch tính)
   - `Mai Anh` (Nữ miền Bắc, trẻ trung, tươi sáng)
   - `Thùy Dung` (Nữ miền Bắc, dịu dàng, nhẹ nhàng)
   - `Quang Sơn` (Nam miền Trung, sắc bén, phản diện)
3. **Tốc độ đọc (Speed)**: `0.9x`, `1.0x` (chuẩn), `1.1x` (nhanh vừa), `1.2x`.
4. **Gắn phụ đề (Burn Subtitles)**: `Có` (`--burn-subtitles`, mặc định) hoặc `Không` (`--no-burn-subtitles`).
5. **Hiệu ứng Animation (Camera Motion)**:
   - `Có` (`--animation`, mặc định): Chuyển động zoom/pan nhẹ bám theo hành động nhân vật.
   - `Không` (`--no-animation`): Chế độ tĩnh (`STATIC`) toàn bộ, hiển thị trọn vẹn panel với nền mờ nghệ thuật, tránh hoàn toàn rung lắc và phóng to.
6. **Chất lượng Video**: `preview` (720p) hoặc `final` (1080p).
7. **Nhạc nền BGM**: File nhạc nền nếu có, âm lượng BGM (`bgm_volume: 0.15`), voice volume (`1.0`).

All approved settings are saved into `data/projects/<slug>/project.json`.

### Stage 1: Ingestion & Crawl
Fetch raw webtoon page images or import local chapter scans into project storage.
```bash
manga-recap crawl --project <project_slug> --chapter <chapter_num> --url "<chapter_url>" --json
```
- Output: `data/projects/<project_slug>/chapters/<chapter_num>/raw/` and updated `chapter.json`.
- State checkpoint: `stage: "crawl", status: "completed"`.

### Stage 2: Segmentation, Inspect & Visual Panel Review
Tear down long-strip webtoon pages into discrete story panels and produce contact sheets for agent visual panel review.
```bash
manga-recap inspect --project <project_slug> --chapter <chapter_num> --json
```
- Reads: `manifest.json`.
- Key Assets:
  - `panels/panel_*.jpg`: Segmented panels with pixel crop boundaries and aspect ratios.
  - `contact_sheets/sheet_*.jpg`: 4x4 downscaled visual grids designed for token-efficient agent visual panel review.

### Stage 3: Character Profiling & Character Identity Consistency
Before scripting any dialogue or narration:
1. Read `data/projects/<project_slug>/characters.json`. Check canonical Vietnamese names, core visual traits, and established character identity voices.
2. Read `data/projects/<project_slug>/story_summary.md`. Review world-building rules and prior chapter events.
3. If new characters appear in the panels, register them in `characters.json` following `.agents/skills/manga-recap/rules/character_consistency.md`.

### Stage 4: 5-Panel Sliding Window Vision & Recap Scripting
Process the chapter panels in batches of **5 panels with a 2-panel lookahead overlap** (stride 3) to guarantee narrative flow and action continuity:
1. Load contact sheet grid or individual panel images for window panels `[N, N+1, N+2, N+3, N+4]`.
2. Perform visual OCR on speech bubbles and observe panel actions.
3. Script Vietnamese narration for commit panels `[N, N+1, N+2]`, keeping panels `[N+3, N+4]` as lookahead context.
4. Advance the sliding window by 3 panels (stride 3): the next batch begins at index `N+3`, spanning `[N+3, N+4, N+5, N+6, N+7]`.
5. Mathematical Sliding Window Partitioning (for any $P \in [1, 20]$ panels):
   - For batch $k = 0, 1, \dots, \lceil P / 3 \rceil - 1$:
     - Commit range: $[3k, \min(3k+3, P))$
     - Lookahead context: $[3k, \min(3k+5, P))$
     - Every panel $p \in [0, P)$ is committed to narration exactly once (100% panel coverage with zero duplicate panels and zero omissions).
6. Checkpoint results incrementally into `script.json` after every batch of recap scripting.
7. Strictly apply rules from:
   - `.agents/skills/manga-recap/rules/narrative_recap.md`
   - `.agents/skills/manga-recap/rules/vision_ocr.md`
   - `.agents/skills/manga-recap/rules/character_consistency.md`

### Stage 5: Validate Script & Metadata
Validate `script.json` against enterprise Pydantic schemas before running audio synthesis:
```bash
manga-recap validate --project <project_slug> --chapter <chapter_num> --json
```
- Confirms: all sequence IDs are unique, referenced panel IDs exist, narrator texts meet length constraints (50–250 chars), camera motions are valid enum values, and focus boxes lie within normalized `[0.0, 1.0]` coordinates.

### Stage 6: Local VieNeu-TTS Audio Generation
Synthesize 48 kHz uncompressed WAV utterances using the local VieNeu-TTS v3 Turbo engine:
```bash
manga-recap tts --project <project_slug> --chapter <chapter_num> [--force] --json
```
- Generated utterances are written directly to `audio/clips/audio_<chapter>_<seq_id>.wav` via crash-safe atomic replacement, with exact measured waveform durations and actual SHA-256 file checksums recorded into `script.json`.
- Resumability: By default, re-running TTS resumes without re-synthesizing segments that already possess valid, verified audio matching the current voice. Pass `--force` to re-synthesize all chapter segments.

### Stage 7: Human Review Gate 1 (Voice & Emotion Strategy)
Prior to full video rendering, present synthesized voice samples to the human user at Review Gate 1:
1. Direct the user to the local listening interface or review `data/benchmarks/vieneu_v3_turbo/review/index.html`.
2. Confirm the narration strategy:
   - **Strategy A (Single Narrator)**: One voice throughout (e.g. `Trúc Ly`).
   - **Strategy B (Narrator + Characters)**: Main narrator + specific voice profiles for key characters.
   - **Strategy C (Emotion-Aware Narrator)**: Single narrator with punctuation tuning and audible cues (`[cười]`, `[thở dài]`, `[hắng giọng]`).
3. Record human approval into `data/projects/<project_slug>/voice_selection.json` before proceeding.

### Stage 8: Cinematic Video Render
Render preview or final video clips with dynamic FFmpeg filterchains:
```bash
manga-recap render --project <project_slug> --chapter <chapter_num> --quality preview --json
```
- Applies chosen camera motion (`static`, `pan_left`, `pan_right`, `pan_up`, `pan_down`, `zoom_in`, `zoom_out`, `focus_crop`, `vertical_scroll`).
- Times panel duration exactly to synthesized audio duration.
- Generates aligned `.srt` subtitles and ducks background music during speech.

### Stage 9: Human Review Gate 2 (Video Preview Verification)
Verify output video stream using `ffprobe` and human inspection at Review Gate 2:
1. Verify video stream: 1280x720 (preview) or 1920x1080 (final), 30 fps, H.264 video, AAC audio.
2. Check subtitle readability and audio-visual synchronization.
3. Ensure no dialogue or faces are cut off by focus_box crops.

### Stage 10: Multi-Chapter Compilation
Assemble multiple completed chapters into a continuous full-length recap video:
```bash
manga-recap compile --project <project_slug> --chapters 1,2 --output "data/projects/<project_slug>/video/full_recap.mp4" --json
```

## Narrative & Cinematic Guidelines

- **5-Second Hook**: Every chapter recap starts with a dramatic 5-second hook (no generic greetings).
- **Frame Centering & Shape Protocol (Chống Phóng To & Tránh Chat Box)**:
  - **KHÔNG zoom vào bong bóng thoại (Speech Bubbles)**: Bong bóng thoại đã được lồng tiếng và phụ đề hiển thị. Tuyệt đối không chọn tâm frame hoặc crop tập trung vào hộp thoại rỗng.
  - **Tâm Frame bám nhân vật (Subject-Centric)**: Luôn đặt tâm frame (`focus_box` hoặc `fy`) vào vị trí ánh mắt, khuôn mặt, hoặc trọng tâm hành động võ thuật của nhân vật. Với panel thoại ở trên, hướng `fy` xuống phía dưới (`0.50 - 0.70`).
  - **Giới hạn tỷ lệ Crop an toàn**: `scale_ratio` tối thiểu $\ge 0.45$ (không crop nhỏ hơn 45% kích thước panel gốc).
  - **Kiểm soát độ phóng to (Ken Burns)**: Hệ số zoom tối đa được khống chế ở $1.15\times$ (thay vì $1.25\times$) để chuyển động nhẹ nhàng, không gây vỡ nét ảnh.
  - **Xử lý Panel dọc (Webtoon Strips $H > 2W$)**: Sử dụng chế độ `static` (nền mờ điện ảnh 16:9 bảo toàn trọn vẹn nét vẽ) hoặc `vertical_scroll` với tốc độ chậm, tránh cắt cụt nhân vật.
- **Camera Motion Modes**: Assign cinematic camera motion modes per segment: `static`, `pan_left`, `pan_right`, `pan_up`, `pan_down`, `zoom_in`, `zoom_out`, `focus_crop`, `vertical_scroll`.
- **Focus Crop**: Use `focus_box` coordinates `[x_min, y_min, x_max, y_max]` to zoom in on character faces or key items.
- **Audible Cues**: Use approved cues `[cười]` and `[thở dài]` to modulate VieNeu-TTS expression.

## CLI Reference & Exit Code Standards

All commands accept `--json` for structured output and return standardized POSIX exit codes:

| Exit Code | Meaning | Agent Action |
|-----------|---------|--------------|
| `0` | Success | Continue to next pipeline step. |
| `1` | Generic / Runtime Error | Inspect `error.message` in JSON, check logs, and retry. |
| `2` | Validation Error | Script or configuration schema violation. Fix fields in JSON. |
| `3` | IO / Storage Error | File not found, permission denied, or lock timeout. Check paths. |
| `4` | Dependency Error | FFmpeg or VieNeu model missing. Verify environment setup. |

## Sub-rule References
- **Narrative Writing & Emotion**: `.agents/skills/manga-recap/rules/narrative_recap.md`
- **Vision, Contact Sheets & OCR**: `.agents/skills/manga-recap/rules/vision_ocr.md`
- **Character Profiles & Continuity**: `.agents/skills/manga-recap/rules/character_consistency.md`
- **Review Gates & Uncertainty**: `.agents/skills/manga-recap/rules/human_review_gates.md`
