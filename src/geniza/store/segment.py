"""Segments: records of stripped messages (DESIGN.md, "Message storage:
segments").

A record is a zstd skippable frame holding a JSON header, followed by one
zstd frame holding the stripped message as an mboxrd entry. A segment is a
concatenation of records, so it is both a valid zstd stream and, once
decompressed, a valid mboxrd file.

Writers only append. Only the last record of the open segment can be torn
(one record is in flight at a time, and each is fsynced), and ``recover``
removes it.
"""

import json
import logging
import os
import re
import struct
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from compression import zstd
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, Self

log = logging.getLogger(__name__)

FORMAT_VERSION = 1

SKIPPABLE_MAGIC = 0x184D2A5E
_FRAME_HEAD = struct.Struct("<II")

MiB = 1024 * 1024
SEGMENT_CAP = 256 * MiB
#: Larger headers are taken as damage, not as a header.
MAX_HEADER_SIZE = 16 * MiB

FROM_LINE_PREFIX = b"From geniza "

_ESCAPE = re.compile(rb"^(>*From )", re.MULTILINE)
_UNESCAPE = re.compile(rb"^>(>*From )", re.MULTILINE)
_SEGMENT_NAME = re.compile(r"(\d{4})-(\d{2})(?:\.([1-9]\d*))?\.zst")


class SegmentError(Exception):
    """A record that cannot be read."""

    def __init__(self, path: Path | str, offset: int, reason: str) -> None:
        super().__init__(f"{path}: record at offset {offset}: {reason}")
        self.path = Path(path)
        self.offset = offset
        self.reason = reason


class TornRecord(SegmentError):
    """A record that runs past the end of the file."""


@dataclass(frozen=True)
class Header:
    id: str
    size: int
    stripped_size: int
    frame_size: int
    received: datetime
    stored: datetime
    #: Extracted parts, as stored; their fields belong to ingest.
    parts: tuple[Mapping[str, Any], ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "v": FORMAT_VERSION,
            "id": self.id,
            "size": self.size,
            "stripped_size": self.stripped_size,
            "frame_size": self.frame_size,
            "received": format_time(self.received),
            "stored": format_time(self.stored),
            "parts": [dict(p) for p in self.parts],
        }

    @classmethod
    def from_json(cls, obj: object) -> Self:
        """Parse a header; raises ``ValueError`` if it is not a valid one."""
        if not isinstance(obj, dict):
            raise ValueError("header is not an object")
        if obj.get("v") != FORMAT_VERSION:
            raise ValueError(f"unknown record version {obj.get('v')!r}")
        id_ = obj.get("id")
        if not isinstance(id_, str) or not id_.startswith("sha256:"):
            raise ValueError("bad id")
        parts = obj.get("parts")
        if not isinstance(parts, list) or not all(isinstance(p, dict) for p in parts):
            raise ValueError("bad parts")
        return cls(
            id=id_,
            size=_size(obj, "size"),
            stripped_size=_size(obj, "stripped_size"),
            frame_size=_size(obj, "frame_size"),
            received=_time(obj, "received"),
            stored=_time(obj, "stored"),
            parts=tuple(parts),
        )


def _size(obj: dict[str, Any], key: str) -> int:
    n = obj.get(key)
    if type(n) is not int or n < 0:
        raise ValueError(f"bad {key}")
    return n


def _time(obj: dict[str, Any], key: str) -> datetime:
    s = obj.get(key)
    if not isinstance(s, str):
        raise ValueError(f"bad {key}")
    return parse_time(s)


@dataclass(frozen=True)
class Location:
    #: Relative to the store root, as the journal records it.
    segment: str
    offset: int
    header: Header


def format_time(t: datetime) -> str:
    if t.tzinfo is None:
        raise ValueError("naive datetime")
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(s: str) -> datetime:
    t = datetime.fromisoformat(s)
    if t.tzinfo is None:
        raise ValueError("naive datetime")
    return t.astimezone(UTC)


def escape(data: bytes) -> bytes:
    """mboxrd: every line matching ``^>*From `` gets one more ``>``."""
    return _ESCAPE.sub(rb">\1", data)


def unescape(data: bytes) -> bytes:
    return _UNESCAPE.sub(rb"\1", data)


