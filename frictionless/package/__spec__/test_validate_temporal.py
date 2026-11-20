import datetime
import random

import pytest

from frictionless import (
    Checklist,
    FrictionlessException,
    Package,
    Report,
    Resource,
    Schema,
)

# Helpers


def make_fact_schema(fields, temporal_foreign_keys, **extra):
    schema = {"fields": fields, "temporalForeignKeys": temporal_foreign_keys}
    schema.update(extra)
    return schema


SALES_FIELDS = [
    {"name": "product", "type": "string"},
    {"name": "occurred", "type": "datetime"},
]

VERSIONS_FIELDS = [
    {"name": "product", "type": "string"},
    {"name": "begin", "type": "datetime"},
    {"name": "end", "type": "datetime"},
]

SALES_TFK = [
    {
        "fields": ["product"],
        "at": "occurred",
        "reference": {
            "resource": "versions",
            "fields": ["product"],
            "validFrom": "begin",
            "validTo": "end",
        },
    }
]


def make_package(
    sales_rows,
    versions_rows,
    *,
    sales_first=True,
    sales_schema=None,
    versions_schema=None,
):
    sales = {
        "name": "sales",
        "data": [["product", "occurred"]] + sales_rows,
        "schema": sales_schema or make_fact_schema(SALES_FIELDS, SALES_TFK),
    }
    versions = {
        "name": "versions",
        "data": [["product", "begin", "end"]] + versions_rows,
        "schema": versions_schema or {"fields": VERSIONS_FIELDS},
    }
    resources = [sales, versions] if sales_first else [versions, sales]
    return {"resources": resources}


def flatten_temporal(report):
    """Flatten temporal errors of all tasks as comparable tuples"""
    result = []
    for task in report.tasks:
        for error in task.errors:
            if error.type.startswith("temporal-"):
                result.append(
                    (
                        task.name,
                        error.type,
                        error.row_number,
                        tuple(error.field_names),
                        getattr(error, "reference_resource", None),
                        getattr(error, "match_count", None),
                        tuple(getattr(error, "reference_rows", [])),
                    )
                )
    return result


# Endpoints (left closed, right open)


def test_temporal_interval_endpoints():
    package = Package(
        make_package(
            sales_rows=[
                ["A", "2023-01-01T00:00:00+00:00"],  # at start -> inside
                ["A", "2023-06-30T23:59:59+00:00"],  # just before end -> inside
                ["A", "2023-07-01T00:00:00+00:00"],  # at end -> outside
                ["A", "2022-12-31T23:59:59+00:00"],  # just before start -> outside
            ],
            versions_rows=[
                ["A", "2023-01-01T00:00:00+00:00", "2023-07-01T00:00:00+00:00"],
            ],
        )
    )
    report = package.validate()
    assert flatten_temporal(report) == [
        ("sales", "temporal-key-missing", 4, ("product",), "versions", 0, ()),
        ("sales", "temporal-key-missing", 5, ("product",), "versions", 0, ()),
    ]


def test_temporal_adjacent_intervals_share_endpoint():
    package = Package(
        make_package(
            sales_rows=[
                ["A", "2023-02-01T00:00:00+00:00"],  # boundary: only the second matches
                ["A", "2023-01-15T00:00:00+00:00"],  # only the first matches
            ],
            versions_rows=[
                ["A", "2023-01-01T00:00:00+00:00", "2023-02-01T00:00:00+00:00"],
                ["A", "2023-02-01T00:00:00+00:00", "2023-03-01T00:00:00+00:00"],
            ],
        )
    )
    report = package.validate()
    assert report.valid


def test_temporal_open_right_boundary():
    package = Package(
        make_package(
            sales_rows=[
                ["A", "2030-01-01T00:00:00+00:00"],  # far future, still valid
                ["B", "2030-01-01T00:00:00+00:00"],  # B has a closed interval
            ],
            versions_rows=[
                ["A", "2023-01-01T00:00:00+00:00", ""],  # empty validTo
                ["B", "2023-01-01T00:00:00+00:00", "2023-07-01T00:00:00+00:00"],
            ],
        )
    )
    report = package.validate()
    assert flatten_temporal(report) == [
        ("sales", "temporal-key-missing", 3, ("product",), "versions", 0, ()),
    ]


def test_temporal_no_valid_to_field():
    # The declaration may omit validTo: every version is open ended
    package = Package(
        make_package(
            sales_rows=[["A", "2030-01-01T00:00:00+00:00"]],
            versions_rows=[["A", "2023-01-01T00:00:00+00:00", ""]],
            sales_schema=make_fact_schema(
                SALES_FIELDS,
                [
                    {
                        "fields": ["product"],
                        "at": "occurred",
                        "reference": {
                            "resource": "versions",
                            "fields": ["product"],
                            "validFrom": "begin",
                        },
                    }
                ],
            ),
        )
    )
    report = package.validate()
    assert report.valid


# Missing and ambiguous


