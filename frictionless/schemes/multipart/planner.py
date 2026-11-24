from __future__ import annotations

import codecs
import csv
import io
import locale
import sys
from dataclasses import dataclass
from itertools import islice
from typing import TYPE_CHECKING, Any, Iterable, Iterator, List, Optional, Sequence

from ...resources import FileResource

if TYPE_CHECKING:
    from ...dialect import Dialect
    from ...resource import Resource


CSV_FORMATS = {"csv", "tsv"}


@dataclass(frozen=True)
class EncodingInfo:
    """A stateless codec name and the BOM removed from the beginning."""

    codec: str
    bom: bytes


@dataclass(frozen=True)
class LogicalRecord:
    """One CSV logical record and its exact encoded bytes without BOM."""

    number: int
    cells: List[Any]
    data: bytes


class HeaderRegion:
    """The leading records that Dialect treats as header/comment/blank area."""

    def __init__(self, dialect: Dialect):
        self.enabled = bool(dialect.header)
        self.first_content_row = dialect.create_first_content_row()
        self.comment_filter = dialect.create_comment_filter()
        self.blank_filter = dialect.create_blank_filter()

    def contains(self, record: LogicalRecord) -> bool:
        if not self.enabled:
            return False

        row_number = record.number
        cells = record.cells

        # Dialect.read_enumerated_content_stream() skips rows before the first
        # content row unconditionally.
        if row_number < self.first_content_row:
            return True

        # From the first possible content row onwards, only rows filtered by
        # Dialect can extend the repeated header region.
        if self.comment_filter is not None:
            if not self.comment_filter(row_number, cells):
                return True
        if self.blank_filter is not None:
            if not self.blank_filter(cells):
                return True

        return False


def get_csv_control(dialect: Dialect, file_format: Optional[str]):
    # Import locally to keep the multipart scheme import graph narrow.
    from ...formats.csv.control import CsvControl

    control = CsvControl.from_dialect(dialect)
    if file_format == "tsv":
        control.set_not_defined("delimiter", "\t")
    return control


def detect_encoding_info(
    byte_stream: Any,
    encoding: Optional[str],
    *,
    default_codec: Optional[str] = None,
) -> EncodingInfo:
    """Return a BOM-less codec suitable for exact character/byte accounting."""

    raw_encoding = encoding or locale.getpreferredencoding(False) or "utf-8"
    try:
        canonical = codecs.lookup(raw_encoding).name.lower().replace("_", "-")
    except LookupError:
        canonical = "utf-8"

    prefix = b""
    if hasattr(byte_stream, "read"):
        try:
            prefix = byte_stream.read(4) or b""
            if hasattr(byte_stream, "seek"):
                byte_stream.seek(0)
        except Exception:
            prefix = b""

    codec = canonical
    bom = b""

    # Check 32-bit BOMs before 16-bit BOMs because their first two bytes match.
    if canonical in {"utf-32", "utf-32-le", "utf-32-be"}:
        if prefix.startswith(codecs.BOM_UTF32_LE):
            codec = "utf-32-le"
        elif prefix.startswith(codecs.BOM_UTF32_BE):
            codec = "utf-32-be"
        elif canonical == "utf-32":
            byte_order = "le" if sys.byteorder == "little" else "be"
            codec = f"utf-32-{byte_order}"
        else:
            codec = canonical
    elif canonical in {"utf-16", "utf-16-le", "utf-16-be"}:
        if prefix.startswith(codecs.BOM_UTF16_LE):
            codec = "utf-16-le"
        elif prefix.startswith(codecs.BOM_UTF16_BE):
            codec = "utf-16-be"
        elif canonical in {"utf-16-le", "utf-16-be"}:
            codec = canonical
        else:
            # Python's generic utf-16 codec uses the host byte order when it
            # writes a BOM. A BOM-less continuation chunk inherits that order.
            byte_order = "le" if sys.byteorder == "little" else "be"
            codec = default_codec or f"utf-16-{byte_order}"
    elif canonical == "utf-8-sig":
        codec = "utf-8"
    else:
        codec = canonical

    try:
        possible_bom = "\ufeff".encode(codec)
    except (UnicodeEncodeError, LookupError):
        possible_bom = b""
    if possible_bom and prefix.startswith(possible_bom):
        bom = possible_bom

    return EncodingInfo(codec=codec, bom=bom)


class _TrackingPhysicalLines:
    """Physical text lines with their exact encoded byte representations."""

    def __init__(self, text_stream: Any, codec: str):
        self.__text_stream = text_stream
        self.__codec = codec
        self.__line_number = 0
        self.__pending: List[bytes] = []

    def __iter__(self) -> _TrackingPhysicalLines:
        return self

    def __next__(self) -> str:
        line = self.__text_stream.readline()
        if line == "":
            raise StopIteration

        self.__line_number += 1
        if self.__line_number == 1 and line.startswith("\ufeff"):
            # The BOM length is exposed through EncodingInfo.bom. Removing the
            # character here keeps comment/header cell detection BOM-safe.
            line = line[1:]

        self.__pending.append(line.encode(self.__codec))
        return line

    def pop_encoded_line_data(self) -> bytes:
        data = b"".join(self.__pending)
        self.__pending.clear()
        return data


