"""``geniza reindex``: rebuild the index from the store, and ``update``:
bring it up to date (DESIGN.md, "Index (cache)").

The headers-only pass reads record headers and the header block of each
message (decompressing only the start of its body, which also gives its
thread), the files in ``incoming/``, the attachment files records name,
and the journals. The index is rebuilt in one transaction, so a reader sees
either the old index or the new one. The body pass (``bodies``) then reads
each message in full for search, in transactions of its own.

Segments are append-only, so the index records how far it has read each
one and ``update`` reads on from there. Journals and ``incoming/`` are only
read by a rebuild for now.

``reindex`` reads the store without the store lock, as other readers do. A
record cut short at the end of a segment can be one being written, so it is
skipped with a warning; other damage is reported as a problem, and the
records the damage hides are left out.
"""

import argparse
import logging
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from geniza.config import load, open_cache, read_store
from geniza.index import bodies, schema, threads
from geniza.index.envelope import Envelope
from geniza.store import segment
from geniza.store.ingest import Catalog
from geniza.store.journal import MSG_PREFIX, JournalError, State, replay
from geniza.store.parts import parse_parts
from geniza.store.segment import HEAD_MAX, SegmentError, TornRecord, format_time

if TYPE_CHECKING:
    from geniza.cli import Output

log = logging.getLogger(__name__)

_INCOMING_NAME = re.compile(r"([0-9a-f]{64})\.eml")


@dataclass
class Report:
    records: int = 0
    messages: int = 0
    incoming: int = 0
    attachments: int = 0
    accounts: int = 0
    #: Messages read by the body pass.
    bodies: int = 0
    problems: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "records": self.records,
            "messages": self.messages,
            "incoming": self.incoming,
            "attachments": self.attachments,
            "accounts": self.accounts,
            "bodies": self.bodies,
            "problems": self.problems,
        }

    def text(self) -> str:
        lines = [
            f"messages: {self.messages} ({self.records} records,"
            f" {self.incoming} files in incoming/)",
            f"attachment files: {self.attachments}",
            f"accounts: {self.accounts}",
            f"message texts read: {self.bodies}",
            f"problems: {len(self.problems)}",
        ]
        lines += [f"  {p}" for p in self.problems]
        return "\n".join(lines) + "\n"


#: The first row of an id stays, except that a record replaces a file in
#: ``incoming/`` (a message promoted since the index last saw it), which
#: the body pass then reads again.
_INSERT_MESSAGE = """
INSERT INTO messages (
    id, location, segment, offset, frame_size, size, received, date,
    from_addr, to_addr, cc_addr, subject, message_id, in_reply_to, state
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'live')
ON CONFLICT (id) DO UPDATE SET
    location = excluded.location, segment = excluded.segment,
    offset = excluded.offset, frame_size = excluded.frame_size,
    size = excluded.size, received = excluded.received, date = excluded.date,
    from_addr = excluded.from_addr, to_addr = excluded.to_addr,
    cc_addr = excluded.cc_addr, subject = excluded.subject,
    message_id = excluded.message_id, in_reply_to = excluded.in_reply_to,
    state = 'live', body_read = 0
WHERE messages.location = 'incoming' AND excluded.location = 'segment'
"""


def _envelope(e: Envelope) -> tuple[str | None, ...]:
    return (
        e.date,
        e.from_addr,
        e.to_addr,
        e.cc_addr,
        e.subject,
        e.message_id,
        e.in_reply_to,
    )


