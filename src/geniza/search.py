"""``geniza search QUERY…``: the messages of a store that match a query
(DESIGN.md, "Search").

The words of the query are joined by spaces and split into terms again, so
quotes are part of the query, not the shell's: ``'subject:"annual
report"'``. Every term must match:

- ``word``, ``"a phrase"``, ``word*`` (a prefix): the message's subject,
  addresses, text and attachment names, through FTS5;
- ``from:``, ``to:``, ``cc:``, ``subject:``, ``body:``, ``attachment:``
  followed by one of those: that field only;
- ``mailbox:NAME``: in a mailbox with that name, path (``Archive/2020``)
  or role (``inbox``), ignoring case;
- ``date:2024``, ``date:2024-03``, ``date:2024-03-05``, or a range of
  those, ``A..B``, ``A..`` or ``..B`` (both ends included): the message's
  date in UTC, its ``Date`` header or else when it was received;
- ``thread:ID``: in the thread of a message (an id or a prefix of one).

A ``-`` before a term negates it. A word that FTS5 would read as syntax
(``AND``, ``NEAR``, ``^``…) is searched for as a word.
"""

import argparse
import json
import logging
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any

from geniza.config import load
from geniza.index import reindex
from geniza.show import parse_query
from geniza.store.journal import MSG_PREFIX

if TYPE_CHECKING:
    from geniza.cli import Output

log = logging.getLogger(__name__)

#: Field prefixes that search one column of ``fts``.
FTS_FIELDS = {
    "from": "from_addr",
    "to": "to_addr",
    "cc": "cc_addr",
    "subject": "subject",
    "body": "body",
    "attachment": "attachments",
}
FIELDS = {*FTS_FIELDS, "mailbox", "date", "thread"}
#: Hex digits of the id in the text output (``show`` takes a prefix).
SHORT_ID = 12
#: Deepest mailbox path followed (parent links can loop on a broken server).
MAX_DEPTH = 64

#: The date of a message, for ``date:`` and the order of results.
WHEN = "coalesce(m.date, m.received)"

_FIELD = re.compile(r"([a-z]+):")
_DATE = re.compile(r"(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?")


class QueryError(ValueError):
    pass


@dataclass(frozen=True)
class Term:
    value: str
    field: str | None = None
    negated: bool = False
    #: Quoted in the query: a phrase.
    quoted: bool = False
    #: Ended with ``*``: a prefix.
    prefix: bool = False


def lex(query: str) -> list[Term]:
    """The terms of a query. Never raises: an unknown ``field:`` is part
    of a word, and an unclosed quote ends with the query."""
    terms = []
    i, n = 0, len(query)
    while i < n:
        if query[i].isspace():
            i += 1
            continue
        negated = query[i] == "-" and i + 1 < n and not query[i + 1].isspace()
        i += negated
        field = None
        if (m := _FIELD.match(query, i)) and m[1] in FIELDS:
            field = m[1]
            i = m.end()
        if i < n and query[i] == '"':
            end = query.find('"', i + 1)
            end = n if end < 0 else end
            value, quoted = query[i + 1 : end], True
            i = end + 1
            prefix = i < n and query[i] == "*"
            i += prefix
        else:
            end = i
            while end < n and not query[end].isspace():
                end += 1
            value, quoted = query[i:end], False
            i = end
            prefix = value.endswith("*")
            value = value.rstrip("*")
        terms.append(Term(value, field, negated, quoted, prefix))
    return terms


def _has_words(text: str) -> bool:
    """Whether FTS5's ``unicode61`` tokenizer finds a token in ``text``
    (near enough: it also counts marks and private-use characters)."""
    return any(c.isalnum() for c in text)


def _fts(term: Term) -> str:
    """A term as an FTS5 query: a quoted string, so that nothing in it is
    syntax."""
    phrase = '"' + term.value.replace('"', '""') + '"' + (" *" if term.prefix else "")
    if term.field is not None:
        return f"{FTS_FIELDS[term.field]} : {phrase}"
    return phrase


