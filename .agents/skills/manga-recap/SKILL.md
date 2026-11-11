---
name: manga-recap
version: "3.0.0"
tags:
  - manga
  - recap
  - video
description: >
  Autonomous end-to-end Manga Recap workflow orchestrating visual panel OCR,
  character consistency, Vietnamese narrative recap scripting, local VieNeu-TTS
  synthesis, and cinematic video rendering via offline CLI tools. Use whenever
  a user requests creating, scripting, narrating, or rendering a manga/webtoon
  recap video from online URLs or local panel images.
metadata:
  version: "3.0.0"
  schema_version: "3.0.0"
  architecture: decoupled-agentic
  tts_engine: vieneu-v3-turbo-local
  target_language: vi
---

# Manga Recap Agentic Skill

## Overview

The `manga-recap` skill enables coding agents (OpenCode, Claude Code, Antigravity) to act as autonomous directors for high-quality Vietnamese Manga Recap videos. The agent handles multimodal vision, OCR, narrative recap scripting, and quality review gates, while driving a suite of offline, deterministic Python CLI tools for image processing, local TTS audio generation, and FFmpeg video assembly.

### Architectural Core Principles
- **Decoupled Intelligence**: All visual interpretation, character understanding, and creative scriptwriting reside in the coding agent. The core Python code contains **zero** direct calls to external LLM/VLM or Cloud TTS APIs.
- **Offline Determinism**: Media processing, audio synthesis via VieNeu-TTS v3 Turbo, video rendering, and state management run 100% locally and deterministically.
- **Strict Recoverability**: Every pipeline stage persists state atomically to JSON checkpoints (`data/projects/<slug>/chapters/<num>/chapter.json`). Any interrupted workflow can be safely resumed without duplicate work.
- **Evidence-Grounded Faithful Storytelling**: The agent examines actual panel imagery and contact sheets before scripting. It never invents plot points or character actions from filenames. All panels containing essential story progression must be retained in the video. Ambiguous panels are flagged for human verification with `[CAN_XAC_MINH: ...]`.
- **Zero Automated Quality Benchmarks**: Storytelling quality, character voice authenticity, pacing, and visual framing are evaluated exclusively through human review. The agent freely selects its OCR/VLM approach without arbitrary confidence thresholds or LLM-as-judge scoring.

## Prerequisites & System Requirements

1. **Python Environment**: Python 3.10+ with project dependencies installed (`pip install -e .` or active virtualenv in `./venv`).
2. **FFmpeg & FFprobe**: Installed and available on system `$PATH` with `libx264` and `aac` codecs (or `h264_videotoolbox` on macOS Apple Silicon, `h264_nvenc` on NVIDIA GPUs).
3. **VieNeu-TTS Model Weights**: Downloaded and cached locally for VieNeu-TTS v3 Turbo (or synthesized via Kaggle GPU with `--kaggle`).
4. **Kaggle Cloud GPU (Optional for `--kaggle` mode)**: Kaggle API token configured in `~/.kaggle/access_token` (or `KAGGLE_API_TOKEN` environment variable). Provides free Tesla T4 GPU access for extreme-speed VieNeu-TTS batched synthesis and NVIDIA NVENC 1080p video rendering.
5. **CLI Entrypoint**: `manga-recap` or `python -m src.cli.main`.

## Streamlined 5-Stage Architecture & Consolidated Review Gates

The pipeline operates across 5 streamlined operational stages:

```
[Gate 0: Series Config (project.json)] ───► [Gate 1: Voice Approval (voice_selection.json)]
                                                       │
                                                       ▼
[Stage 1: INGEST] ───► [Stage 2: AGENT] ───► [Stage 3: AUDIO] ───► [Stage 4: RENDER] ───► [Stage 5: EXPORT]
 (crawl/segment)       (script/validate)         (local TTS)        (1080p / optional Gate 2) (batch / compile)
```

