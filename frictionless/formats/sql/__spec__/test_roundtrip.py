import json
import sqlite3
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import Mock

import pytest

from frictionless import FrictionlessException, Package, Resource, Schema, formats
from frictionless.formats.sql import settings
from frictionless.resources import TableResource

# Helpers

LOCATION_PROPS = [
    "path",
    "data",
    "scheme",
    "format",
    "mediatype",
    "encoding",
    "compression",
    "innerpath",
    "dialect",
    "hash",
    "bytes",
]


def normalize_descriptor(descriptor):
    """Remove the data-location properties exempt from the roundtrip"""
    descriptor = json.loads(json.dumps(descriptor))
    for resource in descriptor.get("resources", []):
        for prop in LOCATION_PROPS:
            resource.pop(prop, None)
    return descriptor


def assert_descriptor_roundtrip(source, target):
    assert normalize_descriptor(source.to_descriptor()) == normalize_descriptor(
        target.to_descriptor()
    )


def assert_cells_equal(expected, actual):
    assert type(expected) is type(actual)
    assert expected == actual
    if isinstance(expected, Decimal):
        assert str(expected) == str(actual)
    if isinstance(expected, (datetime, time)):
        # Naive values must stay naive; aware values keep their offset
        # and their local time
        assert (expected.tzinfo is None) == (actual.tzinfo is None)
        if expected.tzinfo is not None:
            assert expected.utcoffset() == actual.utcoffset()
            assert expected.replace(tzinfo=None) == actual.replace(tzinfo=None)


def assert_rows_roundtrip(expected, actual):
    assert len(expected) == len(actual)
    for expected_row, actual_row in zip(expected, actual):
        expected_row = dict(expected_row)
        actual_row = dict(actual_row)
        assert set(expected_row) == set(actual_row)
        for name, cell in expected_row.items():
            assert_cells_equal(cell, actual_row[name])


def make_url(tmpdir, name="database.db"):
    return "sqlite:///%s" % str(tmpdir.join(name))


def read_sql(url, query, args=()):
    path = url.replace("sqlite:///", "")
    conn = sqlite3.connect(path)
    try:
        return conn.execute(query, args).fetchall()
    finally:
        conn.close()


def execute_sql(url, *statements):
    path = url.replace("sqlite:///", "")
    conn = sqlite3.connect(path)
    try:
        for statement in statements:
            conn.execute(statement)
        conn.commit()
    finally:
        conn.close()


# Roundtrip: all the field types


def test_sql_roundtrip_all_field_types(tmpdir):
    url = make_url(tmpdir)
    schema = Schema.from_descriptor(
        {
            "fields": [
                {"name": "any", "type": "any"},
                {"name": "array", "type": "array"},
                {"name": "boolean", "type": "boolean"},
                {"name": "date", "type": "date"},
                {"name": "datetime", "type": "datetime"},
                {"name": "duration", "type": "duration"},
                {"name": "geojson", "type": "geojson"},
                {"name": "geopoint", "type": "geopoint"},
                {"name": "integer", "type": "integer"},
                {"name": "list", "type": "list"},
                {"name": "number", "type": "number"},
                {"name": "object", "type": "object"},
                {"name": "string", "type": "string"},
                {"name": "time", "type": "time"},
                {"name": "year", "type": "year"},
                {"name": "yearmonth", "type": "yearmonth"},
            ]
        }
    )
    data = [
        [field["name"] for field in schema.to_descriptor()["fields"]],
        [
            "中国人",
            '["Mike", "John"]',
            "true",
            "2015-01-01",
            "2015-01-01T03:00:00",
            "P1Y1M",
            '{"type": "Point", "coordinates": [33, 33.33]}',
            "30,70",
            "1",
            "a,b,c",
            "7.5",
            '{"chars": 560}',
            "english",
            "03:00:00",
            "2015",
            "2015-01",
        ],
        [
            "42",
            "[1, 2]",
            "false",
            "2020-12-31",
            "2020-12-31T23:59:59",
            "PT3H",
            '{"type": "Point", "coordinates": [1, 2]}',
            "45.5,4.5",
            "-2",
            "x",
            "-0.001",
            "{}",
            "русский",
            "23:59:59",
            "1999",
            "1999-12",
        ],
    ]
    source = Package(resources=[TableResource(data=data, schema=schema, name="types")])
    expected_rows = source.resources[0].read_rows()
    source.publish(url, control=formats.SqlControl(roundtrip=True))

    target = Package(url)
    assert_descriptor_roundtrip(source, target)
    assert target.resource_names == ["types"]
    assert_rows_roundtrip(expected_rows, target.resources[0].read_rows())


