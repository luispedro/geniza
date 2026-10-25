"""Threads (DESIGN.md, "Threads"): the connected components of the graph
that links each message's Message-ID with the ids in its ``References``
and the first in its ``In-Reply-To``.

The index keeps them incrementally, as a union-find whose sets are
labelled eagerly: ``threads(message_id, thread)`` gives the thread of
every id seen, a message's own or one it names, and ``messages.thread_id``
the thread of each message. A message that links ids of several threads
merges them into the one with the most ids, relabelling the others, so
each id is relabelled O(log n) times in all. Thread numbers are labels
only: they can change when threads merge, and on a rebuild.
"""

import sqlite3
from collections.abc import Iterable

from geniza.index.envelope import Envelope

#: Ids per query, well under SQLite's limit on bound parameters.
_CHUNK = 500


def keys(id: str, envelope: Envelope) -> list[str]:
    """The ids a message links: its own Message-ID first, then those it
    names. A message without a Message-ID stands for itself with
    ``<id>``, which no Message-ID in the index can equal (they are kept
    without their outer brackets)."""
    own = envelope.message_id or f"<{id}>"
    named = [*envelope.references, envelope.in_reply_to]
    return list(dict.fromkeys([own, *(k for k in named if k)]))


def join(conn: sqlite3.Connection, id: str, envelope: Envelope) -> int:
    """Put message ``id``, whose row is in ``messages``, in the thread of
    the ids it links, merging threads if they are several; returns the
    thread."""
    linked = keys(id, envelope)
    found = set(_threads(conn, linked))
    if not found:
        (thread,) = conn.execute(
            "SELECT coalesce(max(thread), 0) + 1 FROM threads"
        ).fetchone()
    elif len(found) == 1:
        (thread,) = found
    else:
        thread = _merge(conn, found)
    conn.executemany(
        "INSERT OR IGNORE INTO threads (message_id, thread) VALUES (?, ?)",
        [(k, thread) for k in linked],
    )
    conn.execute("UPDATE messages SET thread_id = ? WHERE id = ?", (thread, id))
    return int(thread)


def _threads(conn: sqlite3.Connection, linked: list[str]) -> Iterable[int]:
    for i in range(0, len(linked), _CHUNK):
        chunk = linked[i : i + _CHUNK]
        marks = ", ".join("?" * len(chunk))
        for (thread,) in conn.execute(
            f"SELECT DISTINCT thread FROM threads WHERE message_id IN ({marks})",
            chunk,
        ):
            yield thread


def _merge(conn: sqlite3.Connection, found: set[int]) -> int:
    """Merge threads into the one with the most ids (the lowest number of
    those, on a tie); returns it."""
    sizes = {
        t: conn.execute(
            "SELECT count(*) FROM threads WHERE thread = ?", (t,)
        ).fetchone()[0]
        for t in found
    }
    into = min(found, key=lambda t: (-sizes[t], t))
    for t in found - {into}:
        conn.execute("UPDATE threads SET thread = ? WHERE thread = ?", (into, t))
        conn.execute("UPDATE messages SET thread_id = ? WHERE thread_id = ?", (into, t))
    return into
