# Fill an existing Silver JSONL template

Python 3.11+ recommended. This PoC preserves existing source IDs and hashes, checks the SHA256 of readable source files, and fills parsing metadata with no VLM. It **does not** modify the input template or original files.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python fill_template.py --template "silver-template.jsonl" --output "silver_filled.jsonl" --split-by-type
```

Important: `file_path` values point to an E: drive on the source Windows machine. If running elsewhere, source files will be marked `MISSING`; the JSONL by itself does not contain the file bytes.

## Test the local 03_PROJECTS copy

Run from the workspace root with a Python environment that has the parsers installed:

```powershell
python .\bronze_silver_poc\fill_template.py `
  --template .\silver-template-a0b9b530-55b4-4cdc-8b68-ca92d4491001.jsonl `
  --template-root 'E:\ACO_Marketing Campaign\03_PROJECTS' `
  --source-root .\03_PROJECTS\03_PROJECTS `
  --output .\silver_03_projects\silver_filled.jsonl
```

Each template row produces one JSONL row. The root mapping locates the local bytes while keeping the original `file_path`, IDs, and expected hash in the result. The JSONL is a parser test result. The PoC extracts image dimensions and format, but does not run image OCR or vision analysis.

Status semantics:
- `MISSING`: source path not found
- `HASH_MISMATCH`: file differs from captured version; not processed
- `UNSUPPORTED`: parser not implemented (RVT, DWG, AI, archives, etc.)
- `METADATA_ONLY`: image/table/workbook basic metadata extracted, not full semantic content
- `NEEDS_OCR_OR_VISUAL_EXTRACTION`: document has no extracted text
- `SUCCESS`: basic textual parse completed
- `PARTIAL`: text preview truncated
- `FAILED`: parser error

Notes: PDF/PPTX text is capped per page/slide at `--max-chars`; for long pages the output may not be complete, and this PoC is for testing rather than production completeness. CSV and XLSX are not materialized to Iceberg/Parquet here; use the Bronze-Silver pipeline for the tabular data itself.
