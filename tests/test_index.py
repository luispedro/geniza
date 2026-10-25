"""The index: envelopes, the schema and ``reindex`` (headers-only pass)."""

import json
import logging
import os
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import corpus_files
from hypothesis import given
from hypothesis import strategies as st

from geniza import cli
from geniza.config import init_store, read_store
from geniza.index import schema
from geniza.index.envelope import Envelope
from geniza.index.reindex import reindex
from geniza.mime.recipes import Policy
from geniza.store import segment
from geniza.store.ingest import Ingester, message_id
from geniza.store.journal import (
    Add,
    Drop,
    Flags,
    JournalWriter,
    Mbox,
    Remove,
    Undrop,
)
from geniza.store.lock import StoreLock

T0 = datetime(2026, 10, 23, 14, 2, 11, tzinfo=UTC)
CORPUS = {p.stem: p.read_bytes() for p in corpus_files()}
EXTRACT_ALL = Policy(min_size=1, decoration_min_size=1)


def ids(*names: str) -> list[str]:
    return [message_id(CORPUS[n]) for n in names]


def make_store(store: Path, msgs: list[bytes]) -> list[tuple[str, int]]:
    with StoreLock(store) as lock:
        init_store(store)
        with Ingester(store, lock, policy=EXTRACT_ALL, clock=lambda: T0) as ing:
            return [(a.segment, a.offset) for m in msgs for a in [ing.add(m, T0)]]


def query(cache: Path, sql: str, *args: object) -> list[tuple[object, ...]]:
    conn = sqlite3.connect(cache / schema.INDEX_FILE)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


@pytest.fixture
def store(tmp_path: Path) -> Path:
    path = tmp_path / "store"
    make_store(path, list(CORPUS.values()))
    return path


@pytest.fixture
def cache(tmp_path: Path) -> Path:
    return tmp_path / "cache"


# --- envelopes ---------------------------------------------------------------


def test_envelope_decodes_headers() -> None:
    e = Envelope.parse(
        b"From: =?utf-8?q?Jos=C3=A9?= <jose@example.org>\r\n"
        b"To: a@example.net,\r\n b@example.net\r\n"
        b"To: c@example.com\r\n"
        b"Subject: =?iso-8859-1?q?caf=E9?=\r\n  au lait\r\n"
        b"Date: Fri, 23 Oct 2026 16:02:11 +0200\r\n"
        b"Message-ID: <abc@example.org>\r\n"
        b"In-Reply-To: <p1@example.org> <p2@example.org>\r\n"
        b"\r\n"
        b"Subject: not a header\r\n"
    )
    assert e == Envelope(
        date="2026-10-23T14:02:11Z",
        from_addr="José <jose@example.org>",
        to_addr="a@example.net, b@example.net, c@example.com",
        cc_addr=None,
        subject="café au lait",
        message_id="abc@example.org",
        in_reply_to="p1@example.org",
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (b"Message-ID: abc@example.org\n", "abc@example.org"),
        (b"Message-ID: <a b>\n", None),
        (b"Message-ID: two words\n", None),
        (b"Message-ID:\n", None),
        (b"Subject: x\n", None),
    ],
)
def test_envelope_message_id(raw: bytes, expected: str | None) -> None:
    assert Envelope.parse(raw).message_id == expected


def test_envelope_bad_date_and_no_zone() -> None:
    assert Envelope.parse(b"Date: yesterday\n\n").date is None
    assert Envelope.parse(b"Date: 23 Oct 2026 14:02:11\n\n").date == (
        "2026-10-23T14:02:11Z"
    )


@given(st.binary(max_size=300))
def test_envelope_never_raises(data: bytes) -> None:
    check_text(Envelope.parse(data))


def check_text(e: Envelope) -> None:
    """Every field can go into SQLite: valid Unicode, on one line."""
    for value in vars(e).values():
        if value is not None:
            value.encode("utf-8")
            assert "\n" not in value and "\r" not in value


