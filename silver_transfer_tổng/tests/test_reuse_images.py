import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import numpy as np

from silver_transfer.reuse_images import reuse_image_vectors


class ReuseImagesTest(unittest.TestCase):
    def test_reuses_verified_versions_and_rejects_changed_hash(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "old"
            destination = root / "new"
            source.mkdir()
            destination.mkdir()
            old_silver = root / "old.jsonl"
            new_silver = root / "new.jsonl"
            row = {"document_id": "doc", "version_id": "v1", "status": "METADATA_ONLY",
                   "content_hash": "a" * 64}
            for snapshot in (old_silver, new_silver):
                snapshot.write_text(json.dumps(row) + "\n", encoding="utf-8")
            (source / "config.json").write_text(
                json.dumps({"silver_source": str(old_silver), "model_id": "fixture"}), encoding="utf-8"
            )
            (source / "image_index.jsonl").write_text('{"asset_id":"v1"}\n', encoding="utf-8")
            np.save(source / "image_vectors.npy", np.array([[0.5, 0.5]], dtype=np.float32))
            for name in ("config.json", "stats.json"):
                (destination / name).write_text("{}", encoding="utf-8")

            new_silver.write_text(json.dumps({**row, "content_hash": "b" * 64}), encoding="utf-8")
            with self.assertRaises(SystemExit):
                reuse_image_vectors(source, new_silver, destination)
            self.assertFalse((destination / "image_vectors.npy").exists())

            new_silver.write_text(json.dumps(row), encoding="utf-8")
            reuse_image_vectors(source, new_silver, destination)
            self.assertEqual((destination / "image_vectors.npy").read_bytes(),
                             (source / "image_vectors.npy").read_bytes())
            self.assertEqual(json.loads((destination / "stats.json").read_text())["image_reused"], 1)
