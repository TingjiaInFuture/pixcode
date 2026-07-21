import tempfile
import unittest
from pathlib import Path

from pixrep.manifest import BuildManifest, FileEntry
from pixrep.models import FileInfo, RepoInfo
from pixrep.pdf_generator import PDFGenerator


class TestP0Correctness(unittest.TestCase):
    def _make_gen(self, root: Path, files: list[FileInfo]) -> PDFGenerator:
        repo = RepoInfo(root=root, name=root.name)
        repo.files = files
        return PDFGenerator(
            repo,
            str(root / "out"),
            enable_semantic_minimap=False,
            enable_lint_heatmap=False,
            incremental=True,
        )

    def test_output_name_stable_without_index(self):
        # P0-2: output name must not embed the sort index, so adding a file that
        # sorts earlier does not rename every other output.
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "a.py").write_text("x=1\n")
            info = FileInfo(
                path=Path("a.py"),
                abs_path=d / "a.py",
                language="python",
                size=4,
                index=7,
                git_blob="b1",
            )
            gen = self._make_gen(d, [info])
            name = gen._file_out_name(info)
            self.assertNotIn("007", name)
            # Stable name + short path-hash suffix (collision avoidance).
            self.assertTrue(name.startswith("a.py__"))
            self.assertTrue(name.endswith(".pdf"))

    def test_output_name_path_hash_avoids_collision(self):
        # P1: a/b_c.py and a_b/c.py must not share an output name.
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            info1 = FileInfo(path=Path("a/b_c.py"), abs_path=d / "x", language="python", size=1)
            info2 = FileInfo(path=Path("a_b/c.py"), abs_path=d / "y", language="python", size=1)
            gen = self._make_gen(d, [info1, info2])
            self.assertNotEqual(gen._file_out_name(info1), gen._file_out_name(info2))

    def test_needs_regeneration_on_working_tree_change(self):
        # P0-1: a change in the working-tree content hash (not the git index
        # OID) must trigger regeneration.
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "a.py").write_text("x=1\n")
            info = FileInfo(
                path=Path("a.py"),
                abs_path=d / "a.py",
                language="python",
                size=4,
                git_blob="hash_old",
            )
            gen = self._make_gen(d, [info])
            manifest = BuildManifest(path=gen.insight_engine.manifest_path)
            manifest.options_hash = gen.repo.options_hash
            manifest.files["a.py"] = FileEntry(git_blob="hash_old", size=4, outputs=["a.py.pdf"])
            (gen.output_dir / "a.py.pdf").write_bytes(b"%PDF- dummy")

            self.assertFalse(gen._needs_regeneration(info, manifest))

            # Simulate an uncommitted edit: working-tree hash changes.
            info.git_blob = "hash_new"
            self.assertTrue(gen._needs_regeneration(info, manifest))

    def test_save_manifest_prunes_deleted_files(self):
        # P0-3: deleting a source file must remove its manifest entry and its
        # stale output from disk.
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "a.py").write_text("x=1\n")
            info = FileInfo(
                path=Path("a.py"),
                abs_path=d / "a.py",
                language="python",
                size=4,
                git_blob="h",
            )
            gen = self._make_gen(d, [info])
            gen.output_dir.mkdir(parents=True, exist_ok=True)
            (gen.output_dir / "gone.py.pdf").write_bytes(b"%PDF- stale")

            manifest = BuildManifest(path=gen.insight_engine.manifest_path)
            manifest.options_hash = gen.repo.options_hash
            manifest.files["gone.py"] = FileEntry(git_blob="x", size=4, outputs=["gone.py.pdf"])

            gen._save_manifest(manifest, {"a.py": ["a.py.pdf"]})

            self.assertNotIn("gone.py", manifest.files)
            self.assertFalse((gen.output_dir / "gone.py.pdf").exists())
            self.assertIn("a.py", manifest.files)


if __name__ == "__main__":
    unittest.main()
