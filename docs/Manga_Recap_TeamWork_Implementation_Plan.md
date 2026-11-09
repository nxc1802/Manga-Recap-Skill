# TEAMWORK EXECUTION PLAN — Manga-Recap-Skill V3

> **Trạng thái:** Approved for implementation (theo các quyết định đã chốt với Product Owner)  
> **Repo:** https://github.com/nxc1802/Manga-Recap-Skill  
> **Baseline tham chiếu:** `main` tại commit `a9920a53b54e6aba30bcba3d3ac60323d74c9336` (phân tích trước đó).  
> **Mục tiêu:** Cloud chạy đúng → Local/Cloud thống nhất → Human-driven Agent QA → tối ưu hiệu năng thực tế.  
> **Phạm vi:** Chỉ P0 Cloud Correctness, P1 Unified Contracts, P1 Agent QA, P3 Performance Engineering.

## 0. Mệnh lệnh cho TeamWork Orchestrator

**Hãy thực thi kế hoạch này trực tiếp trong repository, không chỉ lập kế hoạch lại.** Trước khi sửa, fetch/inspect HEAD hiện tại, đối chiếu baseline và điều chỉnh danh sách file theo mã nguồn thực tế. Không tự ý mở rộng scope. Phải có code, test, tài liệu cần thiết và báo cáo thay đổi có bằng chứng. Mọi thay đổi phải được kiểm tra và tích hợp theo thứ tự phụ thuộc.

**Quy tắc tiết kiệm chi phí:** tối đa **2 worker agent chạy đồng thời**; mặc định 1 worker đối với công việc không song song hóa hiệu quả. Orchestrator trực tiếp phân công, khóa API, review diff, merge và ra quyết định; **không spawn thêm agent chuyên quản lý, agent research hoặc agent evaluator**. Giữ một bản repository map/contract dùng chung, tránh cho từng agent đọc lại toàn bộ repo. Sử dụng model tiết kiệm cho khảo sát, test, tài liệu, profiling; chỉ dùng model coding mạnh cho phần logic/cloud contracts khó. Không lặp vô hạn: mỗi lỗi phải có bằng chứng, phạm vi sửa và test liên quan trước khi retry. Nếu không đạt gate, khắc phục đúng phần lỗi, không mở lại toàn bộ phase.

**Giới hạn song song:** mỗi file chỉ có **1 agent được quyền sửa trong cùng thời điểm**. Mỗi worker dùng worktree/branch riêng; không ghi đè file của worker khác. Merge theo thứ tự, review diff và chạy test tương ứng sau mỗi merge. Nếu môi trường không hỗ trợ multi-worktree, thực thi tuần tự, giữ nguyên chất lượng/gates.

### Quyết định kỹ thuật đã khóa

1. Cloud phải đạt **functional feature parity** với Local; hành vi chưa hỗ trợ phải fail rõ ràng, không được silently degrade.
2. Asset thiếu/hỏng: **fail fast** với chapter/segment/path rõ ràng; **không substituting panel hoặc audio**.
3. TTS theo từng segment: `voice_id`, `speed`, `emotion` và các tham số liên quan phải được tôn trọng.
4. Hoàn thành chỉ khi MP4, WAV, SRT, duration, checksum, tương quan timeline A/V và metadata được kiểm tra hợp lệ.
5. Một canonical schema cho project/chapter/script/artifacts/checkpoints; Cloud `tts`, `render`, `produce` có thể chạy riêng hoặc kết hợp, dùng lại cache.
6. Ưu tiên engine render chung, chọn codec/hardware theo môi trường; chỉ giữ adapter riêng khi cùng contract và có lý do thực nghiệm rõ ràng.
7. Config và voice được duyệt theo series; chapter preview **tùy chọn**; technical validation bắt buộc. `skip-preview` không đồng nghĩa `quality_approved`.
8. **Faithful Storytelling**: không bịa tình tiết quan trọng; OCR/VLM do Agent tự chọn và tự xử lý.
9. **Không có Agent quality benchmark, OCR accuracy evaluation, LLM-as-Judge, narrative scoring hoặc threshold tự động.** Chất lượng truyện/âm thanh/hình ảnh do **Human Review** quyết định. Technical validation và performance benchmark **vẫn bắt buộc**.
10. Quan hệ **Panel ↔ Script N:M**: một script có thể dùng 1/nhiều panel; một panel có thể được tái sử dụng; panel vô nghĩa có thể bỏ; **panel mang thông tin quan trọng phải được giữ lại trong nội dung video**. Agent tự quyết, Human duyệt; không ép 1:1 và không chấm điểm coverage ngữ nghĩa tự động.
11. Performance ưu tiên: **E2E wall time → throughput → chi phí**, đo trên **Kaggle GPU + Apple Silicon**; có workload small / large / batch. Chấp nhận các tối ưu hình/âm thanh khó nhận biết bằng mắt/tai **chỉ sau Human Review**; không được giảm correctness.

