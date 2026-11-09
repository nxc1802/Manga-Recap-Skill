# Canonical Contract Decisions & Integration Specifications
**Manga Recap Agentic Skill V3 — Phase 0 Contract Freeze**

- **Document Version:** 1.0.0
- **Status:** APPROVED & FROZEN
- **Author:** A0: Contract/Integration Architect
- **Baseline Git HEAD:** `a9920a53b54e6aba30bcba3d3ac60323d74c9336`
- **Baseline Test Suite:** 554 tests passed (100% green, 188.00s)

---

## 1. Executive Summary & Baseline Reconnaissance

### 1.1 Baseline Git State
- **Branch:** `main`
- **Commit:** `a9920a53b54e6aba30bcba3d3ac60323d74c9336`
- **Working Tree:** Pristine working directory with only `docs/Manga_Recap_TeamWork_Implementation_Plan.md` and deleted `docs/plan.md`.

### 1.2 Baseline Test Execution Evidence
- **Test Command:** `./venv/bin/pytest`
- **Result:**
  ```text
  ============================= test session starts ==============================
  platform darwin -- Python 3.12.13, pytest-9.2.0, pluggy-1.7.0
  rootdir: /Volumes/WorkSpace/Factory/Manga Review
  configfile: pytest.ini
  testpaths: tests
  plugins: platformdirs-4.14.3, anyio-4.16.0
  collected 554 items

  tests/adversarial/... (105 tests)                                [ 19%]
  tests/e2e/... (18 tests)                                         [ 22%]
  tests/test_stitch_segment.py ..                                  [ 23%]
  tests/test_tts_parser.py ...                                     [ 23%]
  tests/test_utils.py ...                                          [ 24%]
  tests/test_video_modes.py ....                                   [ 25%]
  tests/unit/... (397 tests)                                       [100%]

  ======================= 554 passed in 188.00s (0:03:07) ========================
  ```
- **Finding:** The baseline codebase is completely operational and passes all 554 tests cleanly.

### 1.3 Codebase Architecture Audit
```
src/
├── cli/                 # Subcommand handlers (crawl, segment, tts, render, batch, cloud, compile, clean, validate)
├── cloud/               # Cloud batch execution (kaggle_service.py)
├── core/                # Core schemas (schemas.py), storage (storage.py), config, logger, utils, pacing
├── crawler/             # Webtoon crawlers, image extractors, resolvers
├── production/          # SingleSessionProductionRunner, hardware encoder detection
├── render/              # VideoEngine V2, CameraMotionGenerator, SubtitleGenerator, AudioMixer, StreamValidator
├── services/            # Character dictionary, crawler service, TTS service wrappers
└── tts/                 # VieNeu-TTS v3 Turbo engine, voice catalog, voice profiles, benchmark harness
```

---

## 2. Canonical Stage Workflow & Contracts

The pipeline executes in 5 sequential operational stages:

```
[Stage 1: INGEST] ---> [Stage 2: AGENT] ---> [Stage 3: AUDIO] ---> [Stage 4: RENDER] ---> [Stage 5: EXPORT]
 (Crawl & Slice)       (Visual Script)       (VieNeu-TTS)          (VideoEngine)         (Packaging)
```

### Stage 1: Ingest (`StageType.INGEST`)
- **CLI Trigger:** `manga-recap crawl` / `manga-recap segment`
- **Inputs:** Raw chapter URL or images in `raw/`
- **Canonical Outputs:**
  - `manifest.json`: List of `raw_images`, `panels` (with `panel_id`, `crop_box`, `width`, `height`, `aspect_ratio`, `sha256`, `status`: ACTIVE, CONTEXT_ONLY, SKIP, NEEDS_REVIEW), `contact_sheets` (4x4 grids).
  - Physical panels: `panels/panel_001.jpg`, `panel_002.jpg`, etc.
  - Contact sheets: `contact_sheets/sheet_01.jpg`, etc.
- **Checkpoint Update:** `chapter.json` -> `stages["ingest"].status = "completed"`.

### Stage 2: Agent Cognition (`StageType.AGENT`)
- **CLI Trigger:** Agent cognitive visual analysis / `manga-recap validate`
- **Inputs:** `manifest.json`, `contact_sheets/`, `panels/`, `story_summary.md`, `characters.json`
- **Canonical Outputs:**
  - `script.json`: RecapScript containing an array of `ScriptSegment` items.
  - Narrative Rules:
    - **N:M Panel Mapping:** A segment may reference 1, 2, 3, or more panels (`panel_ids`). Panels may be reused. Non-essential panels may be omitted.
    - **Direct Storytelling:** Dialogue in quotes, character voices, and expressive Vietnamese prose.
    - **Zero Cloud LLM:** All perception and reasoning performed natively by the Agent.
