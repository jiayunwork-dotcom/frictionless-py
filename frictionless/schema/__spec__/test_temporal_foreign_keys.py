import pytest

from frictionless import FrictionlessException, Schema

# General


def test_schema_temporal_foreign_keys_default():
    schema = Schema()
    assert schema.temporal_foreign_keys == []


def test_schema_temporal_foreign_keys_from_descriptor():
    descriptor = {
        "fields": [
            {"name": "product", "type": "string"},
            {"name": "occurred", "type": "datetime"},
        ],
        "temporalForeignKeys": [
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
        ],
    }
    schema = Schema(descriptor)
    assert schema.temporal_foreign_keys == [
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


def test_schema_temporal_foreign_keys_descriptor_round_trip():
    descriptor = {
        "fields": [
            {"name": "product", "type": "string"},
            {"name": "occurred", "type": "date"},
        ],
        "temporalForeignKeys": [
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
    }
    schema = Schema(descriptor)
    assert schema.to_descriptor() == descriptor
    assert Schema(schema.to_descriptor()).to_descriptor() == descriptor


def test_schema_temporal_foreign_keys_to_copy():
    descriptor = {
        "fields": [
            {"name": "product", "type": "string"},
            {"name": "occurred", "type": "datetime"},
        ],
        "temporalForeignKeys": [
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
        ],
    }
    schema = Schema(descriptor)
    copied = schema.to_copy()
    assert copied.temporal_foreign_keys == schema.temporal_foreign_keys
    assert copied.to_descriptor() == schema.to_descriptor()


def test_schema_temporal_foreign_keys_not_custom():
    # The declarations must not be silently absorbed as a custom property
    descriptor = {
        "fields": [
            {"name": "product", "type": "string"},
            {"name": "occurred", "type": "datetime"},
        ],
        "temporalForeignKeys": [
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
    }
    schema = Schema(descriptor)
    assert "temporalForeignKeys" not in schema.custom
    assert schema.to_descriptor()["temporalForeignKeys"]


def test_schema_temporal_foreign_keys_shorthand_normalized():
    schema = Schema(
        {
            "fields": [
                {"name": "product", "type": "string"},
                {"name": "occurred", "type": "date"},
            ],
            "temporalForeignKeys": [
                {
                    "fields": "product",
                    "at": "occurred",
                    "reference": {
                        "resource": "versions",
                        "fields": "product",
                        "validFrom": "begin",
                    },
                }
            ],
        }
    )
    assert schema.temporal_foreign_keys[0]["fields"] == ["product"]
    assert schema.temporal_foreign_keys[0]["reference"]["fields"] == ["product"]


# Validate


def make_schema(temporal_foreign_keys, *, self_reference=False):
    fields = [
        {"name": "product", "type": "string"},
        {"name": "occurred", "type": "datetime"},
    ]
    if self_reference:
        fields += [
            {"name": "begin", "type": "datetime"},
            {"name": "end", "type": "datetime"},
        ]
    return {
        "fields": fields,
        "temporalForeignKeys": temporal_foreign_keys,
    }


def make_reference(resource="versions", fields=None, valid_from="begin", valid_to="end"):
    reference = {"resource": resource, "fields": fields or ["product"]}
    if valid_from is not None:
        reference["validFrom"] = valid_from
    if valid_to is not None:
        reference["validTo"] = valid_to
    return reference


def test_schema_temporal_foreign_keys_validate_valid():
    descriptor = make_schema(
        [
            {
                "fields": ["product"],
                "at": "occurred",
                "reference": make_reference(),
            }
        ]
    )
    assert Schema.validate_descriptor(descriptor).valid


def test_schema_temporal_foreign_keys_validate_valid_self_reference():
    descriptor = make_schema(
        [
            {
                "fields": ["product"],
                "at": "occurred",
                "reference": make_reference(resource=""),
            }
        ],
        self_reference=True,
    )
    assert Schema.validate_descriptor(descriptor).valid


def test_schema_temporal_foreign_keys_validate_empty_fields():
    descriptor = make_schema(
        [{"fields": [], "at": "occurred", "reference": make_reference()}]
    )
    report = Schema.validate_descriptor(descriptor)
    assert not report.valid
    assert "must not be empty" in report.errors[0].note


def test_schema_temporal_foreign_keys_validate_duplicate_fields():
    descriptor = make_schema(
        [
            {
                "fields": ["product", "product"],
                "at": "occurred",
                "reference": make_reference(fields=["product", "product"]),
            }
        ]
    )
    report = Schema.validate_descriptor(descriptor)
    assert not report.valid
    assert "are not unique" in report.errors[0].note


def test_schema_temporal_foreign_keys_validate_mismatched_sides():
    descriptor = make_schema(
        [
            {
                "fields": ["product", "occurred"],
                "at": "occurred",
                "reference": make_reference(fields=["product"]),
            }
        ]
    )
    report = Schema.validate_descriptor(descriptor)
    assert not report.valid
    assert "do not match the reference fields" in report.errors[0].note


def test_schema_temporal_foreign_keys_validate_missing_at():
    descriptor = make_schema([{"fields": ["product"], "reference": make_reference()}])
    report = Schema.validate_descriptor(descriptor)
    assert not report.valid
    assert 'requires an "at" field' in report.errors[0].note


def test_schema_temporal_foreign_keys_validate_missing_valid_from():
    descriptor = make_schema(
        [
            {
                "fields": ["product"],
                "at": "occurred",
                "reference": make_reference(valid_from=None),
            }
        ]
    )
    report = Schema.validate_descriptor(descriptor)
    assert not report.valid
    assert 'requires a "validFrom" field' in report.errors[0].note


def test_schema_temporal_foreign_keys_validate_unknown_field():
    descriptor = make_schema(
        [
            {
                "fields": ["unknown"],
                "at": "occurred",
                "reference": make_reference(),
            }
        ]
    )
    report = Schema.validate_descriptor(descriptor)
    assert not report.valid
    assert 'field "unknown" does not match' in report.errors[0].note


def test_schema_temporal_foreign_keys_validate_unknown_at():
    descriptor = make_schema(
        [
            {
                "fields": ["product"],
                "at": "unknown",
                "reference": make_reference(),
            }
        ]
    )
    report = Schema.validate_descriptor(descriptor)
    assert not report.valid
    assert '"at" field "unknown" does not match' in report.errors[0].note


def test_schema_temporal_foreign_keys_validate_bad_key_type():
    descriptor = make_schema(
        [
            {
                "fields": ["occurred"],
                "at": "occurred",
                "reference": make_reference(fields=["begin"]),
            }
        ]
    )
    report = Schema.validate_descriptor(descriptor)
    assert not report.valid
    assert "unsupported type" in report.errors[0].note


def test_schema_temporal_foreign_keys_validate_bad_at_type():
    descriptor = make_schema(
        [
            {
                "fields": ["product"],
                "at": "product",
                "reference": make_reference(),
            }
        ]
    )
    report = Schema.validate_descriptor(descriptor)
    assert not report.valid
    assert '"at" field "product" has unsupported type' in report.errors[0].note


def test_schema_temporal_foreign_keys_validate_self_reference_types():
    descriptor = make_schema(
        [
            {
                "fields": ["product"],
                "at": "occurred",
                "reference": make_reference(resource="", valid_from="product"),
            }
        ],
        self_reference=True,
    )
    report = Schema.validate_descriptor(descriptor)
    assert not report.valid
    assert "unsupported type" in report.errors[0].note


def test_schema_temporal_foreign_keys_validate_self_reference_unknown_reference_field():
    descriptor = make_schema(
        [
            {
                "fields": ["product"],
                "at": "occurred",
                "reference": make_reference(resource="", valid_from="unknown"),
            }
        ],
        self_reference=True,
    )
    report = Schema.validate_descriptor(descriptor)
    assert not report.valid
    assert 'reference field "unknown" does not match' in report.errors[0].note


def test_schema_temporal_foreign_keys_from_descriptor_raises():
    descriptor = make_schema(
        [{"fields": [], "at": "occurred", "reference": make_reference()}]
    )
    with pytest.raises(FrictionlessException) as excinfo:
        Schema(descriptor)
    assert excinfo.value.error.type == "schema-error"
    assert excinfo.value.reasons
    assert "must not be empty" in excinfo.value.reasons[0].note


def test_schema_temporal_foreign_keys_multiple_declarations():
    descriptor = make_schema(
        [
            {"fields": ["product"], "at": "occurred", "reference": make_reference()},
            {
                "fields": ["product"],
                "at": "occurred",
                "reference": make_reference(resource="other", valid_to=None),
            },
        ]
    )
    schema = Schema(descriptor)
    assert len(schema.temporal_foreign_keys) == 2
    assert Schema.validate_descriptor(descriptor).valid
