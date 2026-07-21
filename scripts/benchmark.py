#!/usr/bin/env python
"""Pixrep performance benchmark.

Times ONEPDF (cold vs warm FileBlockCache) and generate (full vs incremental)
so performance regressions show up. Run manually or from CI:

    python scripts/benchmark.py [repo_path]

Prints wall-clock seconds per phase. Does not assert thresholds — pair with a
trend store (hyperfine / pytest-benchmark / CI artifacts) to detect drift.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def _run(cmd: list[str], cache_dir: str, out: str) -> float:
    env = {**os.environ, "PIXREP_CACHE_DIR": cache_dir}
    t0 = time.perf_counter()
    subprocess.run(cmd, env=env, check=True, capture_output=True)
    return time.perf_counter() - t0


def main(repo: str) -> None:
    py = sys.executable
    with (
        tempfile.TemporaryDirectory() as cache,
        tempfile.TemporaryDirectory() as out,
    ):
        cache_dir, out_dir = str(Path(cache)), str(Path(out))

        print("=== pixrep onepdf ===")
        cold = _run(
            [py, "-m", "pixrep", "onepdf", repo, "-o", f"{out_dir}/cold.pdf"], cache_dir, out_dir
        )
        print(f"  cold: {cold:.2f}s")
        warm = _run(
            [py, "-m", "pixrep", "onepdf", repo, "-o", f"{out_dir}/warm.pdf"], cache_dir, out_dir
        )
        print(f"  warm: {warm:.2f}s  (FileBlockCache hit)")

        print("=== pixrep generate ===")
        full = _run(
            [py, "-m", "pixrep", "generate", repo, "-o", f"{out_dir}/gen"], cache_dir, out_dir
        )
        print(f"  full: {full:.2f}s")
        incr = _run(
            [py, "-m", "pixrep", "generate", repo, "-o", f"{out_dir}/gen", "--incremental"],
            cache_dir,
            out_dir,
        )
        print(f"  incremental: {incr:.2f}s")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parents[1]))
