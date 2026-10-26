"""The index database, ``cache/index.sqlite`` (DESIGN.md, "Index (cache)").

Everything in it is rebuilt from the store by ``reindex``, so a schema
change only bumps ``SCHEMA_VERSION``: an index of another version is
emptied and built again, never migrated.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from functools import cache
from pathlib import Path

from geniza.config import ConfigError

INDEX_FILE = "index.sqlite"
SCHEMA_VERSION = 1

#: Run one statement at a time, split at ";": comments must not hold one.
SCHEMA = """
CREATE TABLE messages (
    id TEXT NOT NULL UNIQUE,
    location TEXT NOT NULL CHECK (location IN ('segment', 'incoming')),
    segment TEXT,
    offset INTEGER,
    frame_size INTEGER,
    size INTEGER NOT NULL,
    received TEXT NOT NULL,
    date TEXT,
    from_addr TEXT,
    to_addr TEXT,
    cc_addr TEXT,
    subject TEXT,
    message_id TEXT,
    in_reply_to TEXT,
    thread_id INTEGER,
    state TEXT NOT NULL CHECK (state IN ('live', 'dropped')),
    -- Whether the body pass has read it (fts, attachment types and names).
    body_read INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX messages_body_read ON messages (body_read);
CREATE INDEX messages_message_id ON messages (message_id);
-- The date search uses: Date, else when received.
CREATE INDEX messages_when ON messages (coalesce(date, received));
CREATE INDEX messages_thread_id ON messages (thread_id);

CREATE TABLE incoming (
    account TEXT NOT NULL,
    msg TEXT NOT NULL,
    state TEXT NOT NULL
        CHECK (state IN ('held', 'promoted', 'dropped', 'purged')),
    PRIMARY KEY (account, msg)
) WITHOUT ROWID;

CREATE TABLE mailboxes (
    account TEXT NOT NULL,
    mbox TEXT NOT NULL,
    name TEXT NOT NULL,
    role TEXT,
    parent TEXT,
    sort INTEGER,
    subscribed INTEGER,
    PRIMARY KEY (account, mbox)
) WITHOUT ROWID;

CREATE TABLE membership (
    account TEXT NOT NULL,
    mbox TEXT NOT NULL,
    msg TEXT NOT NULL,
    rid TEXT NOT NULL,
    -- As the server gave them (server thread ids are hints only).
    received TEXT,
    thread TEXT,
    PRIMARY KEY (account, mbox, rid)
) WITHOUT ROWID;
CREATE INDEX membership_msg ON membership (msg);

CREATE TABLE flags (
    account TEXT NOT NULL,
    mbox TEXT NOT NULL,
    msg TEXT NOT NULL,
    flag TEXT NOT NULL,
    PRIMARY KEY (account, mbox, msg, flag)
) WITHOUT ROWID;
CREATE INDEX flags_msg ON flags (msg);

CREATE TABLE attachments (
    blob TEXT PRIMARY KEY,
    file TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('attachment', 'decoration')),
    mime_type TEXT,
    size INTEGER
) WITHOUT ROWID;

CREATE TABLE attachment_names (
    blob TEXT NOT NULL,
    msg TEXT NOT NULL,
    name TEXT NOT NULL,
    PRIMARY KEY (blob, msg, name)
) WITHOUT ROWID;

-- The thread of every id seen: a message's Message-ID (or "<id>" for
-- one without), or an id it names.
CREATE TABLE threads (
    message_id TEXT PRIMARY KEY,
    thread INTEGER NOT NULL
) WITHOUT ROWID;
CREATE INDEX threads_thread ON threads (thread);

-- How far each segment has been read: records end at or before
-- "indexed", and the next one starts there.
CREATE TABLE segments (
    segment TEXT PRIMARY KEY,
    indexed INTEGER NOT NULL
) WITHOUT ROWID;

-- Full-text search, one row per message read by the body pass (rowid:
-- messages.rowid). Contentless: the text is not kept, only the index.
CREATE VIRTUAL TABLE fts USING fts5 (
    subject, from_addr, to_addr, cc_addr, body, attachments,
    content = '', contentless_delete = 1,
    tokenize = 'unicode61 remove_diacritics 2'
);

-- The last journal event applied, per account, and the journal files
-- read ("name:size", space-separated): any change to them means the
-- account is replayed again.
CREATE TABLE accounts (
    account TEXT PRIMARY KEY,
    seq INTEGER NOT NULL,
    journal TEXT NOT NULL
) WITHOUT ROWID;
"""


def connect(path: Path) -> sqlite3.Connection:
    """Open the index at ``path``, creating it if needed. An index of
    another schema version is emptied, as if it were new. Transactions are
    explicit (``transaction``). Raises ``ConfigError`` if SQLite has no
    FTS5."""
    check_fts5()
    conn = sqlite3.connect(path, autocommit=True)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        (version,) = conn.execute("PRAGMA user_version").fetchone()
        if version != SCHEMA_VERSION:
            with transaction(conn):
                create(conn)
    except BaseException:
        conn.close()
        raise
    return conn


@cache
def check_fts5() -> None:
    """Raise ``ConfigError`` if this SQLite cannot make FTS5 tables."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE t USING fts5 (x)")
    except sqlite3.OperationalError:
        raise ConfigError(
            f"SQLite {sqlite3.sqlite_version} has no FTS5, which the index needs"
        ) from None
    finally:
        conn.close()


def create(conn: sqlite3.Connection) -> None:
    """Drop everything in the index and create empty tables, inside the
    caller's transaction."""
    # Virtual tables first: dropping one drops its shadow tables.
    for (name,) in conn.execute(
        "SELECT name FROM sqlite_schema WHERE type = 'table'"
        " AND name NOT LIKE 'sqlite_%' ORDER BY sql LIKE 'CREATE VIRTUAL%' DESC"
    ).fetchall():
        conn.execute(f'DROP TABLE IF EXISTS "{name}"')
    for statement in SCHEMA.split(";"):
        if statement.strip():
            conn.execute(statement)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """One write transaction: committed if the block ends normally, else
    rolled back."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
