"""Incoming: recent INBOX mail, held as files until its fate is clear
(DESIGN.md, "Incoming: recent INBOX mail").

A held message is ``incoming/<account>/<sha256 hex>.eml``, raw, read-only,
with its modification time set to when the account's server received it
(``max_age`` is measured from that). Files are written to a temp file,
fsynced and renamed into place, like attachment files.

``evaluate`` decides, from an account's replayed journal and its files,
what becomes of each held message; it is pure, so the rules are tested
without a store. ``apply`` carries the decisions out in the write order
of DESIGN.md: segment records, then the journal batch, then unlinking.
The caller holds the store lock, and evaluates an account only after a
complete sync run of it.

Drops need a message to be absent at the end of two consecutive complete
runs, so an evaluation returns the messages absent now (``absent``) and
takes those of the previous run; the caller keeps them (in
``sync.json``). Losing them only delays drops by a run.
"""

import logging
import os
import re
import secrets
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from geniza.config import IncomingSettings
from geniza.store.journal import (
    MSG_PREFIX,
    Drop,
    Event,
    JournalWriter,
    Mailbox,
    Promote,
    Purge,
    State,
    Undrop,
)

if TYPE_CHECKING:
    from geniza.store.ingest import Ingester

log = logging.getLogger(__name__)

INCOMING_DIR = "incoming"
#: Mailbox roles whose messages are not filed (DESIGN.md, "Trash and Junk").
UNFILED_ROLES = frozenset({"trash", "junk"})

_FILE_NAME = re.compile(r"([0-9a-f]{64})\.eml")
_TEMP_NAME = re.compile(r"\.[0-9a-f]{64}\.[0-9a-f]{16}\.tmp")


def incoming_dir(store: Path, account: str) -> Path:
    return store / INCOMING_DIR / account


def file_path(store: Path, account: str, msg: str) -> Path:
    return incoming_dir(store, account) / f"{msg.removeprefix(MSG_PREFIX)}.eml"


def files(store: Path, account: str) -> dict[str, Path]:
    """The messages in an account's ``incoming/``, by id."""
    root = incoming_dir(store, account)
    if not root.is_dir():
        return {}
    return {
        MSG_PREFIX + m[1]: path
        for path in sorted(root.iterdir())
        if (m := _FILE_NAME.fullmatch(path.name))
    }


def is_temp(name: str) -> bool:
    """Whether ``name`` is that of a temp file of ``write``."""
    return _TEMP_NAME.fullmatch(name) is not None


def received_of(st: os.stat_result) -> datetime:
    """When a held message was received: its file's modification time."""
    return datetime.fromtimestamp(int(st.st_mtime), UTC)


def held(store: Path, account: str) -> dict[str, datetime]:
    """The messages in an account's ``incoming/`` and when each was received."""
    return {msg: received_of(p.stat()) for msg, p in files(store, account).items()}


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def open_dir(store: Path, account: str) -> Path:
    """Create an account's ``incoming/`` or remove the temp files a crash
    left in it."""
    directory = incoming_dir(store, account)
    if not directory.exists():
        directory.mkdir(parents=True)
        _fsync_dir(directory.parent)
        _fsync_dir(directory.parent.parent)
    for p in sorted(directory.iterdir()):
        if is_temp(p.name):
            log.warning("%s: removing a temp file left by a crash", p)
            p.unlink()
    return directory


