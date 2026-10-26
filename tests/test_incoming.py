"""incoming/: placement, evaluation and its application (DESIGN.md,
"Incoming: recent INBOX mail"), driven by a fake source: a scripted
sequence of mailbox snapshots, synced into the journal as a sync does."""

import os
import stat
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from geniza.config import IncomingSettings
from geniza.store import incoming, journal, segment
from geniza.store.incoming import Evaluation, evaluate
from geniza.store.ingest import Held, Ingester, message_id
from geniza.store.journal import (
    Add,
    Drop,
    Event,
    Fate,
    Flags,
    JournalWriter,
    Mailbox,
    Mbox,
    Promote,
    Purge,
    Remove,
    State,
    Undrop,
)
from geniza.store.lock import StoreLock
from geniza.verify import verify

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
DAY = timedelta(days=1)
SETTINGS = IncomingSettings()
BOXES = {
    "I": ("Inbox", "inbox"),
    "A": ("Archive", "archive"),
    "W": ("Work", None),
    "T": ("Trash", "trash"),
    "J": ("Junk", "junk"),
}


def message(i: int) -> bytes:
    return (
        f"From: sender{i}@example.org\r\nTo: me@example.net\r\n"
        f"Subject: message {i}\r\nMessage-ID: <{i}@example.org>\r\n\r\n"
        f"Body of message {i}.\r\n"
    ).encode()


def msg(i: int) -> str:
    return message_id(message(i))


# --- a fake source and a sync that journals it ---------------------------


@dataclass
class Server:
    """One account's mailboxes as a JMAP server has them: email id →
    (message, received, mailbox keys)."""

    boxes: dict[str, tuple[str, str | None]] = field(default_factory=BOXES.copy)
    mail: dict[str, tuple[bytes, datetime, set[str]]] = field(default_factory=dict)

    def deliver(self, i: int, at: datetime, box: str = "I") -> None:
        self.mail[f"e{i}"] = (message(i), at, {box})

    def move(self, i: int, *boxes: str) -> None:
        self.mail[f"e{i}"][2].clear()
        self.mail[f"e{i}"][2].update(boxes)

    def expunge(self, i: int) -> None:
        del self.mail[f"e{i}"]

    def present(self) -> set[str]:
        return {message_id(data) for data, _, boxes in self.mail.values() if boxes}


@dataclass
class Account:
    store: Path
    name: str = "acct"
    server: Server = field(default_factory=Server)
    settings: IncomingSettings = SETTINGS
    #: What the last evaluation returned as absent (``sync.json``'s).
    absent: frozenset[str] = frozenset()
    #: Raised by the journal writer on the evaluation's batch, if set.
    crash: Callable[[list[Event]], bool] | None = None

    def state(self) -> State:
        return journal.replay(self.store, self.name)

    def run(
        self, now: datetime, *, allow_mass_drop: bool = False, complete: bool = True
    ) -> Evaluation | None:
        """Sync the server into the store, then evaluate ``incoming/``."""
        with (
            StoreLock(self.store) as lock,
            Ingester(self.store, lock, clock=lambda: now) as ing,
        ):
            writer = JournalWriter(self.store, self.name, clock=lambda: now)
            writer.append(self._sync(ing, self.state(), now))
            if not complete:
                return None
            ev = evaluate(
                self.state(),
                incoming.held(self.store, self.name),
                ing.catalog.messages,
                self.settings,
                now=now,
                absent_before=self.absent,
                allow_mass_drop=allow_mass_drop,
            )
            if self.crash is not None:
                crash = self.crash
                append = writer.append

                def failing(events: Any) -> list[Event]:
                    events = list(events)
                    if crash(events):
                        raise OSError("crash")
                    return append(events)

                writer.append = failing  # type: ignore[method-assign]
            incoming.apply(ing, writer, ev)
            self.absent = ev.absent
            return ev

    def _sync(self, ing: Ingester, state: State, now: datetime) -> list[Event]:
        server = self.server
        events: list[Event] = []
        for key, (name, role) in server.boxes.items():
            box = state.mailboxes.get(key)
            if box is None or (box.name, box.role) != (name, role):
                events.append(Mbox(mbox=key, name=name, role=role))
        for mbox, rids in state.contents.items():
            for rid, m in rids.items():
                if rid not in server.mail or mbox not in server.mail[rid][2]:
                    events.append(Remove(msg=m, mbox=mbox, rid=rid))
        for rid, (data, received, boxes) in server.mail.items():
            m = state.eids.get(rid)
            if m is None or not self._stored(ing, m):
                where = [Mailbox(*server.boxes[b]) for b in boxes]
                if incoming.placed_in_incoming(where, received, self.settings, now):
                    m = ing.hold(data, received, self.name).id
                else:
                    m = ing.add(data, received).id
            for b in sorted(boxes):
                if rid not in state.contents.get(b, {}):
                    events.append(
                        Add(msg=m, mbox=b, rid=rid, eid=rid, received=received)
                    )
                    events.append(Flags(msg=m, mbox=b, set=frozenset()))
        return events

    def _stored(self, ing: Ingester, m: str) -> bool:
        return (
            m in ing.catalog.messages
            or incoming.file_path(self.store, self.name, m).exists()
        )

    def held(self) -> set[str]:
        return set(incoming.files(self.store, self.name))

    def fate(self, m: str) -> str | None:
        fate = self.state().fates.get(m)
        return None if fate is None else fate.status


