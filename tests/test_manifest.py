import tempfile
import unittest
from pathlib import Path

from pixrep.manifest import BuildManifest, FileEntry, compute_options_hash


class TestManifest(unittest.TestCase):
    def test_options_hash_is_stable_and_hex(self):
        h1 = compute_options_hash(theme_id="t", font_id="f", png_dpi=150)
        h2 = compute_options_hash(theme_id="t", font_id="f", png_dpi=150)
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 40)

    def test_options_hash_changes_on_any_field(self):
        base = compute_options_hash(
            theme_id="t",
            font_id="f",
            png_dpi=150,
            output_format="pdf",
            enable_semantic=True,
            enable_lint=True,
        )
        self.assertNotEqual(base, compute_options_hash(theme_id="t2", font_id="f", png_dpi=150))
        self.assertNotEqual(base, compute_options_hash(theme_id="t", font_id="f2", png_dpi=150))
        self.assertNotEqual(base, compute_options_hash(theme_id="t", font_id="f", png_dpi=300))
        self.assertNotEqual(
            base, compute_options_hash(theme_id="t", font_id="f", output_format="png")
        )
        self.assertNotEqual(
            base, compute_options_hash(theme_id="t", font_id="f", enable_semantic=False)
        )

    def test_manifest_save_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "manifest.json"
            m = BuildManifest(path=path, pixrep_version="0.8.0", options_hash="abc")
            m.files["src/a.py"] = FileEntry(
                git_blob="oid1", size=100, outputs=["001_a.py.pdf"], mtime_ns=123
            )
            m.save()
            self.assertTrue(path.exists())

            loaded = BuildManifest.load(path)
            self.assertEqual(loaded.pixrep_version, "0.8.0")
            self.assertEqual(loaded.options_hash, "abc")
            self.assertIn("src/a.py", loaded.files)
            entry = loaded.files["src/a.py"]
            self.assertEqual(entry.git_blob, "oid1")
            self.assertEqual(entry.size, 100)
            self.assertEqual(entry.outputs, ["001_a.py.pdf"])

    def test_manifest_load_missing_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            loaded = BuildManifest.load(Path(d) / "does_not_exist.json")
            self.assertEqual(loaded.files, {})
            self.assertEqual(loaded.options_hash, "")


if __name__ == "__main__":
    unittest.main()
