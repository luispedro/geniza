"""Fault injection (DESIGN.md, "Write ordering and crash safety").

A scenario like a first sync (store creation, batches of messages into
segments and their events into the journal, the end of initialization, a
batch after it) runs in a forked child that dies with ``os._exit`` at one
step of the write ordering: before or after an ``fsync``, ``rename`` or
``link``. Before an ``fsync`` of a file, the unsynced data can also be torn
(the last byte missing) or zero-filled, as after a power loss.

The store must then pass ``verify`` once a writer has opened it, and
running the scenario again must give the store a crash-free run gives: the
same messages, each recorded once, the same replayed journal, no orphaned
attachment files and no temp files.

A second scenario does the same with ``geniza sync`` against the fake
JMAP server, reaching ``sync.json`` and the evaluation of ``incoming/``.
"""

import os
import stat
import traceback
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from conftest import corpus_files
from jmapserver import TOKEN, FakeJmap, fake_jmap

from geniza.config import Config, init_store, is_initialized, mark_initialized, parse
from geniza.mime.recipes import Policy
from geniza.store import incoming, segment
from geniza.store.ingest import Catalog, Ingester
from geniza.store.journal import (
    Add,
    Drop,
    Event,
    Flags,
    Gone,
    JournalWriter,
    Mbox,
    Promote,
    Purge,
    State,
    read_events,
    replay,
)
from geniza.store.lock import StoreLock
from geniza.sync.base import SyncFile, read_sync, sync_store
from geniza.sync.jmap import Client
from geniza.verify import verify

T0 = datetime(2026, 10, 23, 14, 2, 11, tzinfo=UTC)
OLD = datetime(2019, 3, 4, 5, 6, 7, tzinfo=UTC)
LATER = datetime(2025, 5, 6, tzinfo=UTC)
EXTRACT_ALL = Policy(min_size=1, decoration_min_size=1)
CORPUS = {p.stem: p.read_bytes() for p in corpus_files()}
CORPUS["reply"] = CORPUS["base64_76_crlf"].replace(b"Subject: ", b"Subject: Re: ", 1)
ACCOUNT = "personal"

#: Batches of (message, received). The store is initialized after the
#: second; "reply" then reuses an attachment file of 2019.
BATCHES = [
    [("plain_text_lf", OLD), ("base64_76_crlf", OLD), ("outlook_related", LATER)],
    [("base64_76_crlf", OLD), ("forwarded_rfc822", OLD), ("apple_inline", T0)],
    [("reply", OLD), ("qp_text_attachments", OLD), ("smime_signed", T0)],
]
INITIALIZED_AFTER = 1

#: Exit statuses of the child.
CRASHED, FINISHED, NOT_APPLICABLE = 70, 71, 72


def clock() -> datetime:
    return T0


def scenario(store: Path) -> None:
    for i, batch in enumerate(BATCHES):
        with StoreLock(store) as lock:
            init_store(store, clock=clock)
            events: list[Event] = [Mbox(mbox="m1", name="INBOX", role="inbox")]
            with Ingester(store, lock, policy=EXTRACT_ALL, clock=clock) as ing:
                for name, received in batch:
                    added = ing.add(CORPUS[name], received)
                    events.append(Add(msg=added.id, mbox="m1", rid=name))
                    events.append(Flags(msg=added.id, mbox="m1", set=frozenset()))
            JournalWriter(store, ACCOUNT, clock=clock).append(events)
            if i == INITIALIZED_AFTER:
                mark_initialized(store, clock=clock)


def recover(store: Path) -> None:
    """What every writer does when it opens the store."""
    with StoreLock(store) as lock:
        init_store(store, clock=clock)
        Ingester(store, lock, clock=clock).close()


def damage(fd: int, variant: str) -> bool:
    """Tear or zero-fill the end of the file ``fd``; False if it is not a
    regular file or is empty."""
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_size == 0:
        return False
    if variant == "torn":
        os.ftruncate(fd, st.st_size - 1)
    else:
        n = min(8, st.st_size)
        os.pwrite(fd, bytes(n), st.st_size - n)
    return True


def crash_at(step: int, variant: str) -> None:
    """Make the step-th call before or after ``os.fsync``, ``os.rename`` or
    ``os.link`` end the process. Variants other than "clean" apply only
    before an ``fsync``, and damage the file first."""
    count = 0

    def point(fn: str, args: tuple[Any, ...], before: bool) -> None:
        nonlocal count
        count += 1
        if count != step:
            return
        if variant != "clean" and not (
            before and fn == "fsync" and damage(args[0], variant)
        ):
            os._exit(NOT_APPLICABLE)
        os._exit(CRASHED)

    def wrap(name: str) -> None:
        real: Callable[..., Any] = getattr(os, name)

        def wrapped(*args: Any, **kw: Any) -> Any:
            point(name, args, before=True)
            result = real(*args, **kw)
            point(name, args, before=False)
            return result

        setattr(os, name, wrapped)

    for name in ("fsync", "rename", "link"):
        wrap(name)