@pytest.mark.parametrize(
    "raw",
    [
        b"Subject: caf\xe9\nFrom: \xff <x@example.org>\n\n",
        b"Message-ID: <\xe9@example.org>\nIn-Reply-To: <\xff>\n\n",
        b"Subject: =?utf-8?q?\xff?=\n\n",
        b"Date: Fri, 23 Oct 2026 16:02:11 +0200 \xe9\n\n",
    ],
)
def test_envelope_8bit_headers(raw: bytes) -> None:
    check_text(Envelope.parse(raw))


@given(
    st.lists(
        st.sampled_from(["From", "To", "Cc", "Subject", "Date", "Message-ID"]),
        max_size=5,
    ),
    st.lists(st.text(max_size=30), min_size=5, max_size=5),
)
def test_envelope_never_raises_on_header_values(
    names: list[str], values: list[str]
) -> None:
    raw = b"".join(
        f"{n}: {v}\n".encode("utf-8", "surrogateescape")
        for n, v in zip(names, values, strict=False)
    )
    check_text(Envelope.parse(raw + b"\n"))


# --- schema ------------------------------------------------------------------


def test_schema_version_mismatch_empties_the_index(tmp_path: Path) -> None:
    path = tmp_path / schema.INDEX_FILE
    conn = schema.connect(path)
    conn.execute("INSERT INTO accounts VALUES ('a', 1)")
    conn.execute("CREATE TABLE stray (x)")
    conn.close()
    conn = schema.connect(path)
    assert conn.execute("SELECT * FROM accounts").fetchall() == [("a", 1)]
    conn.execute(f"PRAGMA user_version = {schema.SCHEMA_VERSION + 1}")
    conn.close()
    conn = schema.connect(path)
    assert conn.execute("SELECT * FROM accounts").fetchall() == []
    tables = {
        name
        for (name,) in conn.execute("SELECT name FROM sqlite_schema WHERE type='table'")
    }
    assert "stray" not in tables and "messages" in tables
    conn.close()


def test_transaction_rolls_back(tmp_path: Path) -> None:
    conn = schema.connect(tmp_path / schema.INDEX_FILE)
    with pytest.raises(ZeroDivisionError), schema.transaction(conn):
        conn.execute("INSERT INTO accounts VALUES ('a', 1)")
        _ = 1 / 0
    assert conn.execute("SELECT * FROM accounts").fetchall() == []
    conn.close()


# --- reindex -----------------------------------------------------------------


def test_reindex_messages(store: Path, cache: Path) -> None:
    report = reindex(store, cache)
    assert report.problems == []
    assert report.records == report.messages == len(CORPUS)
    rows = query(
        cache,
        "SELECT id, location, segment, offset, frame_size, size, received, date,"
        " from_addr, to_addr, cc_addr, subject, message_id, in_reply_to, state"
        " FROM messages",
    )
    assert len(rows) == len(CORPUS)
    headers = {
        h.id: (path.relative_to(store).as_posix(), o, h)
        for path in segment.segments(store)
        for o, h in segment.scan(path)
    }
    by_id = {message_id(m): m for m in CORPUS.values()}
    for row in rows:
        id_, location, name, offset, frame_size, size, received, *env, state = row
        assert isinstance(id_, str)
        seg, off, h = headers[id_]
        assert (location, name, offset, state) == ("segment", seg, off, "live")
        assert (frame_size, size, received) == (
            h.frame_size,
            h.size,
            "2026-10-23T14:02:11Z",
        )
        # The header block alone gives what the whole message gives.
        full = Envelope.parse(by_id[id_])
        assert tuple(env) == (
            full.date,
            full.from_addr,
            full.to_addr,
            full.cc_addr,
            full.subject,
            full.message_id,
            full.in_reply_to,
        )
    [(n,)] = query(cache, "SELECT count(*) FROM messages WHERE message_id IS NULL")
    assert n >= 1  # no_message_id.eml
    (a, b) = ids("duplicate_message_id_a", "duplicate_message_id_b")
    assert query(cache, "SELECT message_id FROM messages WHERE id = ?", a) == query(
        cache, "SELECT message_id FROM messages WHERE id = ?", b
    )


