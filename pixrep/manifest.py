"""Build manifest: per-repo record of rendered files and their content fingerprints.

Drives incremental generation (`--incremental`) so that:

* file identity uses content fingerprints (git blob OID or sha1) instead of
  fragile mtime comparisons (P0-1);
* changing rendering options (theme, font, DPI, format, semantic/lint toggles,
  linter tool version/config, pixrep version) correctly invalidates stale
  outputs (P0-1).

A `BuildManifest` is one JSON file under the cache root: `manifest.json`.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .constants import RENDER_SCHEMA_VERSION
from .file_utils import atomic_write_text
from .version import __version__


def _tool_version(tool: str) -> str:
    """Best-effort version string for a linter tool (empty if unavailable)."""
    if not shutil.which(tool):
        return ""
    try:
        proc = subprocess.run(
            [tool, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return out.splitlines()[0] if out else ""


def _config_signature(paths: list[Path]) -> str:
    """Content hash of config files that affect lint output.

    Uses the file bytes (not mtime+size) so a config rewritten to the same
    length with the same mtime still invalidates the cache — consistent with the
    scanner's working-tree SHA-256 identity."""
    h = hashlib.sha256()
    found = False
    for p in sorted(paths):
        try:
            data = p.read_bytes()
        except OSError:
            continue
        found = True
        h.update(str(p.resolve()).encode("utf-8"))
        h.update(b"\0")
        h.update(hashlib.sha256(data).digest())
    return h.hexdigest()[:16] if found else ""


def compute_options_hash(
    *,
    theme_id: str = "",
    font_id: str = "",
    png_dpi: int = 150,
    output_format: str = "pdf",
    enable_semantic: bool = True,
    enable_lint: bool = True,
    syntax_mode: str = "full",
    repo_root: Path | None = None,
    pixrep_version: str = __version__,
) -> str:
    """Fingerprint of rendering/cache options.

    Changing any of these must invalidate cached semantic maps, lint results,
    symbol index entries and rendered outputs.
    """
    # Skip linter version + config probing entirely when the lint heatmap is
    # disabled — no point spawning ruff/eslint --version on every cold run.
    ruff_version = ""
    eslint_version = ""
    ruff_config_sig = ""
    eslint_config_sig = ""
    if enable_lint:
        ruff_version = _tool_version("ruff")
        eslint_version = _tool_version("eslint")
        if repo_root is not None:
            ruff_config_sig = _config_signature(
                [
                    repo_root / "pyproject.toml",
                    repo_root / "ruff.toml",
                    repo_root / ".ruff.toml",
                ]
            )
            eslint_config_sig = _config_signature(
                [
                    repo_root / ".eslintrc.json",
                    repo_root / ".eslintrc.js",
                    repo_root / ".eslintrc.cjs",
                    repo_root / ".eslintrc.yaml",
                    repo_root / ".eslintrc.yml",
                    repo_root / ".eslintrc",
                    repo_root / "eslint.config.js",
                    repo_root / "eslint.config.mjs",
                    repo_root / "eslint.config.cjs",
                    repo_root / "eslint.config.ts",
                    repo_root / "eslint.config.mts",
                    repo_root / "eslint.config.cts",
                    repo_root / "package.json",
                ]
            )

    payload = "|".join(
        [
            f"pixrep={pixrep_version}",
            f"theme={theme_id}",
            f"font={font_id}",
            f"dpi={png_dpi}",
            f"fmt={output_format}",
            f"sem={int(enable_semantic)}",
            f"lint={int(enable_lint)}",
            f"syntax={syntax_mode}",
            f"ruff={ruff_version}",
            f"eslint={eslint_version}",
            f"ruffcfg={ruff_config_sig}",
            f"eslintcfg={eslint_config_sig}",
            f"rschema={RENDER_SCHEMA_VERSION}",
        ]
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


@dataclass
class FileEntry:
    git_blob: str
    size: int
    outputs: list[str] = field(default_factory=list)
    mtime_ns: int = -1


@dataclass
class BuildManifest:
    """Per-repo build manifest persisted as ``manifest.json``."""

    path: Path
    pixrep_version: str = __version__
    options_hash: str = ""
    files: dict[str, FileEntry] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> BuildManifest:
        manifest = cls(path=path)
        if not path.exists():
            return manifest
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return manifest
        manifest.pixrep_version = str(raw.get("pixrep_version", __version__))
        manifest.options_hash = str(raw.get("options_hash", ""))
        for rel, entry in (raw.get("files") or {}).items():
            if not isinstance(entry, dict):
                continue
            outputs = entry.get("outputs")
            if not outputs and entry.get("output"):
                # Backward compat with the legacy single-output schema.
                outputs = [entry["output"]]
            manifest.files[str(rel)] = FileEntry(
                git_blob=str(entry.get("git_blob", "")),
                size=int(entry.get("size", 0)),
                outputs=[str(o) for o in (outputs or [])],
                mtime_ns=int(entry.get("mtime_ns", -1)),
            )
        return manifest

    def save(self) -> None:
        payload = {
            "pixrep_version": self.pixrep_version,
            "options_hash": self.options_hash,
            "files": {
                rel: {
                    "git_blob": e.git_blob,
                    "size": e.size,
                    "outputs": e.outputs,
                    "mtime_ns": e.mtime_ns,
                }
                for rel, e in self.files.items()
            },
        }
        with contextlib.suppress(OSError):
            atomic_write_text(self.path, json.dumps(payload, ensure_ascii=False))