- **Gate 0 (Video Config)**: Configured **ONCE** per series in `data/projects/<slug>/project.json`. All chapters automatically inherit resolution, video mode, subtitles, animation, voice, and BGM settings without repeated prompts.
- **Gate 1 (Voice Strategy)**: Approved **ONCE** per series in `data/projects/<slug>/voice_selection.json` (or `characters.json`) and inherited across all chapters.
- **Gate 2 (Preview Approval)**: **V4 series policy:** Chapter 1 preview and explicit human approval are mandatory before full-series final production. After this approval, chapters 2+ may bypass repeated previews via `--skip-preview` (still marked `human_review_status = "unreviewed"`). The legacy CLI does not enforce this new policy yet; the Agent must block an unapproved Chapter 1 full-series job.
- **Batch Mode**: Multi-chapter sequential processing via `manga-recap batch` with chapter failure isolation and single-chapter retry (`--retry-chapter`).

### Stage 0: STRICT FORM MODE — MANDATORY 10-QUESTION USER CONFIGURATION GATE

> **HARD CONSTRAINT — KHÔNG CÓ NGOẠI LỆ:** **Agent TUYỆT ĐỐI KHÔNG được tự ý áp dụng giá trị mặc định ngầm. Bắt buộc phải đưa đầy đủ toàn bộ 10 hạng mục vào danh sách câu hỏi của công cụ `ask_question` để người dùng trực tiếp chọn.**
>
> **MUST / FAIL-CLOSED:** Before performing any crawl, segmentation, scripting, TTS, rendering, upload, or cloud job submission for a NEW series, the Agent MUST invoke the interactive `ask_question` tool with ALL TEN questions below, await an explicit answer to EACH, and validate the returned selections. No omitted questions, inferred answers, automatic preselection, saved tool defaults, or silent substitution from `config.yaml`, `project.json`, CLI defaults, or historical preferences are allowed. Tool defaults in the underlying engine are implementation details, NOT user consent.
>
> If `ask_question` cannot display all ten questions in a single call, use successive `ask_question` calls until ALL ten are answered, without executing any pipeline stage in between. If the tool is unavailable, STOP automation and explain the missing requirement; do not replace this gate with guessed values. A user may explicitly select "Không", "Không upload", or "Không có BGM", which counts as an answer; an empty/unanswered field does NOT. If the answer is invalid, ask the same question again rather than guessing. Persist each explicit selection (and its source/time) as the series setup record before starting work.

**Mandatory 10-item question list (show the full item set to the user; labels are NOT preselected):**

1. **Chương & nguồn truyện:** Choose a chapter number / range and supply a source URL, OR explicitly select import of a local image folder and provide its path. Both source and scope must be supplied.
2. **Môi trường thực thi:** Choose exactly one: `Kaggle Cloud GPU (T4 x2)` or `Local Environment`. Cloud is NOT selected automatically.
3. **Giọng đọc:** Choose a VieNeu-TTS preset: `Ngọc Huyền`, `Trúc Ly`, `Hải Đăng`, `Thiền Tâm Đức`, `Thiện Minh`, `Mai Anh`, `Quang Sơn`, or another verified voice from `manga-recap tts_benchmark`. No implicit `Ngọc Huyền`.
4. **Tốc độ giọng đọc:** Choose `0.9x`, `1.0x`, `1.1x`, `1.2x`, or enter an explicitly supported numeric speed. No implicit `1.0x`.
5. **Phụ đề:** Choose `Có` (burn subtitles) or `Không` (no burn). Neither state is assumed.
6. **Animation:** Choose `Có` (motion policy: smooth, slow, top-to-bottom only, visual-content focus; no shake/text-focus) or `Không` (static). This choice MUST feed both local and Kaggle renderers.
7. **Thumbnail:** Choose `Tạo thumbnail` or `Không tạo`. When enabled, the existing later-stage thumbnail review gate STILL MUST ask the user which source panels and hook text to use; Stage 0 is NOT permission to guess these details.
8. **YouTube upload + privacy:** Choose `Không upload` or select an explicitly listed YouTube channel (from `manga-recap youtube accounts --json`) AND privacy `private` / `unlisted` / `public`. If no channel is linked, the user must choose `Không upload` or complete OAuth setup. The later Review Gate 3 still confirms the account before upload. No silent default channel or privacy.
9. **Chất lượng:** Choose `preview 720p` or `final 1080p`. For multi-chapter production, Chapter 1 ALWAYS goes through preview + editorial approval first, then final; chapters 2+ may be dispatched as one cloud production batch only after approval.
10. **Nhạc nền:** Choose `Không BGM` or `Có BGM`; if yes, provide the file/path and explicitly choose `bgm_volume` and `voice_volume`. Never guess file or gains.

