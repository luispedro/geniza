"""The JMAP client, against the in-process fake server."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from jmapserver import ACCOUNT, CORE, TOKEN, FakeJmap, fake_jmap

from geniza.sync import jmap
from geniza.sync.jmap import (
    CannotCalculateChanges,
    Client,
    JmapError,
    MethodError,
    Session,
)

T0 = datetime(2026, 3, 1, tzinfo=UTC)


def message(n: int) -> bytes:
    return (
        f"From: a@example.org\r\nTo: b@example.net\r\n"
        f"Message-ID: <m{n}@example.org>\r\nSubject: {n}\r\n\r\nbody {n}\r\n"
    ).encode()


@pytest.fixture(scope="module")
def module_server() -> Iterator[FakeJmap]:
    with fake_jmap() as fake:
        yield fake


@pytest.fixture
def server() -> Iterator[FakeJmap]:
    with fake_jmap() as fake:
        yield fake


def connect(fake: FakeJmap) -> Client:
    return Client.connect(fake.url, TOKEN, timeout=10)


def error(fake: FakeJmap, token: str = TOKEN) -> str:
    with pytest.raises(JmapError) as e:
        Client.connect(fake.url, token, timeout=10)
    assert token not in str(e.value)
    return str(e.value)


# --- session -----------------------------------------------------------------


def test_session(server: FakeJmap) -> None:
    server.max_objects_in_get = 7
    c = connect(server)
    assert c.session.account_id == ACCOUNT
    assert c.session.max_objects_in_get == 7
    assert c.session.api_url.endswith("/jmap/api")
    assert c.session.has("Email")
    assert not c.session.has("SieveScript")
    assert TOKEN not in repr(c)


def test_wrong_token(server: FakeJmap) -> None:
    assert "authentication failed" in error(server, "wrong-token")


def test_insecure_url() -> None:
    with pytest.raises(JmapError, match="insecure"):
        Client.connect("http://example.org/jmap/session", TOKEN)


@pytest.mark.parametrize("key", ["apiUrl", "downloadUrl"])
def test_session_insecure_urls(server: FakeJmap, key: str) -> None:
    server.session_overrides = {key: "http://example.org/jmap/x"}
    assert "insecure URL" in error(server)


def test_session_without_mail(server: FakeJmap) -> None:
    server.session_overrides = {"primaryAccounts": {}}
    assert "bad session object" in error(server)


@pytest.mark.parametrize("value", [0, -1, "5", None, True])
def test_session_bad_limits(value: object) -> None:
    data = {
        "capabilities": {
            CORE: {"maxObjectsInGet": value, "maxCallsInRequest": 1}
        },
        "primaryAccounts": {"urn:ietf:params:jmap:mail": "A"},
        "accounts": {"A": {"accountCapabilities": {"urn:ietf:params:jmap:mail": {}}}},
        "apiUrl": "https://h/api", "downloadUrl": "https://h/d", "state": "s",
    }  # fmt: skip
    with pytest.raises(JmapError, match="max_objects_in_get"):
        Session.parse(data)


def test_redirect_refused(server: FakeJmap) -> None:
    url = server.url.replace("/jmap/session", "/redirect")
    with pytest.raises(JmapError, match=r"redirected to .* not followed"):
        Client.connect(url, TOKEN, timeout=10)


def test_unreachable() -> None:
    with pytest.raises(JmapError):
        Client.connect("http://127.0.0.1:1/jmap/session", TOKEN, timeout=10)


# --- method calls ------------------------------------------------------------


@pytest.mark.parametrize(
    "method", ["Email/set", "Email/copy", "Email/import", "Mailbox/set",
               "EmailSubmission/get", "Email/parse"],
)  # fmt: skip
def test_only_read_methods(server: FakeJmap, method: str) -> None:
    c = connect(server)
    with pytest.raises(JmapError):
        c.call([(method, {"accountId": ACCOUNT})])
    assert server.methods == []


def test_too_many_calls(server: FakeJmap) -> None:
    server.max_calls_in_request = 2
    c = connect(server)
    with pytest.raises(JmapError, match="too many"):
        c.call([("Mailbox/get", {"accountId": ACCOUNT, "ids": None})] * 3)
    assert server.methods == []


def test_method_error(server: FakeJmap) -> None:
    c = connect(server)
    with pytest.raises(MethodError) as e:
        c.call([("Mailbox/get", {"accountId": "nope", "ids": None})])
    assert e.value.type == "accountNotFound"
    with pytest.raises(MethodError) as e:
        c.get("Email", ["x"], ["bogus"])
    assert e.value.type == "invalidArguments"


def test_mailboxes(server: FakeJmap) -> None:
    inbox = server.add_mailbox("Inbox", "inbox")
    sub = server.add_mailbox("2020", parent=inbox, color="#3a7")
    c = connect(server)
    got = c.get("Mailbox")
    by_id = {m["id"]: m for m in got.objects}
    assert set(by_id) == {inbox, sub}
    assert by_id[inbox]["role"] == "inbox"
    assert by_id[sub]["parentId"] == inbox
    assert by_id[sub]["color"] == "#3a7"

    server.update_mailbox(sub, name="2021")
    server.destroy_mailbox(inbox)
    new = server.add_mailbox("Archive", "archive")
    ch = c.changes("Mailbox", got.state)
    assert (ch.created, ch.updated, ch.destroyed) == ([new], [sub], [inbox])
    assert ch.new_state == c.get("Mailbox").state
    assert c.changes("Mailbox", ch.new_state).created == []


def test_get_batches(server: FakeJmap) -> None:
    server.max_objects_in_get = 3
    server.max_calls_in_request = 2
    box = server.add_mailbox("Inbox", "inbox")
    ids = [server.add_email(message(i), {box}) for i in range(10)]
    c = connect(server)
    got = c.get("Email", [*ids, "Enope"], ["blobId", "mailboxIds", "receivedAt"])
    assert [e["id"] for e in got.objects] == ids
    assert got.not_found == ["Enope"]
    assert set(got.objects[0]) == {"id", "blobId", "mailboxIds", "receivedAt"}
    assert server.get_sizes == [3, 3, 3, 2]
    assert server.requests == 3  # the session and two API requests
    assert c.get("Email", []).objects == []


def test_cannot_calculate_changes(server: FakeJmap) -> None:
    box = server.add_mailbox("Inbox", "inbox")
    c = connect(server)
    state = c.get("Email", []).state
    server.add_email(message(1), {box})
    server.forget_changes()
    with pytest.raises(CannotCalculateChanges):
        c.changes("Email", state)
    with pytest.raises(CannotCalculateChanges):
        c.changes("Email", "garbage-state")


def test_changes_paged(server: FakeJmap) -> None:
    """Pages are merged: an id created in one page and updated in a later
    one stays created; one destroyed later is only destroyed."""
    server.max_changes = 1
    box = server.add_mailbox("Inbox", "inbox")
    c = connect(server)
    state = c.get("Email", []).state
    a = server.add_email(message(1), {box})
    b = server.add_email(message(2), {box})
    server.update_email(a, keywords={"$seen"})
    d = server.add_email(message(3), {box})
    server.destroy_email(b)
    ch = c.changes("Email", state, max_changes=50)
    assert ch.created == [a, d]
    assert ch.updated == []
    assert ch.destroyed == [b]
    assert server.methods.count("Email/changes") == 5


# --- the full listing --------------------------------------------------------


def test_query_emails(server: FakeJmap, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("geniza.sync.jmap.QUERY_PAGE", 3)
    box = server.add_mailbox("Inbox", "inbox")
    ids = [
        server.add_email(message(i), {box}, received=T0 - timedelta(hours=i))
        for i in range(8)
    ]
    c = connect(server)
    state, listed = c.query_emails()
    assert listed == ids[::-1]
    assert state == c.get("Email", []).state


def test_query_survives_destruction(
    server: FakeJmap, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A message destroyed during the listing shifts nothing; a destroyed
    anchor restarts it; one added shows up in the changes since the state
    returned."""
    monkeypatch.setattr("geniza.sync.jmap.QUERY_PAGE", 2)
    box = server.add_mailbox("Inbox", "inbox")
    ids = [
        server.add_email(message(i), {box}, received=T0 + timedelta(hours=i))
        for i in range(7)
    ]
    calls = 0
    added: list[str] = []

    def meddle(args: dict[str, object]) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:  # before the second page: destroy one already listed
            server.destroy_email(ids[0])
        if calls == 3:  # destroy the anchor of the next page
            server.destroy_email(ids[3])
            added.append(server.add_email(message(99), {box}, received=T0))
        if calls == 6:  # after the restart, add one before the anchor
            added.append(server.add_email(message(98), {box}, received=T0))

    server.before_query = meddle
    c = connect(server)
    state, listed = c.query_emails()
    assert listed == [added[0], ids[1], ids[2], ids[4], ids[5], ids[6]]
    assert c.changes("Email", state).created == [added[1]]