def records(store: Path) -> dict[str, int]:
    """Message id → number of records in the store."""
    counts: dict[str, int] = {}
    for path in segment.segments(store):
        for _, header in segment.scan(path):
            counts[header.id] = counts.get(header.id, 0) + 1
    return counts


@pytest.fixture
def acct(tmp_path: Path) -> Account:
    return Account(tmp_path / "store")


# --- placement -----------------------------------------------------------


def test_first_sync_holds_only_recent_inbox_mail(acct: Account) -> None:
    acct.server.deliver(1, T0 - 30 * DAY)
    acct.server.deliver(2, T0 - DAY)
    acct.server.deliver(3, T0 - DAY, "A")
    acct.server.deliver(4, T0 - DAY, "J")
    acct.run(T0)
    assert acct.held() == {msg(2)}
    assert records(acct.store) == {msg(1): 1, msg(3): 1, msg(4): 1}


def test_held_file_is_raw_read_only_and_dated(acct: Account) -> None:
    received = T0 - timedelta(hours=5, seconds=7)
    acct.server.deliver(1, received)
    acct.run(T0)
    path = incoming.file_path(acct.store, acct.name, msg(1))
    assert path.read_bytes() == message(1)
    assert stat.S_IMODE(path.stat().st_mode) == 0o444
    assert incoming.received_of(path.stat()) == received
    assert incoming.held(acct.store, acct.name) == {msg(1): received}


def test_hold(tmp_path: Path) -> None:
    store = tmp_path / "store"
    with StoreLock(store) as lock, Ingester(store, lock, clock=lambda: T0) as ing:
        first = ing.hold(message(1), T0, "a")
        assert isinstance(first, Held) and first.new
        again = ing.hold(message(1), T0 + DAY, "a")
        assert again == replace(first, new=False)
        # Another account gets its own copy.
        other = ing.hold(message(1), T0, "b")
        assert isinstance(other, Held) and other.new and other.path != first.path
        # A message a segment has is not held.
        added = ing.add(message(2), T0)
        assert ing.hold(message(2), T0, "a") == replace(added, parts=(), new=False)
        assert set(incoming.files(store, "a")) == {msg(1)}


def test_hold_removes_temp_files(tmp_path: Path) -> None:
    store = tmp_path / "store"
    directory = incoming.incoming_dir(store, "a")
    directory.mkdir(parents=True)
    temp = directory / f".{msg(1).removeprefix('sha256:')}.0123456789abcdef.tmp"
    temp.write_bytes(b"torn")
    other = directory / "notes.txt"
    other.write_bytes(b"")
    with StoreLock(store) as lock, Ingester(store, lock, clock=lambda: T0) as ing:
        ing.hold(message(2), T0, "a")
    assert not temp.exists()
    assert other.exists()
    assert set(incoming.files(store, "a")) == {msg(2)}


@pytest.mark.parametrize(
    ("box", "entries", "expected"),
    [
        (Mailbox("Inbox", "inbox"), ("INBOX",), True),
        (Mailbox("Inbox", "inbox"), ("inbox",), True),
        (Mailbox("INBOX"), ("INBOX",), True),
        (Mailbox("Lists/foo"), ("Lists/foo",), True),
        (Mailbox("lists/foo"), ("Lists/foo",), False),
        (Mailbox("Junk", "junk"), ("INBOX",), False),
        (Mailbox("Spam", "junk"), ("INBOX", "junk"), True),
    ],
)
def test_matches(box: Mailbox, entries: tuple[str, ...], expected: bool) -> None:
    assert incoming.matches(box, entries) is expected


