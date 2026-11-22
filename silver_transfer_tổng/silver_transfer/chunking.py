"""Split extracted text at readable boundaries before writing Silver."""

import re

MAX_CHUNK_CHARS = 1000
BOUNDARIES = (
    re.compile(r"\n[ \t]*\n"),
    re.compile(r"\n"),
    re.compile(r"(?<=[.!?])\s+"),
    re.compile(r"\s+"),
)


def split_text(text):
    """Preserve the extracted text and prefer boundaries in the latter half."""
    start = 0
    while len(text) - start > MAX_CHUNK_CHARS:
        window = text[start:start + MAX_CHUNK_CHARS]
        cut = MAX_CHUNK_CHARS
        for pattern in BOUNDARIES:
            matches = list(pattern.finditer(window, MAX_CHUNK_CHARS // 2))
            if matches:
                cut = matches[-1].end()
                break
        yield text[start:start + cut]
        start += cut
    if start < len(text):
        yield text[start:]


def split_chunks(chunks):
    result = []
    for chunk in chunks:
        for part in split_text(chunk["content"]):
            if part.strip():
                result.append({**chunk, "chunk_no": len(result) + 1, "content": part})
    return result
