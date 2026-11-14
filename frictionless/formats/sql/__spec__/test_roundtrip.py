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


def execute_sql_in_transaction(url, *statements, commit=True):
    """Execute statements (and optional rollback) in one explicit transaction"""
    path = url.replace("sqlite:///", "")
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("BEGIN")
        for statement in statements:
            if statement == "ROLLBACK":
                conn.execute("ROLLBACK")
                return
            conn.execute(statement)
        if commit:
            conn.execute("COMMIT")
        else:
            conn.execute("ROLLBACK")
    finally:
        conn.close()


def read_ids_and_amounts(url):
    rows = Package(url).resources[0].read_rows()
    return [(row["id"], str(row["amount"])) for row in rows]


def make_items(tmpdir, name, *, primary_key=True, amount="number"):
    schema = {
        "fields": [
            {"name": "id", "type": "integer"},
            {"name": "amount", "type": amount},
        ]
    }
    if primary_key:
        schema["primaryKey"] = ["id"]
    url = make_url(tmpdir, name)
    resource = TableResource(
        data=[["id", "amount"], ["1", "10.10"], ["2", "20.20"]],
        schema=Schema.from_descriptor(schema),
        name="items",
    )
    Package(resources=[resource]).publish(url, control=formats.SqlControl(roundtrip=True))
    return url, resource


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


# Roundtrip: row identity and order under external changes
#
# The tests below reproduce the scenarios in which the SQLite rowid alone is
# not a stable row identity: an INTEGER PRIMARY KEY update moves the rowid,
# a DELETE followed by an INSERT may reuse it, and externally inserted rows
# must be ordered by their insertion order rather than by their rowid.


def test_sql_roundtrip_external_insert_order_with_integer_primary_key(tmpdir):
    url, _ = make_items(tmpdir, "insert_order.db")
    execute_sql_in_transaction(
        url,
        "INSERT INTO items (id, amount) VALUES (20, 20)",
        "INSERT INTO items (id, amount) VALUES (10, 10)",
    )
    # Published rows keep their order; new ones follow in insertion order,
    # regardless of their (integer primary key) rowid / id value
    assert read_ids_and_amounts(url) == [
        (1, "10.10"),
        (2, "20.20"),
        (20, "20.0"),
        (10, "10.0"),
    ]


def test_sql_roundtrip_external_primary_key_update_keeps_position_and_values(tmpdir):
    url, _ = make_items(tmpdir, "pk_update.db")
    execute_sql(url, "UPDATE items SET id = 100 WHERE id = 1")
    # The updated row stays first and keeps its exact published value
    assert read_ids_and_amounts(url) == [(100, "10.10"), (2, "20.20")]


def test_sql_roundtrip_delete_then_insert_does_not_inherit_exact_values(tmpdir):
    url, _ = make_items(tmpdir, "delete_insert.db", primary_key=False)
    execute_sql(
        url,
        "DELETE FROM items WHERE id = 2",
        "INSERT INTO items (id, amount) VALUES (3, 20.2)",
    )
    # The new row reuses the freed rowid but is a new row: it must be
    # decoded from SQL (shortest decimal repr) at the end of the row order
    assert read_ids_and_amounts(url) == [(1, "10.10"), (3, "20.2")]


def test_sql_roundtrip_delete_then_insert_reusing_primary_key_is_a_new_row(tmpdir):
    url, _ = make_items(tmpdir, "delete_insert_pk.db")
    execute_sql(
        url,
        "DELETE FROM items WHERE id = 1",
        "INSERT INTO items (id, amount) VALUES (1, 1.1)",
    )
    assert read_ids_and_amounts(url) == [(2, "20.20"), (1, "1.1")]


def test_sql_roundtrip_update_primary_key_then_delete_and_reinsert(tmpdir):
    url, _ = make_items(tmpdir, "pk_move_delete.db")
    execute_sql(
        url,
        "UPDATE items SET id = 100 WHERE id = 1",
        "DELETE FROM items WHERE id = 100",
        "INSERT INTO items (id, amount) VALUES (1, 99.9)",
    )
    assert read_ids_and_amounts(url) == [(2, "20.20"), (1, "99.9")]


