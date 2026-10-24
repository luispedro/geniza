"""Reading original messages back out of a store.

A record holds the stripped message; ``rebuild`` splices the stubbed parts
back in from their attachment files and checks the result against the
record's id, so a reader never hands out a message that differs from what
was downloaded.
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from geniza.mime.strip import reconstruct
from geniza.store import attachments, segment
from geniza.store.ingest import message_id
from geniza.store.parts import parse_parts, stubs
from geniza.store.segment import Header


class RebuildError(Exception):
    """A record whose message cannot be rebuilt as it was downloaded."""


type FileReader = Callable[[str, str], bytes]


def rebuild(
    store: Path, header: Header, stripped: bytes, read: FileReader | None = None
) -> bytes:
    """The original message of a record. ``read(file, blob)`` returns an
    attachment file's content and may raise ``AttachmentError`` or
    ``OSError``; by default it reads the file and checks its hash."""
    if read is None:

        def read(file: str, blob: str) -> bytes:
            return attachments.read(store, file, blob)

    try:
        parts = parse_parts(header.parts, header.stripped_size)
    except ValueError as e:
        raise RebuildError(f"{header.id}: bad parts: {e}") from None
    contents = [read(p.file, p.blob) for p in parts if p.recipe is not None]
    data = reconstruct(stripped, stubs(parts), contents)
    if message_id(data) != header.id:
        raise RebuildError(f"{header.id}: rebuilt message has another hash")
    return data


def message(store: Path, name: str, offset: int) -> bytes:
    """The original message of the record at ``offset`` of segment ``name``."""
    header, stripped = segment.read(store / name, offset)
    return rebuild(store, header, stripped)


@dataclass(frozen=True)
class Record:
    #: Relative to the store root.
    segment: str
    offset: int
    header: Header
    stripped: bytes


def records(store: Path) -> Iterator[Record]:
    """Every record of the store, in segment order. Raises ``SegmentError``
    on damage."""
    for path in segment.segments(store):
        name = path.relative_to(store).as_posix()
        for offset, header, stripped in segment.records(path):
            yield Record(name, offset, header, stripped)
