from __future__ import annotations

import ast
from collections import defaultdict

from .models import SemanticMap


class PyCombinedVisitor(ast.NodeVisitor):
    def __init__(self, ast_name_resolver):
        self._ast_name = ast_name_resolver
        self.class_stack: list[str] = []
        self.function_depth = 0
        self.classes: dict[str, list[str]] = {}
        self.class_methods: dict[str, set[str]] = defaultdict(set)
        self.module_funcs: set[str] = set()
        self.qualified_methods: set[str] = set()
        self.nested_funcs: set[str] = set()
        self.inherits: list[tuple[str, str]] = []
        self.scope: list[str] = ["(module)"]
        self.edges: set[tuple[str, str]] = set()

    def visit_ClassDef(self, node: ast.ClassDef):
        self.class_stack.append(node.name)
        self.classes.setdefault(node.name, [])
        for base in node.bases:
            bname = self._ast_name(base)
            if bname:
                self.inherits.append((node.name, bname))
        self.generic_visit(node)
        self.class_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef):
        current = self._record_function(node.name)
        self.scope.append(current)
        self.function_depth += 1
        self.generic_visit(node)
        self.function_depth -= 1
        self.scope.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        current = self._record_function(node.name)
        self.scope.append(current)
        self.function_depth += 1
        self.generic_visit(node)
        self.function_depth -= 1
        self.scope.pop()

    def visit_Call(self, node: ast.Call):
        callee = self._call_name(node.func)
        if callee:
            self.edges.add((self.scope[-1], callee))
        self.generic_visit(node)

    def _record_function(self, name: str) -> str:
        if self.class_stack and self.function_depth == 0:
            cls = self.class_stack[-1]
            self.classes.setdefault(cls, []).append(name)
            self.class_methods[cls].add(name)
            qualified = f"{cls}.{name}"
            self.qualified_methods.add(qualified)
            return qualified

        if not self.class_stack and self.function_depth == 0:
            self.module_funcs.add(name)
            return name

        parent_scope = self.scope[-1]
        qualified_nested = name if parent_scope == "(module)" else f"{parent_scope}.{name}"
        self.nested_funcs.add(qualified_nested)
        return qualified_nested

    def _call_name(self, node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            owner = node.value
            method = node.attr
            if isinstance(owner, ast.Name):
                if owner.id in {"self", "cls"} and self.class_stack:
                    cls = self.class_stack[-1]
                    return f"{cls}.{method}"
                if owner.id in self.class_methods:
                    return f"{owner.id}.{method}"
        return ""


def build_python_semantic_map(
    content: str,
    *,
    ast_name_resolver,
    max_semantic_lines: int,
) -> SemanticMap:
    content = content.lstrip("\ufeff")
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return SemanticMap(kind="callgraph", lines=["(parse failed)"], node_count=0, edge_count=0)

    collector = PyCombinedVisitor(ast_name_resolver)
    collector.visit(tree)

    classes: dict[str, list[str]] = collector.classes
    inherits = collector.inherits
    defined = (
        set(collector.module_funcs) | set(collector.qualified_methods) | set(collector.nested_funcs)
    )
    edges = {(src, dst) for src, dst in collector.edges if dst in defined}

    lines: list[str] = []
    if classes:
        lines.append("UML:")
        for class_name, class_methods in list(classes.items())[:6]:
            lines.append(f"[Class] {class_name}")
            for method in class_methods[:4]:
                lines.append(f"  - {method}()")
        for child, parent in inherits[:6]:
            lines.append(f"{child} <|-- {parent}")
    if edges:
        lines.append("Call Graph:")
        for src, dst in list(sorted(edges))[:10]:
            lines.append(f"{src} -> {dst}")
    if not lines:
        lines = ["(no classes/functions detected)"]

    truncated = False
    if len(lines) > max_semantic_lines:
        lines = lines[:max_semantic_lines]
        truncated = True

    return SemanticMap(
        kind="uml+callgraph" if classes else "callgraph",
        lines=lines,
        node_count=len(defined),
        edge_count=len(edges),
        truncated=truncated,
    )