def test_sql_roundtrip_new_row_updated_to_published_value_is_not_restored(tmpdir):
    url, _ = make_items(tmpdir, "inherit.db", primary_key=False)
    execute_sql(
        url,
        "DELETE FROM items WHERE id = 2",
        "INSERT INTO items (id, amount) VALUES (3, 20.2)",
        # Rewriting the new row to the SQL form of the deleted published
        # value must not inherit the deleted row's exact value
        "UPDATE items SET amount = 20.2 WHERE id = 3",
    )
    assert read_ids_and_amounts(url) == [(1, "10.10"), (3, "20.2")]


def test_sql_roundtrip_update_to_same_value_then_back_restores_exact(tmpdir):
    url, _ = make_items(tmpdir, "same_value.db")
    execute_sql(
        url,
        "UPDATE items SET amount = 9.25 WHERE id = 1",
        "UPDATE items SET amount = 10.1 WHERE id = 1",
    )
    # The row still belongs to the publish and its current SQL value equals
    # the published one, so the exact value is restored
    assert read_ids_and_amounts(url) == [(1, "10.10"), (2, "20.20")]


def test_sql_roundtrip_shift_primary_keys_in_one_statement(tmpdir):
    url, _ = make_items(tmpdir, "pk_shift.db")
    execute_sql(url, "UPDATE items SET id = id + 100")
    assert read_ids_and_amounts(url) == [(101, "10.10"), (102, "20.20")]


def test_sql_roundtrip_multiple_published_rows_external_single_statement(tmpdir):
    url = make_url(tmpdir, "multi.db")
    schema = Schema.from_descriptor(
        {
            "fields": [
                {"name": "id", "type": "integer"},
                {"name": "amount", "type": "number"},
            ],
            "primaryKey": ["id"],
        }
    )
    resource = TableResource(
        data=[["id", "amount"], ["0", "0.10"], ["-5", "5.50"], ["-1", "1.10"]],
        schema=schema,
        name="items",
    )
    Package(resources=[resource]).publish(url, control=formats.SqlControl(roundtrip=True))
    execute_sql(
        url,
        "INSERT INTO items (id, amount) VALUES (100, 100.1), (50, 50.5), (75, 75.75)",
    )
    assert read_ids_and_amounts(url) == [
        (0, "0.10"),
        (-5, "5.50"),
        (-1, "1.10"),
        (100, "100.1"),
        (50, "50.5"),
        (75, "75.75"),
    ]


def test_sql_roundtrip_integer_primary_key_named_rowid(tmpdir):
    url = make_url(tmpdir, "pk_named_rowid.db")
    schema = Schema.from_descriptor(
        {
            "fields": [
                {"name": "rowid", "type": "integer"},
                {"name": "amount", "type": "number"},
            ],
            "primaryKey": ["rowid"],
        }
    )
    resource = TableResource(
        data=[["rowid", "amount"], ["1", "1.10"], ["2", "2.20"]],
        schema=schema,
        name="items",
    )
    Package(resources=[resource]).publish(url, control=formats.SqlControl(roundtrip=True))
    execute_sql(
        url,
        "UPDATE items SET rowid = 10 WHERE rowid = 2",
        "INSERT INTO items (rowid, amount) VALUES (3, 3.30)",
    )
    rows = Package(url).resources[0].read_rows()
    assert [(row["rowid"], str(row["amount"])) for row in rows] == [
        (1, "1.10"),
        (10, "2.20"),
        (3, "3.3"),
    ]


def test_sql_roundtrip_text_primary_key(tmpdir):
    url = make_url(tmpdir, "text_pk.db")
    schema = Schema.from_descriptor(
        {
            "fields": [
                {"name": "id", "type": "string"},
                {"name": "amount", "type": "number"},
            ],
            "primaryKey": ["id"],
        }
    )
    resource = TableResource(
        data=[["id", "amount"], ["b", "1.10"], ["a", "2.20"]],
        schema=schema,
        name="items",
    )
    Package(resources=[resource]).publish(url, control=formats.SqlControl(roundtrip=True))
    execute_sql(
        url,
        "UPDATE items SET id = 'c' WHERE id = 'b'",
        "INSERT INTO items VALUES ('d', 3.3)",
    )
    assert read_ids_and_amounts(url) == [
        ("c", "1.10"),
        ("a", "2.20"),
        ("d", "3.3"),
    ]


