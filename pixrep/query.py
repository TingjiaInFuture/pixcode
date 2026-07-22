"""Code query engine using ripgrep and semantic symbol indexing."""

from __future__ import annotations

import ast
import contextlib
import fnmatch
import hashlib
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .constants import DEFAULT_IGNORE_PATTERNS, QUERY_SYMBOL_CACHE_SCHEMA_VERSION
from .file_utils import (
    atomic_write_text,
    compile_ignore_matcher,
    normalize_posix_path,
    resolve_repo_cache_root,
    should_ignore_dir,
)
from .models import FileInfo, RepoInfo
from .version import __version__

log = logging.getLogger(__name__)

SCOPE_LOOKBACK_LINES = 120


@dataclass(frozen=True)
class MatchLocation:
    """A single line match from ripgrep or semantic indexing."""

    rel_path: str
    line_number: int
    line_text: str
    submatches: list[tuple[int, int]] = field(default_factory=list)


@dataclass
class CodeSnippet:
    """A contextual code snippet extracted around match locations."""

    rel_path: str
    language: str
    start_line: int
    end_line: int
    lines: list[str]
    match_lines: list[int]
    abs_path: Path | None = None


@dataclass(frozen=True)
class SymbolEntry:
    rel_path: str
    line_number: int
    line_text: str
    name: str
    qualified: str
    kind: str


class RipgrepSearcher:
    """Wrapper around ripgrep for structured code search."""

    def __init__(
        self,
        repo_root: Path,
        timeout: int = 30,
        max_results: int = 500,
    ):
        self.repo_root = repo_root.resolve()
        self.timeout = timeout
        self.max_results = max_results
        self._rg_available = shutil.which("rg") is not None

    @property
    def available(self) -> bool:
        return self._rg_available

    def search(
        self,
        pattern: str,
        *,
        file_globs: list[str] | None = None,
        type_filters: list[str] | None = None,
        fixed_strings: bool = False,
        case_sensitive: bool = False,
        context_lines: int = 0,
    ) -> list[MatchLocation]:
        """Run ripgrep and return structured match locations.

        Streams rg's JSON output line by line and stops as soon as
        ``max_results`` matches are collected, terminating the subprocess
        instead of buffering its entire output (P0-2).
        """
        if not self._rg_available:
            log.warning("ripgrep (rg) not found; falling back to basic search")
            return self._fallback_search(
                pattern,
                fixed_strings=fixed_strings,
                case_sensitive=case_sensitive,
                file_globs=file_globs,
            )

        cmd = [
            "rg",
            "--json",
            "--max-count",
            "50",
            "--max-columns",
            "500",
            "--no-heading",
        ]

        if fixed_strings:
            cmd.append("--fixed-strings")
        if not case_sensitive:
            cmd.append("--smart-case")
        if context_lines > 0:
            cmd.extend(["-C", str(context_lines)])
        if file_globs:
            for glob in file_globs:
                cmd.extend(["--glob", glob])
        if type_filters:
            for type_name in type_filters:
                cmd.extend(["--type", type_name])

        cmd.append("--")
        cmd.append(pattern)
        cmd.append(str(self.repo_root))

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                cwd=str(self.repo_root),
                bufsize=1,
            )
        except OSError:
            log.debug("ripgrep invocation failed")
            return []

        matches: list[MatchLocation] = []
        # Reader thread + deadline so self.timeout bounds wall-clock time even
        # if rg blocks on a special filesystem (P0-5). The previous
        # `for line in proc.stdout` was a blocking read with no timeout check.
        out_q: queue.Queue[str | None] = queue.Queue()

        def _reader() -> None:
            try:
                if proc.stdout is not None:
                    for line in proc.stdout:
                        out_q.put(line)
            finally:
                out_q.put(None)

        reader = threading.Thread(target=_reader, daemon=True)
        reader.start()
        deadline = time.monotonic() + max(1.0, float(self.timeout))
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    proc.terminate()
                    break
                try:
                    line = out_q.get(timeout=min(remaining, 0.5))
                except queue.Empty:
                    if proc.poll() is not None and out_q.empty():
                        break
                    continue
                if line is None:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                m = self._build_match(msg)
                if m is not None:
                    matches.append(m)
                    if len(matches) >= self.max_results:
                        proc.terminate()
                        break
            proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            self._kill(proc)
        finally:
            self._kill(proc)
            if proc.stdout is not None:
                with contextlib.suppress(OSError):
                    proc.stdout.close()
        return matches

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            with contextlib.suppress(OSError):
                proc.kill()

    def _build_match(self, msg: dict) -> MatchLocation | None:
        if msg.get("type") != "match":
            return None
        data = msg.get("data", {})
        path_data = data.get("path", {})
        path_text = path_data.get("text", "")

        try:
            abs_p = Path(path_text).resolve()
            rel = abs_p.relative_to(self.repo_root)
            rel_posix = normalize_posix_path(rel)
        except (ValueError, OSError):
            rel_posix = normalize_posix_path(path_text)

        line_number = int(data.get("line_number", 0))
        lines_data = data.get("lines", {})
        line_text = lines_data.get("text", "").rstrip("\n")

        submatches: list[tuple[int, int]] = []
        for sm in data.get("submatches", []):
            submatches.append((int(sm.get("start", 0)), int(sm.get("end", 0))))

        return MatchLocation(
            rel_path=rel_posix,
            line_number=line_number,
            line_text=line_text,
            submatches=submatches,
        )

    def _parse_rg_json(self, output: str) -> list[MatchLocation]:
        """Parse already-buffered rg JSON output (kept for tests/fallback)."""
        matches: list[MatchLocation] = []
        for line in output.splitlines():
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            m = self._build_match(msg)
            if m is not None:
                matches.append(m)
                if len(matches) >= self.max_results:
                    break
        return matches

    def _fallback_search(
        self,
        pattern: str,
        *,
        fixed_strings: bool,
        case_sensitive: bool,
        file_globs: list[str] | None,
    ) -> list[MatchLocation]:
        flags = 0 if case_sensitive else re.IGNORECASE
        if fixed_strings:
            compiled = re.compile(re.escape(pattern), flags)
        else:
            try:
                compiled = re.compile(pattern, flags)
            except re.error:
                compiled = re.compile(re.escape(pattern), flags)

        ignore_match = compile_ignore_matcher(DEFAULT_IGNORE_PATTERNS)
        matches: list[MatchLocation] = []
        for dirpath, dirnames, filenames in os.walk(self.repo_root):
            dirnames[:] = sorted(d for d in dirnames if not should_ignore_dir(d))
            for fname in filenames:
                fp = Path(dirpath) / fname
                rel = normalize_posix_path(fp.relative_to(self.repo_root))
                if file_globs and not _glob_accepts(rel, file_globs):
                    continue
                if ignore_match(rel):
                    continue
                try:
                    text = fp.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for i, line in enumerate(text.split("\n"), 1):
                    found = compiled.search(line)
                    if not found:
                        continue
                    matches.append(
                        MatchLocation(
                            rel_path=rel,
                            line_number=i,
                            line_text=line,
                            submatches=[(found.start(), found.end())],
                        )
                    )
                    if len(matches) >= self.max_results:
                        return matches
        return matches


