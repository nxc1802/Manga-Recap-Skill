# API Documentation: Manga Recap Backend Service

Dịch vụ Backend cung cấp các API để điều khiển pipeline Manga Recap tự động, tuỳ chỉnh toàn bộ tham số chạy động (mô hình, tốc độ thuyết minh, bối cảnh truyện, kịch bản dẫn truyện đầu tiên) và theo dõi tiến trình sinh video thời gian thực thông qua Server-Sent Events (SSE).

---

## 1. Health Check Endpoint
Kiểm tra trạng thái hệ thống và cấu hình mô hình mặc định.

* **URL:** `/health`
* **Method:** `GET`
* **Response (JSON):**
```json
{
  "status": "healthy",
  "models": {
    "vlm_model": "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    "llm_model": "nvidia/nemotron-3-ultra-550b-a55b:free"
  },
  "tts_default_speed": 1.0
}
```

---

## 2. Run Recap Pipeline Endpoint
Khởi chạy quy trình recap manga trọn gói và truyền phát trạng thái thời gian thực.

* **URL:** `/api/recap/run`
* **Method:** `POST`
* **Headers:** `Content-Type: application/json`
* **Request Body (JSON):**
```json
{
  "chapter_number": "1",
  "general_story_context": "Ghislain Perdium là Vua Lính Đánh Thuê lừng lẫy của đại lục. Sau khi bị sát hại, hắn trùng sinh quay về quá khứ trong thân xác thời niên thiếu...",
  "project_title": "Mỗi lính đánh thuê trở về đều có một kế hoạch",
  "initial_scripts": {
    "1": "Giữa bóng tối tĩnh mịch của phòng ngủ, Ghislain Perdium giật mình tỉnh giấc, hơi thở dồn dập...",
    "2": "Ghislain nhắm mắt rồi mở to, đưa hai bàn tay gầy gò lên ngắm nghía...",
    "3": "Một bông hoa hy vọng khẽ đặt lên đống tro tàn đổ nát. Kiếp trước hắn đã thất bại..."
  },
  "crawler_urls": {
    "chapter_base_url": "https://truyenqqko.com/truyen-tranh/moi-linh-danh-thue-tro-ve-deu-co-mot-ke-hoach-16550-chap-",
    "base_info_url": "https://truyenqqko.com/truyen-tranh/moi-linh-danh-thue-tro-ve-deu-co-mot-ke-hoach-16550"
  },
  "config_overrides": {
    "vlm_model": "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    "llm_model": "nvidia/nemotron-3-ultra-550b-a55b:free",
    "batch_size": 10,
    "sliding_context_window": 5,
    "vlm_image_resize_shortest_edge": 500,
    "vlm_batch_size": 4,
    "vlm_context_length": 2,
    "max_panels": 40,
    "min_length": 50,
    "max_length": 250,
    "tts_speed": 1.15
  },
  "bgm_path": "data/bgm/theme_song.mp3",
  "output_suffix": "api_test"
}
```

### Các tham số Request Body chi tiết:
* `chapter_number` (string, bắt buộc): Số hiệu chapter muốn xử lý.
* `general_story_context` (string, bắt buộc): Đoạn giới thiệu tổng quát cốt truyện viết thủ công chất lượng cao để định hình đúng nhân vật và thế giới.
* `project_title` (string, mặc định "Mỗi lính đánh thuê trở về đều có một kế hoạch"): Tiêu đề dự án phục vụ việc tạo thư mục và index dữ liệu.
* `initial_scripts` (object, tuỳ chọn): Danh sách kịch bản viết sẵn cho N khung hình đầu tiên để tạo đà dẫn chuyện cho AI. Khóa là số sequence (số nguyên) và giá trị là lời thoại tiếng Việt.
* `crawler_urls` (object, tuỳ chọn): Tuỳ chỉnh URL crawl truyện.
* `config_overrides` (object, tuỳ chọn): Ghi đè cấu hình hệ thống:
  - `vlm_image_resize_shortest_edge`: Kích thước cạnh ngắn nhất của ảnh khi nén và tải lên VLM (mặc định: `500` px).
  - `vlm_batch_size`: Số lượng hình ảnh gửi lên VLM đồng thời trong một API request (mặc định: `4`).
  - `vlm_context_length`: Số lượng khung hình của batch trước được gửi làm ngữ cảnh văn bản cho batch sau (mặc định: `2`).
  - `max_panels`: Giới hạn số lượng khung hình xử lý (dành cho thử nghiệm tốc độ cao).
  - `tts_speed`: Tốc độ thuyết minh của giọng đọc TTS (mặc định: `1.0`). Cho phép giá trị từ `0.5` đến `2.0`.
* `bgm_path` (string, tuỳ chọn): Đường dẫn đến file nhạc nền.
* `output_suffix` (string, mặc định "manual"): Hậu tố của file video để tránh bị ghi đè các lần chạy thử cũ.

---

## 3. Định dạng Dữ liệu SSE Phản hồi (Server-Sent Events)
Khi kết nối với `/api/recap/run`, server sẽ trả về header `Content-Type: text/event-stream` và phát đi các sự kiện theo chuẩn SSE:
`event: <tên_sự_kiện>\ndata: <JSON_object>\n\n`

### Các Sự kiện SSE chính:

1. **`project_setup`**: Khởi tạo dự án thành công.
   ```json
   { "event": "project_setup", "message": "Khởi tạo dự án 'Mỗi lính đánh thuê trở về đều có một kế hoạch'...", "data": { "project_title": "..." } }
   ```