def test_sql_roundtrip_composite_primary_key(tmpdir):
    url = make_url(tmpdir, "composite_pk.db")
    schema = Schema.from_descriptor(
        {
            "fields": [
                {"name": "a", "type": "integer"},
                {"name": "b", "type": "string"},
                {"name": "amount", "type": "number"},
            ],
            "primaryKey": ["a", "b"],
        }
    )
    resource = TableResource(
        data=[
            ["a", "b", "amount"],
            ["1", "x", "1.10"],
            ["1", "y", "2.20"],
            ["2", "z", "3.30"],
        ],
        schema=schema,
        name="items",
    )
    Package(resources=[resource]).publish(url, control=formats.SqlControl(roundtrip=True))
    execute_sql(
        url,
        "UPDATE items SET b = 'w' WHERE a = 1 AND b = 'x'",
        "INSERT INTO items VALUES (0, 'q', 9.9)",
    )
    rows = Package(url).resources[0].read_rows()
    assert [(row["a"], row["b"], str(row["amount"])) for row in rows] == [
        (1, "w", "1.10"),
        (1, "y", "2.20"),
        (2, "z", "3.30"),
        (0, "q", "9.9"),
    ]


def test_sql_roundtrip_no_primary_key_rowid_aliases_are_not_confused(tmpdir):
    # Only "rowid" is shadowed: the identity uses "_rowid_"
    url = make_url(tmpdir, "shadow_rowid.db")
    schema = Schema.from_descriptor(
        {
            "fields": [
                {"name": "rowid", "type": "string"},
                {"name": "amount", "type": "number"},
            ]
        }
    )
    resource = TableResource(
        data=[["rowid", "amount"], ["a", "1.10"], ["b", "2.20"]],
        schema=schema,
        name="items",
    )
    Package(resources=[resource]).publish(url, control=formats.SqlControl(roundtrip=True))
    execute_sql(
        url,
        "INSERT INTO items VALUES ('c', 3.3)",
        "UPDATE items SET amount = 9.9 WHERE rowid = 'a'",
    )
    rows = Package(url).resources[0].read_rows()
    assert [(row["rowid"], str(row["amount"])) for row in rows] == [
        ("a", "9.9"),
        ("b", "2.20"),
        ("c", "3.3"),
    ]


def test_sql_roundtrip_no_primary_key_two_rowid_aliases_are_not_confused(tmpdir):
    # "rowid" and "_rowid_" are shadowed: the identity uses "oid"
    url = make_url(tmpdir, "shadow_two.db")
    schema = Schema.from_descriptor(
        {
            "fields": [
                {"name": "rowid", "type": "string"},
                {"name": "_rowid_", "type": "string"},
                {"name": "amount", "type": "number"},
            ]
        }
    )
    resource = TableResource(
        data=[["rowid", "_rowid_", "amount"], ["a", "x", "1.10"], ["b", "y", "2.20"]],
        schema=schema,
        name="items",
    )
    Package(resources=[resource]).publish(url, control=formats.SqlControl(roundtrip=True))
    execute_sql(
        url,
        "INSERT INTO items VALUES ('c', 'z', 3.3)",
        "UPDATE items SET amount = 9.9 WHERE rowid = 'a'",
    )
    rows = Package(url).resources[0].read_rows()
    assert [(row["rowid"], row["_rowid_"], str(row["amount"])) for row in rows] == [
        ("a", "x", "9.9"),
        ("b", "y", "2.20"),
        ("c", "z", "3.3"),
    ]


