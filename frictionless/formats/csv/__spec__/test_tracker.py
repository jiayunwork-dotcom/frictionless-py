from __future__ import annotations

import pytest

from frictionless import Dialect, formats
from frictionless.errors import CellError, RowError
from frictionless.fields import IntegerField, StringField
from frictionless.resources import TableResource
from frictionless.formats.csv.tracker import TrackingCsvReader, BOM

pytestmark = pytest.mark.skipif(
    not hasattr(formats.CsvControl, "track_source"), reason="source tracking unavailable"
)


# Helpers


def read_tracked(data, *, encoding=None, schema=None, **dialect_options):
    control = formats.CsvControl(track_source=True, **dialect_options.pop("control", {}))
    dialect = Dialect(controls=[control], **dialect_options)
    options = {"format": "csv", "dialect": dialect}
    if encoding:
        options["encoding"] = encoding
    if schema is not None:
        options["schema"] = schema
    with TableResource(data, **options) as resource:
        return resource.read_rows(), resource


def source_of(data, **options):
    rows, _ = read_tracked(data, **options)
    return [row.source for row in rows]


# Tracker unit behavior


def test_track_source_simple_ranges():
    rows, _ = read_tracked(b"a,b\nc,d\n")
    # First physical line is the header; the data row starts on line 2
    assert rows[0].source == {
        "record": [[2, 1], [2, 4]],
        "cells": [[[2, 1], [2, 2]], [[2, 3], [2, 4]]],
    }


def test_track_source_record_excludes_terminator_and_keeps_crlf():
    rows, _ = read_tracked(b"a,b\r\nc,d\r\n")
    assert rows[0].source["record"] == [[2, 1], [2, 4]]
    # Header sample keeps its own source too
    with TableResource(
        b"a,b\r\nc,d\r\n",
        format="csv",
        dialect=Dialect(controls=[formats.CsvControl(track_source=True)]),
    ) as resource:
        assert resource.sample[0].source["record"] == [[1, 1], [1, 4]]


def test_track_source_bare_cr_lf_each_break_a_line():
    # header=False so every physical line is a data row
    rows, _ = read_tracked(b"a\rb\rc", header=False)
    assert [row.source["record"][0][0] for row in rows] == [1, 2, 3]


def test_track_source_quoted_multiline_lf_and_crlf():
    rows, _ = read_tracked(b'"a\nb",c\nx,y\n', header=False)
    # Line 1: " a \n ; line 2: b " , c \n
    assert rows[0].source == {
        "record": [[1, 1], [2, 5]],
        "cells": [[[1, 1], [2, 3]], [[2, 4], [2, 5]]],
    }
    rows2, _ = read_tracked(b'"a\r\nb",c\r\n', header=False)
    assert rows2[0].source["record"] == [[1, 1], [2, 5]]
    # The embedded CRLF stays inside the quoted cell range
    assert rows2[0].source["cells"][0] == [[1, 1], [2, 3]]


def test_track_source_doubled_quote_keeps_quotes():
    rows, _ = read_tracked(b'"a""b",c\n', header=False)
    assert rows[0].cells == ['a"b', "c"]
    # Both outer and doubled quotes remain in the cell range
    assert rows[0].source["cells"][0] == [[1, 1], [1, 7]]


def test_track_source_escape_char_keeps_escape():
    rows, _ = read_tracked(b"a\\,b,c\n", header=False, control={"escape_char": "\\"})
    assert rows[0].cells == ["a,b", "c"]
    assert rows[0].source["cells"][0] == [[1, 1], [1, 5]]


def test_track_source_escaped_newline_is_data():
    rows, _ = read_tracked(b"a\\\nb,c\n", header=False, control={"escape_char": "\\"})
    assert len(rows) == 1
    assert rows[0].cells == ["a\nb", "c"]
    # The escaped newline keeps the record open onto line 2
    assert rows[0].source["record"] == [[1, 1], [2, 4]]


def test_track_source_skip_initial_space_keeps_whitespace():
    rows, _ = read_tracked(
        b"a, b\n", header=False, control={"skip_initial_space": True}
    )
    # The space after the delimiter (col 3) stays in the cell's source range
    assert rows[0].source["cells"][1] == [[1, 3], [1, 5]]


def test_track_source_empty_quoted_field_keeps_both_quotes():
    rows, _ = read_tracked(b'a,"",b\n', header=False)
    assert rows[0].cells == ["a", "", "b"]
    assert rows[0].source["cells"][1] == [[1, 3], [1, 5]]


