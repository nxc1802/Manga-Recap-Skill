---
name: manga-recap
version: "7.1.0"
description: Create Vietnamese manga recap scripts, narration, thumbnails, and videos from online chapters or local panel images.
---

# Manga Recap (V7 Core-Only)

The coding agent owns visual comprehension, OCR, character tracking, Vietnamese single-narrator scripting, framing cues, and 16:9 thumbnail artwork. Python owns deterministic ingestion, VCS panel segmentation, closed-set `gemini-decision-1` routing, VieNeu-TTS v3 Turbo synthesis, FFmpeg 16:9 `landscape_fit` rendering, Kaggle Dual-T4 Policy B orchestration, Hugging Face archival, and YouTube upload/verification. All series state lives under `data/projects/<slug>/`.

## Core Boundaries

- Out of scope: creative cloud LLMs/VLMs, cloud speech APIs, mock TTS in production, Kaggle Policy A/CPU/single-GPU fallbacks, portrait/blurred rendering modes, and external databases.
- Always visually inspect `contact_sheets/` and full-resolution `panels/` before writing `script.json`.
- Confirmed setup choices (`Q01–Q05, Q08–Q10`), narrator voice selection, and Chapter 1 human preview approval fail closed in runtime guards.
- Invoke `manga-recap ... --json` so state, `next_action`, blockers, and errors use the unified JSON envelope.

---

## Stage 1 · Series Setup (8 Confirmed Questions)

Collect all **eight** explicit answers via the host's structured question tool (`answer_source: "user"`). Stop if incomplete:

| ID | Key | Ask the User | Expected Payload |
| --- | --- | --- | --- |
| Q01 | `source` | Chapter/range and source URL or local image dir | `{"chapters": "...", "source": "<url or dir>"}` |
| Q02 | `compute` | Compute backend (`local` or `kaggle` Dual-T4) | `"local"` or `"kaggle"` |
| Q03 | `voice` | Single confirmed narrator preset from VieNeu catalog | Voice name (e.g. `"Ngọc Huyền"`) |
| Q04 | `speed` | Reading speed multiplier (`0.5`–`2.0`) | Float (default `1.0`) |
| Q05 | `subtitles` | Burn Vietnamese subtitles onto video frames? | `true` or `false` |
| Q08 | `publish` | Local only, or confirmed YouTube channel + visibility | `{"no_upload": true}` or `{"account": "<alias>", "privacy": "private\|unlisted\|public"}` |
| Q09 | `quality` | Canonical render target (`"preview"` 720p or `"final"` 1080p) | `"preview"` or `"final"` |
| Q10 | `audio` | No BGM, or BGM file + `bgm_volume` + `voice_volume` | `{"none": true}` or `{"bgm_path": "...", "bgm_volume": 0.15, "voice_volume": 1.0}` |

Persist confirmed setup & narrator voice:
1. `manga-recap setup record --project <slug> --answers-json <path> --json`
2. Write `data/projects/<slug>/voice_selection.json` (`approved_strategy: "single_narrator"`, `status: "approved"`). Optional audition clips: `.venv/bin/python scripts/devtools/tts_benchmark.py --json`.
3. For Kaggle compute (`Q02 = "kaggle"`), keep custom `bgm_path` project-relative.

---

## Stage 2 · Prepare Chapter 1 (`prepare`)

```bash
manga-recap prepare --project <slug> --chapter 1 --json
manga-recap status --project <slug> --chapter 1 --json
```
*(Granular repair tools: `crawl`, `segment`, `decide --refresh`, `repack`, `panel-inspect`, `inspect`.)*

Runs ingestion, VCS segmentation, contact-sheet building, and closed-set `gemini-decision-1` classification (`active`, `context_only`, `skip`, `needs_review`, `needs_split`/`merge_with_prev`, guarded 4-sided edge trims `{0..40%}`). `prepare` never writes `script.json`. Confirm `status` reports `next_action: "author_script"` and resolve required `needs_review` panels before authoring.

---

## Stage 3 · Author Chapter 1 (`script.json`)

