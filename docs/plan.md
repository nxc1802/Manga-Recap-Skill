# MASTER PLAN — MANGA RECAP AGENTIC SKILL V3

**Repository:** https://github.com/nxc1802/Manga-Recap-Skill  
**TTS Engine:** https://github.com/pnnbao97/VieNeu-TTS  
**Target:** VieNeu-TTS v3 Turbo, chạy local  
**Orchestrator:** OpenCode hoặc Coding Agent hỗ trợ Agent Skills  
**Storage:** Local File System + JSON  
**Test case:** Warrior Grandpa and His Supreme Granddaughter — Episode 2

---

## I. Mục tiêu và nguyên tắc kiến trúc

Biến dự án thành một Agentic Skill cho phép Coding Agent tự thực hiện quy trình phân tích truyện tranh, viết kịch bản tiếng Việt, tạo thuyết minh, dựng video và kiểm tra chất lượng.

### Quyết định bắt buộc

1. **Agent-native:** OpenCode hoặc Coding Agent bên ngoài là bộ điều phối duy nhất. Không xây custom Agent, agent loop, model router hoặc LLM orchestrator trong repository.
2. **Không LLM/VLM API:** Python không gọi OpenRouter, Gemini, OpenAI hoặc các dịch vụ inference model khác. Coding Agent sử dụng khả năng reasoning/vision của chính model đang được cấu hình.
3. **Local Storage:** Xóa Supabase và toàn bộ DB coupling. Lưu trạng thái, metadata, kịch bản và memory bằng JSON/Markdown.
4. **Local TTS:** Chỉ sử dụng VieNeu-TTS v3 Turbo. Không giữ Edge-TTS, OpenAI TTS hoặc Gemini TTS làm fallback.
5. **Deterministic Tooling:** Python chỉ xử lý crawler, segmentation, validation, audio, video, storage và các phép đo.
6. **Human Evaluation:** Không tự động chốt giọng đọc hoặc chiến lược biểu cảm trước khi người dùng nghe và lựa chọn output.
7. **Recoverability:** Mọi bước có checkpoint, chạy lại an toàn, có khả năng tiếp tục sau khi lỗi hoặc context Agent bị reset.
8. **No destructive migration:** Giữ một bản V1 để đối chiếu; chỉ xóa code legacy khi phương án mới đã vượt qua kiểm thử.

Không triển khai YouTube uploader trong core V3. Output cuối cùng là video/audio và báo cáo local. Chức năng xuất bản có thể là một tích hợp riêng sau này.

---

## II. Kiến trúc đích

```text
User Request
    │
    ▼
OpenCode / Coding Agent
    │
    ├── AGENTS.md
    └── manga-recap/SKILL.md
              │
              ▼
      Agentic Workflow
              │
      ├── Crawl / Import
      ├── Segment / Inspect
      ├── Vision / OCR
      ├── Character Memory
      ├── Script Generation
      ├── Script Validation
      ├── VieNeu Local TTS
      ├── Audio Evaluation
      ├── Video Rendering
      └── Final Quality Check
              │
              ▼
        Local Artifacts
     JSON / WAV / SRT / MP4
```

### Phân chia trách nhiệm

**Coding Agent:** Phân tích hình ảnh, OCR, xác định cốt truyện, nhận diện nhân vật, viết lời thuyết minh, quyết định ngữ cảnh và cảm xúc, chọn voice profile đã được duyệt, chọn chuyển động camera và điều phối CLI.

**Python CLI:** Tải và chuẩn hóa ảnh từ nguồn được phép sử dụng, tách panel, tạo contact sheet, kiểm tra schema, lưu trạng thái, chạy VieNeu, dựng video, đo hiệu suất và xác thực output.

**Người dùng:** Duyệt chất lượng giọng đọc, so sánh các cách thể hiện cảm xúc, chốt chiến lược narration/character voices và quyết định bản video cuối.

---

## III. Cấu trúc repository mục tiêu

