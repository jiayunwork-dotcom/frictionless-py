"""Memory check: source-tracking overhead must not grow with total row count.

Scenario from the source-tracking requirement: 4 columns, at most 256 code
points per field, fixed sample size and error cap, only two Rows retained.
Each configuration runs in a fresh cold subprocess so one-time warm-up
allocations (encoding detection, imports) never leak between configurations.
Input files are prepared first, so building the input is excluded from the
measurement. The peak Python allocation growth from 100k to 1m rows with
tracking enabled must stay within 16 MiB.

Re-run entry point::

    python3 -s benchmarks/source_tracking/test_memory.py
"""
from __future__ import annotations

import os
import subprocess
import sys

COLUMNS = 4
FIELD_CHARS = 200  # below the 256 code-point cap
HERE = os.path.dirname(os.path.abspath(__file__))
DIRECTORY = os.path.join(HERE, "memdata")
WORKER = os.path.join(HERE, "mem_worker.py")
LIMIT = 16 * 1024 * 1024


def write_file(path: str, n_rows: int) -> None:
    header = ",".join(f"c{i}" for i in range(COLUMNS)) + "\n"
    row = ",".join("x" * FIELD_CHARS for _ in range(COLUMNS)) + "\n"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(header)
        for _ in range(n_rows):
            handle.write(row)


def cold_peak(path: str, track: bool) -> int:
    output = subprocess.run(
        [sys.executable, WORKER, path, "on" if track else "off"],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(output.stdout.strip())


def main() -> None:
    os.makedirs(DIRECTORY, exist_ok=True)
    sizes = (100_000, 1_000_000)
    paths = {}
    for n in sizes:
        path = os.path.join(DIRECTORY, f"rows_{n}.csv")
        if not os.path.exists(path):
            write_file(path, n)  # preparation allocation, not traced
        paths[n] = path

    results = {}
    for track in (False, True):
        for n in sizes:
            peak = cold_peak(paths[n], track)
            results[(track, n)] = peak
            print(f"track={track!s:5} rows={n:>9} peak={peak/1024/1024:8.2f} MiB")

    delta_on = results[(True, 1_000_000)] - results[(True, 100_000)]
    delta_off = results[(False, 1_000_000)] - results[(False, 100_000)]
    print(f"\npeak increment 100k->1m (tracking on) : {delta_on/1024/1024:8.2f} MiB")
    print(f"peak increment 100k->1m (tracking off): {delta_off/1024/1024:8.2f} MiB")
    if delta_on <= LIMIT:
        print("PASS: within 16 MiB")
    else:
        print("FAIL: exceeds 16 MiB")
        sys.exit(1)


if __name__ == "__main__":
    main()