def test_sql_roundtrip_any_type_values(tmpdir):
    url = make_url(tmpdir)
    schema = Schema.from_descriptor(
        {
            "fields": [
                {"name": "id", "type": "integer"},
                {"name": "value", "type": "any"},
            ]
        }
    )
    resource = TableResource(
        data=[
            {"id": 1, "value": "text"},
            {"id": 2, "value": 42},
            {"id": 3, "value": 4.5},
            {"id": 4, "value": True},
            {"id": 5, "value": [1, "two", 3.0]},
            {"id": 6, "value": {"nested": [1, {"deep": None}]}},
            {"id": 7, "value": (1, 2)},
            {"id": 8, "value": None},
        ],
        schema=schema,
        name="t",
    )
    source = Package(resources=[resource])
    expected_rows = resource.read_rows()
    source.publish(url, control=formats.SqlControl(roundtrip=True))
    assert_rows_roundtrip(expected_rows, Package(url).resources[0].read_rows())


def test_sql_roundtrip_high_precision_decimal(tmpdir):
    url = make_url(tmpdir)
    schema = Schema.from_descriptor({"fields": [{"name": "num", "type": "number"}]})
    values = [
        "12345678901234567890.125",
        "0.1",
        "1E+30",
        "0.00000000000000000001",
        "-98765432109876543210.375",
        "100",
    ]
    resource = TableResource(
        data=[["num"]] + [[v] for v in values], schema=schema, name="t"
    )
    source = Package(resources=[resource])
    expected_rows = resource.read_rows()
    source.publish(url, control=formats.SqlControl(roundtrip=True))

    rows = Package(url).resources[0].read_rows()
    assert_rows_roundtrip(expected_rows, rows)
    assert str(rows[0]["num"]) == "12345678901234567890.125"
    # The SQL column stays numeric (comparable and sortable)
    assert read_sql(url, "SELECT num FROM t WHERE num > 1e18 ORDER BY num DESC") == [
        (1e30,),
        (1.2345678901234567e19,),
    ]


def test_sql_roundtrip_datetime_and_time_with_mixed_timezones(tmpdir):
    url = make_url(tmpdir)
    schema = Schema.from_descriptor(
        {
            "fields": [
                {"name": "dt", "type": "datetime"},
                {"name": "tm", "type": "time"},
            ]
        }
    )
    data = [
        ["dt", "tm"],
        ["2020-01-02T03:04:05+08:00", "03:04:05+08:00"],
        ["2020-01-02T03:04:05", "03:04:05"],
        ["2020-01-01T19:04:05Z", "19:04:05Z"],
        ["2020-01-02T03:04:05.123456-05:30", "03:04:05.123456-05:30"],
    ]
    resource = TableResource(data=data, schema=schema, name="t")
    source = Package(resources=[resource])
    expected_rows = resource.read_rows()
    source.publish(url, control=formats.SqlControl(roundtrip=True))

    rows = Package(url).resources[0].read_rows()
    assert_rows_roundtrip(expected_rows, rows)
    # Aware datetimes are stored as UTC instants (SQL-comparable)
    assert read_sql(url, "SELECT dt FROM t ORDER BY dt") == [
        ("2020-01-01 19:04:05.000000",),
        ("2020-01-01 19:04:05.000000",),
        ("2020-01-02 03:04:05.000000",),
        ("2020-01-02 08:34:05.123456",),
    ]


def test_sql_roundtrip_descriptor_versions(tmpdir):
    # v2 descriptor ($schema)
    url = make_url(tmpdir, "v2.db")
    source = Package.from_descriptor(
        {
            "$schema": "https://datapackage.org/profiles/2.0/datapackage.json",
            "resources": [
                {
                    "name": "t",
                    "$schema": "https://datapackage.org/profiles/2.0/dataresource.json",
                    "data": [["id"], ["1"]],
                    "schema": {
                        "$schema": "https://datapackage.org/profiles/2.0/tableschema.json",
                        "fields": [{"name": "id", "type": "integer"}],
                    },
                }
            ],
        }
    )
    source.publish(url, control=formats.SqlControl(roundtrip=True))
    target = Package(url)
    assert_descriptor_roundtrip(source, target)
    assert target.to_descriptor()["$schema"].endswith("datapackage.json")

    # v1 descriptor (no $schema)
    url = make_url(tmpdir, "v1.db")
    source = Package.from_descriptor(
        {
            "resources": [
                {
                    "name": "t",
                    "data": [["id"], ["1"]],
                    "schema": {"fields": [{"name": "id", "type": "integer"}]},
                }
            ]
        }
    )
    source.publish(url, control=formats.SqlControl(roundtrip=True))
    target = Package(url)
    assert_descriptor_roundtrip(source, target)
    assert "$schema" not in target.to_descriptor()