def write(store: Path, account: str, msg: str, data: bytes, received: datetime) -> Path:
    """Write a held message (its directory made by ``open_dir``), dated
    ``received``. ``msg`` is the id of ``data``, trusted."""
    directory = incoming_dir(store, account)
    hexid = msg.removeprefix(MSG_PREFIX)
    tmp = directory / f".{hexid}.{secrets.token_hex(8)}.tmp"
    target = directory / f"{hexid}.eml"
    when = int(received.timestamp())
    try:
        with open(tmp, "xb") as f:
            f.write(data)
            f.flush()
            os.utime(f.fileno(), (when, when))
            os.fchmod(f.fileno(), 0o444)
            os.fsync(f.fileno())
        os.rename(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _fsync_dir(directory)
    return target


# --- mailboxes -----------------------------------------------------------


def matches(box: Mailbox, entries: Iterable[str]) -> bool:
    """Whether a mailbox is one of ``entries``: by role, in any case
    (``"inbox"`` or ``"INBOX"`` for a JMAP inbox), or by name (IMAP)."""
    role = box.role.casefold() if box.role else None
    return any(e == box.name or e.casefold() == role for e in entries)


def placed_in_incoming(
    boxes: Iterable[Mailbox],
    received: datetime,
    settings: IncomingSettings,
    now: datetime,
) -> bool:
    """Whether a newly downloaded message, in ``boxes``, goes to
    ``incoming/`` (DESIGN.md, "Placement at ingest")."""
    return now - received < settings.max_age and any(
        matches(b, settings.mailboxes) for b in boxes
    )


# --- evaluation ----------------------------------------------------------


@dataclass(frozen=True)
class Evaluation:
    """What to do with an account's ``incoming/``. Every message is in at
    most one of ``promote``, ``archived``, ``drop``, ``purge`` and
    ``unlink``; an undropped message may also be promoted."""

    #: To write into a segment, then journal ``promote``.
    promote: tuple[str, ...] = ()
    #: Already in a segment (msg → segment, offset): journal ``promote``
    #: (unless it was) and unlink.
    archived: Mapping[str, tuple[str, int]] = field(default_factory=dict)
    drop: tuple[str, ...] = ()
    undrop: tuple[str, ...] = ()
    purge: tuple[str, ...] = ()
    #: Files whose fate is journaled already (a crash came before the
    #: unlink): unlink only.
    unlink: tuple[str, ...] = ()
    #: Held messages in none of the synced mailboxes now (for the next run).
    absent: frozenset[str] = frozenset()
    #: Why drops were withheld (the mass-drop guard), if they were.
    guard: str | None = None
    #: Messages the guard kept from being dropped.
    withheld: tuple[str, ...] = ()


def evaluate(
    state: State,
    held: Mapping[str, datetime],
    archived: Mapping[str, tuple[str, int]],
    settings: IncomingSettings,
    *,
    now: datetime,
    absent_before: Collection[str] = frozenset(),
    exclude: Collection[str] = (),
    allow_mass_drop: bool = False,
) -> Evaluation:
    """Evaluate an account's ``incoming/`` after a complete run
    (DESIGN.md, "Evaluation").

    ``state`` is the account's replayed journal, ``held`` its files (msg →
    received), ``archived`` the messages in a segment (msg → location,
    as ``Ingester.catalog`` has them), ``absent_before`` what the previous
    complete run's evaluation returned as ``absent``, and ``exclude`` the
    mailboxes not synced (``[accounts.<name>.sync] exclude``).
    """
    promote: list[str] = []
    found: dict[str, tuple[str, int]] = {}
    candidates: list[str] = []
    undrop: list[str] = []
    purge: list[str] = []
    unlink: list[str] = []
    absent: set[str] = set()
    live = 0
    for msg, received in sorted(held.items()):
        fate = state.fates.get(msg)
        if (where := archived.get(msg)) is not None:
            if fate is not None and fate.status == "promoted":
                unlink.append(msg)
            else:
                found[msg] = where
            continue
        boxes = [
            state.mailboxes.get(mbox) or Mailbox(mbox)
            for mbox in state.places.get(msg, {})
        ]
        boxes = [b for b in boxes if not matches(b, exclude)]
        if fate is not None and fate.status in ("dropped", "purged"):
            if not boxes:
                if fate.status == "purged":
                    unlink.append(msg)
                elif now - fate.ts >= settings.grace:
                    purge.append(msg)
                continue
            undrop.append(msg)
        live += 1
        filed = any(
            not matches(b, settings.mailboxes) and (b.role or "") not in UNFILED_ROLES
            for b in boxes
        )
        old = now - received >= settings.max_age
        if filed or (old and (boxes or settings.on_delete == "keep")):
            promote.append(msg)
        elif not boxes and settings.on_delete == "drop":
            absent.add(msg)
            if msg in absent_before:
                candidates.append(msg)
    guard = None
    if candidates and not allow_mass_drop:
        if len(candidates) > settings.max_drop_count:
            guard = (
                f"{len(candidates)} messages to drop, more than max_drop_count"
                f" ({settings.max_drop_count})"
            )
        elif len(candidates) > settings.max_drop_fraction * live:
            guard = (
                f"{len(candidates)} of {live} held messages to drop, more than"
                f" max_drop_fraction ({settings.max_drop_fraction:g})"
            )
    drop = () if guard else tuple(candidates)
    return Evaluation(
        promote=tuple(promote),
        archived=found,
        drop=drop,
        undrop=tuple(undrop),
        purge=tuple(purge),
        unlink=tuple(unlink),
        absent=frozenset(absent - set(drop)),
        guard=guard,
        withheld=tuple(candidates) if guard else (),
    )


@dataclass
class Applied:
    promoted: int = 0
    #: Of ``promoted``, those already in a segment (nothing written).
    archived: int = 0
    dropped: int = 0
    undropped: int = 0
    purged: int = 0
    #: Held files that do not hash to their name, left alone.
    problems: list[str] = field(default_factory=list)


def apply(
    ingester: Ingester,
    journal: JournalWriter,
    evaluation: Evaluation,
) -> Applied:
    """Carry out an evaluation of ``journal.account``'s ``incoming/``:
    promotions into segments, then one journal batch (undrops, promotions,
    drops, purges), then unlinking the files promoted or purged.

    A crash in between leaves files that the next evaluation finds in a
    segment (promoted without writing) or with their fate journaled
    (unlinked).
    """
    from geniza.store.ingest import message_id

    store, account = ingester.store, journal.account
    report = Applied()
    events: list[Event] = [Undrop(msg=m) for m in evaluation.undrop]
    gone: list[str] = list(evaluation.unlink)
    for msg in evaluation.promote:
        path = file_path(store, account, msg)
        data = path.read_bytes()
        if message_id(data) != msg:
            report.problems.append(f"{path}: does not hash to its name")
            continue
        added = ingester.add(data, received_of(path.stat()))
        events.append(Promote(msg=msg, segment=added.segment, offset=added.offset))
        gone.append(msg)
        report.promoted += 1
        report.archived += not added.new
    for msg, (segment, offset) in evaluation.archived.items():
        events.append(Promote(msg=msg, segment=segment, offset=offset))
        gone.append(msg)
        report.promoted += 1
        report.archived += 1
    events += [Drop(msg=m) for m in evaluation.drop]
    events += [Purge(msg=m) for m in evaluation.purge]
    gone += evaluation.purge
    journal.append(events)
    report.undropped = len(evaluation.undrop)
    report.dropped = len(evaluation.drop)
    report.purged = len(evaluation.purge)
    for msg in gone:
        file_path(store, account, msg).unlink(missing_ok=True)
    if gone:
        _fsync_dir(incoming_dir(store, account))
    return report
