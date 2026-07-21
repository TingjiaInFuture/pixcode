import ast
import concurrent.futures
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections import defaultdict
from pathlib import Path

from .file_utils import normalize_posix_path
from .js_parser import build_js_semantic_map
from .lint_collector import iter_target_batches, ruff_severity
from .models import FileInfo, LintIssue, RepoInfo, SemanticMap
from .semantic_analyzer import build_python_semantic_map

log = logging.getLogger(__name__)

MAX_SEMANTIC_LINES = 24


class CodeInsightEngine:
    def __init__(
        self,
        repo: RepoInfo,
        enable_semantic_minimap: bool = True,
        enable_lint_heatmap: bool = True,
        linter_timeout: int = 20,
    ):
        self.repo = repo
        self.enable_semantic_minimap = enable_semantic_minimap
        self.enable_lint_heatmap = enable_lint_heatmap
        self.linter_timeout = linter_timeout
        # Rendering/cache identity fingerprint (populated by the generator); an
        # empty string keeps backward-compatible behaviour for callers that do
        # not set it.
        self._options_hash = repo.options_hash
        self._resolved_root = self.repo.root.resolve()
        self._scanned_paths = {self._normalize_path(info.path) for info in self.repo.files}
        self._scanned_paths_ci = {k.lower(): k for k in self._scanned_paths} if os.name == "nt" else {}
        # rel -> content fingerprint (git blob OID or sha1), used as the cache
        # identity for lint (and available for any per-file cache).
        self._blob_by_rel = {
            self._normalize_path(info.path): info.git_blob
            for info in self.repo.files
        }
        self._cache_root = self._resolve_cache_root()
        self._semantic_cache_dir = self._cache_root / "semantic"
        self._lint_cache_dir = self._cache_root / "lint"
        self._semantic_cache_dir.mkdir(parents=True, exist_ok=True)
        self._lint_cache_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self._cache_root / "manifest.json"

    def _resolve_cache_root(self) -> Path:
        env = os.environ.get("PIXREP_CACHE_DIR", "").strip()
        if env:
            return Path(env).expanduser().resolve() / self.repo.name

        if os.name == "nt":
            base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
            return base / "pixrep" / "cache" / self.repo.name

        xdg = os.environ.get("XDG_CACHE_HOME", "").strip()
        if xdg:
            return Path(xdg).expanduser().resolve() / "pixrep" / self.repo.name
        return Path.home() / ".cache" / "pixrep" / self.repo.name

    def enrich_repo(self) -> None:
        """Populate semantic maps and lint issues onto every repo file."""
        self.enrich_files(self.repo.files)

    def enrich_files(self, files: list[FileInfo]) -> None:
        """Populate semantic maps and lint issues for the given files only.

        Used by incremental generation so that only pending files pay the
        analysis cost (P0-1).
        """
        if not files:
            return

        if self.enable_semantic_minimap:
            workers = min(4, max(1, os.cpu_count() or 1))
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                future_map = {
                    pool.submit(self._build_semantic_map_cached, info): info
                    for info in files
                }
                for fut in concurrent.futures.as_completed(future_map):
                    info = future_map[fut]
                    try:
                        info.semantic_map = fut.result()
                    except Exception:
                        info.semantic_map = SemanticMap(
                            kind="callgraph",
                            lines=["(analysis failed)"],
                            node_count=0,
                            edge_count=0,
                        )
        else:
            for info in files:
                info.semantic_map = SemanticMap()

        if self.enable_lint_heatmap:
            lint_map = self._collect_lint_issues(files)
            lint_map_ci = {k.lower(): v for k, v in lint_map.items()} if os.name == "nt" else {}
            matched = 0
            for info in files:
                key = self._normalize_path(info.path)
                issues = lint_map.get(key)
                if issues is None and lint_map_ci:
                    issues = lint_map_ci.get(key.lower())
                info.lint_issues = issues or []
                if info.lint_issues:
                    matched += 1

            if lint_map and matched == 0:
                log.warning(
                    "Linter found %d files with issues but none matched scanned files. Path normalization mismatch?",
                    len(lint_map),
                )
                sample_lint = next(iter(lint_map))
                sample_file = self._normalize_path(files[0].path)
                log.debug("sample lint path=%r, sample file path=%r", sample_lint, sample_file)
        else:
            for info in files:
                info.lint_issues = []

    def _collect_lint_issues(self, files: list[FileInfo]) -> dict[str, list[LintIssue]]:
        issues: dict[str, list[LintIssue]] = defaultdict(list)
        # Single global deadline shared across ruff + eslint (P1-2). Each batch
        # checks the remaining time and kills its subprocess on timeout, so the
        # total wall-clock stays bounded instead of growing as batches × timeout.
        deadline = time.monotonic() + max(1, self.linter_timeout)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            fut_ruff = pool.submit(self._collect_ruff, files, deadline)
            fut_eslint = pool.submit(self._collect_eslint, files, deadline)
            remaining_wait = max(0.1, deadline - time.monotonic())
            done, _not_done = concurrent.futures.wait(
                {fut_ruff, fut_eslint},
                timeout=remaining_wait,
            )

            for fut in done:
                try:
                    partial = fut.result() or {}
                except Exception:
                    log.debug("lint collector future failed", exc_info=True)
                    partial = {}
                for rel, rel_issues in partial.items():
                    issues[rel].extend(rel_issues)
        return dict(issues)

    def _run_json_command(self, cmd: list[str], *, cwd: Path, tool: str, timeout: float):
        """Run a linter command with a hard per-call timeout, killing the
        subprocess on expiry instead of relying on Future.cancel().
        """
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(cwd),
            )
        except OSError:
            log.debug("%s invocation failed", tool)
            return None

        try:
            out, _err = proc.communicate(timeout=max(0.1, timeout))
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            log.debug("%s timed out and was killed", tool)
            return None

        payload = (out or "").strip()
        if not payload:
            return None
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            log.debug("%s output was not valid json", tool)
            return None

    def _collect_ruff(self, files: list[FileInfo], deadline: float) -> dict[str, list[LintIssue]]:
        issues: dict[str, list[LintIssue]] = defaultdict(list)
        if not shutil.which("ruff"):
            return {}
        targets = [self._normalize_path(info.path) for info in files if info.language == "python"]
        if not targets:
            return {}

        cache_key = self._tool_cache_key("ruff", targets)
        cached = self._load_lint_cache("ruff", cache_key)
        if cached is not None:
            return cached

        for batch in iter_target_batches(targets):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            cmd = ["ruff", "check", "--output-format", "json", *batch]
            data = self._run_json_command(
                cmd, cwd=self.repo.root, tool="ruff", timeout=remaining
            )
            if not data:
                continue
            for item in data:
                filename = item.get("filename")
                location = item.get("location", {})
                row = int(location.get("row", 1))
                code = str(item.get("code", "RUFF"))
                message = str(item.get("message", "ruff finding"))
                rel = self._relative_to_repo(filename)
                if not rel:
                    continue
                issues[rel].append(
                    LintIssue(
                        line=max(1, row),
                        severity=ruff_severity(code),
                        tool="ruff",
                        code=code,
                        message=message,
                    )
                )
        result = dict(issues)
        self._save_lint_cache("ruff", cache_key, result)
        return result

    def _collect_eslint(self, files: list[FileInfo], deadline: float) -> dict[str, list[LintIssue]]:
        issues: dict[str, list[LintIssue]] = defaultdict(list)
        if not shutil.which("eslint"):
            return {}
        targets = [
            self._normalize_path(info.path)
            for info in files
            if info.language in {"javascript", "typescript"}
        ]
        if not targets:
            return {}

        cache_key = self._tool_cache_key("eslint", targets)
        cached = self._load_lint_cache("eslint", cache_key)
        if cached is not None:
            return cached

        for batch in iter_target_batches(targets):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            cmd = [
                "eslint",
                "--format",
                "json",
                *batch,
            ]
            files_json = self._run_json_command(
                cmd, cwd=self.repo.root, tool="eslint", timeout=remaining
            )
            if not files_json:
                continue
            for entry in files_json:
                rel = self._relative_to_repo(entry.get("filePath", ""))
                if not rel:
                    continue
                for msg in entry.get("messages", []):
                    line = int(msg.get("line", 1))
                    sev = int(msg.get("severity", 1))
                    code = str(msg.get("ruleId") or "ESLINT")
                    text = str(msg.get("message", "eslint finding"))
                    issues[rel].append(
                        LintIssue(
                            line=max(1, line),
                            severity="high" if sev >= 2 else "medium",
                            tool="eslint",
                            code=code,
                            message=text,
                        )
                    )
        result = dict(issues)
        self._save_lint_cache("eslint", cache_key, result)
        return result

    def _build_semantic_map(self, info: FileInfo) -> SemanticMap:
        content = info.load_content()
        if info.language == "python":
            return self._python_semantic_map(content)
        if info.language in {"javascript", "typescript"}:
            return self._js_semantic_map(content)
        return self._generic_semantic_map(content, info.language)

    def _build_semantic_map_cached(self, info: FileInfo) -> SemanticMap:
        cache_key = self._semantic_cache_key(info)
        cache_path = self._semantic_cache_dir / f"{cache_key}.json"
        if cache_path.exists():
            try:
                payload = json.loads(cache_path.read_text(encoding="utf-8"))
                return SemanticMap(
                    kind=str(payload.get("kind", "none")),
                    lines=[str(line) for line in payload.get("lines", [])],
                    node_count=int(payload.get("node_count", 0)),
                    edge_count=int(payload.get("edge_count", 0)),
                    truncated=bool(payload.get("truncated", False)),
                )
            except (OSError, json.JSONDecodeError, ValueError, TypeError):
                pass

        semantic_map = self._build_semantic_map(info)
        try:
            cache_path.write_text(
                json.dumps(
                    {
                        "kind": semantic_map.kind,
                        "lines": semantic_map.lines,
                        "node_count": semantic_map.node_count,
                        "edge_count": semantic_map.edge_count,
                        "truncated": semantic_map.truncated,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass
        return semantic_map

    def _python_semantic_map(self, content: str) -> SemanticMap:
        return build_python_semantic_map(
            content,
            ast_name_resolver=self._ast_name,
            max_semantic_lines=MAX_SEMANTIC_LINES,
        )

    def _js_semantic_map(self, content: str) -> SemanticMap:
        return build_js_semantic_map(content, max_semantic_lines=MAX_SEMANTIC_LINES)

    def _generic_semantic_map(self, content: str, language: str) -> SemanticMap:
        if language in {"text", "json", "yaml", "toml", "markdown", "ini"}:
            return SemanticMap(kind="none", lines=[])
        sigs = re.findall(r"^\s*(?:def|fn|func|function)\s+([A-Za-z_]\w*)", content, flags=re.MULTILINE)
        lines = ["Functions:"] + [f"  - {name}()" for name in sigs[:12]] if sigs else ["(no symbols detected)"]
        lines, truncated = self._limit_semantic_lines(lines)
        return SemanticMap(
            kind="callgraph",
            lines=lines,
            node_count=len(sigs),
            edge_count=0,
            truncated=truncated,
        )

    def _relative_to_repo(self, path_value: str) -> str | None:
        if not path_value:
            return None

        root = self._resolved_root
        p = Path(path_value)
        candidate = p if p.is_absolute() else (root / p)
        try:
            resolved = candidate.resolve()
        except OSError:
            return None

        try:
            if not resolved.is_relative_to(root):
                return None
            rel = resolved.relative_to(root)
            norm = self._normalize_path(rel)
            if norm in self._scanned_paths:
                return norm
            if os.name == "nt":
                return self._scanned_paths_ci.get(norm.lower())
            return None
        except (ValueError, OSError):
            return None

    @staticmethod
    def _normalize_path(path_value: str | Path) -> str:
        return normalize_posix_path(path_value)

    def _semantic_cache_key(self, info: FileInfo) -> str:
        rel = self._normalize_path(info.path)
        blob = info.git_blob
        if not blob:
            # Fallback identity for entries without a content fingerprint.
            blob = f"ns{int(info.mtime_ns)}|sz{int(info.size)}"
        sig = f"{self._options_hash}|{rel}|{blob}|v3"
        return hashlib.sha1(sig.encode("utf-8")).hexdigest()

    def _tool_cache_key(self, tool: str, targets: list[str]) -> str:
        h = hashlib.sha1()
        h.update(f"{tool}|{self._options_hash}|v3|".encode("utf-8"))
        for rel in sorted(set(targets)):
            blob = self._blob_by_rel.get(rel)
            if blob:
                h.update(f"{rel}|{blob}\n".encode("utf-8"))
            else:
                h.update(f"{rel}|missing\n".encode("utf-8"))
        return h.hexdigest()

    def _load_lint_cache(self, tool: str, key: str) -> dict[str, list[LintIssue]] | None:
        path = self._lint_cache_dir / f"{tool}_{key}.json"
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

        restored: dict[str, list[LintIssue]] = {}
        for rel, entries in raw.items():
            restored[rel] = [
                LintIssue(
                    line=int(item.get("line", 1)),
                    severity=str(item.get("severity", "medium")),
                    tool=str(item.get("tool", tool)),
                    code=str(item.get("code", tool.upper())),
                    message=str(item.get("message", "")),
                )
                for item in entries
            ]
        return restored

    def _save_lint_cache(self, tool: str, key: str, issues: dict[str, list[LintIssue]]) -> None:
        path = self._lint_cache_dir / f"{tool}_{key}.json"
        payload = {
            rel: [
                {
                    "line": issue.line,
                    "severity": issue.severity,
                    "tool": issue.tool,
                    "code": issue.code,
                    "message": issue.message,
                }
                for issue in rel_issues
            ]
            for rel, rel_issues in issues.items()
        }
        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                delete=False,
                dir=str(self._lint_cache_dir),
                prefix=f".{tool}_{key}.",
                suffix=".tmp",
            ) as tmp:
                tmp.write(json.dumps(payload, ensure_ascii=False))
                tmp.flush()
                os.fsync(tmp.fileno())
                tmp_path = Path(tmp.name)
            os.replace(tmp_path, path)
        except OSError:
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
            return

    @staticmethod
    def _limit_semantic_lines(lines: list[str]) -> tuple[list[str], bool]:
        if len(lines) > MAX_SEMANTIC_LINES:
            return lines[:MAX_SEMANTIC_LINES], True
        return lines, False

    @staticmethod
    def _ast_name(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        return ""
