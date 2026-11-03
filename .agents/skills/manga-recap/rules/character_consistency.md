# Character Consistency & Memory Rules

## 1. Canonical Naming Protocol

1. **Establish Canonical Names in `characters.json`**:
   - Choose a single canonical Vietnamese or standardized Sino-Vietnamese name upon first appearance (e.g., `Tiêu Phàm`, `Lý Bách`, `Lão Gia Chủ`).
   - List all alternative translations or informal nicknames in the `aliases` array (e.g., `["Tiểu Bách", "Đệ Thất Thiếu Gia", "Bách ca"]`).
   - NEVER alternate between English romanization (`Li Bai`) and Sino-Vietnamese (`Lý Bạch`) within the same chapter.
2. **Pronoun & Honorific Hierarchy**:
   - Master / Disciple: Sư phụ / Đồ nhi.
   - Clan Patriarch / Junior: Lão gia chủ / Vãn bối (hoặc tiểu tử).
   - Martial Brothers: Sư huynh / Sư đệ.
   - Maintain rigorous Vietnamese hierarchical pronouns throughout all segments.

## 2. Visual Feature Tracking

In `characters.json`, record stable visual traits under `core_features`:
- **Facial Traits**: Eye scars, eye color, facial hair, distinct marks.
- **Hair**: Color, hairstyle, length (e.g., "Tóc bạch kim dài buộc đuôi ngựa").
- **Costume / Armor**: Current attire and distinctive accessories (e.g., "Hắc y trường bào viền vàng, bội kiếm Thanh Vân").
- When a new panel appears, cross-reference visual features against `characters.json` to confirm identity before attributing dialogue or actions.

## 3. Voice Profile Mapping

Assign appropriate VieNeu preset voices to each character based on gender, age, and personality:

| Character Archetype | Gender | Suggested VieNeu Preset | Characteristics |
|---------------------|--------|-------------------------|-----------------|
| Primary Narrator | Female | `Trúc Ly` (Default) | Warm, expressive, versatile pacing |
| Protagonist (Young Male) | Male | `Hải Đăng` | Energetic, resolute, youthful heroic tone |
| Veteran / Elder / Master | Male | `Thiền Tâm Đức` | Grave, authoritative, archaic dignity |
| Mentor / Strategist | Male | `Thiện Minh` | Deep, calm, measured |
| Villain / Antagonist | Male | `Quang Sơn` | Sharp, cold, menacing |
| Heroine / Female Lead | Female | `Mai Anh` | Gentle, clear, bright |
| Noble / Queen / Senior Female | Female | `Ngọc Trân` | Proud, regal, poised |
| Comic Relief / Cheeky | Male | `Adam bựa` | Humorous, colloquial, punchy |

## 4. Continuity Across Chapters

1. **Read Before Scripting**:
   - Always load `characters.json` and `story_summary.md` before processing Chapter N.
2. **Update After Scripting**:
   - When Chapter N is fully scripted, update `story_summary.md` with a concise 3-paragraph summary of major plot progressions, character deaths, power-ups, or allegiance shifts.
   - Record newly introduced characters into `characters.json`.
