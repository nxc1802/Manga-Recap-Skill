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

## 2. Review Gate 2: Video Preview Approval & Decoupled Status Semantics

Occurs when human verification of video quality, audio-visual synchronization, and storytelling faithfulness is conducted.

### Independent Review Status Architecture
The pipeline strictly decouples technical execution from human editorial judgment:
- **`technical_status`**: Tracks machine execution (`pending`, `running`, `completed`, `failed`).
- **`human_review_status`**: Tracks human editorial evaluation (`unreviewed`, `approved`, `changes_requested`).

### Objectives & Bypass Rules:
1. **Interactive Inspection**: Inspect audio-visual synchronization, Ken Burns motion, subtitle readability, and BGM ducking on `video/preview.mp4` or `video/final.mp4`.
2. **Optional in Production / Batch Runs (`--skip-preview`)**:
   - In single-chapter direct rendering: pass `--skip-preview` to bypass Gate 2 and render production 1080p directly.
   - In batch runs (`manga-recap batch`): Gate 2 is **bypassed by default** (`--skip-preview`).
   - **MANDATORY STATUS INVARIANT**: When preview is bypassed via `--skip-preview`, the technical stage completes (`technical_status = "completed"`), but `human_review_status` MUST BE recorded as **`unreviewed`**.
   - **STRICT PROHIBITION**: The system and agent MUST NEVER automatically mark unreviewed chapters as `"approved"`.
3. **Approval Recording**:
   - When human reviewer approves: set `human_review_status = "approved"` and generate/update `preview_approval.json` with reviewer name, timestamp, and notes.

---

## 3. Human Review Pack & Quality Verification Protocol

To enable fast, transparent human inspection without requiring complex UI dashboards, generate a static **Human Review Pack**:
```bash
manga-recap review-pack --project <project_slug> --chapter <chapter_num> [--format md|html|json] --json
```

### Review Pack Evidence Layers:
1. **Visual Overview**: 4x4 contact sheets (`contact_sheets/sheet_*.jpg`) for rapid scanning of all chapter panels.
2. **Script Alignment Table**: Direct mapping of:
   `seq_id` $\rightarrow$ `panel_ids` $\rightarrow$ `character` $\rightarrow$ `narrator_text` $\rightarrow$ `camera_motion` $\rightarrow$ audio clip duration.
3. **Omitted Panels Log**: Detailed table of panels present in `manifest.json` but omitted from `script.json`:
   - Verifies that only non-essential panels (filler scenery, transitions, ads) were omitted.
   - **Reviewer Check**: Confirm that **all essential story panels are retained**.
4. **Needs Review Flags**: Highlights segments with `[CAN_XAC_MINH: ...]` for quick editorial decision.
5. **Media Preview Links**: Direct paths to `.wav` clips, `.srt` subtitles, and `.mp4` video.

### Reviewer Feedback Protocol:
1. **Acceptance**:
   - Reviewer sets `human_review_status: "approved"` in `chapter.json` or creates `preview_approval.json`.
2. **Changes Requested**:
   - Reviewer specifies feedback targeting exact IDs:
     - `seq_id`: Identify specific segment(s) needing text, emotion, or voice edits.
     - `panel_ids`: Identify missing essential panels to add back, or unwanted panels to remove.
     - Specific instruction: e.g., *"Segment 002_1: Lời thoại của Nam An cần gằn giọng hơn, bổ sung [hắng giọng]"* hoặc *"Panel panel_005.jpg là chiêu thức quyết định, phải đưa vào kịch bản"*.
3. **Agent Remediation & Incremental Re-synthesis**:
   - Agent edits only the specified segments in `script.json`.
   - Re-running `manga-recap tts` and `manga-recap render` automatically preserves valid cached audio clips for untouched segments. Only modified segments are re-synthesized.

---

## 4. Uncertainty Protocol: Never Hallucinate

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