## 1. Tổ chức nhân sự và chi phí

| Phase | Mục tiêu | Worker đồng thời | Agent roles | Điều kiện bắt đầu |
|---|---|---:|---|---|
| **0** | Repo reconnaissance + contract freeze | **1** | Contract/Integration Architect | Ngay lập tức |
| **1** | P0 Cloud Correctness | **2** | Cloud Fixer + Regression Test Builder | Gate 0 đạt |
| **2** | P1 Unified Contracts & Engines | **2** | Core/Local Engineer + Cloud Adapter Engineer | Gate 1 đạt |
| **3** | P1 Agent QA do Human dẫn dắt | **1** | Agent Workflow Engineer | Gate 2 đạt |
| **4** | P3 Performance Baseline | **1** | Performance Profiler | Gate 3 đạt |
| **5** | P3 Targeted Optimizations | **2** | TTS Specialist + Render/Cloud Specialist | Gate 4 có số liệu |
| **6** | Integration, regression, release readiness | **1** | Independent Technical Verifier | Gate 5 đạt |

**Tổng:** 7 phase, **10 worker assignments**, **đỉnh đồng thời = 2**. Orchestrator điều phối xuyên suốt, không tính thêm một worker. Không spawn cả 10 agent cùng lúc; mỗi phase chỉ mở số worker ghi trên bảng. Trong một phase có 2 worker, hai worker có nhiệm vụ tách biệt và quyền sở hữu file không chồng chéo.

**Phân bổ model:** Agent 0/3/4/6: model chi phí vừa/thấp, nâng cấp khi thật sự cần giải quyết issue khó. Agent 1-Cloud và 2-Core/Cloud: model coding mạnh cho thay đổi contract và logic có rủi ro. Agent viết tests và benchmark không cần model đắt nếu fixture/API đã rõ. Agent phase 5 chỉ tối ưu sau khi có số liệu chứng minh bottleneck.

**Không chi thêm cho:** build lại toàn bộ Agent, tìm OCR/VLM/TTS model mới, tạo benchmark chất lượng truyện, tạo UI dashboard mới, refactor không liên quan, chạy benchmark cloud lặp vô ích, nhiều vòng code-review không có lỗi mới.

## 2. Hợp đồng phối hợp chung

### 2.1 Source of truth

- `project.json`, `chapter.json`, `manifest.json`, `script.json` và checkpoint có **schema/version rõ ràng**. Không tạo schema song song cho Cloud.
- Script dùng `seq_id` bất biến (không được giả định `000`, `001`, ...), `panel_ids` có thể chứa nhiều ID, rỗng chỉ khi contract quy định minh thị một cảnh không dùng ảnh; mặc định scene hình ảnh phải có panel hợp lệ.
- `panel_ids` là **panel được chọn đưa vào render**, **không phải toàn bộ panel nguồn**; việc bỏ panel nguồn không mặc định là bug. Các panel đã được chọn phải được render đúng, không bị rớt/đổi hình.
- Lưu ánh xạ traceability nhẹ: `seq_id → script text → source panel_ids → rendered assets`. Có thể lưu text OCR/notes nếu Agent đã tạo; **không ép Agent tạo OCR confidences hoặc scoring**.
- Fingerprint/cache key gồm các đầu vào thực sự tác động artifact: content, voice/settings/model/engine cho audio; panel/assets, motion, subtitles, resolution, audio fingerprint, encoder settings cho video. Không làm invalidation dây chuyền nếu không cần.
- Output format và trạng thái `pending/running/failed/completed` hoặc tên tương đương phải có semantics nhất quán; mọi `completed` cần validator pass.

### 2.2 Chất lượng và review