def test_track_source_empty_unquoted_field_is_zero_length():
    rows, _ = read_tracked(b"a,,b\n", header=False)
    assert rows[0].source["cells"][1] == [[1, 3], [1, 3]]


def test_track_source_bom_utf8_and_utf8_sig():
    payload = BOM + "a,b\r\nc,d\r\n"
    rows_utf8, _ = read_tracked(payload.encode("utf-8"), encoding="utf-8")
    rows_sig, _ = read_tracked(payload.encode("utf-8-sig"), encoding="utf-8-sig")
    for rows in (rows_utf8, rows_sig):
        # Positions start after the stripped BOM; data row is physical line 2
        assert rows[0].source["record"] == [[2, 1], [2, 4]]
        assert rows[0].cells == ["c", "d"]


def test_track_source_tsv():
    rows, _ = read_tracked(b"a\tb\nc\td\n", header=False, control={"delimiter": "\t"})
    assert rows[0].cells == ["a", "b"]
    assert rows[0].source["cells"] == [[[1, 1], [1, 2]], [[1, 3], [1, 4]]]


def test_track_source_dialect_filters_keep_physical_positions():
    data = b"# comment\r\n\r\nh1,h2\r\nv1,v2\r\n"
    dialect = Dialect(
        comment_char="#",
        skip_blank_rows=True,
        controls=[formats.CsvControl(track_source=True)],
    )
    with TableResource(data, format="csv", dialect=dialect) as resource:
        rows = resource.read_rows()
    # Comment and blank lines still occupy logical/physical row numbers
    assert rows[0].row_number == 3
    assert rows[0].source["record"] == [[3, 1], [3, 6]]
    assert rows[1].row_number == 4
    assert rows[1].source["record"] == [[4, 1], [4, 6]]


def test_track_source_no_header_and_multiline_header():
    data = b"a,b\r\nc,d\r\n"
    rows_no_header, _ = read_tracked(data, header=False)
    assert rows_no_header[0].source["record"] == [[1, 1], [1, 4]]

    rows_multi, _ = read_tracked(
        b"h1,h2\r\nk1,k2\r\nv1,v2\r\n", header_rows=[1, 2]
    )
    assert rows_multi[0].source["record"] == [[3, 1], [3, 6]]


def test_track_source_extra_and_missing_cells():
    schema = [StringField(name="a"), StringField(name="b")]
    from frictionless import Schema

    # Extra cell: it has its own real range (data row is physical line 2)
    rows, resource = read_tracked(b"a,b\nx,y,z\n", schema=Schema(fields=schema))
    row = rows[0]
    extra = next(e for e in row.errors if e.type == "extra-cell")
    assert isinstance(extra, CellError)
    assert extra.to_descriptor()["source"]["cell"] == [[2, 5], [2, 6]]

    # Missing cell: no token, cell is null
    rows2, _ = read_tracked(b"a,b\nx\n", schema=Schema(fields=schema))
    missing = next(e for e in rows2[0].errors if e.type == "missing-cell")
    assert missing.to_descriptor()["source"] == {
        "record": [[2, 1], [2, 2]],
        "cell": None,
    }


# Validation integration


def test_track_source_task_file_coordinates():
    text = BOM + "# collected\r\nname,amount\r\n\"北,\r\n京\",oops\r\nok,7\r\n"
    from frictionless import Schema

    dialect = Dialect(
        comment_char="#",
        controls=[formats.CsvControl(track_source=True)],
    )
    schema = Schema(fields=[StringField(name="name"), IntegerField(name="amount")])
    with TableResource(
        text.encode("utf-8"), format="csv", dialect=dialect, schema=schema
    ) as resource:
        rows = resource.read_rows()
    assert rows[0].row_number == 3
    assert rows[0].source["record"] == [[3, 1], [4, 8]]
    assert rows[0].source["cells"][1] == [[4, 4], [4, 8]]

    report = resource.validate()
    error = report.tasks[0].errors[0]
    assert error.row_number == 3
    descriptor = error.to_descriptor()
    assert descriptor["source"] == {
        "record": [[3, 1], [4, 8]],
        "cell": [[4, 4], [4, 8]],
    }


