from __future__ import annotations

from typing import Any, Dict, Iterator, List, Optional, cast

# Temporal foreign key declaration (descriptor level):
# {
#     "fields": ["product"],
#     "at": "occurred",
#     "reference": {
#         "resource": "versions",
#         "fields": ["product"],
#         "validFrom": "begin",
#         "validTo": "end",
#     },
# }

KEY_FIELD_TYPES = ["string", "integer"]
TIME_FIELD_TYPES = ["date", "datetime"]


def _as_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return cast(List[Any], value)
    return [value]


def read_temporal_declaration(descriptor: Dict[str, Any]) -> Dict[str, Any]:
    """Read a temporal foreign key declaration into a normalized dict.

    The descriptor is expected to be transformed by
    `Schema.metadata_transform` first, so list/defaults are in place.
    """
    reference: Dict[str, Any] = descriptor.get("reference", {})
    return {
        "fields": _as_list(descriptor.get("fields", [])),
        "at": descriptor.get("at", ""),
        "resource": reference.get("resource", ""),
        "reference_fields": _as_list(reference.get("fields", [])),
        "valid_from": reference.get("validFrom", ""),
        "valid_to": reference.get("validTo", ""),
    }


def validate_temporal_declaration(
    declaration: Dict[str, Any],
    *,
    field_types: Dict[str, str],
    reference_field_types: Optional[Dict[str, str]] = None,
) -> Iterator[str]:
    """Validate a normalized temporal foreign key declaration.

    Parameters:
        declaration: normalized declaration (see read_temporal_declaration)
        field_types: name -> type mapping of the schema owning the declaration
        reference_field_types: name -> type mapping of the referenced schema;
            provide it for self references and at the package level where the
            referenced schema is known

    Yields:
        str: human readable validation notes (empty when valid)
    """
    fields = declaration["fields"]
    at = declaration["at"]
    reference_fields = declaration["reference_fields"]
    valid_from = declaration["valid_from"]
    valid_to = declaration["valid_to"]

    # Structure
    if not fields:
        yield "temporal foreign key fields must not be empty"
    if len(fields) != len(set(fields)):
        note = 'temporal foreign key fields "%s" are not unique'
        yield note % (", ".join(map(str, fields)))
    if not reference_fields:
        yield "temporal foreign key reference fields must not be empty"
    if len(reference_fields) != len(set(reference_fields)):
        note = 'temporal foreign key reference fields "%s" are not unique'
        yield note % (", ".join(map(str, reference_fields)))
    if len(fields) != len(reference_fields):
        note = 'temporal foreign key fields "%s" do not match the reference fields "%s"'
        yield note % (
            ", ".join(map(str, fields)),
            ", ".join(map(str, reference_fields)),
        )
    if not at:
        yield 'temporal foreign key requires an "at" field'
    if not valid_from:
        yield 'temporal foreign key requires a "validFrom" field'

    # Local fields
    for name in fields:
        if name not in field_types:
            note = 'temporal foreign key field "%s" does not match the fields "%s"'
            yield note % (name, ", ".join(field_types))
    if at and at not in field_types:
        note = 'temporal foreign key "at" field "%s" does not match the fields "%s"'
        yield note % (at, ", ".join(field_types))

    # Local types
    for name in fields:
        type = field_types.get(name)
        if type is not None and type not in KEY_FIELD_TYPES:
            note = 'temporal foreign key field "%s" has unsupported type "%s" (only %s are supported)'
            yield note % (name, type, "/".join(KEY_FIELD_TYPES))
    at_type = field_types.get(at)
    if at and at_type is not None and at_type not in TIME_FIELD_TYPES:
        note = 'temporal foreign key "at" field "%s" has unsupported type "%s" (only %s are supported)'
        yield note % (at, at_type, "/".join(TIME_FIELD_TYPES))

    # Reference fields (self reference or package level validation)
    if reference_field_types is not None:
        for name in reference_fields:
            if name not in reference_field_types:
                note = 'temporal foreign key reference field "%s" does not match the fields "%s"'
                yield note % (name, ", ".join(reference_field_types))
        for name in [valid_from, valid_to]:
            if name and name not in reference_field_types:
                note = 'temporal foreign key reference field "%s" does not match the fields "%s"'
                yield note % (name, ", ".join(reference_field_types))
        for name in reference_fields:
            type = reference_field_types.get(name)
            if type is not None and type not in KEY_FIELD_TYPES:
                note = 'temporal foreign key reference field "%s" has unsupported type "%s" (only %s are supported)'
                yield note % (name, type, "/".join(KEY_FIELD_TYPES))
        for index, name in enumerate(fields):
            if index >= len(reference_fields):
                break
            local_type = field_types.get(name)
            remote_type = reference_field_types.get(reference_fields[index])
            if (
                local_type is not None
                and remote_type is not None
                and local_type != remote_type
            ):
                note = 'temporal foreign key field "%s" type "%s" does not match the reference field "%s" type "%s"'
                yield note % (
                    name,
                    local_type,
                    reference_fields[index],
                    remote_type,
                )
        for name in [valid_from, valid_to]:
            if not name:
                continue
            remote_type = reference_field_types.get(name)
            if remote_type is not None and remote_type not in TIME_FIELD_TYPES:
                note = 'temporal foreign key reference field "%s" has unsupported type "%s" (only %s are supported)'
                yield note % (name, remote_type, "/".join(TIME_FIELD_TYPES))
            if (
                at_type is not None
                and remote_type is not None
                and remote_type in TIME_FIELD_TYPES
                and at_type in TIME_FIELD_TYPES
                and remote_type != at_type
            ):
                note = 'temporal foreign key "at" field type "%s" does not match the reference field "%s" type "%s"'
                yield note % (at_type, name, remote_type)


def list_field_types(schema_descriptor: Dict[str, Any]) -> Dict[str, str]:
    """List name -> type for the fields of a schema descriptor"""
    field_types: Dict[str, str] = {}
    for field in schema_descriptor.get("fields", []):
        if isinstance(field, dict) and "name" in field:
            field_descriptor: Dict[str, Any] = field
            field_types[field_descriptor["name"]] = (
                field_descriptor.get("type") or "string"
            )
    return field_types