def test_placement_age() -> None:
    inbox = [Mailbox("Inbox", "inbox")]
    assert incoming.placed_in_incoming(inbox, T0 - 6 * DAY, SETTINGS, T0)
    assert not incoming.placed_in_incoming(inbox, T0 - 7 * DAY, SETTINGS, T0)
    assert not incoming.placed_in_incoming([], T0, SETTINGS, T0)
    both = [Mailbox("Work"), *inbox]
    assert incoming.placed_in_incoming(both, T0, SETTINGS, T0)


# --- evaluation ----------------------------------------------------------


def test_filed_message_is_promoted(acct: Account) -> None:
    acct.server.deliver(1, T0)
    acct.run(T0)
    acct.server.move(1, "W")
    ev = acct.run(T0 + DAY)
    assert ev is not None and ev.promote == (msg(1),)
    assert acct.held() == set()
    assert records(acct.store) == {msg(1): 1}
    fate = acct.state().fates[msg(1)]
    (name, offset) = acct_location(acct, msg(1))
    assert (fate.status, fate.segment, fate.offset) == ("promoted", name, offset)
    header, _ = segment.read(acct.store / name, offset)
    assert header.received == T0


def acct_location(acct: Account, m: str) -> tuple[str, int]:
    for path in segment.segments(acct.store):
        for offset, header in segment.scan(path):
            if header.id == m:
                return path.relative_to(acct.store).as_posix(), offset
    raise KeyError(m)


def test_message_in_inbox_is_promoted_at_max_age(acct: Account) -> None:
    acct.server.deliver(1, T0)
    acct.run(T0)
    acct.run(T0 + 6 * DAY)
    assert acct.held() == {msg(1)}
    acct.run(T0 + 7 * DAY)
    assert acct.held() == set()
    assert acct.fate(msg(1)) == "promoted"


def test_trash_is_not_filing(acct: Account) -> None:
    acct.server.deliver(1, T0)
    acct.server.deliver(2, T0)
    acct.run(T0)
    acct.server.move(1, "T")
    acct.server.move(2, "J")
    acct.run(T0 + DAY)
    acct.run(T0 + 2 * DAY)
    assert acct.held() == {msg(1), msg(2)}
    acct.run(T0 + 7 * DAY)
    assert acct.held() == set()
    assert records(acct.store) == {msg(1): 1, msg(2): 1}


def test_deleted_message_is_dropped_after_two_runs_then_purged(
    acct: Account,
) -> None:
    acct.server.deliver(1, T0)
    acct.server.deliver(2, T0)
    acct.server.deliver(3, T0)
    acct.run(T0)
    acct.server.move(1, "T")
    acct.run(T0 + DAY)
    acct.server.expunge(1)  # the Trash is emptied
    ev = acct.run(T0 + 2 * DAY)
    assert ev is not None and ev.drop == () and ev.absent == {msg(1)}
    assert acct.fate(msg(1)) is None
    ev = acct.run(T0 + 2 * DAY + timedelta(hours=1))
    assert ev is not None and ev.drop == (msg(1),)
    assert acct.fate(msg(1)) == "dropped"
    assert acct.held() == {msg(1), msg(2), msg(3)}  # kept for the grace period
    # Still dropped, not promoted, past max_age.
    acct.run(T0 + 20 * DAY)
    assert acct.fate(msg(1)) == "dropped"
    assert msg(1) not in records(acct.store)
    acct.run(T0 + 2 * DAY + 30 * DAY)
    assert acct.fate(msg(1)) == "dropped"
    acct.run(T0 + 2 * DAY + 30 * DAY + timedelta(hours=1))
    assert acct.fate(msg(1)) == "purged"
    assert msg(1) not in acct.held()
    assert msg(1) not in records(acct.store)


def test_imap_move_race_is_not_a_drop(acct: Account) -> None:
    """A message moved from INBOX to a mailbox synced earlier in the same
    run looks absent for one run, and is found in its new mailbox on the
    next."""
    acct.server.deliver(1, T0)
    acct.server.deliver(2, T0)
    acct.server.deliver(3, T0)
    acct.run(T0)
    acct.server.move(1)  # in flight: in no mailbox the run saw
    ev = acct.run(T0 + DAY)
    assert ev is not None and ev.absent == {msg(1)}
    acct.server.move(1, "A")
    ev = acct.run(T0 + DAY + timedelta(minutes=5))
    assert ev is not None and ev.drop == () and ev.promote == (msg(1),)
    assert acct.fate(msg(1)) == "promoted"


