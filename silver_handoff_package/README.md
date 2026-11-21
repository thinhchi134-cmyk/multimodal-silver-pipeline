# Silver Finalize — bàn giao tích hợp (PoC, 2026-11-21)

## Phạm vi

Pipeline hiện tại **đã chạy** trên máy thử nghiệm của người tạo dữ liệu: `719 assets`, `391 text chunks`, `623 image vectors` ở `search_index_image_v3`. Đây là số đo do người vận hành cung cấp, **không phải** kết quả chạy lại trên bộ raw trong gói này. Đây là một **bước bổ sung độc lập** để kiểm tra Silver và xuất bảng sang Parquet; **không thay thế** `fill_silver.py`, `search_silver.py` hoặc sửa JSONL template.

- **Bronze:** `03_PROJECTS/03_PROJECTS` — dữ liệu file gốc (read-only).
- **Silver snapshot:** `silver_03_projects/silver_filled_new.jsonl` — dữ liệu parser đã điền, giữ nguyên (read-only).
- **Search index hiện có:** `search_index_image_v3` — embeddings SigLIP2, SQLite FTS5, refs (read-only).
- **Gói bổ sung này:** `silver_finalize.py` → một **thư mục output MỚI** gồm báo cáo QA, Parquet của CSV/XLSM, manifest và issues.

### Ranh giới trách nhiệm

`Silver` = normalized asset metadata + parsed content/data references + lineage + status. `Search index` = materialized index tạo từ Silver/Gold/ảnh Bronze. **Embedding không bắt buộc nằm trong Silver JSONL; caption AI không bắt buộc cho mỗi ảnh.** `Gold` là bảng KPI / data product nghiệp vụ được tạo riêng sau này.

## Cài đặt Windows PowerShell

Giả định thư mục:

```text
D:\test silver\
├── 03_PROJECTS\03_PROJECTS\          # raw
├── silver_03_projects\silver_filled_new.jsonl
├── search_index_image_v3\           # index ảnh đã có
├── silver_search_poc\search_silver.py
└── silver_handoff_package\
    ├── silver_finalize.py
    ├── requirements.txt
    └── README.md
```

Trong PowerShell:

```powershell
cd "D:\test silver\silver_handoff_package"
..\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Nếu môi trường ảo nằm nơi khác, thay đường dẫn kích hoạt `.venv` tương ứng.

**Chạy:**

```powershell
python silver_finalize.py `
  --silver "..\silver_03_projects\silver_filled_new.jsonl" `
  --raw-root "..\03_PROJECTS\03_PROJECTS" `
  --image-index "..\search_index_image_v3" `
  --out "..\silver_quality_handoff_v1"
```

**Chú ý:** `--out` phải là **thư mục chưa tồn tại**. Mỗi lần chạy dùng `v2`, `v3`... nếu output đã có. Không dùng `--out` trong thư mục Bronze.

Dùng `--max-files 10` để chạy smoke test (lưu vào output mới), hoặc `--verify-hash` để so SHA256 toàn bộ file (chậm hơn và tăng disk I/O). Không bắt buộc SHA256 trong lần chạy đầu.

## File output

```text
silver_quality_handoff_v1/
├── QUALITY_REPORT.md         # tóm tắt pass/gap, số lượng file
├── quality_summary.json      # máy đọc được
├── asset_audit.jsonl         # mỗi asset 1 dòng, id/status/path/vector coverage
├── issues.jsonl              # cảnh báo/ERROR có file_id, dễ triage
├── tables_manifest.jsonl     # nguồn bảng ↔ Parquet, tên cột, số dòng
├── special_metadata.jsonl    # SVG text/size, archive inventory, unsupported notes
└── tables/
    ├── <version_id>.parquet
    └── <version_id>_sheet_001.parquet
