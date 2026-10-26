"""``geniza info``: what a store holds, from its index.

The index is brought up to date first (as for ``search``, without the body
pass), so this costs one incremental update, not a read of the store. The
last complete sync of an account is when its ``sync.json`` was last
written.
"""

import argparse
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from geniza.config import INITIALIZED_FILE, load, read_store
from geniza.index import reindex
from geniza.store import segment
from geniza.store.segment import format_time
from geniza.sync.base import sync_path

if TYPE_CHECKING:
    from geniza.cli import Output


@dataclass
class AccountInfo:
    name: str
    configured: bool
    #: Distinct messages in the account's mailboxes.
    messages: int = 0
    mailboxes: int = 0
    #: Files in its ``incoming/`` not dropped (nor promoted) yet.
    held: int = 0
    #: Dropped from its ``incoming/``, in their grace period.
    dropped: int = 0
    journal_events: int = 0
    last_sync: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "configured": self.configured,
            "messages": self.messages,
            "mailboxes": self.mailboxes,
            "held": self.held,
            "dropped": self.dropped,
            "journal_events": self.journal_events,
            "last_sync": self.last_sync,
        }

    def text(self) -> str:
        if self.last_sync is None and not self.journal_events and not self.held:
            line = f"{self.name}: never synced"
        else:
            line = (
                f"{self.name}: {self.messages} messages in {self.mailboxes}"
                f" mailboxes, {self.held} held, {self.dropped} dropped,"
                f" {self.journal_events} journal events, last complete sync "
                + (self.last_sync or "never")
            )
        return line if self.configured else f"{line} (not in the configuration)"


@dataclass
class Info:
    store: str
    path: str
    format: int
    created: str
    #: When initialization ended, or None while it lasts.
    initialized: str | None
    #: Distinct messages, and their size as received.
    messages: int = 0
    bytes: int = 0
    #: Of those, the ones in a segment and the ones only in ``incoming/``
    #: (and of these, the ones dropped by every account holding them).
    in_segments: int = 0
    in_incoming: int = 0
    dropped: int = 0
    #: Messages whose text the body pass has read, for search.
    text_read: int = 0
    oldest: str | None = None
    newest: str | None = None
    segments: int = 0
    segment_bytes: int = 0
    attachment_files: int = 0
    attachment_bytes: int = 0
    attachments: int = 0
    decorations: int = 0
    accounts: list[AccountInfo] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "store": self.store,
            "path": self.path,
            "format": self.format,
            "created": self.created,
            "initialized": self.initialized,
            "messages": self.messages,
            "bytes": self.bytes,
            "in_segments": self.in_segments,
            "in_incoming": self.in_incoming,
            "dropped": self.dropped,
            "text_read": self.text_read,
            "oldest": self.oldest,
            "newest": self.newest,
            "segments": self.segments,
            "segment_bytes": self.segment_bytes,
            "attachment_files": self.attachment_files,
            "attachment_bytes": self.attachment_bytes,
            "attachments": self.attachments,
            "decorations": self.decorations,
            "accounts": [a.to_json() for a in self.accounts],
        }

    def text(self) -> str:
        lines = [
            f"store: {self.store} ({self.path})",
            f"format {self.format}, created {self.created}, "
            + (
                f"initialized {self.initialized}"
                if self.initialized
                else "still initializing"
            ),
            f"messages: {self.messages} ({self.bytes} bytes as received;"
            f" {self.in_segments} in segments, {self.in_incoming} only in"
            f" incoming/, {self.dropped} of them dropped)",
        ]
        if self.oldest is not None:
            lines.append(f"received: {self.oldest} to {self.newest}")
        lines += [
            f"segments: {self.segments} ({self.segment_bytes} bytes)",
            f"attachment files: {self.attachment_files}"
            f" ({self.attachment_bytes} bytes; {self.attachments} attachments,"
            f" {self.decorations} decorations)",
            f"text indexed for search: {self.text_read} of {self.messages} messages",
            f"accounts: {len(self.accounts)}",
        ]
        lines += [f"  {a.text()}" for a in self.accounts]
        return "\n".join(lines) + "\n"


def _mtime(path: Path) -> str | None:
    try:
        t = path.stat().st_mtime
    except FileNotFoundError:
        return None
    return format_time(datetime.fromtimestamp(t, UTC))


def _accounts(
    store: Path, conn: sqlite3.Connection, configured: set[str]
) -> list[AccountInfo]:
    found = {name for (name,) in conn.execute("SELECT account FROM accounts")}
    found |= {name for (name,) in conn.execute("SELECT DISTINCT account FROM incoming")}
    infos = []
    for name in sorted(found | configured):
        a = AccountInfo(
            name, name in configured, last_sync=_mtime(sync_path(store, name))
        )
        a.messages, a.mailboxes = conn.execute(
            "SELECT count(DISTINCT msg), (SELECT count(*) FROM mailboxes"
            " WHERE account = ?1) FROM membership WHERE account = ?1",
            (name,),
        ).fetchone()
        a.held, a.dropped = conn.execute(
            "SELECT count(*) FILTER (WHERE state = 'held'),"
            " count(*) FILTER (WHERE state = 'dropped')"
            " FROM incoming WHERE account = ?",
            (name,),
        ).fetchone()
        row = conn.execute(
            "SELECT seq FROM accounts WHERE account = ?", (name,)
        ).fetchone()
        a.journal_events = row[0] if row else 0
        infos.append(a)
    return infos


def info(store: Path, cache: Path, name: str, configured: set[str]) -> Info:
    meta = read_store(store)
    initialized = store / INITIALIZED_FILE
    result = Info(
        store=name,
        path=str(store),
        format=meta.format,
        created=format_time(meta.created),
        initialized=(initialized.read_text().strip() if initialized.exists() else None),
    )
    for path in segment.segments(store):
        result.segments += 1
        result.segment_bytes += path.stat().st_size
    with closing(reindex.update(store, cache)) as conn:
        (
            result.messages,
            result.bytes,
            result.in_segments,
            result.in_incoming,
            result.dropped,
            result.text_read,
            result.oldest,
            result.newest,
        ) = conn.execute(
            "SELECT count(*), coalesce(sum(size), 0),"
            " count(*) FILTER (WHERE location = 'segment'),"
            " count(*) FILTER (WHERE location = 'incoming'),"
            " count(*) FILTER (WHERE state = 'dropped'),"
            " count(*) FILTER (WHERE body_read),"
            " min(received), max(received) FROM messages"
        ).fetchone()
        (
            result.attachment_files,
            result.attachment_bytes,
            result.attachments,
            result.decorations,
        ) = conn.execute(
            "SELECT count(*), coalesce(sum(size), 0),"
            " count(*) FILTER (WHERE kind = 'attachment'),"
            " count(*) FILTER (WHERE kind = 'decoration') FROM attachments"
        ).fetchone()
        result.accounts = _accounts(store, conn, configured)
    return result


def run(args: argparse.Namespace, out: Output) -> int:
    config = load(args.config)
    store = config.store(args.store)
    configured = {a.name for a in config.accounts.values() if a.store == store.name}
    result = info(store.path, store.cache, store.name, configured)
    out.result(result.text(), result.to_json())
    return 0