```text
Manga-Recap-Skill/
├── AGENTS.md
├── README.md
├── config.yaml
├── pyproject.toml
├── .gitignore
│
├── .agents/skills/manga-recap/
│   ├── SKILL.md
│   ├── references/
│   │   ├── workflow.md
│   │   ├── vision.md
│   │   ├── scripting.md
│   │   ├── tts-guidelines.md
│   │   ├── video-guidelines.md
│   │   └── quality-gates.md
│   └── schemas/
│       ├── chapter.schema.json
│       └── script.schema.json
│
├── src/
│   ├── cli/
│   │   ├── crawl.py
│   │   ├── inspect.py
│   │   ├── validate.py
│   │   ├── tts.py
│   │   ├── tts_benchmark.py
│   │   ├── render.py
│   │   ├── compile.py
│   │   └── status.py
│   │
│   ├── core/
│   │   ├── config.py
│   │   ├── storage.py
│   │   ├── manifest.py
│   │   ├── schemas.py
│   │   └── cache.py
│   │
│   ├── services/
│   │   ├── crawler.py
│   │   └── segmentation.py
│   │
│   ├── tts/
│   │   ├── vieneu_engine.py
│   │   ├── voice_profiles.py
│   │   ├── text_processing.py
│   │   ├── benchmark.py
│   │   └── evaluation.py
│   │
│   └── render/
│       ├── video_engine.py
│       ├── motion.py
│       ├── audio.py
│       └── subtitles.py
│
├── tests/
│   ├── unit/
│   ├── integration/
│   └── e2e/
│
├── benchmarks/
│   ├── tts_corpus.json
│   └── quality_rubric.json
│
└── data/                   # Git ignored
    ├── projects/
    └── benchmarks/
```

TTS model weights, Hugging Face cache, audio samples cá nhân và video output không được commit vào Git.

---

## IV. Implementation Roadmap

### PR-00 — Baseline, Audit và Migration Safety

**Mục tiêu:** Tạo đường cơ sở an toàn trước khi thay đổi kiến trúc.

Công việc:

- Snapshot nhánh `main` hiện tại và tạo tag bảo toàn V1.
- Liệt kê toàn bộ module có Supabase, Cloud TTS, LLM/VLM API hoặc các hàm phụ thuộc chúng.
- Xác định các hàm có thể tái sử dụng: stitch/segment, crawler, FFmpeg, subtitle, video compilation.
- Kiểm tra các unit tests hiện tại và ghi lại những test thất bại.
- Định nghĩa contracts cho chapter, panel, utterance và output.
- Tạo migration checklist; không xóa dữ liệu runtime hiện có.

**Done:** Có baseline, danh sách thay đổi và phương án rollback.

### PR-01 — Local JSON Storage Foundation

**Mục tiêu:** Thay toàn bộ Supabase bằng hệ thống lưu trữ nội bộ.

Công việc:

- Xây dựng `StorageManager` và `ManifestManager`.
- Tạo `project.json`, `chapter.json`, `script.json`, `characters.json`, `story_summary.md`.
- Sử dụng ID ổn định cho project/chapter/panel/utterance.
- Implement atomic write, schema version, file lock và relative paths.
- Thêm các trạng thái `pending`, `running`, `completed`, `failed`, `needs_review`.
- Thêm checkpoint và content fingerprint để resume/cache invalidation.
- Chuyển dữ liệu cũ nếu có, không yêu cầu khởi tạo DB.

**Done:** Toàn bộ metadata và workflow hoạt động không cần Supabase.

### PR-02 — CLI Refactor và Critical Bug Fixes

**Mục tiêu:** Tách pipeline thành các công cụ độc lập, dễ gọi từ Coding Agent.

Công việc:

- Chuẩn hóa CLI `crawl`, `inspect`, `validate`, `tts`, `render`, `compile`, `status`.
- Mỗi CLI hỗ trợ `--help`, exit code và báo cáo JSON.
- Sửa lời gọi `cleanup_temp_files()` không tồn tại.
- Kiểm tra return code và output thực tế của mọi lệnh FFmpeg.
- Sửa suffix/versioning output.
- Không bỏ qua audio/panel/clip bị lỗi mà không ghi nhận.
- Đảm bảo file tạm được dọn an toàn sau khi output hợp lệ.
- Thêm test về đường dẫn, thứ tự panel, tên chapter và khả năng chạy lại.

**Done:** CLI độc lập hoạt động ổn định, báo lỗi rõ ràng và không còn lỗi P0.

### PR-03 — Agentic Skill Integration

**Mục tiêu:** Chuyển việc điều phối hoàn toàn cho OpenCode.

Công việc:

- Tạo `.agents/skills/manga-recap/SKILL.md` với frontmatter chuẩn.
- Tạo `AGENTS.md` khai báo các nguyên tắc repo, cách dùng Skill và quy tắc kiểm tra.
- Tách tài liệu vision, scripting, TTS và video thành các reference theo nhu cầu.
- Bảo tồn logic xử lý 5 panel với overlap 2 panel.
- Agent đọc `characters.json` và `story_summary.md` trước mỗi batch.
- Agent lưu `script.json` sau mỗi batch, checkpoint theo stage.
- Agent thực hiện review dựa trên ảnh, không viết nội dung từ filename hoặc phỏng đoán.
- Định nghĩa hành vi khi không đủ thông tin: đánh dấu review, không bịa.
- Test khả năng tiếp tục trong phiên OpenCode mới.

**Done:** Agent có thể tiếp nhận một yêu cầu Manga Recap, tự sử dụng Skill và các CLI mà không cần Agent Orchestrator riêng.

### PR-04 — Crawler & Segmentation V2

**Mục tiêu:** Tăng độ chính xác và giảm RAM với webtoon dài.

Công việc:

- Cải thiện Webtoons URL parser và phát hiện ảnh trong cấu trúc trang.
- Hỗ trợ import ảnh local từ nguồn hợp lệ.
- Tách chức năng nhận diện nguồn, tải ảnh và segmentation.
- Không ghép toàn bộ chapter thành một canvas khổng lồ trong RAM.
- Phân tích gutter trên ảnh giảm độ phân giải hoặc từng trang, crop từ ảnh gốc.
- Giữ thông tin ảnh nguồn và tọa độ crop.
- Tạo contact sheet để Agent review.
- Hỗ trợ trạng thái `active`, `context_only`, `skip`, `needs_review`.
- Đảm bảo thứ tự panel, không trùng và không mất khung hình.
- Không vượt cơ chế bảo vệ truy cập, DRM hoặc giới hạn cấp phép của website.

**Done:** Segmentation có thể chạy trên chapter dài, tạo manifest đầy đủ và cung cấp đủ thông tin để Agent sửa lỗi phân cảnh.

### PR-05 — VieNeu-TTS v3 Turbo Local Integration

**Mục tiêu:** Tích hợp VieNeu-TTS bằng Python SDK local, không thông qua HTTP API.

**Nguồn chính:** https://github.com/pnnbao97/VieNeu-TTS

#### 5.1. Installation & Runtime

- Tạo môi trường TTS riêng hoặc nhóm dependency tùy chọn để tránh xung đột package.
- Pin SDK version và model revision sau khi kiểm chứng.
- Hỗ trợ CPU ONNX và NVIDIA GPU PyTorch theo hướng dẫn chính thức.
- Tải/cache model khi setup; hỗ trợ chạy offline sau khi tải đủ assets.
- Chỉ load model một lần trong mỗi tiến trình TTS.
- Kiểm tra backend, VRAM/RAM, output sample rate và thời gian warmup.
- Kiểm tra giấy phép code và model weights.

#### 5.2. TTS Engine

Implement `VieNeuEngine` dùng các chức năng chính:

- `Vieneu(mode="v3turbo")`
- `list_preset_voices()`
- `infer()`
- `infer_batch()`
- `save()`
- `add_voice()` và voice cloning khi có nguồn tham chiếu được phép sử dụng.

