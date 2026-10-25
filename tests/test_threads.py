import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from test_index import make_store, query

from geniza.index import schema, threads
from geniza.index.envelope import Envelope
from geniza.index.reindex import reindex, update
from geniza.store.ingest import message_id


def mail(
    mid: str | None,
    refs: tuple[str, ...] = (),
    reply_to: str | None = None,
    subject: str = "Re: the plan",
) -> bytes:
    """A message with the given Message-ID, References and In-Reply-To."""
    lines = ["From: a@example.org", "To: b@example.net", f"Subject: {subject}"]
    if mid is not None:
        lines.append(f"Message-ID: <{mid}>")
    if refs:
        lines.append("References: " + "\r\n ".join(f"<{r}>" for r in refs))
    if reply_to is not None:
        lines.append(f"In-Reply-To: <{reply_to}>")
    return "\r\n".join([*lines, "", "Text.", ""]).encode()


def test_envelope_references() -> None:
    e = Envelope.parse(
        b"References: <a@example.org> <b@example.org>\r\n"
        b"\t<a@example.org>\r\n"
        b"References: <c@example.org> junk\r\n"
        b"In-Reply-To: <c@example.org> (Ann <ann@example.org>'s message)\r\n"
        b"\r\n"
    )
    assert e.references == ("a@example.org", "b@example.org", "c@example.org")
    assert e.in_reply_to == "c@example.org"
    assert Envelope.parse(b"Subject: x\n\n").references == ()


def test_keys() -> None:
    e = Envelope(message_id="m", references=("a", "b"), in_reply_to="b")
    assert threads.keys("sha256:00", e) == ["m", "a", "b"]
    assert threads.keys("sha256:00", Envelope(in_reply_to="m")) == [
        "<sha256:00>",
        "m",
    ]


# --- union-find against connected components ----------------------------------


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    with closing(sqlite3.connect(":memory:", autocommit=True)) as c:
        schema.create(c)
        yield c


def add(conn: sqlite3.Connection, id: str, e: Envelope) -> int:
    conn.execute(
        "INSERT INTO messages (id, location, size, received, state)"
        " VALUES (?, 'segment', 0, '', 'live')",
        (id,),
    )
    return threads.join(conn, id, e)


def partition(conn: sqlite3.Connection) -> set[frozenset[str]]:
    groups: dict[int, set[str]] = {}
    for id, thread in conn.execute("SELECT id, thread_id FROM messages"):
        groups.setdefault(thread, set()).add(id)
    return {frozenset(g) for g in groups.values()}


def components(msgs: list[tuple[str, Envelope]]) -> set[frozenset[str]]:
    """The threads, by repeated merging of sets of linked ids."""
    groups: list[tuple[set[str], set[str]]] = []  # (ids linked, messages)
    for id, e in msgs:
        linked, members = set(threads.keys(id, e)), {id}
        for g in [g for g in groups if g[0] & linked]:
            groups.remove(g)
            linked |= g[0]
            members |= g[1]
        groups.append((linked, members))
    return {frozenset(m) for _, m in groups}


def test_reply_before_its_parent(conn: sqlite3.Connection) -> None:
    reply = add(conn, "r", Envelope(message_id="r", in_reply_to="p"))
    assert add(conn, "p", Envelope(message_id="p")) == reply


def test_a_message_merges_two_threads(conn: sqlite3.Connection) -> None:
    a = add(conn, "a", Envelope(message_id="a"))
    add(conn, "a2", Envelope(message_id="a2", references=("a",)))
    b = add(conn, "b", Envelope(message_id="b"))
    assert a != b
    # The larger thread keeps its number.
    assert add(conn, "c", Envelope(message_id="c", references=("b", "a2"))) == a
    assert partition(conn) == {frozenset(["a", "a2", "b", "c"])}
    assert {t for (t,) in conn.execute("SELECT thread FROM threads")} == {a}


def test_shared_message_id_and_none(conn: sqlite3.Connection) -> None:
    # A sent copy and the list's copy: different messages, one thread.
    first = add(conn, "x1", Envelope(message_id="m"))
    assert add(conn, "x2", Envelope(message_id="m")) == first
    # Without a Message-ID: alone, however alike, unless it names an id.
    n1 = add(conn, "n1", Envelope(subject="Re: x"))
    assert n1 != first
    assert add(conn, "n2", Envelope(subject="Re: x")) not in {first, n1}
    assert add(conn, "n3", Envelope(in_reply_to="m")) == first


def test_many_references(conn: sqlite3.Connection) -> None:
    refs = tuple(f"r{i}" for i in range(1200))
    t = add(conn, "a", Envelope(message_id="a", references=refs))
    assert add(conn, "b", Envelope(message_id="b", in_reply_to="r1100")) == t


IDS = st.sampled_from([f"id{i}" for i in range(8)])
ENVELOPES = st.builds(
    Envelope,
    message_id=st.none() | IDS,
    references=st.lists(IDS, max_size=3, unique=True).map(tuple),
    in_reply_to=st.none() | IDS,
)


@given(st.lists(ENVELOPES, max_size=25))
def test_threads_are_the_connected_components(envelopes: list[Envelope]) -> None:
    msgs = [(f"sha256:{i:02}", e) for i, e in enumerate(envelopes)]
    with closing(sqlite3.connect(":memory:", autocommit=True)) as conn:
        schema.create(conn)
        for id, e in msgs:
            add(conn, id, e)
        assert partition(conn) == components(msgs)
        # Every id linked is in the thread of the messages linking it.
        thread = dict(conn.execute("SELECT id, thread_id FROM messages"))
        known = dict(conn.execute("SELECT message_id, thread FROM threads"))
        for id, e in msgs:
            assert {known[k] for k in threads.keys(id, e)} == {thread[id]}


# --- in the index -------------------------------------------------------------


def thread_of(cache: Path, data: bytes) -> object:
    [(t,)] = query(
        cache, "SELECT thread_id FROM messages WHERE id = ?", message_id(data)
    )
    return t


def test_index_threads_out_of_order_and_merged(tmp_path: Path) -> None:
    store, cache = tmp_path / "store", tmp_path / "cache"
    root = mail("root@example.org", subject="The plan")
    reply = mail("r1@example.org", ("root@example.org",), "root@example.org")
    late = mail("r2@example.org", ("root@example.org", "r1@example.org"))
    other = mail("o@example.org", subject="The plan")
    bridge = mail("b@example.org", ("o@example.org",), "r2@example.org")

    # The reply to a reply comes first; its parent is in a later batch.
    make_store(store, [late, other])
    update(store, cache).close()
    assert thread_of(cache, late) != thread_of(cache, other)
    make_store(store, [root, reply])
    update(store, cache).close()
    assert thread_of(cache, root) == thread_of(cache, reply) == thread_of(cache, late)
    assert thread_of(cache, other) != thread_of(cache, root)  # no subject merging
    make_store(store, [bridge])
    with closing(update(store, cache)) as conn:
        assert len(partition(conn)) == 1

    reindex(store, tmp_path / "rebuilt")
    assert len({thread_of(tmp_path / "rebuilt", m) for m in [root, other]}) == 1
