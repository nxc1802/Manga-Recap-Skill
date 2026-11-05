## 0. Review Gate 0: Video Configuration & Series Setup Gate (Cấu hình 1 LẦN cho cả Series)

Diễn ra **ngay khi khởi tạo project/series** trước khi thực thi bất kỳ bước nào (Crawl, Script, TTS, Render).

### Mục tiêu:
Cấu hình một lần duy nhất cho toàn bộ Series vào `data/projects/<project_slug>/project.json`:
1. **Chapter & URL**: Số chapter khởi điểm và link bộ truyện.
2. **Giọng đọc (Voice ID)**: Chọn giọng đọc VieNeu-TTS (mặc định: `Ngọc Huyền` - Nữ miền Bắc thanh lịch, tự nhiên, biểu cảm cao; hoặc `Trúc Ly`, `Hải Đăng`, `Thiền Tâm Đức`, v.v.).
3. **Tốc độ đọc (Speed)**: `1.0x` (chuẩn mặc định), `0.9x`, `1.1x`, `1.2x`.
4. **Phụ đề (Burn Subtitles)**: `Có` (`true`, mặc định) hoặc `Không` (`false`).
5. **Hiệu ứng Animation (Camera Motion)**:
   - `Không` (`false`, mặc định: `render_pan_mode: static`): Chế độ tĩnh (`STATIC`) toàn bộ, hiển thị trọn vẹn panel với nền mờ nghệ thuật 16:9, tránh rung lắc, vỡ hình và phóng to quá mức.
   - `Có` (`true`, `render_pan_mode: dynamic`): Hiệu ứng zoom/pan nhẹ bám theo nhân vật.
6. **Chất lượng Video**: `preview` (720p) hoặc `final` (1080p, mặc định).
7. **BGM (Nhạc nền)**: File nhạc nền và âm lượng nếu có.

**Quy tắc kế thừa (Inheritance Rule)**: Mọi chapter tiếp theo trong cùng series sẽ **tự động kế thừa** các thông số trên từ `project.json`. Agent và CLI **tuyệt đối không hỏi lại** người dùng cho các chapter sau.

---

## 1. Review Gate 1: Voice & Emotion Strategy Approval (Duyệt 1 LẦN cho cả Series)

Diễn ra **sau** khi thử giọng / audition và **trước** khi render video.

### Objectives
1. Verify synthesized audio quality, pronunciation of character names, and cadence.
2. Xác nhận Narration Strategy: Duy nhất **`single_narrator`** (Một giọng đọc thống nhất kể chuyện kiêm lồng thoại cho tất cả nhân vật), hỗ trợ tùy chọn `emotion_aware` (chèn thẻ cảm xúc `[cười]`, `[thở dài]`).
3. User approval is recorded in `data/projects/<project_slug>/voice_selection.json` at the **project root level**:
   ```json
   {
     "approved_strategy": "single_narrator",
     "primary_voice": "Ngọc Huyền",
     "speed": 1.0,
     "reviewer": "user",
     "approved_at": "2026-11-06T04:00:00Z",
     "notes": "Voice Ngọc Huyền approved as single narrator for entire series."
   }
   ```
4. **Series Inheritance**: All chapters in the project automatically inherit this single approval.

---

## 2. Review Gate 2: Video Preview Approval (Tùy chọn / Bỏ qua trong Batch Mode)

Occurs when manual verification is desired at 720p preview quality.

### Objectives & Bypass Rules:
1. **Interactive Inspection**: Inspect audio-visual synchronization, Ken Burns motion, subtitle readability, and BGM ducking on `preview.mp4`.
2. **Optional in Production / Batch Runs**:
   - In single-chapter direct rendering: pass `--skip-preview` to bypass Gate 2 and render production 1080p directly.
   - In batch runs (`manga-recap batch`): Gate 2 is **automatically bypassed by default** (`--skip-preview`), enabling fully automated, unattended 1080p video production.
3. Once rendered, completing `StageType.RENDER` directly marks chapter status as `COMPLETED`.

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