def test_sql_roundtrip_metadata_and_constraints(tmpdir):
    url = make_url(tmpdir)
    schema = Schema.from_descriptor(
        {
            "fields": [
                {"name": "id", "type": "integer", "constraints": {"minimum": 1}},
                {
                    "name": "label",
                    "type": "string",
                    "constraints": {"enum": ["a", "b"], "maxLength": 5},
                },
                {"name": "code", "type": "string", "constraints": {"pattern": "[0-9]+"}},
            ],
            "primaryKey": ["id"],
            "missingValues": ["", "N/A"],
        }
    )
    resource = TableResource(
        data=[["id", "label", "code"], ["1", "a", "123"], ["2", "b", "N/A"]],
        schema=schema,
        name="main",
        title="Main Resource",
    )
    source = Package(
        resources=[resource],
        title="My Package",
        licenses=[{"name": "MIT", "title": "MIT"}],
    )
    source.custom["customProp"] = {"nested": True}
    source.publish(url, control=formats.SqlControl(roundtrip=True))

    target = Package(url)
    assert_descriptor_roundtrip(source, target)
    assert target.title == "My Package"
    assert target.licenses[0]["name"] == "MIT"
    # Constraints and missing values are restored (not inferred from SQL)
    restored = target.resources[0].schema.to_descriptor()
    assert restored["fields"][1]["constraints"] == {"enum": ["a", "b"], "maxLength": 5}
    assert restored["fields"][2]["constraints"] == {"pattern": "[0-9]+"}
    assert restored["missingValues"] == ["", "N/A"]
    assert target.resources[0].read_rows()[1]["code"] is None


def test_sql_roundtrip_foreign_keys(tmpdir):
    url = make_url(tmpdir)
    main_schema = Schema.from_descriptor(
        {
            "fields": [
                {"name": "id", "type": "integer"},
                {"name": "parent", "type": "integer"},
                {"name": "note", "type": "string"},
            ],
            "primaryKey": ["id"],
            # Self-referencing foreign key
            "foreignKeys": [
                {"fields": ["parent"], "reference": {"resource": "", "fields": ["id"]}}
            ],
        }
    )
    link_schema = Schema.from_descriptor(
        {
            "fields": [
                {"name": "main_id", "type": "integer"},
                {"name": "tag", "type": "string"},
            ],
            # Cross-resource foreign key
            "foreignKeys": [
                {
                    "fields": ["main_id"],
                    "reference": {"resource": "main", "fields": ["id"]},
                }
            ],
        }
    )
    main = TableResource(
        data=[["id", "parent", "note"], ["1", "", "a"], ["2", "1", "b"]],
        schema=main_schema,
        name="main",
    )
    link = TableResource(
        data=[["main_id", "tag"], ["1", "x"], ["2", "y"]],
        schema=link_schema,
        name="link",
    )
    source = Package(resources=[main, link])
    source.publish(url, control=formats.SqlControl(roundtrip=True))

    target = Package(url)
    assert_descriptor_roundtrip(source, target)
    assert target.resource_names == ["main", "link"]
    # The foreign keys are also SQL constraints
    fks = read_sql(url, "PRAGMA foreign_key_list(link)")
    assert fks and fks[0][2] == "main"
    fks = read_sql(url, "PRAGMA foreign_key_list(main)")
    assert fks and fks[0][2] == "main"


def test_sql_roundtrip_resource_and_field_order(tmpdir):
    url = make_url(tmpdir)
    schema = Schema.from_descriptor(
        {
            "fields": [
                {"name": "c", "type": "string"},
                {"name": "a", "type": "integer"},
                {"name": "b", "type": "number"},
            ]
        }
    )
    first = TableResource(
        data=[["c", "a", "b"], ["x", "1", "1.5"]], schema=schema, name="zeta"
    )
    second = TableResource(
        data=[["c", "a", "b"], ["y", "2", "2.5"]], schema=schema, name="alpha"
    )
    source = Package(resources=[first, second])
    source.publish(url, control=formats.SqlControl(roundtrip=True))

    target = Package(url)
    assert target.resource_names == ["zeta", "alpha"]
    assert target.resources[0].schema.field_names == ["c", "a", "b"]
    # Column order follows the field order
    columns = read_sql(url, "PRAGMA table_info(zeta)")
    assert [column[1] for column in columns] == ["c", "a", "b"]


def test_sql_roundtrip_row_order_with_integer_primary_key(tmpdir):
    url = make_url(tmpdir)
    schema = Schema.from_descriptor(
        {
            "fields": [
                {"name": "id", "type": "integer"},
                {"name": "value", "type": "string"},
            ],
            "primaryKey": ["id"],
        }
    )
    resource = TableResource(
        data=[["id", "value"], ["50", "a"], ["10", "b"], ["30", "c"]],
        schema=schema,
        name="t",
    )
    source = Package(resources=[resource])
    source.publish(url, control=formats.SqlControl(roundtrip=True))
    rows = Package(url).resources[0].read_rows()
    assert [row["id"] for row in rows] == [50, 10, 30]