Không triển khai tham số `style` như điều khiển cảm xúc vì v3 Turbo bỏ qua tham số này.

#### 5.3. Audio Segmentation

Thiết kế `script.json` ở cấp **utterance** thay vì gộp toàn bộ narration vào một chuỗi.

Mỗi utterance có:

- `utterance_id`, `panel_id`, `speaker_id`
- `text`, `voice_profile`, `emotion_intent`
- `emotion_cue`, `reference_voice`
- `sampling_config`, `duration_ms`, `audio_path`

Chia lời kể theo câu/cụm ý nghĩa. Không cắt giữa tên riêng, cụm ngữ nghĩa hoặc emotion cue. Giữ ngữ điệu liên tục khi ghép audio, đặc biệt ở câu nói dài.

#### 5.4. Batch & Cache

- Trên GPU, benchmark batch size 1, 4, 8, 16, 32.
- Gom các utterance có cùng voice/reference và cùng cấu hình thành batch.
- `infer_batch()` hiện sử dụng một giọng cho toàn bộ batch; muốn đa giọng phải tổ chức theo nhóm voice hoặc triển khai chiến lược khác.
- CPU sử dụng cùng API nhưng không kỳ vọng tăng tốc batch như GPU.
- Cache key bao gồm model revision, voice, reference hash, text, emotion tags và sampling parameters.
- Chỉ tạo lại audio bị ảnh hưởng khi thay đổi script hoặc cấu hình.

#### 5.5. Audio Quality

- Lưu audio gốc 48 kHz để phục vụ đánh giá.
- Đo duration chính xác từ waveform.
- Kiểm tra clipping, khoảng lặng, âm thanh bất thường và phát âm sai.
- Không tự động trim silence mạnh đến mức mất nhịp thở hoặc biểu cảm.
- Normalize loudness và convert định dạng ở giai đoạn hậu xử lý, không thay đổi bản audio gốc.
- Lưu đầy đủ metadata để tái tạo hoặc đối chiếu output.

**Done:** VieNeu chạy hoàn toàn local, sinh WAV cho từng utterance, có cache hợp lệ, kiểm soát lỗi và không gọi Cloud TTS.

### PR-06 — TTS Voice & Emotion Evaluation Lab

**Mục tiêu:** Khai thác VieNeu v3 Turbo tối đa trước khi quyết định chiến lược sử dụng thực tế.

Đây là một PR độc lập và **bắt buộc có vòng đánh giá của người dùng**.

#### 6.1. Voice Screening — 25 Presets

Trước tiên dùng `list_preset_voices()` để lấy danh sách giọng chính xác từ SDK đã cài.

Tạo một corpus ngắn có nội dung giống nhau giữa các giọng. Mỗi giọng được đánh giá qua ít nhất hai mẫu: một câu narration trung tính và một câu kể chuyện có nhịp điệu.

Ưu tiên nghe nhóm 10 giọng do VieNeu đề xuất trước:

**Adam bựa, Trúc Ly, Thiện Minh, Mai Anh, Hải Đăng, Thùy Dung, Thiền Tâm Đức, Ngọc Huyền, Quang Sơn, Ngọc Trân.**

Sau đó tiếp tục với tất cả preset còn lại, không loại giọng chỉ vì không nằm trong nhóm đề xuất.

**Output:** 50 WAV trở lên, bảng metadata và trang HTML nghe thử có thể so sánh trực tiếp.

#### 6.2. Emotion & Prosody Benchmark

Tạo corpus gốc bằng tiếng Việt, không sao chép lời thoại truyện. Bao gồm các loại câu:

| Nhóm | Mục tiêu |
|---|---|
| Neutral | Độ rõ, phát âm, tự nhiên |
| Dramatic | Nhịp kể trong cao trào |
| Suspense | Mức độ căng thẳng, ngắt nghỉ |
| Sad | Khả năng tạo sắc thái trầm/buồn |
| Excited | Độ sinh động, hứng khởi |
| Dialogue | Khả năng thể hiện lời thoại trực tiếp |
| Long-form | Độ ổn định giữa các câu dài |
| Names | Phát âm tên nhân vật, thuật ngữ |

