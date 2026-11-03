# Human Review Gates & Quality Assurance Protocol

## 0. Review Gate 0: Video Configuration & User Onboarding Gate (BẮT BUỘC)

Diễn ra **ngay khi bắt đầu** trước khi thực thi bất kỳ bước nào (Crawl, Script, TTS, Render).

### Mục tiêu:
Agent **bắt buộc** phải hỏi User (hoặc xác nhận rõ ràng) bộ tham số cấu hình:
1. **Chapter & URL**: Số chapter và link truyện cần recap.
2. **Giọng đọc (Voice ID)**: Chọn giọng đọc VieNeu-TTS (mặc định: `Trúc Ly` - Nữ miền Bắc truyền cảm, `Hải Đăng` - Nam miền Nam, `Thiền Tâm Đức` - Nam miền Bắc trầm cổ phong, `Thiện Minh` - Nam miền Bắc hào sảng, `Mai Anh` - Nữ miền Bắc tươi trẻ, v.v.).
3. **Tốc độ đọc (Speed)**: `0.9x`, `1.0x` (chuẩn), `1.1x`, `1.2x`.
4. **Phụ đề (Burn Subtitles)**: `Có` (`true`) hoặc `Không` (`false`).
5. **Hiệu ứng Animation (Camera Motion)**:
   - `Có` (`true`): Hiệu ứng zoom/pan nhẹ bám theo nhân vật.
   - `Không` (`false`): Chế độ tĩnh (`STATIC`) toàn bộ, hiển thị trọn vẹn panel với nền mờ nghệ thuật, tránh rung lắc và phóng to quá mức.
6. **Chất lượng Video**: `preview` (720p) hoặc `final` (1080p).
7. **BGM (Nhạc nền)**: File nhạc nền và âm lượng nếu có.

Ghi nhận cấu hình vào `data/projects/<project_slug>/project.json`.

---

## 1. Review Gate 1: Voice & Emotion Strategy Approval

Occurs **after** Stage 6 (Local TTS Synthesis of screening samples) and **before** Stage 8 (Full Video Rendering).

### Objectives
1. Verify synthesized audio quality, pronunciation of character names, and cadence.
2. Obtain explicit human approval for the Narration Strategy:
   - **Strategy A (Single Narrator)**: Unified narration across all segments.
   - **Strategy B (Narrator + Characters)**: Main narrator + character-specific voices.
   - **Strategy C (Emotion-Aware Narrator)**: Narrator with fine-tuned punctuation and cue tags.
3. User approval is recorded in `data/projects/<project_slug>/voice_selection.json`:
   ```json
   {
     "approved_strategy": "single_narrator",
     "primary_voice": "Trúc Ly",
     "speed": 1.0,
     "reviewer": "user",
     "approved_at": "2026-11-03T14:30:00Z",
     "notes": "Voice approved for full chapter render."
   }
   ```
4. **Agent Action**: The agent MUST NOT proceed to high-resolution video rendering if `voice_selection.json` is missing or unapproved.

---

## 2. Review Gate 2: Video Preview Approval

Occurs **after** Stage 8 (Preview Render at 720p) and **before** Stage 10 (Final High-Res Compile).

### Objectives
1. Inspect audio-visual synchronization: verify that panel cuts align precisely with utterance ends.
2. Inspect camera motions: ensure pan and zoom speeds feel cinematic and do not cause motion sickness.
3. Check subtitle legibility: ensure Vietnamese font renders correctly with full diacritics and no text clipping.
4. Verify BGM balance: ensure background music ducks cleanly under speech and crossfades smoothly.
5. If changes are requested:
   - Adjust `focus_box` or `camera_motion` in `script.json`.
   - Re-run `manga-recap render --project <slug> --chapter <num> --quality preview`.
6. Once approved, proceed to final 1080p compile.

---

## 3. Uncertainty Protocol: Never Hallucinate

When encountering ambiguous, damaged, or unreadable input:
1. **Trigger Conditions**:
   - Speech bubble text is cut off at the panel edge, obscured by JPEG compression, or illegible.
   - Identity of a masked or silhouette character cannot be corroborated from `characters.json`.
   - Complex martial arts or magic terminology is unclear in source translation.
2. **Mandatory Action**:
   - Set segment `status: "needs_review"`.
   - Prepend `[CAN_XAC_MINH: <lý_do_chi_tiết>]` to `narrator_text` or `notes`.
   - Describe observable action objectively without fabricating lore:
     - *Bad (Hallucination)*: "Hắn ta chính là Ma Đế tái sinh, người từng tiêu diệt Thiên Môn 500 năm trước!" (Fabricated backstory).
     - *Good (Objective + Flag)*: "[CAN_XAC_MINH: Danh tính nhân vật bí ẩn] Bóng người áo đen đứng trên đỉnh tháp quan sát trận chiến trong im lặng."