def run_child(fn: Callable[[], None], step: int = 0, variant: str = "clean") -> int:
    """Run ``fn`` in a forked child that crashes at ``step`` (if not 0)."""
    pid = os.fork()
    if pid == 0:
        status = FINISHED
        try:
            if step:
                crash_at(step, variant)
            fn()
        except BaseException:
            traceback.print_exc()
            status = 1
        finally:
            os._exit(status)
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status)


def run_until_crash(store: Path, step: int, variant: str) -> int:
    return run_child(lambda: scenario(store), step, variant)


def journal_state(store: Path) -> State:
    return replace(replay(store, ACCOUNT), seq=0)


def temp_files(store: Path) -> list[str]:
    return sorted(str(p) for p in store.rglob("*.tmp"))


@pytest.fixture(scope="module")
def expected(tmp_path_factory: pytest.TempPathFactory) -> tuple[set[str], State]:
    store = tmp_path_factory.mktemp("crash-free")
    scenario(store)
    report = verify(store)
    assert not report.problems
    assert not report.orphans
    return set(Catalog.scan(store).messages), journal_state(store)


@pytest.mark.parametrize("variant", ["clean", "torn", "zeros"])
def test_crash_at_every_step(
    tmp_path: Path, variant: str, expected: tuple[set[str], State]
) -> None:
    ids, state = expected
    crashes = 0
    for step in range(1, 10_000):
        store = tmp_path / str(step)
        status = run_until_crash(store, step, variant)
        if status == FINISHED:
            break
        if status == NOT_APPLICABLE:
            continue
        assert status == CRASHED, f"step {step}: child exited with {status}"
        crashes += 1

        recover(store)
        report = verify(store)
        assert not report.problems, f"step {step}: {report.problems}"

        scenario(store)
        report = verify(store)
        assert not report.problems, f"step {step}: {report.problems}"
        assert not report.orphans, f"step {step}"
        assert report.records == report.messages == len(ids), f"step {step}"
        assert set(Catalog.scan(store).messages) == ids, f"step {step}"
        assert journal_state(store) == state, f"step {step}"
        assert not temp_files(store), f"step {step}"
    else:
        pytest.fail("the scenario never finished")
    assert crashes > 20


def test_child_crashes_at_the_requested_step(tmp_path: Path) -> None:
    assert run_until_crash(tmp_path / "a", 1, "clean") == CRASHED
    assert segment.segments(tmp_path / "a") == []


# --- sync -----------------------------------------------------------------
#
# The same, with ``geniza sync`` against the fake JMAP server writing the
# store: stages of changes on the server, each followed by a sync, so the
# crash can also hit ``sync.json`` (step 4 of the write ordering) and the
# evaluation of ``incoming/``. Children start their own server: the parent
# stays single-threaded, as ``fork`` needs. After a crash, the server is
# made again at the stage the child had reached (a server never goes back
# in time) and the remaining stages run in another child.

DAY = timedelta(days=1)


def stage_0(fake: FakeJmap, ids: dict[str, str]) -> None:
    inbox = fake.add_mailbox("Inbox", "inbox")
    archive = fake.add_mailbox("Archive", "archive")
    trash = fake.add_mailbox("Trash", "trash")
    ids |= {"inbox": inbox, "archive": archive, "trash": trash}
    for name, box, received in [
        ("plain_text_lf", inbox, OLD),
        ("base64_76_crlf", archive, OLD),
        ("outlook_related", inbox, LATER),
        ("apple_inline", inbox, T0 - DAY),
        ("forwarded_rfc822", inbox, T0 - 2 * DAY),
    ]:
        ids[name] = fake.add_email(CORPUS[name], {box}, {"$seen"}, received)


def stage_1(fake: FakeJmap, ids: dict[str, str]) -> None:
    inbox, archive = ids["inbox"], ids["archive"]
    fake.update_email(ids["plain_text_lf"], mailboxes={archive})
    fake.update_email(ids["base64_76_crlf"], keywords={"$seen", "$flagged"})
    fake.destroy_email(ids["forwarded_rfc822"])
    ids["reply"] = fake.add_email(CORPUS["reply"], {inbox}, received=OLD)
    for name in ("qp_text_attachments", "smime_signed"):
        ids[name] = fake.add_email(CORPUS[name], {inbox}, received=T0 + DAY / 2)
    fake.add_mailbox("Lists", parent=archive)