def test_sql_roundtrip_empty_package(tmpdir):
    url = make_url(tmpdir)
    source = Package(resources=[])
    source.publish(url, control=formats.SqlControl(roundtrip=True))
    target = Package(url)
    assert target.to_descriptor() == {"resources": []}


def test_sql_roundtrip_field_named_rowid(tmpdir):
    url = make_url(tmpdir)
    schema = Schema.from_descriptor(
        {
            "fields": [
                {"name": "rowid", "type": "integer"},
                {"name": "value", "type": "string"},
            ]
        }
    )
    resource = TableResource(
        data=[["rowid", "value"], ["1", "a"], ["2", "b"]], schema=schema, name="t"
    )
    source = Package(resources=[resource])
    source.publish(url, control=formats.SqlControl(roundtrip=True))
    execute_sql(url, "INSERT INTO t (rowid, value) VALUES (99, 'ext')")
    rows = Package(url).resources[0].read_rows()
    assert [dict(row) for row in rows] == [
        {"rowid": 1, "value": "a"},
        {"rowid": 2, "value": "b"},
        {"rowid": 99, "value": "ext"},
    ]


def test_sql_roundtrip_direct_resource_read(tmpdir):
    url = make_url(tmpdir)
    schema = Schema.from_descriptor({"fields": [{"name": "num", "type": "number"}]})
    resource = TableResource(data=[["num"], ["0.1"]], schema=schema, name="t")
    Package(resources=[resource]).publish(url, control=formats.SqlControl(roundtrip=True))

    # A resource can also be read directly with a roundtrip dialect
    resource = Resource(path=url, control=formats.SqlControl(table="t", roundtrip=True))
    rows = resource.read_rows()
    assert str(rows[0]["num"]) == "0.1"

    # Reading a table missing from the roundtrip metadata raises
    resource = Resource(
        path=url, control=formats.SqlControl(table="missing", roundtrip=True)
    )
    with pytest.raises(FrictionlessException):
        resource.read_rows()


# Roundtrip: external changes


def test_sql_roundtrip_external_insert(tmpdir):
    url = make_url(tmpdir)
    schema = Schema.from_descriptor(
        {
            "fields": [
                {"name": "id", "type": "integer"},
                {"name": "num", "type": "number"},
                {"name": "dt", "type": "datetime"},
                {"name": "tm", "type": "time"},
                {"name": "name", "type": "string"},
            ]
        }
    )
    resource = TableResource(
        data=[
            ["id", "num", "dt", "tm", "name"],
            ["1", "1.5", "2020-01-01T00:00:00", "01:02:03", "a"],
        ],
        schema=schema,
        name="t",
    )
    source = Package(resources=[resource])
    source.publish(url, control=formats.SqlControl(roundtrip=True))
    execute_sql(
        url,
        "INSERT INTO t (id, num, dt, tm, name) VALUES "
        "(2, 2.75, '2021-05-06 07:08:09.000000', '04:05:06.000000', 'b')",
        "INSERT INTO t (id, num, dt, tm, name) VALUES "
        "(3, 100, '2022-01-01 00:00:00.000000', '00:00:01.000000', 'c')",
    )

    rows = Package(url).resources[0].read_rows()
    # Appended after the published rows in insertion order
    assert [row["id"] for row in rows] == [1, 2, 3]
    # Values are interpreted as seen in SQL
    assert rows[1]["num"] == Decimal("2.75") and isinstance(rows[1]["num"], Decimal)
    assert rows[2]["num"] == Decimal("100.0")
    assert rows[1]["dt"] == datetime(2021, 5, 6, 7, 8, 9)
    assert rows[1]["dt"].tzinfo is None
    assert rows[1]["tm"] == time(4, 5, 6)
    assert rows[1]["tm"].tzinfo is None
    # The published row is still exact
    assert str(rows[0]["num"]) == "1.5"


