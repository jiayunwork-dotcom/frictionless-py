from __future__ import annotations

import bisect
import datetime
import itertools
import os
import sqlite3
import tempfile
from typing import TYPE_CHECKING, Any, Dict, Iterator, List, Optional, Tuple

from .. import errors
from ..exception import FrictionlessException
from .declaration import read_temporal_declaration, validate_temporal_declaration

if TYPE_CHECKING:
    from ..error import Error
    from ..package import Package
    from ..table import Row

# Staging rows are flushed to SQLite in batches of this size
FLUSH_SIZE = 10000

# Interval error kinds (stored as integers in the staging database)
INTERVAL_MISSING_FROM = 1
INTERVAL_NAIVE = 2
INTERVAL_INVERTED = 3

# Time error kinds (stored as integers in the staging database)
TIME_MISSING = 1
TIME_NAIVE = 2

EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)


def datetime_to_micros(value: datetime.datetime) -> int:
    """Convert an aware datetime to exact UTC epoch microseconds"""
    delta = value.astimezone(datetime.timezone.utc) - EPOCH
    return delta.days * 86400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


def is_naive_datetime(value: Any) -> bool:
    """Whether the value is a datetime without an explicit timezone"""
    return isinstance(value, datetime.datetime) and (
        value.tzinfo is None or value.tzinfo.utcoffset(value) is None
    )


class TemporalRelation:
    """A single temporal foreign key declaration ready for validation"""

    def __init__(
        self, *, id: int, fact_resource: str, declaration: Dict[str, Any], time_type: str
    ):
        self.id = id
        self.fact_resource = fact_resource
        self.fact_fields: List[str] = declaration["fields"]
        self.at: str = declaration["at"]
        self.dim_resource: str = declaration["resource"] or fact_resource
        self.dim_fields: List[str] = declaration["reference_fields"]
        self.valid_from: str = declaration["valid_from"]
        self.valid_to: Optional[str] = declaration["valid_to"] or None
        self.time_type = time_type
        self.dim_table = f"dim_{id}"
        self.fact_table = f"fact_{id}"

    @property
    def dim_interval_fields(self) -> List[str]:
        fields = [self.valid_from]
        if self.valid_to:
            fields.append(self.valid_to)
        return fields


