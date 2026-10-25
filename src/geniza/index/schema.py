"""The index database, ``cache/index.sqlite`` (DESIGN.md, "Index (cache)").

Everything in it is rebuilt from the store by ``reindex``, so a schema
change only bumps ``SCHEMA_VERSION``: an index of another version is
emptied and built again, never migrated.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

INDEX_FILE = "index.sqlite"
SCHEMA_VERSION = 1

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
    thread_id TEXT,
    state TEXT NOT NULL CHECK (state IN ('live', 'dropped'))
);
CREATE INDEX messages_message_id ON messages (message_id);
CREATE INDEX messages_date ON messages (date);

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
    PRIMARY KEY (account, mbox)
) WITHOUT ROWID;

CREATE TABLE membership (
    account TEXT NOT NULL,
    mbox TEXT NOT NULL,
    msg TEXT NOT NULL,
    rid TEXT NOT NULL,
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

-- The last journal event applied, per account.
CREATE TABLE accounts (
    account TEXT PRIMARY KEY,
    seq INTEGER NOT NULL
) WITHOUT ROWID;
"""


def connect(path: Path) -> sqlite3.Connection:
    """Open the index at ``path``, creating it if needed. An index of
    another schema version is emptied, as if it were new. Transactions are
    explicit (``transaction``)."""
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


def create(conn: sqlite3.Connection) -> None:
    """Drop everything in the index and create empty tables, inside the
    caller's transaction."""
    for (name,) in conn.execute(
        "SELECT name FROM sqlite_schema WHERE type = 'table'"
        " AND name NOT LIKE 'sqlite_%'"
    ).fetchall():
        conn.execute(f'DROP TABLE "{name}"')
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