def test_sql_roundtrip_external_update(tmpdir):
    url = make_url(tmpdir)
    schema = Schema.from_descriptor(
        {
            "fields": [
                {"name": "id", "type": "integer"},
                {"name": "num", "type": "number"},
                {"name": "dt", "type": "datetime"},
            ]
        }
    )
    resource = TableResource(
        data=[
            ["id", "num", "dt"],
            ["1", "12345678901234567890.125", "2020-01-02T03:04:05+08:00"],
            ["2", "0.1", "2020-01-02T03:04:05"],
        ],
        schema=schema,
        name="t",
    )
    source = Package(resources=[resource])
    source.publish(url, control=formats.SqlControl(roundtrip=True))
    execute_sql(
        url,
        "UPDATE t SET num = 9.25 WHERE id = 1",
        "UPDATE t SET dt = '2022-03-04 05:06:07.000000' WHERE id = 2",
    )

    rows = Package(url).resources[0].read_rows()
    # Updated cells are interpreted from SQL
    assert rows[0]["num"] == Decimal("9.25")
    assert rows[1]["dt"] == datetime(2022, 3, 4, 5, 6, 7)
    assert rows[1]["dt"].tzinfo is None
    # Untouched cells are restored exactly
    assert str(rows[0]["dt"]) == "2020-01-02 03:04:05+08:00"
    assert rows[0]["dt"].utcoffset() == timedelta(hours=8)
    assert str(rows[1]["num"]) == "0.1"


def test_sql_roundtrip_external_update_to_same_value_is_unchanged(tmpdir):
    url = make_url(tmpdir)
    schema = Schema.from_descriptor({"fields": [{"name": "num", "type": "number"}]})
    resource = TableResource(
        data=[["num"], ["12345678901234567890.125"]], schema=schema, name="t"
    )
    source = Package(resources=[resource])
    source.publish(url, control=formats.SqlControl(roundtrip=True))
    # Rewriting the same SQL value does not count as a change
    execute_sql(url, "UPDATE t SET num = 1.2345678901234567e+19")
    rows = Package(url).resources[0].read_rows()
    assert str(rows[0]["num"]) == "12345678901234567890.125"


def test_sql_roundtrip_external_delete(tmpdir):
    url = make_url(tmpdir)
    schema = Schema.from_descriptor({"fields": [{"name": "id", "type": "integer"}]})
    resource = TableResource(data=[["id"], ["1"], ["2"], ["3"]], schema=schema, name="t")
    source = Package(resources=[resource])
    source.publish(url, control=formats.SqlControl(roundtrip=True))
    execute_sql(url, "DELETE FROM t WHERE id = 2")
    rows = Package(url).resources[0].read_rows()
    assert [row["id"] for row in rows] == [1, 3]


def test_sql_roundtrip_external_values_of_native_types(tmpdir):
    url = make_url(tmpdir)
    schema = Schema.from_descriptor(
        {
            "fields": [
                {"name": "integer", "type": "integer"},
                {"name": "boolean", "type": "boolean"},
                {"name": "date", "type": "date"},
                {"name": "time", "type": "time"},
                {"name": "year", "type": "year"},
                {"name": "text", "type": "string"},
            ]
        }
    )
    resource = TableResource(
        data=[
            ["integer", "boolean", "date", "time", "year", "text"],
            ["1", "true", "2015-01-01", "03:00:00", "2015", "a"],
        ],
        schema=schema,
        name="t",
    )
    source = Package(resources=[resource])
    source.publish(url, control=formats.SqlControl(roundtrip=True))
    execute_sql(
        url,
        "INSERT INTO t (integer, boolean, date, time, year, text) VALUES "
        "(2, 0, '2020-02-03', '04:05:06.000000', 2021, 'b')",
    )
    rows = Package(url).resources[0].read_rows()
    assert dict(rows[1]) == {
        "integer": 2,
        "boolean": False,
        "date": date(2020, 2, 3),
        "time": time(4, 5, 6),
        "year": 2021,
        "text": "b",
    }
    # Unparseable external values surface as validation type errors
    execute_sql(url, "UPDATE t SET date = 'not-a-date' WHERE integer = 1")
    resource = Package(url).resources[0]
    report = resource.validate()
    assert not report.valid
    error_types = [error.type for task in report.tasks for error in task.errors]
    assert "type-error" in error_types


def test_sql_roundtrip_external_invalid_values_are_reported_by_validation(tmpdir):
    url = make_url(tmpdir)
    schema = Schema.from_descriptor(
        {
            "fields": [
                {"name": "id", "type": "integer"},
                {"name": "label", "type": "string", "constraints": {"enum": ["a", "b"]}},
                {"name": "num", "type": "number"},
            ]
        }
    )
    resource = TableResource(
        data=[["id", "label", "num"], ["1", "a", "5"]], schema=schema, name="t"
    )
    source = Package(resources=[resource])
    source.publish(url, control=formats.SqlControl(roundtrip=True))
    execute_sql(
        url,
        "UPDATE t SET label = 'zzz' WHERE id = 1",
        "INSERT INTO t (id, label, num) VALUES (2, 'b', 'notanumber')",
    )

    # Reading does not raise
    resource = Package(url).resources[0]
    rows = resource.read_rows()
    assert rows[0]["label"] == "zzz"
    # The problems are reported by the regular validation
    report = resource.validate()
    assert not report.valid
    error_types = [error.type for task in report.tasks for error in task.errors]
    assert "constraint-error" in error_types
    assert "type-error" in error_types