def _day(text: str, *, end: bool) -> date:
    """The first day of a ``YYYY[-MM[-DD]]`` period, or with ``end`` the
    first day after it."""
    if not (m := _DATE.fullmatch(text)):
        raise QueryError(f"date:{text}: expected YYYY, YYYY-MM or YYYY-MM-DD")
    year, month, day = int(m[1]), int(m[2] or 1), int(m[3] or 1)
    try:
        start = date(year, month, day)
        if not end:
            return start
        if m[3]:
            return date.fromordinal(start.toordinal() + 1)
        if m[2]:
            return date(year + month // 12, month % 12 + 1, 1)
        return date(year + 1, 1, 1)
    except (ValueError, OverflowError) as e:
        raise QueryError(f"date:{text}: {e}") from None


def date_range(value: str) -> tuple[str | None, str | None]:
    """The bounds, ``lower <= t < upper`` in the index's time format, of a
    ``date:`` value; None for an open end."""
    low, sep, high = value.partition("..")
    if not sep:
        high = low
    if not low and not high:
        raise QueryError(f"date:{value}: give a date or a range of dates")
    lower = _day(low, end=False).isoformat() + "T00:00:00Z" if low else None
    upper = _day(high, end=True).isoformat() + "T00:00:00Z" if high else None
    return lower, upper


def mailboxes(conn: sqlite3.Connection, name: str) -> list[tuple[str, str]]:
    """The ``(account, mbox)`` of the mailboxes whose name, path or role is
    ``name``, ignoring case."""
    rows = conn.execute(
        "SELECT account, mbox, name, role, parent FROM mailboxes"
    ).fetchall()
    boxes = {(a, k): (n, r, p) for a, k, n, r, p in rows}

    def path(account: str, mbox: str) -> str | None:
        names: list[str] = []
        key: str | None = mbox
        while key is not None and len(names) < MAX_DEPTH:
            if (box := boxes.get((account, key))) is None:
                return None
            names.append(box[0])
            key = box[2]
        return None if key is not None else "/".join(reversed(names))

    wanted = name.casefold()
    return sorted(
        (account, mbox)
        for (account, mbox), (leaf, role, _) in boxes.items()
        if wanted
        in {
            leaf.casefold(),
            (role or "").casefold(),
            (path(account, mbox) or "").casefold(),
        }
    )


def thread_of(conn: sqlite3.Connection, value: str) -> tuple[str, int | None]:
    """The id and thread of the message whose id (or a prefix of it) is
    ``value``."""
    try:
        prefix = MSG_PREFIX + parse_query(value)
    except ValueError as e:
        raise QueryError(f"thread:{e}") from None
    rows = conn.execute(
        "SELECT id, thread_id FROM messages WHERE id >= ? AND id < ? LIMIT 2",
        (prefix, prefix + "g"),
    ).fetchall()
    if not rows:
        raise QueryError(f"thread:{value}: no such message")
    if len(rows) > 1:
        raise QueryError(f"thread:{value}: ambiguous, more than one message matches")
    [(id, thread)] = rows
    return str(id), None if thread is None else int(thread)


@dataclass
class Query:
    """A query as SQL: conditions on ``messages AS m``, all to hold."""

    conditions: list[str]
    params: list[Any]


def to_sql(conn: sqlite3.Connection, terms: list[Term], *, dropped: bool) -> Query:
    """The SQL for ``terms``; raises ``QueryError``."""
    conditions: list[str] = []
    params: list[Any] = []
    matched: list[str] = []
    in_fts = "m.rowid IN (SELECT rowid FROM fts WHERE fts MATCH ?)"

    def add(sql: str, *args: Any, negated: bool) -> None:
        conditions.append(f"NOT ({sql})" if negated else sql)
        params.extend(args)

    for t in terms:
        match t.field:
            case None | "from" | "to" | "cc" | "subject" | "body" | "attachment":
                if not _has_words(t.value):
                    continue
                if t.negated:
                    add(in_fts, _fts(t), negated=True)
                else:
                    matched.append(_fts(t))
            case "mailbox":
                if not t.value:
                    raise QueryError("mailbox: give a mailbox name, path or role")
                if not (boxes := mailboxes(conn, t.value)):
                    log.warning("mailbox:%s: no such mailbox", t.value)
                add(
                    "EXISTS (SELECT 1 FROM membership b WHERE b.msg = m.id"
                    " AND (b.account, b.mbox) IN (SELECT value ->> 0, value ->> 1"
                    " FROM json_each(?)))",
                    json.dumps(boxes),
                    negated=t.negated,
                )
            case "date":
                lower, upper = date_range(t.value)
                bounds = [f"{WHEN} >= ?"] * bool(lower) + [f"{WHEN} < ?"] * bool(upper)
                add(
                    " AND ".join(bounds),
                    *(b for b in (lower, upper) if b),
                    negated=t.negated,
                )
            case "thread":
                id, thread = thread_of(conn, t.value)
                if thread is None:  # not threaded: only itself
                    add("m.id = ?", id, negated=t.negated)
                else:
                    add("m.thread_id = ?", thread, negated=t.negated)
    if matched:
        conditions.insert(0, in_fts)
        params.insert(0, " ".join(matched))
    if not conditions:
        raise QueryError("nothing to search for")
    if not dropped:
        conditions.append("m.state = 'live'")
    return Query(conditions, params)


@dataclass(frozen=True)
class Hit:
    id: str
    date: str
    from_addr: str | None
    to_addr: str | None
    subject: str | None
    thread: int | None
    location: str
    state: str

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "date": self.date,
            "from": self.from_addr,
            "to": self.to_addr,
            "subject": self.subject,
            "thread": self.thread,
            "location": self.location,
            "state": self.state,
        }

    def line(self) -> str:
        fields = [
            self.id.removeprefix(MSG_PREFIX)[:SHORT_ID],
            self.date[:10],
            self.from_addr or "(no sender)",
            self.subject or "(no subject)",
        ]
        if self.state != "live":
            fields.append(f"[{self.state}]")
        return "  ".join(fields)