class SemanticSearcher:
    """AST-based semantic search over Python symbols."""

    def __init__(self, repo: RepoInfo, max_results: int = 300):
        self.repo = repo
        self.max_results = max_results
        self._cache_root = self._resolve_cache_root()
        self._files_cache_dir = self._cache_root / "files"
        self._files_cache_dir.mkdir(parents=True, exist_ok=True)

    def _resolve_cache_root(self) -> Path:
        return resolve_repo_cache_root(self.repo.root) / "query"

    def search(
        self,
        pattern: str,
        *,
        fixed_strings: bool = False,
        case_sensitive: bool = False,
        file_globs: list[str] | None = None,
    ) -> list[MatchLocation]:
        checker = self._build_checker(
            pattern, fixed_strings=fixed_strings, case_sensitive=case_sensitive
        )
        out: list[MatchLocation] = []

        entries = self._load_or_build_symbol_index()
        for entry in entries:
            if file_globs and not _glob_accepts(entry.rel_path, file_globs):
                continue
            if checker(entry.name) or checker(entry.qualified):
                out.append(
                    MatchLocation(
                        rel_path=entry.rel_path,
                        line_number=entry.line_number,
                        line_text=entry.line_text,
                        submatches=[],
                    )
                )
            if len(out) >= self.max_results:
                return out

        return out

    def _load_or_build_symbol_index(self) -> list[SymbolEntry]:
        """Build the symbol index reusing per-file caches (P1-1).

        Only Python files whose content fingerprint changed are re-parsed;
        unchanged files reuse their cached symbol entries.
        """
        entries: list[SymbolEntry] = []
        for info in self.repo.files:
            if info.language != "python":
                continue
            rel = normalize_posix_path(info.path)
            blob = info.git_blob or f"ns{info.mtime_ns}|sz{info.size}"
            key = self._file_cache_key(rel, blob)
            cache_path = self._files_cache_dir / f"{key}.json"

            file_entries = self._load_file_cache(cache_path)
            if file_entries is None:
                file_entries = self._build_symbols_for_file(info, rel)
                self._write_file_cache(cache_path, rel, blob, file_entries)
            entries.extend(file_entries)

        entries.sort(key=lambda e: (e.rel_path, e.line_number, e.kind, e.qualified))
        return entries

    @staticmethod
    def _file_cache_key(rel: str, blob: str) -> str:
        # Fold in the cache schema + pixrep version so an upgrade or a change to
        # the symbol-extraction algorithm invalidates stale cached entries
        # instead of being read back as long as the file content is unchanged.
        raw = f"{QUERY_SYMBOL_CACHE_SCHEMA_VERSION}\0{__version__}\0{rel}\0{blob}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def _build_symbols_for_file(self, info: FileInfo, rel: str) -> list[SymbolEntry]:
        try:
            content = info.load_content().lstrip("\ufeff")
            tree = ast.parse(content)
        except (SyntaxError, OSError):
            return []

        try:
            lines = content.split("\n")
            class_by_node: dict[int, str] = {}
            for parent in ast.walk(tree):
                if isinstance(parent, ast.ClassDef):
                    for child in parent.body:
                        class_by_node[id(child)] = parent.name

            file_entries: list[SymbolEntry] = []
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    line_no = max(1, int(getattr(node, "lineno", 1)))
                    file_entries.append(
                        SymbolEntry(
                            rel_path=rel,
                            line_number=line_no,
                            line_text=_line_at(lines, line_no),
                            name=node.name,
                            qualified=node.name,
                            kind="class",
                        )
                    )
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    owner = class_by_node.get(id(node), "")
                    qualified = f"{owner}.{node.name}" if owner else node.name
                    line_no = max(1, int(getattr(node, "lineno", 1)))
                    file_entries.append(
                        SymbolEntry(
                            rel_path=rel,
                            line_number=line_no,
                            line_text=_line_at(lines, line_no),
                            name=node.name,
                            qualified=qualified,
                            kind="function",
                        )
                    )
            return file_entries
        finally:
            # Drop the parsed text so cold-cache scans don't accumulate every
            # Python file's content in memory.
            info.release_content()

    def _load_file_cache(self, cache_path: Path) -> list[SymbolEntry] | None:
        if not cache_path.exists():
            return None
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or (
            payload.get("schema") != QUERY_SYMBOL_CACHE_SCHEMA_VERSION
        ):
            # Missing/old schema → treat as a miss and rebuild.
            return None
        entries_raw = payload.get("entries")
        if not isinstance(entries_raw, list):
            return None
        entries: list[SymbolEntry] = []
        for item in entries_raw:
            if not isinstance(item, dict):
                continue
            entries.append(
                SymbolEntry(
                    rel_path=str(item.get("rel_path", "")),
                    line_number=max(1, int(item.get("line_number", 1))),
                    line_text=str(item.get("line_text", "")),
                    name=str(item.get("name", "")),
                    qualified=str(item.get("qualified", "")),
                    kind=str(item.get("kind", "symbol")),
                )
            )
        return entries

    def _write_file_cache(
        self,
        cache_path: Path,
        rel: str,
        blob: str,
        entries: list[SymbolEntry],
    ) -> None:
        payload = {
            "schema": QUERY_SYMBOL_CACHE_SCHEMA_VERSION,
            "rel": rel,
            "blob": blob,
            "entries": [
                {
                    "rel_path": e.rel_path,
                    "line_number": e.line_number,
                    "line_text": e.line_text,
                    "name": e.name,
                    "qualified": e.qualified,
                    "kind": e.kind,
                }
                for e in entries
            ],
        }
        try:
            atomic_write_text(cache_path, json.dumps(payload, ensure_ascii=False))
        except OSError:
            return

    @staticmethod
    def _build_checker(pattern: str, *, fixed_strings: bool, case_sensitive: bool):
        if fixed_strings:
            needle = pattern if case_sensitive else pattern.lower()

            def _check(text: str) -> bool:
                hay = text if case_sensitive else text.lower()
                return needle in hay

            return _check

        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            regex = re.compile(pattern, flags)
        except re.error:
            regex = re.compile(re.escape(pattern), flags)

        return lambda text: bool(regex.search(text))


