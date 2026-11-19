"""Source position tracking for the CSV parser.

The standard library's ``csv.reader`` returns decoded cell values without any
position information and buffers one physical line ahead, which makes external
synchronization impossible. This module therefore implements
:class:`TrackingCsvReader`, a self-contained streaming parser whose state
machine mirrors the CPython ``csv`` module while emitting, for every logical
record, both the cell values (identical to ``csv.reader`` output) and the
record/field source positions in a single pass.

The decoded text is consumed exactly once: each physical line goes through the
state machine character by character, and only integer offsets, field split
points and the records callers still hold are retained -- the source text
itself is never accumulated.

Positions are 1-based and counted in Unicode code points of the decoded text
after a leading BOM has been removed. A position range is half open
(``[start, end)``); the line terminator closing a record is never part of the
record's range.
"""

from __future__ import annotations

from collections import deque
from typing import Any, Deque, Dict, Iterable, List, Optional, Tuple

# Parser states (mirror CPython Modules/_csv.c)
_START_RECORD = 0
_START_FIELD = 1
_IN_FIELD = 2
_IN_QUOTED_FIELD = 3
_ESCAPE_IN_FIELD = 4
_ESCAPE_IN_QUOTED_FIELD = 5
_AFTER_QUOTED_FIELD = 6

BOM = "﻿"


class TrackedCells(list):
    """A cell list carrying the source position metadata of its record."""

    def __init__(self, cells: Iterable[Any], source: Optional[Dict[str, Any]]):
        super().__init__(cells)
        # ``None`` means that source tracking is switched off
        self.source = source


