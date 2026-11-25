# Vietnamese Narrative Recap & Scripting Guide

## 1. Storytelling Style: Kể Chuyện & Lồng Thoại

Recap scripts are dramatic Vietnamese stories — never dry third-person bullet summaries:
- **Direct dialogue integration (Lồng thoại trực tiếp):** Weave original bubble lines naturally into the narration with matching tone instead of distant indirect paraphrasing (*"Hắn báo rằng..."*).
  - *Weak:* `"Thạch đại phu nói với Lưu lão gia rằng ông đã trúng hàn độc."`
  - *Strong:* `"Thạch đại phu bất giác rụt tay lại, giọng run lên: 'Lưu lão gia... trong người ngài ẩn chứa một luồng hàn độc vô cùng quỷ dị!'"`
- **Faithful storytelling:** Ground every plot twist, realm name, skill, and character motive strictly in visual/OCR panel evidence. Never fabricate lore or filler events.
- **The 5-Second Golden Hook (`seq_id: "001"`):** Open immediately in conflict, mystery, or sharp contrast — no generic channel greetings (*"Chào mừng các bạn..."*).

### 4-Part Beat Structure (Flexible)

When a key story panel merits breathing room (~4–8 seconds), combine up to four elements as natural for the scene:
1. **Bối cảnh / Trạng ngữ** *(thời gian, không gian, không khí)*
2. **Chủ thể & Thần sắc** *(trang phục, ánh mắt, biểu cảm)*
3. **Hành động / Biến chuyển** *(đòn đánh, phản ứng thể chất, khí thế)*
4. **Lời thoại trực tiếp** *(nguyên văn ngữ khí nhân vật)*

Fast action exchanges or quick reactions need only the elements that keep momentum sharp.

---

## 2. Canonical Single-Narrator Strategy & Emotion Cues

Production standard is strictly **`single_narrator`**:
- One user-confirmed VieNeu-TTS v3 Turbo preset (defaulting to `Ngọc Huyền` or another chosen catalog voice from Q03) narrates both storytelling and character dialogue seamlessly using natural Vietnamese punctuation, phrasing, and hierarchical pronouns (`Sư phụ / Đồ nhi`, `Huynh / Đệ`, `Lão gia chủ / Vãn bối`).
- Do **not** assign different `voice_id` presets per character in canonical `script.json` scripts.
- **Emotion metadata:** Choose `emotion` from `neutral`, `dramatic`, `suspense`, `sad`, `excited`, `dialogue`.
- **Supported VieNeu audible tags (`emotion_cue`):**
  - `[cười]` — sinister, amused, or sarcastic laugh
  - `[thở dài]` — heavy sigh, fatigue, sorrow
  - `[hắng giọng]` — stern clearing of throat to command room attention
- Use ellipsis `...` for ~0.4–0.6s pauses and em-dash `—` for dramatic beats.

---

## 3. N:M Panel Mapping & Soft Pacing Guidance

Segment-to-panel relationships in `script.json` (`evidence_panel_ids` / `panel_ids`) are flexible **N:M**:
- **1:1 (Standard):** One story beat paired with one key panel.
- **1:N (Multi-panel beat):** Two or more sequential panels paired with one unified narration (`["panel_002.jpg", "panel_003.jpg"]`) for rapid combat combos or gutter splits.
- **N:1 (Panel reuse):** Multiple dialogue segments reusing the same wide establishing panel while characters converse.
- **Selective omission:** Purely redundant filler strips or `skip` panels may be omitted, **but every panel with core dialogue, plot reveals, or decisive combat MUST be retained.** Any omitted active panels appear in `review_pack` for human audit.

**Pacing guidance (soft guideline, not a hard validation error):**
- VieNeu-TTS averages ~3.5–4.5 Vietnamese syllables/second.
- Ideal segment length is **50–250 characters** (~3–12 seconds of speech) divided cleanly at clause boundaries — short punchy lines like *"'Cút!'"* or slightly richer poetic beats are valid whenever natural to the artwork.

---

## 4. Validation & Human-Driven QA

1. Save `script.json` after each batch, then verify schema, panel evidence paths, and soft pacing budget:
   ```bash
   manga-recap validate --project <slug> --chapter <N> --pacing --json
   ```
2. Flag unresolved ambiguities with `"status": "needs_review"` and `[CAN_XAC_MINH: <lý_do>]`.
3. Revise specific `seq_id` segments cleanly upon human reviewer notes after `preview`; unchanged utterance WAVs stay cached via SHA-256 utterance fingerprints.