class TemporalEngine:
    """Temporal foreign key validation engine

    The engine stages the decoded key/moment/interval values of every
    involved resource into a temporary SQLite database while the resource
    is validated (each source is read exactly once, inside its own
    validation pass). After all the resources are read, `finalize` runs
    the temporal joins and produces the deferred errors.

    Memory use is bounded by the largest version group of a single key
    plus the SQLite page cache; everything else lives in the temporary
    database which is removed in `cleanup` (success or failure).
    """

    def __init__(self, package: Package):
        self.package = package
        self.relations: List[TemporalRelation] = []
        self.dim_roles: Dict[str, List[TemporalRelation]] = {}
        self.fact_roles: Dict[str, List[TemporalRelation]] = {}
        self.buffers: Dict[str, List[Tuple[Any, ...]]] = {}
        self.time_errors: List[Tuple[int, int, int]] = []
        self.interval_errors: List[Tuple[int, int, int]] = []
        self.db: Optional[sqlite3.Connection] = None
        self.tempdir: Optional["tempfile.TemporaryDirectory[str]"] = None
        self.finalized = False
        try:
            self.__prepare_relations()
            self.__prepare_staging()
        except Exception:
            self.cleanup()
            raise

    # Setup

    def __prepare_relations(self):
        from ..resources import TableResource

        # Collect the declarations in a stable order: resources follow the
        # package order, declarations follow the schema order
        for resource in self.package.resources:
            schema = resource.schema
            if not isinstance(resource, TableResource) or not schema:
                continue
            for declaration in schema.temporal_foreign_keys:
                declaration = read_temporal_declaration(declaration)
                at = declaration["at"]
                time_type = ""
                if schema.has_field(at):
                    time_type = schema.get_field(at).type
                relation = TemporalRelation(
                    id=len(self.relations),
                    fact_resource=resource.name,  # type: ignore
                    declaration=declaration,
                    time_type=time_type,
                )
                self.relations.append(relation)

        # Verify the declarations against the runtime schemas; descriptor
        # level validation already checked inline schemas, this second line
        # covers externally referenced or missing schemas before any source
        # is opened
        for relation in self.relations:
            for note in self.__validate_relation(relation):
                error = errors.PackageError(note=note)
                raise FrictionlessException(error)

        # Index the roles
        for relation in self.relations:
            self.fact_roles.setdefault(relation.fact_resource, []).append(relation)
            self.dim_roles.setdefault(relation.dim_resource, []).append(relation)

    def __validate_relation(self, relation: TemporalRelation) -> Iterator[str]:
        field_types = self.__resource_field_types(relation.fact_resource)
        reference_field_types = self.__resource_field_types(relation.dim_resource)
        if field_types is None:
            note = 'temporal foreign key resource "%s" is not tabular'
            yield note % relation.fact_resource
            return
        if reference_field_types is None:
            if not self.package.has_resource(relation.dim_resource):
                note = 'temporal foreign key resource "%s" does not exist'
                yield note % relation.dim_resource
            else:
                note = 'temporal foreign key resource "%s" is not tabular'
                yield note % relation.dim_resource
            return
        declaration = {
            "fields": relation.fact_fields,
            "at": relation.at,
            "resource": relation.dim_resource,
            "reference_fields": relation.dim_fields,
            "valid_from": relation.valid_from,
            "valid_to": relation.valid_to or "",
        }
        yield from validate_temporal_declaration(
            declaration,
            field_types=field_types,
            reference_field_types=reference_field_types,
        )

    def __resource_field_types(self, name: str) -> Optional[Dict[str, str]]:
        from ..resources import TableResource

        if not self.package.has_resource(name):
            return None
        resource = self.package.get_resource(name)
        if not isinstance(resource, TableResource):
            return None
        schema = resource.schema
        if not schema or not schema.fields:
            return None
        return {field.name: field.type for field in schema.fields if field.name}

    def __prepare_staging(self):
        self.tempdir = tempfile.TemporaryDirectory(prefix="frictionless-temporal-")
        path = os.path.join(self.tempdir.name, "staging.db")
        self.db = sqlite3.connect(path)
        # The staging database is temporary by design: it is rebuilt on every
        # validation and removed afterwards, so durability features are off
        self.db.execute("PRAGMA journal_mode=OFF")
        self.db.execute("PRAGMA synchronous=OFF")
        self.db.execute("PRAGMA temp_store=FILE")
        self.db.execute(
            "CREATE TABLE time_errors (rid INTEGER, rownum INTEGER, kind INTEGER)"
        )
        self.db.execute(
            "CREATE TABLE interval_errors (rid INTEGER, rownum INTEGER, kind INTEGER)"
        )
        self.db.execute("CREATE TABLE results (rid INTEGER, rownum INTEGER, cnt INTEGER)")
        for relation in self.relations:
            keys = ", ".join(f"k{i}" for i in range(len(relation.fact_fields)))
            self.db.execute(
                f"CREATE TABLE {relation.dim_table} "
                f"({keys}, vfrom INTEGER, vto INTEGER, rownum INTEGER)"
            )
            self.db.execute(
                f"CREATE TABLE {relation.fact_table} ({keys}, at INTEGER, rownum INTEGER)"
            )
            self.buffers[relation.dim_table] = []
            self.buffers[relation.fact_table] = []
        self.db.commit()

    # Introspection

    def is_involved(self, name: Optional[str]) -> bool:
        """Whether the resource takes part in any temporal relation"""
        if name is None:
            return False
        return name in self.fact_roles or name in self.dim_roles

    @property
    def staging_bytes(self) -> int:
        """Size of the temporary staging content on disk"""
        size = 0
        if self.tempdir is not None:
            for name in os.listdir(self.tempdir.name):
                path = os.path.join(self.tempdir.name, name)
                if os.path.isfile(path):
                    size += os.path.getsize(path)
        return size

    # Stage

    def make_row_callback(self, name: Optional[str]):
        """Create an on_row callback staging rows of the given resource"""
        if name is None:
            return None
        dim_roles = self.dim_roles.get(name, [])
        fact_roles = self.fact_roles.get(name, [])
        if not dim_roles and not fact_roles:
            return None

        def callback(row: Row):
            for relation in dim_roles:
                self.__stage_dim_row(relation, row)
            for relation in fact_roles:
                self.__stage_fact_row(relation, row)

        return callback

    def __stage_dim_row(self, relation: TemporalRelation, row: Row):
        # Rows with decoding failures in the involved fields keep their
        # original type errors only and do not take part in the relation
        involved = relation.dim_fields + relation.dim_interval_fields
        error_cells = row.error_cells
        for name in involved:
            if name in error_cells:
                return
        keys = tuple(row[name] for name in relation.dim_fields)
        if any(key is None for key in keys):
            # A version row without a full key can never match a fact row
            # (fact rows with missing key parts are exempt) so it is skipped
            return
        valid_from = row[relation.valid_from]
        valid_to = row[relation.valid_to] if relation.valid_to else None
        if valid_from is None:
            self.__emit_interval_error(relation, row.row_number, INTERVAL_MISSING_FROM)
            return
        if is_naive_datetime(valid_from) or (
            valid_to is not None and is_naive_datetime(valid_to)
        ):
            self.__emit_interval_error(relation, row.row_number, INTERVAL_NAIVE)
            return
        start = self.__time_to_int(valid_from)
        end = self.__time_to_int(valid_to) if valid_to is not None else None
        if end is not None and start >= end:
            self.__emit_interval_error(relation, row.row_number, INTERVAL_INVERTED)
            return
        self.__buffer(relation.dim_table, keys + (start, end, row.row_number))

    def __stage_fact_row(self, relation: TemporalRelation, row: Row):
        involved = relation.fact_fields + [relation.at]
        error_cells = row.error_cells
        for name in involved:
            if name in error_cells:
                return
        keys = tuple(row[name] for name in relation.fact_fields)
        if any(key is None for key in keys):
            # A fact row with a missing key component is exempt
            return
        at = row[relation.at]
        if at is None:
            self.__emit_time_error(relation, row.row_number, TIME_MISSING)
            return
        if is_naive_datetime(at):
            self.__emit_time_error(relation, row.row_number, TIME_NAIVE)
            return
        self.__buffer(
            relation.fact_table, keys + (self.__time_to_int(at), row.row_number)
        )

    def __time_to_int(self, value: Any) -> int:
        if isinstance(value, datetime.datetime):
            return datetime_to_micros(value)
        if isinstance(value, datetime.date):
            return value.toordinal()
        raise FrictionlessException(
            errors.PackageError(note=f"invalid temporal value: {value!r}")
        )

    # Stage (errors)

    def __emit_interval_error(self, relation: TemporalRelation, rownum: int, kind: int):
        self.interval_errors.append((relation.id, rownum, kind))
        if len(self.interval_errors) >= FLUSH_SIZE:
            self.__flush_error_buffer("interval_errors", self.interval_errors)

    def __emit_time_error(self, relation: TemporalRelation, rownum: int, kind: int):
        self.time_errors.append((relation.id, rownum, kind))
        if len(self.time_errors) >= FLUSH_SIZE:
            self.__flush_error_buffer("time_errors", self.time_errors)

    def __flush_error_buffer(self, table: str, buffer: List[Tuple[int, int, int]]):
        assert self.db is not None
        self.db.executemany(f"INSERT INTO {table} VALUES (?, ?, ?)", buffer)
        self.db.commit()
        buffer.clear()

    # Stage (rows)

    def __buffer(self, table: str, values: Tuple[Any, ...]):
        buffer = self.buffers[table]
        buffer.append(values)
        if len(buffer) >= FLUSH_SIZE:
            self.__flush_table(table)

    def __flush_table(self, table: str):
        assert self.db is not None
        buffer = self.buffers[table]
        if buffer:
            marks = ", ".join("?" for _ in buffer[0])
            self.db.executemany(f"INSERT INTO {table} VALUES ({marks})", buffer)
            self.db.commit()
            buffer.clear()

    def __flush(self):
        assert self.db is not None
        for table in self.buffers:
            self.__flush_table(table)
        self.__flush_error_buffer("time_errors", self.time_errors)
        self.__flush_error_buffer("interval_errors", self.interval_errors)

    # Finalize

    def finalize(
        self, budgets: Dict[str, Optional[int]]
    ) -> Dict[str, Tuple[List[Error], bool]]:
        """Run the temporal joins and build the deferred errors

        Parameters:
            budgets: resource name -> maximum amount of temporal errors to
                materialize for the resource (None means no limit)

        Returns:
            dict: resource name -> (sorted temporal errors, truncated flag);
                the flag tells whether more errors existed than the budget
                allowed to materialize
        """
        assert self.db is not None
        assert not self.finalized
        self.finalized = True
        self.__flush()
        self.__create_indexes()

        # Sweep all the relations: the exact match counts are computed for
        # every staged fact row even when the owning task cannot store any
        # more errors, so truncation reporting stays exact
        for relation in self.relations:
            self.__sweep(relation)

        # Materialize the errors resource by resource
        result: Dict[str, Tuple[List[Error], bool]] = {}
        for name in set(self.fact_roles) | set(self.dim_roles):
            budget = budgets.get(name)
            if budget is not None and budget <= 0:
                if self.__has_candidates(name):
                    result[name] = ([], True)
                continue
            errors, truncated = self.__build_errors(name, budget)
            if errors or truncated:
                result[name] = (errors, truncated)
        return result

    def __has_candidates(self, name: str) -> bool:
        """Whether any error candidate exists for the resource"""
        assert self.db is not None
        for table, roles in (
            ("interval_errors", self.dim_roles.get(name, [])),
            ("time_errors", self.fact_roles.get(name, [])),
            ("results", self.fact_roles.get(name, [])),
        ):
            for relation in roles:
                row = self.db.execute(
                    f"SELECT 1 FROM {table} WHERE rid = ? LIMIT 1", (relation.id,)
                ).fetchone()
                if row is not None:
                    return True
        return False

    def __create_indexes(self):
        assert self.db is not None
        self.db.execute("CREATE INDEX time_errors_rid ON time_errors (rid, rownum)")
        self.db.execute(
            "CREATE INDEX interval_errors_rid ON interval_errors (rid, rownum)"
        )
        self.db.execute("CREATE INDEX results_rid ON results (rid, rownum)")
        for relation in self.relations:
            keys = ", ".join(f"k{i}" for i in range(len(relation.fact_fields)))
            self.db.execute(
                f"CREATE INDEX {relation.dim_table}_key ON {relation.dim_table} ({keys})"
            )
            self.db.execute(
                f"CREATE INDEX {relation.fact_table}_rownum "
                f"ON {relation.fact_table} (rownum)"
            )
        self.db.commit()

    # Finalize (sweep)

    def __sweep(self, relation: TemporalRelation):
        """Compute the exact match counts of every staged fact row

        Both sides are streamed in key order and merged. For every key the
        version starts and ends are sorted once, then every fact moment is
        counted with two binary searches:

            count = |{from <= at}| - |{to <= at}|

        which is exact for well formed intervals (from < to) regardless of
        how many versions overlap, so high-overlap keys cost O(log n) per
        fact row instead of O(overlaps).
        """
        assert self.db is not None
        width = len(relation.fact_fields)
        keys = ", ".join(f"k{i}" for i in range(width))
        dim_cursor = self.db.execute(
            f"SELECT {keys}, vfrom, vto FROM {relation.dim_table} ORDER BY {keys}"
        )
        fact_cursor = self.db.execute(
            f"SELECT {keys}, at, rownum FROM {relation.fact_table} ORDER BY {keys}"
        )
        dim_groups = itertools.groupby(dim_cursor, key=lambda row: tuple(row[:width]))
        fact_groups = itertools.groupby(fact_cursor, key=lambda row: tuple(row[:width]))
        dim_key, dim_rows = self.__next_group(dim_groups)
        fact_key, fact_rows = self.__next_group(fact_groups)
        results: List[Tuple[int, int, int]] = []
        while fact_key is not None:
            while dim_key is not None and dim_key < fact_key:
                dim_key, dim_rows = self.__next_group(dim_groups)
            if dim_key is not None and dim_key == fact_key:
                starts = sorted(row[width] for row in dim_rows)
                ends = sorted(
                    row[width + 1] for row in dim_rows if row[width + 1] is not None
                )
                for row in fact_rows:
                    at = row[width]
                    count = bisect.bisect_right(starts, at) - bisect.bisect_right(
                        ends, at
                    )
                    if count != 1:
                        results.append((relation.id, row[width + 1], count))
            else:
                for row in fact_rows:
                    results.append((relation.id, row[width + 1], 0))
            fact_key, fact_rows = self.__next_group(fact_groups)
            if len(results) >= FLUSH_SIZE:
                self.db.executemany("INSERT INTO results VALUES (?, ?, ?)", results)
                self.db.commit()
                results.clear()
        if results:
            self.db.executemany("INSERT INTO results VALUES (?, ?, ?)", results)
            self.db.commit()

    @staticmethod
    def __next_group(groups: Any) -> Tuple[Optional[Tuple[Any, ...]], List[Any]]:
        try:
            key, rows = next(groups)
        except StopIteration:
            return None, []
        return key, list(rows)

    # Finalize (errors)

    def __build_errors(
        self, name: str, budget: Optional[int]
    ) -> Tuple[List[Error], bool]:
        """Materialize the temporal errors of one resource

        Candidates are read from the staging database in (row number,
        declaration) order and at most `budget` errors are built, so the
        amount of error objects never exceeds the amount that can still be
        stored in the report.
        """
        assert self.db is not None
        limit = budget + 1 if budget is not None else None
        built: List[Error] = []
        rownums: List[int] = []
        rids: List[int] = []

        # Interval errors owned by this resource as the referenced table
        relations = self.dim_roles.get(name, [])
        if relations:
            rows = self.__select_candidates(
                "interval_errors", [relation.id for relation in relations], limit
            )
            by_id = {relation.id: relation for relation in relations}
            for rid, rownum, kind in rows:
                built.append(self.__build_interval_error(by_id[rid], rownum, kind))
                rownums.append(rownum)
                rids.append(rid)

        # Time and key errors owned by this resource as the fact table
        relations = self.fact_roles.get(name, [])
        if relations:
            remaining = None if limit is None else max(0, limit - len(built))
            if remaining is None or remaining > 0:
                rows = self.__select_fact_candidates(relations, remaining)
                by_id = {relation.id: relation for relation in relations}
                for rid, rownum, kind, count in rows:
                    relation = by_id[rid]
                    if kind is not None:
                        built.append(self.__build_time_error(relation, rownum, kind))
                    else:
                        built.append(self.__build_key_error(relation, rownum, count))
                    rownums.append(rownum)
                    rids.append(rid)

        order = sorted(range(len(built)), key=lambda index: (rownums[index], rids[index]))
        built = [built[index] for index in order]
        truncated = False
        if budget is not None and len(built) > budget:
            built = built[:budget]
            truncated = True
        return built, truncated

    def __select_candidates(
        self, table: str, rids: List[int], limit: Optional[int]
    ) -> List[Tuple[int, int, int]]:
        """Read (rid, rownum, kind) candidates of relations in row order"""
        assert self.db is not None
        marks = ", ".join("?" for _ in rids)
        sql = (
            f"SELECT rid, rownum, kind FROM {table} WHERE rid IN ({marks}) "
            "ORDER BY rownum, rid"
        )
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return list(self.db.execute(sql, rids))

    def __select_fact_candidates(
        self, relations: List[TemporalRelation], limit: Optional[int]
    ) -> List[Tuple[int, int, Any, Any]]:
        """Merge time and key error candidates of fact relations in row order"""
        assert self.db is not None
        rids = [relation.id for relation in relations]
        marks = ", ".join("?" for _ in rids)
        sql = (
            "SELECT rid, rownum, kind, NULL FROM time_errors "
            f"WHERE rid IN ({marks}) "
            "UNION ALL "
            "SELECT rid, rownum, NULL, cnt FROM results "
            f"WHERE rid IN ({marks}) "
            "ORDER BY rownum, rid"
        )
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return list(self.db.execute(sql, rids + rids))

    def __build_interval_error(
        self, relation: TemporalRelation, rownum: int, kind: int
    ) -> Error:
        if kind == INTERVAL_MISSING_FROM:
            note = 'the validity start "%s" is required' % relation.valid_from
        elif kind == INTERVAL_NAIVE:
            note = "interval boundaries of datetime fields must have an explicit timezone"
        else:
            note = "the interval start is not earlier than the interval end"
        note += ' (temporal foreign key of resource "%s")' % relation.fact_resource
        error = errors.TemporalIntervalError(
            note=note,
            cells=[],
            row_number=rownum,
            field_names=list(relation.dim_interval_fields),
        )
        return error

    def __build_time_error(
        self, relation: TemporalRelation, rownum: int, kind: int
    ) -> Error:
        if kind == TIME_MISSING:
            note = 'the temporal moment "%s" is required' % relation.at
        else:
            note = (
                "the temporal moment of a datetime field must have an explicit timezone"
            )
        note += ' (temporal foreign key of resource "%s")' % relation.fact_resource
        error = errors.TemporalTimeError(
            note=note,
            cells=[],
            row_number=rownum,
            field_names=[relation.at],
        )
        return error

    def __build_key_error(
        self, relation: TemporalRelation, rownum: int, count: int
    ) -> Error:
        reference_rows: List[int] = []
        if count > 1:
            reference_rows = self.__select_reference_rows(relation, rownum)
        if count == 0:
            note = 'no version of resource "%s" is valid at the row moment'
            Class = errors.TemporalKeyMissingError
        else:
            note = '%s versions of resource "%s" are valid at the row moment'
            Class = errors.TemporalKeyAmbiguousError
        note = (
            note % (relation.dim_resource)
            if count == 0
            else note
            % (
                count,
                relation.dim_resource,
            )
        )
        error = Class(
            note=note,
            cells=[],
            row_number=rownum,
            field_names=list(relation.fact_fields),
            reference_resource=relation.dim_resource,
            match_count=count,
            reference_rows=reference_rows,
        )
        return error

    def __select_reference_rows(
        self, relation: TemporalRelation, rownum: int
    ) -> List[int]:
        """Select up to two matching version row numbers (ascending)"""
        assert self.db is not None
        width = len(relation.fact_fields)
        keys = ", ".join(f"k{i}" for i in range(width))
        row = self.db.execute(
            f"SELECT {keys}, at FROM {relation.fact_table} WHERE rownum = ?",
            (rownum,),
        ).fetchone()
        if row is None:
            return []
        marks = " AND ".join(f"k{i} = ?" for i in range(width))
        sql = (
            f"SELECT rownum FROM {relation.dim_table} "
            f"WHERE {marks} AND vfrom <= ? AND (vto IS NULL OR vto > ?) "
            "ORDER BY rownum LIMIT 2"
        )
        params = list(row[:width]) + [row[width], row[width]]
        return [item[0] for item in self.db.execute(sql, params)]

    # Cleanup

    def cleanup(self):
        """Close the staging database and remove the temporary content"""
        if self.db is not None:
            self.db.close()
            self.db = None
        if self.tempdir is not None:
            self.tempdir.cleanup()
            self.tempdir = None

    def __enter__(self):
        return self

    def __exit__(self, *args: Any):
        self.cleanup()
