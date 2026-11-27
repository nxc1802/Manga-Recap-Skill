# Character Consistency & Series Memory Rules

## 1. Canonical Naming & Honorific Hierarchy

1. **Consistent Canonical Names in `characters.json`:**
   - Establish one canonical Vietnamese or standardized Sino-Vietnamese name upon a character's first appearance (e.g., `Tiêu Phàm`, `Lý Bạch`, `Lão Gia Chủ`).
   - Store alternate translations, nicknames, or titles in `aliases` (e.g., `["Đệ Thất Thiếu Gia", "Phàm ca"]`).
   - Never oscillate between English romanization (`Li Bai`) and Sino-Vietnamese (`Lý Bạch`) inside a series.
2. **Rigid Pronoun Consistency:**
   - Preserve exact hierarchical address (`Sư phụ / Đồ nhi`, `Lão gia chủ / Vãn bối`, `Sư huynh / Sư đệ`) across every chapter so a **single narrator** conveys who speaks to whom cleanly without switching vocal presets.

---

## 2. Visual Identity Tracking (`core_features`)

Record persistent physical details in `data/projects/<slug>/characters.json`:
- **Facial marks & eyes:** Scars, eye color, beard, tattoos, expressions.
- **Hair:** Color, length, style (e.g., `"Tóc bạc dài buộc cao sau lưng"`).
- **Attire & weapons:** Robes, armor motifs, signature blades/artifacts.

Before attributing dialogue in overlapping panel batches, verify visible visual traits against `characters.json` rather than guessing from panel order alone.

---

## 3. Single-Narrator Alignment

Canonical V7 production uses **`single_narrator`** configured at Stage 1 (`voice_selection.json`). Every segment in `script.json` inherits that single confirmed narrator preset while distinguishing character voices through sentence rhythm, word choice, and explicit speaker phrasing in Vietnamese text. Track `"role"` (`protagonist`, `antagonist`, `supporting`) cleanly inside `characters.json` for editorial context without attaching conflicting per-character voice IDs.

---

## 4. Chapter-to-Chapter Memory Loop

1. **Before authoring Chapter N:** Read `data/projects/<slug>/characters.json` and `data/projects/<slug>/story_summary.md`.
2. **After validating Chapter N:**
   - Add newly introduced recurring characters to `characters.json`.
   - Append a concise Chapter N recap entry to `story_summary.md` recording major plot revelations, realm breakthroughs, deaths, or faction shifts.
