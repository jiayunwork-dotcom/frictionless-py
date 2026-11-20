from __future__ import annotations

from typing import List

import attrs

from .row import RowError


@attrs.define(kw_only=True)
class TemporalError(RowError):
    """Temporal error representation.

    A base class for the errors related to temporal foreign keys.
    Temporal errors are emitted after the involved resources have been
    read, so the original row cells are not retained; only the row
    number and the involved field names are reported.
    """

    type = "temporal-error"
    title = "Temporal Error"
    description = "There is a temporal foreign key error."
    template = 'Temporal error at row "{rowNumber}": {note}'
    tags = ["#table", "#row", "#temporal"]

    field_names: List[str]
    """
    Names of the fields taking part in the temporal relation
    on the side of the resource that owns the error.
    """

    # Metadata

    metadata_profile_patch = {
        "properties": {
            "fieldNames": {"type": "array", "items": {"type": "string"}},
        },
    }


class TemporalTimeError(TemporalError):
    type = "temporal-time"
    title = "Temporal Time Error"
    description = (
        "The temporal moment of a fact row is missing or is not comparable. "
        "For datetime fields an explicit timezone is required."
    )
    template = 'Row at position "{rowNumber}" has an invalid temporal moment: {note}'


class TemporalIntervalError(TemporalError):
    type = "temporal-interval"
    title = "Temporal Interval Error"
    description = (
        "The validity interval of a version row is not usable. The interval "
        "start is required, boundaries of datetime fields need an explicit "
        "timezone and the start must be earlier than the end."
    )
    template = 'Row at position "{rowNumber}" has an invalid temporal interval: {note}'


@attrs.define(kw_only=True)
class TemporalKeyError(TemporalError):
    """Temporal key error representation.

    A base class for temporal foreign key lookup errors. It carries the
    exact amount of matching version rows and up to two of their row
    numbers (ascending).
    """

    type = "temporal-key-error"
    title = "Temporal Key Error"
    description = "There is a temporal foreign key lookup error."
    template = 'Row at position "{rowNumber}" violates the temporal foreign key: {note}'
    tags = ["#table", "#row", "#temporal"]

    reference_resource: str
    """
    Name of the referenced resource holding the version rows.
    """

    match_count: int
    """
    Exact amount of version rows matching the fact row at its moment.
    """

    reference_rows: List[int]
    """
    Up to two row numbers (ascending) of the matching version rows.
    """

    # Metadata

    metadata_profile_patch = {
        "properties": {
            "referenceResource": {"type": "string"},
            "matchCount": {"type": "integer"},
            "referenceRows": {"type": "array", "items": {"type": "integer"}},
        },
    }


class TemporalKeyMissingError(TemporalKeyError):
    type = "temporal-key-missing"
    title = "Temporal Key Missing Error"
    description = (
        "No version row is valid at the moment of the fact row "
        "for the temporal foreign key."
    )
    template = 'Row at position "{rowNumber}" violates the temporal foreign key: {note}'


class TemporalKeyAmbiguousError(TemporalKeyError):
    type = "temporal-key-ambiguous"
    title = "Temporal Key Ambiguous Error"
    description = (
        "More than one version row is valid at the moment of the fact row "
        "for the temporal foreign key."
    )
    template = 'Row at position "{rowNumber}" violates the temporal foreign key: {note}'
