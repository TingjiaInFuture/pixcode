import unittest
from pathlib import Path

from pixrep.analysis import CodeInsightEngine
from pixrep.models import FileInfo, RepoInfo


class TestAnalysisOptimizations(unittest.TestCase):
    def setUp(self):
        self.repo = RepoInfo(root=Path.cwd(), name="repo")
        self.engine = CodeInsightEngine(self.repo)

    def test_python_attribute_call_no_false_positive(self):
        content = "\n".join(
            [
                "def get():",
                "    return 1",
                "",
                "def f():",
                "    requests.get('https://example.com')",
                "    return get()",
            ]
        )
        semantic = self.engine._python_semantic_map(content)
        joined = "\n".join(semantic.lines)
        self.assertIn("f -> get", joined)

    def test_python_self_method_qualified_edge(self):
        content = "\n".join(
            [
                "class A:",
                "    def m(self):",
                "        self.n()",
                "",
                "    def n(self):",
                "        return 1",
            ]
        )
        semantic = self.engine._python_semantic_map(content)
        joined = "\n".join(semantic.lines)
        self.assertIn("A.m -> A.n", joined)

    def test_js_brace_balance_ignores_strings_and_comments(self):
        content = "\n".join(
            [
                "function foo() {",
                '  const s = "} {";',
                "  // } in comment",
                "  /* } */",
                "  return bar();",
                "}",
                "function bar() {",
                "  return 1;",
                "}",
            ]
        )
        semantic = self.engine._js_semantic_map(content)
        joined = "\n".join(semantic.lines)
        self.assertIn("foo -> bar", joined)

    def test_python_nested_function_is_tracked(self):
        content = "\n".join(
            [
                "def outer():",
                "    def inner():",
                "        return helper()",
                "    return inner()",
                "",
                "def helper():",
                "    return 1",
            ]
        )
        semantic = self.engine._python_semantic_map(content)
        joined = "\n".join(semantic.lines)
        self.assertIn("outer.inner -> helper", joined)
        self.assertGreaterEqual(semantic.node_count, 3)

    def test_generic_semantic_map_detects_rust_fn(self):
        # Regression: _generic_semantic_map used re.findall but `re` was not
        # imported, silently dropping rust/go/c++/ruby symbols.
        content = "fn main() {}\nfn helper() {}"
        sm = self.engine._generic_semantic_map(content, "rust")
        joined = "\n".join(sm.lines)
        self.assertIn("main", joined)
        self.assertIn("helper", joined)

    def test_build_semantic_map_skips_reading_text_files(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "notes.md"
            p.write_text("# heading\n", encoding="utf-8")
            info = FileInfo(path=Path("notes.md"), abs_path=p, language="markdown", size=10)
            sm = self.engine._build_semantic_map(info)
            self.assertEqual(sm.kind, "none")
            # P0-4: text-like files must not read content from disk.
            self.assertIsNone(info._content_cache)


if __name__ == "__main__":
    unittest.main()
