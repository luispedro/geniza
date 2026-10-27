"""``geniza show ID [--raw]``: one message of a store.

``ID`` is a message id (``sha256:…``) or a prefix of its hex digest, with
or without ``sha256:``, at least ``MIN_PREFIX`` digits long. It is looked
up in the index, which is brought up to date first.

With ``--raw`` the original message is written to stdout byte for byte;
otherwise a summary: where the record is, its dates and sizes, the main
message headers and the extracted parts, then the message text and that of
each attached message. Either way the message is rebuilt and checked
against its id first. A message only in ``incoming/`` is read from the
file there (that of the first account holding it whose file has the right
hash) and has no record, stored time or parts in its summary.
"""

import argparse
import logging
import re
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from email.message import Message
from pathlib import Path
from typing import TYPE_CHECKING, Any

from geniza.config import load
from geniza.index import reindex
from geniza.mime.text import clean, texts
from geniza.store import incoming, reader, segment
from geniza.store.attachments import AttachmentError
from geniza.store.ingest import message_id
from geniza.store.journal import MSG_PREFIX
from geniza.store.parts import parse_parts
from geniza.store.reader import RebuildError
from geniza.store.segment import Header, SegmentError, format_time

if TYPE_CHECKING:
    from geniza.cli import Output

log = logging.getLogger(__name__)

MIN_PREFIX = 4
#: Candidates listed when a prefix is ambiguous.
MAX_LISTED = 10
#: Message headers in the summary, in this order.
SUMMARY_HEADERS = ("From", "To", "Cc", "Date", "Subject", "Message-ID")

_HEX = re.compile(r"[0-9a-f]+")


@dataclass(frozen=True)
class Found:
    id: str
    location: str
    #: Relative to the store root; ``None`` for a message only in
    #: ``incoming/``.
    segment: str | None
    offset: int | None
    received: str
    state: str


class NotHeld(Exception):
    """No file in ``incoming/`` holds a message the index lists there."""


def parse_query(query: str) -> str:
    """The hex digest prefix in ``query``; raises ``ValueError``."""
    digest = query.strip().lower().removeprefix(MSG_PREFIX)
    if not _HEX.fullmatch(digest) or len(digest) > 64:
        raise ValueError(f"{query!r} is not a message id or a prefix of one")
    if len(digest) < MIN_PREFIX:
        raise ValueError(f"{query!r}: give at least {MIN_PREFIX} hex digits")
    return digest


def find(conn: sqlite3.Connection, digest: str) -> list[Found]:
    """The messages whose id starts with ``MSG_PREFIX + digest``, by id,
    with where the index has each."""
    prefix = MSG_PREFIX + digest
    # Every character after the prefix is a hex digit, so below "g".
    rows = conn.execute(
        "SELECT id, location, segment, offset, received, state FROM messages"
        " WHERE id >= ? AND id < ? ORDER BY id",
        (prefix, prefix + "g"),
    )
    return [Found(*row) for row in rows]


def held(store: Path, conn: sqlite3.Connection, id: str) -> tuple[str, bytes]:
    """The file (relative to the store root) and content of a message only
    in ``incoming/``: the first account's whose file has its hash. Raises
    ``NotHeld``, naming each file tried."""
    tried = []
    for (account,) in conn.execute(
        "SELECT account FROM incoming WHERE msg = ? ORDER BY account", (id,)
    ):
        path = incoming.file_path(store, account, id)
        try:
            data = path.read_bytes()
        except OSError as e:
            tried.append(f"{path}: {e.strerror}")
            continue
        if message_id(data) == id:
            return path.relative_to(store).as_posix(), data
        tried.append(f"{path}: holds another message")
    raise NotHeld("; ".join([f"{id}: not in incoming/", *tried]))