def test_temporal_key_missing_and_ambiguous():
    package = Package(
        make_package(
            sales_rows=[
                ["A", "2023-06-01T00:00:00+00:00"],  # two identical versions
                ["B", "2023-06-01T00:00:00+00:00"],  # no versions at all
                ["C", "2023-06-01T00:00:00+00:00"],  # version expired
            ],
            versions_rows=[
                ["A", "2023-01-01T00:00:00+00:00", "2023-07-01T00:00:00+00:00"],
                ["A", "2023-01-01T00:00:00+00:00", "2023-07-01T00:00:00+00:00"],
                ["C", "2023-01-01T00:00:00+00:00", "2023-02-01T00:00:00+00:00"],
            ],
        )
    )
    report = package.validate()
    assert flatten_temporal(report) == [
        ("sales", "temporal-key-ambiguous", 2, ("product",), "versions", 2, (2, 3)),
        ("sales", "temporal-key-missing", 3, ("product",), "versions", 0, ()),
        ("sales", "temporal-key-missing", 4, ("product",), "versions", 0, ()),
    ]


def test_temporal_duplicate_versions_count_twice():
    # Physically duplicated version rows are two versions, not one
    package = Package(
        make_package(
            sales_rows=[["A", "2023-06-01T00:00:00+00:00"]],
            versions_rows=[
                ["A", "2023-01-01T00:00:00+00:00", "2023-07-01T00:00:00+00:00"],
                ["A", "2023-01-01T00:00:00+00:00", "2023-07-01T00:00:00+00:00"],
            ],
        )
    )
    report = package.validate()
    assert flatten_temporal(report) == [
        ("sales", "temporal-key-ambiguous", 2, ("product",), "versions", 2, (2, 3)),
    ]


def test_temporal_ambiguous_reference_rows_first_two_ascending():
    package = Package(
        make_package(
            sales_rows=[["A", "2023-06-01T00:00:00+00:00"]],
            versions_rows=[
                ["A", "2023-01-01T00:00:00+00:00", "2023-07-01T00:00:00+00:00"],
                ["A", "2023-01-01T00:00:00+00:00", "2023-07-01T00:00:00+00:00"],
                ["A", "2023-01-01T00:00:00+00:00", "2023-07-01T00:00:00+00:00"],
            ],
        )
    )
    report = package.validate()
    errors = flatten_temporal(report)
    assert errors == [
        ("sales", "temporal-key-ambiguous", 2, ("product",), "versions", 3, (2, 3)),
    ]


def test_temporal_overlap_only_matters_at_fact_moment():
    # Overlapping versions are not an error per se
    package = Package(
        make_package(
            sales_rows=[
                ["A", "2023-01-15T00:00:00+00:00"],  # only first version
                ["A", "2023-07-15T00:00:00+00:00"],  # only second version
            ],
            versions_rows=[
                ["A", "2023-01-01T00:00:00+00:00", "2023-07-01T00:00:00+00:00"],
                ["A", "2023-02-01T00:00:00+00:00", "2023-08-01T00:00:00+00:00"],
            ],
        )
    )
    report = package.validate()
    assert report.valid


# Composite keys


def test_temporal_composite_key():
    fields = [
        {"name": "shop", "type": "string"},
        {"name": "product", "type": "integer"},
        {"name": "occurred", "type": "datetime"},
    ]
    tfk = [
        {
            "fields": ["shop", "product"],
            "at": "occurred",
            "reference": {
                "resource": "versions",
                "fields": ["shop", "product"],
                "validFrom": "begin",
                "validTo": "end",
            },
        }
    ]
    versions_fields = [
        {"name": "shop", "type": "string"},
        {"name": "product", "type": "integer"},
        {"name": "begin", "type": "datetime"},
        {"name": "end", "type": "datetime"},
    ]
    package = Package(
        make_package(
            sales_rows=[
                ["s1", 1, "2023-06-01T00:00:00+00:00"],  # match
                ["s2", 1, "2023-06-01T00:00:00+00:00"],  # other shop -> missing
                ["s1", 2, "2023-06-01T00:00:00+00:00"],  # other product -> missing
                ["s1", None, "2023-06-01T00:00:00+00:00"],  # null component -> exempt
                [None, 1, "2023-06-01T00:00:00+00:00"],  # null component -> exempt
            ],
            versions_rows=[
                ["s1", 1, "2023-01-01T00:00:00+00:00", "2023-07-01T00:00:00+00:00"],
            ],
            sales_schema=make_fact_schema(fields, tfk),
            versions_schema={"fields": versions_fields},
        )
    )
    # Patch the headers for the composite layout
    package.resources[0].data[0] = ["shop", "product", "occurred"]
    package.resources[1].data[0] = ["shop", "product", "begin", "end"]
    report = package.validate()
    assert flatten_temporal(report) == [
        ("sales", "temporal-key-missing", 3, ("shop", "product"), "versions", 0, ()),
        ("sales", "temporal-key-missing", 4, ("shop", "product"), "versions", 0, ()),
    ]


# Dates and timezones