**Enforcement & gate behavior:**
- An answer counts as confirmed ONLY when directly provided by the user in the current series setup interaction, not a tool's populated default. Present all ten questions as multiple-choice items, with text/path entry for fields requiring a URL, path, title, or gain.
- Do NOT infer choices from example values, README defaults, prior projects, CLI parser defaults, existing GPU hardware, or stored account defaults.
- Block ALL stages until 10/10 valid answers exist. If the user explicitly revises a choice, update the corresponding series setting and invalidate only affected downstream assets.
- Gate 0 is performed ONCE per series. Subsequent chapters inherit the *explicitly approved* stored answers; do not ask again unless configuration is missing, the user changes it, or setup provenance cannot be established.
- Preserve the existing thumbnail selection, voice approval, preview approval, and YouTube channel confirmation gates. Never mark any human approval automatically.
- Save the explicitly approved settings in `data/projects/<slug>/project.json` and a separate `series_setup_answers.json` audit record, with an exact set of question IDs `Q01`–`Q10`, user selections, timestamps and no secrets.
- Any programmatic `--approved`, `--skip-preview`, `--kaggle`, or CLI default MUST NOT bypass this user-facing series configuration gate.

> **Implementation boundary (V4):** The 10-item form is an enforceable **Agent instruction** in this branch, not yet a Python runtime validator. Existing CLI commands can still execute with internal defaults. The planned `SeriesSetupValidator`, canonical top-down animation policy, dual-T4 series job and HF archive are **not yet implemented**; see `docs/V4_Implementation_Plan.md`.

### Stage 1: Ingestion & Crawl
Fetch raw webtoon page images or import local chapter scans into project storage.
```bash
manga-recap crawl --project <project_slug> --chapter <chapter_num> --url "<chapter_url>" --json
```
- Output: `data/projects/<project_slug>/chapters/<chapter_num>/raw/` and updated `chapter.json`.
- State checkpoint: `stage: "crawl", status: "completed"`.

### Stage 2: Segmentation, Inspect & Visual Panel Review
Tear down long-strip webtoon pages into discrete story panels and produce contact sheets for agent visual panel review.
```bash
manga-recap inspect --project <project_slug> --chapter <chapter_num> --json
```
- Reads: `manifest.json`.
- Key Assets:
  - `panels/panel_*.jpg`: Segmented panels with pixel crop boundaries and aspect ratios.
  - `contact_sheets/sheet_*.jpg`: 4x4 downscaled visual grids designed for token-efficient agent visual panel review.

### Stage 3: Character Profiling & Character Identity Consistency
Before scripting any dialogue or narration:
1. Read `data/projects/<project_slug>/characters.json`. Check canonical Vietnamese names, core visual traits, and established character identity voices.
2. Read `data/projects/<project_slug>/story_summary.md`. Review world-building rules and prior chapter events.
3. If new characters appear in the panels, register them in `characters.json` following `.agents/skills/manga-recap/rules/character_consistency.md`.

