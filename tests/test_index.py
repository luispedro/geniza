"""The index: envelopes, the schema, ``reindex`` and ``update`` (headers and
body passes)."""

import json
import logging
import os
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import corpus_files
from hypothesis import example, given, settings
from hypothesis import strategies as st

from geniza import cli
from geniza.config import init_store, read_store
from geniza.index import bodies, schema
from geniza.index.envelope import Envelope
from geniza.index.reindex import catalog, reindex, update
from geniza.mime import tree
from geniza.mime.recipes import Policy
from geniza.mime.text import clean, texts
from geniza.store import reader, segment
from geniza.store.ingest import Catalog, Ingester, message_id
from geniza.store.journal import (
    Add,
    Drop,
    Flags,
    JournalWriter,
    Mbox,
    Promote,
    Purge,
    Remove,
    Undrop,
)
from geniza.store.lock import StoreLock

T0 = datetime(2026, 10, 23, 14, 2, 11, tzinfo=UTC)
CORPUS = {p.stem: p.read_bytes() for p in corpus_files()}
CORPUS_BY_ID = {message_id(m): m for m in CORPUS.values()}
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
        for text in value if isinstance(value, tuple) else [value]:
            if text is not None:
                text.encode("utf-8")
                assert "\n" not in text and "\r" not in text


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
    conn.execute("INSERT INTO accounts VALUES ('a', 1, '')")
    conn.execute("CREATE TABLE stray (x)")
    conn.close()
    conn = schema.connect(path)
    assert conn.execute("SELECT * FROM accounts").fetchall() == [("a", 1, "")]
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
        conn.execute("INSERT INTO accounts VALUES ('a', 1, '')")
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
        full = Envelope.parse(CORPUS_BY_ID[id_])
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
    # The type of the first message naming a blob; every name it has.
    types: dict[str, str] = {}
    names = set()
    for r in reader.records(store):
        nodes = {n.path: n for n in tree.parse(CORPUS_BY_ID[r.header.id]).walk()}
        for part in r.header.parts:
            node = nodes[part["path"]]
            types.setdefault(part["blob"], node.content_type)
            if node.filename:
                names.add((part["blob"], r.header.id, clean(node.filename)))
    for blob, file, kind, mime_type, size in rows:
        assert isinstance(blob, str) and isinstance(file, str)
        assert kind in ("attachment", "decoration")
        assert mime_type == types[blob]
        assert size == (store / file).stat().st_size
    assert set(query(cache, "SELECT * FROM attachment_names")) == names
    assert any(name == "été.pdf" for _, _, name in names)
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
    assert query(cache, "SELECT account, seq FROM accounts ORDER BY account") == [
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


# --- update ------------------------------------------------------------------


def dump(cache: Path) -> dict[str, list[tuple[object, ...]]]:
    """Everything in the index. Thread numbers depend on the order messages
    were indexed in, so each thread is named by its first message id
    instead."""
    tables = {
        t: query(cache, f"SELECT * FROM {t} ORDER BY {order}")
        for t, order in [
            ("attachments", "1"),
            ("attachment_names", "1, 2, 3"),
            ("segments", "1"),
            ("accounts", "1"),
            ("mailboxes", "1, 2"),
            ("membership", "1, 2, 3"),
            ("flags", "1, 2, 3, 4"),
            ("incoming", "1, 2"),
        ]
    }
    name = dict(
        query(cache, "SELECT thread_id, min(id) FROM messages GROUP BY thread_id")
    )
    tables["messages"] = [
        tuple(name[v] if k == "thread_id" else v for k, v in row.items())
        for row in query_dicts(cache, "SELECT * FROM messages ORDER BY 1")
    ]
    tables["threads"] = [
        (k, name[t]) for k, t in query(cache, "SELECT * FROM threads ORDER BY 1")
    ]
    tables["fts"] = fts_terms(cache)
    return tables


def fts_terms(cache: Path) -> list[tuple[object, ...]]:
    """Every term of the search index, by message id (rowids depend on the
    order messages were indexed in)."""
    conn = sqlite3.connect(cache / schema.INDEX_FILE)
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE temp.terms USING fts5vocab(main, fts, instance)"
        )
        return conn.execute(
            "SELECT m.id, t.term, t.col, t.offset FROM temp.terms t"
            " JOIN messages m ON m.rowid = t.doc ORDER BY 1, 2, 3, 4"
        ).fetchall()
    finally:
        conn.close()