def test_sql_roundtrip_unrelated_tables_are_ignored(tmpdir):
    url = make_url(tmpdir)
    execute_sql(url, "CREATE TABLE preexisting (id INTEGER)")
    schema = Schema.from_descriptor({"fields": [{"name": "id", "type": "integer"}]})
    resource = TableResource(data=[["id"], ["1"]], schema=schema, name="t")
    source = Package(resources=[resource])
    source.publish(url, control=formats.SqlControl(roundtrip=True))
    execute_sql(url, "CREATE TABLE created_later (id INTEGER)")

    target = Package(url)
    assert target.resource_names == ["t"]


# Roundtrip: structure changes


def test_sql_roundtrip_dropped_table_error(tmpdir):
    url = make_url(tmpdir)
    resource = TableResource(
        data=[["id"], ["1"]],
        schema=Schema.from_descriptor({"fields": [{"name": "id", "type": "integer"}]}),
        name="myresource",
    )
    Package(resources=[resource]).publish(url, control=formats.SqlControl(roundtrip=True))
    execute_sql(url, "DROP TABLE myresource")
    with pytest.raises(FrictionlessException) as excinfo:
        Package(url)
    assert "myresource" in str(excinfo.value)


def test_sql_roundtrip_added_column_error(tmpdir):
    url = make_url(tmpdir)
    resource = TableResource(
        data=[["id"], ["1"]],
        schema=Schema.from_descriptor({"fields": [{"name": "id", "type": "integer"}]}),
        name="t",
    )
    Package(resources=[resource]).publish(url, control=formats.SqlControl(roundtrip=True))
    execute_sql(url, "ALTER TABLE t ADD COLUMN extra TEXT")
    with pytest.raises(FrictionlessException) as excinfo:
        Package(url)
    assert "t" in str(excinfo.value)
    assert "extra" in str(excinfo.value)


def test_sql_roundtrip_dropped_column_error(tmpdir):
    url = make_url(tmpdir)
    schema = Schema.from_descriptor(
        {
            "fields": [
                {"name": "id", "type": "integer"},
                {"name": "name", "type": "string"},
            ]
        }
    )
    resource = TableResource(data=[["id", "name"], ["1", "a"]], schema=schema, name="t")
    Package(resources=[resource]).publish(url, control=formats.SqlControl(roundtrip=True))
    execute_sql(url, "ALTER TABLE t DROP COLUMN name")
    with pytest.raises(FrictionlessException) as excinfo:
        Package(url)
    assert "t" in str(excinfo.value)
    assert "name" in str(excinfo.value)


def test_sql_roundtrip_renamed_column_error(tmpdir):
    url = make_url(tmpdir)
    schema = Schema.from_descriptor(
        {
            "fields": [
                {"name": "id", "type": "integer"},
                {"name": "name", "type": "string"},
            ]
        }
    )
    resource = TableResource(data=[["id", "name"], ["1", "a"]], schema=schema, name="t")
    Package(resources=[resource]).publish(url, control=formats.SqlControl(roundtrip=True))
    execute_sql(url, "ALTER TABLE t RENAME COLUMN name TO label")
    with pytest.raises(FrictionlessException) as excinfo:
        Package(url)
    assert "t" in str(excinfo.value)
    assert "name" in str(excinfo.value)


def test_sql_roundtrip_retyped_column_error(tmpdir):
    url = make_url(tmpdir)
    schema = Schema.from_descriptor(
        {
            "fields": [
                {"name": "id", "type": "integer"},
                {"name": "num", "type": "number"},
            ]
        }
    )
    resource = TableResource(data=[["id", "num"], ["1", "1.5"]], schema=schema, name="t")
    Package(resources=[resource]).publish(url, control=formats.SqlControl(roundtrip=True))
    # Rebuild the table with another column type (SQLite cannot retype in place)
    execute_sql(
        url,
        "ALTER TABLE t RENAME TO t_old",
        "CREATE TABLE t (id INTEGER, num TEXT)",
        "INSERT INTO t SELECT * FROM t_old",
        "DROP TABLE t_old",
    )
    with pytest.raises(FrictionlessException) as excinfo:
        Package(url)
    assert "t" in str(excinfo.value)
    assert "num" in str(excinfo.value)


