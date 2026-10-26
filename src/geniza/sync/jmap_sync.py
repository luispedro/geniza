"""Syncing one JMAP account into the store (DESIGN.md, "JMAP").

A run journals the account's mailboxes (``mbox`` events, ``gone`` for
destroyed ones), then its emails: everything changed since the last
complete run (``Email/changes``), or everything (``Email/query``) on a
first run, after ``cannotCalculateChanges`` or when the cursors are lost.
Each email's ``mailboxIds`` and ``keywords`` are compared with the
replayed journal, giving ``add``/``remove``/``flags`` events; its body is
downloaded only if the journal does not map its id (``eid``) to a message
the store still holds. Messages are stored and their events journaled in
batches, so an interrupted first sync keeps what it did.

Excluded mailboxes (``[accounts.<name>.sync] exclude``) get ``mbox``
events like the others, but nothing is added to them and no message is
downloaded for them; a message leaving one is still journaled as removed.

The caller holds the store lock, evaluates ``incoming/`` after a complete
run and writes the returned cursors to ``sync.json``.
"""

import logging
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from itertools import batched
from typing import Any

from geniza.config import IncomingSettings
from geniza.store import incoming
from geniza.store.ingest import Held, Ingester, IngestError
from geniza.store.journal import (
    Add,
    Event,
    Flags,
    Gone,
    JournalWriter,
    Mbox,
    Remove,
    State,
)
from geniza.store.segment import parse_time
from geniza.sync.jmap import CannotCalculateChanges, Client, JmapError, Json

log = logging.getLogger(__name__)

#: Mailbox properties journaled in named fields of ``mbox`` events.
MAILBOX_FIELDS = frozenset(
    {"id", "name", "parentId", "role", "sortOrder", "isSubscribed"}
)
#: Mailbox properties that change without the mailbox changing; left out
#: of ``props``, or every sync would journal them.
MAILBOX_COUNTERS = frozenset(
    {"totalEmails", "unreadEmails", "totalThreads", "unreadThreads"}
)
#: Email properties fetched. ``size`` is checked against the download.
EMAIL_PROPERTIES = (
    "blobId", "threadId", "mailboxIds", "keywords", "receivedAt", "size",
)  # fmt: skip
#: Emails fetched, stored and journaled per batch.
BATCH = 256


@dataclass
class Progress:
    """What a run did, for the report."""

    #: Whether every email was listed (rather than changes since the last run).
    full: bool = False
    mailboxes: int = 0
    emails: int = 0
    downloaded: int = 0
    bytes: int = 0
    #: New segment records and new files in ``incoming/``.
    stored: int = 0
    held: int = 0
    events: int = 0
    #: Emails that could not be stored; any makes the run incomplete.
    problems: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Cursors:
    """The states a complete run reached, for ``sync.json``."""

    email_state: str
    mailbox_state: str

    def to_json(self) -> dict[str, str]:
        return {"email_state": self.email_state, "mailbox_state": self.mailbox_state}


def _check[T](value: Any, kind: type[T] | tuple[type, ...], what: str) -> T:
    if not isinstance(value, kind):
        raise JmapError(f"bad {what} from the server")
    return value  # type: ignore[return-value]


def _optional[T](value: Any, kind: type[T], what: str) -> T | None:
    return None if value is None else _check(value, kind, what)


def _id_set(obj: Json, key: str) -> set[str]:
    """A JMAP ``Id[Boolean]`` map (``mailboxIds``, ``keywords``) as a set."""
    value = _check(obj.get(key), dict, key)
    if not all(isinstance(k, str) and v is True for k, v in value.items()):
        raise JmapError(f"bad {key} from the server")
    return set(value)