Với mỗi giọng shortlist, thử các biến thể:

1. Plain text, không can thiệp.
2. Tối ưu dấu câu/ngắt đoạn.
3. Các cue được công bố: `[cười]`, `[thở dài]`, `[hắng giọng]`.
4. Thay đổi sampling parameters: `temperature`, `top_k`, `top_p` trong phạm vi hợp lý.
5. Thử reference voice hoặc clip giọng riêng có quyền sử dụng, nếu có.
6. Tái sinh nhiều lần để đánh giá tính ổn định.

**Lưu ý:** Cảm xúc như giận dữ, sợ hãi, buồn bã chưa được xác nhận là nhãn điều khiển trực tiếp trong SDK. Các nhóm này là mục tiêu đánh giá perceptual quality, không phải những tag chắc chắn được model hiểu.

#### 6.3. Thử ba chiến lược narration

**Strategy A — Single Narrator:** Một giọng xuyên suốt, giữ nhịp kể ổn định, đơn giản và dễ batch.

**Strategy B — Narrator + Character Voices:** Giọng kể chính riêng, một số nhân vật quan trọng có voice profile riêng.

**Strategy C — Emotion-Aware Narrator:** Một giọng chính nhưng điều chỉnh dấu câu, reference hoặc cue cho từng cảnh theo kết quả benchmark.

Có thể đánh giá thêm phương án kết hợp B+C nếu chất lượng đủ tốt.

Không tự động chốt chiến lược khi chưa được người dùng duyệt.

#### 6.4. Benchmark Performance

Đo tối thiểu:

- Audio duration và generation wall time.
- RTF = generation time / audio duration.
- Peak RAM/VRAM.
- Thời gian load model và warmup.
- Throughput theo batch size.
- Tỷ lệ generation lỗi, âm thanh im lặng hoặc sai phát âm.
- Mức ổn định khi tái chạy cùng mẫu.

Mỗi bài benchmark cần nhiều lần lặp, ghi rõ warm/cold runs và phần cứng thực tế. Không lấy benchmark của tác giả làm kết quả của dự án.

#### 6.5. Human Listening Evaluation

Tạo trang HTML local với các chức năng:

- Nghe A/B các audio trên cùng một câu.
- Hiển thị script và metadata khi cần.
- Chấm từ 1–5 cho naturalness, intelligibility, expressiveness, consistency và suitability for Manga Recap.
- Ghi chú lỗi phát âm, nhịp kể, biểu cảm hoặc artifact.
- Lưu nhận xét trong `voice_evaluation.json`.
- Xuất bảng xếp hạng theo từng tiêu chí và tổng hợp các lựa chọn.

Các output được nhóm theo giọng và dạng cảm xúc; có phiên đánh giá ẩn tên giọng để giảm thiên lệch.

**Review Gate:** Người dùng chọn voice profile và chiến lược dựa trên audio thực tế. Quyết định được lưu trong `voice_selection.json`. Chỉ sau đó mới render video chính thức.

**Done:** Có đầy đủ voice samples, emotion samples, benchmark report, giao diện nghe và kết quả duyệt của người dùng. Không cần fine-tune để hoàn thành PR này.

### PR-07 — Video Engine V2

**Mục tiêu:** Nâng video từ slideshow đơn giản thành recap có chuyển động theo nội dung.

Công việc:

- Sửa các tham số `focus_x`, `focus_y`, `crop_scale`, `pan_direction` đang chưa được dùng xuyên suốt.
- Thêm các chế độ `static`, `pan_horizontal`, `pan_vertical`, `zoom_in`, `zoom_out`, `focus_crop`.
- Với webtoon dài, implement vertical scroll theo thời gian thay vì crop cố định.
- Dùng duration thực tế của audio để xây timeline.
- Tạo subtitle theo utterance, không ước lượng đồng bộ chỉ bằng tỷ lệ ký tự.
- Có thể thêm local forced alignment nếu cần phụ đề cấp từ.
- Hỗ trợ output 720p và 1080p, 30 FPS; định dạng xuất có thể mở rộng.
- Đồng bộ BGM, đảm bảo âm nền không lấn giọng.
- Tối ưu số lần encode FFmpeg, tái sử dụng clip hợp lệ.
- Bảo vệ vùng thoại, khuôn mặt và các chi tiết quan trọng khi crop.
- Validation bằng ffprobe: resolution, FPS, duration, audio stream, video stream và subtitle.

**Done:** Các motion modes hoạt động thật, audio/video không bị lệch thời gian, không mất nội dung do crop và không có clip lỗi.

### PR-08 — End-to-End Test: Webtoons Episode 2

**Test URL:**

https://www.webtoons.com/en/action/warrior-grandpa-and-his-supreme-granddaughter/episode-2/viewer?title_no=8953&episode_no=2

**Điều kiện:** Xác minh quyền sử dụng/tạo bản chuyển thể. Việc kiểm tra URL, metadata và khả năng truy cập có thể tách riêng. Chỉ thực hiện tải, biến đổi toàn bộ ảnh và tạo video từ nội dung được phép sử dụng. Nếu chưa có quyền, sử dụng bộ ảnh được cấp phép với cấu trúc tương tự để hoàn tất E2E, đồng thời giữ URL này làm test phát hiện nguồn.

#### Step 1 — Source Validation

- Kiểm tra URL hợp lệ, đúng series và Episode 2.
- Kiểm tra quyền truy cập, khả năng nhận diện ảnh và cấu trúc trang.
- Ghi source URL, title, chapter number và source metadata.
- Không mặc định vượt qua bất kỳ giới hạn truy cập nào.

#### Step 2 — Crawl / Import & Segmentation

- Thu thập input từ nguồn được phép.
- Tạo raw image manifest.
- Chạy segmentation, xuất panel images và contact sheet.
- Agent kiểm tra thứ tự, cảnh bị gộp/cắt sai, panel chỉ mang ngữ cảnh.
- Sửa segmentation nếu cần mà không crawl lại vô ích.

#### Step 3 — Agent Vision & Scripting

- Agent xem panel theo batch 5/overlap 2.
- Đọc nội dung tiếng Anh trong ảnh, viết recap tiếng Việt.
- Đảm bảo nhất quán tên, danh tính và quan hệ nhân vật.
- Nếu cần bối cảnh Episode 1, chỉ dùng thông tin thực sự đã được cung cấp hoặc kiểm chứng.
- Viết `script.json` gồm utterances, panel mapping, speaker, emotion intent và motion.
- Agent tự review script với ảnh gốc trước khi TTS.

#### Step 4 — TTS Test & Selection

- Chạy các giọng đã shortlist trên những đoạn narration đại diện của Episode 2.
- Chọn các phân cảnh trung tính, cao trào, đối thoại và chuyển cảnh.
- So sánh một giọng, đa giọng và emotion-aware nếu phù hợp.
- Xuất audio samples cho người dùng duyệt.
- Sau khi được duyệt, lưu voice strategy để chạy toàn bộ chapter.

#### Step 5 — Full Audio & Video Rendering

- Tạo audio cho toàn bộ utterance theo cấu hình đã chốt.
- Xây timeline và phụ đề dựa trên duration thực tế.
- Tạo motion theo panel, kết xuất clip.
- Ghép clip, thêm BGM tùy chọn và xuất MP4.
- Tạo bản preview chất lượng thấp trước final render khi cần.

#### Step 6 — Quality Review

- Kiểm tra tất cả panel có trạng thái hợp lệ, không mất hoặc lặp.
- Kiểm tra danh tính nhân vật, diễn biến và chất lượng kịch bản.
- Kiểm tra audio, phát âm, biểu cảm, âm lượng và phụ đề.
- Kiểm tra motion, crop, độ rõ hình và các lỗi FFmpeg.
- Xuất báo cáo cuối và artifact paths.