def test_sql_roundtrip_corrupted_metadata_errors(tmpdir):
    schema = Schema.from_descriptor({"fields": [{"name": "id", "type": "integer"}]})

    # Corrupted descriptor
    url = make_url(tmpdir, "corrupt1.db")
    resource = TableResource(data=[["id"], ["1"]], schema=schema, name="t")
    Package(resources=[resource]).publish(url, control=formats.SqlControl(roundtrip=True))
    execute_sql(
        url, "UPDATE _frictionless_package SET descriptor = 'not json' WHERE id = 1"
    )
    with pytest.raises(FrictionlessException):
        Package(url)

    # Corrupted cell values
    url = make_url(tmpdir, "corrupt2.db")
    resource = TableResource(data=[["id"], ["1"]], schema=schema, name="t")
    Package(resources=[resource]).publish(url, control=formats.SqlControl(roundtrip=True))
    execute_sql(
        url, "UPDATE _frictionless_rows SET cells = 'garbage' WHERE resource = 't'"
    )
    with pytest.raises(FrictionlessException):
        Package(url)

    # Missing metadata table
    url = make_url(tmpdir, "corrupt3.db")
    resource = TableResource(data=[["id"], ["1"]], schema=schema, name="t")
    Package(resources=[resource]).publish(url, control=formats.SqlControl(roundtrip=True))
    execute_sql(url, "DROP TABLE _frictionless_rows")
    with pytest.raises(FrictionlessException):
        Package(url)


# Roundtrip: rejected publishes


def test_sql_roundtrip_not_sqlite_error():
    from frictionless.formats.sql.adapter import SqlAdapter

    adapter = SqlAdapter.__new__(SqlAdapter)
    adapter.engine = Mock()
    adapter.engine.dialect.name = "postgresql"
    adapter.control = formats.SqlControl(roundtrip=True)
    with pytest.raises(FrictionlessException) as excinfo:
        adapter.write_package(Package(resources=[]))
    assert "SQLite" in str(excinfo.value)


def test_sql_roundtrip_existing_table_error(tmpdir):
    url = make_url(tmpdir)
    execute_sql(url, "CREATE TABLE t (id INTEGER)")
    resource = TableResource(
        data=[["id"], ["1"]],
        schema=Schema.from_descriptor({"fields": [{"name": "id", "type": "integer"}]}),
        name="t",
    )
    with pytest.raises(FrictionlessException) as excinfo:
        Package(resources=[resource]).publish(
            url, control=formats.SqlControl(roundtrip=True)
        )
    assert "t" in str(excinfo.value)
    # The database is unchanged
    assert read_sql(url, "SELECT name FROM sqlite_master WHERE type='table'") == [("t",)]


def test_sql_roundtrip_republishing_is_rejected(tmpdir):
    url = make_url(tmpdir)
    schema = Schema.from_descriptor({"fields": [{"name": "id", "type": "integer"}]})
    resource = TableResource(data=[["id"], ["1"]], schema=schema, name="t")
    Package(resources=[resource]).publish(url, control=formats.SqlControl(roundtrip=True))
    resource = TableResource(data=[["id"], ["2"]], schema=schema, name="t")
    with pytest.raises(FrictionlessException):
        Package(resources=[resource]).publish(
            url, control=formats.SqlControl(roundtrip=True)
        )
    # The first publish is intact (no duplicated rows)
    assert len(Package(url).resources[0].read_rows()) == 1


def test_sql_roundtrip_resource_names_case_clash_error(tmpdir):
    url = make_url(tmpdir)
    schema = Schema.from_descriptor({"fields": [{"name": "id", "type": "integer"}]})
    resources = [
        TableResource(data=[["id"], ["1"]], schema=schema, name="Foo"),
        TableResource(data=[["id"], ["2"]], schema=schema, name="foo"),
    ]
    with pytest.raises(FrictionlessException):
        Package(resources=resources).publish(
            url, control=formats.SqlControl(roundtrip=True)
        )
    assert read_sql(url, "SELECT name FROM sqlite_master WHERE type='table'") == []


def test_sql_roundtrip_field_names_case_clash_error(tmpdir):
    url = make_url(tmpdir)
    schema = Schema.from_descriptor(
        {
            "fields": [
                {"name": "ID", "type": "integer"},
                {"name": "id", "type": "integer"},
            ]
        }
    )
    resource = TableResource(data=[["ID", "id"], ["1", "2"]], schema=schema, name="t")
    with pytest.raises(FrictionlessException):
        Package(resources=[resource]).publish(
            url, control=formats.SqlControl(roundtrip=True)
        )
    assert read_sql(url, "SELECT name FROM sqlite_master WHERE type='table'") == []


def test_sql_roundtrip_non_table_resource_error(tmpdir):
    url = make_url(tmpdir)
    resource = Resource(data=b"hello", name="doc", format="text")
    with pytest.raises(FrictionlessException) as excinfo:
        Package(resources=[resource]).publish(
            url, control=formats.SqlControl(roundtrip=True)
        )
    assert "doc" in str(excinfo.value)
    assert read_sql(url, "SELECT name FROM sqlite_master WHERE type='table'") == []


