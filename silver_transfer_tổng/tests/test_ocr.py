from contextlib import closing
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from PIL import Image
import pymupdf

from silver_transfer.cli import main
from silver_transfer.ocr import OCR, Reading


class Reader:
    def __init__(self, name, readings):
        self.name = name
        self.readings = readings
        self.calls = 0

    def read(self, image):
        self.calls += 1
        return self.readings


class OCRTest(unittest.TestCase):
    def test_fallback_rejects_noise_and_preserves_bbox(self):
        local = Reader("paddle", [Reading("low confidence", 0.2, (2, 3, 10, 15))])
        cloud = Reader("google", [Reading("Tên dự án | Project name", 0.96, (2, 3, 10, 15))])
        engine = OCR([local, cloud], 0.85)
        rows = engine.chunks(30, 30, lambda box: Image.new("RGB", (30, 30)), page=1)
        self.assertEqual([r["content"] for r in rows], ["Tên dự án | Project name"])
        self.assertEqual(rows[0]["page_number"], 1)
        self.assertEqual(rows[0]["provenance"]["model"], "google")
        self.assertEqual(rows[0]["provenance"]["bbox"], (2, 3, 10, 15))
        local.readings = [Reading("good", 0.9, (2, 3, 10, 15))]
        engine.chunks(30, 30, lambda box: Image.new("RGB", (30, 30)))
        self.assertEqual(cloud.calls, 1)

    def test_run_indexes_vietnamese_and_english_ocr_with_page_provenance(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "03_PROJECTS"
            raw.mkdir()
            with pymupdf.open() as pdf:
                pdf.new_page().insert_text((72, 72), "Embedded drawing text")
                pdf.new_page(width=600, height=700)
                pdf.save(raw / "drawing.pdf")
            with Image.new("RGB", (80, 80), "white") as image:
                image.save(raw / "cover.png")
            template = root / "template.jsonl"
            template.write_text("".join(
                json.dumps({"document_id": name, "version_id": name, "file_name": name,
                            "file_path": f"E:/03_PROJECTS/{name}"}) + "\n"
                for name in ("drawing.pdf", "cover.png", "missing.jpg")
            ), encoding="utf-8")
            reader = Reader("fixture", [Reading("Khối nhà chính | Main building", 0.95, (2, 3, 10, 15))])
            out = root / "out"
            log = StringIO()
            with patch("silver_transfer.ocr.open_ocr", return_value=OCR([reader], 0.85)) as open_ocr, redirect_stdout(log):
                main(["run", "--raw-root", str(raw), "--template", str(template),
                      "--out", str(out), "--ocr", "paddle", "--ocr-device", "gpu"])
            open_ocr.assert_called_once_with("paddle", 0.85, "gpu")
            self.assertEqual(reader.calls, 2)
            self.assertIn("OCR ảnh 0/2", log.getvalue())
            self.assertIn("OCR ảnh 1/2 | PARTIAL | 1 đoạn OCR | cover.png", log.getvalue())
            self.assertIn("OCR ảnh 2/2 | MISSING | 0 đoạn OCR | missing.jpg", log.getvalue())
            pdf, image, _ = map(json.loads, (out / "silver_filled.jsonl").read_text(encoding="utf-8").splitlines())
            self.assertEqual([c["page_number"] for c in pdf["chunks"]], [1, 2])
            self.assertEqual(pdf["chunks"][1]["provenance"]["coordinate_space"], "pdf_pixels_216dpi")
            self.assertEqual(image["chunks"][0]["provenance"]["coordinate_space"], "image_pixels")
            self.assertFalse(pdf["content_complete"])
            self.assertFalse(image["content_complete"])
            with closing(sqlite3.connect(out / "search" / "search.sqlite")) as db:
                for query in ('"Khối nhà chính"', '"Khoi nha chinh"', '"Main building"'):
                    self.assertEqual(db.execute(
                        "SELECT count(*) FROM text_fts WHERE text_fts MATCH ?", (query,)
                    ).fetchone()[0], 2)
