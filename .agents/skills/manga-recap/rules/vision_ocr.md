# Vision, Contact Sheets & OCR Rules

## 1. Visual Inspection Protocol

1. **Evidence-Based Scripting Only**:
   - The coding agent MUST visually inspect panel images (`panels/panel_*.jpg`) and multi-panel contact sheets (`contact_sheets/sheet_*.jpg`).
   - SCRIPTING FROM METADATA OR FILENAMES ALONE IS STRICTLY PROHIBITED.
2. **Contact Sheet Utilization**:
   - Contact sheets bundle up to 16 panels into a 4x4 grid with stamped sequence badges (`#001`, `#002`).
   - Use contact sheets to grasp macro scene flow, character positioning, and combat choreography before diving into individual high-res crops for small speech bubbles.

## 2. Multi-Language OCR Handling

1. **Source Bubble Transcription**:
   - Read speech bubbles in English, Korean (Hangul), Japanese, or Chinese.
   - Transcribe dialogue mentally or note it in scratchpad, verifying character attribution based on tail-pointer direction.
2. **Small Font & Low-Contrast Bubbles**:
   - If a speech bubble is tiny, inspect the full-resolution crop in `panels/panel_*.jpg`.
   - If partially obscured or illegible, do NOT guess. Mark `[CAN_XAC_MINH: chữ trong bong bóng thoại mờ]` and flag `status: "needs_review"`.
3. **Sound Effects (SFX) & Text Overlays**:
   - Korean/Japanese sound effects often overlay action art. Translate the emotional weight into descriptive narration rather than verbalizing the phonetic sound.

## 3. Gutter Anomaly & Crop Defect Detection

During inspection, check for segmentation artifacts:
1. **Split Panels**: An action panel severed across two crops due to internal artwork whitespace.
   - Remedy: Map both panel IDs (`["panel_004.jpg", "panel_005.jpg"]`) to a single narration segment with camera motion `pan_down` or `vertical_scroll`.
2. **Merged Panels**: Two distinct beats combined into one tall crop.
   - Remedy: Use `focus_box` to zoom into the upper beat for segment N, and another `focus_box` for the lower beat in segment N+1.
3. **Advertisements & Watermarks**: Top/bottom banner ads, credit pages, or scanlation team recruitment pages.
   - Remedy: Mark panel status as `skip` in `manifest.json`. Never narrate advertisements.

## 4. Panel Classification

In `manifest.json` and `script.json`, classify each panel:
- `active`: Standard story panel to be narrated and rendered into video.
- `context_only`: Panel provides visual context for the agent's recap understanding, but is skipped in final video render (e.g. recap flashes of previous episodes).
- `skip`: Promotional banner, credit card, or blank page. Completely excluded.
- `needs_review`: Gutter error, corrupted image, or indecipherable scene requiring human intervention.

## 5. Camera Motion & Focus Box Calculation

Video Engine V2 supports 9 cinematic camera motion modes:
1. `static`: No movement, standard 16:9 fit.
2. `pan_left`: Smooth camera pan from right to left across a wide panel.
3. `pan_right`: Smooth camera pan from left to right.
4. `pan_up`: Vertical pan from bottom to top (revealing tall characters or colossal monsters).
5. `pan_down`: Vertical pan from top to bottom (descent, falling, or reading order).
6. `zoom_in`: Progressive zoom into character facial expression or crucial object.
7. `zoom_out`: Pull back from detail to reveal wide scale of devastation.
8. `focus_crop`: Crop to a specific sub-region defined by `focus_box`.
9. `vertical_scroll`: Continuous vertical scroll suited for long-strip webtoon panels.

### FocusBox Coordinate System
`focus_box` is specified in normalized float coordinates `[x_min, y_min, x_max, y_max]` from `0.0` to `1.0`:
- Top-Left: `x_min=0.0, y_min=0.0`
- Bottom-Right: `x_max=1.0, y_max=1.0`
- Example: Focus on character face in top-right quadrant:
  ```json
  "focus_box": { "x_min": 0.5, "y_min": 0.0, "x_max": 1.0, "y_max": 0.5 }
  ```

## 6. Subject-Centric Framing & Chat Box Avoidance Protocol

### Quy tắc 1: Tuyệt đối không chọn tâm frame vào Box Chat
- Trong webtoon, các ô bong bóng thoại thường nằm ở rìa trên (y: 0.05 – 0.35) hoặc góc dưới.
- Lời thoại đã được VieNeu-TTS đọc thành tiếng và phụ đề SRT hiển thị rõ nét ở chân trang.
- Do đó, việc zoom vào box thoại là **lỗi nghiêm trọng**, làm mất đi hình ảnh nhân vật và bối cảnh hành động.
- **Biện pháp**: Khi viết `script.json`, Agent phải xác định tâm nhân vật (`fy` thường nằm trong khoảng `0.45 – 0.70`). Nếu thoại ở trên, đẩy tâm khung nhìn xuống dưới để tập trung vào biểu cảm khuôn mặt.

### Quy tắc 2: Chống phóng to quá mức (Anti-Overzoom)
- Không bao giờ đặt `crop_scale` nhỏ hơn `45%` chiều rộng hoặc chiều cao panel (`scale_ratio >= 0.45`).
- Giới hạn hệ số phóng to Ken Burns ở mức $1.15\times$ tối đa.
- Luôn giữ khoảng đệm (padding) ít nhất `15% – 20%` xung quanh đầu và cơ thể nhân vật để không cắt phạm đỉnh tóc, nón lá, hoặc tay chân.

### Quy tắc 3: Xử lý thông minh Panel dọc dài (Webtoon Tall Strips)
- Với các panel webtoon dạng dải dọc dài ($H > 2W$):
  - **Khuyến nghị 1 (An toàn nhất)**: Dùng chế độ `static` (dual-layer blurred background). Toàn bộ chiều dài tranh được giữ nguyên sắc nét, hai bên là cánh nền mờ nghệ thuật.
  - **Khuyến nghị 2 (Chuyển động)**: Dùng `vertical_scroll` hoặc `pan_down` với thời lượng đủ dài, bắt đầu từ đỉnh đầu nhân vật và kết thúc ở chân/đòn đánh, không cuộn qua khoảng trắng vô nghĩa.
  - Nếu User chọn `--no-animation`, toàn bộ panel được áp dụng chế độ `static` an toàn 100%.