def test_absence_must_be_in_consecutive_runs(acct: Account) -> None:
    for i in range(1, 4):
        acct.server.deliver(i, T0)
    acct.run(T0)
    data = acct.server.mail["e1"]
    for day in (1, 3):
        acct.server.expunge(1)
        acct.run(T0 + day * DAY)
        acct.server.mail["e1"] = data
        acct.run(T0 + (day + 1) * DAY)
    assert acct.fate(msg(1)) is None


def test_restored_during_grace_is_undropped(acct: Account) -> None:
    for i in range(1, 4):
        acct.server.deliver(i, T0)
    acct.run(T0)
    data = acct.server.mail["e1"]
    acct.server.expunge(1)
    acct.run(T0 + DAY)
    acct.run(T0 + 2 * DAY)
    assert acct.fate(msg(1)) == "dropped"
    acct.server.mail["e1"] = data
    ev = acct.run(T0 + 3 * DAY)
    assert ev is not None and ev.undrop == (msg(1),) and ev.promote == ()
    assert acct.fate(msg(1)) is None
    assert acct.held() == {msg(1), msg(2), msg(3)}
    # Evaluated normally again: filed, it is promoted.
    acct.server.expunge(1)
    acct.run(T0 + 4 * DAY)
    acct.server.mail["e1"] = data
    acct.server.move(1, "W")
    ev = acct.run(T0 + 5 * DAY)
    assert ev is not None and ev.promote == (msg(1),)


def test_restored_and_filed_is_undropped_and_promoted(acct: Account) -> None:
    for i in range(1, 4):
        acct.server.deliver(i, T0)
    acct.run(T0)
    data = acct.server.mail["e1"]
    acct.server.expunge(1)
    acct.run(T0 + DAY)
    acct.run(T0 + 2 * DAY)
    acct.server.mail["e1"] = (data[0], data[1], {"W"})
    ev = acct.run(T0 + 3 * DAY)
    assert ev is not None and ev.undrop == (msg(1),) and ev.promote == (msg(1),)
    assert acct.fate(msg(1)) == "promoted"
    events = list(journal.read_events(acct.store, acct.name))
    assert [type(e) for e in events[-2:]] == [Undrop, Promote]


def test_restored_after_purge_is_held_again(acct: Account) -> None:
    for i in range(1, 4):
        acct.server.deliver(i, T0)
    acct.run(T0)
    data = acct.server.mail["e1"]
    acct.server.expunge(1)
    acct.run(T0 + DAY)
    acct.run(T0 + 2 * DAY)
    acct.run(T0 + 40 * DAY)
    assert acct.fate(msg(1)) == "purged"
    # Restored from a backup of the server: downloaded again.
    acct.server.mail["e1"] = (data[0], T0 + 41 * DAY, {"I"})
    ev = acct.run(T0 + 41 * DAY)
    assert ev is not None and ev.undrop == (msg(1),)
    assert acct.fate(msg(1)) is None
    assert msg(1) in acct.held()


def test_on_delete_keep_promotes_deleted_mail(acct: Account) -> None:
    acct.settings = replace(SETTINGS, on_delete="keep")
    acct.server.deliver(1, T0)
    acct.run(T0)
    acct.server.expunge(1)
    for day in range(1, 7):
        ev = acct.run(T0 + day * DAY)
        assert ev is not None and ev.drop == () and ev.absent == frozenset()
    assert acct.held() == {msg(1)}
    acct.run(T0 + 7 * DAY)
    assert acct.fate(msg(1)) == "promoted"
    assert records(acct.store) == {msg(1): 1}
    events = list(journal.read_events(acct.store, acct.name))
    assert any(isinstance(e, Remove) and e.msg == msg(1) for e in events)