```

`asset_audit.jsonl` là **bản báo cáo bổ sung**, không ghi đè `silver_filled_new.jsonl` và không ngầm đổi `status` cũ. Trạng thái `FAILED` của 2 PNG lớn vẫn có thể tồn tại trong Silver cũ trong khi index v3 đã có vectors (lịch sử parser và indexing là **hai công đoạn khác nhau**).

## Hỗ trợ theo loại file

| Định dạng | Silver trước đây | Gói bổ sung làm gì? | Cần thêm cho production |
|---|---|---|---|
| JPG/JPEG/PNG/TIF/WEBP/JFIF | Image metadata, một số FAILED | QA + đối chiếu `version_id` với index v3 | Kiểm tra hình ảnh thực tế, model accuracy; đánh dấu `CHECK_12.png` nếu vẫn mất |
| PDF/PPTX/MD/LOG | Text chunks khi parser đọc được | QA số chunk và references | Kiểm thử text/page; OCR PDF scan, text slide dạng ảnh |
| CSV/TSV | Schema chunks | Trích **toàn bộ hàng** sang Parquet theo streaming | Kiểm tra separator/encoding, data types, units, nulls, schema mapping |
| XLSX/XLSM | Worksheet schema | Từng sheet → Parquet theo streaming | Formulas cached values có thể null/stale, không chạy macro |
| SVG | Unsupported | Parse XML an toàn để lấy kích thước, text nodes | Raster preview/visual embedding nếu cần |
| ZIP/7Z | Unsupported | Chỉ lập danh sách members, **không extract** | Security scanning, giới hạn archive bomb, quyền truy cập |
| RAR | Unsupported | Gắn `MANUAL_INSPECTION_REQUIRED` | RAR decoder chính thức / service riêng |
| DWG/AI/RVT | Unsupported | Liệt kê loại chưa hỗ trợ + hướng xử lý | CAD/BIM conversion service có giấy phép phù hợp |

### Quy tắc xuất bảng

Parquet export giữ tất cả cột ở **string** để tránh mất leading zeros/decimal precision ở giai đoạn ingest. Header được lấy từ dòng đầu; cột trùng được gắn hậu tố `__2`, `__3`; raw source không thay đổi. Sheet trống/CSV thiếu header sẽ không được tạo dữ liệu bịa. `tables_manifest.jsonl` ánh xạ `asset_id`/`document_id`/`source_path` → Parquet.

**Cần người tích hợp xác minh business schema** trước khi chuyển số lượng, giá, ngày, đơn vị từ string sang kiểu dữ liệu. Workbook XLSM/XLSX đọc `data_only=True`: công thức không được chạy, giá trị cached có thể cũ hoặc trống.

### Giới hạn hiện tại

1. Parser đã có có thể gán `SUCCESS` dù nội dung chỉ là table schema. Không dùng `SUCCESS` như bảo đảm đủ row; kiểm tra `tables_manifest.jsonl` và raw row counts.
2. Search image `623` là coverage index, **không phải** đánh giá semantic retrieval. `CHECK_12.png (unidentified image)` phải tìm/khôi phục từ raw hoặc xác nhận missing.
3. `DWG`, `AI`, `RVT`, RAR không được tự động gọi là xử lý xong; chuyển sang dedicated extraction phase.
4. Gói này **không tự tạo embeddings mới** và không tự ghi vào Qdrant/Iceberg/SeaweedFS; đây là một bàn giao PoC local.
5. Parquet phải cài `pyarrow`. Nếu lỗi dependency hoặc file dữ liệu, chi tiết sẽ nằm trong `issues.jsonl`/`special_metadata.jsonl`.

## Acceptance checklist cho người tích hợp

- [ ] Input Silver record count không đổi (`719` trong snapshot hiện tại) và các ID (`document_id`, `version_id`) không bị sửa.
- [ ] Tệp Bronze và Silver gốc giữ nguyên; output mới chỉ ghi vào `silver_quality_handoff_v1/`.
- [ ] `quality_summary.json` cho biết vấn đề thật (missing, unsupported, hash mismatch nếu bật verify, lỗi tables).
- [ ] Toàn bộ CSV/XLSM hợp lệ đã có manifest và Parquet, số hàng đối chiếu sample từ raw.
- [ ] `search_index_image_v3/image_index.jsonl` và `image_vectors.npy` có cùng số dòng; matched refs tương ứng asset IDs.
- [ ] 391 text chunks được kiểm tra thủ công một mẫu các file PDF/MD; không tuyên bố đã OCR PDF scan.
- [ ] `CHECK_12.png` được ghi trong issue tracker và không được bỏ qua âm thầm.
- [ ] DWG/AI/archive/Revit được phân luồng tiếp bằng adapter riêng, không coi là Silver semantically complete.

## Contract tích hợp sau này

- Ingest adapter nhận `(asset_id, version_id, raw_uri, content_hash, file_type)`.
- Output Silver có bảng `assets`, `text_chunks`, `tables`, `processing_runs`, `source_relationships` trong storage chuẩn (ví dụ Iceberg/Parquet). Bản PoC dùng JSONL và SQLite làm trung gian.
- Search worker tiêu thụ Silver refs; image encoder đọc file gốc và lưu vector trong Vector DB kèm `version_id`, `model_version`.
- Khi raw file đổi hash: tạo version mới, reparse, index-upsert và xóa/deactivate vector version cũ theo chính sách retention.
- Áp dụng ACL ở cả truy xuất index và mở raw file; không để cross-project leakage.
- Với dữ liệu bảng: SQL query đọc Parquet/Iceberg; text embedding chỉ dùng tìm **bảng**, không thay cho SQL tính toán.

## Kiểm thử bàn giao

Chạy `python -m unittest discover -s tests -v` trong thư mục này. Tests dùng fixture nhỏ không cần raw 2GB. Phần xuất Parquet sẽ tự báo skip nếu không cài `pyarrow`; trên máy production cần chạy lại kiểm thử với dependencies đầy đủ và sample raw thực.