def test_reindex_attachments(store: Path, cache: Path) -> None:
    reindex(store, cache)
    rows = query(cache, "SELECT blob, file, kind, mime_type, size FROM attachments")
    assert rows
    for blob, file, kind, mime_type, size in rows:
        assert isinstance(blob, str) and isinstance(file, str)
        assert kind in ("attachment", "decoration")
        assert mime_type is None  # the body pass fills it in
        assert size == (store / file).stat().st_size
    # Every blob that a record names, once.
    blobs = {
        p["blob"]
        for path in segment.segments(store)
        for _, h in segment.scan(path)
        for p in h.parts
    }
    assert {r[0] for r in rows} == blobs


def test_reindex_journal_state(store: Path, cache: Path) -> None:
    a, b, c = ids("plain_text_lf", "plain_text_crlf", "pgp_signed")
    w = JournalWriter(store, "work", clock=lambda: T0)
    w.append(
        [
            Mbox(mbox="m1", name="INBOX", role="inbox", sort=1, subscribed=True),
            Mbox(mbox="m2", name="Archive", parent="m1", props={"x": [1]}),
            Add(msg=a, mbox="m1", rid="e1", eid="E1", received=T0, thread="T1"),
            Flags(msg=a, mbox="m1", set=frozenset({"$seen", "$flagged"})),
            Add(msg=b, mbox="m1", rid="e2"),
            Flags(msg=b, mbox="m1", set=frozenset({"$seen"})),
            Add(msg=b, mbox="m2", rid="e2"),
            Add(msg=c, mbox="m2", rid="e3"),
            Flags(msg=c, mbox="m2", set=frozenset({"$seen"})),
        ]
    )
    w.append([Remove(msg=c, mbox="m2", rid="e3")])
    JournalWriter(store, "home", clock=lambda: T0).append(
        [Mbox(mbox="INBOX", name="INBOX"), Add(msg=a, mbox="INBOX", rid="7")]
    )
    report = reindex(store, cache)
    assert report.problems == [] and report.accounts == 2
    assert query(cache, "SELECT * FROM accounts ORDER BY account") == [
        ("home", 2),
        ("work", 10),
    ]
    assert query(cache, "SELECT * FROM mailboxes ORDER BY account, mbox") == [
        ("home", "INBOX", "INBOX", None, None, None, None),
        ("work", "m1", "INBOX", "inbox", None, 1, 1),
        ("work", "m2", "Archive", None, "m1", None, None),
    ]
    assert sorted(query(cache, "SELECT * FROM membership")) == sorted(
        [
            ("home", "INBOX", a, "7", None, None),
            ("work", "m1", a, "e1", "2026-10-23T14:02:11Z", "T1"),
            ("work", "m1", b, "e2", None, None),
            ("work", "m2", b, "e2", None, None),
        ]
    )
    # c left m2, so its flags there are not current.
    assert sorted(query(cache, "SELECT * FROM flags")) == sorted(
        [
            ("work", "m1", a, "$flagged"),
            ("work", "m1", a, "$seen"),
            ("work", "m1", b, "$seen"),
        ]
    )


