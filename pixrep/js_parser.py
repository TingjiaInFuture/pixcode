from __future__ import annotations

import bisect
import re

from .models import SemanticMap

JS_CLASS_PAT = re.compile(
    r"^\s*class\s+([A-Za-z_]\w*)(?:\s+extends\s+([A-Za-z_]\w*))?",
    re.MULTILINE,
)
JS_FN_PATS = (
    re.compile(r"^\s*function\s+([A-Za-z_]\w*)\s*\(", re.MULTILINE),
    re.compile(r"^\s*const\s+([A-Za-z_]\w*)\s*=\s*\([^)]*\)\s*=>", re.MULTILINE),
    re.compile(r"^\s*([A-Za-z_]\w*)\s*:\s*function\s*\(", re.MULTILINE),
)
JS_CALL_PAT = re.compile(r"\b([A-Za-z_]\w*)\s*\(")


def _preprocess_non_code_spans(content: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    i = 0
    length = len(content)
    while i < length:
        ch = content[i]
        nxt = content[i + 1] if i + 1 < length else ""

        if ch == "/" and nxt == "/":
            end = content.find("\n", i + 2)
            if end == -1:
                spans.append((i, length))
                break
            spans.append((i, end))
            i = end
            continue

        if ch == "/" and nxt == "*":
            end = content.find("*/", i + 2)
            if end == -1:
                spans.append((i, length))
                break
            spans.append((i, end + 2))
            i = end + 2
            continue

        if ch in {'"', "'", "`"}:
            start = i
            quote = ch
            i += 1
            escaped = False
            while i < length:
                cur = content[i]
                if escaped:
                    escaped = False
                    i += 1
                    continue
                if cur == "\\":
                    escaped = True
                    i += 1
                    continue
                if cur == quote:
                    i += 1
                    break
                i += 1
            spans.append((start, i))
            continue

        i += 1

    return spans


def _span_at(
    index: int, spans: list[tuple[int, int]], span_starts: list[int]
) -> tuple[int, int] | None:
    pos = bisect.bisect_right(span_starts, index) - 1
    if pos < 0:
        return None
    start, end = spans[pos]
    if start <= index < end:
        return (start, end)
    return None


def _find_next_code_brace(
    content: str,
    start: int,
    spans: list[tuple[int, int]],
    span_starts: list[int],
) -> int:
    i = max(0, start)
    length = len(content)
    while i < length:
        span = _span_at(i, spans, span_starts)
        if span is not None:
            i = span[1]
            continue
        if content[i] == "{":
            return i
        i += 1
    return -1


def _balanced_brace_end_fast(
    content: str,
    brace_start: int,
    spans: list[tuple[int, int]],
    span_starts: list[int],
) -> int:
    depth = 0
    i = brace_start
    length = len(content)
    while i < length:
        span = _span_at(i, spans, span_starts)
        if span is not None:
            i = span[1]
            continue

        ch = content[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return length


def js_function_spans(content: str) -> list[tuple[str, int, int]]:
    hits: list[tuple[str, int]] = []
    for pat in JS_FN_PATS:
        for m in pat.finditer(content):
            hits.append((m.group(1), m.start()))

    if not hits:
        return []

    ordered = sorted(hits, key=lambda t: t[1])
    content_len = len(content)
    spans: list[tuple[str, int, int]] = []
    non_code_spans = _preprocess_non_code_spans(content)
    span_starts = [s for s, _ in non_code_spans]

    for name, start in ordered:
        brace_start = _find_next_code_brace(content, start, non_code_spans, span_starts)
        if brace_start == -1:
            continue

        end = _balanced_brace_end_fast(content, brace_start, non_code_spans, span_starts)
        end = min(end, content_len)

        spans.append((name, start, end))

    return spans


def build_js_semantic_map(content: str, *, max_semantic_lines: int) -> SemanticMap:
    classes = JS_CLASS_PAT.findall(content)
    func_spans = js_function_spans(content)
    funcs = {name for name, _, _ in func_spans}

    call_edges: set[tuple[str, str]] = set()
    max_edges = 64
    js_keywords = {"if", "for", "while", "switch", "catch", "function", "return", "new"}
    for src, start, end in func_spans:
        body = content[start:end]
        for callee in JS_CALL_PAT.findall(body)[:400]:
            if callee in js_keywords:
                continue
            if callee in funcs and callee != src:
                call_edges.add((src, callee))
                if len(call_edges) >= max_edges:
                    break
        if len(call_edges) >= max_edges:
            break

    lines: list[str] = []
    if classes:
        lines.append("UML:")
        for class_name, parent in classes[:6]:
            lines.append(f"[Class] {class_name}")
            if parent:
                lines.append(f"{class_name} <|-- {parent}")
    if funcs:
        lines.append("Functions:")
        for func_name in sorted(funcs)[:8]:
            lines.append(f"  - {func_name}()")
    if call_edges:
        lines.append("Call Graph:")
        for src, dst in sorted(call_edges)[:10]:
            lines.append(f"{src} -> {dst}")
    if not lines:
        lines = ["(no symbols detected)"]

    truncated = False
    if len(lines) > max_semantic_lines:
        lines = lines[:max_semantic_lines]
        truncated = True

    return SemanticMap(
        kind="uml+callgraph" if classes else "callgraph",
        lines=lines,
        node_count=len(funcs) + len(classes),
        edge_count=len(call_edges),
        truncated=truncated,
    )
