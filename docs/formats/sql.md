# Sql Format

Frictionless supports reading and writing SQL databases.

## Supported Databases

Frictionless Framework in-general support many databases that can be used with `sqlalchemy`. Here is a list of the databases with tested support:

### SQLite

> https://www.sqlite.org/index.html

It's a well-tested default database used by Frictionless:

```bash tabs=CLI
pip install frictionless[sql]
```

### PostgreSQL

> https://www.postgresql.org/

This database is well-tested and provides the most data types:

```bash tabs=CLI
pip install frictionless[postgresql]
```

### MySQL

> https://www.mysql.com/

Another popular databases having been tested with Frictionless:

```bash tabs=CLI
pip install frictionless[mysql]
```

### DuckDB

> https://duckdb.org/

DuckDB is a reletively new database and, currently, Frictionless support is experimental:

```bash tabs=CLI
pip install frictionless[duckdb]
```

## Reading Data

You can read SQL database:

```python tabs=Python
from frictionless import Resource, formats

control = SqlControl(table="test_table", basepath='data')
with Resource(path="sqlite:///sqlite.db", control=control) as resource:
    print(resource.read_rows())
```

## Writing Data

You can write SQL databases:

```python tabs=Python
from frictionless import Package

package = Package('path/to/datapackage.json')
package.publish('postgresql://database')
```

## Lossless Roundtrip (SQLite)

By default, publishing a package to a SQL database is lossy: some field types
are downgraded (e.g. `year` becomes `integer`, `geopoint` becomes `string`),
arbitrary-precision numbers are stored as floats, timezones of datetimes are
normalized away, and field constraints as well as package/resource metadata
are not restored when reading the database back.

With SQLite you can publish in a lossless roundtrip mode instead:

```python tabs=Python
from frictionless import Package, formats

package = Package('path/to/datapackage.json')
package.publish('sqlite:///database.db', control=formats.SqlControl(roundtrip=True))

restored = Package('sqlite:///database.db')
```

Reading the database back with `Package(url)` does not require any flag: if
the database contains a roundtrip-published package it is restored,
otherwise the database is reflected as usual.

### What is restored

- The descriptor: `package.to_descriptor()` of the restored package equals
  the published one, except the data-location properties of each resource
  (`path`, `data`, `scheme`, `format`, `mediatype`, `encoding`,
  `compression`, `innerpath`, `dialect`, `hash`, `bytes`) which now point at
  the database. Resource order, field order, custom properties and the
  v1/v2 `$schema` are preserved.
- The values: `resource.read_rows()` yields the same values as published,
  with the same types. `Decimal` values keep their exact precision, aware
  `datetime`/`time` values keep their UTC offset and local time, naive ones
  stay naive, and a column may freely mix aware and naive values. Row order
  is preserved.

### Database layout

Every resource is stored as a table of the same name with one column per
field (in field order); primary and foreign keys are SQL constraints. The
column types are the usual ones: `integer`, `boolean`, `date`, `time` and
`year` use native SQL types, `number` is a numeric (`REAL`) column that can
be compared and sorted in SQL, `datetime` is stored as the UTC instant
(naive values as-is), and the remaining types are stored as the text
frictionless writes for them. The tables can be queried directly with SQL.

In addition, two metadata tables are created and used for the lossless
restore: `_frictionless_package` (the package descriptor) and
`_frictionless_rows` (the row identity/order history and the exact cell
values). Only the resources described there are read back; other tables in
the database are ignored, and the metadata tables themselves are never
exposed as resources.

The business tables have exactly the published business columns: the row
identity is maintained out-of-band, so direct SQL `SELECT`/`INSERT`/
`UPDATE`/`DELETE` statements keep working, and existing primary/foreign key
constraints stay in effect. To keep the history up to date, roundtrip
publishing also installs three triggers per data table (named
`_frictionless_<resource>_insert`, `..._update` and `..._delete`). They
only write to the metadata table; applications do not need to use
Frictionless or maintain any extra column to modify the data.

### Row identity and order