def query_dicts(cache: Path, sql: str) -> list[dict[str, object]]:
    conn = sqlite3.connect(cache / schema.INDEX_FILE)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql)]
    finally:
        conn.close()


def rebuilt(store: Path, tmp_path: Path) -> dict[str, list[tuple[object, ...]]]:
    reindex(store, tmp_path / "rebuilt")
    return dump(tmp_path / "rebuilt")


def test_update_builds_a_new_index(store: Path, cache: Path, tmp_path: Path) -> None:
    update(store, cache, text=True).close()
    assert dump(cache) == rebuilt(store, tmp_path)
    [(name, indexed)] = dump(cache)["segments"]
    assert indexed == (store / str(name)).stat().st_size


MONTHS = [datetime(2019, 3, 4, tzinfo=UTC), datetime(2024, 12, 1, tzinfo=UTC), T0]

#: Two threads, "a" and "b", and a message joining them.
THREADED = {
    f"threaded_{mid}": (
        f"From: x@example.org\nSubject: s\nMessage-ID: <{mid}@example.org>\n"
        + "".join(f"References: <{r}@example.org>\n" for r in refs)
        + "\nText.\n"
    ).encode()
    for mid, refs in [
        ("a", []),
        ("a1", ["a"]),
        ("a2", ["a", "a1"]),
        ("b", []),
        ("b1", ["b"]),
        ("ab", ["a1", "b1"]),
    ]
}
POOL = CORPUS | THREADED


@settings(max_examples=20, deadline=None)
@given(
    st.lists(
        st.lists(
            st.tuples(st.sampled_from(sorted(POOL)), st.sampled_from(MONTHS)),
            max_size=6,
        ),
        min_size=1,
        max_size=4,
    ),
    st.integers(1, 6000),
)
def test_update_matches_a_rebuild(
    batches: list[list[tuple[str, datetime]]], cap: int
) -> None:
    """Batches ingested with the catalog of the updated index, into
    segments of several months (records go to earlier ones too while the
    store is being initialized)."""
    with tempfile.TemporaryDirectory() as tmp:
        store, cache = Path(tmp) / "store", Path(tmp) / "cache"
        with StoreLock(store) as lock:
            init_store(store)
            for batch in batches:
                with closing(update(store, cache)) as conn:
                    known = catalog(conn)
                with Ingester(
                    store,
                    lock,
                    policy=EXTRACT_ALL,
                    catalog=known,
                    cap=cap,
                    clock=lambda: T0,
                ) as ing:
                    for name, received in batch:
                        ing.add(POOL[name], received)
        with closing(update(store, cache, text=True)) as conn:
            assert catalog(conn) == Catalog.scan(store)
        assert dump(cache) == rebuilt(store, Path(tmp))


def test_update_promoted_message(tmp_path: Path, cache: Path) -> None:
    store = tmp_path / "store"
    make_store(store, [CORPUS["plain_text_lf"]])
    data = CORPUS["pgp_signed"]
    put_incoming(store, "a", data, 1_700_000_000)
    update(store, cache, text=True).close()
    sql = "SELECT location, received FROM messages WHERE id = ?"
    assert query(cache, sql, message_id(data)) == [("incoming", "2023-11-14T22:13:20Z")]
    assert search(cache, '"design notes"') == {"pgp_signed"}
    make_store(store, [data])
    update(store, cache, text=True).close()
    assert query(cache, sql, message_id(data)) == [("segment", "2026-10-23T14:02:11Z")]
    assert dump(cache) == rebuilt(store, tmp_path)


