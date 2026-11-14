from __future__ import annotations

# General

BUFFER_SIZE = 1000
ROW_NUMBER_IDENTIFIER = "_rowNumber"
ROW_VALID_IDENTIFIER = "_rowValid"
METADATA_IDENTIFIERS = [ROW_NUMBER_IDENTIFIER, ROW_VALID_IDENTIFIER]

# Roundtrip

ROUNDTRIP_PACKAGE_TABLE = "_frictionless_package"
ROUNDTRIP_ROWS_TABLE = "_frictionless_rows"
ROUNDTRIP_METADATA_TABLES = [ROUNDTRIP_PACKAGE_TABLE, ROUNDTRIP_ROWS_TABLE]
ROUNDTRIP_TAG = "$fr"
# Resource properties describing the data location and physical format.
# They are not restored into the read-back package (the database itself
# becomes the data location of the restored resources).
ROUNDTRIP_LOCATION_PROPS = [
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
# Expected SQL storage category of each field type in roundtrip mode
ROUNDTRIP_TYPE_MAPPING = {
    "any": "text",
    "array": "text",
    "boolean": "boolean",
    "date": "date",
    "datetime": "datetime",
    "duration": "text",
    "geojson": "text",
    "geopoint": "text",
    "integer": "integer",
    "list": "text",
    "number": "float",
    "object": "text",
    "string": "text",
    "time": "time",
    "year": "integer",
    "yearmonth": "text",
}

# Prefixes

# https://docs.sqlalchemy.org/en/13/core/engines.html
# https://docs.sqlalchemy.org/en/13/dialects/index.html
SCHEME_PREFIXES = [
    "postgresql",
    "mysql",
    "oracle",
    "mssql",
    "sqlite",
    "duckdb",
    "firebird",
    "sybase",
    "db2",
    "ibm",
]