def put_incoming(store: Path, account: str, data: bytes, mtime: int) -> None:
    path = (
        store / "incoming" / account / f"{message_id(data).removeprefix('sha256:')}.eml"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.utime(path, (mtime, mtime))


def test_reindex_incoming(tmp_path: Path, cache: Path) -> None:
    store = tmp_path / "store"
    names = ["plain_text_lf", "plain_text_crlf", "pgp_signed", "smime_signed"]
    held, dropped, both, in_segment = (CORPUS[n] for n in names)
    make_store(store, [in_segment])
    put_incoming(store, "a", held, 1_700_000_000)
    put_incoming(store, "a", dropped, 1_700_000_000)
    put_incoming(store, "a", both, 1_700_000_000)
    put_incoming(store, "b", both, 1_700_000_000)
    put_incoming(store, "b", in_segment, 1_700_000_000)
    (store / "incoming" / "a" / "not-a-message.eml").write_bytes(b"x")
    a, d, h, s = map(message_id, (held, dropped, both, in_segment))
    JournalWriter(store, "a", clock=lambda: T0).append(
        [Drop(msg=d), Drop(msg=h), Drop(msg=a), Undrop(msg=a)]
    )
    JournalWriter(store, "b", clock=lambda: T0).append([Drop(msg=s)])
    report = reindex(store, cache)
    assert report.problems == []
    assert (report.records, report.incoming, report.messages) == (1, 5, 4)
    assert sorted(query(cache, "SELECT * FROM incoming")) == sorted(
        [
            ("a", a, "held"),
            ("a", d, "dropped"),
            ("a", h, "dropped"),
            ("b", h, "held"),
            ("b", s, "dropped"),
        ]
    )
    rows = query(
        cache,
        "SELECT id, location, segment, size, received, state, subject FROM messages",
    )
    got = {r[0]: r[1:] for r in rows}
    received = "2023-11-14T22:13:20Z"
    assert got[a][:5] == ("incoming", None, len(held), received, "live")
    assert got[d][:5] == ("incoming", None, len(dropped), received, "dropped")
    # Held by b still.
    assert got[h][:5] == ("incoming", None, len(both), received, "live")
    # In a segment, so neither dropped nor counted as in incoming/.
    assert got[s][0] == "segment" and got[s][4] == "live"
    assert got[a][5] == Envelope.parse(held).subject


def test_reindex_torn_and_damaged(
    store: Path, cache: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    [path] = segment.segments(store)
    offsets = [o for o, _ in segment.scan(path)]
    data = path.read_bytes()
    # Damage the second record's body and cut the last one short.
    second_body = offsets[2] - 20
    damaged = bytearray(data[: offsets[-1] + 30])
    damaged[second_body : second_body + 4] = b"\xff" * 4
    path.write_bytes(damaged)
    logging.getLogger("geniza").addHandler(handler := logging.StreamHandler())
    try:
        report = reindex(store, cache)
    finally:
        logging.getLogger("geniza").removeHandler(handler)
    [problem] = report.problems
    assert f"offset {offsets[1]}" in problem
    assert report.records == len(offsets) - 1
    assert report.messages == len(offsets) - 2
    assert "being written" in capsys.readouterr().err


def test_reindex_is_repeatable(store: Path, cache: Path) -> None:
    reindex(store, cache)
    first = query(cache, "SELECT * FROM messages ORDER BY id")
    (cache / "index.sqlite").unlink()
    reindex(store, cache)
    assert query(cache, "SELECT * FROM messages ORDER BY id") == first
    reindex(store, cache)
    assert query(cache, "SELECT * FROM messages ORDER BY id") == first


def test_reindex_cache_of_another_store(
    tmp_path: Path, store: Path, cache: Path
) -> None:
    reindex(store, cache)
    other = tmp_path / "other"
    make_store(other, [CORPUS["plain_text_lf"]])
    assert reindex(other, cache).messages == 1
    assert (cache / "store-id").read_text().strip() == str(read_store(other).id)


@pytest.fixture
def config(tmp_path: Path, store: Path, cache: Path) -> Iterator[Path]:
    path = tmp_path / "config.toml"
    path.write_text(f'[stores.test]\npath = "{store}"\ncache = "{cache}"\n')
    yield path


def test_reindex_command(config: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--config", str(config), "reindex"]) == 0
    out = capsys.readouterr().out
    assert f"messages: {len(CORPUS)} ({len(CORPUS)} records" in out
    assert "problems: 0" in out
    assert cli.main(["--config", str(config), "--json", "reindex"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["messages"] == len(CORPUS) and report["problems"] == []
