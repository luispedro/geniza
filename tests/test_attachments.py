import os
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from geniza.mime.names import attachment_name
from geniza.store.attachments import (
    AttachmentError,
    AttachmentWriter,
    blob_id,
    file_name,
    parse_file_name,
    read,
    scan,
)

T0 = datetime(2026, 10, 23, 14, 2, 11, tzinfo=UTC)
T1 = datetime(2026, 11, 1, 0, 0, 1, tzinfo=UTC)


class Clock:
    def __init__(self, t: datetime = T0) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t


names = st.builds(
    lambda name, path: attachment_name(name, path, "application/pdf"),
    st.text(max_size=40),
    st.sampled_from(["1", "2.1"]),
)


def test_file_name() -> None:
    blob = blob_id(b"hello")
    assert file_name(blob, "a.pdf") == blob[7:23] + "_a.pdf"
    assert parse_file_name(file_name(blob, "a_b.pdf")) == (blob[7:23], "a_b.pdf")
    for bad in ["", ".hidden", "a/b", " x"]:
        with pytest.raises(ValueError):
            file_name(blob, bad)
    with pytest.raises(ValueError):
        file_name("md5:abc", "a.pdf")
    assert parse_file_name(".3fa9c1e07b22d4a1.0123456789abcdef.tmp") is None
    assert parse_file_name("3fa9c1e07b22d4a_short.pdf") is None


def test_put(tmp_path: Path) -> None:
    w = AttachmentWriter(tmp_path, clock=Clock())
    s = w.put(b"%PDF-1.4 report", "report.pdf")
    assert s.new
    assert s.blob == blob_id(b"%PDF-1.4 report")
    assert s.file == f"attachments/2026/10/{s.blob[7:23]}_report.pdf"
    path = tmp_path / s.file
    assert path.read_bytes() == b"%PDF-1.4 report"
    assert stat.S_IMODE(path.stat().st_mode) == 0o444
    assert read(tmp_path, s.file, s.blob) == b"%PDF-1.4 report"
    assert os.listdir(path.parent) == [path.name]


def test_first_name_wins(tmp_path: Path) -> None:
    w = AttachmentWriter(tmp_path, clock=Clock())
    first = w.put(b"logo", "logo.png")
    again = w.put(b"logo", "image001.png")
    assert not again.new
    assert again.file == first.file
    # A new writer (a later run) finds the file of the current month.
    again = AttachmentWriter(tmp_path, clock=Clock()).put(b"logo", "x.png")
    assert (again.file, again.new) == (first.file, False)
    assert len(list(scan(tmp_path))) == 1


def test_months(tmp_path: Path) -> None:
    clock = Clock()
    w = AttachmentWriter(tmp_path, clock=clock)
    a = w.put(b"a", "a.txt")
    clock.t = T1
    b = w.put(b"b", "b.txt")
    assert a.file.startswith("attachments/2026/10/")
    assert b.file.startswith("attachments/2026/11/")
    # Blobs of earlier months are the caller's to find (through the index).
    again = w.put(b"a", "a.txt")
    assert again.new
    assert again.file.startswith("attachments/2026/11/")
    assert [f for _, f in scan(tmp_path)] == [a.file, b.file, again.file]


def test_prefix_collision(tmp_path: Path) -> None:
    w = AttachmentWriter(tmp_path, clock=Clock())
    s = w.put(b"real", "a.txt")
    fake = "sha256:" + s.blob[7:23] + "0" * 48
    with pytest.raises(AttachmentError):
        w.put(b"other", "b.txt", blob=fake)
    assert [f for _, f in scan(tmp_path)] == [s.file]


def test_read_checks_blob(tmp_path: Path) -> None:
    s = AttachmentWriter(tmp_path, clock=Clock()).put(b"data", "d.bin")
    with pytest.raises(AttachmentError):
        read(tmp_path, s.file, blob_id(b"other"))


def test_temp_files_removed(tmp_path: Path) -> None:
    d = tmp_path / "attachments/2026/10"
    d.mkdir(parents=True)
    tmp = d / f".{'a' * 16}.{'b' * 16}.tmp"
    tmp.write_bytes(b"torn")
    (d / "notes.txt").write_bytes(b"not ours")
    assert list(scan(tmp_path)) == []
    AttachmentWriter(tmp_path, clock=Clock()).put(b"x", "x.txt")
    assert not tmp.exists()
    assert (d / "notes.txt").exists()


def test_failed_write_leaves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = AttachmentWriter(tmp_path, clock=Clock())
    w.put(b"first", "first.txt")

    def fail(*args: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "rename", fail)
    with pytest.raises(OSError):
        w.put(b"second", "second.txt")
    monkeypatch.undo()
    assert len(os.listdir(tmp_path / "attachments/2026/10")) == 1
    assert w.put(b"second", "second.txt").new


@given(st.lists(st.tuples(st.binary(max_size=64), names), max_size=12))
def test_put_scan_round_trip(items: list[tuple[bytes, str]]) -> None:
    with tempfile.TemporaryDirectory() as d:
        store = Path(d)
        w = AttachmentWriter(store, clock=Clock())
        first: dict[str, str] = {}
        for content, name in items:
            s = w.put(content, name)
            assert s.new == (s.blob not in first)
            first.setdefault(s.blob, s.file)
            assert s.file == first[s.blob]
            assert s.file.endswith("_" + next(n for c, n in items if c == content))
            assert read(store, s.file, s.blob) == content
        listed = dict(scan(store))
        assert len(listed) == len(first)
        assert sorted(listed.values()) == sorted(first.values())
        for blob, file in first.items():
            assert listed[blob[7:23]] == file
