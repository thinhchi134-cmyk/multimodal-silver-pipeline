# Exotic files → Silver enrichment v2 (Windows PoC)

**Mục tiêu**: đọc 25 record `UNSUPPORTED` từ `unsupported_enrichment.jsonl` và tạo text, preview, file inventory. **Không chỉnh sửa raw Bronze, Silver JSONL cũ, template, hay search index v3**. Đây là adapter cho PoC, chưa phải converter universal production.

## File layout

```
D:\test silver\
  03_PROJECTS\03_PROJECTS\
  unsupported_recovery_v1\unsupported_enrichment.jsonl
  silver_search_poc\search_silver.py
  search_index_image_v3\
  silver_exotic_adapters_v2\recover_exotic.py
```

## Installation (Python 3.11 venv)

```powershell
cd "D:\test silver\silver_exotic_adapters_v2"
..\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

For RAR/7Z inventory: install 7-Zip and make `7z.exe` discoverable via PATH. These archives **are not expanded** in this version. For full DWG extraction, use a trusted CAD conversion step to create DXF files outside Bronze (see below).

## 1. Extract (no archives expanded)

```powershell
python recover_exotic.py `
  --enrichment "..\unsupported_recovery_v1\unsupported_enrichment.jsonl" `
  --raw-root "..\03_PROJECTS\03_PROJECTS" `
  --out "..\exotic_recovery_v2"
```

Output:

```
exotic_recovery_v2/
  summary.json
  recovery_v2.jsonl
  text_chunks.jsonl
  preview_manifest.jsonl
  archive_children.jsonl
  previews/<document_id>/...jpg
  archive_children/
```

**Status interpretation**: `PREVIEW_READY` means preview available, **not** automatically indexed. `NEEDS_DXF_CONVERSION` is expected for DWG without DXF. `ARCHIVE_INVENTORIED` means archive names only, not content searchable. `ERROR` requires investigation. `NEEDS_SOURCE_REEXPORT` for corrupt SVG.

## 2. Optional ZIP child files (trusted archives only)

Use a **new** --out (never overwrite existing output):

```powershell
python recover_exotic.py `
  --enrichment "..\unsupported_recovery_v1\unsupported_enrichment.jsonl" `
  --raw-root "..\03_PROJECTS\03_PROJECTS" `
  --out "..\exotic_recovery_v2_zip" `
  --expand-zip --max-archive-mb 512 --max-child-mb 200
```

ZIP expansion checks paths, declared size, encryption, symlinks, number of files, output size while streaming. Extracted children are assigned stable IDs and keep parent refs. **No recursive processing** yet; feed child manifest to existing Bronze → Silver pipeline as a separate next batch. This is not a malware scanner: only do this on trusted content in an isolated worker.

RAR and 7Z: inventories via `7z l -slt`; safe expansion into a sandbox with quotas and verified paths should be handled by a production archive worker; not implemented here. Password-protected archives will require explicit handling.

## 3. DWG → DXF

Download **ODA File Converter** from the vendor, export copies to DXF in e.g. `D:\test silver\converted_dxf`. Keep source filenames/stems. Then rerun with a new output path:

```powershell
python recover_exotic.py `
  --enrichment "..\unsupported_recovery_v1\unsupported_enrichment.jsonl" `
  --raw-root "..\03_PROJECTS\03_PROJECTS" `
  --converted-dxf "..\converted_dxf" `
  --out "..\exotic_recovery_dxf_v1"
```

Extracts layers, modelspace entity counts, TEXT/MTEXT and block INSERT attributes. It does not promise complete CAD geometry, layout text, or semantic BIM extraction. Filenames with identical stems in different projects require explicit mapping before production deployment.

## 4. Image preview indexing (optional, never edits existing image v3)

Creates a **new standalone preview vector index** from AI/PDF, SVG, JFIF previews. It uses the same SigLIP2 base model and helper functions already in `search_silver.py`.

```powershell
python index_previews.py build `
  --manifest "..\exotic_recovery_v2\preview_manifest.jsonl" `
  --search-module "..\silver_search_poc" `
  --out "..\exotic_preview_index_v1" `
  --device auto

python index_previews.py search `
  --index "..\exotic_preview_index_v1" `
  --search-module "..\silver_search_poc" `
  --query "modern building architectural illustration" `
  --top 10
```

This is a standalone **supplementary search index**, not a merged index. To implement one unified search endpoint, query v3 + preview index and merge/rerank the two ranked result lists. Metadata and preview `document_id` provide join keys; no updates to historical Silver records are implied.

For `text_chunks.jsonl`, use your existing text semantic worker or FTS index. Each chunk has document_id / locator / content. The enriched record itself does not need an AI caption.

## Notes and limitations

- `.ai` with PDF-compatible headers: first try PyMuPDF; if opening fails, export as PDF via a trusted Illustrator/converter workflow. Some Illustrator-only layers won't be in the PDF-compatible portion.
- `.svg`: parse via defusedxml; preview via CairoSVG. Process SVG only from trusted sources in an isolated worker because vector assets may reference external resources. The malformed SVG is expected to report parse/render error; re-export from its `.ai` where possible.
- `.jfif`: parsed with Pillow as JPEG-compatible image.
- `.dwg`: requires DXF converter; no direct DWG parser or made-up searchable text.
- Missing original paths and unsupported/failed conversions remain explicitly reported.
- Extraction manifests are **new sidecar Silver enrichment**, not modifications of Silver history.
- For production: quarantine inputs; apply per-file time, memory, pixel and CPU quotas; sandbox converters; apply ACLs to source and child assets; persist parser/version/source_hash; support retries and transactionally publish complete outputs. Do not expose local filesystem paths directly to end users.