def _segments(store: Path, conn: sqlite3.Connection, report: Report) -> None:
    """The records not indexed yet, the first indexed of each id; and the
    attachment files they name, the first record indexed naming each
    blob."""
    indexed = dict(conn.execute("SELECT segment, indexed FROM segments"))
    for path in segment.segments(store):
        name = path.relative_to(store).as_posix()
        start = indexed.get(name, 0)
        try:
            for offset, header, head, end in segment.scan_heads(path, start):
                start = end
                report.records += 1
                if isinstance(head, SegmentError):
                    report.problems.append(str(head))
                    continue
                try:
                    parts = parse_parts(header.parts, header.stripped_size)
                except ValueError as e:
                    report.problems.append(f"{name}: record at offset {offset}: {e}")
                    parts = []
                envelope = Envelope.parse(head)
                conn.execute(
                    _INSERT_MESSAGE,
                    (
                        header.id,
                        "segment",
                        name,
                        offset,
                        header.frame_size,
                        header.size,
                        format_time(header.received),
                        *_envelope(envelope),
                    ),
                )
                threads.join(conn, header.id, envelope)
                conn.executemany(
                    "INSERT OR IGNORE INTO attachments (blob, file, kind, size)"
                    " VALUES (?, ?, ?, ?)",
                    [
                        (p.blob, p.file, p.kind, _file_size(store / p.file))
                        for p in parts
                    ],
                )
        except TornRecord as e:
            log.warning("%s (being written?); skipped", e)
        except SegmentError as e:
            report.problems.append(str(e))
        conn.execute(
            "INSERT OR REPLACE INTO segments (segment, indexed) VALUES (?, ?)",
            (name, start),
        )


def _appended_only(store: Path, conn: sqlite3.Connection) -> bool:
    """Whether every segment the index has read is still there, at least as
    long as it was."""
    for name, indexed in conn.execute("SELECT segment, indexed FROM segments"):
        try:
            if (store / name).stat().st_size < indexed:
                return False
        except FileNotFoundError:
            return False
    return True


def _file_size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def _read_head(path: Path) -> tuple[bytes, os.stat_result]:
    """The start of a file, up to the end of its header block."""
    with open(path, "rb") as f:
        st = os.fstat(f.fileno())
        head = b""
        while len(head) < HEAD_MAX and segment.header_end(head) is None:
            chunk = f.read(segment.HEAD_CHUNK)
            if not chunk:
                break
            head += chunk
    return head[: segment.header_end(head) or HEAD_MAX], st


def _incoming(
    store: Path, conn: sqlite3.Connection, states: dict[str, State], report: Report
) -> None:
    """The files in each account's ``incoming/``. A file's received date
    is its modification time."""
    root = store / "incoming"
    accounts = (
        sorted(p.name for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
    )
    for account in accounts:
        state = states.get(account, State())
        for path in sorted((root / account).iterdir()):
            if not (m := _INCOMING_NAME.fullmatch(path.name)):
                continue
            msg = MSG_PREFIX + m[1]
            try:
                head, st = _read_head(path)
            except OSError as e:
                report.problems.append(f"{path}: {e.strerror}")
                continue
            report.incoming += 1
            fate = state.fates.get(msg)
            conn.execute(
                "INSERT INTO incoming (account, msg, state) VALUES (?, ?, ?)",
                (account, msg, "held" if fate is None else fate.status),
            )
            received = datetime.fromtimestamp(int(st.st_mtime), UTC)
            envelope = Envelope.parse(head)
            conn.execute(
                _INSERT_MESSAGE,
                (
                    msg,
                    "incoming",
                    None,
                    None,
                    None,
                    st.st_size,
                    format_time(received),
                    *_envelope(envelope),
                ),
            )
            threads.join(conn, msg, envelope)
    # Dropped: held in no segment, and dropped by every account holding it.
    conn.execute(
        "UPDATE messages SET state = 'dropped' WHERE location = 'incoming'"
        " AND NOT EXISTS (SELECT 1 FROM incoming i WHERE i.msg = messages.id"
        " AND i.state NOT IN ('dropped', 'purged'))"
    )


def _journals(store: Path, report: Report) -> dict[str, State]:
    root = store / "state"
    accounts = (
        sorted(p.name for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
    )
    states = {}
    for account in accounts:
        state = State()
        try:
            replay(store, account, state)
        except JournalError as e:
            report.problems.append(str(e))
        states[account] = state
    return states


def _state(conn: sqlite3.Connection, account: str, state: State) -> None:
    conn.execute(
        "INSERT INTO accounts (account, seq) VALUES (?, ?)", (account, state.seq)
    )
    conn.executemany(
        "INSERT INTO mailboxes (account, mbox, name, role, parent, sort, subscribed)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (account, k, b.name, b.role, b.parent, b.sort, b.subscribed)
            for k, b in state.mailboxes.items()
        ],
    )
    conn.executemany(
        "INSERT INTO membership (account, mbox, msg, rid, received, thread)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [
            (account, mbox, msg, rid, _time(placed.received), placed.thread)
            for mbox, contents in state.contents.items()
            for rid, msg in contents.items()
            for placed in [state.placed[mbox, rid]]
        ],
    )
    conn.executemany(
        "INSERT INTO flags (account, mbox, msg, flag) VALUES (?, ?, ?, ?)",
        [
            (account, mbox, msg, flag)
            for msg, places in state.places.items()
            for mbox in places
            for flag in sorted(state.flags.get((msg, mbox), ()))
        ],
    )


