from __future__ import annotations

import ast
import contextlib
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .constants import ONEPDF_BLOCK_SCHEMA_VERSION, RENDER_SCHEMA_VERSION
from .file_utils import (
    build_tree,
    char_display_width,
    compile_ignore_matcher,
    display_width,
    normalize_posix_path,
)
from .fonts import register_fonts
from .onepdf_writer import StreamingPDFWriter
from .scanner import RepoScanner
from .version import __version__

DEFAULT_CORE_IGNORE_PATTERNS = [
    # Docs / meta
    "*.md",
    "LICENSE*",
    "NOTICE*",
    "CHANGELOG*",
    "CODE_OF_CONDUCT*",
    "CONTRIBUTING*",
    ".github/*",
    "docs/*",
    "doc/*",
    # Tests / fixtures
    "test/*",
    "tests/*",
    "__tests__/*",
    "spec/*",
    "specs/*",
    "fixtures/*",
    "mocks/*",
    "*/*.test.*",
    "*/*.spec.*",
    "*.snap",
]


@dataclass(frozen=True)
class PackedFile:
    rel_posix: str
    abs_path: Path
    language: str
    size: int
    line_count: int
    git_blob: str = ""


def collect_core_files(
    repo_root: Path,
    max_file_size: int,
    extra_ignore: list[str] | None = None,
    core_only: bool = True,
    prefer_git: bool = True,
    include_patterns: list[str] | None = None,
    snapshot_path: Path | None = None,
) -> tuple[list[PackedFile], dict[str, int]]:
    """
    Collect (mostly) code files for ONEPDF_CORE.

    Delegates file discovery and filtering to RepoScanner so that the two
    scanning paths (generate vs onepdf) share a single implementation.
    When prefer_git is True and a git repo is found, applies a secondary
    allow-list derived from `git ls-files` to strip untracked build artefacts.
    """
    extra_ignore = extra_ignore or []
    include_patterns = include_patterns or []

    # Build the combined ignore set including optional core-only extras.
    combined_ignore = [*extra_ignore]
    if core_only:
        # Keep README.md reachable (don't blanket-ignore *.md at the scanner);
        # non-README markdown is filtered out in the loop below so importance
        # can still rank README first.
        combined_ignore = [
            *combined_ignore,
            *(p for p in DEFAULT_CORE_IGNORE_PATTERNS if p != "*.md"),
        ]

    scanner = RepoScanner(
        str(repo_root),
        max_file_size=max_file_size,
        extra_ignore=combined_ignore,
        prefer_git_source=prefer_git,
        snapshot_path=snapshot_path,
    )
    repo = scanner.scan(include_content=False)

    # Restrict to git-tracked files when the scanner enumerated via git
    # (prefer_git asked for it). The tracked set is already populated on
    # RepoInfo by RepoScanner, so reuse it instead of spawning a second
    # `git ls-files` subprocess (P1-4).
    git_set: set[str] | None = repo.tracked_paths if repo.source_mode == "git" else None

    # Build include/exclude matchers for the secondary filters.
    include_match = compile_ignore_matcher(include_patterns) if include_patterns else None

    packed: list[PackedFile] = []
    stats: dict[str, int] = {
        "seen_files": repo.scan_stats.get("seen_files", 0),
        "included": 0,
        "ignored_by_pattern": repo.scan_stats.get("ignored_by_pattern", 0),
        "skipped_unreadable": repo.scan_stats.get("skipped_unreadable", 0),
        "skipped_size_or_empty": repo.scan_stats.get("skipped_size_or_empty", 0),
        "skipped_binary": repo.scan_stats.get("skipped_binary", 0),
        "skipped_not_included": 0,
        "skipped_path_escape": 0,
        "skipped_not_git": 0,
    }

    for info in repo.files:
        rel_posix = normalize_posix_path(info.path)

        # core-only: exclude markdown except README.md (kept for LLM context).
        if (
            core_only
            and rel_posix.lower().endswith(".md")
            and rel_posix.rsplit("/", 1)[-1].lower() != "readme.md"
        ):
            stats["ignored_by_pattern"] = stats.get("ignored_by_pattern", 0) + 1
            continue

        # git allow-list filter.
        if git_set is not None and rel_posix not in git_set:
            stats["skipped_not_git"] = stats.get("skipped_not_git", 0) + 1
            continue

        # Include-pattern filter (if provided, file must match at least one).
        if include_match and not include_match(rel_posix):
            stats["skipped_not_included"] += 1
            continue

        packed.append(
            PackedFile(
                rel_posix=rel_posix,
                abs_path=info.abs_path,
                language=info.language,
                size=info.size,
                line_count=info.line_count,
                git_blob=info.git_blob or "",
            )
        )
        stats["included"] += 1

    packed.sort(key=lambda f: f.rel_posix)
    return packed, stats