def test_sql_roundtrip_invalid_rows_error(tmpdir):
    url = make_url(tmpdir)
    schema = Schema.from_descriptor(
        {
            "fields": [
                {"name": "id", "type": "integer"},
                {"name": "label", "type": "string", "constraints": {"enum": ["a"]}},
            ]
        }
    )
    resource = TableResource(
        data=[["id", "label"], ["1", "a"], ["notanint", "b"]], schema=schema, name="t"
    )
    with pytest.raises(FrictionlessException) as excinfo:
        Package(resources=[resource]).publish(
            url, control=formats.SqlControl(roundtrip=True)
        )
    assert "t" in str(excinfo.value)
    assert read_sql(url, "SELECT name FROM sqlite_master WHERE type='table'") == []


def test_sql_roundtrip_metadata_table_name_clash_error(tmpdir):
    url = make_url(tmpdir)
    resource = TableResource(
        data=[["id"], ["1"]],
        schema=Schema.from_descriptor({"fields": [{"name": "id", "type": "integer"}]}),
        name=settings.ROUNDTRIP_PACKAGE_TABLE,
    )
    with pytest.raises(FrictionlessException):
        Package(resources=[resource]).publish(
            url, control=formats.SqlControl(roundtrip=True)
        )


# Roundtrip: SQL storage


def test_sql_roundtrip_sql_storage_format(tmpdir):
    url = make_url(tmpdir)
    schema = Schema.from_descriptor(
        {
            "fields": [
                {"name": "integer", "type": "integer"},
                {"name": "boolean", "type": "boolean"},
                {"name": "date", "type": "date"},
                {"name": "time", "type": "time"},
                {"name": "year", "type": "year"},
                {"name": "number", "type": "number"},
                {"name": "geopoint", "type": "geopoint"},
                {"name": "array", "type": "array"},
            ]
        }
    )
    resource = TableResource(
        data=[
            ["integer", "boolean", "date", "time", "year", "number", "geopoint", "array"],
            ["1", "true", "2015-01-01", "03:00:00", "2015", "7.5", "30,70", '["a"]'],
        ],
        schema=schema,
        name="t",
    )
    Package(resources=[resource]).publish(url, control=formats.SqlControl(roundtrip=True))
    columns = read_sql(url, "PRAGMA table_info(t)")
    types = {column[1]: column[2] for column in columns}
    # Native column types are kept
    assert types["integer"] == "INTEGER"
    assert types["boolean"] == "BOOLEAN"
    assert types["date"] == "DATE"
    assert types["time"] == "TIME"
    assert types["year"] == "INTEGER"
    # Number is a numeric column
    assert types["number"] == "FLOAT"
    # The other types are stored as the frictionless-written text
    assert types["geopoint"] == "TEXT"
    assert types["array"] == "TEXT"
    row = read_sql(url, "SELECT geopoint, array FROM t")[0]
    assert row == ("30,70", '["a"]')


# Legacy behavior (no roundtrip)


def test_sql_roundtrip_disabled_keeps_legacy_behavior(tmpdir):
    url = make_url(tmpdir)
    source = Package("data/storage/types.json")
    source.publish(url)
    target = Package(url)
    schema = target.resources[0].schema.to_descriptor()
    # The legacy lossy mapping is unchanged
    assert schema["fields"][0] == {"name": "any", "type": "string"}
    assert schema["fields"][8] == {"name": "geopoint", "type": "string"}
    assert schema["fields"][14] == {"name": "year", "type": "integer"}
    # No roundtrip metadata tables are created
    tables = read_sql(url, "SELECT name FROM sqlite_master WHERE type='table'")
    assert tables == [("types",)]


def test_sql_roundtrip_reading_plain_database_is_unchanged(tmpdir):
    url = make_url(tmpdir)
    execute_sql(
        url,
        "CREATE TABLE fruits (uid INTEGER PRIMARY KEY, fruit_name TEXT, calories INTEGER)",
        "INSERT INTO fruits VALUES (1, 'Apples', 200), (2, 'Oranges', 350)",
    )
    target = Package(url)
    assert target.resource_names == ["fruits"]
    assert target.resources[0].read_rows() == [
        {"uid": 1, "fruit_name": "Apples", "calories": 200},
        {"uid": 2, "fruit_name": "Oranges", "calories": 350},
    ]


def test_sql_roundtrip_control_roundtrips_through_dialect(tmpdir):
    control = formats.SqlControl(table="t", roundtrip=True)
    dialect = control.to_dialect().to_descriptor()
    assert dialect == {"sql": {"table": "t", "roundtrip": True}}
    resource = Resource(
        path=make_url(tmpdir),
        control=formats.SqlControl(table="t", roundtrip=True),
    )
    assert formats.SqlControl.from_dialect(resource.dialect).roundtrip is True
