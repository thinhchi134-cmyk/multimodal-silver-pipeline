"""Attach recovered text and artifact references to the Silver snapshot."""

from collections import defaultdict
import json
from .chunking import split_text


def records(path):
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def identity(row):
    return row.get("document_id"), row.get("version_id")


def merge(base, destination, sources):
    extracted = defaultdict(list)
    texts = defaultdict(list)
    for stage, folder, report in sources:
        previews = defaultdict(list)
        for row in records(folder / "preview_manifest.jsonl"):
            previews[identity(row)].append(row)
        for row in records(folder / report):
            key = identity(row)
            extracted[key].append({
                "stage": stage,
                "status": row.get("status", row.get("result")),
                "metadata": row.get("metadata"),
                "converted_path": row.get("converted_path"),
                "previews": previews[key],
            })
        for row in records(folder / "text_chunks.jsonl"):
            texts[identity(row)].append((stage, row))

    with destination.open("x", encoding="utf-8") as output:
        for row in records(base):
            key = identity(row)
            if key in extracted and row.get("status") == "UNSUPPORTED":
                row["derived_extraction"] = extracted[key]
                chunks = row.setdefault("chunks", [])
                seen = {(chunk.get("page_number"), chunk["content"].strip()) for chunk in chunks}
                for stage, item in texts[key]:
                    content = item["content"].strip()
                    page = item.get("page_number")
                    for part in split_text(content):
                        normalized = part.strip()
                        if not normalized or (page, normalized) in seen:
                            continue
                        seen.add((page, normalized))
                        chunks.append({
                            "chunk_no": len(chunks) + 1,
                            "page_number": page,
                            "section_title": item.get("locator"),
                            "content": part,
                            "provenance": {"stage": stage, "chunk_id": item["chunk_id"]},
                        })
            output.write(json.dumps(row, ensure_ascii=False) + "\n")
