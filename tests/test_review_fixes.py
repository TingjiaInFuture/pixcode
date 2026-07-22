"""Regression tests for the review-feedback fixes (fix/perf-v4-review).

Each test pins a specific defect called out in 修改意见.txt and asserts the
post-fix behaviour — and is written so it would FAIL on the pre-fix code.
"""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pixrep.fonts import _font_fingerprint, register_fonts
from pixrep.onepdf_pack import (
    PackedFile,
    _ImportsCache,
    _onepdf_build_signature,
    pack_repo_to_one_pdf,
)
from pixrep.scanner import RepoScanner


class TestGitDirtyParsing(unittest.TestCase):
    def test_parses_spaces_rename_untracked_and_nonascii(self):
        # porcelain v1 -z: "XY <path>"; rename/copy is TWO NUL fields
        # "XY <new>\0<old>\0". Paths with spaces must survive intact, and the
        # old-path field must be skipped (not parsed as a bogus record). Paths
        # are raw UTF-8 bytes so a non-ASCII filename doesn't trip the
        # platform-default codec (cp936/GBK on Chinese Windows).
        scanner = RepoScanner(".")
        nonascii = "修改意见.txt".encode()
        fake = (
            b" M src/my module.py\0A  added.py\0R  new.py\0old.py\0"
            b"?? untracked.py\0?? " + nonascii + b"\0"
        )
        proc = mock.Mock(returncode=0, stdout=fake)
        with mock.patch("pixrep.scanner.subprocess.run", return_value=proc):
            dirty = scanner._git_dirty_set()
        self.assertIsNotNone(dirty)
        self.assertEqual(
            dirty,
            {
                "src/my module.py",
                "added.py",
                "new.py",
                "untracked.py",
                "修改意见.txt",
            },
        )

    def test_returns_none_on_failure_and_timeout(self):
        scanner = RepoScanner(".")
        with mock.patch(
            "pixrep.scanner.subprocess.run",
            return_value=mock.Mock(returncode=1, stdout=""),
        ):
            self.assertIsNone(scanner._git_dirty_set())
        with mock.patch(
            "pixrep.scanner.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["git"], timeout=10),
        ):
            self.assertIsNone(scanner._git_dirty_set())


class TestGitFastStrictFallback(unittest.TestCase):
    """P1: a failing `git status` must distrust every snapshot entry, not
    trust all of them (empty dirty set == trust every snapshot)."""

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

    def test_status_failure_invalidates_snapshot(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "a.py").write_bytes(b"aaaa\n")  # 5 bytes
            self._git(["init", "-q"], str(d))
            self._git(["add", "a.py"], str(d))
            self._git(["commit", "-qm", "x"], str(d))

            snap = d / "snap.json"
            s1 = RepoScanner(str(d), max_file_size=1024, snapshot_path=snap)
            repo1 = s1.scan()
            self.assertTrue(repo1.files)
            old_blob = repo1.files[0].git_blob
            self.assertEqual(old_blob, hashlib.sha256(b"aaaa\n").hexdigest())

            st = (d / "a.py").stat()
            # Rewrite with identical size + identical mtime so the snapshot fast
            # path would happily reuse the old hash — unless the file is dirty.
            (d / "a.py").write_bytes(b"bbbb\n")
            os.utime(d / "a.py", ns=(st.st_atime_ns, st.st_mtime_ns))

            s2 = RepoScanner(str(d), max_file_size=1024, snapshot_path=snap)
            with mock.patch.object(RepoScanner, "_git_dirty_set", return_value=None):
                repo2 = s2.scan()
            new_blob = repo2.files[0].git_blob
            # Strict fallback must re-read → new content hash, not the stale one.
            self.assertNotEqual(old_blob, new_blob)
            self.assertEqual(new_blob, hashlib.sha256(b"bbbb\n").hexdigest())


