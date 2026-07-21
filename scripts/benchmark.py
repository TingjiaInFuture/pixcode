#!/usr/bin/env python
"""Pixrep performance benchmark with regression assertions.

Measures ONEPDF cold/warm (FileBlockCache + scanner snapshot), generate
full/incremental, output bytes, deterministic SHA equality and compact
reduction ratio; asserts basic thresholds so regressions fail the run.

    python scripts/benchmark.py [repo]
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def _run(cmd: list[str], cache_dir: str) -> float:
    env = {**os.environ, "PIXREP_CACHE_DIR": cache_dir}
    t0 = time.perf_counter()
    subprocess.run(cmd, env=env, check=True, capture_output=True)
    return time.perf_counter() - t0


def _sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _size(path: str) -> int:
    return os.path.getsize(path)


def main(repo: str) -> int:
    py = sys.executable
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as cache, tempfile.TemporaryDirectory() as out:
        cache_dir, out_dir = str(Path(cache)), str(Path(out))

        print("=== pixrep onepdf (compact, deterministic) ===")
        cold = _run(
            [py, "-m", "pixrep", "onepdf", repo, "-o", f"{out_dir}/cold.pdf", "--deterministic"],
            cache_dir,
        )
        cold_bytes = _size(f"{out_dir}/cold.pdf")
        print(f"  cold: {cold:.2f}s  bytes: {cold_bytes}")
        warm = _run(
            [py, "-m", "pixrep", "onepdf", repo, "-o", f"{out_dir}/warm.pdf", "--deterministic"],
            cache_dir,
        )
        warm_bytes = _size(f"{out_dir}/warm.pdf")
        print(f"  warm: {warm:.2f}s  bytes: {warm_bytes}  warm/cold: {warm / cold:.2f}")

        print("=== deterministic SHA equality ===")
        sha_cold = _sha(f"{out_dir}/cold.pdf")
        sha_warm = _sha(f"{out_dir}/warm.pdf")
        same = sha_cold == sha_warm
        print(f"  cold==warm: {same}")
        if not same:
            failures.append("deterministic SHA mismatch between two runs")

        print("=== compact vs lossless ===")
        _run(
            [
                py,
                "-m",
                "pixrep",
                "onepdf",
                repo,
                "-o",
                f"{out_dir}/loss.pdf",
                "--profile",
                "lossless",
                "--deterministic",
            ],
            cache_dir,
        )
        loss_bytes = _size(f"{out_dir}/loss.pdf")
        reduction = (1 - warm_bytes / max(loss_bytes, 1)) * 100
        print(f"  lossless: {loss_bytes}  compact: {warm_bytes}  reduction: {reduction:.1f}%")

        print("=== pixrep generate ===")
        full = _run([py, "-m", "pixrep", "generate", repo, "-o", f"{out_dir}/gen"], cache_dir)
        incr = _run(
            [py, "-m", "pixrep", "generate", repo, "-o", f"{out_dir}/gen", "--incremental"],
            cache_dir,
        )
        print(f"  full: {full:.2f}s  incremental: {incr:.2f}s  incr/full: {incr / full:.2f}")

        # Regression thresholds. deterministic-SHA and incremental/full are
        # stable; warm/cold is reported only (small repos are noisy — snapshot/
        # block savings show up on large trees).
        if not same:
            failures.append("deterministic SHA mismatch between two runs")
        if incr > full * 0.35:
            failures.append(f"incremental/full ratio {incr / full:.2f} > 0.35")

    print()
    if failures:
        print("BENCHMARK THRESHOLD FAILURES:")
        for msg in failures:
            print(f"  - {msg}")
        return 1
    print("All benchmark thresholds passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parents[1])))
