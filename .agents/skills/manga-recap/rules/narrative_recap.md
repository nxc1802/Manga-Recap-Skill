# Narrative Storytelling & Dialogue Lồng Thoại Rules

## 1. Triết lý Dẫn Chuyện: Từ "Tóm tắt (Summarize)" sang "Kể chuyện & Lồng thoại (Storytelling & Direct Dialogue)"

1. **Tuyệt đối KHÔNG tóm tắt máy móc ở ngôi thứ ba**:
   - *Lỗi phổ biến cũ:* Đọc bóng thoại của nhân vật rồi tóm tắt lại bằng lời dẫn gián tiếp khô khan (*"Thạch đại phu thông báo một tin chấn động rằng Lưu lão gia đã trúng phải kỳ độc..."*).
   - *Phong cách chuẩn:* **Kể chuyện truyền cảm kết hợp lồng thoại trực tiếp**. Agent phải đưa thẳng câu thoại của nhân vật vào kịch bản với đầy đủ ngữ khí, sắc thái cảm xúc (*"Thạch đại phu biến sắc, bất giác lùi lại một bước, giọng run rẩy: 'Lưu lão gia... trong người ngài có một cỗ hàn độc vô cùng quỷ dị!'"*).
2. **Quy tắc N:M Panel Mapping & Nhịp Điệu Điện Ảnh (Cinematic Pacing)**:
   - **Xóa bỏ ràng buộc cứng 1:1**: Kịch bản không bị ép buộc máy móc mỗi panel phải là một segment riêng biệt. Quan hệ giữa Panel và Segment là quan hệ **N:M** linh hoạt:
     - **1:1 (Tiêu chuẩn)**: 1 segment tương ứng 1 panel chính (nhịp kể chuyện kịch tính, lắng đọng).
     - **1:N (Gộp nhiều panel / Multi-panel Segment)**: 1 segment tham chiếu 2, 3 hoặc nhiều panel trong `panel_ids` (ví dụ `["panel_002.jpg", "panel_003.jpg"]`). Thích hợp cho các cảnh giao chiến dồn dập, chuỗi phản ứng nhanh của nhiều nhân vật, hoặc khi dải tranh bị phân tách rãnh vẽ (gutter split).
     - **N:1 (Tái sử dụng panel / Panel Reuse)**: Nhiều segment liên tiếp có thể tái sử dụng cùng 1 panel (`panel_ids: ["panel_001.jpg"]`). Thích hợp khi nhân vật đối thoại qua lại kéo dài trên cùng bối cảnh, hoặc độc thoại nội tâm sâu sắc mà khung hình không cần đổi.
     - **Lược bỏ panel thứ yếu (Selective Omission)**: Các panel thuần túy là khoảng trắng chuyển trang, phong cảnh lặp lại không có diễn biến, hoặc logo/quảng cáo của nhóm dịch trong `manifest.json` có thể được lược bỏ khỏi `script.json` một cách hợp lệ.
   - **MỆNH LỆNH BẮT BUỘC — Giữ trọn panel mang thông tin cốt lõi (Essential Panel Retention)**:
     - Tất cả các panel mang thông tin quan trọng (diễn biến cốt truyện, manh mối then chốt, lời thoại quyết định, đòn thế tung ra, biểu cảm chuyển biến) **BẮT BUỘC phải được giữ lại và tham chiếu trong video**.
     - Agent tuyệt đối không được lược bỏ các panel cốt lõi của câu chuyện.
   - **Tôn trọng nguyên tác & Kể chuyện trung thực (Faithful Storytelling)**:
     - Tuyệt đối không bịa đặt (hallucinate) tình tiết, không chế thêm cảnh giới võ công hoặc quan hệ nhân vật sai lệch với nguyên tác truyện tranh.
     - Thời lượng tối ưu để người xem kịp cảm nhận tranh và nghe thoại là từ **4 đến 8 giây** cho mỗi visual beat (50–250 ký tự thuyết minh).
3. **Quy tắc mở đầu 5 giây (The 5-Second Golden Hook)**:
   - Segment 001 bắt buộc phải thu hút sự chú ý của người nghe ngay trong **5 giây đầu tiên** (không chào hỏi rườm rà).
   - Đi thẳng vào cao trào hoặc câu thoại đắt giá: *"Giữa đêm đen mù mịt, một tiếng đao kiếm xé toạc sự tĩnh lặng!"*

---

## 2. Cấu Trúc Khuyến Nghị Đầy Đủ Cho 1 Panel Script (4-Part Standard Framework)