class TestBinarySnapshotPrune(unittest.TestCase):
    """Snapshot prune must use every scanned candidate (text + binary), so a
    binary file isn't dropped and re-probed on every run."""

    def test_binary_entry_survives_prune(self):
        with (
            tempfile.TemporaryDirectory() as repo_d,
            tempfile.TemporaryDirectory() as work_d,
        ):
            repo_d = Path(repo_d)
            snap = Path(work_d) / "snap.json"
            (repo_d / "bin.dat").write_bytes(b"\x00\x01\x02 binary\x00data")
            (repo_d / "a.py").write_text("x=1\n")

            RepoScanner(
                str(repo_d), max_file_size=1024, prefer_git_source=False, snapshot_path=snap
            ).scan()
            data = json.loads(snap.read_text(encoding="utf-8"))
            self.assertIn("bin.dat", data)
            self.assertFalse(data["bin.dat"]["is_text"])

            repo = RepoScanner(
                str(repo_d), max_file_size=1024, prefer_git_source=False, snapshot_path=snap
            ).scan()
            data2 = json.loads(snap.read_text(encoding="utf-8"))
            # Still recorded → second run skips the 8 KB binary probe entirely.
            self.assertIn("bin.dat", data2)
            self.assertEqual(repo.scan_stats.get("skipped_binary"), 1)


class TestImportsCacheIdentity(unittest.TestCase):
    """P1: imports cache key must include current_pkg — two byte-identical
    files in different packages resolve relative imports differently."""

    def test_key_includes_current_pkg(self):
        with tempfile.TemporaryDirectory() as d:
            cache = _ImportsCache(Path(d))
            blob = "same-content"
            cache.save(blob, "pkg_a", ["pkg_a.utils"])
            # Same content, different package → must miss, not collide.
            self.assertIsNone(cache.load(blob, "pkg_b"))
            self.assertEqual(cache.load(blob, "pkg_a"), ["pkg_a.utils"])


class TestFontFingerprint(unittest.TestCase):
    def test_fingerprint_differs_by_file_identity(self):
        empty = _font_fingerprint()
        self.assertNotEqual(empty, "")

        with tempfile.NamedTemporaryFile(delete=False) as tf:
            tf.write(b"font-bytes")
            path = tf.name
        try:
            fp_small = _font_fingerprint(path)
            # Duplicate path collapses → identical fingerprint.
            self.assertEqual(fp_small, _font_fingerprint(path, path))
            # A change in size yields a different fingerprint.
            with open(path, "wb") as f:
                f.write(b"font-bytes-now-longer")
            self.assertNotEqual(fp_small, _font_fingerprint(path))
        finally:
            os.unlink(path)

    def test_register_fonts_exposes_stable_fingerprint(self):
        a = register_fonts()
        b = register_fonts()
        self.assertTrue(a.fingerprint)
        self.assertEqual(a.fingerprint, b.fingerprint)


class TestBuildSignatureIncludesFont(unittest.TestCase):
    def test_font_fingerprint_changes_signature(self):
        f = PackedFile(
            rel_posix="a.py",
            abs_path=Path("a.py"),
            language="python",
            size=3,
            line_count=1,
            git_blob="h",
        )
        base = dict(
            files=[f],
            repo_name="r",
            profile="compact",
            deterministic=True,
            order="importance",
            tab_size=2,
            max_cols=120,
            wrap=True,
            include_tree=True,
            include_index=True,
            font_size=7,
            leading=9,
            page_height=842,
        )
        sig_a = _onepdf_build_signature(**base, font_fingerprint="AAAA")
        sig_b = _onepdf_build_signature(**base, font_fingerprint="BBBB")
        self.assertNotEqual(sig_a, sig_b)


class TestOnepdfIncrementalSkip(unittest.TestCase):
    def test_second_incremental_skips_whole_render(self):
        # cache/snapshot/output live OUTSIDE the repo, otherwise the snapshot
        # file itself gets scanned and changes the build signature every run.
        with (
            tempfile.TemporaryDirectory() as repo_d,
            tempfile.TemporaryDirectory() as work_d,
        ):
            repo_d = Path(repo_d)
            work_d = Path(work_d)
            (repo_d / "a.py").write_text("x=1\n")
            opts = dict(
                repo_root=repo_d,
                out_pdf=work_d / "out.pdf",
                incremental=True,
                cache_dir=work_d / "cache",
                snapshot_path=work_d / "snap.json",
                prefer_git=False,
                core_only=False,
            )
            first = pack_repo_to_one_pdf(**opts)
            self.assertNotEqual(first.get("skipped_incremental", 0), 1)
            second = pack_repo_to_one_pdf(**opts)
            self.assertEqual(second.get("skipped_incremental"), 1)


if __name__ == "__main__":
    unittest.main()
