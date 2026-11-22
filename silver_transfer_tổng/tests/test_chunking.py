import unittest

from silver_transfer.chunking import MAX_CHUNK_CHARS, split_chunks


class ChunkingTest(unittest.TestCase):
    def test_preserves_content_and_page_metadata_across_splits(self):
        first_paragraph = "A" * 850 + "\n\n"
        page_text = first_paragraph + "B" * 1700 + "\nLate text."
        chunks = split_chunks([
            {"chunk_no": 1, "page_number": 2, "section_title": "Schedule", "content": page_text},
            {"chunk_no": 2, "page_number": 3, "section_title": "Notes", "content": "Last page."},
        ])
        self.assertEqual(chunks[0]["content"], first_paragraph)
        self.assertEqual("".join(item["content"] for item in chunks[:-1]), page_text)
        self.assertTrue(all(len(item["content"]) <= MAX_CHUNK_CHARS for item in chunks))
        self.assertTrue(all(item["page_number"] == 2 and item["section_title"] == "Schedule" for item in chunks[:-1]))
        self.assertEqual(chunks[-1]["page_number"], 3)
        self.assertEqual([item["chunk_no"] for item in chunks], list(range(1, len(chunks) + 1)))