Để kịch bản đạt độ dày, giàu hình ảnh và giúp khung hình lưu lại vừa vặn cho người xem thưởng thức nét vẽ, một phân đoạn kịch bản đầy đủ được khuyến nghị kết hợp 4 thành phần sau:

```text
[1. Bối cảnh / Trạng ngữ] ──► [2. Chủ thể & Thần sắc] ──► [3. Hành động / Đòn thế] ──► [4. Lời thoại trực tiếp]
(Thời gian, không gian,      (Nhân vật, ngoại hình,     (Động tác, biến chuyển     (Lời nói nguyên văn,
 địa điểm, biến chuyển)       biểu cảm, ánh mắt)          nội tâm, khí tức)          ngữ khí kịch tính)
```

### Chi tiết 4 thành phần:
1. **Trạng ngữ thời gian / không gian / địa điểm:** Xác định bối cảnh xảy ra sự việc (*"Dưới ánh hoàng hôn đỏ quạch buông xuống sườn đồi..."*, *"Lúc này bên trong mật thất Tế Thế Đường..."*, *"Ngay khi tiếng gió rít vừa dứt..."*).
2. **Chủ thể & Thần sắc / Biểu cảm / Trang phục:** Miêu tả nhân vật xuất hiện, trang phục, khí chất, ánh mắt (*"...Thạch đại phu với vầng trán lấm tấm mồ hôi lạnh..."*, *"Tiêu Phàm siết chặt chuôi kiếm cổ, ánh mắt sắc như chim ưng..."*).
3. **Hành động & Biến chuyển trạng thái / Đòn thế:** Khắc họa động tác thể chất hoặc biến chuyển khí tức (*"...khẽ thu ngón tay lại, toàn thân chấn động dữ dội..."*, *"...đột ngột xoay người phóng vụt lên không trung, tung ra một quyền xé rách màn đêm..."*).
4. **Lời thoại trực tiếp / Độc thoại nội tâm:** Đọc trọn vẹn lời thoại trong bóng thoại với ngôn từ thuần Việt, xưng hô tôn ti trật tự (*"...hắn gằn giọng quát lớn: 'Muốn động vào người của ta, trước tiên phải bước qua xác ta đã!' "*).

> [!NOTE]
> **Tính linh hoạt:** Không phải panel nào cũng bắt buộc nhồi nhét đủ cả 4 thành phần. Ví dụ:
> - Panel đối thoại dồn dập: Có thể chỉ gồm *[Thần sắc] + [Lời thoại trực tiếp]*.
> - Panel đòn đánh võ thuật: Tập trung *[Trạng ngữ] + [Chủ thể] + [Hành động/Hiệu ứng]*.
> - Panel mở cảnh: Tập trung *[Trạng ngữ] + [Không gian]*.

---

## 3. Khuyến Nghị Độ Dài Kịch Bản (Script Length Guidance)

- **Khoảng khuyến nghị tối ưu:** **50 đến 250 ký tự** cho mỗi câu thoại/lời dẫn.
  - Tương đương **3 đến 12 giây** âm thanh đọc tự nhiên ở tốc độ 1.0x.
  - Đây là khoảng nhịp thở lý tưởng cho engine VieNeu-TTS phát huy tối đa ngữ điệu và tránh tràn dòng phụ đề.
- **Không ép buộc cứng (Soft Guideline):** Khoảng 50–250 là khuyến nghị trong Prompt chỉ đạo cho Agent, **không phải là điều kiện validate lỗi cứng**. Một câu thoại ngắn gọn đầy uy lực (ví dụ: *" 'Cút!' "* - kèm hành động vung kiếm) hoặc một câu miêu tả bối cảnh giàu chất thơ dài hơn một chút đều hoàn toàn hợp lệ miễn là tự nhiên.

---

## 4. Chiến Lược Giọng Đọc (Voice Strategy)

- **Chiến lược duy nhất: `single_narrator` (Một giọng đọc thống nhất)**:
  - Một giọng đọc duy nhất (Mặc định: **Ngọc Huyền** - Nữ miền Bắc thanh lịch, biểu cảm cao) đảm nhận toàn bộ việc dẫn chuyện kiêm lồng thoại cho tất cả các nhân vật.
  - Sử dụng ngữ điệu, nhịp ngắt và đại từ nhân xưng chuẩn để phân biệt nhân vật mà không cần chuyển giọng phức tạp.