def test_update_reads_on_after_a_torn_record(
    store: Path, cache: Path, tmp_path: Path
) -> None:
    [path] = segment.segments(store)
    offsets = [o for o, _ in segment.scan(path)]
    data = path.read_bytes()
    path.write_bytes(data[: offsets[-1] + 30])
    update(store, cache).close()
    assert dump(cache)["segments"] == [
        (path.relative_to(store).as_posix(), offsets[-1])
    ]
    assert len(dump(cache)["messages"]) == len(offsets) - 1
    path.write_bytes(data)  # the record is complete now
    update(store, cache, text=True).close()
    assert dump(cache) == rebuilt(store, tmp_path)


def test_update_rebuilds_when_a_segment_changed(
    store: Path, cache: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    update(store, cache).close()
    [path] = segment.segments(store)
    offsets = [o for o, _ in segment.scan(path)]
    logging.getLogger("geniza").addHandler(handler := logging.StreamHandler())
    try:
        path.write_bytes(path.read_bytes()[: offsets[3]])
        update(store, cache).close()
        assert "rebuilding" in capsys.readouterr().err
        assert len(dump(cache)["messages"]) == 3
        path.unlink()
        update(store, cache).close()
        assert "rebuilding" in capsys.readouterr().err
        assert dump(cache)["messages"] == []
    finally:
        logging.getLogger("geniza").removeHandler(handler)


def test_update_reports_a_damaged_header_each_time(
    store: Path, cache: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    [path] = segment.segments(store)
    offsets = [o for o, _ in segment.scan(path)]
    data = bytearray(path.read_bytes())
    data[offsets[3]] ^= 0xFF  # the fourth record's magic
    path.write_bytes(data)
    logging.getLogger("geniza").addHandler(handler := logging.StreamHandler())
    try:
        for _ in range(2):
            update(store, cache).close()
            assert f"offset {offsets[3]}" in capsys.readouterr().err
            assert len(dump(cache)["messages"]) == 3
    finally:
        logging.getLogger("geniza").removeHandler(handler)


def unlink_incoming(store: Path, account: str, data: bytes) -> None:
    name = f"{message_id(data).removeprefix('sha256:')}.eml"
    (store / "incoming" / account / name).unlink(missing_ok=True)


def test_update_follows_journals_and_incoming(tmp_path: Path, cache: Path) -> None:
    store = tmp_path / "store"
    make_store(store, [CORPUS["plain_text_lf"]])
    update(store, cache).close()
    a1, ab, b1 = (THREADED[f"threaded_{k}"] for k in ("a1", "ab", "b1"))
    w = JournalWriter(store, "work", clock=lambda: T0)
    w.append([Mbox(mbox="m1", name="INBOX", role="inbox")])
    for data, rid in [(a1, "1"), (ab, "2"), (b1, "3")]:
        put_incoming(store, "work", data, 1_700_000_000)
        w.append([Add(msg=message_id(data), mbox="m1", rid=rid)])
    update(store, cache, text=True).close()
    assert query(cache, "SELECT account, mbox, rid FROM membership ORDER BY 3") == [
        ("work", "m1", "1"),
        ("work", "m1", "2"),
        ("work", "m1", "3"),
    ]
    sql = "SELECT state FROM messages WHERE id = ?"
    assert query(cache, sql, message_id(ab)) == [("live",)]
    # ab joins the threads of a1 and b1.
    assert query(cache, "SELECT count(DISTINCT thread_id) FROM messages") == [(2,)]
    w.append([Remove(msg=message_id(ab), mbox="m1", rid="2"), Drop(msg=message_id(ab))])
    update(store, cache, text=True).close()
    assert query(cache, sql, message_id(ab)) == [("dropped",)]
    assert len(query(cache, "SELECT * FROM membership")) == 2
    w.append([Undrop(msg=message_id(ab))])
    update(store, cache).close()
    assert query(cache, sql, message_id(ab)) == [("live",)]
    w.append([Drop(msg=message_id(ab)), Purge(msg=message_id(ab))])
    unlink_incoming(store, "work", ab)
    update(store, cache, text=True).close()
    assert query(cache, sql, message_id(ab)) == []
    assert search(cache, "references") == set()
    assert query(cache, "SELECT count(DISTINCT thread_id) FROM messages") == [(3,)]
    assert dump(cache) == rebuilt(store, tmp_path)


#: Messages for incoming/: one of each thread shape, and some corpus ones.
HELD = [*THREADED, "plain_text_lf", "pgp_signed", "html_only_meta_charset"]

OPS = st.one_of(
    st.tuples(
        st.sampled_from(["put", "file", "drop", "undrop", "purge", "flag", "remove"]),
        st.sampled_from(["a", "b"]),
        st.sampled_from(HELD),
    ),
    st.tuples(st.just("promote"), st.just(""), st.sampled_from(HELD)),
)


@settings(max_examples=25, deadline=None)
@given(st.lists(st.lists(OPS, max_size=6), min_size=1, max_size=5))
@example(  # b1 was the only link between b and ab; a file with no event
    [
        [(op, "a", f"threaded_{k}") for op in ["put", "file"] for k in ["b", "b1"]]
        + [("put", "b", "threaded_ab")],
        [("purge", "a", "threaded_b1"), ("file", "b", "pgp_signed")],
    ]
)
def test_update_of_journals_and_incoming_matches_a_rebuild(
    batches: list[list[tuple[str, str, str]]],
) -> None:
    """What sync and incoming/ evaluation do (DESIGN.md, "Incoming"),
    with updates in between."""
    with tempfile.TemporaryDirectory() as tmp:
        store, cache = Path(tmp) / "store", Path(tmp) / "cache"
        make_store(store, [CORPUS["smime_signed"]])
        writers = {k: JournalWriter(store, k, clock=lambda: T0) for k in "ab"}
        for w in writers.values():
            w.append([Mbox(mbox="m1", name="INBOX")])
        for batch in batches:
            for op, account, name in batch:
                data = POOL[name]
                msg = message_id(data)
                w = writers.get(account)
                match op:
                    case "put" | "file":
                        # One modification time per message: a rebuild
                        # takes that of the first account holding it.
                        put_incoming(store, account, data, 1_700_000_000 + len(data))
                        if op == "put":  # else as if sync stopped there
                            w.append([Add(msg=msg, mbox="m1", rid=name)])
                    case "drop":
                        w.append([Drop(msg=msg)])
                    case "undrop":
                        w.append([Undrop(msg=msg)])
                    case "purge":
                        unlink_incoming(store, account, data)
                        w.append([Purge(msg=msg)])
                    case "flag":
                        w.append([Flags(msg=msg, mbox="m1", set=frozenset({"$seen"}))])
                    case "remove":
                        w.append([Remove(msg=msg, mbox="m1", rid=name)])
                    case "promote":
                        [(seg, offset)] = make_store(store, [data])
                        for k, w in writers.items():
                            unlink_incoming(store, k, data)
                            w.append([Promote(msg=msg, segment=seg, offset=offset)])
            update(store, cache, text=True).close()
        assert dump(cache) == rebuilt(store, Path(tmp))


# --- body pass ---------------------------------------------------------------


def search(cache: Path, match: str) -> set[str]:
    """The corpus messages (or ids, for others) an FTS5 query matches."""
    names = {message_id(m): n for n, m in POOL.items()}
    rows = query(
        cache,
        "SELECT m.id FROM fts JOIN messages m ON m.rowid = fts.rowid WHERE fts MATCH ?",
        match,
    )
    return {names.get(str(id_), str(id_)) for (id_,) in rows}


def test_text_of_stripped_messages(store: Path) -> None:
    """What the body pass relies on: a stripped message has the text and
    part headers of the original."""
    for r in reader.records(store):
        data = reader.rebuild(store, r.header, r.stripped)
        assert [(t.path, t.text) for t in texts(r.stripped)] == [
            (t.path, t.text) for t in texts(data)
        ]
        assert [
            (n.path, n.content_type, n.filename) for n in tree.parse(r.stripped).walk()
        ] == [(n.path, n.content_type, n.filename) for n in tree.parse(data).walk()]


def test_reindex_reads_bodies(store: Path, cache: Path) -> None:
    report = reindex(store, cache)
    assert report.bodies == len(CORPUS) and report.problems == []
    assert query(cache, "SELECT count(*) FROM messages WHERE body_read = 0") == [(0,)]
    assert search(cache, "bistro") == {"html_only_meta_charset"}
    assert search(cache, "subject:bistro") == {"html_only_meta_charset"}
    assert search(cache, "body:cafe") == {"html_only_meta_charset"}  # Café
    # Neither the title nor the script is text.
    assert "html_only_meta_charset" not in search(cache, '"not text"')
    # The text of an attached message, and the names of attachments.
    assert "forwarded_rfc822" in search(cache, '"original message text"')
    assert search(cache, 'attachments:"quarterly report pdf"') == {
        "forwarded_rfc822",
        "outlook_related",
    }
    assert search(cache, "attachments:image001") == {"outlook_related"}
    assert search(cache, "attachments:ete") == {"filenames_encoded"}
    assert search(cache, "attachments:отчет") == {"filenames_encoded"}
    # Addresses in their columns: Carol is only in the attached message.
    assert "forwarded_rfc822" not in search(cache, "from_addr:carol")
    assert "plain_text_lf" in search(cache, "to_addr:bob")
    assert search(cache, "olá") == search(cache, "ola") >= {"flowed_unlabelled_utf8"}


def test_update_reads_bodies_only_for_text(store: Path, cache: Path) -> None:
    update(store, cache).close()
    assert query(cache, "SELECT count(*) FROM messages WHERE body_read = 1") == [(0,)]
    assert search(cache, "bistro") == set()
    update(store, cache, text=True).close()
    assert search(cache, "bistro") == {"html_only_meta_charset"}


def test_body_pass_goes_on_where_it_stopped(
    store: Path, cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bodies, "BATCH", 3)
    calls = 0
    read = bodies._read

    def interrupted(*args: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 8:
            raise KeyboardInterrupt
        read(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(bodies, "_read", interrupted)
    with pytest.raises(KeyboardInterrupt):
        update(store, cache, text=True)
    # Two batches committed, the third rolled back.
    assert query(cache, "SELECT count(*) FROM messages WHERE body_read = 1") == [(6,)]
    update(store, cache, text=True).close()
    assert dump(cache) == rebuilt(store, tmp_path)


def test_body_pass_reports_a_damaged_body_once(
    tmp_path: Path, cache: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = tmp_path / "store"
    # Long enough that the headers pass stops well before the frame's end.
    long = b"From: x@example.org\nSubject: long\n\n" + b"".join(
        b"%d\n" % (i * 2654435761 % 2**32) for i in range(30_000)
    )
    make_store(store, [CORPUS["plain_text_lf"], long, CORPUS["pgp_signed"]])
    [path] = segment.segments(store)
    offsets = [o for o, _ in segment.scan(path)]
    data = bytearray(path.read_bytes())
    data[offsets[2] - 1] ^= 0xFF  # the second record's frame checksum
    path.write_bytes(data)
    logging.getLogger("geniza").addHandler(handler := logging.StreamHandler())
    try:
        update(store, cache, text=True).close()
        assert f"offset {offsets[1]}" in capsys.readouterr().err
        update(store, cache, text=True).close()
        assert capsys.readouterr().err == ""
    finally:
        logging.getLogger("geniza").removeHandler(handler)
    # Its headers are indexed; its text is not.
    assert len(dump(cache)["messages"]) == 3
    assert search(cache, "subject:long") == set()
    assert search(cache, "subject:plain") == {"plain_text_lf"}


def test_body_pass_caps_the_text(
    store: Path, cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bodies, "MAX_TEXT", 10)
    reindex(store, cache)
    assert search(cache, "hi") >= {"plain_text_lf"}  # "Hi Bob,"
    assert "plain_text_lf" not in search(cache, "body:plain")


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