def from_line(received: datetime) -> bytes:
    stamp = time.asctime(received.astimezone(UTC).timetuple())
    return FROM_LINE_PREFIX + stamp.encode("ascii") + b"\n"


def encode_record(
    stripped: bytes,
    *,
    id: str,
    size: int,
    received: datetime,
    stored: datetime,
    parts: Sequence[Mapping[str, Any]] = (),
    level: int = zstd.COMPRESSION_LEVEL_DEFAULT,
) -> tuple[bytes, Header]:
    """Frame one stripped message; returns the record bytes and its header."""
    body = from_line(received) + escape(stripped) + b"\n"
    frame = zstd.compress(
        body,
        options={
            zstd.CompressionParameter.compression_level: level,
            zstd.CompressionParameter.checksum_flag: 1,
        },
    )
    header = Header(
        id=id,
        size=size,
        stripped_size=len(stripped),
        frame_size=len(frame),
        received=received,
        stored=stored,
        parts=tuple(parts),
    )
    text = json.dumps(header.to_json(), ensure_ascii=False, separators=(",", ":"))
    payload = text.encode("utf-8")
    return _FRAME_HEAD.pack(SKIPPABLE_MAGIC, len(payload)) + payload + frame, header


def decode_body(frame: bytes, header: Header) -> bytes:
    """The stripped message in a body frame; raises ``ValueError``."""
    try:
        if zstd.get_frame_size(frame) != len(frame):
            raise ValueError("frame size does not match the header")
        body = zstd.decompress(frame)
    except zstd.ZstdError as e:
        raise ValueError(f"bad body frame: {e}") from None
    if not body.startswith(FROM_LINE_PREFIX) or not body.endswith(b"\n"):
        raise ValueError("body is not an mboxrd entry")
    start = body.index(b"\n") + 1
    if start > len(body) - 1:
        raise ValueError("body is not an mboxrd entry")
    stripped = unescape(body[start:-1])
    if len(stripped) != header.stripped_size:
        raise ValueError("stripped size does not match the header")
    return stripped


def _read_header(f: BinaryIO, path: Path, offset: int, size: int) -> tuple[Header, int]:
    """Read the header frame at ``offset``; returns it and where the body
    frame starts."""
    f.seek(offset)
    head = f.read(_FRAME_HEAD.size)
    if len(head) < _FRAME_HEAD.size:
        raise TornRecord(path, offset, "header frame cut short")
    magic, n = _FRAME_HEAD.unpack(head)
    if magic != SKIPPABLE_MAGIC:
        raise SegmentError(path, offset, "no header frame")
    if n > MAX_HEADER_SIZE:
        raise SegmentError(path, offset, f"header of {n} bytes")
    body_start = offset + _FRAME_HEAD.size + n
    if body_start > size:
        raise TornRecord(path, offset, "header frame cut short")
    try:
        header = Header.from_json(json.loads(f.read(n).decode("utf-8")))
    except ValueError as e:
        raise SegmentError(path, offset, f"bad header: {e}") from None
    if body_start + header.frame_size > size:
        raise TornRecord(path, offset, "body frame cut short")
    return header, body_start


def scan(path: Path, start: int = 0) -> Iterator[tuple[int, Header]]:
    """Yield ``(offset, header)`` for each record from ``start`` on, without
    decompressing bodies."""
    with open(path, "rb") as f:
        size = os.fstat(f.fileno()).st_size
        offset = start
        while offset < size:
            header, body_start = _read_header(f, path, offset, size)
            yield offset, header
            offset = body_start + header.frame_size


def read(path: Path, offset: int) -> tuple[Header, bytes]:
    """The header and stripped message of the record at ``offset``."""
    with open(path, "rb") as f:
        return _read_record(f, path, offset, os.fstat(f.fileno()).st_size)


def records(path: Path, start: int = 0) -> Iterator[tuple[int, Header, bytes]]:
    """Yield ``(offset, header, stripped)`` for each record from ``start`` on."""
    with open(path, "rb") as f:
        size = os.fstat(f.fileno()).st_size
        offset = start
        while offset < size:
            header, stripped = _read_record(f, path, offset, size)
            yield offset, header, stripped
            offset = f.tell()


