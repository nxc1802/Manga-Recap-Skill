# Vision, Contact Sheets, OCR & Framing Guide

## 1. Visual Evidence Protocol & Tool Boundaries

1. **Evidence-Based Scripting Only:**
   - Visually inspect panel crops (`panels/panel_*.jpg`) and multi-panel contact sheets (`contact_sheets/sheet_*.jpg`).
   - Authoring `script.json` from panel filenames or metadata alone is prohibited.
2. **Decision API vs. Agent Cognition:**
   - `gemini-decision-1` handles closed-set panel routing only: classifying `active` / `context_only` / `skip` (or `needs_review` on low-confidence/blocked edges), flagging structural `needs_split` / `merge_with_prev` candidates for VCS repack, and proposing guarded 4-sided edge trims (`{0, 10, 20, 30, 40}%` per edge); scroll vs static framing is computed deterministically from the accepted crop geometry.
   - The **coding agent** owns all semantic reading, OCR across languages (English, Korean/Hangul, Japanese, Chinese), bubble-tail speaker attribution, story continuity, and narrative emphasis.
3. **Zero Automated Quality Benchmarks:**
   - The pipeline uses no automated OCR confidence gates or LLM-as-judge prompts. Visual and storytelling fidelity is verified directly by the human reviewer on `review_pack.html` / `video/preview.mp4`.
4. **Batch Inspection (5 Panels, 2-Panel Overlap):**
   - Inspect contact sheets (`#001`, `#002` badges) in overlapping windows (e.g., `1–5`, `4–8`, `7–11`) so fights, expressions, and speech bubble continuity across cuts stay unbroken.
   - Zoom into individual high-resolution files in `panels/panel_*.jpg` whenever speech bubbles have small or dense text.

---

## 2. Multi-Language OCR & Uncertainty Protocol

- **Accurate Reading:** Transcribe meaning faithfully from speech bubbles and narration boxes, verifying speakers via bubble tail pointers and open mouths.
- **SFX & Overlays:** Translate the atmospheric weight of combat SFX overlays into vivid Vietnamese action prose instead of spelling out literal phonetic sounds.
- **Strict Anti-Hallucination & Uncertainty Flag:**
  - Never invent unseen lore, martial arts realms, or missing bubble words.
  - Whenever dialogue is cropped at a panel edge, blurred/obscured, or scene action is ambiguous:
    - Set segment `status: "needs_review"`.
    - Prepend `[CAN_XAC_MINH: <lý_do_chi_tiết>]` to `narrator_text` (e.g., `[CAN_XAC_MINH: chữ trong bong bóng thoại bị mờ]`).
    - Describe only physically visible evidence until human review clarifies it.

---

## 3. Gutter Anomalies & Panel Role Classification

Check `contact_sheets/` and `manifest.json` for structural issues before writing `script.json`:

| Manifest Status | Meaning & Video Behavior |
| --- | --- |
| `active` | Story artwork rendered into video segments (`panel_ids` + `evidence_panel_ids`). |
| `context_only` | Plain text box, recap flash, or narration card without story art; informs narration via a neighboring `active` visual or omitted from direct display unless explicitly required. |
| `skip` | Website promo banners, scanlation watermarks, blank spacers, or divider strips. Strictly excluded from narration and video. |
| `needs_review` | Gutter cut through a face/bubble or illegible image requiring inspection or `repack`. |

**Defect Remedies:**
- **Mid-art split across consecutive crops:** Combine both panels into one `script.json` segment (`evidence_panel_ids: ["panel_004.jpg", "panel_005.jpg"]`) with `pan_down` or `vertical_scroll`.
- **Merged composite frame:** Either split via `panel_plan.json` (`repack`) or direct sequential segments with distinct upper/lower `focus_box` regions.

---

## 4. Camera Motion, Framing & Chat Box Avoidance

Every segment sets `camera_motion` (plus optional normalized `focus_box` in `[0.0, 1.0]`):
- **Supported motions:** `static`, `pan_left`, `pan_right`, `pan_up`, `pan_down`, `zoom_in`, `zoom_out`, `focus_crop`, `vertical_scroll`.
- **Render aspect behavior:**

| Display Crop H/W Ratio | Runtime Rendering Behavior |
| --- | --- |
| `<= 1.3` | Static 16:9 composition with adaptive blurred side backdrop |
| `> 1.3` | Smooth downward pan / scroll when vertical room and evidence allow |
| `> 2.4` with static series config | Ordered top-to-bottom static crops across the tall strip |

**Subject-Centric Safety Rules:**
1. **Never center camera zoom/focus on chat bubbles (`Chat Box Avoidance`):** Speech bubbles usually sit near the top (`y: 0.05–0.35`) or corners; subtitles and VieNeu-TTS already deliver the spoken dialogue. Point `focus_box` toward the character's face/expression (`fy ≈ 0.45–0.70`) and torso/action.
2. **Anti-Overzoom:** Never shrink `focus_box` below `45%` (`0.45`) width/height of the panel, keep Ken Burns zoom within `1.15x`, and preserve `15%–20%` margin around heads, hats, and weapons.
3. **Uncertain text regions:** If text placement is uncertain or `Q02/render` disables motion, prefer clean `static` full framing (`center_crop` >= 80%).