def test_track_source_row_error_carries_only_record():
    from frictionless import Schema

    dialect = Dialect(controls=[formats.CsvControl(track_source=True)])
    schema = Schema(fields=[StringField(name="a")])
    # Header on line 1, blank data record on line 2
    with TableResource(b"a\n,\n", format="csv", dialect=dialect, schema=schema) as resource:
        report = resource.validate()
    blank = next(e for e in report.tasks[0].errors if e.type == "blank-row")
    assert isinstance(blank, RowError) and not isinstance(blank, CellError)
    assert blank.to_descriptor()["source"] == {"record": [[2, 1], [2, 2]]}


def test_track_source_descriptor_round_trip():
    from frictionless import Report, Schema

    dialect = Dialect(controls=[formats.CsvControl(track_source=True)])
    schema = Schema(fields=[IntegerField(name="n")])
    with TableResource(b"n\nx\n", format="csv", dialect=dialect, schema=schema) as resource:
        report = resource.validate()
    descriptor = report.to_descriptor()
    restored = Report.from_descriptor(descriptor)
    original_source = report.tasks[0].errors[0].to_descriptor()["source"]
    restored_source = restored.tasks[0].errors[0].to_descriptor()["source"]
    assert restored_source == original_source


def test_track_source_disabled_has_no_source():
    with TableResource(b"a,b\n1,2\n", format="csv") as resource:
        row = resource.read_rows()[0]
    assert row.source is None
    report = resource.validate()
    for task in report.tasks:
        for error in task.errors:
            assert "source" not in error.to_descriptor()


def test_track_source_rejects_non_utf8_encoding():
    from frictionless.exception import FrictionlessException

    data = "café\n".encode("iso8859-1")
    dialect = Dialect(controls=[formats.CsvControl(track_source=True)])
    resource = TableResource(
        data, format="csv", encoding="iso8859-1", dialect=dialect
    )
    with pytest.raises(FrictionlessException) as exc:
        with resource:
            resource.read_rows()
    assert exc.value.error.type == "encoding-error"


def test_track_source_immutable_after_mutation_and_reopen():
    dialect = Dialect(controls=[formats.CsvControl(track_source=True)])
    with TableResource(b"a,b\n1,2\n", format="csv", dialect=dialect) as resource:
        row = resource.read_rows()[0]
    snapshot = row.source
    row["a"] = 999
    row.cells[0] = "changed"
    assert row.source == snapshot

    # Reopening the resource produces fresh, independent sources
    with TableResource(b"a,b\n1,2\n", format="csv", dialect=dialect) as resource:
        other = resource.read_rows()[0]
    assert other.source == snapshot
    assert other.source is not row.source


def test_track_source_dict_and_list_have_no_metadata():
    rows, _ = read_tracked(b"a,b\n1,2\n")
    row = rows[0]
    assert set(row.to_dict().keys()) == {"a", "b"}
    assert row.to_list() == [1, 2]


def test_track_source_control_descriptor_name():
    control = formats.CsvControl(track_source=True)
    assert control.to_descriptor()["trackSource"] is True
    assert "trackSource" not in formats.CsvControl().to_descriptor()
    restored = formats.CsvControl.from_descriptor(
        {"type": "csv", "trackSource": True}
    )
    assert restored.track_source is True


def test_track_source_survives_sample_replay_across_boundary():
    # Default sample size is 100 records; rows beyond that come from the
    # replayed stream. Positions must stay consistent and unique.
    data = ("h\n" + "".join(f"v{index}\n" for index in range(150))).encode()
    dialect = Dialect(controls=[formats.CsvControl(track_source=True)])
    with TableResource(data, format="csv", dialect=dialect) as resource:
        sample_sources = [cells.source["record"] for cells in resource.sample]
        rows = resource.read_rows()
    stream_sources = [row.source["record"] for row in rows]
    # The sample includes the header (line 1); data rows start on line 2
    assert all(
        stream_sources[index] == sample_sources[index + 1] for index in range(99)
    )
    assert len({str(source) for source in stream_sources}) == 150


def test_track_source_reader_matches_stdlib_values():
    # The custom parser must yield the same cell values as csv.reader
    import csv
    import io

    text = "a,b\r\n\"x\r\ny\",z\r\n,,\r\n"
    tracker = TrackingCsvReader(
        iter(io.StringIO(text, newline="")),
        delimiter=",",
        quote_char='"',
        escape_char=None,
        skip_initial_space=False,
        double_quote=True,
    )
    assert [list(item) for item in tracker] == list(
        csv.reader(io.StringIO(text, newline=""))
    )
