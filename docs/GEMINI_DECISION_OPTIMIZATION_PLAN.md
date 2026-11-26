# GEMINI DECISION — OPTIMIZATION PLAN

**Repository:** `nxc1802/Manga-Recap-Skill`  
**Scope:** Gemini Decision 1, panel classification, structural repair, 4-sided display crop, motion, cache, quality evaluation.  
**Ngày audit:** 26/11/2026.  
**Quan hệ:** bổ sung cho REFACTOR_PLAN.md; độc lập với KAGGLE_PRODUCTION_OPTIMIZATION_PLAN.md.  
**Trạng thái:** plan triển khai, chưa sửa repository, chưa chạy live API benchmark.

## 0. Mục tiêu và nguyên tắc

- Thay 9 center-crop preset bằng 4-sided Crop, mỗi cạnh chọn một trong `0, 10, 20, 30, 40%`.
- Tối ưu kết quả thực tế sau render: giữ mặt, chủ thể, hành động và thông tin hình ảnh quan trọng; giảm vùng dư và bong bóng thoại không cần hiển thị nếu có thể cắt an toàn.
- Tối ưu tổng input token, request count, latency và chi phí trên mỗi chapter; không tối ưu một metric bằng cách làm giảm chất lượng khác.
- Giữ 3 nhóm ACTIVE / CONTEXT_ONLY / SKIP; giữ Split/Merge dựa trên VCS, source provenance, original panel cho OCR/narrative, review gates, static/scroll, local/Kaggle parity.
- Gemini Decision chỉ thực hiện closed-set decisions. Không thêm generative LLM, detector mới, database, queue service hoặc framework mới làm dependency bắt buộc.
- Đo trước khi tối ưu. Không tự tuyên bố chất lượng hay chi phí tốt hơn khi chưa có benchmark; không xóa baseline trước khi challenger đạt gate.

## 1. Audit hiện trạng

Source đọc trực tiếp trên default branch: `src/core/decision_engine.py` (blob `ee66c5a7199fca9d5d1b8a9a38d053da8d2fbe10`), `src/core/panel_repack.py` (`c2d16f1efec6cc728e989eea850ff984c164a0df`), `src/render/visual_policy.py` (`1f6aef644cbbe5edc8af04f5a0bd8d45c20a90e6`), `src/cli/main.py` (`48489e8b4b924a8b15a7e479b49b4be86350e231`). Khi bắt đầu triển khai, pin commit SHA và lưu bản baseline; không giả định branch không đổi.

| Vị trí (dòng tại audit) | Hành vi | Hạn chế / rủi ro |
|---|---|---|
| decision_engine.py 38–49, 158–175 | 9 preset crop normalized | Ít lựa chọn bất đối xứng; mô tả dài, không có crop bottom-left/bottom-right riêng |
| 62–74 | JPEG 82, resize nếu H > 1024 | Tall panel bị thu nhỏ chiều rộng, khó thấy thoại/chi tiết; ảnh rất rộng không bị cap chiều rộng |
| 77–112 | REST POST, 30s, tối đa 6 lần thử, retry 429 | Chưa reuse connection rõ ràng, chưa xử lý timeout/5xx có chủ đích; 429 sleep đồng thời giữa các worker; cần retry budget |
| 115–233 | 4 câu hỏi cùng ảnh; dùng choice, bỏ confidence/probabilities | Thiếu answer bị mặc định ACTIVE/KEEP/FULL/STATIC như kết quả bình thường; có thể mâu thuẫn giữa classification, structure, crop, motion |
| 221–223 | Split chỉ ACTIVE và H/W >= 2.2 | Có thể bỏ sót composite ngắn hoặc text-only cần tách; benchmark trước khi đổi ngưỡng |
| 236–299 | Chọn tối đa 2 cut, 8 candidates; no_split chỉ H/W < 4 | Có thể ép cắt continuous illustration rất cao; candidate có thể xuyên qua nét/thoại; secondary có thể trùng primary |
| 322–354 | Cache theo panel_id + SHA, lưu sau toàn Pass 1 | Thiếu version model/question/preprocess; mất kết quả đã thành công nếu future lỗi; reclassify_all chỉ tác động Pass 2 |
| 356–370, 430–449 | Split-selection call và Pass 2 cho child | Token split selection không cộng vào tổng; kết quả Pass 2/split không được cache tương đương Pass 1 |
| 379–389 | Merge với item KEEP/MERGE trước | Chỉ nhìn panel hiện tại, không đủ evidence về quan hệ với panel trước; cần kiểm tra VCS liền kề và nội dung |
| 459–485 | Gán text_detection_uncertain=False cho mọi panel; tạo lại sheets | Không chứng minh chắc chắn về text; repack đã tạo sheets trước đó |
| panel_repack.py 143–253 | Re-extract JPEG tất cả panel từ raw, renumber, xóa file cũ rồi move file mới; tạo contact sheets | Tốn I/O; KEEP không giữ byte identity; swap nhiều file không thực sự atomic; lỗi giữa chừng có thể để artifacts không nhất quán |
| panel_repack.py 299–321 | Candidate = row variance minima cách nhau >= 8% H hoặc 60px | Không đảm bảo là gutter; cần đánh giá candidate recall và actual split sau VCS snapping |
| visual_policy.py 210–247 | Crop metadata chỉ được dùng trực tiếp trong animation branch, focus/avoid/uncertainty có precedence | Static mode có thể bỏ qua Decision crop; uncertainty flag hiện tại thiếu căn cứ |

