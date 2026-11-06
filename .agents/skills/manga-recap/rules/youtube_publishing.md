# YouTube Packaging, SEO & Publishing Rules for Manga Recap

Quy tắc này tổng hợp và chắt lọc các nguyên lý đóng gói (Packaging), tối ưu tỷ lệ nhấp (CTR), SEO và trích xuất Shorts chuyên biệt cho kênh Manga Recap, kết hợp tinh hoa từ tiêu chuẩn của YouTube và bộ kỹ năng `youtube-agent-skill`.

---

## 1. Triết Lý Đóng Gói (Packaging Protocol - từ `yt-package`)

> *"Khán giả không bấm vào xem một video hay, họ bấm vào một cặp Tiêu Đề + Thumbnail gây tò mò tột độ."*

1. **Quy tắc độ dài Tiêu đề (Title Length Rule)**:
   - **Tối đa 100 ký tự** theo giới hạn kỹ thuật của YouTube.
   - **Khuyến nghị vàng:** $\mathbf{\le 65}$ **ký tự**. Quá 65 ký tự, tiêu đề sẽ bị cắt cụt bằng dấu ba chấm (`...`) trên giao diện di động (nơi chiếm 75% lượng người xem).
   - Nội dung quan trọng nhất và từ khóa gây sốc phải nằm ở **35 ký tự đầu tiên**.

2. **4 Công thức Tiêu đề Triệu View cho Manga Recap**:
   - **Công thức 1 — Tương phản thân phận (The Status Flip):**
     *Ví dụ:* `"Tập 1: Tưởng Là Phế Vật, Ai Ngờ Lại Là Lão Tổ Chuyển Sinh?!"`
   - **Công thức 2 — Tình thế ngặt nghèo (The Urgent Crisis):**
     *Ví dụ:* `"Vừa Xuyên Không Đã Bị Ép Uống Kỳ Độc, Lão Gia Chủ Liền Làm Điều Này..."`
   - **Công thức 3 — Tò mò tột độ (The Mystery Hook):**
     *Ví dụ:* `"Bí Mật Rùng Rợn Đằng Sau Khối U Dị Thường Của Đệ Nhất Kiếm Thánh!"`
   - **Công thức 4 — Sức mạnh áp đảo (Overwhelming Force):**
     *Ví dụ:* `"Bị Cả Tông Môn Khinh Thường, Ông Lão 90 Tuổi Một Kiếm Quét Sạch Đại Sảnh!"`

3. **Phối hợp Tiêu đề & Thumbnail (Title + Thumbnail Pairing)**:
   - **Tuyệt đối KHÔNG lặp lại nguyên văn:** Chữ trên Thumbnail không được nhắc lại y hệt Tiêu đề video.
     - *Sai:* Tiêu đề là "Lão Tổ Trọng Sinh" - Thumbnail cũng ghi chữ to "Lão Tổ Trọng Sinh".
     - *Đúng:* Tiêu đề là "Tập 1: Bị Ép Uống Độc Dược, Lão Tổ Liền Làm Điều Này..." - Thumbnail để ảnh lão nhân mắt lóe sáng kèm chữ: **"AI CHO PHÉP?!"**
   - **Giới hạn chữ trên Thumbnail:** Tối đa **3 đến 5 từ**, phông chữ to, viền dày (stroke/drop shadow) nổi bật trên nền tranh truyện.
   - **Vùng an toàn (Safe Zone):** Tuyệt đối không đặt chữ hoặc khuôn mặt ở **góc dưới bên phải** (nơi YouTube hiển thị nhãn thời lượng video như `12:45`).

---

## 2. Mô Tả Video Chuẩn SEO & Timestamps (từ `yt-seo` & `yt-chapters`)

Cấu trúc mô tả video chuẩn YouTube gồm 5 khối nội dung:

```text
[Khối 1: Hook 2 dòng đầu] ──► [Khối 2: Tóm tắt nội dung] ──► [Khối 3: Timestamps Chương]
                                                                        │
                                                                        ▼
[Khối 5: Thẻ Hashtag & Tags] ◄── [Khối 4: Bản quyền & Credit Truyện] ◄┘
```

1. **Khối 1: 2 Dòng Đầu Tiên (Above the Fold):**
   - Đây là phần duy nhất người xem thấy trước khi bấm nút "Xem thêm...". Phải chứa tóm tắt gay cấn nhất kèm lời kêu gọi nhấn Đăng ký kênh.
2. **Khối 2: Timestamps (Mục lục thời gian tự động):**
   - YouTube yêu cầu mốc đầu tiên bắt buộc là `00:00` và tối thiểu có 3 mốc thời gian.
   - CLI `manga-recap youtube metadata` sẽ tự động trích xuất các mốc này từ thời lượng các segment trong `script.json`.
   - *Định dạng chuẩn:*
     ```text
     00:00 - Bối cảnh phủ Tấn Dương
     00:30 - Thạch đại phu bắt mạch Lưu lão gia
     01:15 - Khám phá cỗ hàn độc quỷ dị
     ```
3. **Khối 3: Credit tác giả & Tên truyện gốc:**
   - Tên truyện, tác giả, họa sĩ minh họa, link ủng hộ bản gốc (nếu có).
4. **Khối 4: Tuyên bố miễn trừ bản quyền hợp pháp (Fair Use Disclaimer):**
   - Bắt buộc đính kèm điều khoản Fair Use theo luật bản quyền:
     ```text
     * Copyright Disclaimer Under Section 107 of the Copyright Act 1976:
     Allowance is made for "fair use" for purposes such as criticism, comment, news reporting, teaching, scholarship, and research.
     Video này được thực hiện vì mục đích bình luận, tóm tắt và đánh giá nghệ thuật truyện tranh.
     ```
5. **Khối 5: Thẻ Hashtag:**
   - 3 hashtag chính đặt ở cuối mô tả: `#MangaRecap #ReviewTruyen #Webtoon`.

---

## 3. Trích Xuất Phân Đoạn Làm YouTube Shorts (từ `yt-shorts`)

Mỗi chapter video dài đều có thể trích xuất ra một đoạn **Shorts (30–60 giây)** để kéo traffic về tập chính:
- **Tiêu chí chọn cảnh Shorts:**
  1. Phân cảnh có một câu thoại tuyên bố cực ngầu hoặc hành động lật ngược thế cờ.
  2. Bắt đầu ngay lập tức từ giây thứ 0 bằng đòn đánh hoặc ánh mắt đằng đằng sát khí.
  3. Kết thúc ở điểm lửng (Cliffhanger) kèm ghim bình luận: *"Xem trọn bộ tập 1 tại video đính kèm!"*.

---

## 4. Quy Trình Xuất & Đăng Tải Tự Động Với CLI

Quy trình chuẩn gồm 2 bước:

### Bước 1: Tạo bộ đóng gói Metadata tự động
```bash
manga-recap youtube metadata --project <slug> --chapter <num> --json
```
- Tự động đọc `script.json` và `story_summary.md`.
- Tính toán chính xác các mốc thời gian (Timestamps).
- Tạo file `youtube_metadata.json` chứa Tiêu đề, Mô tả SEO, Tags và gợi ý Thumbnail.

### Bước 2: Tải video lên kênh YouTube
```bash
manga-recap youtube upload --project <slug> --chapter <num> --privacy unlisted --json
```
- Tự động lấy file `output/<slug>/chapter_001.mp4`.
- Tự động tải phụ đề `output/<slug>/chapter_001.srt` lên làm Closed Captions tiếng Việt.
- Tự động điền Title, Description, Tags, Category từ `youtube_metadata.json`.
- Hỗ trợ cờ `--thumbnail <path>` để đặt ảnh bìa tùy chỉnh.