def test_temporal_date_fields():
    sales_fields = [
        {"name": "product", "type": "string"},
        {"name": "occurred", "type": "date"},
    ]
    versions_fields = [
        {"name": "product", "type": "string"},
        {"name": "begin", "type": "date"},
        {"name": "end", "type": "date"},
    ]
    tfk = [
        {
            "fields": ["product"],
            "at": "occurred",
            "reference": {
                "resource": "versions",
                "fields": ["product"],
                "validFrom": "begin",
                "validTo": "end",
            },
        }
    ]
    package = Package(
        make_package(
            sales_rows=[
                ["A", "2023-01-01"],  # at start -> inside
                ["A", "2023-07-01"],  # at end -> outside
            ],
            versions_rows=[["A", "2023-01-01", "2023-07-01"]],
            sales_schema=make_fact_schema(sales_fields, tfk),
            versions_schema={"fields": versions_fields},
        )
    )
    report = package.validate()
    assert flatten_temporal(report) == [
        ("sales", "temporal-key-missing", 3, ("product",), "versions", 0, ()),
    ]


def test_temporal_datetime_timezone_instant_comparison():
    # The same instant written with different offsets must behave identically
    package = Package(
        make_package(
            sales_rows=[
                ["A", "2023-06-01T02:00:00+02:00"],  # == 2023-06-01T00:00:00Z
                ["A", "2023-06-30T22:00:00-02:00"],  # == 2023-07-01T00:00:00Z (end)
            ],
            versions_rows=[
                ["A", "2023-01-01T00:00:00+00:00", "2023-07-01T00:00:00+00:00"],
            ],
        )
    )
    report = package.validate()
    assert flatten_temporal(report) == [
        ("sales", "temporal-key-missing", 3, ("product",), "versions", 0, ()),
    ]


def test_temporal_datetime_naive_fact_moment():
    package = Package(
        make_package(
            sales_rows=[
                ["A", "2023-06-01T00:00:00"],  # no timezone -> temporal-time
                ["A", ""],  # missing -> temporal-time
            ],
            versions_rows=[
                ["A", "2023-01-01T00:00:00+00:00", "2023-07-01T00:00:00+00:00"],
            ],
        )
    )
    report = package.validate()
    assert flatten_temporal(report) == [
        ("sales", "temporal-time", 2, ("occurred",), None, None, ()),
        ("sales", "temporal-time", 3, ("occurred",), None, None, ()),
    ]


def test_temporal_datetime_naive_interval_boundary():
    package = Package(
        make_package(
            sales_rows=[
                ["A", "2023-06-01T00:00:00+00:00"],
                ["B", "2023-06-01T00:00:00+00:00"],
            ],
            versions_rows=[
                ["A", "2023-01-01T00:00:00", "2023-07-01T00:00:00+00:00"],  # naive begin
                ["B", "2023-01-01T00:00:00+00:00", "2023-07-01T00:00:00"],  # naive end
            ],
        )
    )
    report = package.validate()
    # Naive boundaries exclude the versions from the relation, so the facts
    # of both keys are reported missing as well
    assert flatten_temporal(report) == [
        ("sales", "temporal-key-missing", 2, ("product",), "versions", 0, ()),
        ("sales", "temporal-key-missing", 3, ("product",), "versions", 0, ()),
        ("versions", "temporal-interval", 2, ("begin", "end"), None, None, ()),
        ("versions", "temporal-interval", 3, ("begin", "end"), None, None, ()),
    ]


# Interval errors


def test_temporal_interval_inverted():
    package = Package(
        make_package(
            sales_rows=[["A", "2023-06-01T00:00:00+00:00"]],
            versions_rows=[
                ["A", "2023-07-01T00:00:00+00:00", "2023-01-01T00:00:00+00:00"],
            ],
        )
    )
    report = package.validate()
    assert flatten_temporal(report) == [
        ("sales", "temporal-key-missing", 2, ("product",), "versions", 0, ()),
        ("versions", "temporal-interval", 2, ("begin", "end"), None, None, ()),
    ]


def test_temporal_interval_empty_interval():
    # start == end is not earlier than the end
    package = Package(
        make_package(
            sales_rows=[["A", "2023-06-01T00:00:00+00:00"]],
            versions_rows=[
                ["A", "2023-06-01T00:00:00+00:00", "2023-06-01T00:00:00+00:00"],
            ],
        )
    )
    report = package.validate()
    assert flatten_temporal(report) == [
        ("sales", "temporal-key-missing", 2, ("product",), "versions", 0, ()),
        ("versions", "temporal-interval", 2, ("begin", "end"), None, None, ()),
    ]


def test_temporal_interval_missing_valid_from():
    package = Package(
        make_package(
            sales_rows=[["A", "2023-06-01T00:00:00+00:00"]],
            versions_rows=[["A", "", "2023-07-01T00:00:00+00:00"]],
        )
    )
    report = package.validate()
    assert flatten_temporal(report) == [
        ("sales", "temporal-key-missing", 2, ("product",), "versions", 0, ()),
        ("versions", "temporal-interval", 2, ("begin", "end"), None, None, ()),
    ]


def test_temporal_interval_error_fields_without_valid_to():
    package = Package(
        make_package(
            sales_rows=[["A", "2023-06-01T00:00:00+00:00"]],
            versions_rows=[["A", "", ""]],
            sales_schema=make_fact_schema(
                SALES_FIELDS,
                [
                    {
                        "fields": ["product"],
                        "at": "occurred",
                        "reference": {
                            "resource": "versions",
                            "fields": ["product"],
                            "validFrom": "begin",
                        },
                    }
                ],
            ),
        )
    )
    report = package.validate()
    assert flatten_temporal(report) == [
        ("sales", "temporal-key-missing", 2, ("product",), "versions", 0, ()),
        ("versions", "temporal-interval", 2, ("begin",), None, None, ()),
    ]