def stage_2(fake: FakeJmap, ids: dict[str, str]) -> None:
    fake.update_email(ids["smime_signed"], mailboxes={ids["archive"]})
    fake.update_email(ids["apple_inline"], keywords=set())


def stage_3(fake: FakeJmap, ids: dict[str, str]) -> None:
    fake.destroy_email(ids["reply"])
    fake.destroy_mailbox(ids["trash"])
    late = T0 + 44 * DAY
    fake.add_email(CORPUS["calendar_alternative"], {ids["inbox"]}, received=late)


#: Changes on the server, and the time of the sync that follows them. The
#: first sync ends the initialization; the second holds new mail and finds
#: a held message gone; the third promotes one, now older than ``max_age``,
#: and drops the gone one; the fourth purges it, finds a mailbox gone and
#: holds a new message.
STAGES = [
    (stage_0, T0),
    (stage_1, T0 + DAY),
    (stage_2, T0 + 9 * DAY),
    (stage_3, T0 + 45 * DAY),
]


def sync_config(root: Path, fake: FakeJmap) -> Config:
    data = {
        "stores": {"s": {"path": str(root / "store"), "cache": str(root / "cache")}},
        "accounts": {
            ACCOUNT: {
                "store": "s",
                "type": "jmap",
                "url": fake.url,
                "token_command": f"printf %s {TOKEN}",
            }
        },
    }
    return parse(data, root / "config.toml", {})


def sync_scenario(root: Path, start: int = 0) -> None:
    """Run the stages from ``start`` on, against a new server brought to
    that stage. The stage reached is written to ``root/stage``."""
    with fake_jmap() as fake:
        config = sync_config(root, fake)
        store = config.stores["s"]
        ids: dict[str, str] = {}
        for i, (change, now) in enumerate(STAGES):
            change(fake, ids)
            if i < start:
                continue
            (root / "stage").write_text(str(i))
            [report] = sync_store(
                config,
                store,
                [config.accounts[ACCOUNT]],
                connect=lambda url, token: Client.connect(url, token, timeout=10),
                clock=lambda now=now: now,
            )
            assert report.ok, report.text()


@dataclass(frozen=True)
class Outcome:
    messages: set[str]
    state: State
    sync: SyncFile
    held: set[str]


def outcome(root: Path) -> Outcome:
    store = root / "store"
    return Outcome(
        messages=set(Catalog.scan(store).messages),
        state=journal_state(store),
        sync=read_sync(store, ACCOUNT),
        held=set(incoming.held(store, ACCOUNT)),
    )


@pytest.fixture(scope="module")
def sync_expected(tmp_path_factory: pytest.TempPathFactory) -> Outcome:
    root = tmp_path_factory.mktemp("sync-crash-free")
    assert run_child(partial(sync_scenario, root)) == FINISHED
    store = root / "store"
    report = verify(store)
    assert not report.problems
    assert not report.orphans
    assert is_initialized(store)
    kinds = {type(e) for e in read_events(store, ACCOUNT)}
    assert {Gone, Promote, Drop, Purge} <= kinds
    result = outcome(root)
    assert result.sync.jmap and result.held
    return result


@pytest.mark.parametrize("variant", ["clean", "torn", "zeros"])
def test_sync_crash_at_every_step(
    tmp_path: Path, variant: str, sync_expected: Outcome
) -> None:
    crashes = 0
    for step in range(1, 10_000):
        root = tmp_path / str(step)
        root.mkdir()
        status = run_child(partial(sync_scenario, root), step, variant)
        if status == FINISHED:
            break
        if status == NOT_APPLICABLE:
            continue
        assert status == CRASHED, f"step {step}: child exited with {status}"
        crashes += 1

        store = root / "store"
        recover(store)
        report = verify(store)
        assert not report.problems, f"step {step}: {report.problems}"

        start = int((root / "stage").read_text())
        status = run_child(partial(sync_scenario, root, start))
        assert status == FINISHED, f"step {step}: rerun exited with {status}"
        report = verify(store)
        assert not report.problems, f"step {step}: {report.problems}"
        assert not report.orphans, f"step {step}"
        assert outcome(root) == sync_expected, f"step {step}"
        assert is_initialized(store), f"step {step}"
        assert not temp_files(store), f"step {step}"
    else:
        pytest.fail("the scenario never finished")
    assert crashes > 20
