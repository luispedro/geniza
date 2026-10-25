"""``geniza show ID [--raw]``: one message of a store.

``ID`` is a message id (``sha256:…``) or a prefix of its hex digest, with
or without ``sha256:``, at least ``MIN_PREFIX`` digits long. Until the
index exists (M6) it is looked up by scanning every segment header.

With ``--raw`` the original message is written to stdout byte for byte;
otherwise a summary: where the record is, its dates and sizes, the main
message headers and the extracted parts. Either way the message is rebuilt
and checked against its id first.
"""

import argparse
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from geniza.config import load, read_store
from geniza.mime import tree
from geniza.store import reader, segment
from geniza.store.attachments import AttachmentError
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
    #: Relative to the store root.
    segment: str
    offset: int
    header: Header


def parse_query(query: str) -> str:
    """The hex digest prefix in ``query``; raises ``ValueError``."""
    digest = query.strip().lower().removeprefix(MSG_PREFIX)
    if not _HEX.fullmatch(digest) or len(digest) > 64:
        raise ValueError(f"{query!r} is not a message id or a prefix of one")
    if len(digest) < MIN_PREFIX:
        raise ValueError(f"{query!r}: give at least {MIN_PREFIX} hex digits")
    return digest


def find(store: Path, digest: str) -> list[Found]:
    """The records whose id starts with ``MSG_PREFIX + digest``, the first
    record of each id only, in store order."""
    prefix = MSG_PREFIX + digest
    found: dict[str, Found] = {}
    for path in segment.segments(store):
        name = path.relative_to(store).as_posix()
        for offset, header in segment.scan(path):
            if header.id.startswith(prefix) and header.id not in found:
                found[header.id] = Found(name, offset, header)
                if len(digest) == 64:
                    return list(found.values())
    return list(found.values())


def _headers(data: bytes) -> dict[str, list[str]]:
    """The summary headers of a message, decoded for display."""
    message = tree.parse(data).headers
    headers: dict[str, list[str]] = {}
    for name in SUMMARY_HEADERS:
        values = []
        for value in message.get_all(name) or ():
            try:
                values.append(" ".join(str(value).split()))
            except Exception:  # display only: never fail on a header
                values.append(" ".join(repr(value).split()))
        if values:
            headers[name] = values
    return headers


def summary(found: Found, data: bytes) -> dict[str, Any]:
    header = found.header
    return {
        "id": header.id,
        "segment": found.segment,
        "offset": found.offset,
        "received": format_time(header.received),
        "stored": format_time(header.stored),
        "size": header.size,
        "stripped_size": header.stripped_size,
        "headers": _headers(data),
        "parts": [p.to_json() for p in parse_parts(header.parts, header.stripped_size)],
    }


def text(info: dict[str, Any]) -> str:
    lines = [
        f"id: {info['id']}",
        f"record: {info['segment']} at {info['offset']}",
        f"received: {info['received']}",
        f"stored: {info['stored']}",
        f"size: {info['size']} bytes ({info['stripped_size']} stored in the record)",
    ]
    for name, values in info["headers"].items():
        lines += [f"{name}: {v}" for v in values]
    if info["parts"]:
        lines.append("parts:")
        lines += [
            f"  {p['path']}: {p['kind']} {p['mode']} {p['file']}" for p in info["parts"]
        ]
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
    store = load(args.config).store(args.store).path
    read_store(store)
    try:
        matches = find(store, digest)
        if not matches:
            log.error("%s: no such message", args.id)
            return 1
        if len(matches) > 1:
            log.error("%s: ambiguous, %d messages match:", args.id, len(matches))
            for m in matches[:MAX_LISTED]:
                log.error("  %s", m.header.id)
            if len(matches) > MAX_LISTED:
                log.error("  …")
            return 1
        [found] = matches
        record, stripped = segment.read(store / found.segment, found.offset)
        data = reader.rebuild(store, record, stripped)
    except (SegmentError, RebuildError, AttachmentError) as e:
        log.error("%s", e)
        return 1
    except OSError as e:
        log.error("%s: %s", e.filename, e.strerror)
        return 1
    if args.raw:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    else:
        info = summary(found, data)
        out.result(text(info), info)
    return 0