- **Technical QA:** schema, references, audio/video validity, sync/duration tolerance, checksum, cache/resume, lỗi thiếu asset, feature parity và regression tests. Được tự động hóa.
- **Human QA:** OCR đúng/sai, nhân vật, diễn biến, độ hấp dẫn, nhịp kể, panel quan trọng, giọng đọc, hiệu ứng hình ảnh. **Không có auto-score cho các tiêu chí này.**
- `technical_status` và `human_review_status` độc lập: `technically_completed` không tự động thành `quality_approved`; khi bỏ qua preview, đặt `unreviewed` (hoặc giá trị tương đương được schema hóa).
- Human review đủ điều kiện qua **review pack hiện có hoặc bản tối giản**: contact sheets/selected panels, bảng mapping `seq_id → panel_ids → narrator_text`, omissions nếu có, audio/video preview. Ưu tiên tái sử dụng artifact có sẵn; không build frontend nếu không bắt buộc.

### 2.3 Mỗi worker bắt buộc trả về

1. Các file thay đổi và lý do, chỉ trong phạm vi được giao.
2. Tests đã chạy (lệnh + kết quả), tests chưa chạy và nguyên nhân.
3. Các behavioral changes, backward compatibility/migration nếu có.
4. Rủi ro tồn đọng, known limitations, hướng dẫn rollback.
5. Commit hash/branch hoặc patch rõ ràng để Orchestrator tích hợp.

## 3. Phase 0 — Repo Reconnaissance & Contract Freeze

**Số agent: 1** — `A0: Contract/Integration Architect`.

**Nhiệm vụ:**

- Đọc `AGENTS.md`, `.agents/skills/manga-recap/SKILL.md`, CLI, cloud service, core/render/tts/production, fixtures và tests hiện hữu. Xác thực HEAD; cập nhật file paths thay đổi so với baseline.
- Ghi ngắn gọn canonical input/output của từng stage: crawl/segment → Agent script → TTS → render → export; current vs target behavior.
- Liệt kê contract fields và trạng thái hiện tại; xác nhận migration strategy **backward compatible** với project/chapters đang có, hoặc migration rõ ràng nếu không thể.
- Khóa interface chung trước Phase 2: `script`, panel resolver, `tts`, `render`, `produce`, artifact validator, checkpoint và review status. Ưu tiên API hiện hữu, thay đổi tối thiểu.
- Chuẩn bị **test fixture nhỏ** có: segment ID không tuần tự (vd. `002_1`), 2–3 panel/segment, 2 voice IDs, speed/emotion khác nhau, một panel bị bỏ hợp lệ, một panel thiếu có chủ ý.
- Lập **repo map 1 trang + contract decisions** chia sẻ cho mọi worker, tránh phải đọc lại toàn repo.

**Sản phẩm:** `docs/engineering/contract_decisions.md` (hoặc vị trí tương ứng), fixture specification, danh sách code owners/paths và lệnh test baseline; không sửa engine lớn ở phase này.

**Gate 0:** Các API/fields đầu vào–đầu ra đã khóa, các trường hợp cũ được xử lý, ownership của Phase 1–2 không xung đột.

## 4. Phase 1 — P0 Cloud Pipeline Correctness

**Số agent: 2, chạy song song sau Gate 0.**

### A1 — Cloud Fixer (owner implementation)

**Sở hữu:** `src/cloud/kaggle_service.py` và các helper cloud mới nếu thật sự cần. **Không sửa tests của A2**.

**Tasks bắt buộc:**

1. Xóa logic chỉ lấy `panel_ids[0]`: mỗi segment phải trình bày đầy đủ **panel được chọn** theo layout/timeline có kiểm soát; giữ camera motion hoặc đánh dấu fail nếu feature contract chưa hỗ trợ.
2. Panel/audio missing/corrupted: bỏ fallback lấy ảnh đầu thư mục; error gồm `chapter_id`, `seq_id`, asset path, failure stage.
3. TTS batch: chuyển `voice_id`, speed, emotion **đúng từng segment**; nếu backend chỉ batch được cùng tham số, group theo param hoặc fallback có kiểm soát; không dùng giọng của segment đầu cho tất cả. Kiểm tra số audio trả về = số segment, không zip/truncate im lặng.
4. Dùng `seq_id` thật để đặt tên và map file; không hardcode `f"{idx:03d}"` nếu làm mất danh tính.
5. Chỉ mark success sau khi có đủ artifact, validation pass và sync atomic. Timeout phải raise failed/timeout cụ thể; không tiếp tục sync sau timeout. Validate MP4, audio, subtitles, timing, hashes và paths thực tế.
6. Chống path traversal khi extract tar không tin cậy; không tự gán license CC0 cho manga không có quyền rõ ràng. Không đưa auth token vào logs/artifacts.
7. Resume/idempotence: artifact hợp lệ được reuse, lỗi tạo lại đúng phạm vi; failure không ghi `completed`.