def _ascii_safe(s: str, tab_size: int) -> str:
    # Drop CR and expand tabs to the next tab stop (replaces the old fixed
    # "\t" → single-space substitution).
    return s.replace("\r", "").expandtabs(tab_size)


def _wrap_line(line: str, max_cols: int) -> list[str]:
    """Wrap by display width (East Asian Width), not character count, so CJK
    and wide glyphs do not overrun the page."""
    if max_cols <= 0 or display_width(line) <= max_cols:
        return [line]
    result: list[str] = []
    cur: list[str] = []
    cur_w = 0
    for ch in line:
        w = char_display_width(ch)
        if cur and cur_w + w > max_cols:
            result.append("".join(cur))
            cur = [ch]
            cur_w = w
        else:
            cur.append(ch)
            cur_w += w
    if cur:
        result.append("".join(cur))
    return result


def _byte_col_to_char_col(line: str, byte_col: int) -> int:
    """AST col_offset is a UTF-8 byte offset; convert to a character index so
    slicing a str works correctly on lines containing multibyte (e.g. CJK)
    characters."""
    return len(line.encode("utf-8")[:byte_col].decode("utf-8", errors="replace"))


def _strip_python_docstrings(content: str) -> str:
    """AST-safe removal of Python docstrings (module/class/function).

    Only drops a docstring when it is the sole content on its line(s), so a
    construct like ``"doc"; return 1`` is left intact. Functions/classes whose
    body would become empty get a ``pass`` inserted to stay syntactically valid.
    Falls back to the original text on a syntax error.
    """
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return content
    lines = content.splitlines(keepends=True)
    drop: set[int] = set()
    pass_at: dict[int, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        body = node.body
        if not (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            continue
        doc = body[0]
        first_idx = doc.lineno - 1
        last_idx = (doc.end_lineno or doc.lineno) - 1
        if first_idx >= len(lines) or last_idx >= len(lines):
            continue
        # Require the docstring to be alone on its line(s): nothing but
        # whitespace before it on the first line or after it on the last.
        first_col = _byte_col_to_char_col(lines[first_idx], doc.col_offset)
        last_col = _byte_col_to_char_col(lines[last_idx], doc.end_col_offset)
        if lines[first_idx][:first_col].strip():
            continue
        if lines[last_idx][last_col:].strip():
            continue
        for ln in range(doc.lineno, (doc.end_lineno or doc.lineno) + 1):
            drop.add(ln)
        if len(body) == 1 and isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            indent = lines[first_idx][:first_col]
            pass_at[doc.lineno] = f"{indent}pass"
    if not drop:
        return content
    out = []
    for i, line in enumerate(lines, 1):
        if i in drop:
            if i in pass_at:
                out.append(pass_at[i] + "\n")
            continue
        out.append(line)
    return "".join(out)


class _BlockCache:
    """Per-file cache of normalised ONEPDF blocks (ONEPDF v2).

    A block key combines the working-tree content hash with the normalisation
    options, so a warm cache reuses the cleaned/wrapped lines without re-reading
    the source file."""

    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, git_blob: str, opts_sig: str) -> Path:
        key = hashlib.sha256(f"{git_blob}|{opts_sig}".encode()).hexdigest()
        return self.cache_dir / f"{key}.json"

    def load(self, git_blob: str, opts_sig: str) -> list[str] | None:
        path = self._path(git_blob, opts_sig)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if isinstance(data, list):
            return [str(x) for x in data]
        return None

    def save(self, git_blob: str, opts_sig: str, lines: list[str]) -> None:
        path = self._path(git_blob, opts_sig)
        tmp = path.with_name("." + path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(lines, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)


def _normalize_block(
    f: PackedFile,
    tab_size: int,
    max_cols: int,
    compact: bool,
    wrap: bool,
    strip_docs: bool = False,
    block_cache: _BlockCache | None = None,
) -> list[str]:
    opts_sig = (
        f"{ONEPDF_BLOCK_SCHEMA_VERSION}|{f.language}|{tab_size}|{max_cols}|"
        f"{int(compact)}|{int(wrap)}|{int(strip_docs)}"
    )
    if block_cache is not None:
        cached = block_cache.load(f.git_blob, opts_sig)
        if cached is not None:
            return cached
    # Source lines: full text (+ AST docstring strip) for semantic Python,
    # otherwise stream straight from disk.
    if strip_docs and f.language == "python":
        try:
            text = _strip_python_docstrings(
                f.abs_path.read_text(encoding="utf-8", errors="replace")
            )
        except OSError:
            return ["(read failed)"]
        raw_iter: object = iter(text.split("\n"))
    else:
        try:
            raw_iter = f.abs_path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            return ["(read failed)"]
    lines: list[str] = []
    try:
        prev_blank = False
        for raw_line in raw_iter:  # type: ignore[union-attr]
            line = raw_line.rstrip("\n")
            if compact:
                line = line.rstrip()
                is_blank = not line
                if is_blank and prev_blank:
                    continue
                prev_blank = is_blank
            safe_line = _ascii_safe(line, tab_size)
            if wrap:
                lines.extend(_wrap_line(safe_line, max_cols))
            else:
                lines.append(safe_line)
    finally:
        if hasattr(raw_iter, "close"):
            raw_iter.close()  # type: ignore[union-attr]
    if block_cache is not None:
        block_cache.save(f.git_blob, opts_sig, lines)
    return lines


def _importance_key(f: PackedFile) -> tuple[int, str]:
    """Sort key: higher importance first. README/manifest/entrypoints before
    library/utility code; shallower paths before deeper ones; ties broken by
    path for determinism."""
    rel = f.rel_posix.lower()
    name = rel.rsplit("/", 1)[-1]
    score = 0
    if "readme" in name:
        score += 100
    if name in {
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "package.json",
        "cargo.toml",
        "go.mod",
        "pom.xml",
    }:
        score += 90
    if name.startswith(("cli", "main", "app", "__init__", "index")):
        score += 70
    if name.endswith((".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java")):
        score += 30
    score -= rel.count("/") * 2
    return (-score, f.rel_posix)


def _module_name(rel_posix: str) -> str:
    """pixrep/scanner.py -> pixrep.scanner; pixrep/__init__.py -> pixrep."""
    stem = rel_posix[:-3] if rel_posix.endswith(".py") else rel_posix
    parts = [p for p in stem.split("/") if p]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _python_imports(content: str, current_pkg: str = "") -> set[str]:
    """Resolve imported module names, including relative imports (PEP 328)
    resolved against ``current_pkg``. Returns fully-qualified names so they can
    match the full module names produced by ``_module_name``."""
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return set()
    mods: set[str] = set()
    pkg_parts = current_pkg.split(".") if current_pkg else []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mods.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                # Relative import: resolve against the current package.
                drop = node.level - 1
                base = pkg_parts[: len(pkg_parts) - drop] if drop <= len(pkg_parts) else []
                target = [*base, node.module] if node.module else base
                if target:
                    mods.add(".".join(target))
            elif node.module:
                mods.add(node.module)
    return mods


class _ImportsCache:
    """Cache of a Python file's resolved imports keyed by (content hash,
    current package), so --order dependency doesn't re-read + AST-parse every
    Python file when the snapshot/block caches are warm.

    The package is part of the key because relative imports (``from . import x``)
    resolve differently depending on which package the file lives in: two files
    with byte-identical content but different packages yield different resolved
    module names and must not share a cache entry."""

    def __init__(self, cache_dir: Path | None):
        self.cache_dir = cache_dir / "imports" if cache_dir else None
        if self.cache_dir is not None:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, blob: str, current_pkg: str) -> Path | None:
        if self.cache_dir is None:
            return None
        key = hashlib.sha256(f"{blob}\0{current_pkg}".encode()).hexdigest()
        return self.cache_dir / f"{key}.json"

    def load(self, blob: str, current_pkg: str) -> list[str] | None:
        path = self._path(blob, current_pkg)
        if path is None or not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, list) else None

    def save(self, blob: str, current_pkg: str, imports: list[str]) -> None:
        path = self._path(blob, current_pkg)
        if path is None:
            return
        with contextlib.suppress(OSError):
            _atomic_write_text(path, json.dumps(imports))