class TrackingCsvReader:
    """Streaming CSV parser with per-record and per-field source positions.

    The state machine and its quoting/escaping rules follow CPython's
    ``Modules/_csv.c`` so that cell values match ``csv.reader`` for the
    dialects supported by the framework (``QUOTE_MINIMAL`` / ``QUOTE_NONE``).
    """

    def __init__(
        self,
        text_stream: Iterable[str],
        *,
        delimiter: str,
        quote_char: str,
        escape_char: Optional[str],
        skip_initial_space: bool,
        double_quote: bool,
    ):
        self.__text_stream = iter(text_stream)
        self.__delimiter = delimiter
        self.__quote_char = quote_char
        self.__escape_char = escape_char
        self.__skip_initial_space = skip_initial_space
        self.__double_quote = double_quote
        self.__quoting_enabled = bool(quote_char)

        # Current position in the decoded (BOM-less) text, 1-based
        self.__line = 1
        self.__column = 1
        self.__last_was_cr = False
        self.__bom_skipped = False

        # Parser state
        self.__state = _START_RECORD
        self.__field: List[str] = []
        self.__fields: List[str] = []

        # Position state
        self.__record_start: Optional[Tuple[int, int]] = None
        self.__record_end: Optional[Tuple[int, int]] = None
        self.__field_start: Optional[Tuple[int, int]] = None
        self.__field_starts: List[Tuple[int, int]] = []
        self.__splits: List[Tuple[int, int]] = []

        self.__ready: Deque[TrackedCells] = deque()
        self.__stream_closed = False

    def __iter__(self) -> "TrackingCsvReader":
        return self

    def __next__(self) -> TrackedCells:
        while not self.__ready:
            if self.__stream_closed:
                raise StopIteration
            try:
                line = next(self.__text_stream)
            except StopIteration:
                self.__close_stream()
                continue
            if not self.__bom_skipped:
                self.__bom_skipped = True
                if line.startswith(BOM):
                    # The codec strips the BOM before parsing; positions start
                    # after it. The remaining line is processed normally so a
                    # BOM immediately followed by a terminator is an empty record.
                    line = line[1:]
                    self.__column = 1
                    self.__last_was_cr = False
                    # Begin a fresh record so the first character after the
                    # BOM is treated as a record start (a leading quote opens
                    # a quoted field exactly as without a BOM)
                    if line and line not in ("\n", "\r", "\r\n"):
                        position = self.__position()
                        self.__begin_record(position)
                        self.__state = _START_FIELD
            self.__consume(line)
        return self.__ready.popleft()

    def __close_stream(self) -> None:
        self.__stream_closed = True
        # A final record without a line terminator is still open
        if self.__state != _START_RECORD:
            self.__fields.append("".join(self.__field))
            self.__record_end = self.__position()
            self.__emit_record()

    # Position helpers

    def __position(self) -> Tuple[int, int]:
        return (self.__line, self.__column)

    def __advance(self, char: str) -> None:
        if char == "\r":
            self.__line += 1
            self.__column = 1
            self.__last_was_cr = True
        elif char == "\n":
            if self.__last_was_cr:
                # LF completing a CRLF pair: one line break total
                self.__last_was_cr = False
            else:
                self.__line += 1
                self.__column = 1
        else:
            self.__column += 1
            self.__last_was_cr = False

    # State machine

    def __consume(self, line: str) -> None:
        for char in line:
            self.__step(char)

    def __begin_record(self, position: Tuple[int, int]) -> None:
        self.__record_start = position
        self.__record_end = None
        self.__field_start = position
        self.__field_starts = [position]
        self.__splits = []
        self.__field = []
        self.__fields = []

    def __begin_field(self, position: Tuple[int, int]) -> None:
        # ``position`` is already the first code point of the new field
        self.__field_start = position
        self.__field_starts.append(position)
        self.__field = []

    def __next_position(self, position: Tuple[int, int]) -> Tuple[int, int]:
        # Delimiters never span physical lines, so the next code point is on
        # the same line with the column advanced by one
        return (position[0], position[1] + 1)

    def __save_field(self) -> None:
        self.__fields.append("".join(self.__field))

    def __step(self, char: str) -> None:
        state = self.__state
        delimiter = self.__delimiter
        quote_char = self.__quote_char
        escape_char = self.__escape_char

        is_line_break = char in ("\r", "\n")

        # An escape character makes the following character literal data,
        # including a line terminator (the escape itself stays in the source)
        if is_line_break and state in (_ESCAPE_IN_FIELD, _ESCAPE_IN_QUOTED_FIELD):
            self.__field.append(char)
            self.__state = (
                _IN_QUOTED_FIELD
                if state == _ESCAPE_IN_QUOTED_FIELD
                else _IN_FIELD
            )
            self.__advance(char)
            return

        # Line breaks inside a quoted field are data; otherwise they end a record
        if is_line_break and state != _IN_QUOTED_FIELD:
            if state == _START_RECORD:
                if char == "\n" and self.__last_was_cr:
                    # LF completing a CRLF pair: not a record of its own
                    self.__advance(char)
                    return
                # A zero-length record made of one line terminator: csv.reader
                # represents it as a record with zero fields
                position = self.__position()
                self.__ready.append(
                    TrackedCells(
                        [],
                        copy_source(
                            {"record": [list(position), list(position)], "cells": []}
                        ),
                    )
                )
                self.__record_start = None
                self.__record_end = None
                self.__field_start = None
                self.__field_starts = []
                self.__splits = []
                self.__fields = []
                self.__field = []
                self.__state = _START_RECORD
                self.__advance(char)
                return
            self.__save_field()
            self.__record_end = self.__position()
            self.__emit_record()
            self.__advance(char)
            self.__state = _START_RECORD
            return

        if state == _START_RECORD:
            self.__begin_record(self.__position())
            self.__state = _START_FIELD
            state = _START_FIELD
            # fall through with the same character

        if state == _START_FIELD:
            if self.__skip_initial_space and char == " ":
                # Leading whitespace stays part of the field's source range
                self.__advance(char)
                return
            if self.__quoting_enabled and char == quote_char:
                self.__state = _IN_QUOTED_FIELD
            elif escape_char and char == escape_char:
                self.__state = _ESCAPE_IN_FIELD
            elif char == delimiter:
                # Empty field followed by another field
                delimiter_position = self.__position()
                self.__splits.append(delimiter_position)
                self.__save_field()
                self.__begin_field(self.__next_position(delimiter_position))
                self.__state = _START_FIELD
            else:
                self.__field.append(char)
                self.__state = _IN_FIELD
            self.__advance(char)
            return

        if state == _IN_FIELD:
            if escape_char and char == escape_char:
                self.__state = _ESCAPE_IN_FIELD
            elif char == delimiter:
                delimiter_position = self.__position()
                self.__splits.append(delimiter_position)
                self.__save_field()
                self.__begin_field(self.__next_position(delimiter_position))
                self.__state = _START_FIELD
            else:
                # With quoting disabled, or for ordinary characters (including
                # quote chars outside an opening position), this is literal data
                self.__field.append(char)
            self.__advance(char)
            return

        if state == _ESCAPE_IN_FIELD:
            # The escaped character is always literal data
            self.__field.append(char)
            self.__state = _IN_FIELD
            self.__advance(char)
            return

        if state == _IN_QUOTED_FIELD:
            if escape_char and char == escape_char:
                self.__state = _ESCAPE_IN_QUOTED_FIELD
            elif self.__quoting_enabled and char == quote_char:
                self.__state = _AFTER_QUOTED_FIELD
            else:
                self.__field.append(char)
            self.__advance(char)
            return

        if state == _ESCAPE_IN_QUOTED_FIELD:
            self.__field.append(char)
            self.__state = _IN_QUOTED_FIELD
            self.__advance(char)
            return

        if state == _AFTER_QUOTED_FIELD:
            if self.__double_quote and char == quote_char:
                # Doubled quote inside a quoted field: one literal quote
                self.__field.append(quote_char)
                self.__state = _IN_QUOTED_FIELD
            elif char == delimiter:
                delimiter_position = self.__position()
                self.__splits.append(delimiter_position)
                self.__save_field()
                self.__begin_field(self.__next_position(delimiter_position))
                self.__state = _START_FIELD
            else:
                # CPython switches back to an unquoted field; characters such
                # as whitespace after the closing quote remain part of the value
                self.__field.append(char)
                self.__state = _IN_FIELD
            self.__advance(char)
            return

    def __build_cell_ranges(self) -> List[List[List[int]]]:
        # Field starts are tracked explicitly; cell k ends at delimiter k
        # (excluded) or at the record end for the last cell.
        assert self.__record_start is not None
        assert self.__record_end is not None
        assert self.__field_start is not None
        starts = self.__field_starts
        ends = self.__splits + [self.__record_end]
        return [[list(start), list(end)] for start, end in zip(starts, ends)]

    def __emit_record(self) -> None:
        assert self.__record_start is not None
        assert self.__record_end is not None
        assert self.__field_start is not None
        source = {
            "record": [list(self.__record_start), list(self.__record_end)],
            "cells": self.__build_cell_ranges(),
        }
        self.__ready.append(TrackedCells(self.__fields, copy_source(source)))
        self.__record_start = None
        self.__record_end = None
        self.__field_start = None
        self.__field_starts = []
        self.__splits = []
        self.__fields = []
        self.__field = []


def copy_source(source: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Deep-copy a source descriptor so later mutations cannot reach holders."""
    if source is None:
        return None
    return {
        "record": [list(source["record"][0]), list(source["record"][1])],
        "cells": [[list(point) for point in cell] for cell in source["cells"]],
    }
