from __future__ import annotations

import contextlib
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .file_utils import (
    build_tree,
    char_display_width,
    compile_ignore_matcher,
    display_width,
    normalize_posix_path,
)
from .onepdf_writer import StreamingPDFWriter
from .scanner import RepoScanner

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
        combined_ignore = [*combined_ignore, *DEFAULT_CORE_IGNORE_PATTERNS]

    scanner = RepoScanner(
        str(repo_root),
        max_file_size=max_file_size,
        extra_ignore=combined_ignore,
        prefer_git_source=prefer_git,
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
        with contextlib.suppress(OSError):
            path.write_text(json.dumps(lines, ensure_ascii=False), encoding="utf-8")


def _normalize_block(
    f: PackedFile,
    tab_size: int,
    max_cols: int,
    compact: bool,
    wrap: bool,
    block_cache: _BlockCache | None = None,
) -> list[str]:
    opts_sig = f"{tab_size}|{max_cols}|{int(compact)}|{int(wrap)}"
    if block_cache is not None:
        cached = block_cache.load(f.git_blob, opts_sig)
        if cached is not None:
            return cached
    lines: list[str] = []
    try:
        with f.abs_path.open("r", encoding="utf-8", errors="replace") as src:
            prev_blank = False
            for raw_line in src:
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
    except OSError:
        lines = ["(read failed)"]
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
    )
    files.sort(key=_importance_key if order == "importance" else lambda x: (x.rel_posix,))

    compact = profile == "compact"

    font_size = 7
    leading = 9
    top = 36
    bottom = 36
    page_height = 842
    max_lines = max(1, int((page_height - top - bottom) / leading))

    current: list[str] = []
    writer = StreamingPDFWriter(
        title=f"{repo_root.name} onepdf",
        out_path=out_pdf,
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
        for line in _normalize_block(f, tab_size, max_cols, compact, wrap, block_cache):
            emit(line)
        emit("")

    flush_page()

    writer.finalize()
    stats["pages"] = writer.page_count
    stats["output_bytes"] = int(out_pdf.stat().st_size) if out_pdf.exists() else 0
    return stats
