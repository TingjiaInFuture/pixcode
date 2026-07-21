import contextlib
import shutil
import unittest
import uuid
from pathlib import Path

from pixrep.onepdf_writer import StreamingPDFWriter, pdf_escape_literal


class TestStreamingPDFWriter(unittest.TestCase):
    def test_pdf_escape_literal_escapes_newlines(self):
        escaped = pdf_escape_literal("a(1)\\b\nline\r")
        self.assertEqual(escaped, "a\\(1\\)\\\\b\\nline\\r")

    def test_build_writes_valid_pdf_skeleton(self):
        tmp_root = Path(__file__).resolve().parents[1] / ".test_scratch"
        tmp_root.mkdir(parents=True, exist_ok=True)
        out_pdf = tmp_root / f"onepdf_writer_{uuid.uuid4().hex}.pdf"
        try:
            writer = StreamingPDFWriter(title="demo", out_path=out_pdf)
            writer.add_page_lines(["hello", "中文内容"])
            writer.finalize()

            blob = out_pdf.read_bytes()
            self.assertTrue(blob.startswith(b"%PDF-"))
            self.assertIn(b"xref\n", blob)
            self.assertIn(b"startxref\n", blob)
            self.assertTrue(blob.rstrip().endswith(b"%%EOF"))
        finally:
            with contextlib.suppress(Exception):
                out_pdf.unlink(missing_ok=True)
            if tmp_root.exists() and not any(tmp_root.iterdir()):
                shutil.rmtree(tmp_root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
