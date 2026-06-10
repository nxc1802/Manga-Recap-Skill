# Manga Recap Pipeline Orchestrator

Dự án Manga Recap Pipeline Orchestrator là hệ thống tự động hóa toàn bộ quy trình tạo video tóm tắt (recap) truyện tranh từ URL. Hệ thống sử dụng VLM (Visual Language Model) để phân tích hình ảnh, LLM để chuyển thể thành kịch bản tiếng Việt có cốt truyện logic, Piper TTS để sinh giọng thuyết minh tự nhiên và FFmpeg để dựng video, ghép phụ đề (SRT) tự động.

---

## ⚙️ Yêu cầu hệ thống

Trước khi bắt đầu, hãy đảm bảo máy tính của bạn đã cài đặt các công cụ sau:
1. **Python 3.10+**
2. **FFmpeg** (được cài đặt và cấu hình trong PATH hệ thống).
   * *macOS:* `brew install ffmpeg`
   * *Ubuntu/Debian:* `sudo apt install ffmpeg`
   * *Windows:* Tải bản dựng FFmpeg và thêm thư mục `bin` vào PATH.

---

## 🛠️ Hướng dẫn cài đặt và thiết lập local

### Cách 1: Thiết lập tự động sử dụng Setup Script (Khuyên dùng)
Hệ thống cung cấp sẵn các kịch bản thiết lập tự động để kiểm tra môi trường, tạo virtual environment, cài đặt dependencies và cấu hình môi trường:
* **macOS / Linux:**
  ```bash
  ./setup.sh
  ```
* **Windows:** Chạy kịch bản `setup.bat` trong Command Prompt hoặc double-click:
  ```cmd
  setup.bat
  ```

### Cách 2: Thiết lập thủ công

#### Bước 1: Kích hoạt môi trường ảo (Virtual Environment)
Dự án tích hợp sẵn thư mục `venv`. Bạn chỉ cần kích hoạt nó:
* **macOS / Linux:**
  ```bash
  source venv/bin/activate
  ```
* **Windows (Command Prompt):**
  ```cmd
  venv\Scripts\activate.bat
  ```
* **Windows (PowerShell):**
  ```powershell
  .\venv\Scripts\Activate.ps1
  ```

#### Bước 2: Cài đặt các thư viện Python cần thiết
Nếu cần cài đặt hoặc khôi phục các thư viện dependencies từ `requirements.txt`:
```bash
pip install -r requirements.txt
```

### ⚙️ Cấu hình biến môi trường
Hệ thống sử dụng các khóa API để kết nối dịch vụ LLM/VLM và lưu trữ cơ sở dữ liệu.
1. Sao chép tệp `.env.example` thành `.env`:
   ```bash
   cp .env.example .env
   ```
2. Mở tệp `.env` và điền các khóa cấu hình sau:
   - `OPENROUTER_API_KEY`: API key truy cập các mô hình ngôn ngữ (LLM/VLM) qua OpenRouter. Đăng ký tại [OpenRouter](https://openrouter.ai/).
   - `SUPABASE_URL`: Đường dẫn API của dự án Supabase của bạn (ví dụ: `https://xxxx.supabase.co`).
   - `SUPABASE_ANON_KEY`: Khóa ẩn danh của dự án Supabase.
   - `SUPABASE_SERVICE_ROLE_KEY`: Khóa quyền quản trị (service role) của dự án Supabase để thực hiện các thao tác ghi dữ liệu từ pipeline.

### Bước 4: Tải model giọng thuyết minh Piper (Tự động)
Hệ thống sử dụng Piper cho phần tổng hợp tiếng nói tiếng Việt. Khi chạy dự án lần đầu tiên, hệ thống sẽ tự động tải các model giọng đọc ONNX cần thiết về thư mục `models/piper/` từ Hugging Face. Không cần tải thủ công.

---

## 🚀 Hướng dẫn khởi chạy dự án

Quy trình hoạt động gồm 2 phần: Backend API (FastAPI) và Frontend UI (Streamlit). Bạn cần chạy cả hai dịch vụ này.

### 1. Khởi chạy Backend API (FastAPI)
Backend đảm nhận toàn bộ việc crawl truyện, gọi API VLM/LLM, tổng hợp giọng nói và dựng video.
```bash
# Đứng từ thư mục gốc của dự án
kill -9 92711
PYTHONPATH=. venv/bin/python src/backend/app.py
```
Dịch vụ Backend sẽ khởi chạy tại: `http://localhost:8000`. Bạn có thể truy cập tài liệu Swagger tại `http://localhost:8000/docs`.

### 2. Khởi chạy Frontend UI (Streamlit)
Giao diện người dùng Streamlit giúp bạn cấu hình cốt truyện, điều chỉnh giọng thuyết minh và theo dõi tiến trình sinh video thời gian thực.
```bash
# Mở một terminal mới, kích hoạt venv và chạy:
PYTHONPATH=. venv/bin/streamlit run src/frontend/app_streamlit.py
```
Ứng dụng sẽ tự động mở trong trình duyệt của bạn tại địa chỉ: `http://localhost:8501`.

---

## 📈 Các tính năng cấu hình trên giao diện Streamlit

Khi khởi chạy giao diện Streamlit, bạn có thể tinh chỉnh các thông số trong sidebar:
* **Manga URL**: Link của bộ truyện tranh cần chạy recap.
* **Cấu hình lô & Phân tích (VLM)**:
  * *Cạnh ngắn nhất ảnh VLM (px):* Kích thước ảnh nén khi gửi lên VLM (mặc định: `500`px).
  * *Số ảnh gửi VLM mỗi lô:* Số ảnh tối đa gửi đồng thời trong 1 request (mặc định: `4`).
  * *Số ảnh ngữ cảnh từ lô trước:* Số lượng mô tả ảnh ở lô trước cung cấp làm ngữ cảnh văn bản giúp VLM nhất quán nhân vật (mặc định: `2`).
* **Tinh chỉnh mô hình**: Lựa chọn VLM/LLM tuỳ ý từ danh sách OpenRouter (mặc định dùng các model miễn phí).
* **TTS (Giọng đọc)**: Chọn giọng thuyết minh tiếng Việt và điều chỉnh tốc độ đọc mong muốn.
* **Storage Cleanup**: Tự động dọn dẹp dung lượng rác (ảnh raw, ảnh đã nén, audio lẻ) sau khi dựng xong video thành phẩm để tiết kiệm đĩa cứng.

---

## 📂 Cấu trúc thư mục dự án

```text
├── config.yaml          # File cấu hình mặc định của hệ thống
├── api_docs.md          # Tài liệu đặc tả kỹ thuật chi tiết của API
├── data/
│   └── output_videos/   # Thư mục lưu trữ video thành phẩm và file phụ đề (.srt)
├── src/
│   ├── backend/         # FastAPI backend server và SSE streaming
│   ├── frontend/        # Streamlit web client dashboard
│   ├── core/            # Config loader, logger, DB client, prompts templates
│   ├── services/        # Crawler, VLM, LLM, TTS core services
│   └── render/          # Video engine sử dụng FFmpeg
└── models/
    └── piper/           # Nơi lưu trữ các file model giọng thuyết minh
```
