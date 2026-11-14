from __future__ import annotations

import base64
import json
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Dict, Generator, List, cast

from ...exception import FrictionlessException
from ...platform import platform
from ...schema import Schema
from ...system import models
from . import settings

if TYPE_CHECKING:
    from ...package import Package
    from ...resources import TableResource
    from ...schema import Field
    from .adapter import SqlAdapter
    from .control import SqlControl


# This module implements the lossless "roundtrip" SQLite publishing mode.
#
# When a package is published with `formats.SqlControl(roundtrip=True)` the
# adapter, in addition to the regular data tables, creates two metadata
# tables in the same database:
#
# - "_frictionless_package": a single-row table storing the package
#   descriptor (JSON) with the data-location properties removed
# - "_frictionless_rows": one row per published data row storing the rowid,
#   the original row position and the exact cell values (JSON)
#
# Reading the database back with `Package(url)` detects the metadata tables
# and restores the package descriptor and the exact cell values. Cells that
# were externally inserted/updated in the database are interpreted from
# their SQL representation instead (see "decode_storage").


# Metadata


def write_package(adapter: SqlAdapter, package: Package) -> models.PublishResult:
    """Publish a package to SQLite in roundtrip (lossless) mode"""
    sa = platform.sqlalchemy
    check_writable(adapter, package)
    metadata = adapter.metadata

    # Prepare the descriptor to store (without data-location properties)
    descriptor = package.to_descriptor()
    descriptor["resources"] = [
        {
            key: value
            for key, value in resource_descriptor.items()
            if key not in settings.ROUNDTRIP_LOCATION_PROPS
        }
        for resource_descriptor in descriptor.get("resources", [])
    ]

    package_table, rows_table = create_metadata_tables()
    with adapter.engine.begin() as conn:
        # Create data tables
        tables: List[Any] = []
        for resource in package.resources:
            table = adapter.mapper.write_schema(resource.schema, table_name=resource.name)
            table = table.to_metadata(metadata)
            tables.append(table)
        metadata.create_all(conn, tables=tables + [package_table, rows_table])

        # Write rows (in foreign-key dependency order)
        for table in metadata.sorted_tables:
            if package.has_table_resource(table.name):
                resource = package.get_table_resource(table.name)
                write_resource_rows(
                    adapter,
                    resource,
                    table_name=table.name,
                    rows_table=rows_table,
                    conn=conn,
                )

        # Store the descriptor
        conn.execute(
            sa.insert(package_table),
            [{"id": 1, "descriptor": json.dumps(descriptor, ensure_ascii=False)}],
        )

    return models.PublishResult(
        url=adapter.engine.url.render_as_string(hide_password=True),
        context=dict(engine=adapter.engine),
    )


def check_writable(adapter: SqlAdapter, package: Package) -> None:
    """Ensure the package can be published in roundtrip mode

    All the checks run before any table is created so that a rejected
    publish leaves the database untouched.
    """
    dialect = adapter.engine.dialect.name
    if dialect != "sqlite":
        raise FrictionlessException(
            f'roundtrip mode is only supported for SQLite, not for "{dialect}"'
        )

    # Only table resources are supported
    for resource in package.resources:
        if resource.datatype != "table":
            raise FrictionlessException(
                "cannot publish in roundtrip mode: "
                f'resource "{resource.name}" is not a table resource'
            )

    # Resource names must be set and unique case-insensitively
    names: List[str] = []
    for resource in package.resources:
        if not resource.name:
            raise FrictionlessException(
                "cannot publish in roundtrip mode: every resource must have a name"
            )
        names.append(resource.name)
    lowered = [name.lower() for name in names]
    if len(set(lowered)) != len(lowered):
        raise FrictionlessException(
            "cannot publish in roundtrip mode: "
            "resource names must not differ only by case"
        )

    # Resource tables must not exist yet (SQLite names are case-insensitive)
    existing = {name.lower() for name in adapter.metadata.tables}
    existing.update(name.lower() for name in settings.ROUNDTRIP_METADATA_TABLES)
    for name in names:
        if name.lower() in existing:
            raise FrictionlessException(
                "cannot publish in roundtrip mode: "
                f'table "{name}" already exists in the database'
            )

    # The source data must pass schema validation
    for resource in package.resources:
        report = resource.validate()
        if not report.valid:
            note = report.errors[0].note if report.errors else "invalid data"
            raise FrictionlessException(
                "cannot publish in roundtrip mode: "
                f'resource "{resource.name}" has rows that do not pass '
                f"schema validation ({note})"
            )

    # Field names must be unique case-insensitively
    for resource in package.resources:
        if not resource.schema:
            continue
        field_names = [field.name for field in resource.schema.fields]
        lowered = [name.lower() for name in field_names]
        if len(set(lowered)) != len(lowered):
            raise FrictionlessException(
                "cannot publish in roundtrip mode: "
                f'field names in resource "{resource.name}" must not '
                "differ only by case"
            )