def _read_record(
    f: BinaryIO, path: Path, offset: int, size: int
) -> tuple[Header, bytes]:
    header, body_start = _read_header(f, path, offset, size)
    f.seek(body_start)
    try:
        return header, decode_body(f.read(header.frame_size), header)
    except ValueError as e:
        raise SegmentError(path, offset, str(e)) from None


def recover(path: Path) -> int:
    """Truncate a torn record at the end of ``path``; returns the number of
    bytes removed.

    The last record is checked in full; the ones before it by their headers
    only. Damage anywhere else, which a crash cannot cause, raises
    ``SegmentError`` and leaves the file alone.
    """
    with open(path, "r+b") as f:
        size = os.fstat(f.fileno()).st_size
        offset = 0
        last: int | None = None
        cut: int | None = None
        while offset < size:
            try:
                header, body_start = _read_header(f, path, offset, size)
            except TornRecord:
                cut = offset
                break
            except SegmentError:
                f.seek(offset)
                if f.read().strip(b"\0"):
                    raise
                cut = offset
                break
            last = offset
            offset = body_start + header.frame_size
        if cut is None:
            if last is None:
                return 0
            try:
                _read_record(f, path, last, size)
                return 0
            except SegmentError as e:
                log.debug("%s", e)
                cut = last
        f.truncate(cut)
        f.flush()
        os.fsync(f.fileno())
    log.warning(
        "%s: removed a torn record of %d bytes at offset %d", path, size - cut, cut
    )
    return size - cut


def segment_name(year: int, month: int, index: int) -> str:
    """``segments/2026/2026-10.zst``, ``…/2026-10.1.zst`` for rollovers."""
    suffix = f".{index}" if index else ""
    return f"segments/{year:04d}/{year:04d}-{month:02d}{suffix}.zst"


def month_segments(store: Path, year: int, month: int) -> list[tuple[int, Path]]:
    """The segments of a month as ``(index, path)``, in order."""
    found = []
    for p in (store / "segments" / f"{year:04d}").glob(f"{year:04d}-{month:02d}*.zst"):
        m = _SEGMENT_NAME.fullmatch(p.name)
        if m and (int(m[1]), int(m[2])) == (year, month):
            found.append((int(m[3] or 0), p))
    return sorted(found)


def _now() -> datetime:
    return datetime.now(UTC)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class SegmentWriter:
    """Appends records to the open segment of the current month.

    The open segment is the last one of the month the record is written in
    (UTC). A record that would take it over ``cap`` goes to a new rollover
    segment, unless the segment is empty. When a segment is opened, a torn
    record at its end is removed first.
    """

    def __init__(
        self,
        store: Path,
        *,
        cap: int = SEGMENT_CAP,
        level: int = zstd.COMPRESSION_LEVEL_DEFAULT,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self.store = store
        self.cap = cap
        self.level = level
        self.clock = clock
        self._file: BinaryIO | None = None
        self._key: tuple[int, int, int] = (0, 0, 0)
        self._size = 0

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None

    def append(
        self,
        stripped: bytes,
        *,
        id: str,
        size: int,
        received: datetime,
        parts: Sequence[Mapping[str, Any]] = (),
    ) -> Location:
        """Append and fsync one record."""
        stored = self.clock()
        data, header = encode_record(
            stripped,
            id=id,
            size=size,
            received=received,
            stored=stored,
            parts=parts,
            level=self.level,
        )
        year, month = stored.astimezone(UTC).timetuple()[:2]
        if self._file is None or self._key[:2] != (year, month):
            self._open(year, month)
        if self._size and self._size + len(data) > self.cap:
            self._open(year, month, self._key[2] + 1)
        assert self._file is not None
        offset = self._size
        self._file.write(data)
        self._file.flush()
        os.fsync(self._file.fileno())
        self._size += len(data)
        return Location(segment_name(*self._key), offset, header)

    def _open(self, year: int, month: int, index: int | None = None) -> None:
        self.close()
        if index is None:
            existing = month_segments(self.store, year, month)
            index = existing[-1][0] if existing else 0
        path = self.store / segment_name(year, month, index)
        if path.exists():
            recover(path)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
            _fsync_dir(path.parent)
            _fsync_dir(path.parent.parent)
        self._file = open(path, "ab")  # noqa: SIM115  (closed by close())
        self._size = os.fstat(self._file.fileno()).st_size
        self._key = (year, month, index)