# Decode failures


def test_temporal_decode_failure_keeps_type_errors_only():
    package = Package(
        make_package(
            sales_rows=[
                ["A", "not-a-datetime"],  # type error on the fact side
                ["B", "2023-06-01T00:00:00+00:00"],
            ],
            versions_rows=[
                ["A", "2023-01-01T00:00:00+00:00", "2023-07-01T00:00:00+00:00"],
                ["B", "not-a-datetime", "2023-07-01T00:00:00+00:00"],  # type error
            ],
        )
    )
    report = package.validate()
    flattened = [
        (task.name, error.type, error.row_number)
        for task in report.tasks
        for error in task.errors
    ]
    assert flattened == [
        ("sales", "type-error", 2),
        # the broken version row does not take part, so B has no valid version
        ("sales", "temporal-key-missing", 3),
        ("versions", "type-error", 3),
    ]


# Multiple declarations


def test_temporal_multiple_declarations():
    fields = [
        {"name": "product", "type": "string"},
        {"name": "shop", "type": "string"},
        {"name": "occurred", "type": "datetime"},
    ]
    tfks = [
        {
            "fields": ["product"],
            "at": "occurred",
            "reference": {
                "resource": "versions",
                "fields": ["product"],
                "validFrom": "begin",
                "validTo": "end",
            },
        },
        {
            "fields": ["shop"],
            "at": "occurred",
            "reference": {
                "resource": "shops",
                "fields": ["shop"],
                "validFrom": "since",
            },
        },
    ]
    descriptor = {
        "resources": [
            {
                "name": "sales",
                "data": [
                    ["product", "shop", "occurred"],
                    ["A", "x", "2023-06-01T00:00:00+00:00"],  # both ok
                    ["B", "y", "2023-06-01T00:00:00+00:00"],  # both missing
                    ["A", "y", "2023-06-01T00:00:00+00:00"],  # shop missing
                ],
                "schema": make_fact_schema(fields, tfks),
            },
            {
                "name": "versions",
                "data": [
                    ["product", "begin", "end"],
                    ["A", "2023-01-01T00:00:00+00:00", "2023-07-01T00:00:00+00:00"],
                ],
                "schema": {"fields": VERSIONS_FIELDS},
            },
            {
                "name": "shops",
                "data": [
                    ["shop", "since"],
                    ["x", "2023-01-01T00:00:00+00:00"],
                ],
                "schema": {
                    "fields": [
                        {"name": "shop", "type": "string"},
                        {"name": "since", "type": "datetime"},
                    ]
                },
            },
        ]
    }
    report = Package(descriptor).validate()
    assert flatten_temporal(report) == [
        ("sales", "temporal-key-missing", 3, ("product",), "versions", 0, ()),
        ("sales", "temporal-key-missing", 3, ("shop",), "shops", 0, ()),
        ("sales", "temporal-key-missing", 4, ("shop",), "shops", 0, ()),
    ]


# Self reference


def test_temporal_self_reference():
    descriptor = {
        "resources": [
            {
                "name": "items",
                "data": [
                    ["key", "at", "begin", "end"],
                    ["A", "2023-06-01", "2023-01-01", "2023-07-01"],
                    ["B", "2023-06-01", "2023-07-01", "2024-01-01"],
                ],
                "schema": {
                    "fields": [
                        {"name": "key", "type": "string"},
                        {"name": "at", "type": "date"},
                        {"name": "begin", "type": "date"},
                        {"name": "end", "type": "date"},
                    ],
                    "temporalForeignKeys": [
                        {
                            "fields": ["key"],
                            "at": "at",
                            "reference": {
                                "resource": "",
                                "fields": ["key"],
                                "validFrom": "begin",
                                "validTo": "end",
                            },
                        }
                    ],
                },
            }
        ]
    }
    report = Package(descriptor).validate()
    # A is covered by its own version row; B's version starts in July
    assert flatten_temporal(report) == [
        ("items", "temporal-key-missing", 3, ("key",), "items", 0, ()),
    ]


# Resource order and shuffling


def test_temporal_resource_order_does_not_matter():
    sales_rows = [
        ["A", "2023-06-01T00:00:00+00:00"],
        ["B", "2023-06-01T00:00:00+00:00"],
    ]
    versions_rows = [
        ["A", "2023-01-01T00:00:00+00:00", "2023-07-01T00:00:00+00:00"],
        ["A", "2023-01-01T00:00:00+00:00", "2023-07-01T00:00:00+00:00"],
    ]
    first = Package(make_package(sales_rows, versions_rows, sales_first=True)).validate()
    second = Package(
        make_package(sales_rows, versions_rows, sales_first=False)
    ).validate()
    assert flatten_temporal(first) == flatten_temporal(second)
    assert flatten_temporal(first) == [
        ("sales", "temporal-key-ambiguous", 2, ("product",), "versions", 2, (2, 3)),
        ("sales", "temporal-key-missing", 3, ("product",), "versions", 0, ()),
    ]


