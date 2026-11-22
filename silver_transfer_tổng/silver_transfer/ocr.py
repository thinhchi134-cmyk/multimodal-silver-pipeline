"""Optional Vietnamese/English OCR with bounded crops and source coordinates."""

from dataclasses import dataclass
from io import BytesIO
import math
import re
from tempfile import TemporaryDirectory
from pathlib import Path

from PIL import Image, ImageOps

TILE = 2400
STRIDE = 2160
PDF_SCALE = 3
PADDLE_DETECTOR = "PP-OCRv5_server_det"
PADDLE_RECOGNIZER = "latin_PP-OCRv5_mobile_rec"


def add_arguments(parser):
    parser.add_argument("--ocr", choices=("paddle", "google", "fallback"),
                        help="OCR images and scanned PDF pages. Google modes send crops to Cloud Vision.")
    parser.add_argument("--ocr-min-confidence", type=float, default=0.85)
    parser.add_argument("--ocr-device", choices=("cpu", "gpu"), default="cpu",
                        help="Device for local PaddleOCR. GPU requires a CUDA-enabled PaddlePaddle build.")


def validate_arguments(parser, args):
    if not 0 <= args.ocr_min_confidence <= 1:
        parser.error("--ocr-min-confidence must be between 0 and 1")
    if args.ocr_device == "gpu" and args.ocr not in {"paddle", "fallback"}:
        parser.error("--ocr-device gpu requires --ocr paddle or --ocr fallback")


@dataclass(frozen=True)
class Reading:
    text: str
    confidence: float
    box: tuple[int, int, int, int]


class PaddleReader:
    name = PADDLE_RECOGNIZER

    def __init__(self, device="cpu"):
        if device == "gpu":
            import paddle

            if not paddle.is_compiled_with_cuda() or paddle.device.cuda.device_count() < 1:
                raise RuntimeError(
                    "PaddleOCR GPU requires paddlepaddle-gpu and a visible NVIDIA GPU. "
                    "Use the GPU environment or select --ocr-device cpu."
                )
            try:
                probe = paddle.to_tensor([1.0], place=paddle.CUDAPlace(0))
                (probe + 1).numpy()
            except Exception as error:
                raise RuntimeError(f"PaddleOCR could not execute on CUDA device 0: {error}") from error
        from paddleocr import PaddleOCR

        self.model = PaddleOCR(
            text_detection_model_name=PADDLE_DETECTOR,
            text_recognition_model_name=PADDLE_RECOGNIZER,
            text_det_limit_side_len=TILE,
            text_det_limit_type="max",
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=True,
            device="gpu:0" if device == "gpu" else "cpu",
            enable_mkldnn=False,
        )

    def read(self, image):
        # A PNG also gives PaddleOCR an unambiguous channel order.
        with TemporaryDirectory() as directory:
            path = Path(directory) / "crop.png"
            image.save(path)
            for result in self.model.predict(str(path)):
                for text, score, box in zip(
                    result["rec_texts"], result["rec_scores"], result["rec_boxes"], strict=True
                ):
                    yield Reading(text, float(score), tuple(map(int, box)))


class GoogleReader:
    name = "google-vision/document-text-detection"

    def __init__(self):
        from google.cloud import vision

        self.vision = vision
        self.client = vision.ImageAnnotatorClient()

    def read(self, image):
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        response = self.client.document_text_detection(
            image=self.vision.Image(content=buffer.getvalue()), retry=None, timeout=30
        )
        if response.error.message:
            raise RuntimeError(response.error.message)
        for page in response.full_text_annotation.pages:
            for block in page.blocks:
                for paragraph in block.paragraphs:
                    words = paragraph.words
                    if not words:
                        continue
                    text = " ".join("".join(symbol.text for symbol in word.symbols) for word in words)
                    vertices = paragraph.bounding_box.vertices
                    if vertices:
                        xs, ys = zip(*((v.x, v.y) for v in vertices))
                        yield Reading(text, min(word.confidence for word in words),
                                      (min(xs), min(ys), max(xs), max(ys)))


def open_ocr(mode, minimum, device="cpu"):
    readers = []
    if mode in {"paddle", "fallback"}:
        readers.append(PaddleReader(device))
    if mode in {"google", "fallback"}:
        readers.append(GoogleReader())
    return OCR(readers, minimum)


def tiles(width, height):
    for top in range(0, max(1, height - TILE + STRIDE), STRIDE):
        for left in range(0, max(1, width - TILE + STRIDE), STRIDE):
            yield left, top, min(left + TILE, width), min(top + TILE, height)


def overlaps(first, second):
    x1, y1, x2, y2 = first
    a1, b1, a2, b2 = second
    overlap = max(0, min(x2, a2) - max(x1, a1)) * max(0, min(y2, b2) - max(y1, b1))
    return overlap > 0.5 * min((x2 - x1) * (y2 - y1), (a2 - a1) * (b2 - b1))


class OCR:
    def __init__(self, readers, minimum):
        self.readers = readers
        self.minimum = minimum

    def chunks(self, width, height, crop, page=None, coordinate_space="image_pixels"):
        seen = {}
        output = []
        for left, top, right, bottom in tiles(width, height):
            with crop((left, top, right, bottom)) as tile:
                for reader in self.readers:
                    accepted = []
                    for reading in reader.read(tile):
                        text = reading.text.strip()
                        if (not math.isfinite(reading.confidence) or reading.confidence < self.minimum
                                or not any(char.isalnum() for char in text)):
                            continue
                        x1, y1, x2, y2 = reading.box
                        if not (0 <= x1 < x2 <= tile.width and 0 <= y1 < y2 <= tile.height):
                            continue
                        accepted.append((text, reading.confidence, (left + x1, top + y1, left + x2, top + y2)))
                    if accepted:
                        break
            for text, confidence, bbox in accepted:
                key = re.sub(r"\s+", " ", text.casefold())
                boxes = seen.setdefault(key, [])
                if any(overlaps(bbox, box) for box in boxes):
                    continue
                boxes.append(bbox)
                output.append({
                    "page_number": page, "section_title": "OCR", "content": text,
                    "provenance": {"stage": "ocr", "model": reader.name, "confidence": confidence,
                                   "bbox": bbox, "coordinate_space": coordinate_space},
                })
        return output

    def image(self, source):
        try:
            with Image.open(source) as original, ImageOps.exif_transpose(original) as image:
                return self.chunks(*image.size, lambda box: image.crop(box).convert("RGB"))
        except Image.DecompressionBombError:
            import pyvips

            image = pyvips.Image.new_from_file(str(source)).autorot()

            def crop(box):
                left, top, right, bottom = box
                tile = image.crop(left, top, right-left, bottom-top)
                with Image.open(BytesIO(tile.write_to_buffer(".png"))) as decoded:
                    return decoded.convert("RGB")

            return self.chunks(image.width, image.height, crop)

    def pdf_page(self, page):
        import pymupdf

        def crop(box):
            clip = pymupdf.Rect(*(value / PDF_SCALE for value in box)) & page.rect
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(PDF_SCALE, PDF_SCALE),
                                     clip=clip, colorspace=pymupdf.csRGB, alpha=False)
            return Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)

        return self.chunks(math.ceil(page.rect.width * PDF_SCALE),
                           math.ceil(page.rect.height * PDF_SCALE), crop,
                           page=page.number + 1, coordinate_space="pdf_pixels_216dpi")
