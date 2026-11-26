---
name: manga-recap
version: "6.0.0"
description: Create Vietnamese manga recap scripts, narration, thumbnails, and videos from online chapters or local panel images.
---

# Manga Recap

The coding agent owns visual comprehension, OCR, character tracking, Vietnamese storytelling, framing choices, and 16:9 thumbnail artwork. Python owns deterministic ingestion, panel segmentation, closed-set Decision routing (`gemini-decision-1`), VieNeu-TTS v3 Turbo speech synthesis, FFmpeg rendering, Kaggle orchestration, Hugging Face archival, and YouTube upload/verification. All series state lives under `data/projects/<slug>/`.

## Quick Rules & Boundaries

- Creative cloud LLMs/VLMs, cloud speech APIs, mock TTS in production, and external databases are out of scope.
- Never synthesize `script.json` from filenames or metadata alone; visually inspect `contact_sheets/` and full-resolution `panels/` first.
- All setup choices, narrator voice selection, and Chapter 1 human preview approval fail closed in runtime guards.
- Pass `--json` on CLI invocations so failures and resume state (`next_action`, `blocker`) return structured JSON envelopes.

---

## Stage 1 · Series Setup (Human Form Gate)

Before creating jobs for a new series, collect all **eight** explicit answers using `ask_question` or the host's structured question tool. A preselected default never counts until submitted by the user. Stop immediately if the form is incomplete or the question tool is unavailable.

| ID | Semantic Key | Ask the User | Expected Payload |
| --- | --- | --- | --- |
| Q01 | `source` | Chapter/range and source URL or local image directory | `{"chapters": "...", "source": "<url or dir>"}` |
| Q02 | `compute` | Execution backend: local machine or Kaggle Dual-T4 GPU | `"local"` or `"kaggle"` |
| Q03 | `voice` | Single confirmed narrator preset from the VieNeu catalog | Catalog voice name (e.g. `"Ngọc Huyền"`) |
| Q04 | `speed` | Reading speed multiplier (`0.5` to `2.0`) | Float (default `1.0`) |
| Q05 | `subtitles` | Burn Vietnamese subtitles onto video frames? | `true` or `false` |
| Q08 | `publish` | Save locally only, or upload to a listed YouTube channel + visibility | `{"no_upload": true}` or `{"account": "<alias>", "privacy": "private\|unlisted\|public"}` |
| Q09 | `quality` | Render target profile: Preview 720p or Final 1080p | `"preview"` or `"final"` |
| Q10 | `audio` | No BGM, or BGM file path + music gain (`0–1`) + voice gain (`0–2`) | `{"none": true}` or `{"bgm_path": "...", "bgm_volume": 0.15, "voice_volume": 1.0}` |

Persist confirmed answers and narrator voice before running chapter commands:
1. Save JSON keyed by QID (or semantic keys) where each confirmed item includes `user_answer` and `answer_source: "user"`.
2. Record series config: `manga-recap setup record --project <slug> --answers-json <path> --json`.
3. Write the confirmed single narrator and speed into `data/projects/<slug>/voice_selection.json` (`status: "approved"`). Run `manga-recap tts_benchmark --json` first if the user wants sample clips before choosing Q03.
4. For Kaggle compute (`Q02 = "kaggle"`), keep custom `bgm_path` project-relative. Later chapters automatically inherit the confirmed setup.

---

## Stage 2 · Prepare Assets (`prepare`)

Run deterministic ingestion, page-to-panel VCS segmentation, contact-sheet building, and closed-set `gemini-decision-1` classification (`active` story art, `context_only` text cards, `skip` ads/watermarks, guarded `needs_review` escalation, `needs_split`/`merge_with_prev` repack, and 4-sided safe framing trims with deterministic scroll geometry). `prepare` / `decide --json` reports group counts, `needs_review` panels, fast-path status, and estimated API token cost:

```bash
manga-recap prepare --project <slug> --chapter <N> --json
```
*(Compatibility granular equivalents: `crawl` → `segment` → `decide` → `inspect`. `decide` also supports `--dry-run`, `--reapply-policy`, `--refresh-decisions`, and `--decision-policy legacy_9preset` rollback; use `--skip-decide` on `prepare` only when `GEMINI_API_KEY` is intentionally omitted for manual classification via `repack`.)*

**Output:** `raw/`, `panels/`, `contact_sheets/`, `manifest.json`, and updated `chapter.json`. Resolve any flagged `needs_review` panels before authoring/rendering; `prepare` never writes `script.json`.
**Next check:** `manga-recap status --project <slug> --chapter <N> --json` reports `next_action: "author_script"`.

---

## Stage 3 · Author Narration (`script.json`)

Load `characters.json`, `story_summary.md`, `manifest.json`, and inspect `contact_sheets/sheet_*.jpg` + high-res `panels/panel_*.jpg`:

- Read detailed guides before writing scripts: [Vision & Framing](references/vision.md) · [Vietnamese Narration](references/narration.md) · [Characters & Continuity](references/characters.md)
- Canonical example schema: [Script Template](templates/script.json.template)
- Process in visual batches of 5 panels with 2-panel overlap to trace continuous fights and speaker tail pointers cleanly.
- Author in **single narrator lồng thoại** style (`voice_strategy: "single_narrator"`), retaining all essential story panels via flexible N:M `panel_ids` mapping.
- When dialogue or visual evidence is truncated, obscured, or ambiguous, mark `status: "needs_review"` and prepend `[CAN_XAC_MINH: <lý do>]` — never hallucinate lore or unseen words.
- Save atomically after each scripting batch into `data/projects/<slug>/chapters/<N>/script.json`, update `characters.json` & `story_summary.md`, and run schema/evidence validation:

```bash
manga-recap validate --project <slug> --chapter <N> --pacing --json
```

---

## Stage 4 · Preview Chapter 1 (`preview`)

Generate Chapter 1 audio (VieNeu-TTS), render the Chapter 1 `preview.mp4` + subtitles, and emit the static human review bundle (`review_pack.md`, `review_pack.html`, `review_pack.json`):

```bash
manga-recap preview --project <slug> --chapter 1 --json
# Add --kaggle when Q02="kaggle" and running Chapter 1 preview remotely
```
*(Compatibility granular equivalents: `tts` → `render --quality preview` → `review-pack`.)*

**Stop condition:** Always pause after `preview` and present `video/preview.mp4` plus `review_pack.html` / `review_pack.md` (highlighting any `[CAN_XAC_MINH: ...]` flags or omitted panels) to the human reviewer.

---

## Stage 5 · Human Review Approval (`approve`)

Once the human watches Chapter 1's preview and approves the tone, voice, pacing, subtitles, and framing, bind approval strictly to the exact `config_fingerprint` and `artifact_hash` reported inside `review_pack.json`:

```bash
manga-recap approve --project <slug> \
  --reviewer "<user>" \
  --config-fingerprint "<sha256>" \
  --artifact-hash "<sha256>" \
  --json
```
*(Compatibility alias: `review-approve`.)*

If any setup answer, `voice_selection.json`, Chapter 1 script, manifest, or visual plan changes afterward, approval becomes stale and must be re-run after re-reviewing.

---

## Stage 6 · Produce Approved Chapters (`produce`)

Produce verified final chapters locally or on Kaggle Dual-T4 GPU according to `Q02` (`compute`):

```bash
manga-recap produce --project <slug> --chapters <range> --compile --json
```
*(Compatibility equivalents: `render --quality final` for Chapter 1 promotion, `batch`, `cloud produce-series`, `cloud pull`, `compile`.)*

**Resume & external monitoring:**
- Check `manga-recap status --project <slug> --json` to inspect chapter states, `next_action`, blockers, resolved Kaggle policy/hardware telemetry, and chunked series resume receipts after interruptions.
- Kaggle jobs persist pre-submit intent (`SUBMITTING`) and reconcile `SUBMIT_UNKNOWN` / `CLIENT_TIMEOUT` states cleanly before retrying; use `manga-recap cloud pull --project <slug> [--chapter <N>] --json` to resume running kernels or sync completed chunk outputs.
- Monitor Kaggle jobs after **5 minutes** via `status` / `cloud pull`; report queue stalls or OAuth expirations promptly instead of looping blindly. (`--cloud-policy auto` selects verified `dual_tts_shared_encode` on Dual-T4 NVENC hardware with `batch_size=16` for `produce` / `32` for standalone `tts`, and falls back to `baseline_split` otherwise; pass `--cloud-policy baseline_split` for explicit single-producer rollback.)

---

## Stage 7 · Package, Publish, Archive & Clean

1. **Thumbnail:** Select 2–3 representative active panels as visual references and create a clean, speech-bubble-free 16:9 image saved as `chapters/<N>/thumbnail.jpg` (or `.png`), or `output/<slug>/feature_thumbnail.jpg` for multi-chapter compilations.
2. **Metadata:** Generate SEO title, timestamps, and Vietnamese description:
   ```bash
   manga-recap publish --project <slug> --chapter <N> --action metadata --json
   ```
3. **Channel Confirmation Gate:** Immediately before uploading, list configured channels (`manga-recap youtube accounts --json`) and reconfirm target `--account` and `--privacy` with the user.
4. **Upload & Verify:**
   ```bash
   manga-recap publish --project <slug> --chapter <N> --account <alias> --privacy <vis> --wait --json
   # Or run verification separately: --action verify
   ```
   *(If OAuth authorization expires or is headless-blocked with exit code 4, instruct the user to run `manga-recap youtube accounts --account-action add --account <name>` in an interactive terminal — browser login waits up to 5 minutes.)*
5. **Private Archival & Safe Cleanup:**
   ```bash
   manga-recap archive push --project <slug> --json
   manga-recap archive verify --project <slug> --json
   manga-recap clean --project <slug> --dry-run --json
   ```
   `clean` defaults to dry-run. Actual deletion requires `--no-dry-run --confirm-project <slug>`, and `--purge-raw` fails closed unless `archive_receipt.json` is `verified` for the current files.