def test_temporal_shuffled_versions_same_physical_conclusions():
    versions_rows = [
        ["A", "2023-01-01T00:00:00+00:00", "2023-07-01T00:00:00+00:00", "v1"],
        ["A", "2023-01-01T00:00:00+00:00", "2023-07-01T00:00:00+00:00", "v2"],
        ["A", "2023-01-01T00:00:00+00:00", "2023-07-01T00:00:00+00:00", "v3"],
        ["B", "2023-01-01T00:00:00+00:00", "2023-07-01T00:00:00+00:00", "v4"],
    ]
    fields = VERSIONS_FIELDS + [{"name": "vid", "type": "string"}]
    sales_rows = [["A", "2023-06-01T00:00:00+00:00"]]

    def run(rows):
        descriptor = make_package(
            sales_rows,
            [row[:3] for row in rows],
            versions_schema={"fields": VERSIONS_FIELDS},
        )
        # keep the physical identity column out of the relation but in the data
        descriptor["resources"][1]["data"] = [["product", "begin", "end", "vid"]] + [
            list(row) for row in rows
        ]
        descriptor["resources"][1]["schema"] = {"fields": fields}
        report = Package(descriptor).validate()
        (error,) = [
            error
            for task in report.tasks
            for error in task.errors
            if error.type == "temporal-key-ambiguous"
        ]
        # map the reported reference rows back to the physical version ids
        return sorted(rows[row - 2][3] for row in error.reference_rows), error.match_count

    base_vids, base_count = run(versions_rows)
    assert base_vids == ["v1", "v2"]
    assert base_count == 3
    shuffled = versions_rows[::-1]
    shuffled_vids, shuffled_count = run(shuffled)
    # the exact count is stable and the reported reference rows are physical
    # members of the same match set (their positions follow the shuffle)
    assert shuffled_count == base_count
    assert set(shuffled_vids) <= {"v1", "v2", "v3"}
    assert len(shuffled_vids) == 2


# Forward reference (dimension listed after the facts)


def test_temporal_chained_relations():
    # sales -> versions -> regions: a resource can be a fact and a
    # dimension in different relations of the same validation
    descriptor = {
        "resources": [
            {
                "name": "sales",
                "data": [
                    ["product", "occurred"],
                    ["A", "2023-06-01T00:00:00+00:00"],
                ],
                "schema": make_fact_schema(SALES_FIELDS, SALES_TFK),
            },
            {
                "name": "versions",
                "data": [
                    ["product", "begin", "end", "region"],
                    ["A", "2023-01-01T00:00:00+00:00", "2023-07-01T00:00:00+00:00", "eu"],
                    ["A", "2023-07-01T00:00:00+00:00", "2024-01-01T00:00:00+00:00", "xx"],
                ],
                "schema": {
                    "fields": VERSIONS_FIELDS + [{"name": "region", "type": "string"}],
                    "temporalForeignKeys": [
                        {
                            "fields": ["region"],
                            "at": "begin",
                            "reference": {
                                "resource": "regions",
                                "fields": ["region"],
                                "validFrom": "since",
                            },
                        }
                    ],
                },
            },
            {
                "name": "regions",
                "data": [
                    ["region", "since"],
                    ["eu", "2020-01-01T00:00:00+00:00"],
                ],
                "schema": {
                    "fields": [
                        {"name": "region", "type": "string"},
                        {"name": "since", "type": "datetime"},
                    ]
                },
            },
        ]
    }
    report = Package(descriptor).validate()
    # the second version row references the unknown region "xx"
    assert flatten_temporal(report) == [
        ("versions", "temporal-key-missing", 3, ("region",), "regions", 0, ()),
    ]


def test_temporal_dimension_without_schema_is_a_metadata_error():
    descriptor = {
        "resources": [
            {
                "name": "sales",
                "data": [["product", "occurred"], ["A", "2023-06-01T00:00:00+00:00"]],
                "schema": make_fact_schema(SALES_FIELDS, SALES_TFK),
            },
            {
                "name": "versions",
                "data": [["product", "begin", "end"]],
                # no schema: the referenced fields cannot be verified
            },
        ]
    }
    report = Package(descriptor).validate()
    assert not report.valid
    assert report.errors[0].type == "package-error"


def test_temporal_does_not_mutate_package():
    descriptor = make_package(
        sales_rows=[["A", "2023-06-01T00:00:00+00:00"]],
        versions_rows=[["A", "2023-01-01T00:00:00+00:00", "2023-07-01T00:00:00+00:00"]],
    )
    package = Package(descriptor)
    before = package.to_descriptor()
    package.validate()
    assert package.to_descriptor() == before


def test_temporal_forward_referenced_resource():
    descriptor = {
        "resources": [
            {
                "name": "sales",
                "data": [
                    ["product", "occurred"],
                    ["A", "2023-06-01T00:00:00+00:00"],
                ],
                "schema": make_fact_schema(SALES_FIELDS, SALES_TFK),
            },
            {
                "name": "versions",
                "data": [
                    ["product", "begin", "end"],
                    ["A", "2023-01-01T00:00:00+00:00", "2023-07-01T00:00:00+00:00"],
                ],
                "schema": {"fields": VERSIONS_FIELDS},
            },
        ]
    }
    assert Package(descriptor).validate().valid


