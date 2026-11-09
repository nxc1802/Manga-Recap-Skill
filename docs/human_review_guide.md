# Human Review & Editorial Quality Assurance Guide
**Manga Recap Agentic Skill V3 — P1 Human-Driven Agent QA**

- **Document Version:** 1.0.0
- **Audience:** Human Reviewers, Video Editors, and Coding Agents
- **Target Language:** Vietnamese (Recap Output) & English (Engineering Workflow)

---

## 1. Core Philosophy: Why Human-Driven QA?

Manga and webtoon recap videos are **artistic, narrative adaptations**, not literal machine translations. Evaluating storytelling quality requires human aesthetic judgment:
- **Zero Automated Quality Benchmarks**: This repository enforces a strict architectural rule: **ZERO automated OCR accuracy scores, ZERO automated narrative evaluation scores, and ZERO LLM-as-judge prompts**.
- **Human Reviewer as the Sole Quality Authority**: Machine heuristics cannot evaluate comic timing, emotional inflection, cultural nuances in Vietnamese pronouns (*"huynh, đệ, tiền bối, vãn bối"*), or dramatic tension. Storytelling quality, character voice authenticity, and visual composition are validated exclusively by human eyes and ears.
- **Agent Cognition Independence**: Coding agents freely select their vision, OCR, and reasoning methodology (e.g., visual inspection of contact sheets, full-resolution panel crops, or dialogue transcription) without having to produce artificial confidence metrics or satisfy automated accuracy gates.

---

## 2. Review Gate Lifecycle & Decoupled Status Architecture

The pipeline operates with three distinct Review Gates. Settings configured at the series level are inherited automatically by all chapters:

```
[Gate 0: Series Video Settings] ──► [Gate 1: Voice & Narration Strategy]
 (Configured ONCE in project.json)    (Approved ONCE in voice_selection.json)
                                                        │
                                                        ▼
[Stage 1: INGEST] ───► [Stage 2: AGENT] ───► [Stage 3: AUDIO] ───► [Stage 4: RENDER] ───► [Gate 2: Chapter Preview]
 (crawl/segment)       (script/validate)         (local TTS)        (1080p final)           (OPTIONAL inspection)
```

### 2.1 Series Gate 0: Video & Render Configuration
- **Location:** `data/projects/<project_slug>/project.json`
- **Execution:** Configured **ONCE** when creating a project.
- **Parameters:**
  - `render_settings`: Resolution (`1920x1080` or `1280x720`), FPS (`30`), video mode (`auto`, `landscape_fit`, `portrait_padded`, `portrait_vertical`), subtitle burn toggle (`burn_subtitles: true/false`), camera animation (`render_pan_mode: static/dynamic`).
  - `audio_settings`: Default voice ID (`Ngọc Huyền`), default speech speed (`1.0x`), BGM audio path, BGM volume (`0.15`), voice volume (`1.0`), ducking factor (`0.30`).
- **Inheritance Rule**: Every chapter in the project automatically inherits these settings. The agent and CLI **never repeatedly re-prompt** the user for subsequent chapters.

### 2.2 Series Gate 1: Voice Strategy Approval
- **Location:** `data/projects/<project_slug>/voice_selection.json`
- **Execution:** Approved **ONCE** per series during initial audition.
- **Approved Strategy**: Single Narrator (`single_narrator`) with optional audible emotion cues (`emotion_aware`: `[cười]`, `[thở dài]`, `[hắng giọng]`). Default voice: `Ngọc Huyền` (elegant Northern female voice with rich emotional cadence).
- **Inheritance Rule**: All chapters automatically inherit this approval.

### 2.3 Chapter Gate 2: Video Preview Verification & Decoupled Statuses
- **Location:** `data/projects/<project_slug>/chapters/<num>/chapter.json` (and `preview_approval.json`)
- **Execution:** Optional manual quality inspection at 720p or 1080p.
- **Independent Status Semantics**:
  - **`technical_status`**: Tracks deterministic machine execution (`pending`, `running`, `completed`, `failed`).
  - **`human_review_status`**: Tracks editorial evaluation (`unreviewed`, `approved`, `changes_requested`).