def create_metadata_tables() -> Any:
    """Create the roundtrip metadata table objects (unbound)"""
    sa = platform.sqlalchemy
    package_table = sa.Table(
        settings.ROUNDTRIP_PACKAGE_TABLE,
        sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("descriptor", sa.Text, nullable=False),
    )
    rows_table = sa.Table(
        settings.ROUNDTRIP_ROWS_TABLE,
        sa.MetaData(),
        sa.Column("resource", sa.Text, nullable=False),
        sa.Column("rowid", sa.Integer, nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("cells", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("resource", "rowid"),
    )
    return package_table, rows_table


def write_resource_rows(
    adapter: SqlAdapter,
    resource: TableResource,
    *,
    table_name: str,
    rows_table: Any,
    conn: Any,
):
    """Write resource rows to the data table and record the exact cells"""
    sa = platform.sqlalchemy
    schema = resource.schema
    table = adapter.metadata.tables[table_name]

    # SQLite makes a single INTEGER PRIMARY KEY column an alias for the
    # rowid, so the rowid of such rows is the primary key value itself
    alias_field = None
    if len(schema.primary_key) == 1 and schema.has_field(schema.primary_key[0]):
        pk_field = schema.get_field(schema.primary_key[0])
        if adapter.mapper.write_type(pk_field.type) is sa.Integer:
            alias_field = pk_field.name

    max_rowid = 0
    seq = 0
    buffer: List[Dict[str, Any]] = []
    rows_buffer: List[Dict[str, Any]] = []
    with resource:
        for row in resource.row_stream:
            seq += 1
            item: Dict[str, Any] = {}
            cells = {}
            for field in schema.fields:
                cell = row[field.name]
                if cell is None:
                    item[field.name] = None
                else:
                    item[field.name] = encode_storage(field, cell)
                    cells[field.name] = {
                        "e": encode_value(cell),
                        "r": encode_storage_raw(field, cell),
                    }
            if alias_field and item.get(alias_field) is not None:
                rowid = int(item[alias_field])
            else:
                rowid = max_rowid + 1
            max_rowid = max(max_rowid, rowid)
            buffer.append(item)
            rows_buffer.append(
                {
                    "resource": resource.name,
                    "rowid": rowid,
                    "seq": seq,
                    "cells": json.dumps(cells, ensure_ascii=False),
                }
            )
            if len(buffer) > settings.BUFFER_SIZE:
                conn.execute(sa.insert(table), buffer)
                buffer.clear()
                conn.execute(sa.insert(rows_table), rows_buffer)
                rows_buffer.clear()
        if buffer:
            conn.execute(sa.insert(table), buffer)
        if rows_buffer:
            conn.execute(sa.insert(rows_table), rows_buffer)


def read_package(adapter: SqlAdapter) -> Package:
    """Read a roundtrip-published package back from SQLite"""
    Package = platform.frictionless.Package
    descriptor = read_descriptor(adapter)
    for resource_descriptor in descriptor["resources"]:
        check_structure(adapter, resource_descriptor)
    check_rows(adapter, descriptor)

    # Restore the resources pointing at the database tables
    url = adapter.engine.url.render_as_string(hide_password=False)
    resources = []
    for resource_descriptor in descriptor["resources"]:
        restored = dict(resource_descriptor)
        restored["path"] = url
        restored["dialect"] = {
            "sql": {"table": resource_descriptor["name"], "roundtrip": True}
        }
        resources.append(restored)
    restored_descriptor = {
        key: value for key, value in descriptor.items() if key != "resources"
    }
    restored_descriptor["resources"] = resources
    try:
        return Package.from_descriptor(restored_descriptor)
    except FrictionlessException:
        raise
    except Exception as exc:
        raise FrictionlessException(f"cannot read the roundtrip package: {exc}") from exc


def read_descriptor(adapter: SqlAdapter) -> Dict[str, Any]:
    """Read and validate the stored package descriptor"""
    sa = platform.sqlalchemy
    check_metadata_tables(adapter)
    package_table = adapter.metadata.tables[settings.ROUNDTRIP_PACKAGE_TABLE]
    with adapter.engine.begin() as conn:
        row = conn.execute(
            sa.select(package_table.c.descriptor).where(package_table.c.id == 1)
        ).first()
    note = "the roundtrip metadata is corrupted"
    if row is None or row[0] is None:
        raise FrictionlessException(
            f"cannot read the roundtrip package: {note} (missing descriptor)"
        )
    try:
        descriptor = json.loads(row[0])
    except Exception as exc:
        raise FrictionlessException(
            f"cannot read the roundtrip package: {note} (invalid descriptor JSON)"
        ) from exc
    if not isinstance(descriptor, dict) or not isinstance(
        descriptor.get("resources"), list
    ):
        raise FrictionlessException(
            f"cannot read the roundtrip package: {note} (invalid descriptor structure)"
        )
    for resource_descriptor in descriptor["resources"]:
        if (
            not isinstance(resource_descriptor, dict)
            or not resource_descriptor.get("name")
            or not isinstance(resource_descriptor.get("schema"), dict)
        ):
            raise FrictionlessException(
                f"cannot read the roundtrip package: {note} "
                "(invalid resource entry in the descriptor)"
            )
    return descriptor


def check_metadata_tables(adapter: SqlAdapter) -> None:
    """Ensure the roundtrip metadata tables are present and well-formed"""
    tables = adapter.metadata.tables
    if settings.ROUNDTRIP_PACKAGE_TABLE not in tables:
        raise FrictionlessException(
            "cannot read the roundtrip package: the database does not contain "
            f'roundtrip metadata (missing table "{settings.ROUNDTRIP_PACKAGE_TABLE}")'
        )
    if settings.ROUNDTRIP_ROWS_TABLE not in tables:
        raise FrictionlessException(
            "cannot read the roundtrip package: the roundtrip metadata is "
            f'corrupted (missing table "{settings.ROUNDTRIP_ROWS_TABLE}")'
        )
    expected = {
        settings.ROUNDTRIP_PACKAGE_TABLE: ["id", "descriptor"],
        settings.ROUNDTRIP_ROWS_TABLE: ["resource", "rowid", "seq", "cells"],
    }
    for name, columns in expected.items():
        actual = [column.name for column in tables[name].columns]
        if actual != columns:
            raise FrictionlessException(
                "cannot read the roundtrip package: the roundtrip metadata is "
                f'corrupted (unexpected columns of table "{name}")'
            )


def check_structure(adapter: SqlAdapter, resource_descriptor: Dict[str, Any]) -> None:
    """Ensure the data table still matches the published schema"""
    name = resource_descriptor["name"]
    try:
        schema = Schema.from_descriptor(resource_descriptor["schema"])
    except Exception as exc:
        raise FrictionlessException(
            "cannot read the roundtrip package: the roundtrip metadata is "
            f'corrupted (invalid schema of resource "{name}")'
        ) from exc
    table = adapter.metadata.tables.get(name)
    if table is None:
        raise FrictionlessException(
            "cannot read the roundtrip package: "
            f'the table of resource "{name}" was dropped or renamed'
        )
    columns = list(table.columns)
    fields = schema.fields
    for column, field in zip(columns, fields):
        if column.name != field.name:
            raise FrictionlessException(
                "cannot read the roundtrip package: "
                f'column "{field.name}" of resource "{name}" was renamed, '
                f'dropped or reordered (found column "{column.name}" instead)'
            )
        expected = settings.ROUNDTRIP_TYPE_MAPPING.get(field.type, "text")
        actual = read_type_category(column.type)
        if actual != expected:
            raise FrictionlessException(
                "cannot read the roundtrip package: "
                f'the type of column "{field.name}" of resource "{name}" '
                "was changed"
            )
    if len(columns) > len(fields):
        column = columns[len(fields)]
        raise FrictionlessException(
            "cannot read the roundtrip package: "
            f'column "{column.name}" was added to resource "{name}"'
        )
    if len(columns) < len(fields):
        field = fields[len(columns)]
        raise FrictionlessException(
            "cannot read the roundtrip package: "
            f'column "{field.name}" of resource "{name}" was dropped'
        )


def check_rows(adapter: SqlAdapter, descriptor: Dict[str, Any]) -> None:
    """Ensure the stored exact cell values are well-formed"""
    sa = platform.sqlalchemy
    rows_table = adapter.metadata.tables[settings.ROUNDTRIP_ROWS_TABLE]
    names = [resource["name"] for resource in descriptor["resources"]]
    with adapter.engine.begin() as conn:
        result = conn.execute(
            sa.select(rows_table.c.resource, rows_table.c.cells).where(
                rows_table.c.resource.in_(names)
            )
        )
        for item in result:
            decode_cells(item[1], resource=item[0])


def read_resource_descriptor(adapter: SqlAdapter, table_name: str) -> Dict[str, Any]:
    """Find the stored descriptor of the resource published as a table"""
    descriptor = read_descriptor(adapter)
    for candidate in descriptor["resources"]:
        if candidate["name"] == table_name:
            return candidate
    raise FrictionlessException(
        "cannot read the roundtrip package: "
        f'resource "{table_name}" is not described in the roundtrip metadata'
    )


def read_schema(adapter: SqlAdapter, table_name: str) -> Schema:
    """Read the stored schema of a roundtrip-published resource"""
    resource_descriptor = read_resource_descriptor(adapter, table_name)
    return Schema.from_descriptor(resource_descriptor["schema"])


def read_cell_stream(
    adapter: SqlAdapter, control: SqlControl
) -> Generator[List[Any], None, None]:
    """Read a cell stream restoring the exact published values"""
    sa = platform.sqlalchemy
    table_name = control.table
    if not table_name:
        raise FrictionlessException('Please provide "dialect.sql.table" for reading')
    resource_descriptor = read_resource_descriptor(adapter, table_name)
    check_structure(adapter, resource_descriptor)
    schema = Schema.from_descriptor(resource_descriptor["schema"])
    fields = schema.fields

    # Load the exact cell values indexed by rowid
    rows_table = adapter.metadata.tables[settings.ROUNDTRIP_ROWS_TABLE]
    sidecar: Dict[int, Dict[str, Any]] = {}
    with adapter.engine.begin() as conn:
        result = conn.execute(
            sa.select(rows_table.c.rowid, rows_table.c.cells)
            .where(rows_table.c.resource == table_name)
            .order_by(rows_table.c.seq)
        )
        for item in result:
            sidecar[item[0]] = decode_cells(item[1], resource=table_name)

    # Stream the rows: published rows in their original order first,
    # externally inserted rows afterwards in their insertion order
    ridcol = read_rowid_column(fields)
    quoted_table = quote_identifier(table_name)
    quoted_rows = quote_identifier(settings.ROUNDTRIP_ROWS_TABLE)
    published_query = (
        f'SELECT t."{ridcol}", t.* FROM {quoted_table} AS t '
        f'JOIN {quoted_rows} AS r ON t."{ridcol}" = r."rowid" '
        'AND r."resource" = :resource ORDER BY r."seq"'
    )
    appended_query = (
        f'SELECT "{ridcol}", * FROM {quoted_table} '
        f'WHERE "{ridcol}" NOT IN '
        f'(SELECT "rowid" FROM {quoted_rows} WHERE "resource" = :resource) '
        f'ORDER BY "{ridcol}"'
    )
    with adapter.engine.begin() as conn:
        yield [field.name for field in fields]
        for query, published in ((published_query, True), (appended_query, False)):
            result = conn.execute(sa.text(query), {"resource": table_name})
            for item in result:
                cells = sidecar.get(item[0], {}) if published else {}
                row = []
                for field, value in zip(fields, item[1:]):
                    row.append(decode_cell(field, value, cells.get(field.name)))
                yield row


def decode_cell(field: Field, value: Any, entry: Any) -> Any:
    """Decode a raw SQL cell using the stored exact value when unchanged"""
    if entry is None:
        # The cell was empty when published or the row was inserted externally
        return decode_storage(field, value)
    if value is not None and value == entry["r"]:
        # The cell was not modified externally: restore the exact value
        return entry["e"]
    # The cell was updated externally: interpret the SQL value
    return decode_storage(field, value)


# Codecs


def encode_value(value: Any) -> Any:
    """Encode an exact cell value as a JSON-compatible structure"""
    tag = settings.ROUNDTRIP_TAG
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return {tag: "decimal", "v": str(value)}
    if isinstance(value, datetime):
        return {tag: "datetime", "v": value.isoformat()}
    if isinstance(value, date):
        return {tag: "date", "v": value.isoformat()}
    if isinstance(value, time):
        return {tag: "time", "v": value.isoformat()}
    if isinstance(value, timedelta):
        return {tag: "timedelta", "v": [value.days, value.seconds, value.microseconds]}
    if isinstance(value, platform.isodate.Duration):
        return {tag: "duration", "v": platform.isodate.duration_isoformat(value)}
    if isinstance(value, bytes):
        return {tag: "bytes", "v": base64.b64encode(value).decode("ascii")}
    if isinstance(value, tuple):
        items = cast(tuple[Any, ...], value)
        return {tag: "tuple", "v": [encode_value(item) for item in items]}
    if isinstance(value, list):
        items = cast(List[Any], value)
        return [encode_value(item) for item in items]
    if isinstance(value, dict):
        mapping = cast(Dict[Any, Any], value)
        encoded = {str(key): encode_value(item) for key, item in mapping.items()}
        # Escape dictionaries that could be confused with a tagged value
        if set(encoded) == {tag, "v"} and isinstance(encoded[tag], str):
            return {tag: "dict", "v": encoded}
        return encoded
    raise FrictionlessException(
        "cannot publish in roundtrip mode: "
        f'values of type "{type(value).__name__}" are not supported'
    )


def decode_value(item: Any, *, resource: str) -> Any:
    """Decode an exact cell value from its JSON-compatible structure"""
    tag = settings.ROUNDTRIP_TAG
    if isinstance(item, list):
        values = cast(List[Any], item)
        return [decode_value(value, resource=resource) for value in values]
    if isinstance(item, dict):
        mapping = cast(Dict[str, Any], item)
        if set(mapping) == {tag, "v"} and isinstance(mapping[tag], str):
            kind: str = mapping[tag]
            payload: Any = mapping["v"]
            try:
                if kind == "decimal":
                    return Decimal(payload)
                if kind == "datetime":
                    return datetime.fromisoformat(payload)
                if kind == "date":
                    return date.fromisoformat(payload)
                if kind == "time":
                    return time.fromisoformat(payload)
                if kind == "timedelta":
                    return timedelta(*payload)
                if kind == "duration":
                    return platform.isodate.parse_duration(payload)
                if kind == "bytes":
                    return base64.b64decode(payload)
                if kind == "tuple":
                    return tuple(decode_value(payload, resource=resource))
                if kind == "dict":
                    return decode_value(payload, resource=resource)
            except Exception as exc:
                raise FrictionlessException(
                    "cannot read the roundtrip package: the roundtrip metadata "
                    f'is corrupted (invalid "{kind}" value of resource "{resource}")'
                ) from exc
            raise FrictionlessException(
                "cannot read the roundtrip package: the roundtrip metadata is "
                f'corrupted (unknown value tag "{kind}" of resource "{resource}")'
            )
        return {
            key: decode_value(value, resource=resource) for key, value in mapping.items()
        }
    return item


def decode_cells(text: str, *, resource: str) -> Dict[str, Any]:
    """Decode the stored exact cell values of one row"""
    try:
        cells = json.loads(text)
    except Exception as exc:
        raise FrictionlessException(
            "cannot read the roundtrip package: the roundtrip metadata is "
            f'corrupted (invalid cells JSON of resource "{resource}")'
        ) from exc
    if not isinstance(cells, dict):
        raise FrictionlessException(
            "cannot read the roundtrip package: the roundtrip metadata is "
            f'corrupted (invalid cells of resource "{resource}")'
        )
    entries = cast(Dict[str, Any], cells)
    decoded = {}
    for field, entry in entries.items():
        valid = isinstance(entry, dict) and set(cast(Dict[str, Any], entry)) == {"e", "r"}
        if not valid:
            raise FrictionlessException(
                "cannot read the roundtrip package: the roundtrip metadata is "
                f'corrupted (invalid cell entry of resource "{resource}")'
            )
        entry = cast(Dict[str, Any], entry)
        decoded[field] = {
            "e": decode_value(entry["e"], resource=resource),
            "r": entry["r"],
        }
    return decoded


def encode_storage(field: Field, value: Any) -> Any:
    """Encode a cell value for insertion into the data table"""
    if value is None:
        return None
    type = field.type
    if type == "number":
        return float(value)
    if type == "datetime":
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value
    if type == "time":
        if value.tzinfo is not None:
            value = time_to_utc(value)
        return value
    if type in ("boolean", "date", "integer", "string", "year"):
        return value
    return field.write_cell(value)[0]


def encode_storage_raw(field: Field, value: Any) -> Any:
    """Encode a cell value to the form SQLite stores and returns

    This is used to detect whether a cell was externally modified: if the
    value seen in SQL equals the stored form, the cell is unchanged.
    """
    if value is None:
        return None
    type = field.type
    if type == "number":
        return float(value)
    if type == "datetime":
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return format_datetime(value)
    if type == "time":
        if value.tzinfo is not None:
            value = time_to_utc(value)
        return format_time(value)
    if type == "date":
        return value.isoformat()
    if type == "boolean":
        return int(value)
    if type in ("integer", "year"):
        return int(value)
    if type == "string":
        return value
    return field.write_cell(value)[0]


def decode_storage(field: Field, value: Any) -> Any:
    """Interpret a raw SQL value found in a roundtrip column

    Used for rows inserted and cells updated by other tools after the
    package was published.
    """
    if value is None:
        return None
    type = field.type
    if type == "number":
        if isinstance(value, float):
            return Decimal(repr(value))
        if isinstance(value, int):
            return Decimal(value)
        return value
    if type == "boolean":
        if isinstance(value, int):
            return bool(value)
        return value
    if type == "datetime":
        if isinstance(value, str):
            parsed = parse_datetime(value)
            if parsed is not None:
                return parsed
        return value
    if type == "date":
        if isinstance(value, str):
            try:
                return date.fromisoformat(value)
            except ValueError:
                pass
        return value
    if type == "time":
        if isinstance(value, str):
            parsed = parse_time(value)
            if parsed is not None:
                return parsed
        return value
    return value


# Helpers


def format_datetime(value: datetime) -> str:
    """Format a datetime the way SQLAlchemy stores it in SQLite"""
    return "%04d-%02d-%02d %02d:%02d:%02d.%06d" % (
        value.year,
        value.month,
        value.day,
        value.hour,
        value.minute,
        value.second,
        value.microsecond,
    )


def format_time(value: time) -> str:
    """Format a time the way SQLAlchemy stores it in SQLite"""
    return "%02d:%02d:%02d.%06d" % (
        value.hour,
        value.minute,
        value.second,
        value.microsecond,
    )


def parse_datetime(text: str) -> Any:
    """Parse a datetime found in SQL as a naive (UTC-based) datetime"""
    try:
        value = datetime.fromisoformat(
            text[:-1] + "+00:00" if text.endswith("Z") else text
        )
    except ValueError:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def parse_time(text: str) -> Any:
    """Parse a time found in SQL as a naive (UTC-based) time"""
    try:
        value = time.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    if value.tzinfo is not None:
        value = time_to_utc(value)
    return value


def time_to_utc(value: time) -> time:
    """Convert an aware time to a naive UTC time of day"""
    offset = value.utcoffset()
    if offset is None:
        return value.replace(tzinfo=None)
    # A mid-range reference date avoids under/overflow at the extremes
    combined = datetime.combine(date(2000, 1, 1), value.replace(tzinfo=None))
    return (combined - offset).time()


def read_type_category(column_type: Any) -> str:
    """Map a reflected SQL column type to a roundtrip type category"""
    sa = platform.sqlalchemy
    if isinstance(column_type, sa.Boolean):
        return "boolean"
    if isinstance(column_type, sa.DateTime):
        return "datetime"
    if isinstance(column_type, sa.Date):
        return "date"
    if isinstance(column_type, sa.Time):
        return "time"
    if isinstance(column_type, sa.Float):
        return "float"
    if isinstance(column_type, sa.Integer):
        return "integer"
    if isinstance(column_type, sa.String):
        return "text"
    return "other"


def read_rowid_column(fields: List[Field]) -> str:
    """Pick a usable rowid alias that does not clash with field names"""
    names = {field.name for field in fields}
    for candidate in ("rowid", "_rowid_", "oid"):
        if candidate not in names:
            return candidate
    raise FrictionlessException(
        "cannot read the roundtrip package: "
        "the table uses all the available rowid aliases as field names"
    )


def quote_identifier(name: str) -> str:
    """Quote a SQLite identifier"""
    return '"%s"' % name.replace('"', '""')
