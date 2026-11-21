# Silver unsupported recovery (non-destructive)

This script reads only records with `status=UNSUPPORTED` in an existing Silver JSONL. It does not edit the template, Silver input, raw files, image index, or handoff package.

## Run on Windows (PowerShell)

```powershell
cd "D:\test silver\silver_unsupported_recovery"
..\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python recover_unsupported.py `
  --silver "..\silver_03_projects\silver_filled_new.jsonl" `
  --raw-root "..\03_PROJECTS\03_PROJECTS" `
  --out "..\unsupported_recovery_v1"
```

Output: `unsupported_enrichment.jsonl`, `summary.json`. Use a NEW `--out` path on each run.

- SVG: safe XML parsing, element counts, text nodes. It does not render or guarantee full visual content.
- ZIP/RAR/7Z: list member names only; NEVER extracts. Inspect member counts and archive policy before later extraction.
- DWG: reads header and marks `NEEDS_CONVERSION`. Use ODA File Converter to make a **DXF copy** and ezdxf for layers/entities/text. Conversion may lose advanced/custom objects.
- AI: checks whether the header is PDF-compatible, marks `NEEDS_CONVERSION`. Export a PDF/SVG copy in Illustrator or Inkscape when supported. Rendering may differ.
- Original `UNSUPPORTED` Silver status stays as historical parser result; recovered data are separate and can be reconciled in a new Silver version after validation.

For full searchable coverage later, generate search text and/or preview embeddings from converted files, retaining document_id/source references.