### Stage 4: Storytelling, Direct Dialogue & N:M Panel Mapping
Chuyển đổi hoàn toàn từ "Tóm tắt (Summarize)" sang **"Kể chuyện & Lồng thoại (Storytelling & Direct Dialogue)"** với cơ chế **N:M Panel Mapping linh hoạt**:
1. **Quy tắc N:M Panel Mapping & Ghép Cảnh Linh Hoạt**:
   - **1:1 Mapping**: 1 segment tương ứng 1 panel chính (chuẩn nhịp kể chuyện kịch tính).
   - **1:N Mapping (Multi-panel Segment)**: 1 segment tham chiếu nhiều panel trong danh sách `panel_ids` (ví dụ `["panel_002.jpg", "panel_003.jpg", "panel_004.jpg"]`) để thể hiện các đòn thế võ thuật liên hoàn, phản ứng đối thoại nhanh, hoặc các khung tranh bị cắt rãnh (gutter split).
   - **N:1 Mapping (Tái sử dụng Panel)**: Nhiều segment liên tiếp có thể tái sử dụng cùng 1 panel (`panel_ids: ["panel_001.jpg"]`) khi cảnh đối thoại kéo dài, khi nhân vật độc thoại nội tâm sâu sắc, hoặc khi giữ khung cảnh đại cục.
   - **Lược bỏ panel thứ yếu (Omit Non-Essential Panels)**: Các panel trong `manifest.json` thuần túy là phong cảnh tĩnh lặp lại, khoảng trắng vô nghĩa, hoặc trang bìa/quảng cáo có thể được lược bỏ khỏi `script.json` mà không gây lỗi validation.
   - **MỆNH LỆNH BẮT BUỘC — Giữ trọn panel mang thông tin cốt lõi (Essential Story Panel Retention)**: Tất cả panel chứa thông tin quan trọng (tình tiết cốt truyện chính, lời thoại then chốt, biểu cảm mang tính bước ngoặt, đòn đánh hạ màn) **BẮT BUỘC phải được giữ lại và xuất hiện trong video**. Agent tuyệt đối không được bỏ qua các panel quan trọng này.
2. **Kể chuyện trung thực (Faithful Storytelling) & Tự chủ nhận thức**:
   - Agent phải trung thành với nguyên tác: **tuyệt đối không bịa đặt** tình tiết, cảnh giới, tên chiêu thức hay diễn biến không có trong tranh.
   - Agent tự do lựa chọn phương thức đọc/OCR/VLM thích hợp nhất (trực quan trên contact sheet, phóng to panel crop, phân tích văn cảnh) mà không cần tạo hay dựa vào các thang điểm confidence/accuracy tự động hoặc LLM-as-judge.
   - Khi gặp bong bóng thoại bị mờ, chữ bị cắt góc hoặc tình huống chưa rõ ràng: tuân thủ **Uncertainty Protocol**, đặt `status: "needs_review"` và gắn cờ `[CAN_XAC_MINH: <lý_do_chi_tiết>]` để người duyệt kiểm tra.
3. **Cấu trúc 4 thành phần khuyến nghị cho kịch bản hấp dẫn**:
   - `[Bối cảnh / Trạng ngữ]`: Thời gian, không gian, địa điểm hoặc biến chuyển ngoại cảnh.
   - `[Chủ thể & Thần sắc]`: Miêu tả nhân vật, trang phục, ánh mắt, khí chất.
   - `[Hành động & Đòn thế]`: Động tác thể chất, biến chuyển khí tức, cảm xúc nội tâm.
   - `[Lời thoại trực tiếp]`: Đọc nguyên văn lời nói trong bóng thoại (dịch chuẩn tiếng Việt, xưng hô tôn ti trật tự).
4. **Độ dài và nhịp thở khuyến nghị (Pacing Guidance)**: Khoảng **50–250 ký tự** cho mỗi segment (khoảng 3–12 giây phát âm, lưu hình 4–8s mỗi visual beat). Đây là gợi ý nhịp thở tự nhiên cho người xem và TTS, **không phải là điều kiện validate lỗi cứng**.
5. Checkpoint results incrementally into `script.json` after scripting.
6. Strictly apply rules from:
   - `.agents/skills/manga-recap/rules/narrative_recap.md`
   - `.agents/skills/manga-recap/rules/vision_ocr.md`
   - `.agents/skills/manga-recap/rules/character_consistency.md`

