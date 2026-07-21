from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SemanticMap:
    kind: str = "none"
    lines: list[str] = field(default_factory=list)
    node_count: int = 0
    edge_count: int = 0
    truncated: bool = False


@dataclass
class LintIssue:
    line: int
    severity: str
    tool: str
    code: str
    message: str


@dataclass
class FileInfo:
    path: Path
    abs_path: Path
    language: str
    size: int
    mtime_ns: int = -1
    line_count: int = 0
    content: str = field(default="", repr=False)
    index: int = 0
    # Content fingerprint: git stage OID (git repos) or sha1(content) fallback.
    git_blob: str | None = None
    semantic_map: SemanticMap = field(default_factory=SemanticMap)
    lint_issues: list[LintIssue] = field(default_factory=list)
    # Internal cache for lazy-loaded content; None means "not yet read".
    _content_cache: str | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        # If a non-empty string was passed at construction, cache it directly
        # so that `load_content()` never re-reads from disk.
        if self.content:
            self._content_cache = self.content

    def load_content(self) -> str:
        """Return file text, reading from disk the first time if not preloaded.

        Prefer this over `.content` when you want lazy loading to kick in.
        Falls back to `.content` for callers that pass content at construction.
        """
        if self._content_cache is None:
            self._content_cache = self.abs_path.read_text(encoding="utf-8", errors="replace")
        return self._content_cache

    def release_content(self) -> None:
        """Drop the in-memory text after the file's PDF has been written."""
        self._content_cache = None
        self.content = ""


@dataclass
class RepoInfo:
    root: Path
    name: str
    files: list[FileInfo] = field(default_factory=list)
    total_lines: int = 0
    total_size: int = 0
    language_stats: dict = field(default_factory=dict)
    tree_str: str = ""
    scan_stats: dict[str, int] = field(default_factory=dict)
    # RepoSnapshot fields: how files were enumerated, the git-tracked set, and a
    # fingerprint of the rendering/cache options (populated by callers).
    source_mode: str = "walk"
    tracked_paths: set[str] = field(default_factory=set)
    options_hash: str = ""
