"""The body pass of the index: the text of each message for full-text
search, and the MIME types and names of the attachment files (DESIGN.md,
"Index (cache)").

The headers pass leaves each message unread (``messages.body_read``); this
pass reads them in the order they were indexed, a batch of ``BATCH`` per
transaction, so that a long pass does not keep writers waiting and one that
is interrupted goes on where it stopped.

A record's stripped message is enough: body parts are never extracted (only
attachments and decorations are), and a stub replaces a part's body but
keeps its headers, so neither the text nor the attachments' types and
names need the attachment files.
"""

import sqlite3
from contextlib import suppress
from pathlib import Path
from typing import Any

from geniza.index import schema
from geniza.mime import tree
from geniza.mime.text import clean, texts
from geniza.store import segment
from geniza.store.journal import MSG_PREFIX
from geniza.store.parts import PartRecord, parse_parts
from geniza.store.segment import SegmentError

#: Messages read per transaction.
BATCH = 200
#: Most characters of text indexed per message (that of its attached
#: messages included); the rest is not searchable.
MAX_TEXT = 1024 * 1024


def read_bodies(store: Path, conn: sqlite3.Connection, problems: list[str]) -> int:
    """Read every message not read yet; returns how many were read. A
    message that cannot be read is reported in ``problems`` (once: it
    counts as read) and is not searchable."""
    n = 0
    while True:
        with schema.transaction(conn):
            rows = conn.execute(
                "SELECT rowid, id, location, segment, offset, subject, from_addr,"
                " to_addr, cc_addr FROM messages WHERE body_read = 0"
                " ORDER BY rowid LIMIT ?",
                (BATCH,),
            ).fetchall()
            for row in rows:
                _read(store, conn, row, problems)
        n += len(rows)
        if len(rows) < BATCH:
            return n


def _read(
    store: Path, conn: sqlite3.Connection, row: tuple[Any, ...], problems: list[str]
) -> None:
    rowid, id, location, name, offset, *envelope = row
    conn.execute("UPDATE messages SET body_read = 1 WHERE rowid = ?", (rowid,))
    # Read before as a file in incoming/, since promoted.
    conn.execute("DELETE FROM fts WHERE rowid = ?", (rowid,))
    parts: list[PartRecord] = []
    try:
        if location == "segment":
            header, data = segment.read(store / name, offset)
            with suppress(ValueError):  # reported by the headers pass
                parts = parse_parts(header.parts, header.stripped_size)
        else:
            data = incoming_file(store, conn, id).read_bytes()
    except SegmentError as e:
        problems.append(str(e))
        return
    except OSError as e:
        problems.append(f"{e.filename}: {e.strerror}")
        return
    root = tree.parse(data)
    body = "\n\n".join(t.text for t in texts(data, root) if t.text)
    names = [clean(n) for leaf in root.leaves() if (n := leaf.filename)]
    nodes = {node.path: node for node in root.walk()}
    for p in parts:
        if (node := nodes.get(p.path)) is None:
            continue
        conn.execute(
            "UPDATE attachments SET mime_type = ? WHERE blob = ? AND mime_type IS NULL",
            (node.content_type, p.blob),
        )
        if filename := node.filename:
            conn.execute(
                "INSERT OR IGNORE INTO attachment_names (blob, msg, name)"
                " VALUES (?, ?, ?)",
                (p.blob, id, clean(filename)),
            )
    conn.execute(
        "INSERT INTO fts (rowid, subject, from_addr, to_addr, cc_addr, body,"
        " attachments) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (rowid, *envelope, body[:MAX_TEXT], "\n".join(names)),
    )


def incoming_file(store: Path, conn: sqlite3.Connection, id: str) -> Path:
    """The file of a message only in ``incoming/``: that of the first
    account holding it (the index lists the files found)."""
    (account,) = conn.execute(
        "SELECT min(account) FROM incoming WHERE msg = ?", (id,)
    ).fetchone()
    return store / "incoming" / str(account) / f"{id.removeprefix(MSG_PREFIX)}.eml"
