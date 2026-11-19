from __future__ import annotations

import csv
import tempfile
from itertools import chain
from typing import TYPE_CHECKING

from ... import errors
from ...exception import FrictionlessException
from ...system import Parser, system
from . import settings
from .control import CsvControl
from .tracker import TrackingCsvReader

if TYPE_CHECKING:
    from ... import types
    from ...resources import TableResource


# Encodings for which decoded positions map unambiguously onto the source text
TRACKABLE_ENCODINGS = ["utf-8", "utf-8-sig"]


class CsvParser(Parser):
    """CSV parser implementation."""

    requires_loader = True
    supported_types = [
        "string",
    ]

    # Read

    def read_cell_stream_create(self):  # type: ignore
        control = CsvControl.from_dialect(self.resource.dialect)
        sample = extract_samle(self.loader.text_stream)
        if self.resource.format == "tsv":
            control.set_not_defined("delimiter", "\t")
        delimiter = control.get_defined("delimiter", default=",\t;|")
        try:
            config = csv.Sniffer().sniff("".join(sample), delimiter)  # type: ignore
        except csv.Error:
            config = csv.excel()
        # We can't rely on this guess as it's can be confused with embedded JSON
        # https://github.com/frictionlessdata/frictionless-py/issues/493
        if config.quotechar == "'":
            config.quotechar = '"'
        control.set_not_defined("delimiter", config.delimiter, distinct=True)
        control.set_not_defined("line_terminator", config.lineterminator, distinct=True)
        control.set_not_defined("escape_char", config.escapechar, distinct=True)
        control.set_not_defined("quote_char", config.quotechar, distinct=True)
        control.set_not_defined(
            "skip_initial_space", config.skipinitialspace, distinct=True
        )
        source = chain(sample, self.loader.text_stream)
        if control.track_source:
            # Position tracking requires a codec whose decoded characters are a
            # faithful representation of the source; reject other encodings
            # explicitly rather than reporting approximate locations
            encoding = (self.resource.encoding or "").lower()
            if encoding not in TRACKABLE_ENCODINGS:
                note = (
                    'source tracking requires "utf-8" or "utf-8-sig" encoding;'
                    f' got "{self.resource.encoding}"'
                )
                raise FrictionlessException(errors.EncodingError(note=note))
            data = TrackingCsvReader(
                source,
                delimiter=control.delimiter,
                quote_char=control.quote_char,
                escape_char=control.escape_char,
                skip_initial_space=control.skip_initial_space,
                double_quote=control.double_quote if not control.escape_char else True,
            )
        else:
            data = csv.reader(source, dialect=control.to_python())  # type: ignore
        yield from data

    # Write

    def write_row_stream(self, source: TableResource):
        options = {}
        control = CsvControl.from_dialect(self.resource.dialect)
        if self.resource.format == "tsv":
            control.set_not_defined("delimiter", "\t")
        for name, value in vars(control.to_python()).items():
            if not name.startswith("_") and value is not None:
                options[name] = value
        with tempfile.NamedTemporaryFile(
            "wt", delete=False, encoding=self.resource.encoding, newline=""
        ) as file:
            writer = csv.writer(file, **options)  # type: ignore
            with source:
                if self.resource.dialect.header:
                    writer.writerow(source.schema.field_names)
                for row in source.row_stream:
                    writer.writerow(row.to_list(types=self.supported_types))  # type: ignore
        loader = system.create_loader(self.resource)
        loader.write_byte_stream(file.name)  # type: ignore


# Internal

SAMPLE_SIZE = 100


def extract_samle(text_stream: types.ITextStream) -> types.ISample:
    sample: types.ISample = []
    while True:
        try:
            sample.append(next(text_stream))  # type: ignore
        except StopIteration:
            break
        if len(sample) >= SAMPLE_SIZE:
            break
    return sample


# System

# https://stackoverflow.com/a/54517177
csv.field_size_limit(settings.FIELD_SIZE_LIMIT)