# --- downloads ---------------------------------------------------------------


@settings(max_examples=25, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.binary(max_size=3 << 20))
def test_download_exact(module_server: FakeJmap, blob: bytes) -> None:
    box = module_server.add_mailbox("Inbox")
    id_ = module_server.add_email(blob, {box})
    c = connect(module_server)
    (email,) = c.get("Email", [id_], ["blobId", "size"]).objects
    assert email["size"] == len(blob)
    assert c.download(email["blobId"]) == blob


def test_download_missing(server: FakeJmap) -> None:
    c = connect(server)
    with pytest.raises(JmapError, match="HTTP 404"):
        c.download("Gnothing")


# --- retries -----------------------------------------------------------------


def connect_sleeping(fake: FakeJmap) -> tuple[Client, list[float]]:
    sleeps: list[float] = []
    return Client.connect(fake.url, TOKEN, timeout=10, sleep=sleeps.append), sleeps


def test_retry_transient(server: FakeJmap) -> None:
    box = server.add_mailbox("Inbox")
    c, sleeps = connect_sleeping(server)
    server.busy = [(429, "2"), (503, None), (502, None), (504, " 0 ")]
    assert [m["id"] for m in c.get("Mailbox").objects] == [box]
    # Retry-After when given, else a backoff that doubles on each use.
    assert sleeps == [2.0, 1.0, 2.0, 0.0]
    assert server.methods == ["Mailbox/get"]


