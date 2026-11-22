import hashlib
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from silver_transfer.cli import main


class TransferTest(unittest.TestCase):
    def test_run_from_template_to_handoff(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "03_PROJECTS"
            raw.mkdir()
            sources = {
                "notes.md": b"Project penguin",
                "rates.csv": b"Code,Amount\nA,12\nB,15\n",
                "changed.csv": b"Code,Amount\nC,99\n",
                "plan.svg": b'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20"><text>Orchid</text></svg>',
                "missing-close.svg": b'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20"><rect width="40" height="20"/>',
                "unclosed-child.svg": b'<svg xmlns="http://www.w3.org/2000/svg"><g>',
            }
            rows = []
            for i, (name, contents) in enumerate(sources.items()):
                (raw / name).write_bytes(contents)
                rows.append({
                    "document_id": f"d{i}",
                    "version_id": f"v{i}",
                    "file_name": name,
                    "file_path": f"E:/03_PROJECTS/{name}",
                    "content_hash": hashlib.sha256(
                        b"older version" if name == "changed.csv" else contents
                    ).hexdigest(),
                })
            template_text = "".join(json.dumps(row) + "\n" for row in rows)
            template = root / "template.jsonl"
            template.write_text(template_text, encoding="utf-8")
            out = root / "run"
            argv = [
                "run", "--template", str(template), "--raw-root", str(raw),
                "--out", str(out), "--recover", "--auto-convert", "--index",
            ]
            main(argv)

            result = [
                json.loads(line) for line in
                (out / "silver_filled.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual([row["status"] for row in result],
                             ["SUCCESS", "SUCCESS", "HASH_MISMATCH", "UNSUPPORTED", "UNSUPPORTED", "UNSUPPORTED"])
            for original, converted in zip(rows, result):
                self.assertEqual({key: converted[key] for key in original}, original)
            self.assertEqual(template.read_text(encoding="utf-8"), template_text)
            self.assertEqual([chunk["content"] for chunk in result[3]["chunks"]], ["Orchid"])
            self.assertEqual(len(result[3]["derived_extraction"]), 2)
            self.assertEqual({path.name: path.read_bytes() for path in raw.iterdir()}, sources)

            summary = json.loads((out / "handoff" / "quality_summary.json").read_text(encoding="utf-8"))
            self.assertEqual((summary["parquet_files"], summary["table_data_rows"]), (1, 2))
            self.assertEqual(summary["issues"]["HASH_MISMATCH"], 1)
            with closing(sqlite3.connect(out / "search" / "search.sqlite")) as database:
                self.assertEqual(database.execute(
                    "SELECT count(*) FROM text_fts WHERE text_fts MATCH 'penguin'"
                ).fetchone()[0], 1)
                self.assertEqual(database.execute(
                    "SELECT count(*) FROM text_fts WHERE text_fts MATCH 'orchid'"
                ).fetchone()[0], 1)
            self.assertTrue((out / "exotic" / "previews" / "d3" / "preview.jpg").is_file())
            self.assertTrue((out / "exotic" / "previews" / "d4" / "preview.jpg").is_file())
            self.assertFalse((out / "exotic" / "previews" / "d5" / "preview.jpg").exists())
            converted_rows = {
                row["file_name"]: row for row in map(
                    json.loads,
                    (out / "converted" / "results.jsonl").read_text(encoding="utf-8").splitlines(),
                )
            }
            self.assertEqual(converted_rows["plan.svg"]["status"], "SUCCESS")
            self.assertEqual(converted_rows["missing-close.svg"]["status"], "RECOVERED")
            self.assertEqual(converted_rows["missing-close.svg"]["metadata"]["svg_repair"], "missing_root_close_tag")
            self.assertEqual(converted_rows["unclosed-child.svg"]["status"], "ERROR")
            self.assertEqual(len(list((out / "converted" / "previews").rglob("*.png"))), 2)
            with self.assertRaises(SystemExit):
                main(argv)
