# Silver → Searchable PoC

**Input files are read-only.** Generates a NEW index directory containing SQLite FTS5 and optionally SigLIP2 image embeddings. No captions generated. No source JSONL or JPG changes.

## Location (based on your workspace)

```text
D:\test silver\
├── 03_PROJECTS\03_PROJECTS\ (raw)
├── silver_03_projects\silver_filled_new.jsonl
└── silver_search_poc\search_silver.py
```

Open PowerShell inside `silver_search_poc`:

```powershell
..\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

`torch` must be installed in this virtual environment. If absent, install a matching build from https://pytorch.org/get-started/locally/ . Check:

```powershell
python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
```

### Step 1: build FAST keyword index, no GPU, no model downloads

```powershell
python search_silver.py build `
  --silver "..\silver_03_projects\silver_filled_new.jsonl" `
  --raw-root "..\03_PROJECTS\03_PROJECTS" `
  --out "..\search_index_keyword_v1"

python search_silver.py search --index "..\search_index_keyword_v1" --mode keyword --query "project"
```

### Step 2: NEW full index including images

```powershell
python search_silver.py build `
  --silver "..\silver_03_projects\silver_filled_new.jsonl" `
  --raw-root "..\03_PROJECTS\03_PROJECTS" `
  --out "..\search_index_image_v1" `
  --images --device auto

python search_silver.py search `
  --index "..\search_index_image_v1" `
  --mode image --query "Aerial rendering of a seaside resort with swimming pool" --top 10
```

Search by an exemplar image:

```powershell
python search_silver.py search --index "..\search_index_image_v1" `
  --mode image --query-image "C:\path\example.jpg" --top 10
```

First model run downloads weights from Hugging Face; CPU works but is slower. NVIDIA RTX A1000 8GB: run with `--device auto`, and set `--device cpu` if CUDA is unavailable or runs out of memory. The index uses flat NumPy cosine search rather than FAISS; ~600 images is small enough for a reliable baseline.

**Important**: `--out` must point to a NEW folder. To rebuild, choose another version folder; this code deliberately does not overwrite indexes.

### Known limits

- Filename/path mapping uses `03_PROJECTS` suffix or unique basename; ambiguous matches are skipped.
- Read-only source files. `source_path` and `document_id` are returned in search results.
- Large Pillow-decompression-bomb images are skipped with warnings, not unsafely decoded.
- `keyword` searches filename/title and extracted Silver text chunks. This is not yet text-semantic retrieval.
- Image mode uses SigLIP2 paired text/image encoders: natural language text-to-image and image-to-image. No VLM caption required.
- CSV/Excel contents cannot be accurately aggregated using this search index; use Trino/SQL for table values.
- PDF pages containing only scanned pictures will not be semantically searchable unless a visual page index or OCR is added later.
- Revit/DWG require specialized parsers for content search.
- The model is not bundled; initial download requires internet. Model quality is unverified on your images.
- Source hash verification, access controls, and enterprise index refresh/deletion workflow are beyond this PoC.