def test_retry_session_and_download(server: FakeJmap) -> None:
    box = server.add_mailbox("Inbox")
    id_ = server.add_email(message(1), {box})
    server.busy = [(503, None)]
    c, sleeps = connect_sleeping(server)
    blob_id = server.emails[id_].blob_id
    server.busy = [(429, None), (429, None)]
    assert c.download(blob_id) == message(1)
    assert sleeps == [1.0, 1.0, 2.0]


def test_retry_gives_up(server: FakeJmap) -> None:
    c, sleeps = connect_sleeping(server)
    server.busy = [(503, None)] * (jmap.RETRIES + 1)
    with pytest.raises(JmapError, match="HTTP 503"):
        c.get("Mailbox")
    assert sleeps == [1.0, 2.0, 4.0, 8.0, 16.0]
    assert server.busy == []
    assert server.methods == []


def test_retry_after_too_long(server: FakeJmap) -> None:
    c, sleeps = connect_sleeping(server)
    server.busy = [(429, "3600")]
    with pytest.raises(JmapError, match="retry after 3600 s"):
        c.get("Mailbox")
    assert sleeps == []


@pytest.mark.parametrize("code", [400, 404, 500])
def test_no_retry(server: FakeJmap, code: int) -> None:
    c, sleeps = connect_sleeping(server)
    server.busy = [(code, "1")]
    with pytest.raises(JmapError, match=f"HTTP {code}"):
        c.get("Mailbox")
    assert sleeps == []


def test_retry_after_values() -> None:
    soon = datetime.now(UTC) + timedelta(seconds=30)
    wait = jmap._retry_after(soon.strftime("%a, %d %b %Y %H:%M:%S GMT"))
    assert wait is not None and 25 <= wait <= 30
    assert jmap._retry_after("Mon, 01 Jan 2001 00:00:00 GMT") == 0.0
    assert jmap._retry_after("120") == 120.0
    for bad in [None, "", "soon", "-5", "1.5", "Mon, 01 Jan 2001 00:00:00"]:
        assert jmap._retry_after(bad) is None


# --- changes, as a property ----------------------------------------------------

OPS = st.lists(
    st.tuples(st.sampled_from(["add", "update", "destroy"]), st.integers(0, 50)),
    max_size=30,
)


@settings(max_examples=40, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(OPS, OPS, st.integers(1, 6))
def test_changes_property(
    module_server: FakeJmap,
    before: list[tuple[str, int]],
    after: list[tuple[str, int]],
    max_changes: int,
) -> None:
    """From any state, the created, updated and destroyed ids take the
    ids of then to those of now, and name every message that changed."""
    fake = module_server
    box = fake.add_mailbox("Inbox")
    fake.max_changes = max_changes
    mine: list[str] = []

    def run(ops: list[tuple[str, int]]) -> set[str]:
        changed = set()
        for op, n in ops:
            live = [i for i in mine if i in fake.emails]
            if op == "add" or not live:
                mine.append(fake.add_email(message(n), {box}))
                changed.add(mine[-1])
            elif op == "update":
                fake.update_email(live[n % len(live)], keywords={f"k{n}"})
                changed.add(live[n % len(live)])
            else:
                fake.destroy_email(live[n % len(live)])
        return changed

    run(before)
    c = connect(fake)
    state = c.get("Email", []).state
    then = {i for i in mine if i in fake.emails}
    changed = run(after)
    now = {i for i in mine if i in fake.emails}
    ch = c.changes("Email", state)
    created, updated, destroyed = set(ch.created), set(ch.updated), set(ch.destroyed)
    assert not created & updated and not created & destroyed and not updated & destroyed
    assert (then - destroyed) | created == now
    assert not created & then
    assert updated <= then
    assert changed & now <= created | updated
    assert ch.new_state == c.get("Email", []).state
