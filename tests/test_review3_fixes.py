"""Regression tests for the third round of review feedback (0.8.4).

Pins the P0 repo_lock rewrite, path-escape resolve, dependency alias imports,
core-only scope, save_snapshot=False, and path_escape stat passthrough.
"""

import tempfile
import unittest
import uuid
from pathlib import Path

from pixrep.file_utils import repo_lock
from pixrep.onepdf_pack import _python_imports, collect_core_files
from pixrep.scanner import RepoScanner


class TestRepoLockOsFileLock(unittest.TestCase):
    """The OS file lock must not delete .pixrep.lock and must still refuse a
    second holder — and must NOT probe PIDs with os.kill (Windows TerminateProcess)."""

    def test_lock_file_persists_after_release(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with repo_lock(root):
                pass
            # OS-lock files are reused, not unlinked (unlike the old PID-file lock).
            self.assertTrue((root / ".pixrep.lock").exists())

    def test_refuses_concurrent_holder(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with repo_lock(root):
                self.assertRaises(RuntimeError, repo_lock(root).__enter__)


class TestPathEscapeResolve(unittest.TestCase):
    """A "../" path that resolves outside the repo must be rejected, not read."""

    def test_scan_files_rejects_dotdot_escape(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "a.py").write_text("x=1\n")
            tag = uuid.uuid4().hex
            outside = d.parent / f"pixrep_escape_{tag}.py"
            outside.write_text("SECRET")
            try:
                scanner = RepoScanner(str(d), max_file_size=1024, prefer_git_source=False)
                escape = Path(str(d)) / ".." / outside.name
                files = scanner.scan_files([escape], include_content=False)
                self.assertEqual(files, [])
            finally:
                outside.unlink(missing_ok=True)


class TestDependencyAlias(unittest.TestCase):
    """`from pkg import x` and `from .sub import x` must record both the module
    and the module.alias candidate, so a dependency on the imported name is seen."""

    def test_absolute_module_alias(self):
        imps = _python_imports("from pixrep import scanner\n", current_pkg="")
        self.assertIn("pixrep", imps)
        self.assertIn("pixrep.scanner", imps)

    def test_relative_module_alias(self):
        imps = _python_imports("from .sub import scanner\n", current_pkg="pkg")
        self.assertIn("pkg.sub", imps)
        self.assertIn("pkg.sub.scanner", imps)


class TestCoreOnlyScope(unittest.TestCase):
    """core-only keeps ONLY the root README.md and drops nested tests dirs."""

    def test_root_readme_kept_nested_readme_and_tests_dropped(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "README.md").write_text("# root")
            (d / "a.py").write_text("x=1\n")
            (d / "sub").mkdir()
            (d / "sub" / "README.md").write_text("# nested")
            (d / "sub" / "tests").mkdir()
            (d / "sub" / "tests" / "t.py").write_text("x=1\n")
            packed, _ = collect_core_files(d, max_file_size=1024, core_only=True, prefer_git=False)
            rels = {f.rel_posix for f in packed}
            self.assertIn("README.md", rels)
            self.assertIn("a.py", rels)
            self.assertNotIn("sub/README.md", rels)
            self.assertNotIn("sub/tests/t.py", rels)


class TestSaveSnapshotFlag(unittest.TestCase):
    """save_snapshot=False must not persist the snapshot (read-only commands)."""

    def test_false_skips_write(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "a.py").write_text("x=1\n")
            snap = d / "snap.json"
            RepoScanner(
                str(d), max_file_size=1024, prefer_git_source=False, snapshot_path=snap
            ).scan(save_snapshot=False)
            self.assertFalse(snap.exists())
            RepoScanner(
                str(d), max_file_size=1024, prefer_git_source=False, snapshot_path=snap
            ).scan()
            self.assertTrue(snap.exists())


class TestPathEscapeStatPassthrough(unittest.TestCase):
    """collect_core_files must surface the scanner's skipped_path_escape count."""

    def test_symlink_count_passed_through(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "a.py").write_text("x=1\n")
            try:
                (d / "link.py").symlink_to(d / "a.py")
            except (OSError, NotImplementedError):
                self.skipTest("creating symlinks is not supported here")
            _, stats = collect_core_files(d, max_file_size=1024, core_only=False, prefer_git=False)
            self.assertGreaterEqual(stats.get("skipped_path_escape", 0), 1)


if __name__ == "__main__":
    unittest.main()