- **Checkpoint Update:** `chapter.json` -> `stages["agent"].status = "completed"`.

### Stage 3: Audio Synthesis (`StageType.AUDIO`)
- **CLI Trigger:** `manga-recap tts --project <slug> --chapter <num> [--kaggle]`
- **Inputs:** `script.json` (`narrator_text`, `voice_id`, `emotion`, `emotion_cue`, `speed`)
- **Canonical Outputs:**
  - Physical audio clips: `audio/clips/audio_<chapter>_<seq_id>.wav` (48 kHz uncompressed 16-bit PCM WAV).
  - Updated `script.json`: `duration` (exact seconds), `audio_path`, `checksum` (SHA-256), `status: "synthesized"`, `synthesized_voice`.
- **Zero Cloud TTS:** Offline execution via VieNeu-TTS v3 Turbo.
- **Checkpoint Update:** `chapter.json` -> `stages["audio"].status = "completed"`.

### Stage 4: Video Rendering (`StageType.RENDER`)
- **CLI Trigger:** `manga-recap render --project <slug> --chapter <num> --quality final --skip-preview [--kaggle]`
- **Inputs:** `script.json` with synthesized audio, `panels/`, `audio/clips/`, `project.json` (render settings).
- **Canonical Outputs:**
  - Subtitles: `subtitles/chapter_<chapter>.srt`.
  - Mixed audio: `audio/combined_audio.wav` (concatenated speech + ducked BGM).
  - Master video: `video/final.mp4` and `video/chapter_<chapter>.mp4`.
  - Validation: ffprobe verification (resolution, codec, fps, audio rate, A/V drift <= 150 ms).
- **Audio Independence:** If valid audio clips exist, render **must not** invoke TTS.
- **Checkpoint Update:** `chapter.json` -> `stages["render"].status = "completed"`.

### Stage 5: Export & Packaging (`StageType.EXPORT`)
- **CLI Trigger:** Automatic upon final render or `manga-recap compile` / `manga-recap batch`
- **Canonical Outputs:**
  - Staged final video: `output/<project_slug>/chapter_<padded_chapter>.mp4`.
  - Staged subtitles: `output/<project_slug>/chapter_<padded_chapter>.srt`.
  - Feature compilation (if batch compiled): `output/<project_slug>/feature_compilation.mp4`.
- **Checkpoint Update:** `chapter.json` -> `overall_status = "completed"`, `current_stage = "export"`.

---

## 3. Canonical Document Schemas & Invariants

### 3.1 Document Schema Reference Table

| Document | File Path | Model Class | Invariants & Constraints |
|---|---|---|---|
| **Project Config** | `data/projects/<slug>/project.json` | `ProjectConfig` | `schema_version = "3.0.0"`, single source of truth for series-wide render & audio settings. |
| **Chapter Metadata** | `.../chapters/<num>/chapter.json` | `ChapterMetadata` | Tracks checkpoints across operational stages (`ingest`, `agent`, `audio`, `render`, `export`). |
| **Panel Manifest** | `.../chapters/<num>/manifest.json` | `PanelManifest` | Full catalogue of raw images, cropped panels, dimensions, checksums, and contact sheets. |
| **Recap Script** | `.../chapters/<num>/script.json` | `RecapScript` | Unique immutable `seq_id`, `panel_ids` list, text, audio metadata. |
| **Character Dictionary**| `data/projects/<slug>/characters.json`| `CharacterDictionary`| Character profiles, core features, assigned voice IDs. |
| **Voice Approval** | `data/projects/<slug>/voice_selection.json`| `VoiceSelectionApproval`| Gate 1 approval: `primary_voice`, `status = "approved"`, `reviewer`. |
| **Preview Approval** | `.../chapters/<num>/preview_approval.json`| `PreviewApproval` | Gate 2 approval: `reviewer`, `status = "approved"`. Optional when `--skip-preview` is used. |

### 3.2 Key Invariants Frozen for Phase 1 & Phase 2
1. **`seq_id` Identity Preservation:**
   - `seq_id` is an opaque, immutable string (e.g. `"001"`, `"002_1"`, `"003"`).
   - Systems **must never** assume sequential integers `0, 1, 2...` or format filenames as `f"{idx:03d}"` if it causes mismatch with `seq_id`.
   - Audio file naming contract: `audio_{chapter}_{seq_id}.wav`.
