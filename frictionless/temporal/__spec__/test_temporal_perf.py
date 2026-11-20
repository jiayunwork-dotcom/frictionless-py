import json
import subprocess
import sys

import pytest

from . import perf_temporal

# The budgets the temporal validation must respect
PEAK_LIMIT_MIB = 128


def run_harness(scale):
    """Run the performance harness in an independent process"""
    script = perf_temporal.__file__
    output = subprocess.run(
        [sys.executable, script, scale],
        capture_output=True,
        text=True,
        check=True,
        timeout=900,
    )
    return json.loads(output.stdout.strip().splitlines()[-1])


@pytest.mark.timeout(300)
def test_temporal_perf_small_scale():
    metrics = run_harness("small")
    print("\nperf small:", json.dumps(metrics))
    assert metrics["versions"] == 100000
    assert metrics["facts"] == 200000
    assert metrics["peak_mib"] <= PEAK_LIMIT_MIB
    # exact counting is exercised on high-overlap keys
    assert metrics["ambiguous_match_count"] == 500
    assert metrics["ambiguous_saved"] > 0
    assert metrics["missing_saved"] > 0
    assert metrics["saved_errors"] == 1000
    assert metrics["staging_mib"] > 0


@pytest.mark.timeout(600)
def test_temporal_perf_large_scale():
    metrics = run_harness("large")
    print("\nperf large:", json.dumps(metrics))
    assert metrics["versions"] == 500000
    assert metrics["facts"] == 1000000
    assert metrics["peak_mib"] <= PEAK_LIMIT_MIB
    assert metrics["ambiguous_match_count"] == 500
    assert metrics["saved_errors"] == 1000
    assert metrics["staging_mib"] > 0


def test_temporal_perf_end_to_end_package_validate():
    # A moderate end-to-end pass through Package.validate (the public
    # path) with the same fixture rules at a reduced scale factor
    package = perf_temporal.make_package(0.1)
    report = package.validate()
    # 20000 facts: 2000 ambiguous (500 matches each) + 2000 missing
    assert report.stats["errors"] == 1000  # capped by the default limit
    task = report.tasks[0]
    assert "reached error limit: 1000" in task.warnings
    types = {error.type for error in task.errors}
    assert types == {"temporal-key-missing", "temporal-key-ambiguous"}
    ambiguous = [error for error in task.errors if error.type == "temporal-key-ambiguous"]
    assert ambiguous[0].match_count == 500
    assert ambiguous[0].reference_rows == sorted(ambiguous[0].reference_rows)