### Stage 5: Validate Script & Metadata
Validate `script.json` against enterprise Pydantic schemas before running audio synthesis:
```bash
manga-recap validate --project <project_slug> --chapter <chapter_num> --json
```
- Confirms: all sequence IDs are unique, referenced panel IDs exist in manifest, and focus boxes lie within normalized `[0.0, 1.0]` coordinates.

### Stage 6: VieNeu-TTS Audio Generation (Local or Cloud GPU)
Synthesize 48 kHz uncompressed WAV utterances using the VieNeu-TTS v3 Turbo engine:
```bash
# Option A: Local synthesis (CPU or local Apple Silicon / CUDA)
manga-recap tts --project <project_slug> --chapter <chapter_num> [--force] --json

# Option B: Cloud GPU acceleration (Kaggle Tesla T4, batching 32 utterances/step)
manga-recap tts --project <project_slug> --chapter <chapter_num> --kaggle --json
```
- Generated utterances are written directly to `audio/clips/audio_<chapter>_<seq_id>.wav` via crash-safe atomic replacement, with exact measured waveform durations and actual SHA-256 file checksums recorded into `script.json`.
- Cloud mode (`--kaggle`): Automatically packages script and assets, offloads batched inference to Kaggle GPU Tensor Cores (~0.32s/segment), and pulls back all `.wav` audio clips into local storage seamlessly.
- Resumability: By default, re-running TTS resumes without re-synthesizing segments that already possess valid, verified audio matching the current voice. Pass `--force` to re-synthesize all chapter segments.

### Stage 7: Human Review Gate 1 (Voice Strategy)
Xác nhận chiến lược lồng tiếng duy nhất: **`single_narrator`** (Một giọng đọc thống nhất — giọng đã được người dùng chọn rõ tại Q03), tùy chọn hỗ trợ thẻ cảm xúc `emotion_aware` (`[cười]`, `[thở dài]`, `[hắng giọng]`):
1. Direct the user to listen to voice samples or review `data/benchmarks/vieneu_v3_turbo/review/index.html`.
2. Record human approval into `data/projects/<project_slug>/voice_selection.json` once per series.

### Stage 8: Cinematic Video Render (Local or Cloud NVENC)
Render preview or final video clips with dynamic FFmpeg filterchains:
```bash
# Preview render (for manual inspection)
manga-recap render --project <project_slug> --chapter <chapter_num> --quality preview --json

# Final 1080p render (ONLY after mandatory Chapter 1 preview approval for V4 series)
manga-recap render --project <project_slug> --chapter <chapter_num> --quality final --skip-preview --json

# Cloud GPU render (ONLY after mandatory Chapter 1 preview approval for V4 series)
manga-recap render --project <project_slug> --chapter <chapter_num> --quality final --skip-preview --kaggle --json
```
- Applies chosen camera motion (`static`, `pan_left`, `pan_right`, `pan_up`, `pan_down`, `zoom_in`, `zoom_out`, `focus_crop`, `vertical_scroll`).
- Times panel duration exactly to synthesized audio duration.
- Generates aligned `.srt` subtitles and ducks background music during speech.
- Cloud mode (`--kaggle`): Composites frames in parallel (8 workers), merges audio, and renders Full HD 1080p using NVIDIA NVENC hardware encoder in a single-pass concat demuxer (~76s for an 8-minute video). Automatically syncs `final.mp4`, `chapter_<num>.srt`, and `cloud_telemetry.json` back to local storage.

### Stage 9: Human Review Gate 2 (Video Preview Verification)
Verify output video stream using `ffprobe` and human inspection at Review Gate 2. **Chapter 1 preview + explicit approval are mandatory for V4 series; later chapters may bypass repeated manual preview** (the existing CLI does not yet enforce the Chapter 1 exception):
1. **Interactive Inspection**: Check audio-visual synchronization, Ken Burns motion, subtitle readability, and BGM ducking on `video/preview.mp4` or `video/final.mp4`.
2. **Review Status Semantics**:
   - If preview is skipped (`--skip-preview` or batch mode): `technical_status = "completed"` is recorded, but `human_review_status` is strictly recorded as **`unreviewed`**. It is **NEVER** automatically marked as `approved`.
   - If human reviewer accepts: set `human_review_status = "approved"` (recorded in `chapter.json` or `preview_approval.json`).
   - If human reviewer requests edits: set `human_review_status = "changes_requested"` with specific notes citing `seq_id` and/or `panel_ids`.