def test_empty_listing_trips_the_guard(acct: Account) -> None:
    n = incoming.MASS_DROP_MIN + 1
    for i in range(n):
        acct.server.deliver(i, T0)
    acct.run(T0)
    acct.server.mail.clear()
    acct.run(T0 + DAY)
    ev = acct.run(T0 + 2 * DAY)
    assert ev is not None and ev.drop == ()
    assert ev.guard is not None and "max_drop_fraction" in ev.guard
    assert set(ev.withheld) == {msg(i) for i in range(n)}
    assert all(acct.fate(msg(i)) is None for i in range(n))
    # It stays tripped while the listing is empty.
    ev = acct.run(T0 + 3 * DAY)
    assert ev is not None and ev.guard is not None
    # --allow-mass-drop overrides it.
    ev = acct.run(T0 + 4 * DAY, allow_mass_drop=True)
    assert ev is not None and len(ev.drop) == n and ev.guard is None
    assert all(acct.fate(msg(i)) == "dropped" for i in range(n))


@pytest.mark.parametrize("held", [1, incoming.MASS_DROP_MIN])
def test_few_drops_are_not_a_mass_drop(acct: Account, held: int) -> None:
    """Dropping every held message, if there are few, passes the fraction."""
    for i in range(held):
        acct.server.deliver(i, T0)
    acct.run(T0)
    acct.server.mail.clear()
    acct.run(T0 + DAY)
    ev = acct.run(T0 + 2 * DAY)
    assert ev is not None and ev.guard is None and len(ev.drop) == held


def test_guard_by_count(acct: Account) -> None:
    acct.settings = replace(SETTINGS, max_drop_count=2, max_drop_fraction=1.0)
    for i in range(1, 5):
        acct.server.deliver(i, T0)
    acct.run(T0)
    for i in (1, 2, 3):
        acct.server.expunge(i)
    acct.run(T0 + DAY)
    ev = acct.run(T0 + 2 * DAY)
    assert ev is not None and ev.drop == ()
    assert ev.guard is not None and "max_drop_count" in ev.guard
    acct.server.mail["e3"] = (message(3), T0, {"I"})
    ev = acct.run(T0 + 3 * DAY)
    acct.run(T0 + 4 * DAY)
    assert {acct.fate(msg(i)) for i in (1, 2)} == {"dropped"}
    assert acct.fate(msg(3)) is None


def test_two_accounts_hold_one_message(tmp_path: Path) -> None:
    """One account drops it, the other promotes it; the archive keeps it,
    and the later promotion writes nothing."""
    store = tmp_path / "store"
    a, b = Account(store, "a"), Account(store, "b")
    for acct in (a, b):
        for i in range(1, 4):
            acct.server.deliver(i, T0)
        acct.run(T0)
    assert a.held() == b.held() == {msg(1), msg(2), msg(3)}
    a.server.expunge(1)
    a.run(T0 + DAY)
    a.run(T0 + 2 * DAY)
    assert a.fate(msg(1)) == "dropped"
    b.server.move(1, "W")
    b.run(T0 + 2 * DAY)
    assert b.fate(msg(1)) == "promoted"
    assert records(store) == {msg(1): 1}
    # The dropped copy is only a duplicate now.
    ev = a.run(T0 + 3 * DAY)
    assert ev is not None and set(ev.archived) == {msg(1)}
    assert a.fate(msg(1)) == "promoted"
    assert a.state().fates[msg(1)].segment == b.state().fates[msg(1)].segment
    assert msg(1) not in a.held()
    # Both promote another message at max_age: one record.
    for acct in (a, b):
        acct.run(T0 + 8 * DAY)
        assert acct.fate(msg(2)) == "promoted"
    assert records(store) == {msg(1): 1, msg(2): 1, msg(3): 1}


def test_promotion_interrupted_after_the_segment_write(acct: Account) -> None:
    acct.server.deliver(1, T0)
    acct.run(T0)
    acct.server.move(1, "W")
    acct.crash = lambda events: any(isinstance(e, Promote) for e in events)
    with pytest.raises(OSError, match="crash"):
        acct.run(T0 + DAY)
    acct.crash = None
    assert records(acct.store) == {msg(1): 1}
    assert acct.held() == {msg(1)} and acct.fate(msg(1)) is None
    ev = acct.run(T0 + 2 * DAY)
    assert ev is not None and ev.promote == () and set(ev.archived) == {msg(1)}
    assert records(acct.store) == {msg(1): 1}
    assert acct.held() == set()
    assert acct.state().fates[msg(1)].segment == acct_location(acct, msg(1))[0]


