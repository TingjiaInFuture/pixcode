import hashlib
import logging
import os
import subprocess
import concurrent.futures
from pathlib import Path

from .constants import DEFAULT_IGNORE_PATTERNS
from .file_utils import (
    build_tree,
    compile_ignore_matcher,
    detect_language,
    is_probably_text,
    line_count_from_bytes,
    normalize_posix_path,
    should_ignore_dir,
)
from .models import FileInfo, RepoInfo

log = logging.getLogger(__name__)


class RepoScanner:
    def __init__(self, root: str, max_file_size: int = 512 * 1024,
                 extra_ignore: list[str] | None = None,
                 prefer_git_source: bool = True,
                 scan_workers: int | None = None):
        self.root = Path(root).resolve()
        self.max_file_size = max_file_size
        self.extra_ignore = extra_ignore or []
        self.prefer_git_source = prefer_git_source
        self.scan_workers = scan_workers or 8
        self._ignore_patterns = [*DEFAULT_IGNORE_PATTERNS, *self.extra_ignore]
        self._ignore_match = compile_ignore_matcher(self._ignore_patterns)

    def _should_ignore_file(self, rel_posix: str, filename: str) -> bool:
        _ = filename
        return self._ignore_match(rel_posix)

    def _detect_language(self, filepath: Path) -> str:
        return detect_language(filepath)

    def _read_bytes(self, filepath: Path) -> bytes | None:
        try:
            return filepath.read_bytes()
        except (IOError, OSError) as e:
            log.debug("failed to read file: %s (%s)", filepath, e)
            return None

    def _count_lines_stream(self, filepath: Path, chunk_size: int = 64 * 1024) -> int | None:
        """Stream line count for very large files (current 512 KB cap uses
        `line_count_from_bytes` on a single read instead). Kept as a fallback
        for a future large-file path.
        """
        try:
            total = 0
            ends_with_newline = False
            with filepath.open("rb") as f:
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    total += chunk.count(b"\n")
                    ends_with_newline = chunk.endswith(b"\n")
            if total == 0:
                # Non-empty file with no newline terminators.
                return 1
            if not ends_with_newline:
                total += 1
            return total
        except (IOError, OSError) as e:
            log.debug("failed to stream-count lines: %s (%s)", filepath, e)
            return None

    def _git_ls_files_with_oid(self) -> tuple[list[Path], dict[str, str]] | None:
        """Return (sorted abs paths, rel_posix -> oid) for a git repo, else None.

        A single `git ls-files -s` call yields both the tracked file list and
        each file's stage OID, which we use as a stable content fingerprint
        (FileInfo.git_blob) without re-reading file contents.
        """
        if not self.prefer_git_source:
            return None
        try:
            top = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=str(self.root),
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if top.returncode != 0:
                return None
            top_root = Path(top.stdout.strip()).resolve()
            if top_root != self.root:
                return None

            proc = subprocess.run(
                ["git", "ls-files", "-s"],
                cwd=str(self.root),
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0:
            return None

        paths: list[Path] = []
        oid_map: dict[str, str] = {}
        for line in proc.stdout.splitlines():
            # Format: "<mode> <oid> <stage>\t<path>"
            meta, _, rel = line.partition("\t")
            parts = meta.split()
            rel = rel.strip()
            if len(parts) < 2 or not rel:
                continue
            oid_map[normalize_posix_path(rel)] = parts[1]
            paths.append(self.root / Path(rel))
        if not paths:
            return None
        return paths, oid_map

    def _walk_files(self):
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if not should_ignore_dir(d))
            for filename in sorted(filenames):
                yield Path(dirpath) / filename

    def _scan_one_file(
        self,
        filepath: Path,
        include_content: bool,
        oid_map: dict[str, str] | None = None,
    ) -> tuple[str, FileInfo | None]:
        if oid_map is None:
            oid_map = {}
        try:
            rel_path = filepath.relative_to(self.root)
        except ValueError:
            return ("skipped_unreadable", None)

        rel_posix = normalize_posix_path(rel_path)
        if self._should_ignore_file(rel_posix, filepath.name):
            return ("ignored_by_pattern", None)

        try:
            st = filepath.stat()
            size = st.st_size
            mtime_ns = int(st.st_mtime_ns)
        except OSError:
            return ("skipped_unreadable", None)

        if size > self.max_file_size or size == 0:
            return ("skipped_size_or_empty", None)

        # Single read for both content-aware and metadata-only scans (P1-3):
        # one read_bytes() serves binary detection + line count (+ decode when
        # content is requested). Replaces the prior 8 KB sample + mmap(mm[:])
        # double-read that copied the whole file into a bytes object anyway.
        blob = self._read_bytes(filepath)
        if blob is None:
            return ("skipped_unreadable", None)
        if not is_probably_text(blob[:8192]):
            return ("skipped_binary", None)
        line_count = line_count_from_bytes(blob)
        content = blob.decode(encoding="utf-8", errors="replace") if include_content else ""

        # Content fingerprint: git stage OID when available, else sha1(content)
        # computed from the bytes already in hand (no extra I/O).
        git_blob = oid_map.get(rel_posix)
        if git_blob is None:
            git_blob = hashlib.sha1(blob).hexdigest()

        info = FileInfo(
            path=rel_path,
            abs_path=filepath,
            language=self._detect_language(filepath),
            size=size,
            mtime_ns=mtime_ns,
            line_count=line_count,
            content=content,
            git_blob=git_blob,
        )
        return ("ok", info)

    def scan_files(
        self,
        paths: list[str | Path],
        include_content: bool = False,
    ) -> list[FileInfo]:
        """Scan only the given absolute paths (used by query hit-file scanning).

        git stage OIDs are unavailable on this path, so the content fingerprint
        falls back to sha1(content).
        """
        results: list[FileInfo] = []
        for raw in paths:
            status, info = self._scan_one_file(Path(raw), include_content, oid_map={})
            if status == "ok" and info is not None:
                results.append(info)
        results.sort(key=lambda item: str(item.path))
        for index, info in enumerate(results, 1):
            info.index = index
        return results

    def scan(self, include_content: bool = True) -> RepoInfo:
        """Scan repository files and return a populated RepoInfo."""
        repo = RepoInfo(root=self.root, name=self.root.name)
        files: list[FileInfo] = []
        scan_stats: dict[str, int] = {
            "seen_files": 0,
            "ignored_by_pattern": 0,
            "skipped_unreadable": 0,
            "skipped_size_or_empty": 0,
            "skipped_binary": 0,
        }

        oid_map: dict[str, str] = {}
        git_info = self._git_ls_files_with_oid()
        if git_info is not None:
            repo.source_mode = "git"
            git_paths, oid_map = git_info
            candidates = sorted(p for p in git_paths if p.is_file())
            repo.tracked_paths = set(oid_map.keys())
        else:
            repo.source_mode = "walk"
            candidates = list(self._walk_files())
        scan_stats["seen_files"] = len(candidates)

        max_workers = max(1, min(self.scan_workers, len(candidates) or 1))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            for status, info in pool.map(
                lambda p: self._scan_one_file(p, include_content, oid_map), candidates
            ):
                if status == "ok" and info is not None:
                    files.append(info)
                elif status in scan_stats:
                    scan_stats[status] += 1

        files.sort(key=lambda item: str(item.path))
        for index, info in enumerate(files, 1):
            info.index = index

        repo.files = files
        repo.total_lines = sum(item.line_count for item in files)
        repo.total_size = sum(item.size for item in files)

        lang_stats: dict[str, dict[str, int]] = {}
        for info in files:
            lang_stats.setdefault(info.language, {"files": 0, "lines": 0})
            lang_stats[info.language]["files"] += 1
            lang_stats[info.language]["lines"] += info.line_count
        repo.language_stats = dict(sorted(
            lang_stats.items(), key=lambda item: item[1]["lines"], reverse=True))
        repo.tree_str = self._build_tree(files)
        repo.scan_stats = scan_stats
        return repo

    def _build_tree(self, files: list[FileInfo]) -> str:
        rels = [normalize_posix_path(info.path) for info in files]
        return build_tree(rels, self.root.name, style="unicode")