- **Tùy chọn `emotion_aware` (Cảm xúc phụ trợ)**:
  - Cho phép chèn các thẻ cảm xúc âm thanh được hỗ trợ trong VieNeu-TTS v3 Turbo:
    - `[cười]`: Tiếng cười khẽ, mỉa mai hoặc sảng khoái.
    - `[thở dài]`: Tiếng thở dài trĩu nặng của tiền bối, người mệt mỏi.
    - `[hắng giọng]`: Tiếng hắng giọng ngắt lời trang nghiêm.
  - Dấu chấm lửng `...` tạo khoảng dừng lắng đọng (0.4–0.6s). Dấu gạch ngang `—` tạo ngắt nhịp kịch tính.

---

## 5. Bảng So Sánh Kịch Bản Trước và Sau Nâng Cấp

| Panel Tình Huống | Kịch Bản Cũ (Tóm tắt khô khan, chạy quá nhanh) | Kịch Bản Mới (Kể chuyện & Lồng thoại chuẩn 4 phần) |
|---|---|---|
| **Panel Đại phu bắt mạch** | "Thế nhưng bên trong Tế Thế Đường, không khí lại chùng xuống nặng nề khi thầy thuốc Thạch đại phu bắt đầu bắt mạch cho một lão nhân." *(Gom 3 panel, chạy vèo trong 5.8s, mất thoại)* | "Giữa gian phòng tĩnh mịch của Tế Thế Đường, Thạch đại phu khẽ đặt ngón tay lên cổ tay Lưu lão gia. Cảm nhận từng đợt chân khí hỗn loạn cuộn trào, ông giật mình biến sắc: 'Mạch tượng này... rốt cuộc ngài đã luyện tà công gì?'" *(1 Panel = 1 Segment, đủ 4 phần, lưu hình 6.5s)* |
| **Panel Nhân vật phản diện** | "Tên thủ lĩnh hắc y nhân ra lệnh cho thuộc hạ bao vây ngôi nhà và tiêu diệt mục tiêu." *(Tóm tắt ngôi 3)* | "Từ trên nóc nhà đối diện, tên thủ lĩnh áo đen chậm rãi rút đao, ánh mắt lóe lên tia sát khí lạnh lẽo. Hắn gằn giọng ra lệnh: 'Giết sạch không tha, một con chó cũng không được để sống sót!'" *(Lồng thoại trực tiếp, kịch tính)* |
| **Panel Đòn đánh võ thuật** | "Hai bên giao chiến ác liệt làm vỡ nát bàn ghế xung quanh." *(Báo cáo vụ việc)* | "Trong chớp mắt, quyền phong mãnh liệt của Lão gia chủ xé toạc không khí, đánh thẳng vào ngực đối phương. Một tiếng nổ kinh hoàng vang lên, làm vỡ vụn toàn bộ bàn ghế trong đại sảnh!" *(Giàu từ ngữ gợi hình, hành động dồn dập)* |

---

## 6. Thẩm Định Chất Lượng Kể Chuyện do Con Người Dẫn Dắt (Human-Driven QA)

1. **Tuyệt đối không xây dựng Automated Quality Benchmarks**:
   - Hệ thống **không** sử dụng các thuật toán chấm điểm tự động, OCR accuracy score, narrative evaluation score, hay LLM-as-judge prompts.
   - Không thể dùng máy móc để đánh giá độ hấp dẫn, nhịp điệu kể chuyện hay cảm xúc của diễn biến kịch tính.
2. **Quyền quyết định tối cao thuộc về Human Reviewer**:
   - Mọi tiêu chí về: độ trung thực của cốt truyện (faithful storytelling), việc giữ trọn các panel cốt lõi (essential panel retention), cách xưng hô nhân vật, và cảm xúc biểu đạt đều do con người trực tiếp thẩm định.
   - Người duyệt kiểm tra dựa trên **Human Review Pack** (`review_pack.md` / `review_pack.html`): đối chiếu contact sheet, bảng mapping `seq_id` $\rightarrow$ `panel_ids` $\rightarrow$ lời thoại, danh sách panel bị lược bỏ, và video/audio preview.
3. **Phản hồi và Vòng lặp cải tiến (Feedback Loop)**:
   - Khi cần chỉnh sửa, Human Reviewer cung cấp phản hồi gắn với `seq_id` hoặc `panel_ids` cụ thể (ví dụ: *"Segment 002: điều chỉnh ngữ khí nhân vật sang giận dữ"*).
   - Agent tiếp nhận phản hồi và sửa đổi có mục tiêu vào `script.json`, tận dụng cơ chế audio cache để chỉ render/synthesize lại đúng các phân đoạn bị ảnh hưởng.