def test_sql_roundtrip_all_rowid_aliases_shadowed_with_primary_key(tmpdir):
    # All aliases are shadowed: the identity falls back to the primary key
    url = make_url(tmpdir, "shadow_all.db")
    schema = Schema.from_descriptor(
        {
            "fields": [
                {"name": "rowid", "type": "string"},
                {"name": "_rowid_", "type": "string"},
                {"name": "oid", "type": "string"},
                {"name": "amount", "type": "number"},
            ],
            "primaryKey": ["oid"],
        }
    )
    resource = TableResource(
        data=[
            ["rowid", "_rowid_", "oid", "amount"],
            ["a", "x", "k1", "1.10"],
            ["b", "y", "k2", "2.20"],
        ],
        schema=schema,
        name="items",
    )
    Package(resources=[resource]).publish(url, control=formats.SqlControl(roundtrip=True))
    execute_sql(
        url,
        "UPDATE items SET amount = 9.55 WHERE oid = 'k1'",
        "UPDATE items SET oid = 'k9' WHERE oid = 'k2'",
        "INSERT INTO items VALUES ('c', 'z', 'k3', 3.3)",
    )
    rows = Package(url).resources[0].read_rows()
    assert [(row["oid"], str(row["amount"])) for row in rows] == [
        ("k1", "9.55"),
        ("k9", "2.20"),
        ("k3", "3.3"),
    ]


def test_sql_roundtrip_all_rowid_aliases_shadowed_without_primary_key_is_rejected(
    tmpdir,
):
    url = make_url(tmpdir, "shadow_rejected.db")
    schema = Schema.from_descriptor(
        {
            "fields": [
                {"name": "rowid", "type": "string"},
                {"name": "_rowid_", "type": "string"},
                {"name": "oid", "type": "string"},
                {"name": "amount", "type": "number"},
            ]
        }
    )
    resource = TableResource(
        data=[["rowid", "_rowid_", "oid", "amount"], ["a", "x", "c", "1.10"]],
        schema=schema,
        name="items",
    )
    with pytest.raises(FrictionlessException) as excinfo:
        Package(resources=[resource]).publish(
            url, control=formats.SqlControl(roundtrip=True)
        )
    assert "rowid" in str(excinfo.value)
    assert read_sql(url, "SELECT name FROM sqlite_master WHERE type='table'") == []


def test_sql_roundtrip_transaction_rollback_leaves_no_trace(tmpdir):
    url, _ = make_items(tmpdir, "rollback.db")
    execute_sql_in_transaction(
        url,
        "INSERT INTO items (id, amount) VALUES (30, 30.3)",
        "UPDATE items SET id = 100 WHERE id = 1",
        "DELETE FROM items WHERE id = 2",
        "ROLLBACK",
    )
    assert read_ids_and_amounts(url) == [(1, "10.10"), (2, "20.20")]


def test_sql_roundtrip_transaction_commit_is_consistent(tmpdir):
    url, _ = make_items(tmpdir, "commit.db")
    execute_sql_in_transaction(
        url,
        "INSERT INTO items (id, amount) VALUES (30, 30.3)",
        "INSERT INTO items (id, amount) VALUES (40, 40.4)",
        "UPDATE items SET id = 100 WHERE id = 1",
        "DELETE FROM items WHERE id = 2",
    )
    assert read_ids_and_amounts(url) == [
        (100, "10.10"),
        (30, "30.3"),
        (40, "40.4"),
    ]


