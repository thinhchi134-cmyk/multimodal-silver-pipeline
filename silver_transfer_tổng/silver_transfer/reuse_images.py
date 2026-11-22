"""Reuse image vectors for source versions verified by the current fill run."""

import json
from pathlib import Path
import re
from shutil import copyfile

import numpy as np

from .search_silver import read_records


def snapshot(path):
    rows = {}
    for _, row in read_records(path):
        key = row.get("version_id") or row.get("document_id")
        if key:
            if key in rows:
                raise SystemExit(f"Duplicate asset ID in {path}: {key}")
            rows[key] = row
    return rows


def reuse_image_vectors(source, silver, destination):
    source = source.resolve()
    config = json.loads((source / "config.json").read_text(encoding="utf-8"))
    previous_silver = Path(config["silver_source"])
    if not previous_silver.is_absolute():
        previous_silver = source / previous_silver
    previous = snapshot(previous_silver)
    current = snapshot(silver)
    manifest = source / "image_index.jsonl"
    refs = [row for _, row in read_records(manifest)]
    vectors = source / "image_vectors.npy"
    matrix = np.load(vectors, mmap_mode="r", allow_pickle=False)
    if (matrix.ndim != 2 or not matrix.shape[1] or not refs or len(refs) != len(matrix)
            or not np.issubdtype(matrix.dtype, np.floating) or not np.isfinite(matrix).all()):
        raise SystemExit("Invalid image vector matrix or manifest")
    del matrix

    seen = set()
    for ref in refs:
        key = ref["asset_id"]
        old = previous.get(key, {})
        new = current.get(key, {})
        digest = new.get("content_hash") or ""
        if (key in seen or new.get("status") not in {"METADATA_ONLY", "PARTIAL"}
                or not re.fullmatch(r"[0-9a-fA-F]{64}", digest)
                or digest.casefold() != (old.get("content_hash") or "").casefold()
                or old.get("document_id") != new.get("document_id")):
            raise SystemExit(f"Cannot reuse image vector for {key}. Rebuild with --images.")
        seen.add(key)

    copyfile(manifest, destination / manifest.name)
    copyfile(vectors, destination / vectors.name)
    config_path = destination / "config.json"
    new_config = json.loads(config_path.read_text(encoding="utf-8"))
    new_config.update(model_id=config["model_id"], mode="image-and-keyword",
                      reused_image_index=str(source))
    config_path.write_text(json.dumps(new_config, ensure_ascii=False, indent=2), encoding="utf-8")
    stats_path = destination / "stats.json"
    stats = json.loads(stats_path.read_text(encoding="utf-8"))
    stats["image_reused"] = len(refs)
    stats_path.write_text(json.dumps(stats, indent=2), encoding="utf-8")
    print(f"Reused {len(refs)} image vectors from {source}")