# Report


def test_temporal_report_error_descriptor_round_trip():
    package = Package(
        make_package(
            sales_rows=[
                ["A", "2023-06-01T00:00:00+00:00"],
                ["B", "2023-06-01T00:00:00+00:00"],
            ],
            versions_rows=[
                ["A", "2023-01-01T00:00:00+00:00", "2023-07-01T00:00:00+00:00"],
                ["A", "2023-01-01T00:00:00+00:00", "2023-07-01T00:00:00+00:00"],
            ],
        )
    )
    report = package.validate()
    descriptor = report.to_descriptor()
    restored = Report.from_descriptor(descriptor)
    assert flatten_temporal(restored) == flatten_temporal(report)
    error = restored.tasks[0].errors[0]
    assert error.type == "temporal-key-ambiguous"
    assert error.reference_resource == "versions"
    assert error.match_count == 2
    assert error.reference_rows == [2, 3]
    assert error.field_names == ["product"]


def test_temporal_errors_in_fact_resource_task():
    package = Package(
        make_package(
            sales_rows=[["B", "2023-06-01T00:00:00+00:00"]],
            versions_rows=[
                ["A", "2023-01-01T00:00:00+00:00", "2023-07-01T00:00:00+00:00"]
            ],
        )
    )
    report = package.validate()
    tasks = {task.name: task for task in report.tasks}
    assert [error.type for error in tasks["sales"].errors] == ["temporal-key-missing"]
    assert tasks["versions"].valid


# Options


def test_temporal_rejects_parallel():
    package = Package(make_package([], []))
    with pytest.raises(FrictionlessException) as excinfo:
        package.validate(parallel=True)
    assert "parallel" in excinfo.value.error.note


def test_temporal_rejects_name():
    package = Package(make_package([], []))
    with pytest.raises(FrictionlessException) as excinfo:
        package.validate(name="sales")
    assert "single resource" in excinfo.value.error.note


def test_temporal_rejects_limit_rows():
    package = Package(make_package([], []))
    with pytest.raises(FrictionlessException) as excinfo:
        package.validate(limit_rows=10)
    assert "limiting rows" in excinfo.value.error.note


def test_temporal_rejections_happen_before_reading():
    consumed = []

    def data():
        consumed.append(True)
        yield ["product", "occurred"]

    package = Package(
        resources=[
            Resource(
                data=data(),
                name="sales",
                schema=Schema(make_fact_schema(SALES_FIELDS, SALES_TFK)),
            )
        ]
    )
    with pytest.raises(FrictionlessException):
        package.validate(limit_rows=1)
    assert consumed == []


def test_temporal_bad_declaration_fails_before_reading():
    consumed = []

    def data():
        consumed.append(True)
        yield ["product", "occurred"]

    package = Package(
        resources=[
            Resource(
                data=data(),
                name="sales",
                schema=Schema(
                    {
                        "fields": SALES_FIELDS,
                        "temporalForeignKeys": [
                            {
                                "fields": ["product"],
                                "at": "occurred",
                                "reference": {
                                    "resource": "unknown",
                                    "fields": ["product"],
                                    "validFrom": "begin",
                                },
                            }
                        ],
                    }
                ),
            )
        ]
    )
    report = package.validate()
    assert consumed == []
    assert not report.valid
    assert report.errors[0].type == "package-error"
    assert 'resource "unknown" does not exist' in report.errors[0].note


# One-shot sources


def test_temporal_one_shot_sources():
    def sales_data():
        yield ["product", "occurred"]
        yield ["A", "2023-06-01T00:00:00+00:00"]
        yield ["B", "2023-06-01T00:00:00+00:00"]

    def versions_data():
        yield ["product", "begin", "end"]
        yield ["A", "2023-01-01T00:00:00+00:00", "2023-07-01T00:00:00+00:00"]

    package = Package(
        resources=[
            Resource(
                data=sales_data(),
                name="sales",
                schema=Schema(make_fact_schema(SALES_FIELDS, SALES_TFK)),
            ),
            Resource(
                data=versions_data(),
                name="versions",
                schema=Schema({"fields": VERSIONS_FIELDS}),
            ),
        ]
    )
    report = package.validate()
    assert flatten_temporal(report) == [
        ("sales", "temporal-key-missing", 3, ("product",), "versions", 0, ()),
    ]


# Regular foreign keys coexistence


def test_temporal_with_regular_foreign_keys():
    descriptor = {
        "resources": [
            {
                "name": "sales",
                "data": [
                    ["product", "occurred", "seller"],
                    ["A", "2023-06-01T00:00:00+00:00", 1],
                    ["B", "2023-06-01T00:00:00+00:00", 99],
                ],
                "schema": {
                    "fields": SALES_FIELDS + [{"name": "seller", "type": "integer"}],
                    "foreignKeys": [
                        {
                            "fields": "seller",
                            "reference": {"resource": "sellers", "fields": "id"},
                        }
                    ],
                    "temporalForeignKeys": SALES_TFK,
                },
            },
            {
                "name": "versions",
                "data": [
                    ["product", "begin", "end"],
                    ["A", "2023-01-01T00:00:00+00:00", "2023-07-01T00:00:00+00:00"],
                ],
                "schema": {"fields": VERSIONS_FIELDS},
            },
            {
                "name": "sellers",
                "data": [["id", "name"], [1, "shop"]],
                "schema": {
                    "fields": [
                        {"name": "id", "type": "integer"},
                        {"name": "name", "type": "string"},
                    ]
                },
            },
        ]
    }
    report = Package(descriptor).validate()
    flattened = [
        (task.name, error.type, error.row_number)
        for task in report.tasks
        for error in task.errors
    ]
    assert flattened == [
        ("sales", "foreign-key", 3),
        ("sales", "temporal-key-missing", 3),
    ]