### A2 — Regression Test Builder (owner tests)

**Sở hữu:** `tests/unit/cloud/**`, `tests/adversarial/**` và cloud fixture files mới. **Không sửa implementation của A1**.

**Tests phải viết:**

- `seq_id` dạng `002_1` vẫn map đúng filename/audio/subtitle/video.
- Nhiều panel trong cùng segment đều được render; bỏ panel không chọn là hợp lệ; đã chọn mà thiếu thì **fail**, không thay bằng panel khác.
- Nhiều voice/speed/emotion: mock xác minh call args riêng từng segment; không yêu cầu benchmark chất lượng voice.
- Batch TTS thiếu một result => fail; file hỏng/thiếu => fail; render timeout/download incomplete => fail; không có false `completed`.
- Checkpoint resume không nhân đôi file/job hợp lệ; failure có đủ định danh.
- Tests sử dụng mock/synthetic data khi có thể, tránh tiêu tốn Kaggle quota.

**Phối hợp:** A2 viết tests theo Gate 0 contract; A1 không thay test để làm chúng pass. Orchestrator merge A1 + A2, chạy regression test. Nếu test và contract bất đồng, quyết định tại Orchestrator, không cho agent âm thầm nới lỏng.

**Gate 1:** Tất cả P0 regression tests pass; không còn silent fallback, false success, sai panel/voice/ID; một sample E2E local-cloud (mock worker) chạy thông suốt. **Nếu chưa chạy cloud thật, phải ghi “live Kaggle unverified”, không được tự tuyên bố cloud production-ready.**

## 5. Phase 2 — P1 Unified Local & Cloud Contracts

**Số agent: 2.** Trước khi dispatch, Orchestrator công bố contract từ Phase 0 và bản P0 đã merge.

### A3 — Core/Local Engineer

**Sở hữu:** canonical schema/validation ở `src/core/**` (xác định file thật khi inspect), `src/cli/tts_cmd.py`, `src/cli/render_cmd.py`, `src/production/runner.py`, các file render core được phân công. **Không sửa `src/cloud/kaggle_service.py`.**

**Tasks:**

- Triển khai schema/version và validator chung với backward compatibility cho project/script cũ.
- Chuẩn hóa API `tts`, `render`, `produce`; `tts` chỉ làm TTS, `render` ưu tiên audio đã có, `produce` điều phối cả hai. CLI không được mặc định ép option khác cấu hình project nếu user không truyền.
- Checkpoint/cache fingerprints nhất quán, có validation trước commit trạng thái.
- Cô lập logic render chung (composition, motion, subtitles, audio mixing) thành reusable module **nếu khả thi mà không tăng chi phí/độ rủi ro bất hợp lý**.
- Hợp nhất ngữ nghĩa Human Gates và trạng thái technical/human review.

### A4 — Cloud Adapter Engineer

**Sở hữu:** `src/cloud/kaggle_service.py`, cloud-specific tests/adapter files. **Không sửa schema/core hoặc CLI A3 đang sở hữu.**

**Tasks:**

- Cloud chỉ consume canonical schema, không định nghĩa bản sao fields hoặc defaults trái contract.
- Cloud `tts`/`render`/`produce` thực sự tách job; chỉ upload/download artifacts cần thiết và reuse audio cache khi render-only.
- Chuẩn hóa output layout, error taxonomy, checkpoint, status, subtitle style, camera motion, BGM/mixing, fps/resolution và các option có trong Local.
- Ưu tiên dùng shared renderer; nếu cloud worker không thể import shared module trên Kaggle, đóng gói module hoặc tạo adapter có test feature parity, ghi rõ lý do.
- Không đánh đồng **functional parity** với video byte-for-byte giống nhau (khác encoder là hợp lệ).

**Integration protocol:** A3 chốt API/schema trên một commit nhỏ được Orchestrator tích hợp trước khi A4 kết nối; sau đó hai agent hoàn thiện file ownership riêng. Mọi thay đổi public interface cần Orchestrator phê duyệt, không tự drift.

