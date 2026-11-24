#!/usr/bin/env python3
"""Reproducible checks for record-safe multipart CSV shards.

Examples
--------
Small boundary/content checks:

    python scripts/check_multipart.py

One-million-row / 1 MiB shard memory check:

    python scripts/check_multipart.py --million
"""

from __future__ import annotations

import argparse
import csv
import json
import tempfile
import time
import tracemalloc
from pathlib import Path

from frictionless import schemes
from frictionless.resources import TableResource


def small_checks(workdir: Path) -> None:
    rows = [{"id": number, "name": f"n{number}"} for number in range(1, 9)]
    pattern = workdir / "table{number}.csv"

    target = TableResource(data=rows).write(
        str(pattern),
        scheme="multipart",
        control=schemes.MultipartControl(chunk_size=40),
    )

    first = Path(target.normpaths[0]).read_bytes()
    second = Path(target.normpaths[1]).read_bytes()
    assert first == (
        b"id,name\r\n"
        b"1,n1\r\n2,n2\r\n3,n3\r\n4,n4\r\n5,n5\r\n"
    )
    assert len(first) <= 40
    assert second == b"id,name\r\n6,n6\r\n7,n7\r\n8,n8\r\n"
    assert target.read_rows() == rows
    assert target.validate().valid

    TableResource(data=rows).write(str(workdir / "single.csv"))
    multi = TableResource(
        path=target.path,
        extrapaths=target.extrapaths,
        basepath=target.basepath,
    )
    single = TableResource(str(workdir / "single.csv"))
    multi.infer(stats=True)
    single.infer(stats=True)
    assert multi.bytes == single.bytes
    assert multi.hash == single.hash

    descriptor = target.to_descriptor()
    restored = TableResource.from_descriptor(
        descriptor,
        basepath=target.basepath,
    )
    assert restored.read_rows() == rows
    assert restored.validate().valid

    descriptor_path = workdir / "multipart-resource.json"
    descriptor_path.write_text(json.dumps(descriptor), encoding="utf-8")
    restored_from_file = TableResource.from_descriptor(str(descriptor_path))
    assert restored_from_file.read_rows() == rows
    assert restored_from_file.validate().valid

    print("small checks: OK")
    print("shards:", *target.normpaths, sep="\n  ")
    print("bytes/hash:", multi.bytes, multi.hash)


def million_row_check(workdir: Path) -> None:
    row_count = 1_000_000
    source = workdir / "million-single.csv"
    pattern = workdir / "million-part{number}.csv"

    print(f"generating {row_count} rows in {source} ...")
    with source.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["id", "name"])
        for number in range(row_count):
            writer.writerow([number, f"name-{number}"])

    tracemalloc.start(25)
    baseline = tracemalloc.get_traced_memory()[0]
    tracemalloc.reset_peak()
    started = time.time()

    target = TableResource(path=str(source), encoding="utf-8").write(
        str(pattern),
        scheme="multipart",
        control=schemes.MultipartControl(chunk_size=1024 * 1024),
    )

    _, peak = tracemalloc.get_traced_memory()
    peak_increment = peak - baseline
    elapsed = time.time() - started

    multi = TableResource(
        path=target.path,
        extrapaths=target.extrapaths,
        basepath=target.basepath,
        encoding="utf-8",
    )
    single = TableResource(path=str(source), encoding="utf-8")
    multi.infer(stats=True)
    single.infer(stats=True)

    assert multi.rows == row_count
    assert multi.bytes == single.bytes
    assert multi.hash == single.hash
    assert peak_increment <= 8 * 1024 * 1024

    print(f"shards: {len(target.paths)}")
    print(f"write seconds: {elapsed:.1f}")
    print(f"rows: {multi.rows}; bytes: {multi.bytes}")
    print(f"hash: {multi.hash}")
    print(f"tracemalloc peak increment: {peak_increment / 1024 / 1024:.2f} MiB")
    print("million-row check: OK")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--million", action="store_true")
    parser.add_argument(
        "--workdir",
        type=Path,
        default=Path(tempfile.mkdtemp(prefix="frictionless-multipart-")),
    )
    args = parser.parse_args()
    args.workdir.mkdir(parents=True, exist_ok=True)

    small_checks(args.workdir)
    if args.million:
        million_row_check(args.workdir)


if __name__ == "__main__":
    main()
