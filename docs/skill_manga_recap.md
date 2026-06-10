---
name: manga-recap
description: Tự động hóa quy trình sản xuất video tóm tắt truyện tranh (Manga Recap) bằng AI, kết hợp crawl, viết kịch bản đa phương thức (vision) và kết xuất video chuyển động Cinematic.
---

# 🤖 Manga Recap System Skill

Hành động này hướng dẫn bạn thực hiện quy trình tự động hóa sản xuất video Manga Recap sử dụng các module và CLI có sẵn trong dự án.

## 📋 Tham Số Đầu Vào (Parameters & Defaults)

Khi người dùng yêu cầu thực hiện Manga Recap, hãy hỏi hoặc xác nhận các tham số sau. Nếu người dùng không nhập hoặc yêu cầu sử dụng mặc định, hãy tự điền:

1. **Chapter Number** (ví dụ: `1`)
2. **Project Title** (Tên truyện, mặc định: `Mỗi lính đánh thuê trở về đều có một kế hoạch`)
3. **Chapter URL** (mặc định: `https://truyenqqko.com/truyen-tranh/moi-linh-danh-thue-tro-ve-deu-co-mot-ke-hoach-16550-chap-[SỐ_CHAPTER]`)
4. **Voice Model** (Các lựa chọn giọng đọc: `xtts-vi` - Giọng nam cao cấp (đề xuất), `xtts-vi-female` - Giọng nữ cao cấp, `vi_VN-vais1000-medium` - Giọng nam Piper, `vi_VN-25hours_single-low` - Giọng nữ Piper, mặc định: `xtts-vi`)
5. **BGM Path** (Đường dẫn nhạc nền, mặc định: không dùng nhạc nền)
6. **Speed** (Tốc độ đọc, mặc định: `1.0`)
7. **Burn Subtitles** (Tùy chọn ghi phụ đề lên video, mặc định: `True`, có thể bỏ bằng `--no-subs`)

---

## 🛠️ Quy Trình Thực Hiện Từng Bước (Step-by-Step Execution)

### Bước 1: Thu thập hình ảnh (Crawl)
Chạy lệnh crawl để tải ảnh manga về máy:
```bash
venv/bin/python src/cli/crawler_cli.py --chapter [CHAPTER_NUMBER] --url "[CHAPTER_URL]" --project "[PROJECT_TITLE]"
```
Lệnh này sẽ lưu ảnh trực tiếp vào thư mục `data/[PROJECT_SLUG]/raw_images/` với tên file `image_[CHAPTER]_[SEQ].[ext]` và đăng ký trong cơ sở dữ liệu.

### Bước 2: Viết kịch bản & Phân tích trực tiếp (Integrated Multimodal Vision + Scripting)
Thay vì sử dụng các API VLM và LLM riêng lẻ qua mạng, **bạn (Agent)** sẽ tận dụng khả năng nhìn (vision) đa phương thức trực tiếp để phân tích và viết kịch bản. Để tối ưu hóa token và hiệu suất:

1. **Xác định đường dẫn & Đọc bộ nhớ dài hạn**:
   - `[PROJECT_SLUG]`: viết thường không dấu của `PROJECT_TITLE`.
   - `data/[PROJECT_SLUG]/recap_memory/character_dictionary.json` (thông tin nhân vật cốt lõi).
   - `data/[PROJECT_SLUG]/recap_memory/storyline.md` (tóm tắt cốt truyện tổng thể).

2. **Cơ chế xử lý phân đoạn tối ưu (Batching N = 10 frames)**:
   - Chia tổng số ảnh trong thư mục `raw_images` thuộc chapter hiện tại thành các đợt nhỏ, tối đa **10 khung hình** mỗi lượt.
   - **TỐI ƯU CONTEXT**: Không cần giữ lại hình ảnh hay mô tả thô của các khung hình trước đó trong bộ nhớ làm việc. Bạn **chỉ cần** lấy kịch bản thuyết minh tiếng Việt đã tạo từ file `data/[PROJECT_SLUG]/scripts/script.csv` làm ngữ cảnh dẫn chuyện để đảm bảo tính liên kết.

3. **Quy tắc chọn Tọa độ Cắt ảnh (Crop Coordinate Guidelines - RẤT QUAN TRỌNG)**:
   - **Không chọn biên panel**: Tránh chọn các giá trị chia đều tuyệt đối như `0`, `25`, `50`, `75`, `100` làm `focus_y` nếu chúng trùng vào các khoảng phân cách trắng hoặc đen giữa các khung tranh.
   - **Tập trung vào Nhân vật & Hành động**: Tìm khuôn mặt, ánh mắt nhân vật chính hoặc vị trí trung tâm hành động để làm tiêu điểm (`focus_x`, `focus_y`), không đặt tâm crop vào góc chứa bong bóng thoại trống rỗng.
   - **Sử dụng `crop_scale` thông minh**: Nếu vật thể/nhân vật quá nhỏ, hãy thu nhỏ khung cắt bằng cách đặt `crop_scale` từ `60` đến `80` (để camera zoom sâu hơn vào chi tiết).

4. **Ghi trực tiếp kết quả**:
   - Ghi kết quả kịch bản vào file `data/[PROJECT_SLUG]/scripts/script.csv`.
   - Các cột của CSV: `chapter`, `sequence_id`, `text`, `focus_x`, `focus_y`, `pan_direction`, `crop_scale`.

5. **Cập nhật bộ nhớ dài hạn**: Sau khi viết xong kịch bản cho toàn bộ chapter, hãy cập nhật các đặc điểm/trang phục nhân vật mới phát hiện vào `character_dictionary.json` (cơ chế RAG sẽ tự động lưu crops dưới dạng `character_[INDEX].jpg` trong `data/[PROJECT_SLUG]/character_images/`).

### Bước 3: Đồng bộ Kịch bản vào Database
Chạy CLI import để đẩy kịch bản `script.csv` vào database:
```bash
venv/bin/python src/cli/import_agent_script.py --chapter [CHAPTER_NUMBER] --project "[PROJECT_TITLE]"
```

### Bước 4: Tạo giọng đọc & Kết xuất video (Render)
Gọi CLI render để tạo âm thanh thuyết minh và lắp ghép thành video hoàn chỉnh:
```bash
venv/bin/python src/cli/run_recap.py --chapter [CHAPTER_NUMBER] --project "[PROJECT_TITLE]" --voice "[VOICE_MODEL]" --speed [SPEED] --suffix "final_agent" [THÊM_THAM_SỐ_BGM_NẾU_CÓ] [--no-subs]
```
- Khi chạy xong, file video thành phẩm sẽ được lưu tại: `data/[PROJECT_SLUG]/video/chapter_[CHAPTER_NUMBER]_[VERSION].mp4`.
- Phiên bản `[VERSION]` sẽ tự động tăng tiến (e.g. `chapter_1_1.mp4`, `chapter_1_2.mp4`) nếu chạy lại để tránh đè đè.

### Bước 5: Ghép nối toàn bộ Chapter (Compilation)
Khi muốn ghép nối toàn bộ video chapter thành 1 tập recap hoàn chỉnh:
```bash
venv/bin/python src/cli/compile_chapters.py --project "[PROJECT_TITLE]"
```
- Kết quả video tổng hợp sẽ được lưu tại: `data/[PROJECT_SLUG]/final_recap.mp4`.
