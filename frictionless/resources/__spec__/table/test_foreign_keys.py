import csv
import time
import tracemalloc

import pytest

from frictionless import (
    FrictionlessException,
    Package,
    Schema,
    system,
)
from frictionless.resource.lookup import LookupScope
from frictionless.resources import TableResource

# Helpers


def make_schema(*names, foreign_keys=None, types=None):
    types = types or {}
    return {
        "fields": [
            {"name": name, "type": types.get(name, "integer")} for name in names
        ],
        "foreignKeys": foreign_keys or [],
    }


def fk(fields_, resource, ref_fields):
    return {"fields": fields_, "reference": {"resource": resource, "fields": ref_fields}}


def write_csv(path, header, rows):
    with open(path, "w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(header)
        for row in rows:
            writer.writerow(row)


def fk_errors(report):
    errors = []
    for task in report.tasks:
        for error in task.errors:
            if error.type == "foreign-key":
                errors.append(
                    (task.name, error.row_number, error.field_names, error.field_cells)
                )
    return errors


# Read isolation


def test_foreign_key_open_does_not_disturb_open_target_csv(tmp_path):
    write_csv(tmp_path / "b.csv", ["id", "ref"], [[10, ""], [20, ""], [30, ""]])
    write_csv(tmp_path / "a.csv", ["id", "ref"], [[1, 10]])
    package = Package(
        {
            "resources": [
                {"name": "b", "path": "b.csv", "schema": make_schema("id", "ref")},
                {
                    "name": "a",
                    "path": "a.csv",
                    "schema": make_schema(
                        "id", "ref", foreign_keys=[fk(["ref"], "b", ["id"])]
                    ),
                },
            ]
        },
        basepath=str(tmp_path),
    )
    b = package.get_resource("b")
    a = package.get_resource("a")

    # Consume the first row of the target through the caller's stream
    b.open()
    caller_stream = b.row_stream
    assert next(caller_stream).to_list() == [10, None]

    # Establishing the reference must not close, advance or reset the target
    a.open()
    assert not b.closed
    assert [row.to_list() for row in caller_stream] == [[20, None], [30, None]]

    # The referencing resource reads its own rows completely
    assert [row.to_list() for row in a.row_stream] == [[1, 10]]
    a.close()
    b.close()


def test_foreign_key_open_does_not_disturb_open_target_inline():
    package = Package(
        {
            "resources": [
                {
                    "name": "b",
                    "data": [["id", "ref"], [10, None], [20, None], [30, None]],
                    "schema": make_schema("id", "ref"),
                },
                {
                    "name": "a",
                    "data": [["id", "ref"], [1, 10]],
                    "schema": make_schema(
                        "id", "ref", foreign_keys=[fk(["ref"], "b", ["id"])]
                    ),
                },
            ]
        }
    )
    b = package.get_resource("b")
    a = package.get_resource("a")

    b.open()
    caller_stream = b.row_stream
    assert next(caller_stream).to_list() == [10, None]

    # Interleave the reads of the two resources
    a.open()
    assert next(a.row_stream).to_list() == [1, 10]
    assert not b.closed
    assert next(caller_stream).to_list() == [20, None]
    assert next(caller_stream).to_list() == [30, None]
    with pytest.raises(StopIteration):
        next(caller_stream)
    a.close()
    b.close()


def test_foreign_key_lookup_preserves_target_schema_and_cursor(tmp_path):
    write_csv(tmp_path / "b.csv", ["id", "ref"], [[10, ""], [20, ""]])
    write_csv(tmp_path / "a.csv", ["id", "ref"], [[1, 10]])
    package = Package(
        {
            "resources": [
                {
                    "name": "b",
                    "path": "b.csv",
                    "schema": make_schema(
                        "id", "ref", foreign_keys=[fk(["ref"], "a", ["id"])]
                    ),
                },
                {
                    "name": "a",
                    "path": "a.csv",
                    "schema": make_schema(
                        "id", "ref", foreign_keys=[fk(["ref"], "b", ["id"])]
                    ),
                },
            ]
        },
        basepath=str(tmp_path),
    )
    b = package.get_resource("b")
    a = package.get_resource("a")
    declared = list(b.schema.foreign_keys)

    b.open()
    assert next(b.row_stream).to_list() == [10, None]
    a.open()

    # The declared relations and the caller's cursor survive the lookup
    assert b.schema.foreign_keys == declared
    assert next(b.row_stream).to_list() == [20, None]
    a.close()
    b.close()
    assert b.schema.foreign_keys == declared


def test_foreign_key_lookup_rejects_non_replayable_target():
    def generate():
        yield ["id", "ref"]
        yield [10, None]
        yield [20, None]

    b = TableResource(
        name="b",
        data=generate(),
        schema=Schema.from_descriptor(make_schema("id", "ref")),
    )
    a = TableResource(
        name="a",
        data=[["id", "ref"], [1, 10]],
        schema=Schema.from_descriptor(
            make_schema("id", "ref", foreign_keys=[fk(["ref"], "b", ["id"])])
        ),
    )
    package = Package(resources=[b, a])  # noqa: F841 (attaches the resources)

    # The source is rejected clearly instead of being consumed or closed
    report = a.validate()
    assert not report.valid
    error = report.tasks[0].errors[0]
    assert error.type == "resource-error"
    assert "cannot be re-read" in error.note

    # The target's one-shot data was not touched by the failed lookup
    assert b.closed
    assert [row.to_list() for row in b.read_rows()] == [[10, None], [20, None]]


def test_foreign_key_lookup_rejects_non_replayable_self_reference():
    def generate():
        yield ["id", "ref"]
        yield [1, 1]

    resource = TableResource(
        data=generate(),
        schema=Schema.from_descriptor(
            make_schema("id", "ref", foreign_keys=[fk(["ref"], "", ["id"])])
        ),
    )
    report = resource.validate()
    assert not report.valid
    assert report.tasks[0].errors[0].type == "resource-error"
    assert "cannot be re-read" in report.tasks[0].errors[0].note


# Matching semantics


def test_foreign_key_self_reference():
    resource = TableResource(
        data=[["id", "parent"], [1, None], [2, 1], [3, 1], [4, 30]],
        schema=Schema.from_descriptor(
            make_schema("id", "parent", foreign_keys=[fk(["parent"], "", ["id"])])
        ),
    )
    report = resource.validate()
    assert fk_errors(report) == [("memory", 5, ["parent"], ["30"])]


def test_foreign_key_mutual_cycle():
    package = Package(
        {
            "resources": [
                {
                    "name": "a",
                    "data": [["id", "ref"], [1, 10], [2, 20]],
                    "schema": make_schema(
                        "id", "ref", foreign_keys=[fk(["ref"], "b", ["id"])]
                    ),
                },
                {
                    "name": "b",
                    "data": [["id", "ref"], [10, 1], [20, 999]],
                    "schema": make_schema(
                        "id", "ref", foreign_keys=[fk(["ref"], "a", ["id"])]
                    ),
                },
            ]
        }
    )
    report = package.validate()
    assert fk_errors(report) == [("b", 3, ["ref"], ["999"])]


def test_foreign_key_longer_cycle():
    resources = []
    for name, reference, rows in [
        ("a", "b", [[1, 10], [2, 20]]),
        ("b", "c", [[10, 100], [20, 200]]),
        ("c", "a", [[100, 1], [200, 777]]),
    ]:
        resources.append(
            {
                "name": name,
                "data": [["id", "ref"]] + rows,
                "schema": make_schema(
                    "id", "ref", foreign_keys=[fk(["ref"], reference, ["id"])]
                ),
            }
        )
    report = Package({"resources": resources}).validate()
    assert fk_errors(report) == [("c", 3, ["ref"], ["777"])]


def test_foreign_key_shared_target():
    target = {
        "name": "t",
        "data": [["id"], [1], [2]],
        "schema": make_schema("id"),
    }
    referencers = [
        {
            "name": name,
            "data": [["k"]] + [[key] for key in keys],
            "schema": make_schema("k", foreign_keys=[fk(["k"], "t", ["id"])]),
        }
        for name, keys in [("r1", [1, 3]), ("r2", [2, 4])]
    ]
    report = Package({"resources": [target] + referencers}).validate()
    assert fk_errors(report) == [
        ("r1", 3, ["k"], ["3"]),
        ("r2", 3, ["k"], ["4"]),
    ]


def test_foreign_key_composite_and_null_cells():
    package = Package(
        {
            "resources": [
                {
                    "name": "t",
                    "data": [["a", "b"], [1, "x"], [2, "y"]],
                    "schema": make_schema("a", "b", types={"b": "string"}),
                },
                {
                    "name": "r",
                    "data": [
                        ["ka", "kb"],
                        [1, "x"],  # found
                        [1, None],  # partially null: checked as (1, None)
                        [1, "z"],  # not found
                    ],
                    "schema": make_schema(
                        "ka",
                        "kb",
                        types={"kb": "string"},
                        foreign_keys=[fk(["ka", "kb"], "t", ["a", "b"])],
                    ),
                },
            ]
        }
    )
    report = package.validate()
    assert fk_errors(report) == [
        ("r", 3, ["ka", "kb"], ["1", ""]),
        ("r", 4, ["ka", "kb"], ["1", "z"]),
    ]


def test_foreign_key_all_null_cells_are_exempt():
    package = Package(
        {
            "resources": [
                {
                    "name": "t",
                    "data": [["a", "b"], [1, "x"]],
                    "schema": make_schema("a", "b", types={"b": "string"}),
                },
                {
                    "name": "r",
                    "data": [["ka", "kb"], [None, None], [1, "x"]],
                    "schema": make_schema(
                        "ka",
                        "kb",
                        types={"kb": "string"},
                        foreign_keys=[fk(["ka", "kb"], "t", ["a", "b"])],
                    ),
                },
            ]
        }
    )
    report = package.validate()
    assert fk_errors(report) == []


def test_foreign_key_duplicate_target_keys_match_any_row():
    package = Package(
        {
            "resources": [
                {
                    "name": "t",
                    "data": [["id"], [2], [2], [2]],
                    "schema": make_schema("id"),
                },
                {
                    "name": "r",
                    "data": [["k"], [2]],
                    "schema": make_schema("k", foreign_keys=[fk(["k"], "t", ["id"])]),
                },
            ]
        }
    )
    report = package.validate()
    assert fk_errors(report) == []


def test_foreign_key_matches_decoded_values():
    package = Package(
        {
            "resources": [
                {
                    "name": "t",
                    "data": [["id", "day"], [1, "2024-01-01"], [2, "2024-01-02"]],
                    "schema": make_schema("id", "day", types={"day": "date"}),
                },
                {
                    "name": "r",
                    "data": [
                        ["k", "day"],
                        [1, "2024-01-01"],  # found as a decoded date
                        [2, "2024-01-03"],  # decoded but absent
                    ],
                    "schema": make_schema(
                        "k",
                        "day",
                        types={"day": "date"},
                        foreign_keys=[fk(["day"], "t", ["day"])],
                    ),
                },
            ]
        }
    )
    report = package.validate()
    assert fk_errors(report) == [("r", 3, ["day"], ["2024-01-03"])]


def test_foreign_key_target_row_with_own_errors_still_matches():
    package = Package(
        {
            "resources": [
                {
                    "name": "t",
                    "data": [["id", "v"], [1, "bad"]],
                    "schema": make_schema("id", "v"),
                },
                {
                    "name": "r",
                    "data": [["k"], [1]],
                    "schema": make_schema("k", foreign_keys=[fk(["k"], "t", ["id"])]),
                },
            ]
        }
    )
    report = package.validate()
    # The target reports its own type error but the key still exists
    assert fk_errors(report) == []
    assert report.flatten(["rowNumber", "type"]) == [[2, "type-error"]]


# Lifecycle


def test_foreign_key_scope_released_after_package_validate():
    package = Package(
        {
            "resources": [
                {"name": "t", "data": [["id"], [1]], "schema": make_schema("id")},
                {
                    "name": "r",
                    "data": [["k"], [1]],
                    "schema": make_schema("k", foreign_keys=[fk(["k"], "t", ["id"])]),
                },
            ]
        }
    )
    assert package.validate().valid
    assert LookupScope.current() is None
    for resource in package.resources:
        assert resource._TableResource__lookup_scope is None


def test_foreign_key_scope_released_on_early_close():
    package = Package(
        {
            "resources": [
                {"name": "t", "data": [["id"], [1]], "schema": make_schema("id")},
                {
                    "name": "r",
                    "data": [["k"], [1]],
                    "schema": make_schema("k", foreign_keys=[fk(["k"], "t", ["id"])]),
                },
            ]
        }
    )
    resource = package.get_resource("r")
    resource.open()
    assert resource._TableResource__lookup_scope is not None
    resource.close()
    assert resource._TableResource__lookup_scope is None
    assert LookupScope.current() is None


def test_foreign_key_target_read_failure_releases_handles(tmp_path, monkeypatch):
    write_csv(tmp_path / "t.csv", ["id"], [["bad"]])
    write_csv(tmp_path / "r.csv", ["k"], [[1]])
    package = Package(
        {
            "resources": [
                {"name": "t", "path": "t.csv", "schema": make_schema("id")},
                {
                    "name": "r",
                    "path": "r.csv",
                    "schema": make_schema("k", foreign_keys=[fk(["k"], "t", ["id"])]),
                },
            ]
        },
        basepath=str(tmp_path),
    )
    t = package.get_resource("t")
    r = package.get_resource("r")

    # Track the isolated copies created for the key collection
    from frictionless.resource import lookup as lookup_module

    copies = []
    original_create = lookup_module.create_isolated_resource

    def tracking_create(resource):
        copy = original_create(resource)
        copies.append(copy)
        return copy

    monkeypatch.setattr(lookup_module, "create_isolated_resource", tracking_create)

    # Consume one row of the target through the caller's stream
    write_csv(tmp_path / "t.csv", ["id"], [["bad"], [2]])
    t.open()
    caller_stream = t.row_stream
    assert next(caller_stream).to_list() == [None]

    # The isolated target scan fails in the middle of the reading
    with system.use_context(onerror="raise"):
        with pytest.raises(FrictionlessException):
            r.open()
    assert r.closed
    assert r._TableResource__lookup_scope is None
    assert LookupScope.current() is None
    # The handle created for the key collection is closed as well
    assert len(copies) == 1
    assert copies[0].closed

    # The caller's stream on the target is still usable
    with system.use_context(onerror="ignore"):
        rows = [row.to_list() for row in caller_stream]
    assert rows == [[2]]
    t.close()

    # The next legitimate validation works normally
    write_csv(tmp_path / "t.csv", ["id"], [[1]])
    report = package.validate()
    assert report.valid


def test_foreign_key_validation_after_failure():
    package = Package(
        {
            "resources": [
                {"name": "t", "data": [["id"], [1]], "schema": make_schema("id")},
                {
                    "name": "r",
                    "data": [["k"], [2]],
                    "schema": make_schema("k", foreign_keys=[fk(["k"], "t", ["id"])]),
                },
            ]
        }
    )
    # A failing validation (the target is temporarily not re-readable)
    broken = package.get_resource("t")
    real_data = broken.data
    broken.data = iter(real_data)
    report = package.validate()
    assert not report.valid
    assert LookupScope.current() is None

    # A legitimate validation afterwards works and sees fresh data
    broken.data = real_data
    report = package.validate()
    assert fk_errors(report) == [("r", 2, ["k"], ["2"])]


# Direct reading


def test_foreign_key_direct_read_rows():
    package = Package(
        {
            "resources": [
                {"name": "t", "data": [["id"], [1]], "schema": make_schema("id")},
                {
                    "name": "r",
                    "data": [["k"], [1], [2]],
                    "schema": make_schema("k", foreign_keys=[fk(["k"], "t", ["id"])]),
                },
            ]
        }
    )
    t = package.get_resource("t")
    r = package.get_resource("r")
    rows = r.read_rows()
    assert [row.to_list() for row in rows] == [[1], [2]]
    assert rows[0].valid
    assert [error.type for error in rows[1].errors] == ["foreign-key"]
    # The target was not left open and keeps its declared relations
    assert t.closed
    assert t.schema.foreign_keys == []


def test_foreign_key_direct_open_close_cycles_see_fresh_data(tmp_path):
    write_csv(tmp_path / "t.csv", ["id"], [[1]])
    write_csv(tmp_path / "r.csv", ["k"], [[2]])
    package = Package(
        {
            "resources": [
                {"name": "t", "path": "t.csv", "schema": make_schema("id")},
                {
                    "name": "r",
                    "path": "r.csv",
                    "schema": make_schema("k", foreign_keys=[fk(["k"], "t", ["id"])]),
                },
            ]
        },
        basepath=str(tmp_path),
    )
    r = package.get_resource("r")

    report = r.validate()
    assert fk_errors(report) == [("r", 2, ["k"], ["2"])]

    # The next opening collects the keys again from the updated target
    write_csv(tmp_path / "t.csv", ["id"], [[2]])
    report = r.validate()
    assert fk_errors(report) == []


# Source tracking


def test_foreign_key_error_source_points_to_referencing_record(tmp_path):
    write_csv(tmp_path / "t.csv", ["id"], [[1]])
    write_csv(tmp_path / "r.csv", ["k"], [[1], [2]])
    package = Package(
        {
            "resources": [
                {"name": "t", "path": "t.csv", "schema": make_schema("id")},
                {
                    "name": "r",
                    "path": "r.csv",
                    "dialect": {"csv": {"trackSource": True}},
                    "schema": make_schema("k", foreign_keys=[fk(["k"], "t", ["id"])]),
                },
            ]
        },
        basepath=str(tmp_path),
    )
    report = package.validate()
    error = report.tasks[1].errors[0]
    assert error.type == "foreign-key"
    # The dangling value is on the third physical line of r.csv
    assert error.to_descriptor()["source"] == {"record": [[3, 1], [3, 2]]}


def test_foreign_key_values_without_source_tracking(tmp_path):
    write_csv(tmp_path / "t.csv", ["id"], [[1]])
    write_csv(tmp_path / "r.csv", ["k"], [[1], [2]])
    package = Package(
        {
            "resources": [
                {"name": "t", "path": "t.csv", "schema": make_schema("id")},
                {
                    "name": "r",
                    "path": "r.csv",
                    "schema": make_schema("k", foreign_keys=[fk(["k"], "t", ["id"])]),
                },
            ]
        },
        basepath=str(tmp_path),
    )
    report = package.validate()
    error = report.tasks[1].errors[0]
    assert error.type == "foreign-key"
    assert error.cells == ["2"]
    assert "source" not in error.to_descriptor()


# Collection boundary


def test_foreign_key_merged_collection_counts(tmp_path):
    # A target shared by several referencers is scanned once per validation
    write_csv(tmp_path / "t.csv", ["id", "code"], [[i, f"c{i}"] for i in range(100)])
    resources = [
        {
            "name": "t",
            "path": "t.csv",
            "schema": make_schema("id", "code", types={"code": "string"}),
        }
    ]
    for index, key in enumerate(["id", "code", "id"], start=1):
        resources.append(
            {
                "name": f"r{index}",
                "path": "t.csv",
                "schema": make_schema(
                    "id",
                    "code",
                    types={"code": "string"},
                    foreign_keys=[fk([key], "t", [key])],
                ),
            }
        )
    package = Package({"resources": resources}, basepath=str(tmp_path))

    collections = []
    original = LookupScope._collect

    def counting_collect(self, plan):
        collections.append(plan.target.name)
        return original(self, plan)

    LookupScope._collect = counting_collect
    try:
        report = package.validate()
    finally:
        LookupScope._collect = original
    assert report.valid
    # All the key combinations of the target are collected in a single scan
    assert collections == ["t"]


def test_foreign_key_standalone_validations_have_own_boundaries(tmp_path):
    write_csv(tmp_path / "t.csv", ["id"], [[1], [2], [3]])
    package = Package(
        {
            "resources": [
                {"name": "t", "path": "t.csv", "schema": make_schema("id")},
                {
                    "name": "r1",
                    "path": "t.csv",
                    "schema": make_schema("id", foreign_keys=[fk(["id"], "t", ["id"])]),
                },
                {
                    "name": "r2",
                    "path": "t.csv",
                    "schema": make_schema("id", foreign_keys=[fk(["id"], "t", ["id"])]),
                },
            ]
        },
        basepath=str(tmp_path),
    )
    collections = []
    original = LookupScope._collect

    def counting_collect(self, plan):
        collections.append(plan.target.name)
        return original(self, plan)

    LookupScope._collect = counting_collect
    try:
        package.get_resource("r1").validate()
        package.get_resource("r2").validate()
        # A direct Resource.validate never reuses a previous run's keys
        package.validate()
    finally:
        LookupScope._collect = original
    assert collections == ["t", "t", "t"]


def test_foreign_key_merged_collection_performance(tmp_path):
    # Record the actual cost of the chosen sharing boundary: one isolated
    # scan of a shared target per Package.validate instead of one per
    # referencing resource
    row_count = 20000
    write_csv(tmp_path / "t.csv", ["id"], [[i] for i in range(row_count)])
    resources = [
        {"name": "t", "path": "t.csv", "schema": make_schema("id")},
    ]
    for index in range(3):
        resources.append(
            {
                "name": f"r{index}",
                "path": "t.csv",
                "schema": make_schema("id", foreign_keys=[fk(["id"], "t", ["id"])]),
            }
        )

    def make_package():
        return Package({"resources": resources}, basepath=str(tmp_path))

    collections = []
    opens = []
    original_collect = LookupScope._collect
    original_open = TableResource.open

    def counting_collect(self, plan):
        collections.append(plan.target.name)
        return original_collect(self, plan)

    def counting_open(self):
        opens.append(self.name)
        return original_open(self)

    # Merged: one Package.validate
    LookupScope._collect = counting_collect
    TableResource.open = counting_open
    try:
        tracemalloc.start()
        started = time.perf_counter()
        report = make_package().validate()
        merged_time = time.perf_counter() - started
        merged_peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
    finally:
        LookupScope._collect = original_collect
        TableResource.open = original_open
    assert report.valid
    merged_collections = list(collections)
    merged_opens = opens.count("t")
    del opens[:]

    # Separate: one Resource.validate per referencer (own boundary each)
    collections.clear()
    LookupScope._collect = counting_collect
    TableResource.open = counting_open
    try:
        tracemalloc.start()
        started = time.perf_counter()
        separate_package = make_package()
        for name in ["r0", "r1", "r2"]:
            assert separate_package.get_resource(name).validate().valid
        separate_time = time.perf_counter() - started
        separate_peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
    finally:
        LookupScope._collect = original_collect
        TableResource.open = original_open
    separate_opens = opens.count("t")

    merged_rows = merged_collections.count("t") * row_count
    separate_rows = collections.count("t") * row_count
    print(
        "\nlookup collection cost:"
        f"\n  merged    : opens={merged_opens} collections={merged_collections.count('t')}"
        f" rows={merged_rows} time={merged_time:.3f}s peak={merged_peak / 1e6:.1f}MB"
        f"\n  separate  : opens={separate_opens} collections={collections.count('t')}"
        f" rows={separate_rows} time={separate_time:.3f}s peak={separate_peak / 1e6:.1f}MB"
    )
    # The target is opened twice per merged validation (one isolated key
    # scan plus its own validation read) instead of once per referencer
    assert merged_collections == ["t"]
    assert collections == ["t", "t", "t"]
    assert merged_opens == 2
    assert separate_opens == 3
    assert merged_rows == row_count
    assert separate_rows == 3 * row_count
