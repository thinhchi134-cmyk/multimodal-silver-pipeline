# Hướng dẫn kỹ sư xử lý file và bàn giao kết quả Silver

**Mục tiêu:** dùng AI đọc file gốc theo template rồi bàn giao kết quả Silver. **Mỗi record trong template tương ứng một file kết quả `.json` riêng.** “Gán” ở đây là điền dữ liệu Silver; nhãn trong file chỉ là đề xuất.

**Phạm vi công việc của kỹ sư:** nhận template và file gốc → xử lý AI/OCR → kiểm tra nội dung → bàn giao JSON cùng báo cáo kết quả.

**Bắt buộc:** kỹ sư phải thực hiện đọc AI/OCR và kiểm tra kết quả trước khi bàn giao. Ảnh và các trang PDF scan phải được đọc bằng AI có khả năng đọc ảnh hoặc công cụ OCR phù hợp; PDF có lớp chữ được trích xuất trực tiếp và kiểm tra các trang còn thiếu. Không bàn giao file chưa đọc với lý do “cần OCR”.

## 1. Giữ nguyên thông tin gốc

Không sửa: `schema_version`, `processing_batch_id`, `source_id`, `scan_id`, `observation_id`, `document_id`, `version_id`, `content_hash`, `file_path`, `file_name`.

Mở đúng file tại `file_path`. Nếu file đã đổi so với bản Bronze, yêu cầu quét lại và xuất template mới; không tự sửa hash. Giữ nguyên thông tin nhận diện của từng record khi xử lý và bàn giao.

## 2. Dùng AI đọc và điền kết quả

| Trường | Điền gì? |
|---|---|
| `title` | Tiêu đề dễ hiểu, đúng tài liệu |
| `processing.parser_name` | Tên công cụ/model thực tế đã dùng |
| `processing.parser_version` | Phiên bản công cụ/model thực tế |
| `processing.config_hash` | SHA-256 của cấu hình xử lý, gồm prompt và tùy chọn liên quan |
| `status` | Chọn theo bảng bên dưới |
| `content_complete` | `true` chỉ khi `SUCCESS` và đã đọc đủ |
| `page_count` | Số trang nếu biết; không biết để `null` |
| `chunks` | Nội dung thực tế đã đọc, chia thành các đoạn |
| `proposed_label_codes` | Mã nhãn đã có trong danh mục; chưa gán để `[]` |
| `detail` | Ghi phần thiếu, giới hạn hoặc lý do lỗi |

Không để `FILL_IN`. Văn bản phải giữ đúng ý nghĩa, số liệu, mã và đơn vị. Không đoán chữ mờ, không thay văn bản gốc bằng bản tóm tắt. Với bản vẽ, không ghi đã đọc đủ nếu mới lấy được chữ mà còn bỏ sót thông tin cần xử lý.

| Kết quả thực tế | `status` | `content_complete` | `chunks` |
|---|---|---|---|
| Đã đọc AI/OCR khi cần và kiểm tra đầy đủ — đạt để bàn giao | `SUCCESS` | `true` | Có nội dung |
| Đã xử lý nhưng chỉ đọc được một phần — cần xử lý bổ sung | `PARTIAL` | `false` | Có phần đọc được |
| Đã thử nhưng xử lý bị lỗi — cần sửa hoặc thử công cụ khác | `FAILED` | `false` | `[]` |

`PENDING` và `NEEDS_OCR` là việc chưa làm xong, không đưa vào file bàn giao đã hoàn thành. Nếu công cụ không hỗ trợ định dạng (`UNSUPPORTED`), phải tìm cách đọc/chuyển đổi phù hợp hoặc báo riêng để thống nhất cách xử lý; không tính là đã hoàn thành. `PARTIAL` và `FAILED` chỉ dùng trong báo cáo tồn đọng, không tính là đạt nghiệm thu. Không đổi trạng thái thành `SUCCESS` để bỏ qua lỗi hoặc phần chưa đọc được.

## 3. Ví dụ phần cần điền

Giả sử AI đọc ảnh và thấy “Dự án: Mercure Đà Lạt”. **Đây chỉ là minh họa, không phải kết quả đọc file CHECK_12 của bạn.** Thay các giá trị công cụ/phiên bản/hash bằng giá trị thật. Ghép các trường sau vào bản ghi gốc, giữ nguyên các trường ở mục 1:

```json
{
  "title": "Thông tin dự án Mercure Đà Lạt",
  "processing": {
    "parser_name": "TEN_CONG_CU_AI_THUC_TE",
    "parser_version": "PHIEN_BAN_THUC_TE",
    "config_hash": "SHA256_CAU_HINH_THUC_TE"
  },
  "status": "SUCCESS",
  "content_complete": true,
  "page_count": 1,
  "chunks": [
    {
      "chunk_no": 1,
      "page_number": 1,
      "section_title": null,
      "content": "Dự án: Mercure Đà Lạt"
    }
  ],
  "proposed_label_codes": [],
  "detail": "Đã đối chiếu và đọc đầy đủ nội dung ảnh."
}
```

Mỗi chunk có 1–20.000 ký tự. Đánh `chunk_no` liên tục từ 1. Ưu tiên chia theo trang/mục/đoạn; không bỏ hoặc lặp nội dung. `page_number` bắt đầu từ 1, không xác định được để `null`.

## 4. Bàn giao file kết quả

1. Thực hiện đọc AI/OCR cho toàn bộ tài liệu trong nhóm bàn giao. Đối chiếu kết quả với file gốc, đặc biệt chữ mờ, số liệu và trang bị bỏ sót.
2. Với **mỗi record trong template**, tạo **một file `.json`** chứa một đối tượng JSON đầy đủ: các trường nhận diện gốc ở mục 1 và toàn bộ trường kết quả đã điền ở mục 2. Không chỉ lưu riêng phần ví dụ ở mục 3, không gom nhiều record thành một mảng hoặc một file JSONL.
3. Lưu bằng UTF-8, đặt tên `<document_id>__<version_id>.json` để phân biệt tài liệu và phiên bản. JSON có thể xuống dòng, thụt lề để dễ đọc; không có comment hoặc dấu phẩy thừa.
4. Bỏ các file đã hoàn thành (`SUCCESS`, `content_complete: true`, nội dung đã kiểm tra) vào thư mục `silver-results/`. Giữ nguyên `processing_batch_id` theo từng record gốc. Có thể bàn giao một nhóm nhỏ đã hoàn thành trước.
5. Gửi kèm `bao-cao-ban-giao.md`: tổng số record trong nhóm được giao xử lý, số hoàn thành, số còn tồn đọng; liệt kê ID, trạng thái, nguyên nhân và bước xử lý tiếp theo của từng tài liệu chưa đạt. Không âm thầm bỏ sót record; tổng số hoàn thành và tồn đọng phải khớp số được giao.

Ví dụ tên file kết quả cho record CHECK_12:

```text
silver-results/
  93c8430e-ec50-5b21-9322-6e5278c3777d__cc31d0a4-9d7a-5ff2-8c28-dcb42bdd71a9.json
bao-cao-ban-giao.md
```