**Done:** Hoàn thành một chapter recap từ input hợp lệ, có log, script, audio, subtitle, video và báo cáo QA. Nếu test bằng nội dung WEBTOON chưa được cấp phép, không xuất bản hoặc phân phối bản chuyển thể đó.

### PR-09 — README, Cleanup & Release Validation

**Mục tiêu:** Hoàn thiện trải nghiệm sử dụng, loại bỏ code thừa và chuẩn hóa dự án.

Công việc:

- Viết lại README theo kiến trúc Agent-native.
- Hướng dẫn cài đặt OpenCode, VieNeu local, Python, FFmpeg và dependencies.
- Tài liệu hóa cách chạy từ URL hoặc ảnh local.
- Hướng dẫn chạy TTS benchmark và quy trình duyệt giọng.
- Tài liệu hóa config, storage, resume và output.
- Xóa Supabase, Cloud TTS và LLM/VLM API legacy code.
- Kiểm tra `.env`, dependencies và không để secrets trong source.
- Bổ sung unit, integration và E2E tests trong CI.
- Kiểm tra workflow trên môi trường sạch.
- Tạo release notes, limitations và troubleshooting.

**Done:** Một người mới có thể cài, khởi chạy Agentic Skill, benchmark TTS và tạo output từ bộ dữ liệu hợp lệ bằng README mà không cần cấu hình dịch vụ model API trong repository.

---

## V. Cấu trúc output cho TTS Benchmark

```text
data/benchmarks/vieneu_v3_turbo/
├── environment.json
├── voice_catalog.json
├── corpus.json
│
├── samples/
│   ├── screening/
│   ├── emotion/
│   ├── prosody/
│   ├── sampling/
│   └── cloning/
│
├── metrics/
│   ├── inference_results.csv
│   ├── voice_evaluation.json
│   └── benchmark_summary.json
│
├── review/
│   ├── index.html
│   ├── ratings.json
│   └── voice_selection.json
│
└── reports/
    ├── benchmark.md
    └── recommended_strategies.md
```

Mỗi WAV có bản gốc và metadata. File đánh giá phải cho phép so sánh cùng một câu giữa nhiều giọng để tránh việc chọn giọng do nội dung script khác nhau.

Nếu thử voice cloning, chỉ sử dụng reference clips thuộc quyền sở hữu hoặc có sự cho phép của người nói. Không dùng giọng của nhân vật/người thật từ nội dung bên thứ ba khi chưa được phép.

---

## VI. Testing & Acceptance Criteria

### Automated Tests

| Nhóm | Acceptance Criteria |
|---|---|
| Storage | JSON hợp lệ, atomic write, checkpoint và resume hoạt động |
| Agent | Skill có thể được nhận diện, không phụ thuộc custom orchestrator |
| API Removal | Không có direct LLM/VLM/Cloud-TTS API trong core |
| Crawler | Kiểm tra source, thứ tự ảnh, lỗi mạng, dữ liệu không hợp lệ |
| Segmentation | Có manifest, không bỏ sót panel mà không ghi trạng thái |
| Script | Schema hợp lệ, IDs nhất quán, không thiếu utterance bắt buộc |
| VieNeu TTS | Sinh WAV 48 kHz hợp lệ, cache chính xác, không có audio rỗng |
| Voice Benchmark | Tất cả preset có output hoặc lý do thất bại được ghi nhận |
| Emotion Benchmark | Có bản đối chiếu và đánh giá; không mặc định cue luôn thành công |
| Rendering | Video/Audio/Subtitles hợp lệ, motion đúng, không cắt ảnh vô lý |
| Workflow | Resume từ các stage thất bại, không làm mất dữ liệu hợp lệ |
| E2E | Một chapter hoàn chỉnh, có video và báo cáo QA |

### Bộ kiểm thử theo quy mô