**Gate 2:** Cùng `script.json` + project config chạy được ở Local và Cloud với tương đương chức năng, cùng cấu trúc artifact/status, không chạy lại TTS khi chỉ render, không silent-disable feature. Regression tests cả hai backend pass.

## 6. Phase 3 — P1 Human-Driven Agent QA

**Số agent: 1** — `A5: Agent Workflow Engineer`.

**Sở hữu:** `.agents/skills/manga-recap/{SKILL.md,rules/**,prompts/**}`, template/script-review helper nếu cần, docs về review; tránh sửa render/cloud core trừ khi Orchestrator cho phép một patch tách riêng.

**Tasks:**

1. Cập nhật prompt/rules: **Agent tự chọn OCR/VLM/strategy**, không yêu cầu benchmark hoặc confidence gating; bắt buộc faithful storytelling và giữ các panel có thông tin quan trọng trong video.
2. Sửa các hướng dẫn ép `1 panel = 1 segment`: hỗ trợ N:M mapping, gộp nhiều panel thành một script, reuse panel, bỏ panel không quan trọng; không mất thứ tự sự kiện logic.
3. Evidence Layer **tối giản, phục vụ người duyệt**: per segment link tới source panels, text OCR/notes nếu có, narrator text, preview. Không bắt buộc lưu toàn bộ OCR hoặc xây scoring.
4. Dùng contact sheets/assets hiện có tạo human-review pack: retained/omitted panels, script alignment, preview audio/video; có thể dùng Markdown/JSON/HTML đơn giản, **không tạo ứng dụng UI mới**.
5. Series Gate0/Gate1 duyệt một lần; preview Gate2 optional. Nếu không duyệt preview, chỉ lưu `unreviewed`; **không có auto `quality_approved`**.
6. Chỉnh docs cho human reviewer: chấp nhận hoặc yêu cầu sửa với ID segment/panel và ghi chú; Agent sửa theo feedback, giữ artifact caching/invalidation.

**Gate 3:** Có sample kịch bản gồm 1→N panel, reused panel, omitted panel; technical mapping hợp lệ. Human có đủ tư liệu để xác nhận panel quan trọng đã giữ và truyện không bị bịa. **Không tạo/chạy OCR accuracy, narrative benchmarks hoặc LLM judge.** Human Review chưa thực tế được thực hiện thì không ghi là đã approved.

## 7. Phase 4 — P3 Performance Baseline

**Số agent: 1** — `A6: Performance Profiler`.

**Sở hữu:** benchmark harness/log/report ở `docs/performance/**`, `tests/performance/**` hoặc script riêng; **không refactor runtime** trong phase baseline.

**Tasks:**

- Chốt workload tái lập được: **small 30–60 segments**, **large 100–200 segments**, **batch 5–10 chapters**; reuse test assets không chứa dữ liệu riêng tư/bản quyền khi chia sẻ.
- Đo tách biệt: packaging, upload, Kaggle startup, TTS inference, compositing, video encode, download, validation; thêm tổng E2E wall time, video-minutes/hour, peak RAM/VRAM, cache hit và GPU/CPU utilization nếu thu thập được.
- Hai môi trường: **Kaggle GPU và Apple Silicon**. Ghi model GPU/CPU, encoder, resolution, preset, voice model, thread count, dependencies, warm/cold start, cache state.
- Chạy profiling hữu hạn, không spam Kaggle; **một baseline tin cậy cho mỗi workload/hardware sẵn có**, warm/cold rõ ràng. Nếu không có quyền Kaggle/GPU, ghi blocker và baseline local; không giả lập số liệu cloud thành số đo thực tế.
- Lập bảng bottleneck theo **E2E time impact**; đề xuất **tối đa ba** tối ưu có ROI cao nhất. Không kết luận GPU utilization thấp đồng nghĩa phải tăng threads nếu chưa chứng minh.

**Gate 4:** Có `baseline.md` + raw machine-readable timings, môi trường tái lập và 1–3 bottleneck định lượng. Không bắt đầu tối ưu bằng cảm tính.

## 8. Phase 5 — P3 Targeted Performance Optimizations