def test_temporal_with_primary_key_check():
    descriptor = {
        "resources": [
            {
                "name": "sales",
                "data": [
                    ["product", "occurred"],
                    ["A", "2023-06-01T00:00:00+00:00"],
                    ["A", "2023-06-01T00:00:00+00:00"],
                ],
                "schema": make_fact_schema(
                    SALES_FIELDS, SALES_TFK, primaryKey=["product"]
                ),
            },
            {
                "name": "versions",
                "data": [
                    ["product", "begin", "end"],
                    ["A", "2023-01-01T00:00:00+00:00", "2023-07-01T00:00:00+00:00"],
                ],
                "schema": {"fields": VERSIONS_FIELDS},
            },
        ]
    }
    report = Package(descriptor).validate()
    assert [error.type for error in report.tasks[0].errors] == ["primary-key"]


# Limit errors


def test_temporal_limit_errors():
    sales_rows = [["K%s" % index, "2023-06-01T00:00:00+00:00"] for index in range(10)]
    package = Package(make_package(sales_rows, []))
    report = package.validate(limit_errors=3)
    task = report.tasks[0]
    assert len(task.errors) == 3
    assert [error.type for error in task.errors] == ["temporal-key-missing"] * 3
    assert [error.row_number for error in task.errors] == [2, 3, 4]
    assert "reached error limit: 3" in task.warnings


def test_temporal_limit_errors_does_not_truncate_dimension_reads():
    # The fact resource comes first and hits the error limit immediately;
    # the dimension resource listed afterwards is still read completely
    sales_rows = [["K%s" % index, "2023-06-01T00:00:00+00:00"] for index in range(10)]
    sales_rows.append(["A", "2023-06-01T00:00:00+00:00"])
    versions_rows = [["A", "2023-01-01T00:00:00+00:00", "2023-07-01T00:00:00+00:00"]] * 5
    package = Package(make_package(sales_rows, versions_rows))
    report = package.validate(limit_errors=3)
    sales_task = report.tasks[0]
    assert len(sales_task.errors) == 3
    # the last fact row (A) is beyond the limit but the join stayed exact:
    # it is not among the saved errors because the limit was already reached
    assert all(error.row_number <= 4 for error in sales_task.errors)
    # the dimension resource was read to the end despite the error limit
    assert report.tasks[1].stats["rows"] == 5


def test_temporal_limit_errors_on_dimension_task():
    versions_rows = [
        ["A", "", "2023-07-01T00:00:00+00:00"],  # missing validFrom
    ] * 5
    package = Package(
        make_package(
            sales_rows=[["A", "2023-06-01T00:00:00+00:00"]],
            versions_rows=versions_rows,
        )
    )
    report = package.validate(limit_errors=2)
    versions_task = report.tasks[1]
    assert [error.type for error in versions_task.errors] == [
        "temporal-interval",
        "temporal-interval",
    ]
    assert "reached error limit: 2" in versions_task.warnings
    # all the versions were still read
    assert versions_task.stats["rows"] == 5


def test_temporal_limit_errors_with_type_errors():
    descriptor = {
        "resources": [
            {
                "name": "sales",
                "data": [
                    ["product", "occurred"],
                    ["A", "bad"],  # type error (1)
                    ["B", "bad"],  # type error (2)
                    ["C", "2023-06-01T00:00:00+00:00"],  # temporal missing (3)
                    ["D", "2023-06-01T00:00:00+00:00"],  # temporal missing (over)
                ],
                "schema": make_fact_schema(SALES_FIELDS, SALES_TFK),
            },
            {
                "name": "versions",
                "data": [["product", "begin", "end"]],
                "schema": {"fields": VERSIONS_FIELDS},
            },
        ]
    }
    report = Package(descriptor).validate(limit_errors=3)
    task = report.tasks[0]
    assert [(error.type, error.row_number) for error in task.errors] == [
        ("type-error", 2),
        ("type-error", 3),
        ("temporal-key-missing", 4),
    ]
    assert "reached error limit: 3" in task.warnings


# Checklist integration


def test_temporal_checklist_skip_errors():
    package = Package(
        make_package(
            sales_rows=[["B", "2023-06-01T00:00:00+00:00"]],
            versions_rows=[],
        )
    )
    checklist = Checklist(skip_errors=["temporal-key-missing"])
    report = package.validate(checklist=checklist)
    assert report.valid


