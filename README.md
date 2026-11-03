# Manga Recap Pipeline Orchestrator (100% Cloud API Architecture)

Dự án Manga Recap Pipeline Orchestrator là hệ thống tự động hóa toàn bộ quy trình tạo video tóm tắt (recap) truyện tranh từ URL. Hệ thống sử dụng VLM (Visual Language Model) và LLM qua API để phân tích hình ảnh và chuyển thể thành kịch bản tiếng Việt có cốt truyện logic, Cloud TTS API (Edge-TTS, Gemini, OpenAI) để sinh giọng thuyết minh tự nhiên và FFmpeg để dựng video, ghép phụ đề (SRT) tự động.

> **Lưu ý kiến trúc**: Hệ thống đã được chuyển đổi hoàn toàn sang **100% cơ chế Call API** (không cần tải hay chạy bất kỳ local model nào như PyTorch, ONNX, YOLO hay Piper), giúp giảm tải dung lượng ổ cứng, tăng tốc độ cài đặt và hoạt động ổn định trên mọi hệ điều hành.

---

## ⚙️ Yêu cầu hệ thống

Trước khi bắt đầu, hãy đảm bảo máy tính của bạn đã cài đặt các công cụ sau:
1. **Python 3.10+**
2. **FFmpeg** (được cài đặt và cấu hình trong PATH hệ thống).
   * *macOS:* `brew install ffmpeg`
   * *Ubuntu/Debian:* `sudo apt install ffmpeg`
   * *Windows:* Tải bản dựng FFmpeg và thêm thư mục `bin` vào PATH.

---

## 🛠️ Hướng dẫn cài đặt và thiết lập

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
```bash
source venv/bin/activate
```
*(Windows: `venv\Scripts\activate.bat`)*

#### Bước 2: Cài đặt các thư viện Python cần thiết
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
   - *(Tùy chọn)* `GOOGLE_API_KEY`: Dành cho giọng đọc Gemini TTS.
   - *(Tùy chọn)* `OPENAI_API_KEY`: Dành cho giọng đọc OpenAI TTS.

---

## 🚀 Hướng dẫn khởi chạy dự án (CLI)

Dự án hoạt động qua hệ thống CLI module hóa:

| CLI Tool | Đường dẫn | Chức năng | Ví dụ câu lệnh |
|---|---|---|---|
| **Run Recap** | `src/cli/run_recap.py` | Chạy pipeline tạo âm thanh TTS (API) và video | `PYTHONPATH=. venv/bin/python src/cli/run_recap.py --project "Warrior Grandpa" --chapter "1" --video-mode auto --voice edge-tts-female` |
| **Crawler CLI** | `src/cli/crawler_cli.py` | Crawl hình ảnh từ webtoon và phân cảnh (recut) | `PYTHONPATH=. venv/bin/python src/cli/crawler_cli.py --chapter "1" --project "Warrior Grandpa" --url "..."` |
| **Import Script** | `src/cli/import_agent_script.py` | Import kịch bản (JSON/Text) vào database | `PYTHONPATH=. venv/bin/python src/cli/import_agent_script.py --chapter "1" --project "Warrior Grandpa"` |
| **Compile Chapters** | `src/cli/compile_chapters.py` | Ghép nhiều chapter video thành 1 video duy nhất | `PYTHONPATH=. venv/bin/python src/cli/compile_chapters.py --project "Warrior Grandpa"` |
| **YouTube Uploader** | `src/cli/youtube_uploader.py` | Upload video thành phẩm lên YouTube qua API v3 | `PYTHONPATH=. venv/bin/python src/cli/youtube_uploader.py --video "data/.../final.mp4" --title "..."` |

### Chọn giọng thuyết minh (Cloud API TTS Pool)
Bạn có thể cấu hình giọng thuyết minh trong `config.yaml` hoặc qua CLI arg `--voice`. Toàn bộ giọng đọc đều chạy qua Cloud API:

*   **Microsoft Edge-TTS Cloud API (Mặc định - Miễn phí - Tốc độ cao):**
    *   `edge-tts-female`: Giọng nữ Hoài My truyền cảm, tự nhiên.
    *   `edge-tts-male`: Giọng nam Nam Minh rõ ràng, trầm ấm.
    *   `edge-tts-en-female`, `edge-tts-en-male`: Giọng đọc tiếng Anh chuẩn.
*   **Google Gemini TTS API:** `gemini-2.5-flash`, `gemini-3.1-flash`, `gemini-kore`.
*   **OpenAI Cloud TTS API:** `openai-tts-alloy`, `openai-tts-nova`, `openai-tts-echo`, `openai-tts-shimmer`.

---

## 📂 Cấu trúc thư mục dự án

```text
├── config.yaml          # File cấu hình mặc định của hệ thống
├── requirements.txt     # Danh sách thư viện Python (nhẹ, không PyTorch/model local)
├── data/
│   └── [project_slug]/  # Thư mục dữ liệu độc lập cho từng bộ truyện
│       ├── image/       # raw, recut, cropped
│       ├── audio/       # clips, chapters, final.wav
│       ├── video/       # clips, chapters, final.mp4
│       └── memory/      # script.csv, character_dictionary.json
├── src/
│   ├── cli/             # 5 CLI Tools điều khiển pipeline
│   ├── core/            # Config loader, logger, DB client, prompts, stitch_segment
│   ├── services/        # Crawler, VLM Gateway, LLM Orchestrator, TTS Service (API), Vision Detector (API)
│   └── render/          # Video engine sử dụng FFmpeg
└── tests/               # Bộ test kiểm tra chất lượng
```