2. **Fail-Fast Asset Resolution:**
   - If any panel in `panel_ids` or any audio clip is missing or corrupted:
     - **Raise immediate error** identifying `project_slug`, `chapter_number`, `seq_id`, missing asset path, and stage.
     - **Strictly forbidden:** Falling back to `panel_001.jpg`, substituting images from the directory, or using default audio.
3. **N:M Panel Mapping:**
   - A segment may map to multiple panels (`len(panel_ids) >= 1`).
   - Panels may be reused across segments.
   - Omitted manifest panels are valid (non-essential panels skipped by Agent).
   - Technical validation checks that all panels in `panel_ids` exist on disk; it **does not** enforce that 100% of raw panels are rendered.
4. **Per-Segment Voice Parameter Integrity:**
   - Each segment's `voice_id`, `emotion`, `emotion_cue`, and `speed` must be respected.
   - Cloud batch TTS must group or iterate by parameter; it **must not** apply the first segment's voice to all segments.
   - Batch synthesis count must strictly equal segment count; silent truncation/zipping is forbidden.
5. **Separation of Render and TTS:**
   - Invoking `render` when valid `.wav` files already exist **must not** invoke the TTS engine.
   - Cloud render jobs must support receiving pre-existing audio without running cloud TTS.

---

## 4. Backward Compatibility & Migration Strategy

1. **Schema Versioning:**
   - Current canonical version is `"3.0.0"`.
   - Older documents without `schema_version` or with version `1.x`/`2.x` are transparently loaded via `from_legacy_dict` or default field fallbacks in Pydantic.
2. **Stage Name Aliasing:**
   - Code recognizes both new operational names and legacy aliases:
     - `crawl` -> `ingest`
     - `segment` -> `ingest`
     - `script` -> `agent`
     - `tts` -> `audio`
     - `finalize` -> `export`
3. **Review Gate Semantics:**
   - Gate 1 (`voice_selection.json`) is approved once at series level.
   - Gate 2 preview is optional: when `--skip-preview` is specified, `chapter.json` records `human_review_status = "unreviewed"`. It is **never** automatically marked as `"quality_approved"` without human review.

---

## 5. Architectural Guardrails & Mandates

- **Zero Cloud LLM / Cloud VLM:** No OpenAI, Gemini, Anthropic, or OpenRouter API calls in Python runtime.
- **Zero Cloud TTS:** No Edge-TTS, OpenAI TTS, or Google Cloud TTS. 100% offline local VieNeu-TTS.
- **Zero Supabase / External DB:** State is strictly local JSON and Markdown in `data/projects/`.
- **Atomic Persistence:** All state modifications use `atomic_write_json` (`.tmp` write + atomic `os.replace`).
- **File Concurrency:** Governed by `FileLock` (`.lock`) on state files.
- **Integrity Mandate:** No hardcoded mock outputs, no dummy facades, no bypass of validation logic.

---

## 6. Disjoint File Ownership Boundaries (Phases 1 & 2)

To ensure zero merge conflicts and clean isolation, file ownership is strictly partitioned:

### Phase 1: P0 Cloud Pipeline Correctness (Max 2 Concurrent Workers)

| Agent | Role | Exclusive Write Ownership | Prohibited Paths |
|---|---|---|---|
| **A1** | Cloud Fixer | `src/cloud/kaggle_service.py`<br>`src/cloud/` (any new cloud helper) | `tests/**`<br>`src/core/**`<br>`src/render/**`<br>`src/cli/**` |
| **A2** | Regression Test Builder | `tests/unit/cloud/**`<br>`tests/adversarial/test_cloud_p0_adversarial.py`<br>`tests/fixtures/cloud/**` | `src/cloud/kaggle_service.py`<br>`src/core/**`<br>`src/render/**` |

### Phase 2: P1 Unified Contracts & Engines (Max 2 Concurrent Workers)

