import contextlib
import shutil
import unittest
import uuid
from pathlib import Path

from pixrep.onepdf_pack import pack_repo_to_one_pdf
from pixrep.scanner import RepoScanner


class TestScannerAndOnepdf(unittest.TestCase):
    def test_scanner_skips_binary_and_size(self):
        tmp_root = Path(__file__).resolve().parents[1] / ".test_scratch"
        tmp_root.mkdir(parents=True, exist_ok=True)
        root = tmp_root / f"repo_{uuid.uuid4().hex}"
        try:
            root.mkdir()
            (root / "src").mkdir()
            (root / "src" / "a.py").write_text("print('hi')\n", encoding="utf-8")
            (root / "src" / "bin.dat").write_bytes(b"abc\x00def")
            (root / "big.txt").write_text("x" * 5000, encoding="utf-8")

            scanner = RepoScanner(str(root), max_file_size=1024)  # 1KB
            repo = scanner.scan(include_content=True)

            paths = {str(f.path).replace("\\", "/") for f in repo.files}
            self.assertIn("src/a.py", paths)
            self.assertNotIn("src/bin.dat", paths)
            self.assertNotIn("big.txt", paths)
            self.assertEqual(repo.language_stats.get("python", {}).get("files"), 1)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_onepdf_pack_writes_pdf(self):
        tmp_root = Path(__file__).resolve().parents[1] / ".test_scratch"
        tmp_root.mkdir(parents=True, exist_ok=True)
        root = tmp_root / f"repo_{uuid.uuid4().hex}"
        try:
            root.mkdir()
            (root / "a.py").write_text(
                "def f():\n    注释 = '中文'\n    return 注释\n", encoding="utf-8"
            )
            (root / "b.js").write_text("function g(){ return f(); }\n", encoding="utf-8")

            out_pdf = tmp_root / f"out_{uuid.uuid4().hex}.pdf"
            stats = pack_repo_to_one_pdf(
                repo_root=root,
                out_pdf=out_pdf,
                prefer_git=False,
                core_only=False,
                include_tree=True,
                include_index=True,
            )
            self.assertTrue(out_pdf.exists())
            self.assertGreater(out_pdf.stat().st_size, 20)
            self.assertGreaterEqual(stats.get("pages", 0), 1)
            blob = out_pdf.read_bytes()
            self.assertTrue(blob.startswith(b"%PDF-"))
        finally:
            shutil.rmtree(root, ignore_errors=True)
            with contextlib.suppress(Exception):
                out_pdf.unlink(missing_ok=True)

    def test_scanner_metadata_only_lazy_load(self):
        tmp_root = Path(__file__).resolve().parents[1] / ".test_scratch"
        tmp_root.mkdir(parents=True, exist_ok=True)
        root = tmp_root / f"repo_{uuid.uuid4().hex}"
        try:
            root.mkdir()
            file_path = root / "lazy.py"
            file_path.write_text("a\nb\nc", encoding="utf-8")

            scanner = RepoScanner(str(root), max_file_size=1024)
            repo = scanner.scan(include_content=False)
            self.assertEqual(len(repo.files), 1)

            info = repo.files[0]
            self.assertEqual(info.content, "")
            self.assertEqual(info.line_count, 3)
            self.assertEqual(info.load_content(), "a\nb\nc")
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_scan_files_only_scans_given_paths(self):
        tmp_root = Path(__file__).resolve().parents[1] / ".test_scratch"
        tmp_root.mkdir(parents=True, exist_ok=True)
        root = tmp_root / f"repo_{uuid.uuid4().hex}"
        try:
            root.mkdir()
            (root / "a.py").write_text("a\nb\n", encoding="utf-8")
            (root / "b.py").write_text("c\nd\ne\n", encoding="utf-8")
            scanner = RepoScanner(str(root), max_file_size=1024)
            files = scanner.scan_files([root / "a.py"], include_content=False)
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].line_count, 2)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_scanner_walk_mode_fills_git_blob(self):
        import hashlib

        tmp_root = Path(__file__).resolve().parents[1] / ".test_scratch"
        tmp_root.mkdir(parents=True, exist_ok=True)
        root = tmp_root / f"repo_{uuid.uuid4().hex}"
        try:
            root.mkdir()
            (root / "a.py").write_bytes(b"hello\n")
            scanner = RepoScanner(str(root), max_file_size=1024, prefer_git_source=False)
            repo = scanner.scan(include_content=False)
            self.assertEqual(repo.source_mode, "walk")
            self.assertEqual(len(repo.files), 1)
            # No git → content fingerprint falls back to sha1(content).
            self.assertEqual(repo.files[0].git_blob, hashlib.sha1(b"hello\n").hexdigest())
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