def _dependency_order(files: list[PackedFile], cache_dir: Path | None = None) -> None:
    """Order files so widely-depended-on Python modules come first (an
    approximation of import-topological order; non-Python falls back to
    importance). Imports are resolved as full module names with relative imports
    resolved against each file's package, and cached by content hash."""
    mod_to_rel = {_module_name(f.rel_posix): f.rel_posix for f in files if f.language == "python"}
    popularity = {f.rel_posix: 0 for f in files}
    imports_cache = _ImportsCache(cache_dir)
    for f in files:
        if f.language != "python":
            continue
        pkg = _module_name(str(f.rel_posix).rsplit("/", 1)[0]) if "/" in f.rel_posix else ""
        cached = imports_cache.load(f.git_blob, pkg)
        if cached is not None:
            imps = set(cached)
        else:
            try:
                content = f.abs_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            imps = _python_imports(content, pkg)
            imports_cache.save(f.git_blob, pkg, sorted(imps))
        for mod, target_rel in mod_to_rel.items():
            if target_rel == f.rel_posix:
                continue
            # f depends on target if it imports target's module exactly or as a
            # prefix (e.g. `import pixrep.scanner` depends on pixrep.scanner).
            if mod in imps or any(i.startswith(mod + ".") for i in imps):
                popularity[target_rel] += 1
    files.sort(key=lambda f: (-popularity[f.rel_posix], _importance_key(f)))


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_name("." + path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)


