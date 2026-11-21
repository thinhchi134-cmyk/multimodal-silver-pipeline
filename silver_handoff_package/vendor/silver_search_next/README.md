# Silver Search — next steps, without changing original data

Copy these three Python files into your existing `silver_search_poc/` folder (beside `search_silver.py`). **Do not move, edit, or overwrite** the Bronze files, filled JSONL, or the existing `search_index_image_v1/`.

## 1. Audit formats and current index

```powershell
python audit_index.py --index "..\search_index_image_v1"
```

## 2. Try repairing huge `600.png` and `600_2.png`

Try installing libvips Python binary wheels (64-bit Windows with compatible Python; if binary installation fails, use an installed `vips` executable externally to make a small preview and retain original input):

```powershell
python -m pip install "pyvips[binary]"
python -c "import pyvips; print(pyvips.version(0))"
python extend_image_index.py `
  --index "..\search_index_image_v1" `
  --out "..\search_index_image_v2" `
  --names "600.png" "600_2.png" `
  --device auto
```

This creates previews under a new index, copies previous 621 embeddings and APPENDS successful new embeddings. The source index is untouched. If `pyvips` or image resources cannot handle these giant files, they remain skipped and can be handled by a dedicated image-service. The program reports the reason.

```powershell
python search_silver.py search --index "..\search_index_image_v2" --mode image --query "Modern seaside resort with swimming pool" --top 10
```

## 3. Text semantic search over the existing 391 nonempty chunks

```powershell
python -m pip install sentence-transformers
python text_semantic.py build --index "..\search_index_image_v1" --out "..\search_index_text_v1" --device cpu
python text_semantic.py search --index "..\search_index_text_v1" --query "báo cáo dự án kiến trúc" --top 10
```

This is semantic search for **already extracted text only**. CSV / XLSX table values are **not** magically converted into a SQL database; direct row querying needs a structured-data pipeline. PDF scans need OCR, PPTX with diagrams/photos may need slide image indexing, and DWG/RVT need separate CAD/BIM parsers. Archive formats should be inspected and extracted safely, with file-size and path-traversal checks.

The returned similarity values are **ranking signals**, not accuracy percentages. Evaluate relevance using labeled queries and Recall@K.