def _headers(message: Message) -> dict[str, list[str]]:
    """The summary headers of a message, decoded for display."""
    headers: dict[str, list[str]] = {}
    for name in SUMMARY_HEADERS:
        values = []
        for value in message.get_all(name) or ():
            try:
                values.append(clean(" ".join(str(value).split())))
            except Exception:  # display only: never fail on a header
                values.append(clean(" ".join(repr(value).split())))
        if values:
            headers[name] = values
    return headers


def summary(
    found: Found, header: Header | None, data: bytes, file: str | None = None
) -> dict[str, Any]:
    """That of a record (``header``) or of a message only in ``incoming/``
    (``file``, holding ``data``)."""
    message, *attached = texts(data)
    info: dict[str, Any] = {"id": found.id, "location": found.location}
    if header is None:
        info |= {
            "file": file,
            "state": found.state,
            "received": found.received,
            "size": len(data),
            "parts": [],
        }
    else:
        info |= {
            "segment": found.segment,
            "offset": found.offset,
            "received": format_time(header.received),
            "stored": format_time(header.stored),
            "size": header.size,
            "stripped_size": header.stripped_size,
            "parts": [
                p.to_json() for p in parse_parts(header.parts, header.stripped_size)
            ],
        }
    return info | {
        "headers": _headers(message.root.headers),
        "text": message.text,
        "attached": [
            {"path": m.path, "headers": _headers(m.root.headers), "text": m.text}
            for m in attached
        ],
    }


def text(info: dict[str, Any]) -> str:
    lines = [f"id: {info['id']}"]
    if info["location"] == "segment":
        lines += [
            f"record: {info['segment']} at {info['offset']}",
            f"received: {info['received']}",
            f"stored: {info['stored']}",
            f"size: {info['size']:,} bytes"
            f" ({info['stripped_size']:,} stored in the record)",
        ]
    else:
        dropped = " (dropped)" if info["state"] == "dropped" else ""
        lines += [
            f"held: {info['file']}{dropped}",
            f"received: {info['received']}",
            f"size: {info['size']:,} bytes",
        ]
    for name, values in info["headers"].items():
        lines += [f"{name}: {v}" for v in values]
    if info["parts"]:
        lines.append("parts:")
        lines += [
            f"  {p['path']}: {p['kind']} {p['mode']} {p['file']}" for p in info["parts"]
        ]
    if info["text"]:
        lines += ["", info["text"]]
    for m in info["attached"]:
        lines += ["", f"--- attached message {m['path']} ---"]
        for name, values in m["headers"].items():
            lines += [f"{name}: {v}" for v in values]
        if m["text"]:
            lines += ["", m["text"]]
    return "\n".join(lines) + "\n"


def run(args: argparse.Namespace, out: Output) -> int:
    if args.raw and args.json:
        log.error("--raw and --json cannot be combined")
        return 2
    try:
        digest = parse_query(args.id)
    except ValueError as e:
        log.error("%s", e)
        return 2
    config = load(args.config).store(args.store)
    store = config.path
    with closing(reindex.update(store, config.cache)) as conn:
        matches = find(conn, digest)
        if not matches:
            log.error("%s: no such message", args.id)
            return 1
        if len(matches) > 1:
            log.error("%s: ambiguous, %d messages match:", args.id, len(matches))
            for m in matches[:MAX_LISTED]:
                log.error("  %s", m.id)
            if len(matches) > MAX_LISTED:
                log.error("  …")
            return 1
        [found] = matches
        record: Header | None = None
        file = None
        try:
            if found.segment is None or found.offset is None:
                file, data = held(store, conn, found.id)
            else:
                record, stripped = segment.read(store / found.segment, found.offset)
                data = reader.rebuild(store, record, stripped)
        except (SegmentError, RebuildError, AttachmentError, NotHeld) as e:
            log.error("%s", e)
            return 1
        except OSError as e:
            log.error("%s: %s", e.filename, e.strerror)
            return 1
    if args.raw:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    else:
        info = summary(found, record, data, file)
        out.result(text(info), info)
    return 0
