# Manga-Recap-Skill V3 — Upgrade Implementation Plan

> **Repository:** [nxc1802/Manga-Recap-Skill](https://github.com/nxc1802/Manga-Recap-Skill)  
> **Baseline:** V3, nhánh `main`, audit mã nguồn ngày 08/11/2026  
> **Phạm vi:** Kế hoạch triển khai, **chưa chỉnh sửa code**.  
> **Mục tiêu:** Nâng chất lượng lựa chọn/camera ảnh, khả năng ingest nguồn truyện, phụ đề, thời lượng video, YouTube chapters và hiệu suất sản xuất trên cloud; giữ nguyên kiến trúc Agent-Native.

## 0. Executive summary

Hệ thống hiện phân tách **Agent** (hiểu nội dung, OCR, storytelling, chọn camera) với **Python CLI** (crawl, segment, VieNeu-TTS, FFmpeg render, compile, YouTube). Các thành phần checkpoint JSON, render clip cache và batch chapter đã tồn tại và **phải được bảo toàn**.

Các vấn đề không độc lập:

- **Visual Intelligence dùng chung** giải quyết AutoFocus/scene selection và làm nguồn ứng viên Thumbnail.
- **Script Pacing → TTS → Subtitle/Shot Timing**: thời lượng thực tế được chốt sau TTS, không được ép bằng cách cắt audio/video.
- **Compile → YouTube Chapters**: chapter marker phải tính theo độ dài thực tế của video đã ghép.
- **Cloud Production** nhận các artifact đã duyệt, chạy TTS + Render trong **một phiên cloud** để xuất trực tiếp video hoàn chỉnh và phụ đề.

### Ưu tiên và quan hệ phụ thuộc

| PR | Chủ đề | Ưu tiên | Phụ thuộc |
|---|---|---|---|
| PR-01 | AutoFocus + Semantic Visual Director | P0 | Không; tạo visual regions dùng lại |
| PR-02 | Sentence-Aware Subtitle + CLI | P0 | Không; tích hợp render cache |
| PR-03 | Soft Script Pacing | P0 | Không; trước TTS |
| PR-04 | Universal/Agentic Crawler | P0 | Không; tương thích manifest hiện tại |
| PR-05 | YouTube Chapter Timeline | P1 | Compile/YouTube hiện tại |
| PR-06 | Thumbnail Director | P1 | Ưu tiên tái sử dụng PR-01 |
| PR-07 | One-Session Cloud Production | P1 | PR-01/02/03 ổn định; tận dụng batch hiện tại |

> Thứ tự PR là thứ tự khuyến nghị theo tác động chất lượng; PR-02, PR-03, PR-04, PR-05 có thể thực hiện song song. Không tự động gộp thành một PR lớn.

---

## 1. Ràng buộc kiến trúc và nguyên tắc triển khai

1. **Không thêm Cloud LLM/VLM API trực tiếp vào Python core.** Nếu cần AI, để Coding Agent xử lý qua khả năng vision/tool/MCP rồi xuất kết quả có cấu trúc. CLI chỉ validate và thực thi.
2. **Không thay VieNeu-TTS/FFmpeg/storage khi chưa chứng minh điểm nghẽn.** Tối ưu theo profiling và benchmark.
3. **Tương thích ngược** với `project.json`, `manifest.json`, `script.json`, CLI cũ; trường mới nên optional và có fallback an toàn.
4. **Script, Visual Shot Plan, Subtitle Cues là ba hợp đồng khác nhau.** Không buộc `1 panel = 1 narration = 1 shot`.
5. **Các mốc render cache luôn có fingerprint bao gồm config liên quan.** Thay số dòng/font/chunking, shot plan, camera hay encoder phải invalidate đúng render artifact.
6. **Không mất nội dung/continuity để chạy nhanh.** `skip` chỉ áp dụng cho *visual shot* sau khi chứng minh không còn nội dung hình ảnh quan trọng; narration có thể sử dụng panel lân cận.
7. **Không cam kết crawl mọi site.** Respect robots/ToS, bản quyền, quyền truy cập và rate limit; không bypass CAPTCHA/paywall/DRM hoặc các hạn chế truy cập.
8. **Benchmark trước/sau** trên cùng chapter, cùng voice, chất lượng render, hardware và điều kiện đo.

### Artefact/contract đề xuất

```text
data/projects/<slug>/
  project.json                           # pacing, subtitle, visual, production settings
  source_profiles/<domain>.json          # cấu hình adapter đã kiểm thử (nếu được lưu)
  chapters/<n>/
    manifest.json                        # ảnh gốc + panel metadata + status
    visual_analysis.json                 # vùng nhân vật/hành động/thoại/trống, scores
    shot_plan.json                       # visual shots, crop/motion/timing, provenance
    script.json                          # narration/TTS contract hiện tại
    subtitle_cues.json                   # cue text/timing/style đã chốt
    thumbnail_candidates.json            # candidate scores, crop, lý do chọn
    audio/clips/*.wav                    # file audio các câu thoại (WAV 48kHz)
    video/clips/*.mp4                    # render cache
    video/final.mp4
  feature_timeline.json                  # offsets thật của chapter trong compilation
  production_job.json                    # job/checkpoint cloud, không chứa secrets
```

Các file mới chỉ là **đề xuất**, không phải trạng thái hiện tại.

---

## 2. PR-01 — AutoFocus Character + Semantic Visual Director

### 2.1. Hiện trạng đã xác nhận

**Mã nguồn:**

- [`src/core/streaming_segmenter.py`](https://github.com/nxc1802/Manga-Recap-Skill/blob/main/src/core/streaming_segmenter.py): phát hiện gutter nhờ row luminance/variance; `refine_intervals()` tự chia dải cao hơn `target_width × 3.5` tại row variance tối thiểu. Không biết đường cắt qua nhân vật hoặc hành động.
- `extract_panels()`: `if final_h < target_width` padding, `elif crop_var < sufficiency_variance` mới đánh `SKIP`; panel ngắn đi vào nhánh padding **không qua kiểm tra low-variance** này.
- [`rules/vision_ocr.md`](https://github.com/nxc1802/Manga-Recap-Skill/blob/main/.agents/skills/manga-recap/rules/vision_ocr.md): chỉ dẫn Agent không focus speech bubbles, ưu tiên nhân vật và dùng normalized `focus_box`; chưa có kiểm tra semantic tự động trên kết quả cuối.
- [`src/core/schemas.py`](https://github.com/nxc1802/Manga-Recap-Skill/blob/main/src/core/schemas.py): `ScriptSegment` có `panel_ids`, **một** `camera_motion`, **một** `focus_box`.
- [`src/render/camera.py`](https://github.com/nxc1802/Manga-Recap-Skill/blob/main/src/render/camera.py): `STATIC` fit/pad ảnh, không auto-crop bằng focus box; `vertical_scroll` quét toàn chiều cao; với `allow_animation=False`, motion chuyển thành `STATIC`.
- [`src/render/video_engine.py`](https://github.com/nxc1802/Manga-Recap-Skill/blob/main/src/render/video_engine.py): `render_chapter()` chia thời gian segment **đều cho tất cả panel_ids** (`f_base = f_total // n_panels`); `focus_box` chỉ áp dụng cho panel đầu (`k == 0`), panel sau thường static; renderer resolve panel từ manifest nhưng không tạo semantic filter trên từng shot.
- [`rules/narrative_recap.md`](https://github.com/nxc1802/Manga-Recap-Skill/blob/main/.agents/skills/manga-recap/rules/narrative_recap.md): khuyến nghị `1 panel ↔ 1 segment`, gộp panel bị hạn chế. Điều này vô tình thúc đẩy quá nhiều scene cuts.

**Các lỗi đầu ra cần xử lý:** chỉ chat box; vùng trắng/trống; chủ thể quá nhỏ; nhân vật bị cắt qua 2 panel liên tiếp; cuộn dọc qua khoảng trống; nhảy shot quá thường xuyên; frame không khớp narration.

### 2.2. Thiết kế đề xuất

Tách pipeline:

```text
Raw Image/Panel + Script Context
    ↓
Region Analysis (character/face/action/object/landscape/bubble/empty)
    ↓
Semantic Scene Grouping (nhóm crop cùng cảnh / nối qua biên cắt)
    ↓
Visual Shot Planning (chọn ít shot có ích, camera focus + duration)
    ↓
Quality Gate (kiểm tra crop tại đầu/giữa/cuối shot)
    ↓
Existing FFmpeg Renderer
```

**Không đồng nhất panel với shot.** Một `ScriptSegment` có thể dùng 0–N visual shots; một shot có thể lấy vùng từ raw strip, panel hoặc vùng ghép hợp lệ. Nếu narration đi qua scene không có hình quan trọng, được phép giữ shot trước hoặc dùng shot lân cận có ngữ nghĩa liên quan.

**Visual semantic labels:** `character`, `face`, `action`, `object`, `landscape`, `dialogue_only`, `empty_spacer`, `transition`, `advertisement`, `uncertain`.

**Scene grouping:** so sánh source ranges, scene continuity, vùng ảnh và nhân vật để hợp nhất các cut liên tiếp thuộc một cảnh. Với dải truyện cao, ưu tiên giữ tọa độ liên tục theo raw source/Virtual Continuous Coordinate Space; không chỉ ghép hai JPG đã crop mất dữ liệu.

**Camera modes:** bổ sung `static_focus` (*không animation nhưng có crop đúng chủ thể*), `subject_pan`, `content_aware_scroll`. `vertical_scroll` mới không quét vô điều kiện từ đầu đến cuối; sử dụng key regions và smooth easing, tránh di chuyển qua gutter dài.

**Candidate ranking:** Agent chọn chủ thể phù hợp câu narration; phần deterministic đánh giá subject coverage, bubble dominance, empty ratio, crop safety, số lần cut, chuyển động, thời gian đọc hiểu ảnh. Không dùng variance làm chỉ báo duy nhất.

**Visual QC:** render ảnh xem trước ở t=0%, 50%, 100% cho mỗi shot; kiểm tra cắt khuôn mặt, crop quá bé, chỉ chứa chữ, thiếu chủ thể và quá nhiều cảnh giống nhau. Các ngưỡng phải benchmark, không hard-code một ngưỡng cho mọi thể loại.

### 2.3. Schema gợi ý

```json
{
  "segment_id": "012",
  "shots": [
    {
      "source": {"kind": "raw_region", "image_id": "page_004.jpg"},
      "focus_box": [0.12, 0.21, 0.82, 0.88],
      "subject": "main_character",
      "motion": "static_focus",
      "duration_ratio": 0.70,
      "reason": "Narration describes character reaction"
    },
    {
      "source": {"kind": "panel", "panel_id": "panel_013"},
      "focus_box": [0.34, 0.12, 0.91, 0.76],
      "subject": "important_object",
      "motion": "zoom_in",
      "duration_ratio": 0.30
    }
  ]
}
```

`duration_ratio` cộng thành 1 theo segment. Shot timing được quy đổi thành frame **sau TTS** dựa vào audio WAV thật, dùng cumulative frame anchoring để không tích lũy drift.

### 2.4. Checklist thực thi

- [ ] Sửa low-variance/blank detection tách khỏi `elif` padding; kiểm thử ảnh ngắn/trống.
- [ ] Giữ provenance của panel từ raw image/virtual coordinates; tránh cắt không thể phục hồi.
- [ ] Thiết kế `visual_analysis.json`/`shot_plan.json` Pydantic với fallback sang `panel_ids + focus_box` cũ.
- [ ] Viết Agent rule để nhận diện vùng cảnh quan trọng và đánh dấu độ tin cậy.
- [ ] Không render panel `skip/context_only` trừ khi có visual justification xác nhận.
- [ ] Thêm `static_focus` không phụ thuộc cờ animation.
- [ ] Thêm camera content-aware theo key regions, easing và giới hạn chuyển động.
- [ ] `render_chapter()` đọc shot plan; bỏ chia đều thời lượng theo số panel khi shot plan hiện diện.
- [ ] Không dùng một focus box chung cho mọi panel; từng shot có focus/motion riêng.
- [ ] Quality gate kiểm tra thumbnail preview frame của từng shot trước final.
- [ ] Fingerprint render bao gồm shot plan, camera, focus, subtitle config và source hash.
- [ ] Giữ rendering legacy nếu không có shot plan.

### 2.5. Acceptance / benchmark

Dùng 20–30 panel khó: bubble-only, blank, nhân vật ở rìa, ảnh dọc dài, một nhân vật qua hai crop, cảnh có nhiều chủ thể, scene liên tiếp giống nhau. Đánh giá mù trước/sau trên video cùng narration.

- [ ] Không còn shot *vô nghĩa* chỉ hiện bong bóng thoại hoặc khoảng trắng, trừ trường hợp được đánh dấu dụng ý nghệ thuật.
- [ ] Một nhân vật bị cắt do gutter/split có thể được trình bày trọn vẹn trong ít nhất một shot.
- [ ] Không làm mất diễn biến quan trọng/continuity khi giảm scene cuts.
- [ ] Render không phụ thuộc `allow_animation=True` để lấy đúng vùng focus.
- [ ] Số shot/scene cuts giảm trên tập lỗi, đồng thời reviewer đánh giá nhịp kể tự nhiên hơn.
- [ ] Script/audio duration và final audio/video sync vẫn đạt validator hiện có.

---

## 3. PR-02 — Subtitle Intelligence (ưu tiên hoàn thành câu)

### 3.1. Hiện trạng đã xác nhận

- [`src/render/subtitle.py`](https://github.com/nxc1802/Manga-Recap-Skill/blob/main/src/render/subtitle.py): `chunk_vietnamese_text()` tách theo số từ/ký tự và line limit, **không ưu tiên câu/mệnh đề**; `split_text_into_cues()` phân bổ audio duration theo tỷ lệ số từ, không có word-level alignment thực.
- `SubtitleGenerator` mặc định 38 char/line, 2 lines; nhưng `VideoEngine.__init__` tạo `SubtitleGenerator(max_chars_per_line=42, max_lines=2)`.
- `video_engine.py::create_subtitle_overlay_png()` lại có wrap khoảng 38 ký tự, limit 2 lines và font-size base 26 trên 1280px (tăng theo output width). Có nguy cơ bất nhất giữa cue generation và overlay renderer.
- [`src/cli/main.py`](https://github.com/nxc1802/Manga-Recap-Skill/blob/main/src/cli/main.py): chưa có `--subtitle-lines` / `--subtitle-font-size` / `--subtitle-chunking`.
- Render clip cache fingerprint chưa chứa đầy đủ style/chunking settings nếu được bổ sung.

### 3.2. Giải pháp

**Sentence-aware chunker**: token hóa ranh giới `.?!…`, ưu tiên ngữ nghĩa; nếu câu vừa frame thì giữ trọn câu. Nếu quá dài, tách tại dấu phẩy/chấm phẩy/mệnh đề, hạn chế orphan words (1–2 từ cuối thành một cue). Không làm nhỏ font vô hạn để ép câu quá dài vào 1 dòng.

**Shared cue contract**: generate một `subtitle_cues.json`/SRT từ chuỗi đã chuẩn hóa; SRT và PNG/burn overlay dùng cùng text breaks, không tự wrap lại khác nhau. Dùng đo pixel bằng font metrics thay vì coi số ký tự là giới hạn chính. Timing có thể dùng word proportion trước; tùy chọn forced alignment sau nếu chất lượng yêu cầu.

**CLI (đề xuất):**

```bash
manga-recap render --project my_manga --chapter 1 \
  --subtitle-lines 1 \
  --subtitle-font-size 26 \
  --subtitle-chunking sentence-aware
```

Default mới: **1 dòng**, **font size base 26 ở 1280px như renderer hiện tại**, `sentence-aware`, bottom-center. Đưa vào `project.json`; CLI override cấu hình project. Font size phải được định nghĩa nhất quán theo canvas/scale để preview 720p và final 1080p có kích thước thị giác tương đương.

### 3.3. Checklist + nghiệm thu

- [ ] Tạo parser sentence/clause-aware, kiểm thử tiếng Việt có dấu, dấu ngoặc kép, câu cảm thán, ellipsis, tên nhân vật, dấu hai chấm.
- [ ] Giữ trọn câu trong cue **nếu** câu vừa một dòng tại size đã chọn; nếu không, tách ở ranh giới tự nhiên.
- [ ] SRT và burned subtitles khớp hoàn toàn về text/timing.
- [ ] Expose CLI và project defaults; cập nhật Skill.
- [ ] Fingerprint render chứa font size, max lines, chunker mode và subtitle cue hash.
- [ ] Tạo test visual xem preview 720p và final 1080p; subtitle không tràn safe area.
- [ ] Kiểm tra đồng bộ cue và audio; ưu tiên timing thật nếu có, không khẳng định word-proportional là forced alignment.

---

## 4. PR-03 — Script Pacing / Soft Duration Budget

### 4.1. Hiện trạng đã xác nhận

- [`rules/narrative_recap.md`](https://github.com/nxc1802/Manga-Recap-Skill/blob/main/.agents/skills/manga-recap/rules/narrative_recap.md) khuyến nghị 1 panel/segment, 4–8 giây, 50–250 ký tự, mô tả theo bối cảnh–nhân vật–hành động–lời thoại. Những quy tắc mềm này làm chất lượng kể chuyện dày hơn, nhưng dễ tạo script dài cho mọi panel.
- `RecapScript` có `total_duration_seconds`; `AudioSettings.default_speed` thay đổi tốc độ **đọc**, không kiểm soát **mật độ nội dung**.
- Không thấy budget duration định trước cho một chapter/feature trong schema và workflow đang dùng.
- Ví dụ người dùng báo: video 10 chapters, khoảng **279.437 video frames**, dài **2,5 giờ**. Đây là video frames, không được nhầm với số source panels. Cần kiểm chứng FPS/duration thực qua ffprobe trước khi dùng làm baseline.

### 4.2. Giải pháp

**Story Outline → Importance Scoring → Soft Time Budget → Agent Script → Pre-TTS Estimate → Gentle Revision → TTS Actual.**

```yaml
script_pacing:
  mode: balanced           # concise | balanced | detailed
  target_total_minutes: 90 # minh họa, không áp dụng cứng cho mọi video
  tolerance: 0.20          # ±20%, chỉ cảnh báo/đề xuất sửa
  preserve_important_dialogue: true
  adaptive_scene_density: true
```

- `target_total_minutes` có thể khai báo theo feature hoặc chapter; chia thời lượng theo importance và số scene, không chia đều.
- Ước lượng duration bằng words/syllables-per-second đo từ các WAV trước đó của cùng voice/speed; báo khoảng tin cậy, không xem số ký tự là thời lượng chính xác.
- Trước TTS, nếu vượt budget: giảm miêu tả trùng lặp, gộp hành động liên tục/cảnh phụ, giảm lồng thoại không cần thiết; **không cắt cliffhanger, nguyên nhân–hệ quả, tình tiết mới hoặc thoại đắt giá**.
- Sau TTS, báo `actual_duration` và deviation; không time-stretch audio chỉ để ép đạt target.

### 4.3. Checklist + nghiệm thu

- [ ] Thêm project-level pacing config và Agent rule tương ứng.
- [ ] Thêm pre-TTS duration estimator + report theo chapter/scene.
- [ ] Cho phép mode `off`/legacy tương thích ngược.
- [ ] Kiểm thử `concise`/`balanced`/`detailed` trên cùng 1–2 chapter.
- [ ] Reviewer xác nhận cốt truyện không mất nguyên nhân–kết quả và narration vẫn tự nhiên.
- [ ] Có báo cáo planned vs estimated vs actual, không khóa cứng thời lượng video.

---

## 5. PR-04 — Universal/Agentic Manga Crawler

### 5.1. Hiện trạng đã xác nhận

- [`src/cli/crawl_cmd.py`](https://github.com/nxc1802/Manga-Recap-Skill/blob/main/src/cli/crawl_cmd.py) gọi `crawler_service.crawl_raw_pages(url, raw_dir)` hoặc import thư mục local.
- [`src/services/crawler_service.py`](https://github.com/nxc1802/Manga-Recap-Skill/blob/main/src/services/crawler_service.py) dùng HTTPX, BeautifulSoup, imageList regex / img attributes; có nhánh chuyên Webtoons và fallback cho một số cấu trúc `page-chapter`/URL ảnh.
- Thiếu series URL resolver chung, browser JS fallback, profile cache, image provenance/validation đầy đủ; `download_image()` có thể coi file đã tồn tại là thành công mà không verify nội dung.
- URL ví dụ `https://truyenqqko.com/truyen-tranh/lam-nong-dan-trong-toa-thap-thu-thach-14069` có thể là **series landing page**, cần resolver tới chapter reader; chưa kiểm tra live DOM trong audit này, không giả định selector cụ thể.

### 5.2. Thiết kế

```text
URL → classify(series/chapter/direct_image)
    → known adapter? → HTTPX + selectors
    → generic DOM + lazy-load extraction
    → (if needed, permitted) browser MCP/Playwright + Coding Agent discovery
    → validate discovered adapter on sample chapter
    → cache source_profile.json
    → ordered image manifest + download + decode/checksum validation
    → current Segmenter
```

**Roles:**
- `SeriesResolver`: tìm chapter listing và link reader (chỉ với nội dung truy cập hợp lệ).
- `ChapterExtractor`: trích image URLs có thứ tự; hỗ trợ `data-src`, `data-original`, `srcset`, embedded JS và lazy-load khi cần.
- `SiteAdapterRegistry`: domain-specific config đã được xác nhận; dùng lại cho chapter sau.
- `AgentBrowserDiscovery`: Agent dùng browser MCP/Playwright để quan sát cấu trúc trang mới và tạo adapter; không cần AI gọi cho từng ảnh hay từng chapter sau khi adapter chạy tốt.
- `ImageVerifier`: decode image thật, kiểm tra MIME, resolution, duplicate, placeholder, HTML giả ảnh, số lượng, thứ tự và missing pages.

**Network/permissions:** ủng hộ backoff, bounded concurrency, origin/referrer hợp lệ, có logging; không bypass CAPTCHA, paywall, DRM hoặc kiểm soát truy cập.

### 5.3. Checklist + nghiệm thu

- [ ] Tách crawler_service thành orchestrator + resolver + extractor adapters, giữ CLI cũ hoạt động.
- [ ] Thiết kế `source_profile` có version, domain, selector, confidence, last_verified.
- [ ] Browser fallback chỉ khi HTTP/DOM extraction chưa đạt validation.
- [ ] Tự nhận diện series URL vs chapter URL và xử lý lựa chọn chapter theo yêu cầu.
- [ ] Verify ảnh trước khi ghi checkpoint completed; download tồn tại phải checksum/decode lại.
- [ ] Test Webtoons (regression), một website chapter-reader mới và import local.
- [ ] Có error taxonomy: blocked/auth-required, unsupported structure, no chapters, no images, partial download, corrupt asset.
- [ ] Không tự tuyên bố website đã được hỗ trợ nếu chưa có một chapter end-to-end thành công.

---

## 6. PR-05 — YouTube Chapters trên thanh tiến trình

### 6.1. Hiện trạng đã xác nhận

- [`src/cli/youtube_cmd.py`](https://github.com/nxc1802/Manga-Recap-Skill/blob/main/src/cli/youtube_cmd.py) có `generate_timestamps_from_script()`, tạo marker khoảng mỗi >=25 giây dựa trên `narrator_text`; metadata lưu timestamps trong `youtube_metadata.json` và description.
- [`src/cli/compile_cmd.py`](https://github.com/nxc1802/Manga-Recap-Skill/blob/main/src/cli/compile_cmd.py) concatenate chapter MP4 qua FFmpeg `-c copy`, nhưng chưa lưu offset start/end chính xác của từng chapter.
- Đối với feature upload, `youtube_cmd.py` tìm `feature.youtube.json`; nếu không có thì description mặc định có thể thiếu chapter markers.
- YouTube Chapters trên player phụ thuộc timestamp hợp lệ trong description **và điều kiện kênh/video**, không chỉ có trường timestamps trong JSON. Thông lệ YouTube: bắt đầu `00:00`, >=3 chapter, mỗi chapter >=10 giây, mốc tăng dần; kiểm tra tài liệu hiện hành khi triển khai.

### 6.2. Giải pháp

- Khi compile, lấy **duration thực** từng MP4 bằng ffprobe, tính cumulative offsets, lưu `feature_timeline.json` cùng chapter title.
- `youtube metadata --chapter feature` đọc timeline này để tạo section timestamps trong description, không dùng thuật toán mỗi 25 giây cho video compilation.
- Có 2 cấp: `chapter boundaries` mặc định; `story arcs` chỉ thêm nếu chapter dài và agent có semantic labels, đồng thời không tạo quá dày.
- Validate mốc monotonic, không bị lặp do rounding, chapter đủ dài, mốc `00:00` và >=3 markers; với compilation ít chapter, có thể chia thành các story arcs hợp lệ thay vì bịa timestamp.
- Thực hiện post-upload verification qua metadata đã upload và kiểm tra player/manual khi cần, vì hiển thị Chapters không được bảo đảm chỉ bằng API upload thành công.

### 6.3. Checklist + nghiệm thu

- [ ] `compile` xuất `feature_timeline.json` với start/end/tên chapter, dựa trên duration đo bằng ffprobe.
- [ ] `youtube metadata` cho feature dùng offsets, descriptions valid.
- [ ] Giữ chapter-level metadata cũ nhưng thay marker mỗi 25s bằng scene-based khi có dữ liệu.
- [ ] Validate quy tắc YouTube và log khi không thể tạo chapters hợp lệ.
- [ ] Đối chiếu mỗi mốc timeline với đúng nội dung video thực tế.
- [ ] Kiểm tra trên video upload thử đủ điều kiện để xác nhận Chapters xuất hiện trên thanh phát.

---

## 7. PR-06 — Thumbnail Director: key-frame selection + AI-assisted editing

### 7.1. Hiện trạng đã xác nhận

- [`src/cli/youtube_cmd.py`](https://github.com/nxc1802/Manga-Recap-Skill/blob/main/src/cli/youtube_cmd.py) ghi `thumbnail_concept` văn bản cố định; upload hỗ trợ `--thumbnail` hoặc `feature_thumbnail.jpg`.
- [`rules/youtube_publishing.md`](https://github.com/nxc1802/Manga-Recap-Skill/blob/main/.agents/skills/manga-recap/rules/youtube_publishing.md) có hướng dẫn 3–5 từ, chữ lớn, tránh góc dưới phải, cặp Title–Thumbnail.
- Chưa thấy pipeline thực thi chấm điểm toàn bộ frame/candidate, tạo nhiều biến thể hoặc kiểm tra character consistency.

### 7.2. Giải pháp

**Default = Source-Grounded Edit:** đọc `visual_analysis.json` + script và raw panels, đề xuất 5–10 cảnh cao trào/cảm xúc → chọn Top 3–5 khác biệt → crop 16:9 → làm nổi chủ thể, chữ, tương phản → xuất thumbnail variants. Ưu tiên raw/panel source thay vì extract video frame đã bị encode.

**Optional = AI Image Edit:** dùng character/source references để tăng biểu cảm/bố cục/ánh sáng qua editing tool mà Agent có quyền truy cập; tránh tạo thêm biến cố hoặc thay đổi danh tính nhân vật. Chỉ dùng image generation hoàn toàn mới nếu được cho phép, vì rủi ro sai character/style/cốt truyện.

**Ranking signals:** narrative importance, emotional impact, subject visibility, scene clarity, composition, safe-zone for typography, visual curiosity. Điểm số là heuristic khởi đầu, không coi là predictor CTR đã được kiểm chứng.

### 7.3. Checklist + nghiệm thu

- [ ] Agent xuất `thumbnail_candidates.json` gồm source, focus, semantic explanation, rank, overlay text.
- [ ] Composer tạo ít nhất 3 phương án 16:9 có safe zones và preview thu nhỏ.
- [ ] Cho phép user chọn hoặc Agent tự chọn khi có confidence rõ ràng.
- [ ] Có tùy chọn `source_edit` vs `ai_edit`; `source_edit` default.
- [ ] Hook file đã chọn vào YouTube upload, tránh `thumbnail_concept` hard-code.
- [ ] Review nhân vật/scene đúng tác phẩm, không crop mất chủ thể, không text quá nhỏ.
- [ ] Có thể lưu CTR/impressions theo thử nghiệm về sau; chưa giả định tăng CTR khi chưa có dữ liệu.

---

## 8. PR-07 — Kaggle/Colab Single-Session TTS + Render

### 8.1. Hiện trạng đã xác nhận

- [`src/cli/batch_cmd.py`](https://github.com/nxc1802/Manga-Recap-Skill/blob/main/src/cli/batch_cmd.py) đã chạy tuần tự chapter: validate → TTS → render → export → optional compile; có isolation, retry và render cache skip.
- [`src/cli/tts_cmd.py`](https://github.com/nxc1802/Manga-Recap-Skill/blob/main/src/cli/tts_cmd.py): TTS xử lý utterance với ThreadPoolExecutor mặc định 2, cập nhật `script.json` và invalidates render khi audio thay đổi.
- [`src/tts/engine.py`](https://github.com/nxc1802/Manga-Recap-Skill/blob/main/src/tts/engine.py): `get_instance(force_mode)` khởi tạo mới nếu `force_mode is not None`; `handle_tts` dùng `get_tts_engine(force_mode=NEURAL)` → khả năng nạp lại engine theo chapter, cần profile thực tế.
- [`src/render/video_engine.py`](https://github.com/nxc1802/Manga-Recap-Skill/blob/main/src/render/video_engine.py): FFmpeg subprocess riêng cho từng clip, PNG subtitle overlays, encoder `libx264` trên Linux; chỉ có nhánh `h264_videotoolbox` cho macOS; concat/mux `-c copy` không cần GPU.
- [`src/cli/main.py`](https://github.com/nxc1802/Manga-Recap-Skill/blob/main/src/cli/main.py): `--hw-accel` mới có `none`, `videotoolbox`, `auto`; chưa có `h264_nvenc`; batch chưa forward đầy đủ workers/hardware config xuống các stage.

**Lưu ý:** GPU không tự tăng tốc mọi filter/render. TTS neural có thể hưởng lợi GPU, nhưng Pillow, overlay/scale nhiều bước có thể ở CPU, còn NVENC chỉ tăng tốc encoder; phải đo pipeline end-to-end.

### 8.2. Mục tiêu đúng

**Một lần chuẩn bị dữ liệu → một cloud session → TTS toàn bộ chapter → render toàn bộ chapter → compile/metadata → export/copy kết quả.** Xử lý tuần tự hai stage TTS và Render trong cùng một phiên cloud để tối ưu thời gian khởi tạo môi trường.

```text
Local Agent:
  source images + script + shot plan + configs
      ↓ package / artifact manifest
Kaggle / Colab:
  initialize VieNeu once
  synthesize audio cho toàn bộ script theo chapter
  persist WAV + exact durations + metadata
  render shots using verified encoder
  persist MP4/SRT + checkpoints chapter-wise
  compile video + feature timeline
      ↓
Download/sync only final outputs (MP4, SRT, timeline, metadata)
```

### 8.3. Thay đổi cần làm

1. **Persistent TTS service trong một process**: sửa lifecycle của singleton, chỉ tái khởi tạo khi thay đổi backend/config thực, thử workers=1/2/4 trên GPU vì concurrent inference không mặc định nhanh hơn hoặc thread-safe.
2. **Encoder selection**: CLI `--encoder auto|libx264|h264_nvenc|h264_videotoolbox`; probe `ffmpeg -encoders` + smoke-test thật trên GPU/driver; fallback CPU có warning. Không đặt `-hwaccel` thành synonym của NVENC encode.
3. **Giảm overhead per-shot**: profile số FFmpeg process, PNG overlay count, pixel filters, I/O, CPU usage/GPU utilization; benchmark chunked render hoặc reusable subtitle overlay, nhưng tránh filter graph khổng lồ.
4. **Cloud job package + manifest**: bảo đảm đồng bộ project/chapters/script/shot plan/voice config/BGM/model weights hoặc cơ chế tải hợp lệ; có SHA-256, schema version, không kèm credential.
5. **Checkpoint bền vững**: sync sau từng chapter/stage vì runtime có giới hạn thời gian và có thể bị thu hồi.
6. **Batch CLI forwarding**: `--encoder`, workers TTS/render, quality, subtitle settings, output paths phải được truyền xuống đúng handler.
7. **Artifacts**: Chỉ đóng gói và đồng bộ MP4, SRT, feature timeline, metadata xuất bản và logs.

### 8.4. Checklist + nghiệm thu

- [ ] Chạy local một chapter end-to-end bằng CPU (baseline).
- [ ] Chạy trên cloud TTS với cùng voice, đối chiếu chất lượng/segment duration.
- [ ] Đo thời gian model init và memory trước/sau persistent backend.
- [ ] Test `h264_nvenc` nếu runtime hỗ trợ, fallback CPU nếu không.
- [ ] Thống kê breakdown: TTS inference, TTS model init, panel encode, subtitle overlay, concat/mux, I/O, upload/download.
- [ ] Benchmark cùng số chapter/segments/frame resolution/fps/quality để so sánh.
- [ ] Bị ngắt giữa chừng vẫn resume an toàn theo checkpoint chapter.
- [ ] Verify A/V sync, codec/profile, 1080p/30fps, SRT và các output paths.
- [ ] Không claim GPU speedup trước khi có đo thời gian end-to-end và chi phí cloud thực.

---

## 9. Cross-cutting integration: Config → Skill → CLI → Render Cache

| Setting | Proposed location | Default | Must affect |
|---|---|---|---|
| `visual.smart_focus` | project.json | `true` khi có shot plan, legacy fallback | shot planner/render fingerprint |
| `visual.camera_behavior` | project.json | `content_aware` (khi có shot plan) | shot planner |
| `subtitle.max_lines` | project.json | **1** | cue generator/renderer/cache |
| `subtitle.font_size` | project.json | **26** base tại 1280px | renderer/cache |
| `subtitle.chunking` | project.json | `sentence-aware` | SRT/overlay/cache |
| `script_pacing.mode` | project.json | `balanced` | Agent scripting |
| `script_pacing.target_total_minutes` | project/feature config | `null` (user chọn) | planning report |
| `thumbnail.mode` | project.json | `source_edit` | candidate/composer |
| `production.encoder` | project.json / CLI | `auto` | render fingerprint |
| `production.tts_workers` | cloud config | benchmark-tuned | TTS service |
| `production.render_workers` | cloud config | benchmark-tuned | FFmpeg pool |

**Xung đột hiện trạng cần xử lý:** `SKILL.md`/`AGENTS.md` mô tả static/no animation làm default, nhưng CLI `render --animation` hiện mặc định `True` (`argparse.BooleanOptionalAction`). Thiết lập nguồn cấu hình thống nhất, phân biệt `static_focus` với animation và đảm bảo CLI explicit override config project.

**Versioning:** thêm `config_version`/`shot_plan_version`/`source_profile_version` hoặc migration adapter; không thay đổi ngầm ý nghĩa field cũ.

**Testing:** unit (text segmentation/coordinates/timing), integration (JSON→CLI→artifacts), E2E (2–3 chapter), adversarial (tall strips, broken files, missing audio, site DOM changed, cache stale), manual visual review.

---

## 10. Rollout và nghiệm thu theo giai đoạn

### Phase A — Quality foundations

- [ ] PR-01 Smart Focus + visual shot plan chạy được, tương thích ngược với legacy script.
- [ ] PR-02 subtitle 1 dòng mặc định, cắt theo câu, không tràn và không lệch cue.
- [ ] PR-03 pacing estimator/report xuất được trước TTS, soft budget không phá narration.

**Gate A:** 1 chapter có các panel lỗi được reviewer đánh giá rõ ràng tốt hơn; không regression A/V sync, không mất story continuity.

### Phase B — Ingestion & publishing

- [ ] PR-04 thử được Webtoons cũ + ít nhất 1 site có cấu trúc khác, có error report cho site không hỗ trợ.
- [ ] PR-05 feature timeline đúng chapter offsets và chapters trên YouTube được xác minh trên video thử đủ điều kiện.
- [ ] PR-06 tạo >=3 thumbnail candidates đẹp, có review/selection rồi upload đúng file.

**Gate B:** một truyện từ nguồn mới chạy đến `manifest.json`; một feature xuất metadata chapters hợp lệ; thumbnail có provenance.

### Phase C — Cloud production

- [ ] PR-07 cloud job TTS + Render trong một session, persistence/checkpoint và benchmark CPU vs GPU.
- [ ] Test resume theo checkpoint, render cache invalidation, partial chapter failure, upload/download và dataset quota.
- [ ] Benchmark 10-chapter feature nếu tài nguyên cho phép, giữ render output đúng specs.

**Gate C:** cùng input cho chất lượng hợp lệ và output hoàn chỉnh, có báo cáo throughput/cost thực tế; không cần thao tác chuyển môi trường thủ công giữa TTS và Render.

---

## 11. Regression suite và bộ dữ liệu đánh giá tối thiểu

**Không cần build dataset lớn trước khi chứng minh hiệu quả.** Lấy 1–2 chapter hiện có, kèm 20–30 panel được chọn có chủ đích và vài source URLs có quyền truy cập hợp lệ.

| Test case | Expected |
|---|---|
| Short blank panel + padding | Không bị đánh `ACTIVE` vô điều kiện |
| Speech bubble + character at bottom | Frame focus character, không bubble-dominant |
| Single character across split boundary | Có shot không cắt cụt nhân vật |
| Very long vertical panel with gutter | Camera không cuộn quá lâu qua khoảng trống |
| Consecutive similar panels | Ít cut không cần thiết, narration vẫn đầy đủ |
| `--no-animation` + focus | Crop tĩnh đúng chủ thể |
| Sentence just over line limit | Không tách 1–2 từ cuối vô lý; font không thu nhỏ quá mức |
| Subtitle preview/final | Same semantic chunks, no overflow, sync OK |
| Series URL vs chapter URL | Phân loại và lấy đúng chapter image list |
| Placeholder/HTML returned for image | Không đánh crawl completed |
| Compiled chapter boundaries | Marker khớp thời điểm chuyển chương |
| Different script pacing modes | Duration khác có kiểm soát, không mất story beats |
| Cloud disconnect during batch | Re-run resume đúng chapter chưa hoàn tất theo checkpoint |
| Render settings changed | Chỉ invalidates relevant render cache |

### Các chỉ số cần ghi lại

- **Visual:** số cut/phút, số shot bị đánh giá vô nghĩa, tỷ lệ crop giữ đủ chủ thể, số trường hợp tách nhân vật, reviewer preference.
- **Subtitle:** tỷ lệ cue có ranh giới câu tự nhiên, orphan-word cues, overflow, sync drift.
- **Crawler:** chapter coverage, precision ảnh nội dung, completeness, corruption/duplicate, success/failure reasons.
- **Pacing:** planned/estimated/actual duration, số segments, reviewer story coverage.
- **Publishing:** số marker hợp lệ, sai lệch offset, Chapters hiển thị trên player (có thể phụ thuộc kênh).
- **Performance:** TTS sec/utterance, RTF, engine load time, clips/minute, total wall time, CPU/GPU usage, storage/transfer, cost/job.

---

## 12. Out of scope / không thực hiện trong đợt này

- Nâng cấp, đồng bộ hoặc mở rộng hệ thống TTS Cache (WAV cache) qua cloud/local; giữ nguyên quy trình sinh audio và checkpoint hiện có.
- Huấn luyện từ đầu một model detector/VLM hoặc dataset lớn trước khi xác nhận Agent Vision không đủ.
- Thay VieNeu-TTS bằng voice engine khác; thay FFmpeg bằng video generation AI.
- Tự động bypass CAPTCHA, paywall, DRM hoặc các hạn chế từ website truyện.
- Tối ưu CTR bằng tuyên bố cảm tính; chỉ coi thumbnail ranking là heuristic cho đến khi có thí nghiệm thực.
- Ép video phải đúng phút mục tiêu bằng cắt nội dung, tăng tốc audio một cách tự động hoặc phá continuity.
- Tự đăng công khai trước khi có quyền sử dụng nội dung và phê duyệt của người dùng.

## 13. Definition of Done (áp dụng cho từng PR)

- [ ] Mô tả đúng baseline code và contract cũ.
- [ ] Tests pass: unit + integration + cases đặc thù của PR.
- [ ] CLI flags/documentation/Skill rules đồng bộ và có default rõ ràng.
- [ ] Compatibility: dữ liệu V3 cũ chạy được hoặc có migration xác định.
- [ ] Render cache và checkpoint invalidation đúng phạm vi.
- [ ] Có log/report phân biệt lỗi nguồn, AI decision, render engine và environment.
- [ ] Có demo/benchmark trước–sau trên cùng input; không quảng cáo speed/quality chưa đo.
- [ ] Không phụ thuộc vào manual steps bí mật; production job hoặc CLI có thể chạy lại bằng config và input đã lưu.

---

## 14. Source code references (baseline audit)

- [`.agents/skills/manga-recap/SKILL.md`](https://github.com/nxc1802/Manga-Recap-Skill/blob/main/.agents/skills/manga-recap/SKILL.md)
- [`AGENTS.md`](https://github.com/nxc1802/Manga-Recap-Skill/blob/main/AGENTS.md)
- [`src/services/crawler_service.py`](https://github.com/nxc1802/Manga-Recap-Skill/blob/main/src/services/crawler_service.py)
- [`src/cli/crawl_cmd.py`](https://github.com/nxc1802/Manga-Recap-Skill/blob/main/src/cli/crawl_cmd.py)
- [`src/core/streaming_segmenter.py`](https://github.com/nxc1802/Manga-Recap-Skill/blob/main/src/core/streaming_segmenter.py)
- [`src/core/schemas.py`](https://github.com/nxc1802/Manga-Recap-Skill/blob/main/src/core/schemas.py)
- [`src/render/camera.py`](https://github.com/nxc1802/Manga-Recap-Skill/blob/main/src/render/camera.py)
- [`src/render/video_engine.py`](https://github.com/nxc1802/Manga-Recap-Skill/blob/main/src/render/video_engine.py)
- [`src/render/subtitle.py`](https://github.com/nxc1802/Manga-Recap-Skill/blob/main/src/render/subtitle.py)
- [`src/cli/tts_cmd.py`](https://github.com/nxc1802/Manga-Recap-Skill/blob/main/src/cli/tts_cmd.py)
- [`src/tts/engine.py`](https://github.com/nxc1802/Manga-Recap-Skill/blob/main/src/tts/engine.py)
- [`src/cli/render_cmd.py`](https://github.com/nxc1802/Manga-Recap-Skill/blob/main/src/cli/render_cmd.py)
- [`src/cli/batch_cmd.py`](https://github.com/nxc1802/Manga-Recap-Skill/blob/main/src/cli/batch_cmd.py)
- [`src/cli/compile_cmd.py`](https://github.com/nxc1802/Manga-Recap-Skill/blob/main/src/cli/compile_cmd.py)
- [`src/cli/youtube_cmd.py`](https://github.com/nxc1802/Manga-Recap-Skill/blob/main/src/cli/youtube_cmd.py)
- [`.agents/skills/manga-recap/rules/vision_ocr.md`](https://github.com/nxc1802/Manga-Recap-Skill/blob/main/.agents/skills/manga-recap/rules/vision_ocr.md)
- [`.agents/skills/manga-recap/rules/narrative_recap.md`](https://github.com/nxc1802/Manga-Recap-Skill/blob/main/.agents/skills/manga-recap/rules/narrative_recap.md)
- [`.agents/skills/manga-recap/rules/youtube_publishing.md`](https://github.com/nxc1802/Manga-Recap-Skill/blob/main/.agents/skills/manga-recap/rules/youtube_publishing.md)

> **Evidence status:** các mô tả “hiện trạng” dựa trên lần đọc mã nguồn repo V3 trong cuộc hội thoại. Các phần schema, algorithm, CLI flags và ngưỡng nghiệm thu ở đây đều là **đề xuất**, chưa implement hay đo thực nghiệm. Trước khi code, xác nhận repo vẫn ở baseline phù hợp và bổ sung source fixture/video đầu ra để kiểm chứng nguyên nhân theo từng ca.