3. Ensure no dialogue or character faces are cut off by focus_box crops.

### Stage 9b: Minimal Human Review Pack & Feedback Loop
To provide the human reviewer with a clear, transparent, and actionable inspection tool without building a complex frontend UI, generate a static **Human Review Pack**:
```bash
manga-recap review-pack --project <project_slug> --chapter <chapter_num> [--format md|html|json] --json
```
- **Review Pack Components**:
  1. **Visual Overview**: Direct links/paths to 4x4 contact sheets (`contact_sheets/sheet_*.jpg`).
  2. **Script Alignment Table**: Complete map of `seq_id` $\rightarrow$ `panel_ids` $\rightarrow$ `character` $\rightarrow$ `narrator_text` $\rightarrow$ `camera_motion` $\rightarrow$ audio clip status.
  3. **Omitted Panels Log**: Full catalogue of panels in `manifest.json` not referenced in `script.json` with classification and omission reason, allowing the reviewer to instantly verify that **no essential story panels were omitted**.
  4. **Uncertainty & Needs Review Flags**: Highlight of any segment containing `[CAN_XAC_MINH: ...]` for targeted editorial decision.
  5. **Preview Media Links**: Direct file paths to synthesized `.wav` clips, `.srt` subtitles, and `.mp4` video.
- **Human Feedback & Incremental Invalidation Loop**:
  - The reviewer submits targeted feedback (e.g., *"Segment 002: sửa lại câu thoại của Thạch đại phu"*, *"Panel panel_005.jpg chứa tình tiết quan trọng, cần thêm vào kịch bản"*).
  - The agent edits only the affected segments in `script.json`.
  - Re-running `manga-recap tts` and `manga-recap render` automatically utilizes audio clip caching: unchanged segments are preserved instantly, and only modified segments are re-synthesized.

### Stage 10: Multi-Chapter Compilation & Batch Mode
Process multiple chapters in unattended batch mode or assemble into a master feature recap:
```bash
# 1. Legacy V3 sequential multi-chapter batch mode (Local; run ONLY after Chapter 1 preview approval)
manga-recap batch --project <project_slug> --chapters 1-5 --compile --json

# 2. Legacy V3 cloud batch mode (chapter-by-chapter; NOT the planned V4 single-session dual-T4 job)
manga-recap batch --project <project_slug> --chapters 1-5 --kaggle --compile --json

# 3. Single-chapter retry without restarting completed chapters
manga-recap batch --project <project_slug> --retry-chapter 2 --kaggle --json

# 4. Assemble multiple completed chapters into a continuous full-length recap video
manga-recap compile --project <project_slug> --chapters 1,2 --output "data/projects/<project_slug>/video/full_recap.mp4" --json
```

### Stage 10b: Cloud GPU Acceleration & Extreme-Speed Pipeline (`--kaggle` Mode)
When running on large chapters or resource-constrained local machines, offload heavy computation to Kaggle Cloud GPU:
```bash
# Check Kaggle API authentication & quota
manga-recap cloud info --json

# Run end-to-end cloud production job for a single chapter
manga-recap cloud run --project <project_slug> --chapter <chapter_num> --json
```
**Extreme Speed Optimizations Implemented on Cloud GPU**:
1. **VieNeu-TTS Batched Inference**: Utterances are grouped into batches (`infer_batch(..., batch_size=32)`) on Tensor Cores, cutting TTS time from ~75s down to **~20s** for 60+ lines.
2. **Parallel Frame Compositing**: 8-worker thread pool composites 1080p frames with blurred background wings and centered panels in **~6.4s**.
3. **Single-Pass Hardware Video Encoding**: Replaces multi-process rendering with an instant wave audio merge and a single FFmpeg concat demuxer pass powered by NVIDIA Hardware NVENC (`h264_nvenc -preset p4 -tune ll`), encoding an 8-minute video in **~76s** (overall render: **~82s**, 4.7x faster than real-time playback).
4. **Auto-Packaging & Local Sync**: CLI packages local assets into a lightweight tarball, uploads to Kaggle Dataset `cuongnguyen1802/manga-recap-payload`, polls execution, and downloads results directly into local paths (`video/final.mp4`, `subtitles/*.srt`, `audio/clips/`, `cloud_telemetry.json`). Checkpoint status in `chapter.json` is updated atomically to `completed`.
5. **Zero Cloud API / Zero Database Rule Compliance**: Reasoning, scriptwriting, and OCR remain 100% native to the Agent. VieNeu-TTS runs offline within the Kaggle container. State persistence is strictly local JSON.