def iter_logical_records(
    byte_stream: Any,
    *,
    dialect: Dialect,
    file_format: Optional[str],
    encoding: Optional[str] = None,
    encoding_info: Optional[EncodingInfo] = None,
) -> Iterator[LogicalRecord]:
    """Yield CSV records using the same CsvControl rules as CsvParser."""

    info = encoding_info or detect_encoding_info(byte_stream, encoding)
    control = get_csv_control(dialect, file_format)
    text_stream = io.TextIOWrapper(byte_stream, encoding=info.codec, newline="")
    lines = _TrackingPhysicalLines(text_stream, info.codec)
    reader = csv.reader(lines, dialect=control.to_python())

    for number, cells in enumerate(reader, start=1):
        yield LogicalRecord(
            number=number,
            cells=cells,
            data=lines.pop_encoded_line_data(),
        )


def get_header_prefix_size(
    path: str,
    *,
    dialect: Dialect,
    file_format: Optional[str],
    encoding: Optional[str],
    default_codec: Optional[str] = None,
) -> int:
    """Return the exact byte length of one shard's repeated header region."""

    if file_format not in CSV_FORMATS or not dialect.header:
        return 0

    with FileResource(path=path) as resource:
        byte_stream = resource.byte_stream
        info = detect_encoding_info(
            byte_stream,
            encoding,
            default_codec=default_codec,
        )
        region = HeaderRegion(dialect)
        size = len(info.bom)

        for record in iter_logical_records(
            byte_stream,
            dialect=dialect,
            file_format=file_format,
            encoding_info=info,
        ):
            if not region.contains(record):
                break
            size += len(record.data)

        return size


def detect_multipart_dialect(resource: Resource, paths: Sequence[str]) -> None:
    """Best-effort equivalent of TableResource's dialect detection.

    Running it before concatenation prevents a repeated comment/header prefix
    from contaminating the sample used by the normal table open sequence.
    """

    if not paths:
        return
    if resource.format not in CSV_FORMATS:
        return

    dialect = resource.dialect
    if not dialect.header:
        return
    if dialect.has_defined("header_rows") or dialect.has_defined("header"):
        return

    try:
        with FileResource(path=paths[0]) as file_resource:
            records = iter_logical_records(
                file_resource.byte_stream,
                dialect=dialect,
                file_format=resource.format,
                encoding=resource.encoding,
            )
            sample = [
                record.cells
                for record in islice(records, resource.detector.sample_size)
            ]
        resource.detector.detect_dialect(sample, dialect=dialect)
    except Exception:
        # The regular read path owns user-facing errors. Dialect detection is
        # only an optimization for comment-prefixed files.
        pass


def plan_csv_chunks(
    byte_stream: Any,
    *,
    chunk_size: int,
    dialect: Dialect,
    file_format: Optional[str],
    encoding: Optional[str],
) -> Iterator[bytes]:
    """Yield complete multipart shards.

    Every shard contains the Dialect header region. Records are never split.
    A record which cannot fit even as the first record of a new shard is still
    emitted alone, allowing that shard to exceed ``chunk_size``.
    """

    if chunk_size <= 0:
        note = f'Multipart chunk_size must be greater than zero, got {chunk_size}'
        # Import here to avoid exposing exception helpers at module import.
        from ...exception import FrictionlessException
        from ...errors import SchemeError

        raise FrictionlessException(SchemeError(note=note))

    info = detect_encoding_info(byte_stream, encoding)
    records = iter_logical_records(
        byte_stream,
        dialect=dialect,
        file_format=file_format,
        encoding_info=info,
    )
    region = HeaderRegion(dialect)

    header_parts: List[bytes] = []
    first_data_record: Optional[LogicalRecord] = None
    for record in records:
        if region.contains(record):
            header_parts.append(record.data)
        else:
            first_data_record = record
            break

    header_data = b"".join(header_parts)

    def reset_chunk(number: int) -> bytearray:
        chunk = bytearray()
        if number == 1 and info.bom:
            chunk.extend(info.bom)
        if header_data:
            chunk.extend(header_data)
        return chunk

    chunk_number = 1
    chunk = reset_chunk(chunk_number)
    data_count = 0

    data_records: Iterable[LogicalRecord]
    if first_data_record is None:
        data_records = ()
    else:
        data_records = _chain_record(first_data_record, records)

    for record in data_records:
        record_size = len(record.data)
        if data_count > 0 and len(chunk) + record_size > chunk_size:
            yield bytes(chunk)
            chunk_number += 1
            chunk = reset_chunk(chunk_number)
            data_count = 0

        chunk.extend(record.data)
        data_count += 1

    # Always write at least the header/BOM-only shard for an empty table.
    if data_count > 0 or chunk:
        yield bytes(chunk)


def _chain_record(
    first: LogicalRecord,
    records: Iterator[LogicalRecord],
) -> Iterator[LogicalRecord]:
    yield first
    yield from records