**Số agent: 2**, chỉ mở khi Gate 4 có bottleneck phù hợp. Nếu baseline cho thấy chỉ một bottleneck áp đảo, **chỉ dùng 1 agent** và bỏ worker còn lại để tiết kiệm chi phí.

### A7 — TTS Specialist

**Sở hữu:** `src/tts/engine.py`, các helper/cache/CLI TTS riêng được giao và tests TTS. **Không sửa `src/cloud/kaggle_service.py` hay renderer**.

**Ưu tiên theo profiling:**

- Tăng batch throughput hợp lệ cho neural TTS; lưu ý backend hiện khóa `sdk.infer` nên thêm ThreadPool có thể **không** làm tăng tốc inference. Chỉ sửa lock/concurrency khi chứng minh thread-safety và speedup.
- Tái sử dụng model/voice, giảm model load và conversions thừa; optimize audio cache, tránh re-synthesize nếu chỉ chỉnh render.
- Giữ voice/speed/emotion, duration và audio validity; không làm giảm chất lượng âm thanh nếu chưa có Human Review.

### A8 — Render/Cloud Specialist

**Sở hữu:** `src/render/video_engine.py`, `src/production/hardware.py`, `src/cloud/kaggle_service.py` và tests riêng của render/cloud. **Không sửa TTS core do A7 sở hữu**.

**Ưu tiên theo profiling:**

- Giảm decode/resize/composite trung gian không cần thiết, tận dụng hardware-accelerated encode (NVENC/VideoToolbox) khi khả dụng, tránh encoder fallback im lặng.
- Tối ưu file packaging/upload/download và Cloud startup/cache, đặc biệt incremental render.
- Giữ 100% selected-panel coverage, camera motion, subtitles, BGM, A/V sync và contract parity; thay đổi FPS/quality presets chỉ khi được cấu hình và Human Review chấp nhận.

**Gate 5:** Có before/after cùng workload & hardware, ghi E2E wall time, throughput, resource cost và bottleneck; ưu tiên cải thiện E2E thực sự. Tất cả P0/P1 regression tests pass. Nếu không có cải thiện đáng kể hoặc làm giảm chất lượng/contract, **revert tối ưu**, không giữ code phức tạp vì lý do hình thức.

## 9. Phase 6 — Independent Verification & Release Readiness

**Số agent: 1** — `A9: Independent Technical Verifier` (không phải agent chấm chất lượng truyện).

**Sở hữu:** tests integration, smoke-test scripts, docs/release notes; mặc định reviewer chỉ đọc implementation, các bugfix được giao lại cho owner có liên quan hoặc tạo patch nhỏ sau khi Orchestrator phê duyệt.

**Checklist:**

- [ ] Đối chiếu requirements P0, P1 Contracts, P1 Human QA, P3 Performance với từng commit.
- [ ] Unit + adversarial + integration + existing E2E đều pass; không sửa/xóa test chỉ để tạo kết quả pass.
- [ ] Thử case missing panel/audio, `seq_id` không tuần tự, multi-panel, multi-voice, preview skipped, resume, invalid/cloud partial download.
- [ ] Local/Cloud cùng canonical schema; render-only không gọi TTS; script/panel N:M hợp lệ; output validation trước status completed.
- [ ] Chạy **ít nhất một Kaggle GPU smoke test thật** nếu credentials/quota cho phép; kiểm tra MP4/WAV/SRT thực sự tải về, duration & ffprobe. Nếu chưa chạy được, ghi rõ `BLOCKED: live Cloud E2E not verified`, **không claim production ready**.
- [ ] Không phát sinh automated OCR/narrative quality evaluation; human review status đúng `approved`/`unreviewed` theo sự kiện thật.
- [ ] Human xem mẫu output về kể chuyện, panel quan trọng, hình/âm và quyết định phê duyệt; technical verifier không được thay Human quyết định quality.
- [ ] Docs/CLI flags/defaults khớp implementation; migration guidance, privacy/copyright/license warnings và rollback rõ ràng.
- [ ] Có baseline và after metrics (hoặc ghi missing measurement thật thà) trên Apple Silicon/Kaggle.

**Gate 6 / Definition of Done:** Code merged, test pass, cloud smoke verified khi có thể, artifacts hợp lệ, contract parity đạt, human-review workflow hữu dụng, performance report minh bạch. **Trạng thái sản phẩm phải phân biệt:** `technical_ready`, `human_quality_approved`, và `cloud_live_verified` (không đánh đồng ba trạng thái).