class JmapSync:
    """One run of one account. ``state`` is its replayed journal, kept up
    to date with the events the run writes."""

    def __init__(
        self,
        client: Client,
        ingester: Ingester,
        journal: JournalWriter,
        state: State,
        *,
        settings: IncomingSettings,
        exclude: Collection[str],
        now: datetime,
    ) -> None:
        self.client = client
        self.ingester = ingester
        self.journal = journal
        self.account = journal.account
        self.state = state
        self.settings = settings
        self.exclude = exclude
        self.now = now
        self.progress = Progress()

    def run(self, email_state: str | None, mailbox_state: str | None) -> Cursors:
        """Sync from the cursors of the last complete run (None: list
        everything). Raises ``JmapError``; what was journaled stays."""
        new_mailbox_state, gone = self._mailboxes(mailbox_state)
        new_email_state, removed = self._emails(email_state)
        self._finish(removed, gone)
        return Cursors(new_email_state, new_mailbox_state)

    def _write(self, events: Iterable[Event]) -> None:
        for event in self.journal.append(events):
            self.state.apply(event)
            self.progress.events += 1

    # --- mailboxes --------------------------------------------------------

    def _mailboxes(self, since: str | None) -> tuple[str, list[str]]:
        """Journal new and changed mailboxes. Returns the new state and
        the destroyed mailboxes, which are journaled ``gone`` last."""
        if since is not None:
            try:
                changes = self.client.changes("Mailbox", since)
            except CannotCalculateChanges:
                log.info("%s: mailbox changes unavailable; listing all", self.account)
            else:
                ids = changes.created + changes.updated
                objects = self.client.get("Mailbox", ids).objects if ids else []
                self._write(self._mbox_events(objects))
                gone = [m for m in changes.destroyed if m in self.state.mailboxes]
                return changes.new_state, gone
        got = self.client.get("Mailbox")
        self._write(self._mbox_events(got.objects))
        listed = {o["id"] for o in got.objects}
        return got.state, [m for m in self.state.mailboxes if m not in listed]

    def _mbox_events(self, objects: Iterable[Json]) -> list[Event]:
        events: list[Event] = []
        for obj in objects:
            event = self._mbox(obj)
            box = self.state.mailboxes.get(event.mbox)
            if box is None or (
                box.name, box.role, box.parent, box.sort, box.subscribed, box.props
            ) != (
                event.name, event.role, event.parent, event.sort, event.subscribed,
                event.props,
            ):  # fmt: skip
                events.append(event)
        self.progress.mailboxes += len(events)
        return events

    @staticmethod
    def _mbox(obj: Json) -> Mbox:
        props = {
            k: v
            for k, v in obj.items()
            if k not in MAILBOX_FIELDS and k not in MAILBOX_COUNTERS
        }
        sort = obj.get("sortOrder")
        if isinstance(sort, bool):
            raise JmapError("bad sortOrder from the server")
        return Mbox(
            mbox=_check(obj.get("id"), str, "mailbox id"),
            name=_check(obj.get("name"), str, "mailbox name"),
            role=_optional(obj.get("role"), str, "mailbox role"),
            parent=_optional(obj.get("parentId"), str, "mailbox parentId"),
            sort=_optional(sort, int, "mailbox sortOrder"),
            subscribed=_optional(obj.get("isSubscribed"), bool, "isSubscribed"),
            props=props or None,
        )

    # --- emails -----------------------------------------------------------

    def _emails(self, since: str | None) -> tuple[str, set[str]]:
        """Journal the emails changed since ``since`` (all of them if
        None). Returns the new state and the email ids to remove from
        every mailbox."""
        destroyed: list[str] = []
        ids: Sequence[str] | None = None
        if since is not None:
            try:
                changes = self.client.changes("Email", since)
            except CannotCalculateChanges:
                log.info("%s: email changes unavailable; listing all", self.account)
            else:
                state, ids = changes.new_state, changes.created + changes.updated
                destroyed = changes.destroyed
        if ids is None:
            self.progress.full = True
            state, ids = self.client.query_emails()
        seen: set[str] = set()
        for chunk in batched(ids, BATCH, strict=False):
            objects = self.client.get("Email", chunk, EMAIL_PROPERTIES).objects
            seen.update(_check(o.get("id"), str, "email id") for o in objects)
            self._batch(objects)
        if self.progress.full:
            # Whatever the journal has that the listing does not is gone.
            removed = {rid for rids in self.state.contents.values() for rid in rids}
        else:
            # Listed as changed but not found: destroyed meanwhile.
            removed = set(destroyed) | set(ids)
        return state, removed - seen

    def _batch(self, objects: list[Json]) -> None:
        # A mailbox created after the mailboxes were synced.
        unknown = {
            m
            for o in objects
            for m in _id_set(o, "mailboxIds")
            if m not in self.state.mailboxes
        }
        if unknown:
            got = self.client.get("Mailbox", sorted(unknown))
            self._write(self._mbox_events(got.objects))
        events: list[Event] = []
        for obj in objects:
            events += self._email(obj)
        self._write(events)

    def _email(self, obj: Json) -> list[Event]:
        state = self.state
        rid = obj["id"]
        self.progress.emails += 1
        # Mailboxes neither known nor found (destroyed meanwhile) are
        # left for the next run.
        boxes = {m for m in _id_set(obj, "mailboxIds") if m in state.mailboxes}
        synced = sorted(
            m for m in boxes if not incoming.matches(state.mailboxes[m], self.exclude)
        )
        keywords = frozenset(_id_set(obj, "keywords"))
        journaled = {m: rids[rid] for m, rids in state.contents.items() if rid in rids}
        msg = state.eids.get(rid)
        events: list[Event] = [
            Remove(msg=journaled[m], mbox=m, rid=rid)
            for m in sorted(journaled.keys() - boxes)
        ]
        if not synced:
            return events
        if msg is None or not self._stored(msg):
            msg = self._download(obj, synced)
            if msg is None:
                return []
        received = self._received(obj)
        thread = _check(obj.get("threadId"), str, "threadId")
        for m in synced:
            if m not in journaled:
                events.append(
                    Add(
                        msg=msg,
                        mbox=m,
                        rid=rid,
                        eid=rid,
                        received=received,
                        thread=thread,
                    )
                )
                events.append(Flags(msg=msg, mbox=m, set=keywords))
            elif state.flags_of(journaled[m], m) != keywords:
                events.append(Flags(msg=journaled[m], mbox=m, set=keywords))
        return events

    @staticmethod
    def _received(obj: Json) -> datetime:
        try:
            return parse_time(_check(obj.get("receivedAt"), str, "receivedAt"))
        except ValueError:
            raise JmapError("bad receivedAt from the server") from None

    def _stored(self, msg: str) -> bool:
        """Whether the store still has the message for this account: in a
        segment, or in its own ``incoming/`` (a purged message is not)."""
        return (
            msg in self.ingester.catalog.messages
            or incoming.file_path(self.ingester.store, self.account, msg).exists()
        )

    def _download(self, obj: Json, synced: list[str]) -> str | None:
        """Download and store an email; its id, or None (a problem)."""
        rid = obj["id"]
        blob = _check(obj.get("blobId"), str, "blobId")
        size = obj.get("size")
        if type(size) is not int:
            raise JmapError("bad size from the server")
        received = self._received(obj)
        try:
            data = self.client.download(blob)
        except JmapError as e:
            self.progress.problems.append(f"email {rid}: {e}")
            return None
        self.progress.downloaded += 1
        self.progress.bytes += len(data)
        if len(data) != size:
            self.progress.problems.append(
                f"email {rid}: downloaded {len(data)} bytes, the server said {size};"
                " not stored"
            )
            return None
        where = [self.state.mailboxes[m] for m in synced]
        try:
            if incoming.placed_in_incoming(where, received, self.settings, self.now):
                stored = self.ingester.hold(data, received, self.account)
                self.progress.held += isinstance(stored, Held) and stored.new
            else:
                stored = self.ingester.add(data, received)
                self.progress.stored += stored.new
        except IngestError as e:
            self.progress.problems.append(f"email {rid}: {e}")
            return None
        if self.progress.downloaded % 1000 == 0:
            log.info("%s: %d emails downloaded", self.account, self.progress.downloaded)
        return stored.id

    # --- the end ----------------------------------------------------------

    def _finish(self, removed: set[str], gone: list[str]) -> None:
        """Remove destroyed emails from every mailbox, then the messages
        still in destroyed mailboxes, then journal the mailboxes ``gone``."""
        events: list[Event] = []
        for mbox, rids in sorted(self.state.contents.items()):
            for rid, msg in sorted(rids.items()):
                if rid in removed or mbox in gone:
                    events.append(Remove(msg=msg, mbox=mbox, rid=rid))
        events += [Gone(mbox=m) for m in gone]
        self.progress.mailboxes += len(gone)
        self._write(events)