Read `characters.json`, `story_summary.md`, `manifest.json`, and inspect `contact_sheets/` + high-res `panels/`:
- Guides: [Vision & Framing](references/vision.md) · [Vietnamese Narration](references/narration.md) · [Characters & Continuity](references/characters.md) · [Script Template](templates/script.json.template)
- Inspect in overlapping batches (5 panels, 2-panel overlap); set `"status": "needs_review"` + `[CAN_XAC_MINH: <reason>]` on ambiguous/cropped evidence.
- Use `voice_strategy: "single_narrator"` and supported camera motions (`static`, `slow_pan_down`, `static_focus`) only.
- Save atomically to `chapters/1/script.json`, update `characters.json` & `story_summary.md`, then validate:
  ```bash
  manga-recap validate --project <slug> --chapter 1 --pacing --json
  ```

---

## Stage 4 · Preview Chapter 1 (`preview`)

```bash
manga-recap preview --project <slug> --chapter 1 --json
```

Automatically routes to Local or Kaggle Dual-T4 according to confirmed `Q02` setup (`preview.mp4`, `preview.srt`, `review_pack.md`, `review_pack.json`).

---

## Stage 5 · Human Approval Gate (`approve`)

Pause and present `chapters/1/video/preview.mp4`, `chapters/1/subtitles/preview.srt`, `review_pack.md`, and `review_pack.json` (plus any `[CAN_XAC_MINH: ...]` flags) to the human reviewer. Once approved, bind the exact `config_fingerprint` and `artifact_hash` from `review_pack.json`:

```bash
manga-recap approve --project <slug> \
  --reviewer "<user>" \
  --config-fingerprint "<sha256>" \
  --artifact-hash "<sha256>" \
  --json
```

---

## Stage 6 · Prepare & Author Remaining Chapters

Repeat **Stage 2 (`prepare`)** and **Stage 3 (`script.json` + `validate --pacing`)** for every remaining chapter in the confirmed `Q01` range, keeping `characters.json` and `story_summary.md` updated continuously across chapters:

```bash
manga-recap prepare --project <slug> --chapter <N> --json
manga-recap validate --project <slug> --chapter <N> --pacing --json
```

---

## Stage 7 · Full Production & Compile (`produce`)

```bash
manga-recap produce --project <slug> --chapters <range> --compile --json
manga-recap status --project <slug> --json
```
*(Granular tools: `tts`, `visual-plan`, `render`, `compile`, `cloud run|produce-series|pull|reconcile|accounts|info`.)*

- Chapter 1 promotes exact `preview.mp4` bytes to `final.mp4` at the confirmed `Q09` target; changing setup/script invalidates approval and requires re-review.
- Kaggle runs execute canonical **Dual-T4 Policy B** (`h264_nvenc`, produce `batch_size=16`, standalone TTS `batch_size=32`) via the automatic Chunk Planner on a single pinned account without CPU/single-GPU/account fallbacks.
- Sync completed remote jobs via `cloud pull`, or refresh remote status via `cloud reconcile` after cancelling in Kaggle UI.

---

## Stage 8 · Package & Release (`publish`)

1. **Thumbnail:** Save clean 16:9 `chapters/<N>/thumbnail.jpg` (or `output/<slug>/feature_thumbnail.jpg`).
2. **Metadata & Channel Gate:** Generate SEO metadata (`manga-recap publish -p <slug> -c <N-or-feature> --action metadata --json`), inspect configured channels (`manga-recap youtube accounts --json`), and reconfirm exact `--account` and `--privacy` with the user.
3. **Upload & Live Verification (canonical paths only, after explicit user authorization):**
   ```bash
   manga-recap publish --project <slug> --chapter <N-or-feature> --account <alias> --privacy <vis> --json
   manga-recap publish --project <slug> --chapter <N-or-feature> --action verify --account <alias> --json
   ```

---

## Optional Maintenance · Private Archive & Explicit Cleanup

Run only when explicitly authorized by the user:
```bash
manga-recap archive push --project <slug> --json
manga-recap archive verify --project <slug> --json
manga-recap clean --project <slug> --dry-run --json
```
- Deletion requires explicit `--no-dry-run --confirm-project <slug>`, and `--purge-raw` fails closed until `archive_receipt.json` is `verified`.
- Standalone archive restore utility (outside production runtime): `.venv/bin/python scripts/restore_archive.py -p <slug> --target-dir <empty-dir> --json`.