Điểm tốt: một request cho nhiều question trên mỗi panel; parallel request có giới hạn; two-pass chỉ hỏi lại panel split/merge; crop dạng metadata render-only; có source SHA và deterministic geometry; giữ CONTEXT_ONLY cho narrative.

Kết luận: giữ pipeline hai pass về mặt logic, thay question contract + validation, tối giản I/O và đánh giá output end-to-end. Chưa có dữ liệu ground truth để kết luận accuracy hiện tại cao hay thấp.

## 2. Gemini Decision API contract

Nguồn chính thức: [Google announcement](https://blog.google/innovation-and-ai/technology/developers-tools/gemini-decisions-api/), [Google Codelab](https://codelabs.developers.google.com/getting-started-with-gemini-decisions-api?hl=en). Google mô tả shared input được tính một lần mỗi request, các câu hỏi có instruction/criteria token riêng, trả probability/confidence, không sinh output token tính phí; giá công bố 0.10 USD/1M input token. Kiểm tra lại docs/SDK và response thật trước triển khai parser. Không giả định rate limit hoặc image token mapping cố định.

Các câu hỏi độc lập: không viết `motion_mode` dựa trên đáp án crop của câu hỏi khác trong cùng request. Kết hợp kết quả bằng code. `choice` lấy đáp án trong criteria; không tự ép output ngoài danh sách. Dùng probability để đánh giá độ chắc chắn, không coi confidence là ground truth. Không thay API bằng generateContent.

## 3. 4-sided Crop — canonical contract

### 3.1. Geometry

Mỗi cạnh có đúng 5 options `0/10/20/30/40`, % được đo trên kích thước ảnh gốc của panel hiện tại, không cắt tuần tự. Tổng 5^4 = 625 tổ hợp, không phải 625 options trong một câu hỏi. Không đồng nghĩa 625 tổ hợp đều an toàn.

```python
# Input: integers from {0, 10, 20, 30, 40}
# L, T, R, B refer to original panel, not already cropped image.
box = {
    "x_min": L / 100,
    "y_min": T / 100,
    "x_max": 1 - R / 100,
    "y_max": 1 - B / 100,
}
```

Example `L=10,T=20,R=0,B=10` => `[0.1, 0.2, 1.0, 0.9]`, giữ 90% width, 70% height, 63% area. `L=R=T=B=40` chỉ giữ 4% area, phải bị guardrail chặn mặc dù hợp lệ hình học. Không dùng crop này để thay đổi raw, panel.jpg, OCR, narration evidence hay panel IDs. `display_crop` là canonical normalized Region downstream; lưu thêm `crop_edges_pct`, `crop_method`, `crop_confidence`, `crop_policy_version` phục vụ audit. Legacy `center_crop_preset` chỉ đọc trong migration, không còn là output mới.

### 3.2. Question design

Một ảnh + context dùng chung, 4 câu hỏi crop trong **cùng một request**, không gửi ảnh 4 lần. Ví dụ ngắn (English prompts như code hiện tại; A/B test wording):

```text
Shared: Manga panel for 16:9 recap. Keep all important faces, bodies,
actions and scene clues. Trim only safely dispensable edge whitespace,
decoration or speech bubbles. If unsure, trim less or 0%. Percentages
refer to the original image. Consider the combined crop conservatively.

crop_left: Largest safe trim from the left edge?
crop_top: Largest safe trim from the top edge?
crop_right: Largest safe trim from the right edge?
crop_bottom: Largest safe trim from the bottom edge?

criteria (each question):
0: Keep edge.
10: Remove 10%.
20: Remove 20%.
30: Remove 30%.
40: Remove 40%.
```

Dùng `choice` với criteria map (keys dạng string nhất quán `0`, `10`, ... nếu schema cho phép, nếu không dùng `p00`, `p10`, ...). Không dựa vào thứ tự map. Không nói remove all speech bubbles: khi bong bóng chứa thông tin trọng yếu hoặc overlap face/action, giữ nguyên. Không cắt bên trái 10% rồi tiếp tục cắt bên phải theo chiều rộng còn lại. Không thêm câu hỏi thứ 5 cho crop mặc định; safety check có thể thực hiện offline hoặc escalation có điều kiện.

### 3.3. Tối ưu token: kiểm chứng thay vì giả định

Preset cũ có 9 mô tả dài. 4-sided có 20 criteria ngắn, 4 instructions và shared safety context; có thể ít text token hơn nhưng cũng có thể nhiều hơn. Shared image token không tăng 4 lần. JPEG byte size không đồng nghĩa token cost. A/B test cùng ảnh/model/preprocess và log total_input_tokens thật, cả split/merge/validation calls. Nếu 4-sided tốt hơn chất lượng nhưng tốn token hơn nhẹ, báo trade-off và yêu cầu gate rõ; không quảng cáo giảm token nếu số liệu không chứng minh.

### 3.4. Guardrail và confidence

- Strict validation tất cả answer keys, options, xác suất hữu hạn hợp lệ nếu API cung cấp; thiếu hoặc unknown answer không biến thành quyết định chắc chắn. Không giả định API luôn trả cùng field names trước contract test.
- Với edge, chọn mức conservative theo confidence/margin giữa top 2; khi không chắc chắn giảm về mức nhỏ hơn đã được support hoặc 0. Threshold tune trên validation (khởi đầu thử 0.75/0.15, không phải giá trị đã hiệu chuẩn). Không nhân 4 confidence như thể các cạnh độc lập về xác suất.
- Tính retained width, height, area. Thử guardrail `min_width=0.6`, `min_height=0.6`, `min_area=0.5` (giá trị validation candidate). Nếu vượt giới hạn, giảm trim cạnh ít tin cậy trước; giữ nguyên nếu không chứng minh an toàn. Không ép aspect ratio thành 16:9 bằng crop phá nội dung.
- Nếu có reliable focus regions, box phải chứa tất cả focus bắt buộc; nếu có reliable avoid regions, dùng canonical policy và không ưu tiên tránh bubble hơn giữ chủ thể. Không tự tạo bounding box không có evidence.
- Lỗi hình học/uncertainty => FULL + static an toàn + `needs_review`/reason. Không đặt `text_detection_uncertain=False` chỉ vì nhận được choice. Tách `decision_valid`, `crop_safe`, `text_detection_uncertain` thành semantics rõ ràng; test migration/renderer consumption. Giữ fallback full cho text uncertainty nếu chưa có safe crop evidence đáng tin.
- Có thể A/B validation call chỉ cho high-risk crop (e.g. aggressive crop, thin faces, edge text), dùng một `noul` hoặc `choice` trên ảnh overlay original/candidate hỏi giữ đủ nội dung quan trọng không. Mặc định không thêm call cho mọi panel. Nếu extra call không cải thiện risk/cost, bỏ.

## 4. Classification, structure và motion

### 4.1. Một request phổ biến

Giữ `panel_group` (story art, text-only có ý nghĩa, ad/blank/logo) và `structure_action` (single, split candidate, merge candidate). Thay `center_crop_2d` bằng 4 crop questions. Có thể bỏ `motion_mode` khỏi request phổ biến nếu xác nhận rule-based motion parity. Tổng 6 câu (2 routing + 4 crop), hoặc 7 nếu giữ motion trong ablation. Model chỉ được đề xuất merge/split; code xác nhận. Với non-ACTIVE: ignore crop, không render panel; CONTEXT_ONLY vẫn trong evidence và contact sheet. Với panel uncertain classification: không DROP, đưa review, không auto-mark ACTIVE valid để vượt gate.

Crop cho split/merge parent là speculative, sẽ discard khi structural change. So sánh A: one-pass bundled (chi phí ít round trip), B: routing first, crop only final ACTIVE panels (tốn thêm image token/requests). Không mặc định B tiết kiệm. Dùng profile theo tỷ lệ non-active và repaired. Có thể hỏi structure chỉ trên candidate theo geometry heuristic nhưng phải chứng minh recall không giảm.

### 4.2. Split

- Giữ local candidate generation, đo tỷ lệ true boundary được candidate cover. Row variance thấp chỉ là candidate, không phải chứng cứ gutter. Giữ tối đa 8 như baseline trước; cải tiến local bằng width/continuity/gutter extent nếu benchmark cho thấy cần.
- Luôn có `no_split`, kể cả H/W >= 4; hỏi chọn clean boundary, không xuyên face/body/dialogue/story art. Nếu không có clean cut, giữ intact + review, không forced split. Ảnh annotated phải còn đọc được và label không che content trọng yếu.
- Với >1 cut, không giả định secondary thấy primary: validate uniqueness, min separation, min child height, VCS coordinates và coverage. Benchmark hỏi một option là tập cut candidates có giới hạn hoặc iterative split tối đa depth 1–2 nếu cần; mặc định giữ 2-pass, child còn nghi lỗi => review (không recursion vô hạn).
- Test final VCS cut có bị snapping lệch qua vùng quan trọng không; lưu predicted và applied cut. Nếu candidate không cover true boundary, ghi unresolved thay vì bịa tỷ lệ.

### 4.3. Merge

- Merge_with_prev từ ảnh đơn chỉ là candidate. Xác nhận prev/current VCS liên tục, cùng context, không merge qua ad/SKIP, không đổi thứ tự; short panel tự nó không phải fragment.
- Khi cần, gửi một composite prev/current có separator rõ hoặc context hai ảnh với câu hỏi closed-set `merge/keep/uncertain` (chỉ cho candidate). Ghi cost. Nếu chưa chứng minh, keep + review. Test first panel, adjacent fragments, split-merge conflict, text-only, cross-page valid VCS.

### 4.4. Motion và render contract

- Compute ratio từ **final accepted crop**: `H' / W' = H*(1-(T+B)/100) / (W*(1-(L+R)/100))`. Aspect threshold hiện tại 1.3; giữ baseline threshold đến khi benchmark chứng minh đổi tốt hơn.
- Ưu tiên project config (motion disabled => không pan), safe crop, duration/travel, then optional calibrated semantic motion hint nếu còn cần. Không dùng câu hỏi motion phụ thuộc câu trả lời crop trong cùng API call.
- Motion-enabled: giữ static nếu ratio <= threshold hoặc duration/travel không phù hợp. Motion-disabled: áp dụng crop đã accept cho shot static khi không xung đột focus/avoid hoặc safe tall decomposition; dùng crop-relative geometry rồi map về original normalized Region. Không cắt thêm vô ý.
- `segment.focus_box`, explicit manual override, authored VisualPlan, text uncertainty, substitution safety và avoid/focus precedence phải được audit và giữ nguyên hoặc định nghĩa rõ. Không silently overwrite approved plans. Thay crop/policy => visual fingerprint thay đổi, invalidate preview approval theo existing contract; không publish tự động.
- Verify actual generated `source_box`, motion, frame sequence, subtitle timing và video chứ không chỉ xem decision_cache. Local/Kaggle dùng cùng policy implementation.

## 5. Cache, API client, transaction và observability

- Tách provider adapter `decision_client` (HTTP, response contract, retry, auth) khỏi question builder và panel policy/pipeline. Giữ `run_decision_pipeline` là public entrypoint; tránh tạo class/facade nhiều lớp. Nếu refactor tổng đã đổi folder, đặt module vào providers/ingest tương ứng.
- Cache key content hash thực, dimensions, model, question schema/version, preprocess config/hash, request type, candidate/neighbor context hash (nếu có). Không dùng panel_id làm identity duy nhất. Lưu raw normalized answer/probabilities, usage, accepted decision + policy version; đổi threshold có thể tái đánh giá raw answer offline nếu question contract không đổi.
- Cache cả classification, split selection, merge verification, child decisions. Persist thành công từng request hoặc batch nhỏ atomic với single writer, lock chapter, tránh cache partial invalid. Resume chỉ request missing/changed. `--reclassify-all` bypass đúng tất cả caches và cả Pass 1/2, hoặc chia `--refresh-decisions`/`--reapply-policy` semantics rõ ràng. Dry run không mutate artifacts.
- Repack chỉ khi cấu trúc đổi; metadata-only classification không cần encode lại ảnh. Nếu đổi cấu trúc, giữ source identity của KEEP nếu contract cho phép; nếu renumber bắt buộc, lưu mapping VCS/hash và invalidate downstream đúng. Không phá source raw.
- Stage new manifest, panels, sheets; validate coverage, content hash, counts, paths, and mapping; commit dưới lock với recovery journal/atomic generation pointer nếu cần. Không gọi nhiều-file swap atomic nếu thực tế xóa rồi move. Nếu crash, restore generation trước hoặc resume từ verified snapshot. Dùng `apply_panel_plan(..., regenerate_sheets=False)` hoặc tương đương để generate contact sheet một lần sau final decisions.
- API client reuse connection an toàn cho concurrency (thread-local session hoặc client hỗ trợ thread safety); bounded workers mặc định hiện CLI=2, pipeline=3, thống nhất config. Global rate limiter/backoff có jitter, honor Retry-After, bounded deadline/retry. Retry 429/5xx/transient network theo policy; 400/401/403/invalid contract không retry mù. API call có thể tính phí dù timeout; log retry/unknown usage, không giả định zero. Không log credential, base64 hay full private manga image.
- Token counter cho mọi request: classification, split, child, optional verification, retry observed; cache hit = 0 new usage; missing usage = unknown không phải 0. Log payload bytes, dimensions, question count, input tokens, latency, retries, group counts, confidence buckets, fallback/review counts và reasons. CLI JSON summary dễ kiểm chứng. Giá configurable, report cost ước tính theo current pricing, không thay hóa đơn provider.
- Preprocessing: thử same JPEG82 + max height1024 baseline, adaptive max side/pixel budget và tall-panel detail policy (e.g. downscale vừa đủ hoặc overview + detail only for uncertain). Đo token thật và classification/crop/split quality; không mặc định ảnh nhỏ hơn luôn tốt. Không gửi toàn bộ chapter contact sheet vào một request nếu chưa test spatial attribution, image detail và chi phí.

## 6. Benchmark và kiểm thử chất lượng đầu ra (bắt buộc)

### 6.1. Baseline / Dataset

Pin commit, model ID, questions, preprocessing, config, retry, code and test seed. Save current raw decisions and actual rendered frames on same fixture; baseline A là 9 presets, không tự sửa baseline trong quá trình so sánh. Baseline B nếu cần là 9 presets + deterministic safety, để tách lợi ích của crop method và safety guardrail.

Khởi đầu đề xuất 150–300 panels từ 3–5 bộ truyện hợp quyền sử dụng, nhiều chapter, genre/style, panel geometry; có thể dùng dữ liệu sẵn. Cố gắng có >=30 samples cho mỗi nhóm hiếm quan trọng (text-only/skip, structural defect) và đủ crop edge cases; nếu không đủ ghi rõ CI và giới hạn kết luận. Không yêu cầu xây dataset lớn trước smoke test. Smoke 20–30 panel trước, nếu fail sửa rồi mới full benchmark. Tách train/tune (validation) và held-out test theo chapter/truyện; không tune trên test. Ghi source SHA; chỉ chạy live calls với credentials/budget được phép, không lưu token vào repo.

Annotate manual ground truth: group, structural action, true boundary windows (safe cut intervals) / merge pair, required content boxes/masks (faces, action, meaningful text if relevant), optional dispensable margins, acceptable edge ranges / one or more acceptable boxes, motion preference under agreed config. Crop có nhiều đáp án đúng: không đánh giá chỉ bằng exact-match một box. Hai người review độc lập nếu có thể; nếu một người, disclose. Review blind A/B order, tie allowed, adjudicate disagreements. Khóa annotation trước held-out benchmark.

### 6.2. Ablation matrix

| ID | Policy | Mục đích |
|---|---|---|
| A | Existing 9 presets / 4 questions | Frozen baseline |
| B | 9 presets + new safety | Isolate validation/guardrail effect |
| C | 4-sided + existing motion question | Isolate crop improvement |
| D | 4-sided + deterministic motion + guardrails | Main candidate |
| E | D + conditional verification for high-risk cases | Measure extra accuracy vs cost |
| F (optional) | D routing-first vs bundled / adaptive preprocessing | Optimize input cost after quality gate |

Run same panels/settings, randomized order where practical, independent cache namespaces, cold cache and warm cache separately. Repeat subset >=3 times to measure consistency if budget permits. Record API unavailable/failures explicitly. Compare total chapter, not just successful calls. Freeze best policy on validation then held-out test once; retain report and sample before/after images.

### 6.3. Quality metrics

| Task | Metric |
|---|---|
| Group | Accuracy, macro F1, confusion matrix, per-class precision/recall, story->SKIP catastrophic errors |
| Split/Merge | Action macro F1, boundary precision/recall/F1 within annotated safe intervals, candidate recall, false split continuous scene, false merge unrelated panels, content coverage and source order |
| Crop | Required region retention fraction and catastrophic cut rate; safe crop rate; dispensable area removed conditional on safe crop; normalized edge MAE against acceptable ranges; best IoU to acceptable boxes as secondary; blind preference win/tie/loss |
| Motion | Correct static/pan by project mode, scroll starts/ends, full intended content coverage, pan speed, fallback frequency |
| Render | FFprobe dimensions/FPS/duration, no corrupt frames, no clipping of must-keep content, subtitle/audio sync, preview approval invalidation, static/animation visual comparison |
| Confidence | Reliability plot / ECE or Brier if probabilities support; risk vs coverage, needs-review rate; no synthetic confidence |
| Cost | Mean/median/p95 input tokens per panel, per final ACTIVE panel, per chapter (including all phases), request count, retry/unknown usage, estimated USD |
| Speed | p50/p95 request latency, cold/warm chapter wall time, cache hit, peak memory/disk; concurrency throughput |

Report counts/denominators, per-stratum results, paired bootstrap CI by chapter if feasible. A single pretty panel is not proof. Save `metrics.json`, `report.md`, per-panel JSONL/CSV, representative contact sheets of original/baseline/challenger/final render; no copyrighted image redistribution outside allowed private workspace.

### 6.4. Suggested acceptance gates (proposed, calibrate with baseline)

- Hard: no invalid Region, out-of-bound pixel crop, modified source image, lost VCS coverage, reordered narrative, unverified DROP, auto approval bypass, mock result in production, silent missing answer, corrupt media. 100% deterministic invariants/tests pass.
- Held-out: zero catastrophic crop of annotated mandatory regions and zero story->SKIP observed in test set; if baseline has violations, fix them; sample zero is not guarantee for unseen panels. Group and structure macro F1 no worse than A by >1 percentage point (or pre-agreed noninferiority margin; report uncertainty). Candidate recall not degrade. If quality insufficient, keep conservative fallback and do not promote candidate.
- Crop challenger: safe-crop rate >= A; improve dispensable-area removal among safe crops or reviewer preference (target >=60% among non-ties, report sample/CI); report ties and full-image fallback so always-full cannot win by default.
- Cost: target total cold input tokens <= baseline, stretch -15%; target warm unchanged rerun zero additional calls. If cost rises but quality materially improves, report trade-off and seek explicit acceptance rather than claim optimization. p95/wall-time no substantial regression (initial <=10% tolerance, tune after baseline); report rate-limit effects.
- Review frequency bounded and reported; don't pass by sending every panel to manual review. Separate hard safety from comparative KPI; no fabricated metric.

### 6.5. Automated tests

Unit: all 625 edge combinations (geometry/guardrails), 0/10/20/30/40 mapping and rounding on odd dimensions, 0 full region, all 40 fallback, invalid keys/types/NaN/probability missing, unknown API option, group safety, confidence conservative fallback, deterministic motion based on final box, precedence of manual/authored/uncertain metadata, model/prompt/preprocess invalidation. Property test monotonic normalized bounds and no source mutation. Use synthetic colored margins/marker boxes (not only API mocks).

Mock contract/integration: valid response and malformed response; 429 Retry-After, 500, timeout, 401, retry budget, concurrent failure resume; cache after partial failure; cache invalidation changed SHA/model/question/context, force refresh; split no candidates and no_split on tall continuous, duplicate cut, missing true candidate, VCS snap, merge previous verification, child still needs repair; no duplicate requests for unchanged panels; usage includes split/child/verification; repeated run produces identical decision artifacts or explicitly invalidates; one contact-sheet generation; crash at repack/manifest boundary and recovery; no secret in log/payload/cache.

End-to-end: raw -> segment -> decide -> contact sheets -> agent evidence -> visual plan -> render (both static/animation, local/Kaggle path or worker fixture) -> review fingerprint. Compare source/accepted crop overlays and sampled real rendered frames; verify ACTIVE only visually rendered, CONTEXT_ONLY narrative preserved, no SKIP contact sheets, subtitle/audio constraints; invalidation on changed crop. Use existing test suite plus new tests. Run actual small live Decision API benchmark only after offline tests pass and user-authorized budget/credentials available; record failure if blocked, never substitute mock for accuracy.

## 7. Implementation phases (coding agent checklist)

### D0 — Freeze baseline and harness (P0)
- [ ] Pin commit, inspect existing tests and all consumers of `center_crop_preset`, `display_crop`, `text_detection_uncertain`, `tall_motion_hint`, cache and VCS.
- [ ] Run existing tests; record commands/results and known failures; create baseline fixture and metrics harness.
- [ ] Capture current real response schema and usage with bounded smoke test if available.
**Exit:** reproducible baseline, known contract, labeled smoke fixture. No live result => mark blocked, offline harness still works.

### D1 — Provider contract/cache/telemetry (P0)
- [ ] Separate thin client, question builders and pipeline logic; strict response validation, confidence preservation, bounded retry/rate-limit.
- [ ] Version cache, all phases usage, incremental resume, explicit refresh semantics.
- [ ] Contract + error + cache tests. Do not rewrite unrelated providers.
**Exit:** no silent defaults, cache-safe, measurable cost; existing gates preserved.

### D2 — 4-sided Crop + safety (P0)
- [ ] Implement 4 questions, exact 5 options, shared context and normalized mapping; keep A/B switch internal.
- [ ] Add deterministic guardrail, confidence policy and optional high-risk verify only if needed.
- [ ] Run all 625 combinations, synthetic fixtures, live small A/B with manual review.
**Exit:** crop contract valid and visually safe on smoke set; cannot pass just by comparing normalized boxes.

### D3 — Structural and render correctness (P0)
- [ ] no_split always, split selection guard, candidate diagnostics, merge verify only candidate; preserve two-pass and VCS provenance.
- [ ] Motion derived from accepted geometry, static branch uses valid crop, precedence rules consistent; ensure uncertainty is truthful.
- [ ] Preserve original panels for OCR/narrative, same render path in Kaggle/local, approval invalidation.
**Exit:** structural tests + actual rendered frames in both modes pass.

### D4 — I/O and throughput (P1)
- [ ] Metadata-only fast path, stable identity or provenance mapping; staged repack recovery; generate final contact sheets once.
- [ ] Benchmark concurrency, image preprocessing, bundled vs conditional routing. Keep simplest validated candidate.
**Exit:** measured end-to-end token/latency/I/O report; no artifact loss or duplicates.

### D5 — Held-out A/B evaluation (P0)
- [ ] Run matrix, frozen thresholds; quality, cost, speed, confidence and render metrics.
- [ ] Save report and failed examples with fixes limited to validation; rerun held-out only with new version clearly labeled.
- [ ] Promote challenger only if gates met; otherwise preserve baseline or safe FULL fallback and document blocker.
**Exit:** evidence-based choice, not unverified target.

### D6 — Integration and cleanup (P1)
- [ ] Remove unused 9-preset prompt and runtime legacy branch after accepted benchmark; retain migration reader/rollback fixture as needed.
- [ ] Update SKILL.md/vision reference/README/CLI help: one canonical workflow, no duplicated question instructions in multiple docs. `decide` report includes review needs and cost.
- [ ] Update prior plan sequencing and tests; no additional user-facing question for engineering settings unless user-facing behavior changes.
**Exit:** concise code, one source of truth, complete regression suite, plan report.

## 8. Target architecture (illustrative; align with overall refactor)

```text
providers/decision.py     # transport, schema normalization, usage
 ingest/decision.py       # question contract + routing + orchestration
 ingest/panel_repack.py   # deterministic VCS, candidate generation, transaction
 media/visual_policy.py   # accepted display crop, safe geometry, motion
 tests/decision/          # contract, crop, structure, cache, render integration
 benchmarks/decision/     # fixtures/annotations/harness/report (private data excluded)
```

Can keep current paths in this PR if moving would cause unnecessary churn. No new service hierarchy required. Avoid writing a generic framework for a single provider. CLI thin, API adapter does not know manga semantics, render does not call external Decision API, Kaggle receives immutable approved/prepared input.

## 9. Definition of Done / deliverables

- [ ] Implementation and short migration notes; source audit with exact commit; tested question schema and cache version.
- [ ] 4-sided Crop contract with four independent choice questions in a single call; correct accepted `display_crop` at render time.
- [ ] Strict uncertain/invalid response and review policy, split/merge safeguards, deterministic motion validated.
- [ ] Regression tests + benchmark report comparing A/B with actual output frames, costs, latency, errors, confidence, and human labels.
- [ ] No repeated work on unchanged panels; safe recovery; single final contact-sheet pass; no bypass of review.
- [ ] Measured results, limitations and any failed acceptance gate reported. Do not report unrun tests as passed. If live API unavailable, mark quality/cost acceptance BLOCKED, ship no default policy change justified only by mocks.
- [ ] Backward compatible old artifacts or explicit migration with approval invalidation; rollback path to frozen baseline. No repository change outside Decision/render dependency scope unless necessary and documented.

## 10. Source references

- https://github.com/nxc1802/Manga-Recap-Skill/blob/main/src/core/decision_engine.py
- https://github.com/nxc1802/Manga-Recap-Skill/blob/main/src/core/panel_repack.py
- https://github.com/nxc1802/Manga-Recap-Skill/blob/main/src/render/visual_policy.py
- https://github.com/nxc1802/Manga-Recap-Skill/blob/main/src/cli/main.py
- https://blog.google/innovation-and-ai/technology/developers-tools/gemini-decisions-api/
- https://codelabs.developers.google.com/getting-started-with-gemini-decisions-api?hl=en

**Implementation decisions requiring verification:** confidence threshold, crop retention limit, motion rule replacement, structural ratio thresholds, dataset size, and quantitative acceptance targets above are proposed engineering defaults, not measured outcomes. Source audit is static; no live API calls, test execution, or repo edits were performed in preparation of this plan.