def test_sql_roundtrip_reopened_multiple_times_and_from_another_process(tmpdir):
    import os
    import subprocess
    import sys

    import frictionless

    url, _ = make_items(tmpdir, "reopen.db")
    execute_sql(
        url,
        "INSERT INTO items (id, amount) VALUES (30, 30.3)",
        "UPDATE items SET id = 100 WHERE id = 1",
    )
    expected = [(100, "10.10"), (2, "20.20"), (30, "30.3")]
    assert read_ids_and_amounts(url) == expected
    assert read_ids_and_amounts(url) == expected
    code = (
        "from frictionless import Package\n"
        f"rows = Package({url!r}).resources[0].read_rows()\n"
        'print([(row["id"], str(row["amount"])) for row in rows])\n'
    )
    # Start a fresh interpreter to prove the state lives in the database
    env = dict(os.environ)
    root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.dirname(frictionless.__file__)))
    )
    env["PYTHONPATH"] = os.pathsep.join(
        [root] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(tmpdir),
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(expected)


def test_sql_roundtrip_untouched_timezone_survives_external_update(tmpdir):
    url = make_url(tmpdir, "timezone.db")
    schema = Schema.from_descriptor(
        {
            "fields": [
                {"name": "id", "type": "integer"},
                {"name": "dt", "type": "datetime"},
                {"name": "tm", "type": "time"},
                {"name": "amount", "type": "number"},
            ],
            "primaryKey": ["id"],
        }
    )
    resource = TableResource(
        data=[
            ["id", "dt", "tm", "amount"],
            ["1", "2020-01-02T03:04:05+08:00", "03:04:05+08:00", "1.10"],
        ],
        schema=schema,
        name="items",
    )
    Package(resources=[resource]).publish(url, control=formats.SqlControl(roundtrip=True))
    execute_sql(url, "UPDATE items SET amount = 2.5 WHERE id = 1")
    row = Package(url).resources[0].read_rows()[0]
    assert str(row["dt"]) == "2020-01-02 03:04:05+08:00"
    assert row["dt"].utcoffset() == timedelta(hours=8)
    assert str(row["tm"]) == "03:04:05+08:00"
    assert row["tm"].utcoffset() == timedelta(hours=8)


def test_sql_roundtrip_external_insert_update_to_published_value_is_not_inherited(
    tmpdir,
):
    url, _ = make_items(tmpdir, "new_naive.db")
    execute_sql_in_transaction(
        url,
        "INSERT INTO items (id, amount) VALUES (3, 20.2)",
        # Same SQL value as the published row id=2: the new row must still
        # be decoded from SQL (shortest repr), not inherit its exact value
        "UPDATE items SET amount = 20.2 WHERE id = 3",
    )
    rows = Package(url).resources[0].read_rows()
    assert str(rows[2]["amount"]) == "20.2"


def run_random_sql_operations(url, *, rows, rng, steps=60):
    """Apply random SQL operations, maintaining an independent oracle

    The oracle tracks the row identities, their position and their expected
    values using only the documented rules (published rows keep their exact
    values while their SQL value is unchanged; new rows are decoded from
    SQL). It never calls the roundtrip reader to build the expectations.
    """
    path = url.replace("sqlite:///", "")
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("BEGIN")
    next_id = 1000
    deleted_ids = []
    try:
        for _ in range(steps):
            operation = rng.choice(
                ["insert", "insert", "update_amount", "update_id", "delete"]
            )
            if operation == "insert" or not rows:
                # Sometimes reuse a deleted primary key: it must be a new
                # row at the end of the order, without the old exact value
                if deleted_ids and rng.random() < 0.5:
                    new_id = deleted_ids.pop(rng.randrange(len(deleted_ids)))
                else:
                    next_id += 1
                    new_id = next_id
                amount = rng.choice([0.1, 2.5, 3.14, 10.1, 20.2, 20.0, 100.0, 7, 42])
                conn.execute(
                    "INSERT INTO items (id, amount) VALUES (?, ?)", (new_id, amount)
                )
                rows.append({"id": new_id, "stored": float(amount), "exact": None})
                continue
            row = rng.choice(rows)
            if operation == "delete":
                conn.execute("DELETE FROM items WHERE id = ?", (row["id"],))
                rows.remove(row)
                deleted_ids.append(row["id"])
            elif operation == "update_amount":
                amount = rng.choice([0.1, 2.5, 3.14, 10.1, 20.2, 20.0, 100.0, 7, 42])
                conn.execute(
                    "UPDATE items SET amount = ? WHERE id = ?", (amount, row["id"])
                )
                row["stored"] = float(amount)
            elif operation == "update_id":
                next_id += 1
                conn.execute("UPDATE items SET id = ? WHERE id = ?", (next_id, row["id"]))
                row["id"] = next_id
        conn.execute("COMMIT")
    finally:
        conn.close()

    result = []
    for row in rows:
        if row["exact"] is not None and row["stored"] == float(row["exact"]):
            amount = row["exact"]
        else:
            amount = Decimal(repr(row["stored"]))
        result.append((row["id"], amount))
    return result


def assert_read_rows_match(url, expected):
    rows = Package(url).resources[0].read_rows()
    assert [(row["id"], row["amount"]) for row in rows] == expected
    for row, (_, amount) in zip(rows, expected):
        assert isinstance(row["amount"], Decimal)
        assert str(row["amount"]) == str(amount)


def test_sql_roundtrip_random_sql_operations_with_primary_key(tmpdir):
    import random

    url, _ = make_items(tmpdir, "random_pk.db")
    rng = random.Random(20240918)
    rows = [
        {"id": 1, "stored": 10.1, "exact": Decimal("10.10")},
        {"id": 2, "stored": 20.2, "exact": Decimal("20.20")},
    ]
    expected = run_random_sql_operations(url, rows=rows, rng=rng)
    assert_read_rows_match(url, expected)


def test_sql_roundtrip_random_sql_operations_without_primary_key(tmpdir):
    import random

    url, _ = make_items(tmpdir, "random_no_pk.db", primary_key=False)
    rng = random.Random(73192)
    rows = [
        {"id": 1, "stored": 10.1, "exact": Decimal("10.10")},
        {"id": 2, "stored": 20.2, "exact": Decimal("20.20")},
    ]
    expected = run_random_sql_operations(url, rows=rows, rng=rng)
    assert_read_rows_match(url, expected)


def test_sql_roundtrip_random_sql_operations_with_rollbacks(tmpdir):
    import random

    url, _ = make_items(tmpdir, "random_rollback.db")
    rng = random.Random(991)
    rows = [
        {"id": 1, "stored": 10.1, "exact": Decimal("10.10")},
        {"id": 2, "stored": 20.2, "exact": Decimal("20.20")},
    ]
    path = url.replace("sqlite:///", "")
    conn = sqlite3.connect(path, isolation_level=None)
    next_id = 2000
    try:
        for _ in range(30):
            conn.execute("BEGIN")
            rollback = rng.random() < 0.5
            local_rows = [dict(row) for row in rows]
            for _ in range(rng.randint(1, 4)):
                row = rng.choice(local_rows)
                if rng.random() < 0.5:
                    next_id += 1
                    amount = rng.choice([1.5, 10.1, 20.2, 33.0])
                    conn.execute(
                        "INSERT INTO items (id, amount) VALUES (?, ?)",
                        (next_id, amount),
                    )
                    local_rows.append(
                        {"id": next_id, "stored": float(amount), "exact": None}
                    )
                else:
                    next_id += 1
                    conn.execute(
                        "UPDATE items SET id = ? WHERE id = ?",
                        (next_id, row["id"]),
                    )
                    row["id"] = next_id
            conn.execute("ROLLBACK" if rollback else "COMMIT")
            if not rollback:
                rows = local_rows
    finally:
        conn.close()

    expected = [
        (
            row["id"],
            row["exact"]
            if row["exact"] is not None and row["stored"] == float(row["exact"])
            else Decimal(repr(row["stored"])),
        )
        for row in rows
    ]
    assert_read_rows_match(url, expected)


def test_sql_roundtrip_legacy_metadata_without_external_changes(tmpdir):
    """A database published by an older version still reads back exactly

    Legacy metadata identified published rows by their rowid only and had
    no triggers. Such a database cannot track rows changed by other tools,
    but an unmodified one must still restore its exact values.
    """
    url, _ = make_items(tmpdir, "legacy.db")
    path = url.replace("sqlite:///", "")
    conn = sqlite3.connect(path)
    try:
        rowids = conn.execute("SELECT rowid, id FROM items ORDER BY rowid").fetchall()
        cells = conn.execute(
            "SELECT seq, cells FROM _frictionless_rows ORDER BY seq"
        ).fetchall()
        conn.execute("DROP TRIGGER IF EXISTS _frictionless_items_insert")
        conn.execute("DROP TRIGGER IF EXISTS _frictionless_items_update")
        conn.execute("DROP TRIGGER IF EXISTS _frictionless_items_delete")
        conn.execute("DROP TABLE _frictionless_rows")
        conn.execute(
            "CREATE TABLE _frictionless_rows ("
            "resource TEXT NOT NULL, rowid INTEGER NOT NULL, "
            "seq INTEGER NOT NULL, cells TEXT NOT NULL, "
            "PRIMARY KEY (resource, rowid))"
        )
        by_seq = {seq: text for seq, text in cells}
        conn.executemany(
            "INSERT INTO _frictionless_rows (resource, rowid, seq, cells) "
            "VALUES ('items', ?, ?, ?)",
            [
                (rid, index + 1, by_seq[index + 1])
                for index, (rid, _) in enumerate(rowids)
            ],
        )
        conn.commit()
    finally:
        conn.close()
    # No triggers exist any more: the legacy layout is detected and read
    assert read_sql(url, "SELECT name FROM sqlite_master WHERE type='trigger'") == []
    assert read_ids_and_amounts(url) == [(1, "10.10"), (2, "20.20")]


def test_sql_roundtrip_legacy_metadata_cannot_track_external_identity_changes(
    tmpdir,
):
    """The documented legacy limitation: row identity changes are untracked"""
    url, _ = make_items(tmpdir, "legacy_changed.db")
    path = url.replace("sqlite:///", "")
    conn = sqlite3.connect(path)
    try:
        rowids = conn.execute("SELECT rowid, id FROM items ORDER BY rowid").fetchall()
        cells = conn.execute(
            "SELECT seq, cells FROM _frictionless_rows ORDER BY seq"
        ).fetchall()
        conn.execute("DROP TRIGGER IF EXISTS _frictionless_items_insert")
        conn.execute("DROP TRIGGER IF EXISTS _frictionless_items_update")
        conn.execute("DROP TRIGGER IF EXISTS _frictionless_items_delete")
        conn.execute("DROP TABLE _frictionless_rows")
        conn.execute(
            "CREATE TABLE _frictionless_rows ("
            "resource TEXT NOT NULL, rowid INTEGER NOT NULL, "
            "seq INTEGER NOT NULL, cells TEXT NOT NULL, "
            "PRIMARY KEY (resource, rowid))"
        )
        by_seq = {seq: text for seq, text in cells}
        conn.executemany(
            "INSERT INTO _frictionless_rows (resource, rowid, seq, cells) "
            "VALUES ('items', ?, ?, ?)",
            [
                (rid, index + 1, by_seq[index + 1])
                for index, (rid, _) in enumerate(rowids)
            ],
        )
        conn.commit()
    finally:
        conn.close()
    # An old database has no operation history, so updating the integer
    # primary key cannot be distinguished from a delete plus an insert: the
    # row moves to the appended rows and loses its exact value (10.1 rather
    # than 10.10). This is the documented limitation of legacy databases.
    execute_sql(url, "UPDATE items SET id = 100 WHERE id = 1")
    rows = Package(url).resources[0].read_rows()
    assert [(row["id"], str(row["amount"])) for row in rows] == [
        (2, "20.20"),
        (100, "10.1"),
    ]


def test_sql_roundtrip_identical_rows_stay_distinct(tmpdir):
    url = make_url(tmpdir, "identical.db")
    schema = Schema.from_descriptor({"fields": [{"name": "amount", "type": "number"}]})
    resource = TableResource(
        data=[["amount"], ["1.10"], ["1.10"]], schema=schema, name="items"
    )
    Package(resources=[resource]).publish(url, control=formats.SqlControl(roundtrip=True))
    execute_sql(
        url,
        "INSERT INTO items (amount) VALUES (1.10)",
    )
    rows = Package(url).resources[0].read_rows()
    assert [str(row["amount"]) for row in rows] == ["1.10", "1.10", "1.1"]


def test_sql_roundtrip_each_resource_has_its_own_history(tmpdir):
    url = make_url(tmpdir, "two_resources.db")
    schema = Schema.from_descriptor(
        {
            "fields": [
                {"name": "id", "type": "integer"},
                {"name": "amount", "type": "number"},
            ],
            "primaryKey": ["id"],
        }
    )
    first = TableResource(
        data=[["id", "amount"], ["1", "1.10"]], schema=schema, name="first"
    )
    second = TableResource(
        data=[["id", "amount"], ["1", "2.20"]], schema=schema, name="second"
    )
    Package(resources=[first, second]).publish(
        url, control=formats.SqlControl(roundtrip=True)
    )
    execute_sql(
        url,
        "UPDATE first SET id = 10 WHERE id = 1",
        "INSERT INTO second (id, amount) VALUES (2, 3.3)",
    )
    target = Package(url)
    assert [
        (row["id"], str(row["amount"])) for row in target.resources[0].read_rows()
    ] == [(10, "1.10")]
    assert [
        (row["id"], str(row["amount"])) for row in target.resources[1].read_rows()
    ] == [
        (1, "2.20"),
        (2, "3.3"),
    ]


def test_sql_roundtrip_existing_history_index_clash_is_rejected(tmpdir):
    url = make_url(tmpdir, "index_clash.db")
    execute_sql(
        url,
        "CREATE TABLE preexisting (id INTEGER)",
        "CREATE INDEX _frictionless_rows_seq ON preexisting (id)",
    )
    schema = Schema.from_descriptor({"fields": [{"name": "id", "type": "integer"}]})
    resource = TableResource(data=[["id"], ["1"]], schema=schema, name="t")
    with pytest.raises(FrictionlessException) as excinfo:
        Package(resources=[resource]).publish(
            url, control=formats.SqlControl(roundtrip=True)
        )
    assert "_frictionless_rows_seq" in str(excinfo.value)
    # The database is unchanged apart from the pre-existing objects
    assert read_sql(url, "SELECT name FROM sqlite_master WHERE type='table'") == [
        ("preexisting",)
    ]


def test_sql_roundtrip_existing_history_trigger_clash_is_rejected(tmpdir):
    url = make_url(tmpdir, "trigger_clash.db")
    execute_sql(
        url,
        "CREATE TABLE preexisting (id INTEGER)",
        "CREATE TRIGGER _frictionless_t_insert AFTER INSERT ON preexisting "
        "BEGIN SELECT 1; END",
    )
    schema = Schema.from_descriptor({"fields": [{"name": "id", "type": "integer"}]})
    resource = TableResource(data=[["id"], ["1"]], schema=schema, name="t")
    with pytest.raises(FrictionlessException) as excinfo:
        Package(resources=[resource]).publish(
            url, control=formats.SqlControl(roundtrip=True)
        )
    assert "_frictionless_t_insert" in str(excinfo.value)


def test_sql_roundtrip_real_primary_key_identity_is_exact(tmpdir):
    """A REAL primary key (shadowing all rowid aliases) is identified exactly

    SQLite renders two adjacent doubles with the same 15-digit text, so an
    identity built from ``hex`` would collide. The identity uses 17
    significant digits instead.
    """
    url = make_url(tmpdir, "real_pk.db")
    schema = Schema.from_descriptor(
        {
            "fields": [
                {"name": "rowid", "type": "string"},
                {"name": "_rowid_", "type": "string"},
                {"name": "oid", "type": "number"},
                {"name": "amount", "type": "string"},
            ],
            "primaryKey": ["oid"],
        }
    )
    resource = TableResource(
        data=[
            ["rowid", "_rowid_", "oid", "amount"],
            ["a", "x", "0.1", "first"],
            ["b", "y", "0.09999999999999999", "second"],
        ],
        schema=schema,
        name="items",
    )
    Package(resources=[resource]).publish(url, control=formats.SqlControl(roundtrip=True))
    rows = Package(url).resources[0].read_rows()
    # Both rows keep their own exact amount (the identity did not collide)
    assert [(row["oid"], row["amount"]) for row in rows] == [
        (Decimal("0.1"), "first"),
        (Decimal("0.09999999999999999"), "second"),
    ]
