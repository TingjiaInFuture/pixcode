import tempfile
import unittest
from pathlib import Path

from pixrep.file_utils import char_display_width, display_width
from pixrep.onepdf_pack import _ascii_safe, _strip_python_docstrings, _wrap_line
from pixrep.onepdf_writer import StreamingPDFWriter


class TestOnepdfBasic(unittest.TestCase):
    def test_display_width_cjk_is_double(self):
        self.assertEqual(char_display_width("a"), 1)
        self.assertEqual(char_display_width("中"), 2)
        # 2 ASCII + 2 CJK = 2 + 4 = 6 display columns
        self.assertEqual(display_width("ab中文"), 6)

    def test_ascii_safe_expands_tabs_to_tabstops(self):
        # tab advances to the next multiple of tab_size
        self.assertEqual(_ascii_safe("a\tb", 4), "a   b")
        self.assertEqual(_ascii_safe("\tx", 2), "  x")
        # CR is stripped, LF preserved
        self.assertEqual(_ascii_safe("a\r\nb", 2), "a\nb")

    def test_wrap_line_uses_display_width(self):
        # 6 CJK chars (display width 12) must wrap so every line stays within
        # the column budget — character-count wrapping would overrun here.
        wrapped = _wrap_line("中文中文中文", 6)
        self.assertGreater(len(wrapped), 1)
        for line in wrapped:
            self.assertLessEqual(display_width(line), 6)

    def test_strip_docstring_preserves_inline_code(self):
        # A docstring that shares its line with real code (e.g. a ternary) must
        # not be dropped, and the docstring itself must not take code with it.
        src = 'def f():\n    "doc"; return 1\n'
        out = _strip_python_docstrings(src)
        self.assertIn("return 1", out)

    def test_strip_docstring_cjk_byte_col_offset(self):
        # Regression: AST col_offset is a UTF-8 byte offset; a CJK char before
        # the docstring must not corrupt the "alone on its line" check.
        src = 'x = 1\n\n\ndef f():\n    "中文说明"\n    return 2\n'
        out = _strip_python_docstrings(src)
        self.assertIn("return 2", out)
        self.assertNotIn("中文说明", out)

    def test_streaming_writer_atomic_no_tmp_leftover(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "out.pdf"
            writer = StreamingPDFWriter(title="t", out_path=out)
            writer.add_page_lines(["hello", "world"])
            writer.finalize()
            self.assertTrue(out.exists())
            self.assertTrue(out.read_bytes().startswith(b"%PDF-"))
            # The temp file must be gone after the atomic replace.
            self.assertFalse(out.with_name(out.name + ".tmp").exists())


if __name__ == "__main__":
    unittest.main()