## 10. Test matrix tối thiểu (technical; không phải Agent quality eval)

| Test ID | Kịch bản | Kết quả bắt buộc | Owner |
|---|---|---|---|
| T01 | 1 segment có 3 selected panels | Cả 3 được dùng đúng, không mất panel | A2/A4 |
| T02 | 1 panel nguồn được loại bỏ có chủ ý | Không fail vì panel không xuất hiện trong `panel_ids` | A2/A5 |
| T03 | Panel trong `panel_ids` không tồn tại | Fail với `seq_id` + path; không substitute | A2 |
| T04 | 2 segments có voice/speed/emotion khác | TTS backend nhận đúng config từng segment | A2 |
| T05 | `seq_id = 002_1` / không liên tục | Output mapping không lệch, SRT/audio đúng | A2 |
| T06 | TTS batch trả thiếu audio | Fail; không truncate silently | A2 |
| T07 | MP4 thiếu/hỏng hoặc download incomplete | Không đánh dấu completed | A2/A9 |
| T08 | Rerun không thay input | Cache hit; không tái sinh valid artifacts | A2/A9 |
| T09 | `render` khi WAV hợp lệ đã có | Không invoke TTS | A3/A4 |
| T10 | Local vs Cloud cùng script | Parity về feature/options/artifacts | A4/A9 |
| T11 | Bỏ qua chapter preview | `human_review_status = unreviewed` | A5/A9 |
| T12 | Video + SRT + WAV | Valid duration/timeline/checksum/ffprobe | A2/A9 |

**Lưu ý:** `T01` kiểm tra các panel **được chọn** xuất hiện, không ép render toàn bộ panel gốc. `T02` và Human Review xử lý việc bỏ panel không quan trọng; **không xây thuật toán tự động đo “importance”**.

## 11. Báo cáo bắt buộc khi kết thúc mỗi phase

Orchestrator ghi ngắn gọn vào `docs/engineering/implementation_log.md` (hoặc file tương đương):

```markdown
### Phase <n> — <title>
- Status: PASS / FAIL / BLOCKED
- Agents used / max concurrency: ...
- Changes: <commits + file paths>
- Tests: <commands + result summaries>
- Acceptance evidence: <artifact paths / logs / reports>
- Remaining risks: ...
- Next phase unlocked: yes/no
- Human decisions needed: ...
```

### Escalation rules

- Test fail: trả task về **đúng file owner**, không spawn thêm nhóm chẩn đoán.
- Feature không đạt parity: **fail rõ ràng** + ghi backlog; không khai báo Phase 2 PASS cho tới khi contract đạt hoặc Product Owner chấp thuận thay đổi scope.
- Phải thay đổi schema đã khóa: Orchestrator tạm dừng phần phụ thuộc, cập nhật contract một lần, sau đó thông báo các worker.
- Không có Kaggle credentials/quota: vẫn thực hiện unit/mock/local verification; đánh dấu Cloud live chưa kiểm chứng và xin quyền/chạy test khi có thể, không giả định thành công.
- Nghi ngờ sai câu chuyện hoặc thiếu panel quan trọng: trình bằng chứng cho **Human Reviewer**, không thêm evaluator agent.
- Performance regression: revert thay đổi gây regression, giữ baseline và báo nguyên nhân.

## 12. Lệnh giao việc đầu tiên cho TeamWork

> **Execute this plan inside `nxc1802/Manga-Recap-Skill`. Start Phase 0 with exactly one Architect agent. Freeze interfaces and file ownership, then unlock phases sequentially only when gates pass. Use no more than two concurrent worker agents, keep file ownership disjoint, prioritize minimal safe diffs, technical regression coverage, real cloud correctness, and measured performance. Do not build Agent OCR/narrative benchmarks or automated quality evaluators; story quality is approved only by a human. Do not claim success for unrun tests or unavailable Kaggle jobs. Deliver integrated code, test evidence, metrics, and an implementation log—not another planning document.**

---

**Không nằm trong phạm vi:** xây bộ dữ liệu lớn; tìm kiếm model/engine mới chỉ để thử nghiệm; hệ thống chấm điểm OCR/story tự động; dashboard UI mới; chức năng YouTube không liên quan; nâng cấp repo tràn lan; tuyên bố đã chạy cloud/test nếu chưa có kết quả thật.
