# Safe Silver JSONL filler

This script **never writes to the raw folder or the source template**. It creates a new JSONL file. `document_id`, `version_id`, `content_hash`, `file_path`, `file_name`, `title`, and all other input fields remain unchanged; only `processing`, `status`, `content_complete`, `page_count`, `chunks`, and `detail` are filled in the output.

## Windows PowerShell example

Place `fill_silver.py` in the folder that contains both `03_PROJECTS` and `silver_03_projects`, or use the full path to the script.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

python fill_silver.py `
  --raw-root ".\03_PROJECTS\03_PROJECTS" `
  --template ".\silver_03_projects\silver-template-a0b9b530-55b4-4cdc-8b68-ca92d4491001.jsonl" `
  --output ".\silver_03_projects\silver_filled_new.jsonl"
```

Optional: `--verify-hash` recomputes SHA256 for each matched raw file and **skips** extraction when the value differs from the template. This adds I/O cost for large datasets.

Important notes:
- Images: metadata only, `chunks=[]`, no invented caption. Their raw image can later be image-embedded directly by a separate process.
- CSV and XLSX: one lightweight schema chunk per table/sheet. This script does not load whole rows into JSONL.
- PDF: extracts embedded text only; scanned PDF may require OCR in a later stage.
- PPTX: extracts slide text/tables, not image semantics.
- DWG/RVT/AI/ZIP: marked `UNSUPPORTED` if raw exists.
- Source matching first uses the path suffix after `03_PROJECTS`, and only falls back to the filename if **unique**. Ambiguous matches are not guessed.
- `content_complete` is false when image semantics, OCR, embedded images, or table rows were not fully extracted. `METADATA_ONLY` for images is intentional.
- The script is a **Silver filling tool**, not an embedding/vector-index builder. The output is searchable by filename/text after indexing; image semantic search requires an image encoder and vector index.
- Output defaults to fail safely if it already exists; change output filename for each run. To consciously replace output use `--force-output` (never overwrites template).
