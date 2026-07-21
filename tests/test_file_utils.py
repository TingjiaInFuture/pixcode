import shutil
import unittest
import uuid
from pathlib import Path

from pixrep.file_utils import (
    build_tree,
    compile_ignore_matcher,
    detect_language,
    is_probably_text,
    line_count_from_bytes,
    matches_any,
    normalize_posix_path,
    safe_join_repo,
)


class TestFileUtils(unittest.TestCase):
    def test_normalize_posix_path(self):
        self.assertEqual(normalize_posix_path(r"src\\main.py"), "src/main.py")
        self.assertEqual(normalize_posix_path("a/b/c.txt"), "a/b/c.txt")

    def test_matches_any_case_insensitive(self):
        self.assertTrue(matches_any("SRC/Main.PY", ["src/*.py"]))
        self.assertTrue(matches_any("README.md", ["readme.md"]))
        self.assertFalse(matches_any("src/main.py", ["src/*.js"]))

    def test_compile_ignore_matcher_case_insensitive(self):
        matcher = compile_ignore_matcher(["SRC/*.PY", "README.*"])
        self.assertTrue(matcher("src/main.py"))
        self.assertTrue(matcher("ReadMe.md"))
        self.assertTrue(matcher("docs/readme.rst"))
        self.assertFalse(matcher("docs/guide.rst"))

    def test_is_probably_text(self):
        self.assertTrue(is_probably_text(b"hello\nworld"))
        self.assertFalse(is_probably_text(b"abc\x00def"))

    def test_line_count_from_bytes(self):
        self.assertEqual(line_count_from_bytes(b""), 0)
        self.assertEqual(line_count_from_bytes(b"a"), 1)
        self.assertEqual(line_count_from_bytes(b"a\n"), 1)
        self.assertEqual(line_count_from_bytes(b"a\nb"), 2)

    def test_detect_language(self):
        self.assertEqual(detect_language("Dockerfile"), "docker")
        self.assertEqual(detect_language("src/app.py"), "python")
        self.assertEqual(detect_language("unknown.unknownext"), "text")

    def test_safe_join_repo_blocks_escape(self):
        tmp_root = Path(__file__).resolve().parents[1] / ".test_scratch"
        tmp_root.mkdir(parents=True, exist_ok=True)
        root = tmp_root / f"repo_{uuid.uuid4().hex}"
        try:
            root.mkdir()
            (root / "a").mkdir()
            (root / "a" / "b.txt").write_text("ok", encoding="utf-8")

            p = safe_join_repo(root, "a/b.txt")
            self.assertIsNotNone(p)
            self.assertTrue(p.exists())

            # Attempt escape.
            self.assertIsNone(safe_join_repo(root, "../outside.txt"))
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_build_tree_ascii(self):
        tree = build_tree(["a/b.py", "a/c.py", "d.txt"], "repo", style="ascii")
        self.assertIn("repo/", tree)
        self.assertIn("|-- a/", tree)
        self.assertIn("`-- ", tree)


if __name__ == "__main__":
    unittest.main()