- **Smoke:** Khoảng 10 panel để kiểm tra pipeline.
- **Standard:** Khoảng 50 panel để kiểm tra workflow và chất lượng.
- **Stress:** Từ 200 panel để kiểm tra RAM, GPU, cache và thời gian xử lý.
- **Real-world:** Episode 2 hoặc bộ ảnh tương đương đã được cấp quyền, kiểm tra end-to-end.

Các mốc này là quy mô kiểm thử, không phải khẳng định số panel của Episode 2.

### Điều kiện release

- Không còn lỗi P0/P1 chưa được xử lý.
- Các unit/integration tests bắt buộc đều pass.
- Output từ TTS không có lỗi hệ thống chưa được giải quyết.
- Người dùng đã duyệt ít nhất một cấu hình voice strategy.
- Video được kiểm tra về script, đồng bộ âm thanh và khung hình.
- Khả năng resume hoạt động sau khi giả lập gián đoạn.
- README phản ánh đúng hành vi thực tế.

---

## VII. Thứ tự triển khai và điểm cần phê duyệt

| Thứ tự | PR | Phụ thuộc | Kết quả |
|---|---|---|---|
| 1 | PR-00 | — | Baseline V1 |
| 2 | PR-01 | PR-00 | Local Storage |
| 3 | PR-02 | PR-01 | CLI sạch, fix critical bugs |
| 4 | PR-03 | PR-02 | Agentic Skill hoạt động |
| 5 | PR-04 | PR-02 | Crawler & Segmentation V2 |
| 6 | PR-05 | PR-02 | VieNeu-TTS local |
| 7 | PR-06 | PR-05 | Voice/Emotion Benchmark |
| 8 | PR-07 | PR-03, PR-04, PR-05 | Video Engine V2 |
| 9 | PR-08 | PR-06, PR-07 | End-to-end Test |
| 10 | PR-09 | Các PR trước | Release-ready |

PR-04 và PR-05 có thể triển khai song song khi interfaces đã được thống nhất.

**Human Gate 1:** Sau PR-06, trình bày các audio và bảng đánh giá để người dùng chọn giọng, emotion strategy và narration strategy.

**Human Gate 2:** Sau khi render preview ở PR-08, người dùng xem và đánh giá video trước khi chốt cấu hình final.

Không tự ý triển khai fine-tuning, voice cloning diện rộng hoặc thêm một TTS model mới chỉ vì benchmark chưa như mong muốn. Trước hết phân tích lỗi và kiểm tra xem có cải thiện bằng preset, cách viết script, reference voice hay inference settings hay không.

---

## VIII. Deliverables cuối cùng

Dự án hoàn thành cần có:

1. Một Agentic Skill tương thích OpenCode và không phụ thuộc custom Agent.
2. Python tools có contracts ổn định, không sử dụng LLM/VLM/Cloud TTS API.
3. Local storage với checkpoint và resume.
4. Crawler và segmentation có thể kiểm tra, hiệu chỉnh.
5. VieNeu-TTS v3 Turbo local với nhiều preset và voice profiles.
6. Bộ benchmark thực tế về chất lượng giọng, biểu cảm, tốc độ, RAM/VRAM.
7. Giao diện nghe A/B và kết quả lựa chọn từ người dùng.
8. Video renderer hỗ trợ cinematic motion, audio alignment và subtitles.
9. End-to-end test cùng báo cáo chất lượng.
10. README hoàn chỉnh, automated tests và release-ready repository.

**Final Objective:** Người dùng cung cấp một nguồn manga hợp lệ, Coding Agent thực hiện toàn bộ quy trình bằng Skill và CLI. VieNeu-TTS xử lý giọng đọc tại máy. Người dùng có thể so sánh, chọn giọng và chiến lược biểu cảm trước khi tạo bản video cuối cùng. Hệ thống hoạt động có checkpoint, dễ kiểm tra và có thể mở rộng cho nhiều chapter mà không cần thiết kế một Agent riêng.