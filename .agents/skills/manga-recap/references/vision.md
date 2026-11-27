# Vision, Contact Sheets, OCR & Framing Guide (V7)

## 1. Visual Evidence Protocol & Tool Boundaries

1. **Evidence-Based Scripting Only:**
   - Visually inspect panel crops (`panels/panel_*.jpg`) and multi-panel contact sheets (`contact_sheets/sheet_*.jpg`).
   - Authoring `script.json` from panel filenames or metadata alone is prohibited.
2. **Decision API vs. Agent Cognition:**
   - `gemini-decision-1` handles closed-set panel routing only: classifying `active` / `context_only` / `skip` (or `needs_review` on low-confidence/blocked edges), flagging structural `needs_split` / `merge_with_prev` candidates for VCS repack, and proposing guarded 4-sided edge trims (`{0, 10, 20, 30, 40}%` per edge); scroll vs static framing is derived deterministically from accepted crop geometry.
   - The **coding agent** owns all semantic reading, OCR across languages (English, Korean/Hangul, Japanese, Chinese), bubble-tail speaker attribution, story continuity, and narrative emphasis.
3. **Human-Gated Quality:**
   - Visual and storytelling fidelity is verified directly by the human reviewer using `video/preview.mp4`, `review_pack.md`, and `review_pack.json`.
4. **Batch Inspection (5 Panels, 2-Panel Overlap):**
   - Inspect contact sheets (`#001`, `#002` badges) in overlapping windows (e.g., `1–5`, `4–8`, `7–11`) so fights, expressions, and speech bubble continuity across cuts stay unbroken.
   - Open high-resolution files in `panels/panel_*.jpg` whenever speech bubbles have dense or small text.

---

## 2. Multi-Language OCR & Uncertainty Protocol

- **Accurate Reading:** Transcribe meaning faithfully from speech bubbles and narration boxes, verifying speakers via bubble tail pointers and expressions.
- **SFX & Overlays:** Translate combat SFX overlays into vivid Vietnamese action prose instead of literal phonetic syllables.
- **Strict Anti-Hallucination & Uncertainty Flag:**
  - Never invent unseen lore, realms, or missing dialogue.
  - Whenever dialogue is cropped at a panel edge, obscured, or visually ambiguous:
    - Set segment `status: "needs_review"`.
    - Prepend `[CAN_XAC_MINH: <lý_do_chi_tiết>]` to `narrator_text`.
    - Describe only physically visible evidence until human review clarifies it.

---

## 3. Gutter Anomalies & Panel Role Classification

| Manifest Status | Meaning & Video Behavior |
| --- | --- |
| `active` | Story artwork rendered into video segments (`panel_ids` + `evidence_panel_ids`). |
| `context_only` | Plain text box, recap flash, or narration card without story art; informs narration without replacing primary story panels. |
| `skip` | Website promo banners, scanlation watermarks, blank spacers, or divider strips. Strictly excluded. |
| `needs_review` | Ambiguous gutter cut or illegible crop requiring human/agent `repack` inspection before scripting. |

---

## 4. Camera Motion & 16:9 `landscape_fit` Framing

Every segment sets `camera_motion` (plus optional normalized `focus_box` in `[0.0, 1.0]`):
- **Supported motions (V7):** `static`, `slow_pan_down`, `static_focus`.
- **Canonical aspect mode:** Fixed 16:9 `landscape_fit` (`H/W <= 1.3` static composition; taller strips pan downward cleanly via `slow_pan_down` or ordered static segments).

**Subject-Centric Safety Rules:**
1. **Chat Box Avoidance:** Speech bubbles usually sit near top corners (`y: 0.05–0.35`); point `focus_box` toward faces and expressions (`fy ≈ 0.45–0.70`) and action rather than zooming onto bubbles.
2. **Safe Bounds:** Keep `focus_box` width/height `>= 0.45` and preserve `15%–20%` margin around heads and key weapons.
3. **Uncertain Framing:** Prefer clean `static` full framing whenever composition is uncertain.