Rows written by the publish keep their published order. Rows inserted later
by other tools are placed after all the earlier rows, in the order the
inserts succeeded (for a multi-row `INSERT`, in the order of its rows).
Positions are recorded by the triggers, so they are stable across
transactions and process restarts:

- `UPDATE` never changes the position of a row, even when it changes an
  integer primary key (which is an alias of the SQLite rowid).
- `DELETE` followed by an `INSERT` creates a new row, placed at the end of
  the row order. Even if it reuses the deleted row's primary key or internal
  rowid, it does not inherit the deleted row's exact published values.
- Multiple `INSERT`s in one transaction are ordered by execution order.
- A rolled back transaction leaves no rows, no positions and no restored
  values behind.

This works with no primary key, a single integer primary key, a text
primary key and a composite primary key. If the data table uses `rowid`,
`_rowid_` or `oid` as business column names, the reader automatically picks
an unused alias instead of binding the business column; a table shadowing
all three aliases falls back to its primary key. Publishing without any
primary key in that case is rejected, because the rows cannot be identified.
Two rows with identical content are still two rows with their own identities.

Row order cannot be recovered from the final table contents alone for
databases published by an older version of this implementation: see
"Legacy databases" below.

### External changes

Other tools may modify the database after publishing. On read-back:

- Rows added with `INSERT` (writing only the field columns) are read after
  the published rows, in insertion order. Their values are interpreted as
  seen in SQL: `number` becomes a `Decimal` (a `REAL` value is converted
  using the shortest decimal form of Python's `repr`), `datetime` and `time`
  become naive, other types are read as usual.
- Cells changed with `UPDATE` are interpreted from their SQL value the same
  way; untouched cells are restored exactly. A cell whose SQL value equals
  the published one counts as untouched. This also applies to a published
  row changed and then changed back to its published value, and to an
  externally inserted row changed later: only rows written by the publish
  own exact values, so a new row can never inherit the exact values of an
  older row that happened to have the same SQL representation.
- Rows removed with `DELETE` do not appear.
- A row whose identity changed (e.g. an integer primary key update) keeps
  its position and its exact values for untouched cells.
- Externally written values that violate the schema do not raise while
  reading; they are reported by the regular validation.
- If a table was dropped, or a column was added, dropped, renamed or
  retyped, `Package(url)` raises a `FrictionlessException` naming the
  resource (and the column when applicable).
- If the roundtrip metadata tables were damaged, `Package(url)` raises a
  `FrictionlessException`.

### Legacy databases

These guarantees apply to databases published after this fix. A database
published by an older version, when it has not been modified externally,
still reads back exactly (descriptor and values): the reader recognizes the
old metadata layout.

However, old databases recorded only the rowid of each published row and
have no operation history. For them the reader cannot know whether a row
disappeared or was re-inserted, whether an integer primary key update
moved a published row, or which later insert reused a freed rowid. It
cannot be claimed that row identity and order can be reconstructed from the
final table contents alone, and previously applied external modifications
cannot be retroactively restored. Such databases should be republished in
roundtrip mode if that history is needed.

Roundtrip publishing and reading of tables without a roundtrip metadata
(`roundtrip=False`) are unchanged by all of the above.

### Rejected publishes

Publishing with `roundtrip=True` raises a `FrictionlessException` before
creating any table (the database is left unchanged) when:

- the database is not SQLite (only SQLite is supported),
- a table with the name of a resource already exists,
- two resource names, or two field names of one resource, differ only by
  case,
- the package contains a non-table resource,
- the source data contains rows that do not pass schema validation,
- a resource has no primary key but uses `rowid`, `_rowid_` and `oid` all
  as field names (the rows cannot be identified),
- a trigger with one of the reserved `_frictionless_<resource>_*` names
  already exists in the database,
- the reserved `_frictionless_rows_seq` index name already exists in the
  database.

## Configuration

There is a dialect to configure how Frictionless read and write files in this format. For example:

```python tabs=Python
from frictionless import Resource, formats

control = SqlControl(table='table', order_by='field', where='field > 20')
resource = Resource('postgresql://database', control=control)
```

## Reference

```yaml reference
references:
  - frictionless.formats.SqlControl
```