### Stage 10c: Thumbnail Composition & Interactive Selection Gate
Nếu và chỉ nếu người dùng chọn **Tạo thumbnail** ở Q07, thực hiện quy trình thumbnail sau trong lúc chờ TTS & Render. Nếu Q07 chọn Không tạo, không tạo hay hỏi thêm về thumbnail:
1. **Conditional Interactive Selection Gate**: Khi Q07 = Tạo thumbnail, Agent **BẮT BUỘC** hỏi người dùng 2 bước trước khi tạo thumbnail:
   - **Chọn ảnh**: Quét và đề xuất danh sách 5–8 panel đẹp nhất qua CLI `manga-recap thumbnail --candidates`, hỏi người dùng chọn ảnh Trái - Giữa - Phải (hoặc 1 ảnh Hero chính).
   - **In chữ & Typography**: Hỏi người dùng có in chữ không? Nếu có thì in tiêu đề gì (gợi ý 2–3 hook 3–5 từ), màu sắc và font chữ ra sao.
2. **Deterministic CLI Execution**:
```bash
# 1. Quét ứng viên kịch tính từ chapter
manga-recap thumbnail --project <slug> --chapter <num> --candidates --json

# 2. Tạo thumbnail theo lựa chọn của người dùng (Bố cục 3 ảnh hoặc 1 ảnh)
manga-recap thumbnail --project <slug> --chapter <num> \
  --left panel_037 --center panel_026 --right panel_008 \
  --title "XUYÊN KHÔNG VÀO THẾ GIỚI ĐIÊN LOẠN" \
  --subtitle "NHÂN VẬT PHỤ SINH TỒN THẾ NÀO?!" \
  --color-preset yellow_black --json

# 3. Tạo thumbnail không chữ (pure artwork)
manga-recap thumbnail --project <slug> --chapter <num> --center panel_026 --no-text --json

# 4. Tạo và tự động cập nhật ngay lên video YouTube đã upload
manga-recap thumbnail --project <slug> --chapter <num> --center panel_026 --title "..." --upload --json
```

### Stage 11: YouTube SEO Packaging & Multi-Account Publishing
Generate metadata (SEO description, click-worthy titles, timestamps, hashtags) and publish directly to the selected YouTube channel:
```bash
# 1. Manage and inspect linked YouTube channels
manga-recap youtube accounts --json

# 2. Generate SEO packaging and timestamps metadata
manga-recap youtube metadata --project <slug> --chapter <num> --json

# 3. Upload video to YouTube (Hỗ trợ 2 chế độ linh hoạt):
# Mode 1: Chỉ định trực tiếp / Headless Automation Mode
manga-recap youtube upload --project <slug> --chapter <num> --account <channel_alias_or_title> --privacy [private|unlisted|public] [--thumbnail <path>] --json

# Mode 2: Chọn kênh tương tác trên Terminal (Interactive Menu Mode)
manga-recap youtube upload --project <slug> --chapter <num> -i
# (Hoặc gõ không truyền --account trên Terminal, CLI sẽ hiển thị menu số [1], [2], [3]... để người dùng bấm chọn)
```