def test_purge_interrupted_before_the_unlink(acct: Account) -> None:
    for i in range(1, 4):
        acct.server.deliver(i, T0)
    acct.run(T0)
    acct.server.expunge(1)
    acct.run(T0 + DAY)
    acct.run(T0 + 2 * DAY)
    # A crash between the journal batch and the unlink.
    real = incoming.apply

    def no_unlink(ing: Ingester, writer: JournalWriter, ev: Evaluation) -> Any:
        unlink = Path.unlink
        Path.unlink = lambda self, missing_ok=False: None  # type: ignore[method-assign,assignment]
        try:
            return real(ing, writer, ev)
        finally:
            Path.unlink = unlink  # type: ignore[method-assign]

    incoming.apply = no_unlink  # type: ignore[assignment]
    try:
        acct.run(T0 + 40 * DAY)
    finally:
        incoming.apply = real  # type: ignore[assignment]
    # Messages 2 and 3 were promoted by the same run.
    assert acct.fate(msg(1)) == "purged" and acct.fate(msg(2)) == "promoted"
    assert acct.held() == {msg(1), msg(2), msg(3)}
    seq = acct.state().seq
    ev = acct.run(T0 + 41 * DAY)
    assert ev is not None and set(ev.unlink) == {msg(1), msg(2), msg(3)}
    assert acct.held() == set()
    assert records(acct.store) == {msg(2): 1, msg(3): 1}
    assert all(
        not isinstance(e, Purge | Drop | Promote)
        for e in journal.read_events(acct.store, acct.name, after=seq)
    )


def damage_record(acct: Account, m: str) -> None:
    """Flip a byte in the body of ``m``'s record, after which a partial run
    stores message 2 (a damaged last record would be truncated as torn)."""
    acct.server.deliver(2, T0, "W")
    acct.run(T0 + DAY, complete=False)
    seg, offset = acct_location(acct, m)
    path = acct.store / seg
    offsets = [o for o, _ in segment.scan(path)]
    end = offsets[offsets.index(offset) + 1]
    os.chmod(path, 0o644)
    data = bytearray(path.read_bytes())
    data[end - 3] ^= 0xFF
    path.write_bytes(data)


def test_archived_file_is_kept_if_its_record_does_not_rebuild(
    acct: Account,
) -> None:
    acct.server.deliver(1, T0)
    acct.run(T0)
    acct.server.move(1, "W")
    acct.crash = lambda events: any(isinstance(e, Promote) for e in events)
    with pytest.raises(OSError, match="crash"):
        acct.run(T0 + DAY)
    acct.crash = None
    damage_record(acct, msg(1))
    with (
        StoreLock(acct.store) as lock,
        Ingester(acct.store, lock, clock=lambda: T0) as ing,
    ):
        writer = JournalWriter(acct.store, acct.name, clock=lambda: T0)
        ev = evaluate(
            acct.state(),
            incoming.held(acct.store, acct.name),
            ing.catalog.messages,
            SETTINGS,
            now=T0 + 2 * DAY,
        )
        assert set(ev.archived) == {msg(1)}
        applied = incoming.apply(ing, writer, ev)
        # The same through a promotion that finds the message stored.
        again = incoming.apply(ing, writer, Evaluation(promote=(msg(1),)))
    for report in (applied, again):
        [problem] = report.problems
        assert "kept, its record" in problem and report.promoted == 0
    assert acct.held() == {msg(1)} and acct.fate(msg(1)) is None


def test_promoted_file_is_kept_if_its_record_does_not_rebuild(
    acct: Account,
) -> None:
    acct.server.deliver(1, T0)
    acct.run(T0)
    acct.server.move(1, "W")
    real = Path.unlink
    Path.unlink = lambda self, missing_ok=False: None  # type: ignore[method-assign,assignment]
    try:
        acct.run(T0 + DAY)
    finally:
        Path.unlink = real  # type: ignore[method-assign]
    assert acct.fate(msg(1)) == "promoted" and acct.held() == {msg(1)}
    damage_record(acct, msg(1))
    ev = acct.run(T0 + 2 * DAY)
    assert ev is not None and ev.unlink == (msg(1),)
    assert acct.held() == {msg(1)}


def test_partial_run_writes_no_fates(acct: Account) -> None:
    for i in range(1, 4):
        acct.server.deliver(i, T0)
    acct.run(T0)
    acct.server.expunge(1)
    acct.run(T0 + DAY)
    assert acct.run(T0 + 2 * DAY, complete=False) is None
    assert acct.fate(msg(1)) is None


