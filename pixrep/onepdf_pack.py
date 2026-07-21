from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .constants import DEFAULT_IGNORE_PATTERNS
from .file_utils import (
    build_tree,
    compile_ignore_matcher,
    normalize_posix_path,
)
from .onepdf_writer import StreamingPDFWriter
from .scanner import RepoScanner

# Pre-built translate table shared by all _ascii_safe calls.
_ASCII_SAFE_TABLE = str.maketrans({"\t": "    ", "\r": ""})


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
            )
        )
        stats["included"] += 1

    packed.sort(key=lambda f: f.rel_posix)
    return packed, stats


def _ascii_safe(s: str) -> str:
    return s.translate(_ASCII_SAFE_TABLE)


def _wrap_line(line: str, max_cols: int) -> list[str]:
    if max_cols <= 0 or len(line) <= max_cols:
        return [line]
    return [line[i : i + max_cols] for i in range(0, len(line), max_cols)]


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
    include_tree: bool = True,
    include_index: bool = True,
) -> dict[str, int]:
    """Pack repository files into a single minimized PDF (ONEPDF_CORE).

    Lines are emitted one at a time and flushed into page streams without
    ever building the full line list in memory (streaming approach).
    """
    files, stats = collect_core_files(
        repo_root=repo_root,
        max_file_size=max_file_size,
        extra_ignore=extra_ignore,
        core_only=core_only,
        prefer_git=prefer_git,
        include_patterns=include_patterns,
    )

    font_size = 7
    leading = 9  # points
    top = 36
    bottom = 36
    page_height = 842
    max_lines = max(1, int((page_height - top - bottom) / leading))

    current: list[str] = []
    writer = StreamingPDFWriter(title=f"{repo_root.name} onepdf", out_path=out_pdf)

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
            emit(f"{idx:04d}  {f.rel_posix}  ({f.line_count} lines, {f.size} bytes)")
        emit("")

    emit("== files ==")
    emit("")

    # ── File content ──────────────────────────────────────────────────
    for idx, f in enumerate(files, start=1):
        header = f"[{idx:04d}] {f.rel_posix}  ({f.language}, {f.line_count} lines, {f.size} bytes)"
        emit(header)
        emit("-" * min(max_cols, max(10, len(header))))
        try:
            src = f.abs_path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            emit("(read failed)")
            emit("")
            continue
        with src:
            for raw_line in src:
                safe_line = _ascii_safe(raw_line.rstrip())
                if wrap:
                    for chunk in _wrap_line(safe_line, max_cols):
                        emit(chunk)
                else:
                    emit(safe_line)
        emit("")

    flush_page()

    writer.finalize()
    stats["pages"] = writer.page_count
    stats["output_bytes"] = int(out_pdf.stat().st_size) if out_pdf.exists() else 0
    return stats
