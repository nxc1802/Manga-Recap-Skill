# Narrative Recap & Prosody Rules

## 1. Narrative Pacing & The 5-Second Hook

1. **The 5-Second Golden Rule**:
   - The opening segment (Segment 001) MUST capture listener attention within the first 5 seconds.
   - **Banned Openings**: "Chào các bạn đã quay trở lại với kênh...", "Hôm nay chúng ta sẽ cùng theo dõi...", "Ở tập trước chúng ta đã thấy...".
   - **Required Openings**: Jump straight into dramatic tension, a philosophical clash, or an intense turning point:
     - *Example*: "Giữa đêm đen mù mịt của núi Thiên Nhai, một luồng sát khí lạnh thấu xương đột ngột giáng xuống, báo hiệu ngày tàn của cả một môn phái!"
2. **Segment Text Length Limits**:
   - Minimum: **50 characters** per segment.
   - Maximum: **250 characters** per segment.
   - Rationale: Utterances under 50 characters feel choppy; utterances over 250 characters degrade TTS prosody stability and cause subtitle overflow on screen.
3. **Pacing Synchrony**:
   - High-action fight scenes: Short, punchy, rhythmic sentences with dynamic verbs.
   - Mystery / Suspense scenes: Longer clauses, deliberate pauses, atmospheric phrasing.

## 2. Vietnamese Recapping Prose vs. Mechanical Translation

1. **Adaptation, Not Literal Translation**:
   - Webtoon English text is often terse or stiff. The recap script MUST translate the narrative intent into idiomatic, evocative Vietnamese storytelling.
   - Do not translate sound effects literally (e.g. do not say "Bùm", "Keng" as spoken words unless stylistically appropriate). Instead, narrate the impact: "Cú va chạm kinh hoàng làm mặt đất nứt toác!"
2. **Third-Person Narrator vs. Direct Character Dialogue**:
   - **Narration**: When setting context, describe what is happening objectively from a narrator persona:
     - *Text*: "Nhận thấy đối phương sơ hở, Tiêu Phàm lập tức vung kiếm lao tới."
   - **Character Attribution**: When conveying speech bubble text, either integrate it as reported speech or direct speech with clear emotional inflection:
     - *Direct Speech*: "Hắn gầm lên trong phẫn nộ: 'Các ngươi hôm nay đừng hòng bước ra khỏi đây nửa bước!'"
     - If character dialogue is spoken directly, set the `character` attribute in `ScriptSegment` to the character's canonical name.

## 3. Emotion Intent & Prosody Cues for VieNeu-TTS v3 Turbo

VieNeu-TTS v3 Turbo generates natural cadence based on punctuation and specific audible cues.

1. **Supported Audible Cues**:
   - `[cười]`: Inserts an audible chuckle or light laughter before or within speech.
   - `[thở dài]`: Inserts an audible sigh (ideal for weary elders, disappointed heroes).
   - `[hắng giọng]`: Inserts a throat-clearing pause (ideal for formal declarations or tense interruptions).
   - *Rule*: Never invent unsupported cue tags like `[khóc]`, `[hét]`, `[tức giận]`. VieNeu v3 Turbo does not support them and will pronounce them literally.
2. **Punctuation-Based Prosody Shaping**:
   - Ellipses (`...`): Adds suspenseful trailing pauses (0.4–0.6s).
   - Em-dash (`—`): Creates abrupt dramatic breaks or scene interruptions.
   - Exclamation mark (`!`): Boosts vocal pitch, urgency, and energy.
   - Question mark (`?`): Raises terminal pitch inflection naturally.
3. **Emotion Category Field**:
   - Set the `emotion` field in `ScriptSegment` to one of the benchmarked intent categories: `neutral`, `dramatic`, `suspense`, `sad`, `excited`, `dialogue`, `long_form`, `names`.

## 4. Bad vs. Good Scripting Examples

| Bad (Mechanical / Flat) | Good (Dynamic / Dramatic) | Why |
|-------------------------|---------------------------|-----|
| "Khung 1: Người đàn ông đang đi vào phòng và nói xin chào." | "Bước chân nặng trĩu của lão gia chủ phá tan bầu không khí tĩnh mịch, mang theo điềm báo chẳng lành." | Hook, evocative vocabulary, establishes tone. |
| "Tiêu Phàm nói: 'Tôi sẽ không bỏ cuộc đâu'." | "Ánh mắt Tiêu Phàm rực lửa quyết tâm, hắn gằn từng tiếng: 'Cho dù phải trả giá bằng tính mạng, ta tuyệt đối không lùi bước!'" | Vivid action description, compelling dialogue. |
| "Quái vật tấn công. Mọi người hoảng sợ chạy đi." | "Trong chớp mắt, bóng ma hung tợn xé toạc màn sương [thở dài] khiến đám đông kinh hoàng tháo chạy trong tuyệt vọng!" | Uses approved cue, sensory details, dynamic verbs. |
