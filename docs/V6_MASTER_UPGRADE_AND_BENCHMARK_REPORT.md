# Báo cáo Tổng hợp Nâng cấp Kiến trúc V6, Tối ưu Hiệu năng & Kiểm thử Toàn diện

**Repository:** [`nxc1802/Manga-Recap-Skill`](https://github.com/nxc1802/Manga-Recap-Skill)  
**Mốc so sánh gốc (`main`):** `6da46997632b360e4ba9c0cfc0008832b6882352`  
**Ngày hoàn tất:** `2026-11-27`  
**Phạm vi:** Toàn bộ 3 giai đoạn nâng cấp nối tiếp kể từ `main`:
1. **V6 Architecture & Workflow Refactor** (`d79716d` → `aad3250`, 9 commits)
2. **Kaggle Dual-T4 Production Optimization, Live Verification & Batch-Size Tuning** (`a5228f9` → `2caaad2`, 7 commits + Phase 8 Live Bugfixes & Batch-Size Sweep)
3. **Gemini Decisions API (`gemini-decision-1`) 4-Sided Crop, Adaptive Resolution & Repack Optimization** (Phases D0–D6)

---

## 1. Tóm tắt Điều hành (Executive Summary)

Chuỗi nâng cấp này tái cấu trúc toàn bộ codebase từ trạng thái nguyên khối sang kiến trúc V6 tách lớp rõ ràng, đồng thời kiểm chứng và tối ưu hóa hai nút thắt chi phí/thời gian lớn nhất trong thực tế vận hành (`Kaggle Dual-T4` và `gemini-decision-1`):

| Hạng mục | Trước nâng cấp (`Baseline`) | Sau nâng cấp (`V6 Promoted Default`) | Mức cải thiện đo đạc thực tế |
|---|---|---|---|
| **Kiến trúc & CLI** | Business logic, FFmpeg, provider transport, locking/checkpoint trộn trong CLI/storage | Domain workflow tách riêng tại `src/core/workflow.py`; CLI mỏng; 4 provider boundaries (`decision`, `kaggle`, `huggingface`, `youtube`); `src/media/ffmpeg.py` | **Dễ bảo trì, 1 canonical workflow**, `sync_requirements.py --check` đồng bộ dependency |
| **Kaggle Dual-T4 `produce`** (Ch2+3, 71 segments, 167 shots, `h264_nvenc`) | `Policy A (baseline_split)`, `batch_size=12`: `210.18s` makespan, `254.36s` GPU quota | **`Policy B (dual_tts_shared_encode)`**, **`batch_size=16`**: **`179.04s`** makespan (`183.89s` ở `bs=12`), quota **`227.93s`** (`bs=12`) | **Nhanh hơn 14.8% (`1.17x`)** end-to-end (`179.04s` vs `210.18s`), tận dụng song song Dual-T4 TTS + Dual-NVENC + CPU finalize |
| **Kaggle Standalone `tts` (`bs`)** (71 câu, ~590–593s audio, warm T4) | `batch_size=12`: `30.076s` warm median | **`batch_size=32`**: **`21.596s`** warm median (`wall_rtf=0.03645`, `3.287 segs/s`, `0 OOM`) | **Nhanh hơn `1.39x`**, VRAM peak `1731 / 2624 MB` (`<19%` T4) |
| **Gemini Decision Routing (`tro-thanh-rong-nho` Held-Out, 118 panels)** | `legacy_9preset` (`high`): `313.50s`, `282,114` tokens (`145,930` image), `structural_staged`, cắt lệch thoại/mặt nhiều panel | **`4sided` adaptive (`medium`/`high` ở `H/W >= 2.2`)**: **`241.29s`**, **`206,578`** tokens (**`80,976`** image), `metadata_only` | **`-26.8%` tổng token**, **`-44.5%` image token**, **`-23.0%` wall time**, **`0/79` lỗi cắt thoại/nhân vật**, `0` re-encode ảnh khi không split/merge |
| **Kiểm thử tự động toàn Repo** | Suite cũ theo milestone | Reorganize theo domain behavior + bổ sung đầy đủ suite Provider/Cloud/Media/Decision | **`583 passed in 61.33s`** (`0` failed, `0` skipped) |

---

## 2. Trụ cột 1 — V6 Architecture, Workflow & Documentation Refactor (`d79716d..aad3250`)

Kế hoạch gốc: [`docs/REFACTOR_PLAN.md`](REFACTOR_PLAN.md). Toàn bộ 9 commit được thực hiện theo nguyên tắc giữ nguyên hành vi và hợp đồng kiểm duyệt con người (human-gated Q01–Q05, Q08–Q10, Chapter 1 Preview approval):

1. **`d79716d` — Tách Storage, Project Store, Checkpoints, Cleanup & Domain Errors:**
   - Chia nhỏ `src/core/storage.py` cồng kềnh (giảm `-947` dòng trộn lẫn) sang các module chuyên trách: [`src/core/project.py`](../src/core/project.py), [`src/core/checkpoints.py`](../src/core/checkpoints.py), [`src/core/cleanup.py`](../src/core/cleanup.py), [`src/core/release_schemas.py`](../src/core/release_schemas.py), [`src/core/errors.py`](../src/core/errors.py), [`src/core/version.py`](../src/core/version.py).
2. **`2ba8950` — Config Migration & Dependency Sync:**
   - Bổ sung [`src/compat/migrate.py`](../src/compat/migrate.py) và [`src/core/settings.py`](../src/core/settings.py) cho named setup settings (`Q01–Q10`) và cấu hình runtime tập trung; thêm [`scripts/sync_requirements.py`](../scripts/sync_requirements.py) để kiểm tra đồng bộ giữa `pyproject.toml` và `requirements.txt` (+ `.env.example`, `setup.sh`, `setup.bat`).
3. **`1860688` — Tách Domain Workflow & Làm mỏng CLI Registry:**
   - Gom toàn bộ orchestration logic khỏi `cli/*_cmd.py` vào [`src/core/workflow.py`](../src/core/workflow.py) và [`src/core/review_pack.py`](../src/core/review_pack.py); các lệnh CLI trong `src/cli/` chỉ còn parse argument, enforce execution policy và trả JSON envelope.
4. **`75c1eb5`, `dde46bc`, `49de0fb` — Cô lập 4 Provider Boundaries:**
   - [`src/providers/decision.py`](../src/providers/decision.py): tách HTTP transport / retry khỏi `src/ingest/decision.py` (kèm facade tương thích ở [`src/core/decision_engine.py`](../src/core/decision_engine.py)).
   - [`src/providers/kaggle.py`](../src/providers/kaggle.py) + [`src/cloud/bootstrap.py`](../src/cloud/bootstrap.py) + [`src/cloud/jobs.py`](../src/cloud/jobs.py): tách SDK/lifecycle khỏi `KaggleCloudService`.
   - [`src/providers/huggingface.py`](../src/providers/huggingface.py), [`src/providers/youtube.py`](../src/providers/youtube.py), [`src/youtube/publisher.py`](../src/youtube/publisher.py): tách transport/OAuth/publish verification sạch sẽ khỏi `cli/youtube_cmd.py` và `archive/hf_archive.py`.
5. **`c61fb50` — Thống nhất Boundary FFmpeg:**
   - Tạo [`src/media/ffmpeg.py`](../src/media/ffmpeg.py) làm một cửa gọi/probe FFmpeg chung cho render, audio mixer và TTS service.
6. **`14f5528` & `aad3250` — Chuẩn hóa Skill Docs & Reorganize Test Suite:**
   - Hợp nhất tài liệu vào [`.agents/skills/manga-recap/SKILL.md`](../.agents/skills/manga-recap/SKILL.md), [`.agents/skills/manga-recap/references/`](../.agents/skills/manga-recap/references/vision.md) và [`README.md`](../README.md), xóa bỏ rule/prompt trùng lặp (`-480` dòng dư thừa).
   - Tổ chức lại test theo domain behavior (`tests/core/`, `tests/ingest/`, `tests/media/`, `tests/providers/`, `tests/workflow/`, `tests/integration/`), dọn dev stress scripts sang `scripts/devtools/` và xóa module chết (`src/services/character_dictionary.py`).

---

## 3. Trụ cột 2 — Tối ưu Kaggle Dual-T4 Production & Batch-Size Tuning (`a5228f9..2caaad2` + Phase 8 Live)

Kế hoạch gốc: [`docs/KAGGLE_PRODUCTION_OPTIMIZATION_PLAN.md`](KAGGLE_PRODUCTION_OPTIMIZATION_PLAN.md).  
Báo cáo & dữ liệu thô: [`docs/verification/v6_phase8_2026-11-26/phase8_live_kaggle_report.md`](verification/v6_phase8_2026-11-26/phase8_live_kaggle_report.md).

### 3.1. Các nâng cấp kiến trúc Cloud (Phases K0–K6)
- **K0 — Telemetry & Probe (`a5228f9`):** [`src/cloud/telemetry.py`](../src/cloud/telemetry.py) + `probe_worker_hardware` ghi nhận từng phase (`tts`, `shot_prep`, `encode`, `finalize`), VRAM/GPU utilization theo từng giây, overlap time.
- **K1 — Job Lifecycle & Reliability (`93e1157`):** [`src/cloud/lifecycle.py`](../src/cloud/lifecycle.py) phân tách deadline hàng đợi/thực thi/transfer; máy trạng thái `SUBMIT_UNKNOWN`, `QUEUED`, `RUNNING`, `REMOTE_COMPLETE`, `SYNCED`, `FAILED`, `CANCELLED`; chống resubmit trùng; thêm worker heartbeat và bounded queues tránh deadlock.
- **K2 — Dual-T4 Policies A–D & Cost-Aware Dispatcher (`276a7cc`):** [`src/cloud/scheduler.py`](../src/cloud/scheduler.py) + [`src/cloud/dual_gpu_worker.py`](../src/cloud/dual_gpu_worker.py) cài đặt 4 chính sách điều phối:
  - `baseline_split` (Policy A), **`dual_tts_shared_encode` (Policy B — được promote mặc định)**, `dual_tts_then_dual_encode` (Policy C), `one_tts_dual_encode` (Policy D), cùng `UtteranceDispatcher` sắp batch theo ước lượng chi phí câu + OOM backoff.
- **K3 — Non-blocking CPU Finalizer (`1acbeb4`):** Tách bước finalize/mux SRT+audio của chapter trước gối đầu lên CPU trong lúc GPU encode chapter kế tiếp, kèm verified temp cleanup.
- **K4 — Payload Pruning & Chunking (`7775a94`):** [`src/cloud/payload.py`](../src/cloud/payload.py) loại bỏ ảnh `PanelStatus.SKIP` khỏi payload upload, [`src/cloud/chunking.py`](../src/cloud/chunking.py) chia chunk và lưu receipt phục vụ incremental resume.
- **K5 & K6 (`44e405d`, `2caaad2`):** Harness benchmark [`src/cloud/benchmark.py`](../src/cloud/benchmark.py), CLI flags `--cloud-policy` / `--chunked`, và telemetry hiển thị trên `manga-recap status`.

### 3.2. Các lỗi thực tế phát hiện & vá khi chạy Live Kaggle Dual-T4 (Phase 8)
1. **`PanelStatus.SKIP` Missing File trên Pruned Cloud Payload ([`src/render/visual_policy.py`](../src/render/visual_policy.py)):**
   - Khi chạy Policy A live trên Kaggle, pipeline dừng ở `panel_004` (`Missing or unsafe panel evidence`) do `package_payload` cố ý không đóng gói ảnh `SKIP`, nhưng `validate_narration_evidence` và `iter_segment_shots` vẫn đòi mở file `SKIP` trên đĩa. Đã vá để bỏ qua việc mở file/checksum với panel `SKIP` vắng mặt hợp lệ trong payload (vẫn giữ kiểm tra path containment dưới `panels/` và vẫn chặn nếu segment narration tham chiếu tới panel `SKIP`).
2. **Kaggle Dataset Propagation Race khi `kernels_push` ([`src/providers/kaggle.py`](../src/providers/kaggle.py), [`src/cloud/kaggle_service.py`](../src/cloud/kaggle_service.py)):**
   - Khi tạo dataset mới và push kernel ngay lập tức, Kaggle API trả transient HTTP `400/404` / `invalidDatasetSources`. Đã phân loại rõ `category="dataset_propagating"` trong `push_kernel` và retry có giới hạn (`4` lần × `6s`) ở `dispatch_chapter_job` (không làm mờ `SUBMIT_UNKNOWN`).

### 3.3. Số liệu Live Kaggle Dual-T4 & Batch-Size Tuning (`NvidiaTeslaT4` × 2)
Corpus kiểm thử cô lập trong `/tmp`: `nhan-vat-phu-trong-the-gioi-dien-loan` (Chapters `2, 3`; `37 + 34 = 71` segments; `167` shots; `~590–593s` video `1920x1080` `30fps` `h264_nvenc` + `aac 48000 Hz`).

#### Live Policy Matrix A–D (`batch_size=12`, `render_workers=2`)
| Policy | Chiến lược | `wall_makespan_sec` | Speedup vs A | `active_union_sec` | Quota Billed (`s`) | `tts` Wall/Sum (`s`) | `shot_prep` (`s`) | `encode` Wall/Sum (`s`) | `finalize` (`s`) | Peak VRAM `GPU0`/`GPU1` (`MB`) | Media `ffprobe` |
|---|---|---:|---:|---:|---:|---|---|---|---|---|---|
| **A** | `baseline_split` | 210.18s | 1.00x | 201.69s | 254.36s | 69.81 / 69.81 | 91.14 | 125.64 / 279.65 | 49.41 | 1915 / 433 | **PASS** |
| **B** | **`dual_tts_shared_encode`** | **183.89s** | **1.14x** | **175.11s** | **227.93s** | **57.45** / 112.36 | 31.81 | 90.37 / 349.90 | 51.15 | 2467 / 2139 | **PASS** |
| **C** | `dual_tts_then_dual_encode` | 238.46s | 0.88x | 210.02s | 316.38s | 82.89 / 163.45 | **31.34** | **85.67** / 339.18 | 54.84 | 433* / 433* | **PASS** |
| **D** | `one_tts_dual_encode` | 212.47s | 0.99x | 195.64s | 288.28s | 90.01 / 90.01 | 31.60 | 94.62 / 332.90 | 53.87 | 1753 / 433 | **PASS** |

#### Warm Tesla T4 `batch_size` Sweep (`4 → 32`) & End-to-End `Policy B` Tuning
1. **Standalone Raw VieNeu-TTS Sweep (`71` utterances, ABBA design, `0` OOM across all rounds):**
   - `bs=4`: `47.778s` (`0.63x`) · `bs=8`: `32.552s` (`0.92x`) · `bs=12`: `30.076s` (`1.00x`) · `bs=16`: `25.035s` (`1.20x`) · `bs=24`: `24.485s` (`1.23x`) · **`bs=32`: `21.596s` (`1.39x`, `wall_rtf=0.03645`, `3.287 segs/sec`)**.
2. **End-to-End Dual-T4 `Policy B` (`bs=12` vs `16` vs `32`):**
   - `bs=12`: makespan `183.89s`, first encode start `66.21s`, quota `227.93s`, VRAM `2467 / 2139 MB`.
   - **`bs=16` (Promoted Default cho `produce`/`render`)**: makespan **`179.04s`** (`active_union=170.83s`), **first encode start sớm nhất `63.21s`**, `tts` wall **`53.70s`**, `encode` wall **`88.21s`**, quota `245.89s`, VRAM **`2183 / 2051 MB`**.
   - `bs=32` (Promoted Default cho standalone `tts`): trong `produce`, việc dồn gần hết chapter đầu vào ít chunk quá lớn làm trễ thời điểm bắt đầu `shot_prep`/NVENC tới `75.68s` (`+12.47s`), khiến makespan tăng ngược lên `190.57s`.

---

## 4. Trụ cột 3 — Tối ưu hóa Gemini Decisions API (`gemini-decision-1`, Phases D0–D6)

Kế hoạch gốc: [`docs/GEMINI_DECISION_OPTIMIZATION_PLAN.md`](GEMINI_DECISION_OPTIMIZATION_PLAN.md).  
Báo cáo & JSON chi tiết: [`docs/verification/v6_decision_d0_2026-11-27/d5_d6_ab_benchmark_report.md`](verification/v6_decision_d0_2026-11-27/d5_d6_ab_benchmark_report.md), [`ab_benchmark_metrics.json`](verification/v6_decision_d0_2026-11-27/ab_benchmark_metrics.json).

### 4.1. Vấn đề Baseline A (`legacy_9preset`) & Những gì đã làm (D0–D6)
- **Transport Hardening ([`src/providers/decision.py`](../src/providers/decision.py)):** Bổ sung `ProviderContentBlockedError`, tái sử dụng `httpx.Client` thread-local, đồng bộ global `_cooldown_until` across threads khi dính HTTP 429 (sửa lỗi tự ngủ kép khi retry), validate `confidence`/`probabilities` hữu hạn trong `[0, 1]` (loại `NaN`, giữ `None` nếu API không trả), `extract_usage_metrics`, và tham số `image_resolution` (`high`/`medium`/`low`).
- **4-Sided Crop, Guardrails & Adaptive Resolution ([`src/ingest/decision.py`](../src/ingest/decision.py)):**
  - Thay 9 center-crop presets cứng nhắc bằng 4 câu hỏi cắt cạnh độc lập (`crop_left`, `crop_top`, `crop_right`, `crop_bottom` ∈ `{0, 10, 20, 30, 40}%`) có phép tỉa biên trống và site watermark mà bảo toàn nhân vật/thoại chính.
  - Tính `motion_mode` tất định (`compute_deterministic_motion`) dựa trên aspect ratio thực tế sau crop, tiết kiệm text tokens ở cả Pass 1 và Pass 2.
  - Cumulative support backoff (`support(k) - p_zero`), ngưỡng tin cậy (`edge_conf >= 0.60`, `edge_margin >= 0.12`, `group_conf >= 0.55` → `needs_review`), giữ tối thiểu `60%` width/height & `50%` area.
  - Sửa cấu trúc: luôn có lựa chọn `no_split` cả khi `H/W >= 4.0`, cache split selection, kiểm tra VCS contiguity trước khi gọi `verify_merge_pair_via_decision`.
  - Hỗ trợ `--refresh-decisions`, `--reapply-policy` (`0` API tokens để đánh giá lại threshold), `--dry-run`, và `--decision-policy legacy_9preset` để rollback tức thì.
- **Transactional Repack Fast Path ([`src/core/panel_repack.py`](../src/core/panel_repack.py)) & Static Crop Parity ([`src/render/visual_policy.py`](../src/render/visual_policy.py)):**
  - Atomic staged swap + `_recover_interrupted_repack`; fast path **`metadata_only`** (`0` re-encode ảnh khi không phát sinh split/merge) và tái sử dụng byte cho `KEEP` panels; chỉ dựng `contact_sheets` 1 lần cuối pipeline.
  - Sửa nhánh `static` bị bỏ qua `display_crop` trong `iter_segment_shots`, đồng thời enforce 3 cờ `decision_valid`, `crop_safe`, `text_detection_uncertain` ở cả compile lẫn `validate_visual_plan`.

### 4.2. Số liệu Validation (`bac-si-hoi-quy`) & One-Shot Held-Out (`tro-thanh-rong-nho`)

#### Full Validation (`bac-si-hoi-quy` Ch1 — 188 raw panels)
| Policy | Wall (s) | Total Tokens | Text | Image | Final (`act`/`ctx`/`skip`/`nr`) | Splits Resolved (Checked) | Mean Area Trimmed | Nonzero Crops | Fast Path |
|---|---:|---:|---:|---:|---|---:|---:|---:|---|
| `D_4sided` (all `high`) | 484.94 | 431,524 | 210,273 | 221,251 | `98 / 34 / 56 / 1` | `3 (9)` | 0.0563 | 38 / 98 | `structural_staged` |
| **`F_4sided_adaptive`** (`med`/`high`) | **370.67** | **338,381** | 210,660 | **127,721** | `98 / 34 / 56 / 1` | `3 (9)` | **0.0899** | **52 / 98** | `structural_staged` |

- Kiểm tra trực quan toàn bộ **52/98** active panel có crop cho thấy 100% cắt sạch watermark (`NewTruyenHOT.Org`, `UTOON.NET`, `RESET-SCAN.CO`) và viền thừa, không cắt phạm mặt/thoại; độ đồng thuận `F` vs historical manifest đạt **`145/146` (`99.3%`)**.

#### Frozen One-Shot Held-Out (`tro-thanh-rong-nho` Ch1 — 118 raw panels)
| Policy | Wall (s) | Total Tokens | Text | Image | Est. Cost | Final (`act`/`ctx`/`skip`/`nr`) | Splits (Checked) | Mean Trim | Nonzero | Fast Path |
|---|---:|---:|---:|---:|---:|---|---:|---:|---:|---|
| `A_legacy` | 313.50s | 282,114 | 136,184 | 145,930 | \$0.028211 | `81 / 13 / 23 / 0` | `4 (6)` | 0.2437 | 76 / 81 | `structural_staged` |
| `D_4sided` | 298.22s | 257,423 | 125,602 | 131,821 | \$0.025742 | `79 / 14 / 21 / 4` | `0 (5)` | 0.0390 | 19 / 79 | `metadata_only` |
| **`F_4sided_adaptive`** | **241.29s** | **206,578** | **125,602** | **80,976** | **\$0.020658** | `79 / 15 / 19 / 5` | `0 (5)` | **0.0529** | **27 / 79** | **`metadata_only`** |

- **An toàn khung hình & phân loại:** Trên toàn bộ 79 active panels của Held-Out, `F_4sided_adaptive` đạt **`0/79` lỗi cắt bong bóng thoại/nhân vật** và **`0/79` lỗi bỏ nhầm panel truyện**, đồng thời giữ nguyên toàn vẹn cả hình lẫn thoại (trong khi `A_legacy` cắt cụt thoại/mặt ở hàng loạt panel và split sai 10% lên panel tranh liên tục `panel_115`). Cả `5/118` ca bị giữ lại `needs_review` đều là banner promo `TRUYENQQQ.COM` hoặc dải SFX mảnh `40–167px`.
- **Render Parity End-to-End (`tro-thanh-rong-nho` adapted 39 segments, `1920x1080@30fps`):**
  - Chặn fail-closed khi còn panel `needs_review` (`ValueError: Panel needs review: panel_001`).
  - Full VisualPlan: `static` cho **`160` shots (`128` cropped)**; `motion` cho **`79` shots (`27` cropped, `65 slow_pan_down / 14 static`)**.
  - Encode MP4 thực tế (`libx264` + AAC, `42.48s` subset 6 segment): `F_static` (`4.75s` render, `28` shots) và `F_motion` (`8.46s` render, `10` shots) đều qua `ffprobe` (`1920x1080@30/1`, `has_audio=True`, khớp thời lượng `42.48s`), giữ nguyên mũi kiếm & aura ở `panel_009` trái ngược với `A_legacy` bị cắt đầu & đuôi.

---

## 5. Tổng hợp Kiểm thử Sau Chỉnh sửa Cuối

Toàn bộ lệnh xác minh chạy trực tiếp sau chỉnh sửa cuối cùng của Phase D6 đều **PASS 100%**:

- `.venv/bin/python -m compileall -q src tests scripts` → exit code `0`
- `.venv/bin/python -m pytest -q` → **`583 passed in 61.33s`** (`0` failed, `0` skipped)
  - Bao gồm suite mới [`tests/ingest/test_decision_4sided_and_hardening.py`](../tests/ingest/test_decision_4sided_and_hardening.py) (kiểm tra đủ `5^4 = 625` tổ hợp 4 cạnh, backoff đa đỉnh, từ chối `NaN`, `needs_review`, `no_split` ở `H/W=4.5`, merge contiguity, `--reapply-policy` / `--dry-run`, crash recovery repack, pixel check static crop), [`tests/ingest/test_decision_provider_and_ingest.py`](../tests/ingest/test_decision_provider_and_ingest.py) (default `4sided` + `test_legacy_9preset_rollback_and_cache_upgrade`), và toàn bộ suite Kaggle / Media / Core / Workflow.
- `.venv/bin/python scripts/sync_requirements.py --check` → exit code `0`
- `.venv/bin/manga-recap --help` & `.venv/bin/manga-recap decide --help` → exit code `0`, hiển thị đầy đủ các tùy chọn vận hành mới.
- Dữ liệu gốc trong `data/projects/*` hoàn toàn nguyên vẹn (`gitignored`), mọi live benchmark đều chạy trên bản sao cô lập.

---

## 6. Cơ chế Rollback Nhanh & Tài liệu Liên quan

- **Rollback Kaggle Execution Policy:** `--cloud-policy baseline_split`
- **Rollback Kaggle Batch Size:** `--cloud-batch-size 12` (khi cần quay về mức mặc định cũ trên `produce`)
- **Rollback Gemini Decision Policy:** `manga-recap decide --decision-policy legacy_9preset ...`
- **Tài liệu chi tiết từng giai đoạn:**
  - Kiến trúc V6 Refactor: [`docs/REFACTOR_PLAN.md`](REFACTOR_PLAN.md)
  - Kaggle Optimization & Sweep: [`docs/KAGGLE_PRODUCTION_OPTIMIZATION_PLAN.md`](KAGGLE_PRODUCTION_OPTIMIZATION_PLAN.md), [`docs/verification/v6_phase8_2026-11-26/phase8_live_kaggle_report.md`](verification/v6_phase8_2026-11-26/phase8_live_kaggle_report.md)
  - Gemini Decision Optimization: [`docs/GEMINI_DECISION_OPTIMIZATION_PLAN.md`](GEMINI_DECISION_OPTIMIZATION_PLAN.md), [`docs/verification/v6_decision_d0_2026-11-27/d0_baseline_report.md`](verification/v6_decision_d0_2026-11-27/d0_baseline_report.md), [`docs/verification/v6_decision_d0_2026-11-27/d5_d6_ab_benchmark_report.md`](verification/v6_decision_d0_2026-11-27/d5_d6_ab_benchmark_report.md)
