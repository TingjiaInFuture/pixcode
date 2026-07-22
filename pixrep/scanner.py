import concurrent.futures
import hashlib
import json
import logging
import os
import subprocess
from pathlib import Path

from .constants import DEFAULT_IGNORE_PATTERNS, SNAPSHOT_SCHEMA_VERSION
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
    def __init__(
        self,
        root: str,
        max_file_size: int = 512 * 1024,
        extra_ignore: list[str] | None = None,
        prefer_git_source: bool = True,
        scan_workers: int | None = None,
        snapshot_path: Path | None = None,
        snapshot_mode: str = "git-fast",
    ):
        self.root = Path(root).resolve()
        self.max_file_size = max_file_size
        self.extra_ignore = extra_ignore or []
        self.prefer_git_source = prefer_git_source
        self.scan_workers = scan_workers or 8
        self._ignore_patterns = [*DEFAULT_IGNORE_PATTERNS, *self.extra_ignore]
        self._ignore_match = compile_ignore_matcher(self._ignore_patterns)
        # "git-fast": distrust the snapshot for files git reports as dirty, so a
        # tool that preserves mtime+size while changing content can't fool us.
        self._snapshot_mode = snapshot_mode
        # Per-file snapshot for the warm-cache fast path (ONEPDF snapshot):
        # unchanged (mtime, size) → reuse sha256/line_count/is_text without
        # reading the file body.
        self._snapshot_path = snapshot_path
        self._snapshot: dict[str, dict] = self._load_snapshot()
        self._snapshot_dirty = False

    def _load_snapshot(self) -> dict[str, dict]:
        if not self._snapshot_path or not self._snapshot_path.exists():
            return {}
        try:
            data = json.loads(self._snapshot_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_snapshot(self) -> None:
        if not self._snapshot_path or not self._snapshot_dirty:
            return
        try:
            self._snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._snapshot_path.with_name("." + self._snapshot_path.name + ".tmp")
            tmp.write_text(json.dumps(self._snapshot), encoding="utf-8")
            os.replace(tmp, self._snapshot_path)
        except OSError:
            log.debug("failed to save scanner snapshot", exc_info=True)

    def _prune_snapshot(self, current_rel_paths: set[str]) -> None:
        if not self._snapshot_path:
            return
        # Prune against every scanned candidate (text + binary), not just the
        # files that made it into the report. Binary files are recorded in the
        # snapshot with is_text=False so they can be skipped without re-reading;
        # pruning against only the text files would drop them every run and
        # force a fresh 8 KB binary probe each time.
        stale = [rel for rel in self._snapshot if rel not in current_rel_paths]
        for rel in stale:
            self._snapshot.pop(rel, None)
        if stale:
            self._snapshot_dirty = True

    def _should_ignore_file(self, rel_posix: str, filename: str) -> bool:
        _ = filename
        return self._ignore_match(rel_posix)

    def _detect_language(self, filepath: Path) -> str:
        return detect_language(filepath)

    def _read_bytes(self, filepath: Path) -> bytes | None:
        try:
            return filepath.read_bytes()
        except OSError as e:
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
        except OSError as e:
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
                timeout=5,
                check=False,
            )
            if top.returncode != 0:
                return None
            top_root = Path(top.stdout.decode("utf-8", errors="replace").strip()).resolve()
            if top_root != self.root:
                return None

            proc = subprocess.run(
                ["git", "ls-files", "-s", "-z"],
                cwd=str(self.root),
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0:
            return None

        # Raw bytes + manual UTF-8 decode: avoids the platform-default codec
        # (cp936/GBK on Chinese Windows) choking on non-ASCII filenames.
        paths: list[Path] = []
        oid_map: dict[str, str] = {}
        for record in proc.stdout.split(b"\0"):
            # NUL-terminated records; each is "<mode> <oid> <stage>\t<path>".
            # Splitting on the tab (not strip) keeps leading/trailing spaces in
            # a path intact; only the first tab separates meta from path.
            meta, sep, rel = record.partition(b"\t")
            if not sep:
                continue
            parts = meta.split()
            if len(parts) < 2 or not rel:
                continue
            rel_str = rel.decode("utf-8", errors="replace")
            oid_map[normalize_posix_path(rel_str)] = parts[1].decode("utf-8", errors="replace")
            paths.append(self.root / Path(rel_str))
        if not paths:
            return None
        return paths, oid_map

    def _git_dirty_set(self) -> set[str] | None:
        """Repo-relative posix paths git reports as modified/untracked, so the
        snapshot fast path can distrust their mtime+size.

        Returns ``None`` (rather than an empty set) when git is unavailable,
        fails, or times out: an empty set reads as "nothing is dirty" and would
        let a transient git failure reuse stale snapshot hashes. Callers must
        fall back to treating every file as dirty when this returns ``None``.
        """
        try:
            proc = subprocess.run(
                ["git", "status", "--porcelain=v1", "-z"],
                cwd=str(self.root),
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0:
            return None
        # Read raw bytes and decode paths as UTF-8 ourselves: ``text=True`` would
        # use the platform's default codec (cp936/GBK on a Chinese Windows),
        # which raises UnicodeDecodeError on a non-ASCII filename and leaves
        # stdout as None. git's -z output is unquoted UTF-8.
        dirty: set[str] = set()
        for entry in proc.stdout.split(b"\0"):
            if not entry:
                continue
            # porcelain v1 -z record: "XY <path>" (two status chars + a space,
            # then the literal path with no quoting). A rename is
            # "XY <src>\t<dst>" — keep the destination path. Because the path is
            # everything after the fixed 3-byte prefix, embedded spaces (and any
            # other character except NUL) survive intact.
            if len(entry) < 3:
                continue
            path_part = entry[3:]
            if b"\t" in path_part:
                path_part = path_part.rsplit(b"\t", 1)[-1]
            if path_part:
                dirty.add(normalize_posix_path(path_part.decode("utf-8", errors="replace")))
        return dirty

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
        dirty: set[str] | None = None,
    ) -> tuple[str, FileInfo | None]:
        if oid_map is None:
            oid_map = {}
        if dirty is None:
            dirty = set()
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

        git_index_oid = oid_map.get(rel_posix)
        language = self._detect_language(filepath)

        # Snapshot fast path (ONEPDF snapshot): when (mtime, size) is unchanged
        # and the schema matches, reuse the cached sha256 / line_count / is_text
        # and avoid reading the file body — warm caches approach O(changed bytes).
        snap = self._snapshot.get(rel_posix)
        if (
            snap
            and snap.get("mtime_ns") == mtime_ns
            and snap.get("size") == size
            and snap.get("schema") == SNAPSHOT_SCHEMA_VERSION
            and rel_posix not in dirty
        ):
            if not snap.get("is_text", True):
                return ("skipped_binary", None)
            content = ""
            if include_content:
                blob = self._read_bytes(filepath)
                if blob is None:
                    return ("skipped_unreadable", None)
                content = blob.decode(encoding="utf-8", errors="replace")
            return (
                "ok",
                FileInfo(
                    path=rel_path,
                    abs_path=filepath,
                    language=language,
                    size=size,
                    mtime_ns=mtime_ns,
                    line_count=snap["line_count"],
                    content=content,
                    git_blob=snap["sha256"],
                    git_index_oid=git_index_oid,
                ),
            )

        # Miss: read the file body once for binary detection + line count +
        # content hash (single read, P1-3).
        blob = self._read_bytes(filepath)
        if blob is None:
            return ("skipped_unreadable", None)
        if not is_probably_text(blob[:8192]):
            self._record_snapshot(rel_posix, mtime_ns, size, "", 0, is_text=False)
            return ("skipped_binary", None)
        line_count = line_count_from_bytes(blob)
        content = blob.decode(encoding="utf-8", errors="replace") if include_content else ""
        # Content fingerprint of the actual working-tree bytes (P0-1).
        git_blob = hashlib.sha256(blob).hexdigest()
        self._record_snapshot(rel_posix, mtime_ns, size, git_blob, line_count, is_text=True)

        info = FileInfo(
            path=rel_path,
            abs_path=filepath,
            language=language,
            size=size,
            mtime_ns=mtime_ns,
            line_count=line_count,
            content=content,
            git_blob=git_blob,
            git_index_oid=git_index_oid,
        )
        return ("ok", info)

    def _record_snapshot(
        self,
        rel_posix: str,
        mtime_ns: int,
        size: int,
        sha256: str,
        line_count: int,
        *,
        is_text: bool,
    ) -> None:
        if not self._snapshot_path:
            return
        self._snapshot[rel_posix] = {
            "mtime_ns": mtime_ns,
            "size": size,
            "schema": SNAPSHOT_SCHEMA_VERSION,
            "sha256": sha256,
            "line_count": line_count,
            "is_text": is_text,
        }
        self._snapshot_dirty = True

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

        # Every candidate's rel path — used to fall back to "all dirty" when git
        # status fails, and to prune snapshot entries for deleted files.
        candidate_rels: set[str] = set()
        for p in candidates:
            try:
                candidate_rels.add(normalize_posix_path(p.relative_to(self.root)))
            except ValueError:
                continue

        # git-fast: distrust the snapshot for files git reports as dirty, so a
        # tool that preserves mtime+size while changing content can't fool us.
        # If git status itself fails we treat ALL files as dirty (strict):
        # an empty dirty set would mean "trust every snapshot" and could let a
        # transient git failure (lock, timeout, fs error) reuse stale hashes.
        if self._snapshot_mode == "git-fast" and repo.source_mode == "git":
            dirty = self._git_dirty_set()
            if dirty is None:
                dirty = candidate_rels
        else:
            dirty = set()

        max_workers = max(1, min(self.scan_workers, len(candidates) or 1))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            for status, info in pool.map(
                lambda p: self._scan_one_file(p, include_content, oid_map, dirty), candidates
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
        repo.language_stats = dict(
            sorted(lang_stats.items(), key=lambda item: item[1]["lines"], reverse=True)
        )
        repo.tree_str = self._build_tree(files)
        repo.scan_stats = scan_stats
        self._prune_snapshot(candidate_rels)
        self._save_snapshot()
        return repo

    def _build_tree(self, files: list[FileInfo]) -> str:
        rels = [normalize_posix_path(info.path) for info in files]
        return build_tree(rels, self.root.name, style="unicode")
