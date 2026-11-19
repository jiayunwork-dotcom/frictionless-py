"""Worker for the memory check: one fresh process per (track, rows) config.

Prints a single number: the tracemalloc peak in bytes during the streaming
phase. Input files are prepared beforehand (their allocation is excluded).
Run via ``test_memory.py``, never directly in a shared interpreter.
"""
import gc
import os
import sys
import tracemalloc

# Make the in-tree package importable without relying on an installed copy
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from frictionless import Dialect, Resource, Schema, formats  # noqa
from frictionless.fields import StringField  # noqa

COLUMNS = 4


def main() -> None:
    path = sys.argv[1]
    track = sys.argv[2] == "on"
    dialect = Dialect(controls=[formats.CsvControl(track_source=track)])
    schema = Schema(fields=[StringField(name=f"c{i}") for i in range(COLUMNS)])
    gc.collect()
    tracemalloc.start()
    try:
        gc.collect()
        tracemalloc.reset_peak()
        kept = []
        with Resource(path, format="csv", dialect=dialect, schema=schema) as resource:
            for index, row in enumerate(resource.row_stream):
                row.valid  # exercise lazy processing / error structures
                if index < 2:
                    kept.append(row)
        assert len(kept) == 2
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    print(peak)


if __name__ == "__main__":
    main()
