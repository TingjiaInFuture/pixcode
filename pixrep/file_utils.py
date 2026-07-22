from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import json
import os
import re
import tempfile
import unicodedata
from pathlib import Path, PurePosixPath

from .constants import DEFAULT_IGNORE_DIRS
from .syntax import LANG_MAP


def normalize_posix_path(rel_path: str | Path) -> str:
    """
    Normalize a repo-relative path to a posix-style string (forward slashes).

    This is used for ignore/include matching and stable PDF index output.
    """
    if isinstance(rel_path, Path):
        rel_path = str(rel_path)
    # PurePosixPath does not treat backslashes as separators, so normalize first.
    rel_path = rel_path.replace("\\", "/")
    return str(PurePosixPath(rel_path))


def matches_any(path_posix: str, patterns: list[str]) -> bool:
    """
    Case-insensitive glob matching for both full relative paths and basenames.

    Patterns are expected to use forward slashes (git-style), but we match
    against a normalized posix path.
    """
    lower = path_posix.lower()
    for pat in patterns:
        if fnmatch.fnmatch(path_posix, pat) or fnmatch.fnmatch(lower, pat.lower()):
            return True
    return False


def compile_ignore_matcher(patterns: list[str]):
    """
    Compile glob ignore patterns into a single case-insensitive matcher.

    Returns a callable: matcher(path_posix: str) -> bool
    """
    normalized = [normalize_posix_path(p) for p in patterns if p]
    if not normalized:
        return lambda _path: False

    path_patterns = [p for p in normalized if "/" in p]
    basename_patterns = [p for p in normalized if "/" not in p]

    path_re = None
    basename_re = None
    if path_patterns:
        path_pieces = [f"(?:{fnmatch.translate(p)})" for p in path_patterns]
        path_re = re.compile("|".join(path_pieces), re.IGNORECASE)
    if basename_patterns:
        base_pieces = [f"(?:{fnmatch.translate(p)})" for p in basename_patterns]
        basename_re = re.compile("|".join(base_pieces), re.IGNORECASE)

    def _match(path_posix: str) -> bool:
        normalized_path = normalize_posix_path(path_posix)
        if path_re and path_re.match(normalized_path):
            return True
        if basename_re:
            base = PurePosixPath(normalized_path).name
            return bool(basename_re.match(base))
        return False

    return _match


def should_ignore_dir(dirname: str) -> bool:
    return dirname in DEFAULT_IGNORE_DIRS or dirname.startswith(".")


def is_probably_text(blob: bytes, sample: int = 8192) -> bool:
    return b"\x00" not in blob[:sample]


def char_display_width(ch: str) -> int:
    """East Asian Width for monospace column accounting: W/F → 2,
    control/combining → 0, others → 1."""
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    cat = unicodedata.category(ch)
    if cat.startswith("C") or cat.startswith("M"):
        return 0
    return 1


def display_width(text: str) -> int:
    return sum(char_display_width(c) for c in text)


def resolve_repo_cache_root(repo_root: Path) -> Path:
    """Per-repo cache root, namespaced by the resolved path so two repos that
    share a basename never collide (e.g. company-a/backend vs company-b/backend)."""
    resolved = str(repo_root.resolve())
    repo_id = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:12]
    cache_name = f"{repo_root.name}-{repo_id}"
    env = os.environ.get("PIXREP_CACHE_DIR", "").strip()
    if env:
        return Path(env).expanduser().resolve() / cache_name
    if os.name == "nt":
        return (
            Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
            / "pixrep"
            / "cache"
            / cache_name
        )
    xdg = os.environ.get("XDG_CACHE_HOME", "").strip()
    if xdg:
        return Path(xdg).expanduser().resolve() / "pixrep" / cache_name
    return Path.home() / ".cache" / "pixrep" / cache_name


def line_count_from_bytes(blob: bytes) -> int:
    if not blob:
        return 0
    n = blob.count(b"\n")
    if not blob.endswith(b"\n"):
        n += 1
    return n


