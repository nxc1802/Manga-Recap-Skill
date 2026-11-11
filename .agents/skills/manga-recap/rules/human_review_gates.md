> **HARD CONSTRAINT / STRICT FORM MODE (V4):** Agent TUYỆT ĐỐI KHÔNG được tự ý áp dụng giá trị mặc định ngầm. Bắt buộc phải đưa đầy đủ toàn bộ 10 hạng mục vào danh sách câu hỏi của công cụ `ask_question` để người dùng trực tiếp chọn. Collect and validate explicit user answers for Q01–Q10 from the canonical `SKILL.md` Stage 0 BEFORE crawl, segmentation, scripting, GPU dispatch, TTS or render. Do NOT bypass via `--approved`, `--skip-preview`, system defaults or restored configurations without user-approval provenance. If the tool cannot ask ten items at once, ask them in successive tool invocations; if unavailable, stop. The separate thumbnail and YouTube channel-selection gates remain mandatory at their respective stages.

## 0. Review Gate 0: Video Configuration & Series Setup Gate (Cấu hình 1 LẦN cho cả Series)

Diễn ra **ngay khi khởi tạo project/series** trước khi thực thi bất kỳ bước nào (Crawl, Script, TTS, Render).

### Mục tiêu:
Stage 0 **phải** gọi công cụ `ask_question` thu thập đủ 10 hạng mục Q01–Q10 theo danh sách chuẩn trong `.agents/skills/manga-recap/SKILL.md`. Đây là câu trả lời **người dùng tự chọn**, không phải các giá trị đề xuất được Agent tự điền.

1. **Q01 — Chapter & URL / Import:** Chọn chapter hoặc chapter range và URL, hoặc thư mục ảnh nguồn.
2. **Q02 — Compute Server:** `Kaggle Cloud GPU` hoặc `Local Environment`. Không tự chọn Kaggle.
3. **Q03 — Voice ID:** Chọn một giọng VieNeu-TTS khả dụng, không tự chọn `Ngọc Huyền`.
4. **Q04 — Speed:** Chọn tốc độ đọc từ các giá trị được hỗ trợ.
5. **Q05 — Burn Subtitles:** Chọn `Có` hoặc `Không`.
6. **Q06 — Animation:** Chọn `Có` hoặc `Không`; motion policy top-down mới chỉ nằm trong kế hoạch V4 cho đến khi Render Engine được cập nhật.
7. **Q07 — Thumbnail:** Chọn `Tạo` hoặc `Không tạo`. Nếu chọn `Tạo`, Gate thumbnail tiếp tục yêu cầu chọn panel và chữ sau khi có hình nguồn.
8. **Q08 — YouTube Upload:** Chọn `Không upload` hoặc một kênh cụ thể cùng privacy `private`/`unlisted`/`public`; nếu có upload, Gate 3 tiếp tục xác nhận kênh.
9. **Q09 — Video Quality:** Chọn `preview 720p` hoặc `final 1080p`. Chapter 1 của quy trình V4 phải qua preview và human approval trước khi final, dù Q09 chọn `final`.
10. **Q10 — BGM:** Chọn `Không BGM` hoặc `Có BGM` kèm đường dẫn nguồn và gain đã xác nhận.

Đối với mỗi Q, thiếu hoặc không hợp lệ => phải hỏi lại, không tự thay bằng default. Lưu `project.json` và `series_setup_answers.json` (kế hoạch: Python runtime validator cần được triển khai ở PR-01 kế tiếp, chưa có trong runtime hiện tại).

**Quy tắc kế thừa (Inheritance Rule)**: Mọi chapter tiếp theo trong cùng series sẽ **tự động kế thừa** các thông số trên từ `project.json`. Agent không hỏi lại khi đã có bản ghi xác nhận 10/10 hợp lệ và không thay đổi; nếu thiếu provenance hoặc người dùng đổi cấu hình, phải hỏi và xác nhận lại các trường bị ảnh hưởng. CLI validator cho quy tắc này chưa được triển khai.

---

## 1. Review Gate 1: Voice & Emotion Strategy Approval (Duyệt 1 LẦN cho cả Series)

Diễn ra **sau** khi thử giọng / audition và **trước** khi render video. Giọng trong bản ghi mẫu dưới đây chỉ minh họa; không được dùng để suy ra lựa chọn của người dùng.

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
2. **Chapter 1 Mandatory Preview in the V4 Series Workflow**:
   - Chapter 1 MUST be rendered in preview quality and explicitly approved by a human before a full-series final job is requested. A user choosing `final` at Q09 does not waive the Chapter 1 preview.
   - After Chapter 1 approval is recorded with its script/config fingerprint, chapters 2+ MAY use `--skip-preview` to render finals without repeated manual approval.
   - **Current limitation:** legacy CLI still accepts `--skip-preview` for Chapter 1; this policy is enforced by Agent guidance only until the proposed Python runtime checks ship.
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

## 3. Review Gate 3: YouTube Channel Selection Gate (Cổng Chọn Kênh YouTube Bắt Buộc)

Diễn ra **sau** khi render video hoàn tất và **trước** khi upload lên YouTube.

### Quy Tắc Cốt Lõi:
1. **Tuyệt đối không tự ý upload**: Agent **KHÔNG BAO GIỜ** được tự ý chọn kênh mặc định hoặc bất kỳ kênh nào để tải lên khi chưa hỏi người dùng.
2. **Quy trình truy vấn & trình bày**:
   - Agent gọi CLI: `manga-recap youtube accounts --json` để lấy danh sách kênh hiện tại (Title, Channel ID, Alias, kênh mặc định).
   - Agent sử dụng công cụ `ask_question` hiển thị câu hỏi trắc nghiệm rõ ràng:
     - **Câu hỏi**: *"Bạn muốn upload video này lên kênh YouTube nào?"*
     - **Các lựa chọn**:
       - Tên từng kênh khả dụng (ví dụ: `Chờ Review (alias: cho_review)`, `Muốn Review (alias: muon_review)`, v.v.).
       - Kèm lựa chọn: `Không upload (Chỉ lưu file video và thumbnail nội bộ)`.
3. **Thực thi chính xác**:
   - Sau khi người dùng chọn, Agent truyền chính xác `--account <kênh_được_chọn>` vào lệnh `manga-recap youtube upload`.
   - Nếu người dùng chọn "Không upload", Agent kết thúc pipeline và báo cáo đường dẫn file video thành phẩm.

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