def test_temporal_checklist_pick_errors():
    package = Package(
        make_package(
            sales_rows=[
                ["A", "bad"],  # type error (filtered out by the pick)
                ["B", "2023-06-01T00:00:00+00:00"],
            ],
            versions_rows=[],
        )
    )
    checklist = Checklist(pick_errors=["temporal-key-missing"])
    report = package.validate(checklist=checklist)
    assert [error.type for error in report.tasks[0].errors] == ["temporal-key-missing"]


# Exceptions and cleanup


def test_temporal_source_exception_is_reported_and_cleaned_up(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))

    def broken():
        yield ["product", "begin", "end"]
        yield ["A", "2023-01-01T00:00:00+00:00", "2023-07-01T00:00:00+00:00"]
        raise RuntimeError("the source is broken")

    package = Package(
        resources=[
            Resource(
                data=[
                    ["product", "occurred"],
                    ["A", "2023-06-01T00:00:00+00:00"],
                ],
                name="sales",
                schema=Schema(make_fact_schema(SALES_FIELDS, SALES_TFK)),
            ),
            Resource(
                data=broken(),
                name="versions",
                schema=Schema({"fields": VERSIONS_FIELDS}),
            ),
        ]
    )
    report = package.validate()
    assert not report.valid
    assert any(error.type == "source-error" for error in report.tasks[1].errors)
    # staging content is cleaned up after the validation
    assert list(tmp_path.glob("frictionless-temporal-*")) == []


def test_temporal_staging_failure_propagates_and_cleans_up(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    import sqlite3

    from frictionless.temporal import engine as engine_module

    real_connect = sqlite3.connect

    def failing_connect(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    package = Package(
        make_package(
            sales_rows=[["A", "2023-06-01T00:00:00+00:00"]],
            versions_rows=[
                ["A", "2023-01-01T00:00:00+00:00", "2023-07-01T00:00:00+00:00"]
            ],
        )
    )
    monkeypatch.setattr(engine_module.sqlite3, "connect", failing_connect)
    with pytest.raises(sqlite3.OperationalError):
        package.validate()
    monkeypatch.setattr(engine_module.sqlite3, "connect", real_connect)
    assert list(tmp_path.glob("frictionless-temporal-*")) == []
    # the next validation works normally
    report = package.validate()
    assert report.valid


def test_temporal_cleanup_after_success(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    package = Package(
        make_package(
            sales_rows=[["A", "2023-06-01T00:00:00+00:00"]],
            versions_rows=[
                ["A", "2023-01-01T00:00:00+00:00", "2023-07-01T00:00:00+00:00"]
            ],
        )
    )
    assert package.validate().valid
    assert list(tmp_path.glob("frictionless-temporal-*")) == []


# Exhaustive small-scale verification


def test_temporal_exhaustive_small_scale():
    """Compare against a brute force join over generated cases"""
    base = datetime.date(2023, 1, 1)

    def day(offset):
        return (base + datetime.timedelta(days=offset)).isoformat()

    candidate_intervals = [
        (0, 2),
        (1, 3),
        (2, 4),
        (3, None),
        (0, 6),
    ]
    keys = ["A", "B"]
    moments = list(range(7))
    rng = random.Random(42)
    cases = 0
    for sample in range(60):
        versions = []
        for key in keys:
            for start, end in candidate_intervals:
                if rng.random() < 0.4:
                    versions.append(
                        [key, day(start), day(end) if end is not None else ""]
                    )
        facts = []
        for key in keys:
            for moment in moments:
                if rng.random() < 0.5:
                    facts.append([key, day(moment)])
        sales_fields = [
            {"name": "product", "type": "string"},
            {"name": "occurred", "type": "date"},
        ]
        versions_fields = [
            {"name": "product", "type": "string"},
            {"name": "begin", "type": "date"},
            {"name": "end", "type": "date"},
        ]
        tfk = [
            {
                "fields": ["product"],
                "at": "occurred",
                "reference": {
                    "resource": "versions",
                    "fields": ["product"],
                    "validFrom": "begin",
                    "validTo": "end",
                },
            }
        ]
        descriptor = {
            "resources": [
                {
                    "name": "sales",
                    "data": [["product", "occurred"]] + facts,
                    "schema": make_fact_schema(sales_fields, tfk),
                },
                {
                    "name": "versions",
                    "data": [["product", "begin", "end"]] + versions,
                    "schema": {"fields": versions_fields},
                },
            ]
        }
        report = Package(descriptor).validate()
        # brute force
        expected = []
        for fact_index, (key, moment) in enumerate(facts, start=2):
            matches = []
            for version_index, (vkey, start, end) in enumerate(versions, start=2):
                if vkey != key or not start:
                    continue
                if end and start >= end:
                    continue
                if start <= moment and (not end or moment < end):
                    matches.append(version_index)
            if len(matches) == 0:
                expected.append(("temporal-key-missing", fact_index, 0, ()))
            elif len(matches) > 1:
                expected.append(
                    (
                        "temporal-key-ambiguous",
                        fact_index,
                        len(matches),
                        tuple(matches[:2]),
                    )
                )
        actual = [
            (error.type, error.row_number, error.match_count, tuple(error.reference_rows))
            for error in report.tasks[0].errors
            if error.type.startswith("temporal-")
        ]
        assert actual == expected, f"case {sample}: {descriptor}"
        cases += 1
    assert cases == 60