def test_held_file_that_does_not_hash_is_left(acct: Account) -> None:
    acct.server.deliver(1, T0)
    acct.run(T0)
    path = incoming.file_path(acct.store, acct.name, msg(1))
    os.chmod(path, 0o644)
    path.write_bytes(b"damaged")
    with (
        StoreLock(acct.store) as lock,
        Ingester(acct.store, lock, clock=lambda: T0) as ing,
    ):
        writer = JournalWriter(acct.store, acct.name, clock=lambda: T0)
        applied = incoming.apply(ing, writer, Evaluation(promote=(msg(1),)))
    assert applied.promoted == 0 and len(applied.problems) == 1
    assert path.exists() and records(acct.store) == {}
    assert acct.fate(msg(1)) is None


def test_evaluate_ignores_excluded_mailboxes() -> None:
    state = State()
    for event in (
        Mbox(mbox="I", name="Inbox", role="inbox"),
        Mbox(mbox="X", name="Lists"),
        Add(msg=msg(1), mbox="I", rid="e1"),
        Add(msg=msg(2), mbox="X", rid="e2"),
    ):
        state.apply(event)
    held = {msg(1): T0, msg(2): T0}
    ev = evaluate(state, held, {}, SETTINGS, now=T0 + DAY)
    assert ev.promote == (msg(2),)
    ev = evaluate(state, held, {}, SETTINGS, now=T0 + DAY, exclude=["Lists"])
    assert ev.promote == () and ev.absent == {msg(2)}


def test_evaluate_grace_runs_from_the_drop() -> None:
    state = State()
    state.fates[msg(1)] = Fate("dropped", T0)
    held = {msg(1): T0 - 100 * DAY}
    assert evaluate(state, held, {}, SETTINGS, now=T0 + 29 * DAY) == Evaluation()
    ev = evaluate(state, held, {}, SETTINGS, now=T0 + 30 * DAY)
    assert ev.purge == (msg(1),)


# --- verify ------------------------------------------------------------


def test_verify_checks_held_files(acct: Account) -> None:
    for i in range(1, 4):
        acct.server.deliver(i, T0)
    acct.run(T0)
    report = verify(acct.store)
    assert report.problems == [] and report.held == 3
    assert report.held_bytes == sum(len(message(i)) for i in range(1, 4))
    directory = incoming.incoming_dir(acct.store, acct.name)
    damaged = incoming.file_path(acct.store, acct.name, msg(1))
    os.chmod(damaged, 0o644)
    damaged.write_bytes(b"damaged")
    temp = directory / f".{msg(2).removeprefix('sha256:')}.{'0' * 16}.tmp"
    temp.write_bytes(b"partial")
    (directory / "notes.txt").write_bytes(b"stray")
    report = verify(acct.store)
    rel = damaged.relative_to(acct.store).as_posix()
    assert report.temp_files == [temp.relative_to(acct.store).as_posix()]
    # The damaged file no longer holds message 1, which the journal names.
    assert sorted(report.problems) == sorted(
        [
            f"{rel}: does not hash to its name",
            "incoming/acct/notes.txt: not a held message",
            f"journal of acct: {msg(1)} is in a mailbox but not stored",
        ]
    )


def test_verify_lists_held_files_also_in_a_segment(acct: Account) -> None:
    acct.server.deliver(1, T0)
    acct.run(T0)
    acct.server.move(1, "W")
    acct.crash = lambda events: any(isinstance(e, Promote) for e in events)
    with pytest.raises(OSError, match="crash"):
        acct.run(T0 + DAY)
    report = verify(acct.store)
    path = incoming.file_path(acct.store, acct.name, msg(1))
    assert report.problems == []
    assert report.held_in_segment == [path.relative_to(acct.store).as_posix()]


def test_verify_checks_promotions(acct: Account) -> None:
    for i in (1, 2):
        acct.server.deliver(i, T0, "W")
    acct.run(T0)
    seg, offset = acct_location(acct, msg(2))
    with StoreLock(acct.store):
        JournalWriter(acct.store, acct.name).append(
            [Promote(msg=msg(1), segment=seg, offset=offset)]
        )
    assert verify(acct.store).problems == [
        f"journal of acct: {msg(1)} promoted to {seg} at {offset},"
        " which is not a record of it"
    ]


