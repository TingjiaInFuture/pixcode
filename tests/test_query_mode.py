import shutil
import tempfile
import unittest
from pathlib import Path

from pixrep.cli import build_parser
from pixrep.models import FileInfo, RepoInfo
from pixrep.query import ContextExtractor, MatchLocation, RipgrepSearcher, SemanticSearcher


class TestQueryMode(unittest.TestCase):
    def test_cli_has_query_command(self):
        parser, _ = build_parser()
        args = parser.parse_args(["query", ".", "-q", "cache", "--type-filter", "py"])
        self.assertEqual(args.command, "query")
        self.assertEqual(args.query, "cache")
        self.assertEqual(args.type_filter, ["py"])

    def test_parse_rg_json(self):
        root = Path.cwd()
        searcher = RipgrepSearcher(repo_root=root)
        payload = (
            '{"type":"begin","data":{}}\n'
            '{"type":"match","data":{"path":{"text":"foo.py"},"line_number":5,'
            '"lines":{"text":"def cache_hit():\\n"},"submatches":[{"start":4,"end":9}]}}\n'
        )
        matches = searcher._parse_rg_json(payload)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].line_number, 5)
        self.assertEqual(matches[0].line_text, "def cache_hit():")
        self.assertEqual(matches[0].submatches, [(4, 9)])

    def test_context_extractor_merges_nearby_matches(self):
        tmpdir = Path(tempfile.mkdtemp(prefix="pixrep_query_"))
        try:
            file_path = tmpdir / "a.py"
            file_path.write_text(
                "\n".join(
                    [
                        "def x():",
                        "    a = 1",
                        "    cache = {}",
                        "    cache['a'] = 1",
                        "    return cache",
                        "",
                        "def y():",
                        "    return 2",
                    ]
                ),
                encoding="utf-8",
            )

            repo = RepoInfo(
                root=tmpdir,
                name="tmp",
                files=[
                    FileInfo(
                        path=Path("a.py"),
                        abs_path=file_path,
                        language="python",
                        size=file_path.stat().st_size,
                        line_count=8,
                    )
                ],
            )
            extractor = ContextExtractor(
                repo=repo, context_lines=1, max_snippet_lines=20, merge_gap=2
            )
            snippets = extractor.extract(
                [
                    MatchLocation("a.py", 3, "    cache = {}"),
                    MatchLocation("a.py", 4, "    cache['a'] = 1"),
                ]
            )
            self.assertEqual(len(snippets), 1)
            self.assertIn(3, snippets[0].match_lines)
            self.assertIn(4, snippets[0].match_lines)
            self.assertLessEqual(snippets[0].start_line, 3)
            self.assertGreaterEqual(snippets[0].end_line, 4)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_semantic_search_python_symbols(self):
        tmpdir = Path(tempfile.mkdtemp(prefix="pixrep_sem_"))
        try:
            file_path = tmpdir / "m.py"
            file_path.write_text(
                "\n".join(
                    [
                        "class CacheEngine:",
                        "    def load(self):",
                        "        return 1",
                        "",
                        "def helper_cache():",
                        "    return CacheEngine()",
                    ]
                ),
                encoding="utf-8",
            )
            repo = RepoInfo(
                root=tmpdir,
                name="tmp",
                files=[
                    FileInfo(
                        path=Path("m.py"),
                        abs_path=file_path,
                        language="python",
                        size=file_path.stat().st_size,
                        line_count=6,
                    )
                ],
            )
            searcher = SemanticSearcher(repo=repo, max_results=20)
            matches = searcher.search("cache", fixed_strings=False, case_sensitive=False)
            self.assertGreaterEqual(len(matches), 2)
            lines = {m.line_number for m in matches}
            self.assertIn(1, lines)
            self.assertIn(5, lines)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_build_match_parses_match_event(self):
        tmpdir = Path(tempfile.mkdtemp(prefix="pixrep_bm_"))
        try:
            file_path = tmpdir / "a.py"
            file_path.write_text("x = 1\n", encoding="utf-8")
            searcher = RipgrepSearcher(repo_root=tmpdir)
            msg = {
                "type": "match",
                "data": {
                    "path": {"text": str(file_path)},
                    "line_number": 5,
                    "lines": {"text": "x = 1\n"},
                    "submatches": [{"start": 0, "end": 1}],
                },
            }
            m = searcher._build_match(msg)
            self.assertIsNotNone(m)
            self.assertEqual(m.line_number, 5)
            self.assertEqual(m.line_text, "x = 1")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_file_cache_key_is_blob_sensitive(self):
        # P1-1: per-file cache key must be stable for the same (rel, blob) and
        # change when the content fingerprint changes.
        k1 = SemanticSearcher._file_cache_key("a.py", "blob1")
        k2 = SemanticSearcher._file_cache_key("a.py", "blob1")
        k3 = SemanticSearcher._file_cache_key("a.py", "blob2")
        self.assertEqual(k1, k2)
        self.assertNotEqual(k1, k3)


if __name__ == "__main__":
    unittest.main()