def _time(t: datetime | None) -> str | None:
    return None if t is None else format_time(t)


def _rebuild(store: Path, conn: sqlite3.Connection, report: Report) -> None:
    """Empty the index and build it again, inside the caller's
    transaction."""
    schema.create(conn)
    states = _journals(store, report)
    _segments(store, conn, report)
    _incoming(store, conn, states, report)
    for account, state in states.items():
        _state(conn, account, state)


def _counts(conn: sqlite3.Connection, report: Report) -> None:
    (report.messages,) = conn.execute("SELECT count(*) FROM messages").fetchone()
    (report.attachments,) = conn.execute("SELECT count(*) FROM attachments").fetchone()
    (report.accounts,) = conn.execute("SELECT count(*) FROM accounts").fetchone()


def _open(store: Path, cache: Path) -> sqlite3.Connection:
    info = read_store(store)
    open_cache(cache, info.id)
    return schema.connect(cache / schema.INDEX_FILE)


def reindex(store: Path, cache: Path) -> Report:
    """Rebuild ``cache/index.sqlite`` from the store at ``store``, then
    read every message's body."""
    report = Report()
    conn = _open(store, cache)
    try:
        with schema.transaction(conn):
            _rebuild(store, conn, report)
            _counts(conn, report)
        report.bodies = bodies.read_bodies(store, conn, report.problems)
    finally:
        conn.close()
    return report


def update(store: Path, cache: Path, *, text: bool = False) -> sqlite3.Connection:
    """Open the index of the store at ``store``, bringing it up to date:
    index the records appended to segments since it was last updated, or
    rebuild it if it has never read a segment or a segment it read is gone
    or shorter. With ``text``, also read the bodies not read yet, for
    search. Problems found are logged as warnings."""
    report = Report()
    conn = _open(store, cache)
    try:
        with schema.transaction(conn):
            if conn.execute("SELECT 1 FROM segments LIMIT 1").fetchone() is None:
                _rebuild(store, conn, report)
            elif not _appended_only(store, conn):
                log.warning("%s: segments changed since indexed; rebuilding", cache)
                _rebuild(store, conn, report)
            else:
                _segments(store, conn, report)
        if text:
            report.bodies = bodies.read_bodies(store, conn, report.problems)
    except BaseException:
        conn.close()
        raise
    for problem in report.problems:
        log.warning("%s", problem)
    log.debug("index: %d records, %d bodies read", report.records, report.bodies)
    return conn


def catalog(conn: sqlite3.Connection) -> Catalog:
    """What the store holds, for ingest, from an up-to-date index. A record
    whose body is damaged is not in it."""
    return Catalog(
        messages={
            id: (name, offset)
            for id, name, offset in conn.execute(
                "SELECT id, segment, offset FROM messages WHERE location = 'segment'"
            )
        },
        blobs=dict(conn.execute("SELECT blob, file FROM attachments")),
    )


def run(args: argparse.Namespace, out: Output) -> int:
    store = load(args.config).store(args.store)
    report = reindex(store.path, store.cache)
    out.result(report.text(), report.to_json())
    return 1 if report.problems else 0