def test_verify_accepts_purged_messages(acct: Account) -> None:
    for i in range(1, 4):
        acct.server.deliver(i, T0)
    acct.run(T0)
    acct.server.expunge(1)
    for days in (1, 2, 40):
        acct.run(T0 + days * DAY)
    assert acct.fate(msg(1)) == "purged"
    report = verify(acct.store)
    assert report.problems == [] and (report.records, report.held) == (2, 0)


def test_verify_accepts_purged_messages_in_an_excluded_mailbox(
    tmp_path: Path,
) -> None:
    """Evaluation ignores excluded mailboxes, so a message the journal still
    has in one can be purged."""
    store = tmp_path / "store"
    events: list[Event] = [
        Mbox(mbox="X", name="Lists"),
        Add(msg=msg(1), mbox="X", rid="e1"),
        Drop(msg=msg(1)),
    ]
    with StoreLock(store):
        JournalWriter(store, "acct").append(events)
    assert verify(store).problems == [
        f"journal of acct: {msg(1)} is in a mailbox but not stored"
    ]
    with StoreLock(store):
        JournalWriter(store, "acct").append([Purge(msg=msg(1))])
    assert verify(store).problems == []


# --- property test -------------------------------------------------------

N = 4
change = st.one_of(
    st.tuples(st.just("deliver"), st.integers(0, N - 1)),
    st.tuples(
        st.just("move"),
        st.integers(0, N - 1),
        st.sets(st.sampled_from(sorted(BOXES)), max_size=2),
    ),
    st.tuples(st.just("expunge"), st.integers(0, N - 1)),
)
#: Each step: changes on the server, hours waited, then a run (complete
#: or not).
steps = st.lists(
    st.tuples(
        st.lists(change, max_size=3),
        st.sampled_from([0, 1, 12, 24, 48, 24 * 7, 24 * 31]),
        st.booleans(),
    ),
    max_size=15,
)


def _tmp() -> Iterator[Path]:
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@settings(max_examples=300, deadline=None)
@given(steps, st.sampled_from(["drop", "keep"]))
def test_no_mail_is_lost(
    script: list[tuple[list[tuple[Any, ...]], int, bool]], on_delete: str
) -> None:
    """Whatever the server does: every message downloaded is in a segment
    or held, unless it was purged; a message is dropped only when absent at
    the end of two consecutive complete runs, and never with "keep"; and
    a message in a segment is not held, nor one that was filed or (unless it
    may yet be dropped) reached max_age; and purges wait for the grace
    period."""
    for root in _tmp():
        acct = Account(
            root / "store",
            settings=replace(SETTINGS, on_delete=on_delete, max_drop_fraction=1.0),  # type: ignore[arg-type]
        )
        now = T0
        seen: set[str] = set()
        before: set[str] | None = None
        for changes, hours, complete in script:
            for op, i, *args in changes:
                match op:
                    case "deliver":
                        acct.server.deliver(i, now)
                    case "move" if f"e{i}" in acct.server.mail:
                        acct.server.move(i, *args[0])
                    case "expunge" if f"e{i}" in acct.server.mail:
                        acct.server.expunge(i)
            now += timedelta(hours=hours)
            if not complete:
                acct.run(now, complete=False)
                continue
            fates = acct.state().fates
            seq = acct.state().seq
            seen |= {message_id(d) for d, _, _ in acct.server.mail.values()}
            present = acct.server.present()
            filed = {
                message_id(d)
                for d, _, boxes in acct.server.mail.values()
                if boxes - {"I", "T", "J"}
            }
            acct.run(now)
            assert verify(acct.store).problems == []
            state = acct.state()
            stored = set(records(acct.store))
            held = acct.held()
            for m in seen:
                fate = state.fates.get(m)
                purged = fate is not None and fate.status == "purged"
                assert m in stored or m in held or purged
                assert not (m in stored and m in held)
                assert m not in filed or m in stored
            for m, received in incoming.held(acct.store, acct.name).items():
                if m not in state.fates and now - received >= SETTINGS.max_age:
                    assert on_delete == "drop" and m not in present
            for e in journal.read_events(acct.store, acct.name, after=seq):
                if isinstance(e, Drop):
                    assert on_delete == "drop"
                    assert e.msg not in present
                    assert before is not None and e.msg not in before
                if isinstance(e, Purge):
                    dropped = fates[e.msg]
                    assert dropped.status == "dropped"
                    assert e.ts - dropped.ts >= SETTINGS.grace
            before = present