class ContextExtractor:
    """Expand matches to context/scope-aware snippets."""

    def __init__(
        self,
        repo: RepoInfo,
        context_lines: int = 5,
        max_snippet_lines: int = 60,
        merge_gap: int = 3,
    ):
        self.repo = repo
        self.context_lines = context_lines
        self.max_snippet_lines = max_snippet_lines
        self.merge_gap = merge_gap
        self._file_map: dict[str, FileInfo] = {
            normalize_posix_path(info.path): info for info in repo.files
        }

    def extract(self, matches: list[MatchLocation]) -> list[CodeSnippet]:
        by_file: dict[str, list[MatchLocation]] = defaultdict(list)
        for m in matches:
            by_file[m.rel_path].append(m)

        snippets: list[CodeSnippet] = []
        for rel_path, file_matches in sorted(by_file.items()):
            file_info = self._file_map.get(rel_path)
            if file_info is None:
                continue

            try:
                content = file_info.load_content()
            except OSError:
                continue

            all_lines = content.split("\n")
            stripped_lines = [line.lstrip() for line in all_lines]
            indents = [
                (len(line) - len(stripped)) if stripped else -1
                for line, stripped in zip(all_lines, stripped_lines, strict=False)
            ]
            total = len(all_lines)
            file_matches.sort(key=lambda m: m.line_number)

            ranges: list[tuple[int, int, list[int]]] = []
            for m in file_matches:
                start = max(1, m.line_number - self.context_lines)
                end = min(total, m.line_number + self.context_lines)
                start, end = self._expand_to_scope(
                    all_lines, stripped_lines, indents, m.line_number, start, end
                )
                ranges.append((start, end, [m.line_number]))

            for start, end, match_lines in self._merge_ranges(ranges):
                if end - start + 1 > self.max_snippet_lines:
                    end = start + self.max_snippet_lines - 1

                snippets.append(
                    CodeSnippet(
                        rel_path=rel_path,
                        language=file_info.language,
                        start_line=start,
                        end_line=end,
                        lines=all_lines[start - 1 : end],
                        match_lines=sorted(set(match_lines)),
                        abs_path=file_info.abs_path,
                    )
                )

        return snippets

    def _expand_to_scope(
        self,
        all_lines: list[str],
        stripped_lines: list[str],
        indents: list[int],
        match_line: int,
        current_start: int,
        current_end: int,
    ) -> tuple[int, int]:
        if not all_lines or match_line < 1 or match_line > len(all_lines):
            return current_start, current_end

        scope_keywords = ("async def ", "def ", "class ", "function ", "fn ", "func ")

        match_indent = indents[match_line - 1] if indents[match_line - 1] >= 0 else 0
        found_header_line: int | None = None
        header_indent = match_indent

        for i in range(match_line - 1, max(0, match_line - SCOPE_LOOKBACK_LINES) - 1, -1):
            stripped = stripped_lines[i]
            if not stripped:
                continue
            indent = indents[i] if indents[i] >= 0 else 0
            if any(stripped.startswith(kw) for kw in scope_keywords) and (
                indent <= match_indent or i == match_line - 1
            ):
                found_header_line = i + 1
                header_indent = indent
                break

        if found_header_line is None:
            return current_start, current_end

        current_start = min(current_start, found_header_line)
        downward_end = current_end

        for j in range(
            found_header_line, min(len(all_lines), found_header_line + self.max_snippet_lines)
        ):
            stripped = stripped_lines[j]
            if not stripped:
                continue
            indent = indents[j] if indents[j] >= 0 else 0
            if (
                j + 1 > match_line
                and indent <= header_indent
                and any(stripped.startswith(kw) for kw in scope_keywords)
            ):
                break
            downward_end = j + 1

        return current_start, max(current_end, downward_end)

    def _merge_ranges(
        self,
        ranges: list[tuple[int, int, list[int]]],
    ) -> list[tuple[int, int, list[int]]]:
        if not ranges:
            return []

        ranges.sort(key=lambda r: r[0])
        merged: list[tuple[int, int, list[int]]] = []
        cur_start, cur_end, cur_matches = ranges[0]

        for start, end, match_lines in ranges[1:]:
            if start <= cur_end + self.merge_gap:
                cur_end = max(cur_end, end)
                cur_matches = cur_matches + match_lines
            else:
                merged.append((cur_start, cur_end, cur_matches))
                cur_start, cur_end, cur_matches = start, end, match_lines

        merged.append((cur_start, cur_end, cur_matches))
        return merged


def _glob_accepts(path_posix: str, globs: list[str]) -> bool:
    lower = path_posix.lower()
    for pat in globs:
        if fnmatch.fnmatch(path_posix, pat) or fnmatch.fnmatch(lower, pat.lower()):
            return True
    return False


def _line_at(lines: list[str], line_no: int) -> str:
    if line_no < 1 or line_no > len(lines):
        return ""
    return lines[line_no - 1]
