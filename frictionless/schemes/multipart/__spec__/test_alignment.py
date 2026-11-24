from pathlib import Path

import pytest

from frictionless import Dialect, schemes
from frictionless.resources import TableResource


def _rows(count=8):
    return [{"id": number, "name": f"n{number}"} for number in range(1, count + 1)]


def test_multipart_csv_write_cuts_only_at_records_and_lists_parts(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    target = TableResource(data=_rows()).write(
        "table{number}.csv",
        scheme="multipart",
        control=schemes.MultipartControl(chunk_size=40),
    )

    assert target.path == "table1.csv"
    assert target.extrapaths == ["table2.csv"]
    assert target.paths == ["table1.csv", "table2.csv"]
    assert [Path(path).name for path in target.normpaths] == [
        "table1.csv",
        "table2.csv",
    ]

    first = Path("table1.csv").read_bytes()
    second = Path("table2.csv").read_bytes()
    assert first == (
        b"id,name\r\n"
        b"1,n1\r\n2,n2\r\n3,n3\r\n4,n4\r\n5,n5\r\n"
    )
    assert len(first) <= 40
    assert second == b"id,name\r\n6,n6\r\n7,n7\r\n8,n8\r\n"

    assert target.read_rows() == _rows()
    report = target.validate()
    assert report.valid, report.to_summary()


def test_multipart_descriptor_roundtrip_and_stats_match_single_file(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    rows = _rows(49)
    target = TableResource(data=rows).write(
        "part{number}.csv",
        scheme="multipart",
        control=schemes.MultipartControl(chunk_size=120),
    )
    TableResource(data=rows).write("single.csv")

    descriptor = target.to_descriptor()
    restored = TableResource.from_descriptor(descriptor)
    assert restored.paths == target.paths
    assert restored.read_rows() == rows
    assert restored.validate().valid

    multi = TableResource.from_descriptor(descriptor)
    single = TableResource("single.csv")
    multi.infer(stats=True)
    single.infer(stats=True)
    assert multi.rows == single.rows == len(rows)
    assert multi.bytes == single.bytes
    assert multi.hash == single.hash


@pytest.mark.parametrize("extension", ["csv", "tsv"])
def test_multipart_reader_removes_logical_header_region(tmp_path, extension):
    delimiter = "\t" if extension == "tsv" else ","
    header = f"id{delimiter}name\n".encode()
    first = tmp_path / f"a1.{extension}"
    second = tmp_path / f"a2.{extension}"
    first.write_bytes(header + f"1{delimiter}a\n".encode())
    second.write_bytes(header + f"2{delimiter}b\n".encode())

    resource = TableResource(
        path=str(first),
        extrapaths=[str(second)],
        format=extension,
    )
    assert [row.to_list() for row in resource.read_rows()] == [[1, "a"], [2, "b"]]


def test_multipart_reader_supports_multiline_header_rows_and_comments(tmp_path):
    prefix = b'group,value\n"multi\nline",header2\n# repeated comment\n'
    first = tmp_path / "h1.csv"
    second = tmp_path / "h2.csv"
    first.write_bytes(prefix + b"g1,v1\n")
    second.write_bytes(prefix + b"g2,v2\n")

    dialect = Dialect(header_rows=[1, 2], comment_char="#")
    resource = TableResource(
        path=str(first),
        extrapaths=[str(second)],
        dialect=dialect,
    )

    assert resource.read_rows() == [
        {"group multi line": "g1", "value header2": "v1"},
        {"group multi line": "g2", "value header2": "v2"},
    ]


def test_multipart_reader_does_not_glue_missing_final_newline(tmp_path):
    first = tmp_path / "m1.csv"
    second = tmp_path / "m2.csv"
    first.write_bytes(b"id,name\n1,a")
    second.write_bytes(b"id,name\n2,b\n")

    resource = TableResource(path=str(first), extrapaths=[str(second)])
    assert resource.read_rows() == [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]


def test_multipart_header_false_concatenation_has_no_removal(tmp_path):
    first = tmp_path / "h1.csv"
    second = tmp_path / "h2.csv"
    first.write_bytes(b"1,a\n")
    second.write_bytes(b"2,b\n")

    resource = TableResource(
        path=str(first),
        extrapaths=[str(second)],
        dialect=Dialect(header=False),
    )
    rows = resource.read_rows()
    assert [row.to_list() for row in rows] == [[1, "a"], [2, "b"]]


def test_multipart_quoted_newline_is_one_record_boundary(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rows = [
        {"id": 1, "name": "line\nbreak"},
        {"id": 2, "name": "normal"},
        {"id": 3, "name": 'quote"end'},
    ]
    target = TableResource(data=rows).write(
        "quoted{number}.csv",
        scheme="multipart",
        control=schemes.MultipartControl(chunk_size=45),
    )

    for path in target.paths:
        data = Path(path).read_bytes()
        assert data.startswith(b"id,name\r\n")
        text = data.decode("utf-8")
        # Every physical shard remains parseable on its own.
        assert list(__import__("csv").reader(text.splitlines()))

    assert target.read_rows() == rows
    assert target.validate().valid


def test_multipart_oversized_record_gets_own_shard(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    large_name = "x" * 100
    rows = [{"id": 1, "name": large_name}, {"id": 2, "name": "y"}]
    target = TableResource(data=rows).write(
        "big{number}.csv",
        scheme="multipart",
        control=schemes.MultipartControl(chunk_size=30),
    )

    assert len(target.paths) == 2
    assert Path(target.paths[0]).stat().st_size > 30
    assert target.read_rows() == rows


def test_multipart_absolute_path_validate_and_basepath_roundtrip(tmp_path):
    rows = _rows()
    target = TableResource(data=rows).write(
        str(tmp_path / "absolute{number}.csv"),
        scheme="multipart",
        control=schemes.MultipartControl(chunk_size=40),
    )

    assert target.validate().valid
    restored = TableResource.from_descriptor(
        target.to_descriptor(),
        basepath=target.basepath,
    )
    assert restored.normpaths == target.normpaths
    assert restored.read_rows() == rows


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16"])
def test_multipart_unicode_bom_and_roundtrip(tmp_path, monkeypatch, encoding):
    monkeypatch.chdir(tmp_path)
    rows = _rows(6)
    target = TableResource(data=rows).write(
        "unicode{number}.csv",
        scheme="multipart",
        encoding=encoding,
        control=schemes.MultipartControl(chunk_size=90),
    )

    bom_count = 0
    for path in target.normpaths:
        data = Path(path).read_bytes()
        if encoding == "utf-8-sig":
            bom_count += data.count(b"\xef\xbb\xbf")
        else:
            bom_count += int(data.startswith((b"\xff\xfe", b"\xfe\xff")))

    if encoding == "utf-8-sig":
        assert bom_count == 1
    else:
        # The first shard has the UTF-16 BOM; continuation shards are explicit
        # endian and are understood by the multipart reader.
        assert bom_count == 1

    restored = TableResource.from_descriptor(target.to_descriptor())
    assert restored.read_rows() == rows
    assert restored.validate().valid
