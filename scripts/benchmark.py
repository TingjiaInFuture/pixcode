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

SOURCE_ROOT = str(Path(__file__).resolve().parents[1])
# Make in-process imports (the incremental test) resolve to the source tree,
# not a stale installed copy in site-packages.
sys.path.insert(0, SOURCE_ROOT)


def _run(cmd: list[str], cache_dir: str) -> float:
    env = {
        **os.environ,
        "PIXREP_CACHE_DIR": cache_dir,
        # A subprocess `python -m pixrep` does NOT inherit the parent sys.path,
        # so force the source tree onto PYTHONPATH and run it from SOURCE_ROOT,
        # otherwise we benchmark whatever stale pixrep is installed.
        "PYTHONPATH": SOURCE_ROOT + os.pathsep + os.environ.get("PYTHONPATH", ""),
    }
    t0 = time.perf_counter()
    subprocess.run(cmd, env=env, check=True, capture_output=True, cwd=SOURCE_ROOT)
    return time.perf_counter() - t0


def _sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _size(path: str) -> int:
    return os.path.getsize(path)


def _check_pdf(path: str) -> tuple[bool, str]:
    """Open the PDF for real when PyMuPDF is installed (verifies xref / page
    tree / stream decompression), otherwise fall back to the %PDF- header."""
    try:
        import fitz  # PyMuPDF (optional [png] extra)
    except ImportError:
        with open(path, "rb") as fh:
            return (fh.read(5) == b"%PDF-"), "header-only"
    try:
        doc = fitz.open(path)
        try:
            pages = doc.page_count
            if pages > 0:
                doc.load_page(0)  # force page-tree + content-stream decode
        finally:
            doc.close()
        return pages > 0, f"opened ({pages} pages)"
    except Exception as exc:  # report any opener failure verbatim
        return False, f"open-failed: {exc}"


def main(repo: str) -> int:
    py = sys.executable
    failures: list[str] = []

    # Verify a subprocess resolves pixrep to the source tree, not a stale
    # installed copy in site-packages — otherwise every measurement below is
    # meaningless.
    probe_env = {
        **os.environ,
        "PYTHONPATH": SOURCE_ROOT + os.pathsep + os.environ.get("PYTHONPATH", ""),
    }
    probe = subprocess.run(
        [py, "-c", f"import pixrep; assert {SOURCE_ROOT!r} in pixrep.__file__"],
        capture_output=True,
        env=probe_env,
        cwd=SOURCE_ROOT,
    )
    if probe.returncode != 0:
        print("ERROR: subprocess imports pixrep from outside the source tree:")
        print(probe.stderr.decode("utf-8", errors="replace"))
        return 1

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

        print("=== output PDF openability ===")
        for name in ("cold", "warm"):
            ok, detail = _check_pdf(f"{out_dir}/{name}.pdf")
            print(f"  {name}.pdf: {'ok' if ok else 'BAD'} ({detail})")
            if not ok:
                failures.append(f"{name}.pdf failed to open: {detail}")

        print("=== deterministic SHA equality ===")
        sha_cold = _sha(f"{out_dir}/cold.pdf")
        sha_warm = _sha(f"{out_dir}/warm.pdf")
        same = sha_cold == sha_warm
        print(f"  cold==warm: {same}")

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

        print("=== pixrep onepdf --incremental (whole-render skip) ===")
        # Same output path twice with --incremental: an unchanged build signature
        # must skip the entire PDF render on the second run. This exercises the
        # ONEPDF incremental fast path that cold/warm (different output paths,
        # no --incremental) does not.
        from pixrep.onepdf_pack import pack_repo_to_one_pdf

        same_pdf = f"{out_dir}/same.pdf"
        incr_cache = f"{cache_dir}/incr"
        incr_opts = dict(
            repo_root=Path(repo),
            out_pdf=Path(same_pdf),
            profile="compact",
            deterministic=True,
            order="importance",
            incremental=True,
            cache_dir=Path(incr_cache),
            snapshot_path=Path(f"{incr_cache}/snapshot.json"),
        )
        first_stats = pack_repo_to_one_pdf(**incr_opts)
        first_sha = _sha(same_pdf)
        print(
            f"  first:  pages={first_stats.get('pages', 0)} incr_skip={first_stats.get('skipped_incremental', 0)}"
        )

        second_stats = pack_repo_to_one_pdf(**incr_opts)
        second_sha = _sha(same_pdf)
        whole_skip = second_stats.get("skipped_incremental", 0) == 1
        sha_same = first_sha == second_sha
        print(
            f"  second: pages={second_stats.get('pages', 0)} incr_skip={second_stats.get('skipped_incremental', 0)}"
        )
        print(f"  whole-render skipped: {whole_skip}  sha unchanged: {sha_same}")
        if not whole_skip:
            failures.append("second --incremental run did not skip the whole render")
        if not sha_same:
            failures.append("PDF SHA changed between two incremental runs")

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