- **Bypass Rule (`--skip-preview`)**:
  - In unattended production runs or batch processing (`manga-recap batch`), passing `--skip-preview` allows direct 1080p rendering.
  - **CRITICAL STATUS INVARIANT**: When preview is bypassed, the chapter technical stage completes (`technical_status = "completed"`), but editorial review is strictly recorded as **`human_review_status = "unreviewed"`**.
  - **STRICT PROHIBITION**: The system and agent MUST NEVER automatically mark unreviewed chapters as `"approved"` without actual human verification.

---

## 3. The Minimal Human Review Pack

To enable human reviewers to inspect chapter assets thoroughly and quickly without building a complex frontend UI, the CLI generates a static, transparent **Human Review Pack**:

```bash
# Generate Review Pack in Markdown, HTML, and JSON
manga-recap review-pack --project <project_slug> --chapter <chapter_num> [--format md|html|json|all] --json
```

### Generated Artifacts in Chapter Directory:
- `review_pack.md`: Complete Markdown report viewable in any text editor, GitHub, or VS Code preview.
- `review_pack.html`: Standalone, responsive HTML page with embedded metrics, style badges, and visual contact sheets for one-click browser inspection.
- `review_pack.json`: Machine-readable summary for automation scripts.

### Review Pack Evidence Layers:
1. **Chapter Execution Summary**: Total segments, audio duration, manifest panel count, referenced panel count, omitted panel count, reused panel count, and needs-review flags.
2. **Visual Contact Sheets Gallery**: 4x4 visual grids (`contact_sheets/sheet_*.jpg`) displaying all chapter panels with stamped sequence badges (`#001`, `#002`). Reviewers can inspect the complete visual comic flow in under 60 seconds.
3. **Script-to-Panel Alignment Table (N:M Mapping)**:
   - `Seq ID`: Segment identifier (`001`, `002_1`, etc.).
   - `Panel IDs`: Source panels referenced (`panel_001.jpg`, or multi-panel `panel_002.jpg + panel_003.jpg`).
   - `Character / Voice`: Speaker name and assigned VieNeu-TTS voice ID.
   - `Camera / Focus`: Camera motion mode (`static`, `zoom_in`, `pan_down`, `focus_crop`) and normalized focus box coordinates.
   - `Duration`: Measured audio utterance duration in seconds.
   - `Narrator Text & Direct Dialogue`: Vietnamese prose, character quotes, and audible emotion tags.
   - `Review Flag`: Highlights segments requiring attention (`[CAN_XAC_MINH: ...]`).
4. **Omitted Panels Log**:
   - Comprehensive catalogue of panels extracted in `manifest.json` but omitted from `script.json`.
   - Displays dimensions, aspect ratio, manifest status, and omission reason.
   - **Essential Story Retention Check**: Allows the reviewer to verify that **no crucial story progression panels were accidentally omitted**.
5. **Reused Panels Log**:
   - Identifies panels referenced across multiple segments (e.g., wide dialogue shots held across turns).
6. **Media Artifacts & Preview Files**:
   - Direct file paths to synthesized `.wav` clips, `.srt` subtitles, and `.mp4` video.

---

## 4. Human Reviewer Inspection Checklist (5 Core Criteria)

Reviewers should evaluate the chapter against these 5 core criteria:

| # | Criterion | What to Look For | Pass Condition |
|---|---|---|---|
| **1** | **Faithful Storytelling (Anti-Hallucination)** | Compare `narrator_text` with speech bubbles and artwork on contact sheets. | No fabricated backstories, fake power tiers, or invented plot points not grounded in the manga. |
| **2** | **Essential Panel Retention** | Inspect the **Omitted Panels Log** in `review_pack.md`. | All panels with major plot reveals, crucial dialogue, or combat climax are retained in `panel_ids`. Omitted panels are strictly filler scenery, transitions, or ads. |
| **3** | **Voice Authenticity & Cadence** | Listen to audio clips or video audio. | Natural Vietnamese pronunciation of character names, respectful pronoun hierarchy (*sư phụ, đồ nhi, gia chủ*), and tasteful emotion cues (`[cười]`, `[thở dài]`). |
| **4** | **Visual Framing & Anti-Chat-Box** | Inspect video preview or `focus_box` settings. | Camera centers on character eyes, facial expressions, or martial arts action. Camera NEVER zooms into empty speech bubbles. Crop scale $\ge 0.45$, Ken Burns zoom $\le 1.15\times$. |
| **5** | **A/V Sync & Subtitle Legibility** | Watch video preview. | Subtitles fit within 1–2 lines without overflowing canvas. Speech is synchronized with on-screen panel pacing (holding 4–8s per visual beat). |

---

## 5. Editorial Decision & Feedback Protocol

### 5.1 Accepting and Approving the Chapter
When all 5 criteria pass:
1. Update chapter review status via CLI:
   ```bash
   manga-recap render -p <project_slug> -c <chapter_num> --quality final --approved --json
   ```
2. Or record approval directly in `data/projects/<project_slug>/chapters/<chapter_num>/preview_approval.json`:
   ```json
   {
     "reviewer": "Senior Editor",
     "approved_at": "2026-11-08T21:15:00Z",
     "status": "approved",
     "notes": "Chapter 1 narrative, voice cadence, and visual framing verified and approved."
   }
   ```
3. `chapter.json` updates `human_review_status = "approved"`.

### 5.2 Requesting Changes (Editorial Feedback Loop)
If any criterion fails, the reviewer rejects or requests edits by providing targeted, structured feedback:

#### Feedback Format:
Always cite specific `seq_id` and/or `panel_ids`:
```text
[CHANGE_REQUEST: Chapter 1]
- seq_id "002_1": Câu thoại của Ghislain Perdium cần đổi ngữ khí quyết liệt hơn: 'Ta tuyệt đối không lùi bước!' -> 'Dù có phải tan xương nát thịt, ta cũng quyết không lùi nửa bước!'. Bổ sung emotion_cue: '[hắng giọng]'.
- panel_id "panel_005.jpg": Panel này vẽ cảnh kiếm khí chém nát vách đá, là chiêu thức kết màn quan trọng, không được bỏ. Hãy bổ sung thành segment "004" với camera_motion "zoom_out".
```

### 5.3 Agent Remediation & Incremental Cache Preservation
When the agent receives editorial feedback:
1. **Targeted Edits**: The agent modifies only the requested segment(s) in `script.json` (or inserts new segments for missing essential panels).
2. **Audio Cache Preservation**:
   - VieNeu-TTS checks pre-existing `.wav` files in `audio/clips/` and compares waveform durations and text hashes.
   - Untouched segments are **preserved instantly without re-synthesis**.
   - Only modified or newly added segments are synthesized.
3. **Re-rendering**:
   - Re-running `manga-recap render` re-combines the audio and renders the updated video.
4. **Re-inspect**:
   - Re-run `manga-recap review-pack` to verify that all change requests have been resolved.

---

## 6. Uncertainty Protocol: How the Agent Flags Ambiguities

When an agent encounters ambiguous manga inputs:
- Speech bubbles obscured by compression or cut off at page edges.
- Unidentified silhouette or masked characters.
- Complex or untranslated martial arts terms.

**Mandatory Action**:
1. The agent sets `status: "needs_review"` on the segment.
2. The agent prepends `[CAN_XAC_MINH: <lý_do_chi_tiết>]` to `narrator_text` or `notes`.
3. The Review Pack automatically extracts and highlights these items under **Section 4: Segments Requiring Editorial Verification**.
4. The human reviewer makes the final editorial decision on how to phrase the line.