def search(
    conn: sqlite3.Connection, query: Query, limit: int | None
) -> tuple[list[Hit], int]:
    """The messages ``query`` matches, newest first, at most ``limit``, and
    how many match in all."""
    rows = conn.execute(
        f"SELECT m.id, {WHEN}, m.from_addr, m.to_addr, m.subject, m.thread_id,"
        " m.location, m.state, count(*) OVER () FROM messages m"
        f" WHERE {' AND '.join(query.conditions)}"
        f" ORDER BY {WHEN} DESC, m.id LIMIT ?",
        [*query.params, -1 if limit is None else limit],
    ).fetchall()
    total = rows[0][-1] if rows else 0
    return [Hit(*row[:-1]) for row in rows], total


def run(args: argparse.Namespace, out: Output) -> int:
    if args.limit < 0:
        log.error("--limit: must be 0 (no limit) or more")
        return 2
    terms = lex(" ".join(args.query))
    config = load(args.config).store(args.store)
    with closing(reindex.update(config.path, config.cache, text=True)) as conn:
        try:
            query = to_sql(conn, terms, dropped=args.dropped)
        except QueryError as e:
            log.error("%s", e)
            return 2
        start = time.monotonic()
        hits, total = search(conn, query, args.limit or None)
        log.info("%d messages match (%.2f s)", total, time.monotonic() - start)
    if len(hits) < total:
        log.warning("%d of %d messages shown (see --limit)", len(hits), total)
    out.result(
        "\n".join(h.line() for h in hits),
        {"total": total, "messages": [h.to_json() for h in hits]},
    )
    return 0