> [!IMPORTANT]
> **Mandatory Agent Channel Selection Gate**:
> Khi chuẩn bị upload video lên YouTube, Agent **BẮT BUỘC** phải:
> 1. Quét danh sách kênh bằng `manga-recap youtube accounts --json`.
> 2. Sử dụng công cụ `ask_question` để hỏi người dùng: *"Bạn muốn upload video lên kênh YouTube nào?"* kèm danh sách các kênh hiện có (Tên hiển thị, Alias, Channel ID, đánh dấu Mặc định) và tùy chọn "Không upload (Chỉ lưu file nội bộ)".
> 3. Chỉ sau khi người dùng bấm chọn kênh, Agent mới được truyền cờ `--account <kênh_đã_chọn>` vào lệnh upload. Tuyệt đối KHÔNG tự ý tải lên kênh mặc định khi chưa hỏi.

## Narrative & Cinematic Guidelines

- **5-Second Hook**: Every chapter recap starts with a dramatic 5-second hook (no generic greetings).
- **Frame Centering & Shape Protocol (Chống Phóng To & Tránh Chat Box)**:
  - **KHÔNG zoom vào bong bóng thoại (Speech Bubbles)**: Bong bóng thoại đã được lồng tiếng và phụ đề hiển thị. Tuyệt đối không chọn tâm frame hoặc crop tập trung vào hộp thoại rỗng.
  - **Tâm Frame bám nhân vật (Subject-Centric)**: Luôn đặt tâm frame (`focus_box` hoặc `fy`) vào vị trí ánh mắt, khuôn mặt, hoặc trọng tâm hành động võ thuật của nhân vật. Với panel thoại ở trên, hướng `fy` xuống phía dưới (`0.50 - 0.70`).
  - **Giới hạn tỷ lệ Crop an toàn**: `scale_ratio` tối thiểu $\ge 0.45$ (không crop nhỏ hơn 45% kích thước panel gốc).
  - **Kiểm soát độ phóng to (Ken Burns)**: Hệ số zoom tối đa được khống chế ở $1.15\times$ (thay vì $1.25\times$) để chuyển động nhẹ nhàng, không gây vỡ nét ảnh.
  - **Xử lý Panel dọc (Webtoon Strips $H > 2W$)**: Sử dụng chế độ `static` (nền mờ điện ảnh 16:9 bảo toàn trọn vẹn nét vẽ) hoặc `vertical_scroll` với tốc độ chậm, tránh cắt cụt nhân vật.
- **Camera Motion Modes**: Assign cinematic camera motion modes per segment: `static`, `pan_left`, `pan_right`, `pan_up`, `pan_down`, `zoom_in`, `zoom_out`, `focus_crop`, `vertical_scroll`.
- **Focus Crop**: Use `focus_box` coordinates `[x_min, y_min, x_max, y_max]` to zoom in on character faces or key items.
- **Audible Cues**: Use approved cues `[cười]` and `[thở dài]` to modulate VieNeu-TTS expression.

## CLI Reference & Exit Code Standards

All commands accept `--json` for structured output and return standardized POSIX exit codes:

| Exit Code | Meaning | Agent Action |
|-----------|---------|--------------|
| `0` | Success | Continue to next pipeline step. |
| `1` | Generic / Runtime Error | Inspect `error.message` in JSON, check logs, and retry. |
| `2` | Validation Error | Script or configuration schema violation. Fix fields in JSON. |
| `3` | IO / Storage Error | File not found, permission denied, or lock timeout. Check paths. |
| `4` | Dependency Error | FFmpeg or VieNeu model missing. Verify environment setup. |

## Sub-rule References
- **Narrative Writing & Emotion**: `.agents/skills/manga-recap/rules/narrative_recap.md`
- **Vision, Contact Sheets & OCR**: `.agents/skills/manga-recap/rules/vision_ocr.md`
- **Character Profiles & Continuity**: `.agents/skills/manga-recap/rules/character_consistency.md`
- **Review Gates & Uncertainty**: `.agents/skills/manga-recap/rules/human_review_gates.md`
- **YouTube SEO & Publishing**: `.agents/skills/manga-recap/rules/youtube_publishing.md`
