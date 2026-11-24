from __future__ import annotations

import io
import os
import tempfile
from typing import Any, List, Optional

from ... import helpers, types
from ...resources import FileResource
from ...system import Loader
from .control import MultipartControl
from .planner import (
    CSV_FORMATS,
    detect_encoding_info,
    detect_multipart_dialect,
    get_csv_control,
    get_header_prefix_size,
    plan_csv_chunks,
)


class MultipartLoader(Loader):
    """Multipart loader implementation."""

    # Read

    def read_byte_stream_create(self):  # type: ignore
        assert self.resource.normpath
        paths = list(self.resource.normpaths)
        detect_multipart_dialect(self.resource, paths)
        return MultipartByteStream(
            paths,
            remote=self.resource.remote,
            resource=self.resource,
        )

    # Write

    def write_byte_stream_save(self, byte_stream: types.IByteStream):
        assert self.resource.normpath
        assert self.resource.path is not None
        resource = self.resource
        control = MultipartControl.from_dialect(resource.dialect)
        physical_paths: List[str] = []
        descriptor_paths: List[str] = []

        if resource.format in CSV_FORMATS:
            chunks = plan_csv_chunks(
                byte_stream,
                chunk_size=control.chunk_size,
                dialect=resource.dialect,
                file_format=resource.format,
                encoding=resource.encoding,
            )
            for chunk in chunks:
                number = len(physical_paths) + 1
                physical_paths.append(self.__write_chunk(chunk, number))
                descriptor_paths.append(resource.path.format(number=number))
        else:
            # Keep the historical byte-level behavior for non-CSV formats.
            while True:
                chunk = byte_stream.read(control.chunk_size)
                if not chunk:
                    break
                number = len(physical_paths) + 1
                physical_paths.append(self.__write_chunk(chunk, number))
                descriptor_paths.append(resource.path.format(number=number))

        if descriptor_paths:
            path_template = resource.path
            if not resource.basepath and os.path.isabs(path_template):
                # Absolute paths are rejected by descriptor safety validation.
                # Keep the location as a runtime basepath and serialize only
                # safe file names; callers can pass basepath on round-trip.
                resource.basepath = os.path.dirname(physical_paths[0])
                resource.path = os.path.basename(descriptor_paths[0])
                resource.extrapaths = [
                    os.path.basename(path) for path in descriptor_paths[1:]
                ]
            else:
                # Keep the user-facing path relative to resource.basepath; the
                # physical paths above have already been normalized for writes.
                resource.path = descriptor_paths[0]
                resource.extrapaths = descriptor_paths[1:]

        return physical_paths

    def __write_chunk(self, chunk: bytes, number: int) -> str:
        assert self.resource.normpath
        path = self.resource.normpath.format(number=number)
        with tempfile.NamedTemporaryFile(delete=False) as file:
            file.write(chunk)
            temporary_path = file.name
        helpers.move_file(temporary_path, path)
        return path


# Internal