| Agent | Role | Exclusive Write Ownership | Prohibited Paths |
|---|---|---|---|
| **A3** | Core/Local Engineer | `src/core/schemas.py`<br>`src/core/storage.py`<br>`src/cli/tts_cmd.py`<br>`src/cli/render_cmd.py`<br>`src/cli/validate_cmd.py`<br>`src/production/runner.py` | `src/cloud/kaggle_service.py`<br>`tests/unit/cloud/**` |
| **A4** | Cloud Adapter Engineer | `src/cloud/kaggle_service.py`<br>`src/cloud/adapter.py`<br>`tests/unit/cloud/test_kaggle_service.py` | `src/core/**`<br>`src/cli/tts_cmd.py`<br>`src/cli/render_cmd.py` |

---

## 7. Canonical Small Test Fixture Specification

Located at: `tests/fixtures/synthetic/`

```
tests/fixtures/synthetic/
├── project.json                      # ProjectConfig (1920x1080, 48kHz, balanced pacing)
├── characters.json                   # CharacterDictionary (Ghislain Perdium, Narrator)
├── voice_selection.json              # VoiceSelectionApproval (Primary: Ngọc Huyền, Char: Nam An)
├── story_summary.md                  # Lore summary
└── chapters/
    └── 1/
        ├── chapter.json              # ChapterMetadata with completed ingest, agent, audio stages
        ├── manifest.json             # PanelManifest (5 active panels: panel_001 .. panel_005)
        ├── script.json               # RecapScript (3 segments: '001', '002_1', '003')
        ├── script_missing_panel.json # Adversarial script referencing missing panel_missing_999.jpg
        ├── panels/
        │   ├── panel_001.jpg         # 800x600 JPEG (Intro scene)
        │   ├── panel_002.jpg         # 800x600 JPEG (Multi-panel segment 002_1)
        │   ├── panel_003.jpg         # 800x600 JPEG (Multi-panel segment 002_1)
        │   ├── panel_004.jpg         # 800x600 JPEG (Multi-panel segment 002_1)
        │   └── panel_005.jpg         # 800x600 JPEG (Omitted valid panel: in manifest, NOT in script)
        ├── raw/
        │   └── page_001.jpg          # 800x3000 JPEG
        └── audio/clips/
            ├── audio_1_001.wav       # 3.5s 48kHz PCM WAV (Voice: Ngọc Huyền, neutral)
            ├── audio_1_002_1.wav     # 5.2s 48kHz PCM WAV (Voice: Nam An, dramatic, [hắng giọng])
            └── audio_1_003.wav       # 4.0s 48kHz PCM WAV (Voice: Ngọc Huyền, sad, [thở dài])
```

### Edge-Case Matrix Covered by Fixture:
1. **Non-sequential `seq_id`:** Segment `"002_1"` maps directly to `audio_1_002_1.wav` without assuming integer indices.
2. **Multi-panel segment:** Segment `"002_1"` contains 3 panels: `["panel_002.jpg", "panel_003.jpg", "panel_004.jpg"]`.
3. **Multi-voice & Emotion:**
   - `"001"`: `"Ngọc Huyền"`, speed 1.0, emotion `"neutral"`.
   - `"002_1"`: `"Nam An"`, speed 1.15, emotion `"dramatic"`, cue `"[hắng giọng]"`.
   - `"003"`: `"Ngọc Huyền"`, speed 0.95, emotion `"sad"`, cue `"[thở dài]"`.
4. **Omitted Valid Panel:** `panel_005.jpg` is present on disk and in `manifest.json`, but omitted from `script.json`. Demonstrates valid N:M panel selection.
5. **Intentionally Missing Panel:** `script_missing_panel.json` contains segment `"004_missing_test"` referencing `panel_missing_999.jpg`. `panel_missing_999.jpg` is guaranteed absent from disk to verify fail-fast behavior.
6. **Fixture Test Command:** `./venv/bin/pytest tests/unit/test_synthetic_fixture.py -v` (7 passed in 0.10s).

---

## 8. Gate 0 Completion & Sign-off

- [x] Baseline Git HEAD verified: `a9920a53b54e6aba30bcba3d3ac60323d74c9336`.
- [x] Baseline test suite executed: 554 tests passing.
- [x] Canonical stage inputs/outputs locked across all 5 operational stages.
- [x] Strict architectural guardrails enforced (zero cloud LLM/TTS, zero external DB, fail-fast).
- [x] Canonical small synthetic test fixture created and validated with unit tests.
- [x] Disjoint file ownership boundaries established for Phase 1 (A1 vs A2) and Phase 2 (A3 vs A4).
- [x] Contract decisions and specifications published in `docs/engineering/contract_decisions.md`.

**Phase 0 Gate Status:** PASSED. Phase 1 is unlocked for execution by workers A1 and A2.
