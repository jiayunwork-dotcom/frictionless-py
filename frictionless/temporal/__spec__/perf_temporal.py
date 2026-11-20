"""Performance harness for temporal foreign key validation.

Run as a script (not collected by pytest):

    python perf_temporal.py small|large

The fixture is produced by generators only (nothing is materialized as a
list on the harness side) and mixes two stress shapes under the same
rules at both scales:

- "history" keys with a long chain of disjoint monthly versions
- "overlap" keys with hundreds of simultaneously valid versions

Facts are 80% valid single matches, 10% ambiguous (inside the overlap)
and 10% missing, so the exact counting path is exercised for every fact
row instead of assuming a single hit.

Prints one JSON line with scale, seconds, tracemalloc peak, staging
bytes and observed error statistics.
"""

import json
import sys
import time
import tracemalloc

from frictionless import Package, Resource, Schema
from frictionless.temporal import TemporalEngine

VERSION_DAY = "2020-01-01T00:00:00+00:00"


def month_iso(offset):
    """ISO moment for 2020-01 + offset months (UTC)"""
    year = 2020 + offset // 12
    month = offset % 12 + 1
    return f"{year:04d}-{month:02d}-01T00:00:00+00:00"


def day_iso(offset):
    """ISO moment for 2020-01-01 + offset days (UTC, 2020 is a leap year)"""
    year = 2020
    month = 1
    day = 1 + offset
    while True:
        if month == 12:
            days = 31
        elif month == 2:
            days = 29 if year % 4 == 0 else 28
        elif month in (4, 6, 9, 11):
            days = 30
        else:
            days = 31
        if day <= days:
            break
        day -= days
        month += 1
        if month == 13:
            month = 1
            year += 1
    return f"{year:04d}-{month:02d}-{day:02d}T00:00:00+00:00"


def versions_data(scale):
    """Yield the version rows (header first) exactly once"""
    yield ["product", "begin", "end"]
    # history keys: 1000 disjoint monthly versions each
    for key in range(int(50 * scale)):
        name = f"H{key:05d}"
        for offset in range(1000):
            yield [name, month_iso(offset), month_iso(offset + 1)]
    # overlap keys: 500 overlapping versions each (all covering mid 2022)
    for key in range(int(100 * scale)):
        name = f"O{key:05d}"
        for offset in range(500):
            yield [name, day_iso(offset), "2025-01-01T00:00:00+00:00"]


def facts_data(scale):
    """Yield the fact rows (header first) exactly once"""
    yield ["product", "occurred"]
    count = int(200000 * scale)
    for index in range(count):
        bucket = index % 10
        if bucket < 8:
            # valid: inside exactly one monthly interval of a history key
            yield [f"H{index % int(50 * scale):05d}", month_iso(index % 1000)]
        elif bucket == 8:
            # ambiguous: every overlap version covers this moment
            yield [f"O{index % int(100 * scale):05d}", "2022-06-01T00:00:00+00:00"]
        else:
            # missing: unknown key
            yield [f"X{index:06d}", "2022-06-01T00:00:00+00:00"]


def make_package(scale):
    sales_schema = Schema(
        {
            "fields": [
                {"name": "product", "type": "string"},
                {"name": "occurred", "type": "datetime"},
            ],
            "temporalForeignKeys": [
                {
                    "fields": ["product"],
                    "at": "occurred",
                    "reference": {
                        "resource": "versions",
                        "fields": ["product"],
                        "validFrom": "begin",
                        "validTo": "end",
                    },
                }
            ],
        }
    )
    versions_schema = Schema(
        {
            "fields": [
                {"name": "product", "type": "string"},
                {"name": "begin", "type": "datetime"},
                {"name": "end", "type": "datetime"},
            ]
        }
    )
    return Package(
        resources=[
            Resource(data=facts_data(scale), name="sales", schema=sales_schema),
            Resource(data=versions_data(scale), name="versions", schema=versions_schema),
        ]
    )


def run(scale):
    package = make_package(scale)
    tracemalloc.start()
    started = time.time()
    # Drive the engine exactly like Package.validate does, keeping access
    # to the staging statistics for the report
    engine = TemporalEngine(package)
    try:
        reports = []
        for resource in package.resources:
            involved = engine.is_involved(resource.name)
            report = resource.validate(
                limit_errors=None if involved else 1000,
                on_row=engine.make_row_callback(resource.name),
            )
            reports.append(report)
        budgets = {}
        for report in reports:
            task = report.tasks[0]
            budgets[task.name] = max(0, 1000 - len(task.errors))
        temporal_errors = engine.finalize(budgets)
        staging_bytes = engine.staging_bytes
    finally:
        engine.cleanup()
    seconds = time.time() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    errors, truncated = temporal_errors.get("sales", ([], False))
    ambiguous = [error for error in errors if error.type == "temporal-key-ambiguous"]
    missing = [error for error in errors if error.type == "temporal-key-missing"]
    return {
        "scale": scale,
        "versions": 100000 * scale,
        "facts": 200000 * scale,
        "seconds": round(seconds, 2),
        "peak_mib": round(peak / 1024 / 1024, 2),
        "staging_mib": round(staging_bytes / 1024 / 1024, 2),
        "saved_errors": len(errors),
        "truncated": truncated,
        "ambiguous_saved": len(ambiguous),
        "missing_saved": len(missing),
        "ambiguous_match_count": ambiguous[0].match_count if ambiguous else None,
        "ambiguous_reference_rows": (ambiguous[0].reference_rows if ambiguous else None),
    }


if __name__ == "__main__":
    scale = {"small": 1, "large": 5}[sys.argv[1]]
    print(json.dumps(run(scale)))