class MultipartByteStream:
    def __init__(
        self,
        paths: List[str],
        *,
        remote: bool = False,
        headless: Optional[bool] = None,
        resource: Any = None,
    ):
        self.__paths = list(paths)
        self.__remote = remote
        self.__resource = resource
        self.__headless = headless
        self.__closed = False
        self.__buffer = b""
        self.__default_codec: Optional[str] = None
        self.__stream = self.read_chunk_stream()

    def __enter__(self):
        return self

    def __exit__(self, *args: Any, **kwargs: Any):
        self.close()

    @property
    def remote(self):
        return self.__remote

    @property
    def paths(self) -> List[str]:
        return list(self.__paths)

    @property
    def closed(self):
        return self.__closed

    def readable(self):
        return True

    def seekable(self):
        return True

    def writable(self):
        return False

    def close(self):
        if not self.__closed:
            self.__closed = True
            self.__buffer = b""
            self.__stream.close()

    def flush(self):
        pass

    def read1(self, size: int = -1):
        return self.read(size)

    def seek(self, offset: int, whence: int = 0):
        if whence != 0 or offset != 0:
            raise OSError("MultipartByteStream only supports seek(0)")
        self.__buffer = b""
        self.__stream.close()
        self.__stream = self.read_chunk_stream()
        return 0

    def read(self, size: int = -1):
        if self.__closed:
            return b""
        if size is None:
            size = -1

        if size < 0:
            chunks = [self.__buffer]
            self.__buffer = b""
            chunks.extend(self.__stream)
            return b"".join(chunks)

        if size == 0:
            return b""

        while len(self.__buffer) < size:
            try:
                chunk = next(self.__stream)
            except StopIteration:
                break
            if not chunk:
                break
            self.__buffer += chunk

        result = self.__buffer[:size]
        self.__buffer = self.__buffer[size:]
        return result

    def read_chunk_stream(self):
        resource = self.__resource
        dialect = getattr(resource, "dialect", None)
        file_format = getattr(resource, "format", None)
        encoding = getattr(resource, "encoding", None)

        # Compatibility for any direct internal caller that used the old
        # headless switch without a full Resource.
        if resource is None:
            headless = True if self.__headless is None else self.__headless
            yield from self.__read_legacy_line_stream(headless)
            return

        insert_record_separator = (
            file_format in CSV_FORMATS and dialect is not None and bool(dialect.header)
        )

        encoding_info = detect_encoding_info(None, encoding)
        codec = encoding_info.codec
        csv_control = (
            get_csv_control(dialect, file_format)
            if file_format in CSV_FORMATS and dialect is not None
            else None
        )
        line_terminator = (
            getattr(csv_control, "line_terminator", None) or "\n"
        )

        def encoded_endings(current_codec: str):
            endings = {line_terminator, "\r\n", "\n", "\r"}
            encoded = [ending.encode(current_codec) for ending in endings]
            return encoded, line_terminator.encode(current_codec)

        encoded_line_endings, encoded_newline = encoded_endings(codec)
        max_ending_length = max(map(len, encoded_line_endings), default=1)
        emitted_tail = b""

        for index, path in enumerate(self.__paths):
            skip = 0
            if (
                index > 0
                and resource is not None
                and file_format in CSV_FORMATS
                and dialect is not None
                and dialect.header
            ):
                skip = get_header_prefix_size(
                    path,
                    dialect=dialect,
                    file_format=file_format,
                    encoding=encoding,
                    default_codec=self.__default_codec,
                )

            with FileResource(path=path) as file_resource:
                byte_stream = file_resource.byte_stream

                # Read the first shard's BOM to settle UTF-16 endianness for
                # BOM-less continuation shards and separator encoding.
                if index == 0:
                    first_encoding = detect_encoding_info(
                        byte_stream,
                        encoding,
                        default_codec=self.__default_codec,
                    )
                    codec = first_encoding.codec
                    self.__default_codec = codec
                    encoded_line_endings, encoded_newline = encoded_endings(codec)
                    max_ending_length = max(map(len, encoded_line_endings), default=1)

                if skip:
                    self.__read_exact(byte_stream, skip)

                while True:
                    chunk = byte_stream.read1(io.DEFAULT_BUFFER_SIZE)
                    if not chunk:
                        break
                    emitted_tail = (emitted_tail + chunk)[-max_ending_length:]
                    yield chunk

            # A final CSV record may omit its line terminator. Without this
            # separator it would be concatenated with the next shard's first
            # data record after that shard's header region is removed. Check
            # encoded suffixes rather than the final raw byte: in UTF-16 the
            # last byte of an encoded LF is zero.
            ends_with_line_ending = any(
                emitted_tail.endswith(ending) for ending in encoded_line_endings
            )
            if (
                insert_record_separator
                and index < len(self.__paths) - 1
                and not ends_with_line_ending
            ):
                emitted_tail = encoded_newline[-max_ending_length:]
                yield encoded_newline

    @staticmethod
    def __read_exact(byte_stream: types.IByteStream, size: int) -> None:
        remaining = size
        while remaining > 0:
            chunk = byte_stream.read(remaining)
            if not chunk:
                return
            remaining -= len(chunk)

    def __read_legacy_line_stream(self, headless: bool):
        for number, path in enumerate(self.__paths, start=1):
            with FileResource(path=path) as file_resource:
                for line_number, line in enumerate(file_resource.byte_stream, start=1):
                    if not headless and number > 1 and line_number == 1:
                        continue
                    yield line
