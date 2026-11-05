# Project: Manga-Recap-Skill V3 Pipeline Correctness Hardening

## Architecture
- **Cognition & Vision**: Agent-Native execution (visual inspection, script writing, review approvals).
- **Deterministic Tooling**: Python CLI offline pipeline (`crawl`, `segment`, `script/validate`, `tts`, `render`, `compile`).
- **Data Persistence**: Local atomic JSON files (`data/projects/<slug>/`). Zero Supabase, zero external DBs.
- **TTS Engine**: Local offline VieNeu-TTS v3 Turbo. Zero cloud TTS APIs.
- **Rendering & Video**: Local FFmpeg pipeline with Ken Burns motion, subtitle muxing, and master audio sync.

## Feature Inventory
| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| 1 | FIX-01: Full TTS Fingerprinting & Resume | SHA-256 fingerprint of (text, voice, speed, emotion, cue, engine) and checksum match before audio reuse | M1 | ORIGINAL_REQUEST §R1 |
| 2 | FIX-02: Crawler Exception Handling | Eliminate `except Exception: pass`, fail checkpoint on network/anti-bot error or 0 images | M1 | ORIGINAL_REQUEST §R1 |
| 3 | FIX-03: Sliding Window Stride & Coverage | Fix stride off-by-one, formula for 1–20 panels, validate 100% panel coverage without duplicates | M1 | ORIGINAL_REQUEST §R1 |
| 4 | FIX-04: Review Gates Enforcement | Require Voice Approval (Gate 1) & Preview Approval (Gate 2) before final render; remove bypass, add `--approved` | M1 | ORIGINAL_REQUEST §R1 |
| 5 | FIX-05: CLI Arguments & Forwarding Sync | Forward `--video-mode` to render engine, wire `--force` in segment, 48kHz audio in compile | M1 | ORIGINAL_REQUEST §R1 |
| 6 | FIX-06: Checkpoint Lifecycle Standard | `pending` -> `running` -> `completed`/`failed`/`needs_review`; remove dead `FINALIZE`; handle crashes | M2 | ORIGINAL_REQUEST §R2 |
| 7 | FIX-07: Artifact Invalidation Cascades | Script/voice edits invalidate audio/subtitles/video; audio edit invalidates video; compile prefers final | M2 | ORIGINAL_REQUEST §R2 |
| 8 | FIX-08: Strict Schema Validation | Unique `seq_id`, missing panels treated as errors, verify audio checksums, validate FocusBox bounds | M2 | ORIGINAL_REQUEST §R2 |
| 9 | FIX-09: Timeline Sync (<= 150ms) | Remove 0.5s clamping desync, save exact WAV durations to script, accurate SRT alignment | M2 | ORIGINAL_REQUEST §R2 |
| 10 | FIX-10: Cross-Platform Packaging | Add `pyproject.toml` with `manga-recap` CLI entrypoint; clean cloud API keys from setup scripts | M2 | ORIGINAL_REQUEST §R2 |
| 11 | Phase 3: Comprehensive Regression Tests | Author unit/regression tests covering FIX-01..10, 100% pass on all 313+ existing tests | M3 | ORIGINAL_REQUEST §R3 |
| 12 | Phase 4: Real E2E Chapter Verification | Real chapter test with neural TTS on Webtoons URL, verify anti-bot handling and timeline sync | M4 | ORIGINAL_REQUEST §R4 |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| M1 | P0 Critical Fixes | FIX-01, FIX-02, FIX-03, FIX-04, FIX-05 | Survey complete | DONE |
| M2 | P1 Data Integrity & Contracts | FIX-06, FIX-07, FIX-08, FIX-09, FIX-10 | M1 | DONE |
| M3 | Regression Testing Suite | Comprehensive test suite for FIX-01..10 and baseline integrity | M1, M2 | DONE |
| M4 | Real E2E Chapter Verification | Real Webtoons URL crawl, neural TTS, render, and forensic audit | M1, M2, M3 | DONE |

## Interface Contracts
### TTS Fingerprinting Contract (FIX-01)
- Input: `text: str`, `voice: str`, `speed: float`, `emotion: Optional[str]`, `emotion_cue: Optional[str]`, `engine_mode: str`.
- Fingerprint: `SHA256(f"{text}|{voice}|{speed:.2f}|{emotion or ''}|{emotion_cue or ''}|{engine_mode}")`.
- Stored in: `ScriptSegment.fingerprint: Optional[str]`, `ScriptSegment.checksum: Optional[str]`.
- Resume condition: File exists, size > 44, `seg.checksum == calculate_sha256(file)` AND `seg.fingerprint == calculated_fingerprint`.

### Crawler Checkpoint Contract (FIX-02)
- On network error, HTTP error, anti-bot block, or `raw_count == 0`:
  - `update_checkpoint(chap_dir, StageType.CRAWL, CheckpointStatus.FAILED, error=...)`
  - Raise `CliValidationError` or exit with non-zero exit code.
  - Zero silent `except Exception: pass`.

### Sliding Window Partitioning Contract (FIX-03)
- For chapter with $P$ panels ($P \ge 1$):
  - Stride = 3, window commit = 3 (last commit = remainder).
  - Each panel $p \in [0, P)$ is committed to narration in exactly ONE window batch.
  - `validate_cmd.py` enforces: `set(all_seg_panel_ids) == set(manifest_panel_ids)` with zero duplicates.

### Review Gates Contract (FIX-04)
- Gate 1 (Voice Selection): `voice_selection.json` exists with `status: "approved"`, OR `--approved` flag provided.
- Gate 2 (Preview Approval): `preview.mp4` exists AND `preview_approval.json` exists with `status: "approved"`, OR `--approved` flag provided.
- Final 1080p render (`quality == "final"`) MUST enforce both Gate 1 and Gate 2. Remove `--skip-review` bypass.

### CLI Sync Contract (FIX-05)
- `main.py` -> `render_cmd.py` -> `video_engine.py`: `--video-mode` (`auto`, `landscape_fit`, `portrait_vertical`, `portrait_padded`) forwarded to `render_chapter()`.
- `segment_cmd.py`: respects `--force`.
- `compile_cmd.py`: `-ar 48000`.

## Code Layout
- `src/cli/`: Command handlers (`main.py`, `crawl_cmd.py`, `segment_cmd.py`, `validate_cmd.py`, `tts_cmd.py`, `render_cmd.py`, `compile_cmd.py`, `status_cmd.py`).
- `src/core/`: Schemas (`schemas.py`), storage (`storage.py`), config (`config.py`).
- `src/crawler/`: Webtoons crawler service (`crawler_service.py`).
- `src/segment/`: Panel segmenter (`streaming_segmenter.py`).
- `src/tts/`: TTS engine abstraction (`engine.py`, `tts_service.py`).
- `src/render/`: Video engine (`video_engine.py`), audio mixer (`audio_mixer.py`), subtitle generator (`subtitle.py`).
- `tests/`: Unit, adversarial, and E2E tests (`tests/unit/`, `tests/adversarial/`, `tests/e2e/`).
- `pyproject.toml`: Packaging specification for `pip install -e .`.