2. **`crawl_start` / `crawl_complete`**: Tiến độ tải hình ảnh truyện.
   ```json
   { "event": "crawl_complete", "message": "Crawl hoàn tất chapter 1.", "data": { "chapter_id": "981ae5cb-..." } }
   ```
3. **`vlm_panel`**: Phân tích hình ảnh VLM của từng khung hình hoàn tất.
   ```json
   {
     "event": "vlm_panel",
     "message": "Hoàn thành phân tích VLM cho khung hình 4.",
     "data": {
       "sequence_number": 4,
       "ocr_text": "Không có",
       "vlm_description": "Ngoại hình nhân vật: ... Hành động: ... Background: ..."
     }
   }
   ```
4. **`llm_script_batch`**: Sinh kịch bản thuyết minh (LLM) cho một lô (batch) hoàn tất.
   ```json
   {
     "event": "llm_script_batch",
     "message": "Đã sinh kịch bản thuyết minh cho các khung hình 4 đến 13.",
     "data": {
       "start": 4,
       "end": 13,
       "script_text": "[Khung hình 4] ... [Khung hình 5] ..."
     }
   }
   ```
5. **`tts_panel`**: Tổng hợp giọng đọc TTS cho từng khung hình thành công.
   ```json
   {
     "event": "tts_panel",
     "message": "Đã tạo giọng đọc cho khung hình 4.",
     "data": {
       "sequence_number": 4,
       "duration_ms": 13896
     }
   }
   ```
6. **`video_clip`**: Kết xuất video clip đơn lẻ cho từng khung hình hoàn thành.
   ```json
   {
     "event": "video_clip",
     "message": "Đã kết xuất xong video clip cho khung hình 4.",
     "data": { "sequence_number": 4, "duration_ms": 13896 }
   }
   ```
7. **`video_concat_start` / `video_mixing_start`**: Tiến trình ghép nối các clip và ghép nhạc nền BGM.
8. **`completed`**: Toàn bộ quy trình hoàn tất thành công. Trả về URL tải/xem video và phụ đề.
   ```json
   {
     "event": "completed",
     "message": "Pipeline executed successfully!",
     "data": {
       "video_path": "data/output_videos/chap_1_final_api_test.mp4",
       "web_video_url": "/data/output_videos/chap_1_final_api_test.mp4",
       "web_srt_url": "/data/output_videos/chap_1_subs_api_test.srt"
     }
   }
   ```
9. **`failed`**: Xảy ra lỗi làm sập quy trình.
   ```json
   { "event": "failed", "message": "Pipeline failed: Supabase error...", "data": { "error": "..." } }
   ```

---

## 4. Hướng dẫn cách code Javascript ở Frontend để nhận SSE từ API POST
Vì API sử dụng phương thức `POST` gửi payload JSON rất lớn, chúng ta không thể dùng đối tượng `EventSource` mặc định của trình duyệt (chỉ hỗ trợ `GET`). Thay vào đó, Frontend cần sử dụng API `fetch` của trình duyệt cùng đối tượng đọc Stream (`ReadableStream`):

```javascript
async function startMangaRecap(payload) {
  try {
    const response = await fetch('http://localhost:8000/api/recap/run', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json'
      },
      body: JSON.stringify(payload)
    });

    if (!response.ok) {
      throw new Error(`HTTP error! status: ${response.status}`);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      
      // Giữ lại dòng cuối cùng chưa hoàn chỉnh để chờ chunk tiếp theo
      buffer = lines.pop();

      let currentEvent = '';
      
      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed) continue;

        if (trimmed.startsWith('event:')) {
          currentEvent = trimmed.replace('event:', '').trim();
        } else if (trimmed.startsWith('data:')) {
          const rawData = trimmed.replace('data:', '').trim();
          try {
            const parsedData = JSON.parse(rawData);
            
            // Xử lý sự kiện nhận được ở Frontend
            handleRecapEvent(currentEvent, parsedData);
            
          } catch (err) {
            console.error("Lỗi parse JSON dữ liệu SSE:", err);
          }
        }
      }
    }
    console.log("Quy trình stream dữ liệu hoàn tất.");
  } catch (error) {
    console.error("Lỗi khi kết nối API:", error);
  }
}

function handleRecapEvent(event, data) {
  console.log(`[Event: ${event}]`, data.message);
  
  // Dựa vào tên event để update UI thời gian thực:
  switch (event) {
    case 'vlm_panel':
      // Show thông tin OCR và mô tả ảnh lên khung hình tương ứng của UI
      updateVlmPanelUI(data.data);
      break;
    case 'llm_script_batch':
      // Show kịch bản text thuyết minh tương ứng lên màn hình tóm tắt kịch bản
      updateScriptBatchUI(data.data);
      break;
    case 'tts_panel':
      // Đánh dấu khung hình đã sinh xong giọng đọc thành công
      updateTtsProgressUI(data.data);
      break;
    case 'video_clip':
      // Tăng thanh tiến trình (progress bar) render video
      updateVideoProgressUI(data.data);
      break;
    case 'completed':
      // Hiển thị player video với nguồn: http://localhost:8000 + data.data.web_video_url
      showFinalVideoPlayer(data.data.web_video_url);
      break;
    case 'failed':
      showErrorMessage(data.message);
      break;
    default:
      updateGeneralStatus(data.message);
  }
}
```
