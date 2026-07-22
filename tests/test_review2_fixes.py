"""Regression tests for the second round of review feedback (0.8.3).

Each test pins a defect from 修改意见2.txt and asserts post-fix behaviour.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from pixrep.constants import IMPORTS_CACHE_SCHEMA_VERSION
from pixrep.file_utils import atomic_write_text, repo_lock
from pixrep.models import RepoInfo
from pixrep.onepdf_pack import _ImportsCache, _python_imports
from pixrep.query import SemanticSearcher
from pixrep.scanner import RepoScanner


class TestSymlinkSkip(unittest.TestCase):
    """Symlinks must be skipped (skipped_path_escape), not followed — a link
    pointing outside the repo must never be read into the PDF."""

    def test_symlink_is_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "secret.txt").write_text("TOPSECRET")
            (d / "a.py").write_text("x=1\n")
            try:
                (d / "link.py").symlink_to(d / "secret.txt")
            except (OSError, NotImplementedError):
                self.skipTest("creating symlinks is not supported here")
            scanner = RepoScanner(str(d), max_file_size=1024, prefer_git_source=False)
            repo = scanner.scan()
            paths = {str(f.path).replace("\\", "/") for f in repo.files}
            self.assertNotIn("link.py", paths)
            self.assertEqual(repo.scan_stats.get("skipped_path_escape"), 1)


class TestImportsCacheSchema(unittest.TestCase):
    def test_old_schema_payload_is_a_miss(self):
        with tempfile.TemporaryDirectory() as d:
            cache = _ImportsCache(Path(d))
            cache.save("blob", "pkg", ["pkg.utils"])
            self.assertEqual(cache.load("blob", "pkg"), ["pkg.utils"])
            # Rewrite with a future/old schema → must be treated as a miss.
            path = cache._path("blob", "pkg")
            path.write_text(
                json.dumps({"schema": IMPORTS_CACHE_SCHEMA_VERSION + 999, "entries": ["stale"]}),
                encoding="utf-8",
            )
            self.assertIsNone(cache.load("blob", "pkg"))


class TestSymbolCacheSchema(unittest.TestCase):
    def test_old_schema_payload_is_a_miss(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            repo = RepoInfo(root=d, name=d.name)
            searcher = SemanticSearcher(repo)
            stale = searcher._files_cache_dir / "stale.json"
            stale.write_text(
                json.dumps({"schema": 999, "entries": [{"name": "x"}]}), encoding="utf-8"
            )
            self.assertIsNone(searcher._load_file_cache(stale))


class TestGitSubdirScan(unittest.TestCase):
    """Scanning a subdirectory of a monorepo must still use git (tracked files
    only), not fall back to os.walk."""

    def setUp(self):
        if shutil.which("git") is None:
            self.skipTest("git not available")

    def _git(self, args: list[str], cwd: str) -> None:
        subprocess.run(
            ["git", "-c", "user.email=t@t.com", "-c", "user.name=t", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
        )

    def test_subdir_uses_tracked_files(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "top.py").write_text("t=1\n")
            (d / "backend").mkdir()
            (d / "backend" / "a.py").write_text("x=1\n")
            (d / "backend" / "b.py").write_text("y=2\n")
            self._git(["init", "-q"], str(d))
            self._git(["add", "-A"], str(d))
            self._git(["commit", "-qm", "init"], str(d))

            scanner = RepoScanner(str(d / "backend"), max_file_size=1024)
            repo = scanner.scan()
            self.assertEqual(repo.source_mode, "git")
            paths = {str(f.path).replace("\\", "/") for f in repo.files}
            self.assertEqual(paths, {"a.py", "b.py"})
            self.assertNotIn("top.py", paths)


class TestRelativeImportFromDot(unittest.TestCase):
    """`from . import utils` must resolve to <pkg>.utils, not just <pkg>."""

    def test_from_dot_import_resolves_names(self):
        imps = _python_imports("from . import utils\n", current_pkg="pkg")
        self.assertIn("pkg.utils", imps)

    def test_from_dot_module_resolves(self):
        imps = _python_imports("from .sub import x\n", current_pkg="pkg")
        self.assertIn("pkg.sub", imps)

    def test_from_dot_star_is_skipped(self):
        imps = _python_imports("from . import *\n", current_pkg="pkg")
        self.assertEqual(imps, set())


class TestAtomicWriteText(unittest.TestCase):
    def test_writes_and_leaves_no_tmp(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "out.json"
            atomic_write_text(p, '{"a": 1}')
            self.assertEqual(p.read_text(encoding="utf-8"), '{"a": 1}')
            # No lingering fixed-name temp file.
            self.assertEqual(list(Path(d).glob("*.tmp")), [])

    def test_overwrite_is_atomic(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "out.json"
            atomic_write_text(p, "v1")
            atomic_write_text(p, "v2")
            self.assertEqual(p.read_text(encoding="utf-8"), "v2")


class TestRepoLock(unittest.TestCase):
    def test_refuses_concurrent_holder(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with repo_lock(root):
                # Same live PID (ours) already holds the lock → a second taker
                # must be refused instead of silently racing.
                self.assertRaises(RuntimeError, repo_lock(root).__enter__)

    def test_released_after_block(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with repo_lock(root):
                pass
            # Lock released → a new acquisition must succeed.
            with repo_lock(root):
                pass


class TestConfigSignature(unittest.TestCase):
    """Lint config signature must hash content, so a rewrite that preserves
    length + mtime still invalidates the cache."""

    def test_content_change_invalidates_even_with_same_size_mtime(self):
        from pixrep.manifest import _config_signature

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "ruff.toml"
            p.write_bytes(b"AAAA")
            sig1 = _config_signature([p])
            st = p.stat()
            p.write_bytes(b"BBBB")  # same length
            os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))  # same mtime
            sig2 = _config_signature([p])
            self.assertNotEqual(sig1, sig2)


if __name__ == "__main__":
    unittest.main()