def _onepdf_build_signature(
    files: list[PackedFile],
    *,
    repo_name: str,
    profile: str,
    deterministic: bool,
    order: str,
    tab_size: int,
    max_cols: int,
    wrap: bool,
    include_tree: bool,
    include_index: bool,
    font_size: int,
    leading: int,
    page_height: int,
    font_fingerprint: str = "",
) -> str:
    """Fingerprint of every input that affects the ONEPDF output bytes. If it
    matches the previous build and the output exists, the whole PDF render is
    skipped (--incremental). The font fingerprint covers the actual backing
    font file (not just the point size), so swapping the system CJK font
    invalidates the cache."""
    parts = [
        f"repo={repo_name}",
        f"profile={profile}|det={int(deterministic)}|order={order}",
        f"tab={tab_size}|cols={max_cols}|wrap={int(wrap)}",
        f"font={font_size}|leading={leading}|page={page_height}|fp={font_fingerprint}",
        f"tree={int(include_tree)}|index={int(include_index)}",
        f"schema={ONEPDF_BLOCK_SCHEMA_VERSION}|{RENDER_SCHEMA_VERSION}|{__version__}",
    ]
    for f in files:
        parts.append(f"{f.rel_posix}:{f.git_blob}:{f.language}")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def pack_repo_to_one_pdf(
    repo_root: Path,
    out_pdf: Path,
    max_file_size: int = 512 * 1024,
    extra_ignore: list[str] | None = None,
    core_only: bool = True,
    prefer_git: bool = True,
    include_patterns: list[str] | None = None,
    max_cols: int = 120,
    wrap: bool = True,
    tab_size: int = 2,
    include_tree: bool = True,
    include_index: bool = True,
    profile: str = "compact",
    deterministic: bool = False,
    order: str = "importance",
    cache_dir: Path | None = None,
    snapshot_path: Path | None = None,
    incremental: bool = False,
) -> dict[str, int]:
    """Pack repository files into a single minimized PDF (ONEPDF_CORE).

    Streaming: lines are emitted one at a time and flushed without ever
    building the full line list in memory.

    profile="compact" squeezes blank lines, strips trailing whitespace and
    uses a compact ``@@ <path> <lang>`` header; "lossless" keeps the original
    formatting and verbose headers. ``deterministic`` omits the generation
    timestamp and fixes PDF metadata. ``order`` controls file ordering.
    """
    files, stats = collect_core_files(
        repo_root=repo_root,
        max_file_size=max_file_size,
        extra_ignore=extra_ignore,
        core_only=core_only,
        prefer_git=prefer_git,
        include_patterns=include_patterns,
        snapshot_path=snapshot_path,
    )
    if order == "dependency":
        _dependency_order(files, cache_dir)
    elif order == "importance":
        files.sort(key=_importance_key)
    else:
        files.sort(key=lambda x: (x.rel_posix,))

    font_size = 7
    leading = 9
    top = 36
    bottom = 36
    page_height = 842
    max_lines = max(1, int((page_height - top - bottom) / leading))

    # Resolve the font once and reuse it for both the build signature (so a
    # system-font swap invalidates --incremental) and the writer (so we don't
    # register fonts twice).
    fonts = register_fonts()

    # --incremental: if the build signature is unchanged and the output exists,
    # skip the whole PDF render.
    sig = ""
    if incremental:
        sig = _onepdf_build_signature(
            files,
            repo_name=repo_root.name,
            profile=profile,
            deterministic=deterministic,
            order=order,
            tab_size=tab_size,
            max_cols=max_cols,
            wrap=wrap,
            include_tree=include_tree,
            include_index=include_index,
            font_size=font_size,
            leading=leading,
            page_height=page_height,
            font_fingerprint=fonts.fingerprint,
        )
        sig_path = out_pdf.with_name(out_pdf.name + ".buildsig")
        if out_pdf.exists() and sig_path.exists():
            try:
                meta = json.loads(sig_path.read_text(encoding="utf-8"))
                if meta.get("input_signature") == sig:
                    actual_size = out_pdf.stat().st_size
                    if meta.get("output_size") == actual_size and meta.get(
                        "output_sha256"
                    ) == _sha256_file(out_pdf):
                        stats["skipped_incremental"] = 1
                        stats["output_bytes"] = actual_size
                        stats["pages"] = int(meta.get("pages", 0))
                        return stats
            except (OSError, json.JSONDecodeError):
                pass

    compact = profile in {"compact", "semantic"}
    strip_docs = profile == "semantic"

    current: list[str] = []
    writer = StreamingPDFWriter(
        title=f"{repo_root.name} onepdf",
        out_path=out_pdf,
        fonts=fonts,
        deterministic=deterministic,
    )

    def flush_page() -> None:
        if not current:
            return
        writer.add_page_lines(current, font_size=font_size, leading=leading)
        current.clear()

    def emit(line: str) -> None:
        current.append(line)
        if len(current) >= max_lines:
            flush_page()

    # ── Header ────────────────────────────────────────────────────────
    emit("pixrep onepdf")
    emit(f"repo: {repo_root.name}")
    if not deterministic:
        emit(f"generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    emit(f"files: {len(files)}")
    emit("")

    if include_tree:
        emit("== tree ==")
        tree_str = build_tree([f.rel_posix for f in files], repo_root.name, style="ascii")
        for tree_line in tree_str.split("\n"):
            emit(tree_line)
        emit("")

    if include_index:
        emit("== index ==")
        for idx, f in enumerate(files, start=1):
            if compact:
                emit(f"{idx:04d} {f.rel_posix}")
            else:
                emit(f"{idx:04d}  {f.rel_posix}  ({f.line_count} lines, {f.size} bytes)")
        emit("")

    emit("== files ==")
    emit("")

    # ── File content ──────────────────────────────────────────────────
    block_cache = _BlockCache(cache_dir) if cache_dir else None
    for idx, f in enumerate(files, start=1):
        if compact:
            emit(f"@@ {f.rel_posix} {f.language}")
        else:
            header = (
                f"[{idx:04d}] {f.rel_posix}  ({f.language}, {f.line_count} lines, {f.size} bytes)"
            )
            emit(header)
            emit("-" * min(max_cols, max(10, len(header))))
        for line in _normalize_block(f, tab_size, max_cols, compact, wrap, strip_docs, block_cache):
            emit(line)
        emit("")

    flush_page()

    writer.finalize()
    stats["pages"] = writer.page_count
    stats["output_bytes"] = int(out_pdf.stat().st_size) if out_pdf.exists() else 0
    if incremental:
        sig_path = out_pdf.with_name(out_pdf.name + ".buildsig")
        meta = {
            "input_signature": sig,
            "output_sha256": _sha256_file(out_pdf) if out_pdf.exists() else "",
            "output_size": out_pdf.stat().st_size if out_pdf.exists() else 0,
            "pages": writer.page_count,
        }
        with contextlib.suppress(OSError):
            _atomic_write_text(sig_path, json.dumps(meta, ensure_ascii=False))
    return stats