def detect_language(path_value: str | Path) -> str:
    """
    Detect a language id used by pixrep.

    Supports both filenames (Dockerfile, Makefile, ...) and extension mapping.
    """
    if isinstance(path_value, Path):
        name = path_value.name.lower()
        suffix = path_value.suffix.lower()
    else:
        p = PurePosixPath(path_value)
        name = p.name.lower()
        suffix = p.suffix.lower()

    special = {
        "dockerfile": "docker",
        "makefile": "makefile",
        "cmakelists.txt": "cmake",
        "rakefile": "ruby",
        "gemfile": "ruby",
        "requirements.txt": "text",
        "pipfile": "toml",
        "cargo.toml": "toml",
        "go.mod": "go",
        "go.sum": "text",
    }
    if name in special:
        return special[name]
    return LANG_MAP.get(suffix, "text")


def safe_join_repo(repo_root: Path, rel_posix: str) -> Path | None:
    """
    Join a repo root and a repo-relative posix path and ensure it doesn't escape.

    This prevents symlink/path tricks from making us read outside the repo.
    """
    repo_resolved = repo_root.resolve()
    candidate = repo_root / PurePosixPath(rel_posix)
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    try:
        if not resolved.is_relative_to(repo_resolved):
            return None
    except AttributeError:
        # Python < 3.9 fallback (not expected, but keep safe).
        try:
            resolved.relative_to(repo_resolved)
        except ValueError:
            return None
    return resolved


def build_tree(rel_paths_posix: list[str], root_name: str, style: str = "ascii") -> str:
    """
    Build a directory tree string from repo-relative posix paths.

    style:
      - "ascii": |-- / `-- connectors (glyph-safe in built-in PDF fonts)
      - "unicode": ├── / └── connectors (nicer in terminals)
    """
    tree: dict[str, dict | None] = {}
    for p in rel_paths_posix:
        parts = PurePosixPath(p).parts
        node = tree
        for part in parts[:-1]:
            node = node.setdefault(f"{part}/", {})
        node[parts[-1]] = None

    if style == "unicode":
        branch_mid, branch_last = "├── ", "└── "
        vert, indent = "│   ", "    "
    else:
        branch_mid, branch_last = "|-- ", "`-- "
        vert, indent = "|   ", "    "

    lines = [f"{root_name}/"]

    def walk(node: dict, prefix: str):
        items = list(node.items())
        for idx, (name, subtree) in enumerate(items):
            is_last = idx == len(items) - 1
            connector = branch_last if is_last else branch_mid
            lines.append(f"{prefix}{connector}{name}")
            if subtree is not None:
                extension = indent if is_last else vert
                walk(subtree, prefix + extension)

    walk(tree, "")
    return "\n".join(lines)


def atomic_write_text(path: Path, text: str) -> None:
    """Atomically write text to ``path`` via a uniquely-named temp file.

    A unique temp name (not a fixed ``path.tmp``) avoids two concurrent pixrep
    processes clobbering each other's temp file; ``fsync`` + ``os.replace`` make
    the destination either fully old or fully new, never half-written. Stale
    temps are cleaned up if the replace fails.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            delete=False,
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        ) as tmp:
            tmp.write(text)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_path = Path(tmp.name)
        os.replace(tmp_path, path)
    finally:
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                tmp_path.unlink(missing_ok=True)


def _pid_alive(pid: int) -> bool:
    """Return True if a process with ``pid`` is currently running."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


@contextlib.contextmanager
def repo_lock(cache_root: Path):
    """Lightweight per-repo advisory lock.

    Prevents two concurrent generation runs over the same cache root from
    racing on the manifest/snapshot (lost update) or one output overwriting the
    other. Uses a PID file with a liveness check so a crashed process does not
    leave a permanent lock behind. Raises ``RuntimeError`` if another live
    pixrep holds the lock.
    """
    cache_root.mkdir(parents=True, exist_ok=True)
    lock_path = cache_root / ".pixrep.lock"
    if lock_path.exists():
        try:
            data = json.loads(lock_path.read_text(encoding="utf-8"))
            pid = int(data.get("pid", 0))
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            pid = 0
        if pid and _pid_alive(pid):
            raise RuntimeError(
                f"another pixrep process (pid {pid}) is generating this repository; "
                f"remove {lock_path} if it is stale"
            )
    try:
        lock_path.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    except OSError:
        # If we can't write the lock we can't enforce mutual exclusion — proceed
        # rather than hard-failing a generation for a transient FS error.
        yield
        return
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            lock_path.unlink(missing_ok=True)
