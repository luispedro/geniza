"""The journal: per-account membership and flag events (DESIGN.md, "State:
the journal").

Events are JSON lines in ``state/<account>/journal-YYYY-MM.jsonl``, by the
UTC month they were written in. ``seq`` increases by one per event across
the files of an account. A batch of events is appended and fsynced at once.

Every event assigns a value (a membership, a flag set, a fate) without
looking at what was there before, so replaying events that were already
applied changes nothing.

Only the end of the last file can be torn, and only by a crash during the
last batch. ``recover`` removes it; ``read_events`` skips it with a warning.
Damage anywhere else raises ``JournalError``.
"""

import json
import logging
import os
import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field, fields, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, Literal

from geniza.store.segment import format_time, parse_time

log = logging.getLogger(__name__)

MSG_PREFIX = "sha256:"
#: ``ts`` of an event that has not been written yet.
UNSET = datetime(1970, 1, 1, tzinfo=UTC)

_FILE_NAME = re.compile(r"journal-(\d{4})-(\d{2})\.jsonl")


class JournalError(Exception):
    """A journal line that cannot be read."""

    def __init__(self, path: Path | str, line: int, reason: str) -> None:
        super().__init__(f"{path}: line {line}: {reason}")
        self.path = Path(path)
        self.line = line
        self.reason = reason


@dataclass(frozen=True, kw_only=True)
class Event:
    op: ClassVar[str]
    #: Set by the writer.
    seq: int = 0
    ts: datetime = UNSET

    def to_json(self) -> dict[str, Any]:
        obj: dict[str, Any] = {
            "seq": self.seq,
            "ts": format_time(self.ts),
            "op": self.op,
        }
        for f in fields(self):
            if f.name in ("seq", "ts"):
                continue
            value = getattr(self, f.name)
            if value is None:
                continue
            if isinstance(value, frozenset):
                value = sorted(value)
            elif isinstance(value, datetime):
                value = format_time(value)
            obj[f.name] = value
        return obj

    @classmethod
    def from_json(cls, obj: object) -> Event:
        """Parse an event; raises ``ValueError`` if it is not a valid one."""
        if not isinstance(obj, dict):
            raise ValueError("event is not an object")
        op = obj.get("op")
        kind = EVENTS.get(op) if isinstance(op, str) else None
        if kind is None:
            raise ValueError(f"unknown op {op!r}")
        seq = obj.get("seq")
        if type(seq) is not int or seq < 1:
            raise ValueError("bad seq")
        ts = obj.get("ts")
        if not isinstance(ts, str):
            raise ValueError("bad ts")
        values: dict[str, Any] = {"seq": seq, "ts": parse_time(ts)}
        for f in fields(kind):
            if f.name not in ("seq", "ts"):
                values[f.name] = _field(obj, f.name, optional=f.default is None)
        unknown = obj.keys() - values.keys() - {"op"}
        if unknown:
            raise ValueError(f"unknown fields {sorted(unknown)}")
        return kind(**values)


#: What each field holds; a field means the same in every event.
_KINDS = {
    "msg": "msg",
    "mbox": "str",
    "name": "str",
    "role": "str",
    "rid": "str",
    "eid": "str",
    "set": "flags",
    "uidvalidity": "int",
    "segment": "str",
    "offset": "int",
    "parent": "str",
    "sort": "int",
    "subscribed": "bool",
    "props": "object",
    "received": "time",
    "thread": "str",
    "type": "str",
    "id": "str",
    "value": "object",
    "content": "text",
}


def _field(obj: dict[str, Any], key: str, *, optional: bool) -> Any:
    if key not in obj:
        if optional:
            return None
        raise ValueError(f"missing {key}")
    value = obj[key]
    match _KINDS[key]:
        case "str" if isinstance(value, str) and value:
            return value
        case "msg" if isinstance(value, str) and value.startswith(MSG_PREFIX):
            return value
        case "int" if type(value) is int and value >= 0:
            return value
        case "bool" if type(value) is bool:
            return value
        case "text" if isinstance(value, str):
            return value
        case "object" if isinstance(value, dict):
            return value
        case "time" if isinstance(value, str):
            return parse_time(value)
        case "flags" if isinstance(value, list) and all(
            isinstance(v, str) for v in value
        ):
            return frozenset(value)
    raise ValueError(f"bad {key}")


@dataclass(frozen=True, kw_only=True)
class Mbox(Event):
    """Everything known about the mailbox with key ``mbox``: what a field
    leaves out is unknown or unset, not unchanged."""

    op = "mbox"
    mbox: str
    name: str
    role: str | None = None
    #: The key of the parent mailbox; None at the top level.
    parent: str | None = None
    sort: int | None = None
    subscribed: bool | None = None
    #: The server's other properties of the mailbox, verbatim.
    props: dict[str, Any] | None = None


@dataclass(frozen=True, kw_only=True)
class Add(Event):
    op = "add"
    msg: str
    mbox: str
    rid: str
    eid: str | None = None
    #: When the server received the message (JMAP ``receivedAt``, IMAP
    #: ``INTERNALDATE``), in this account.
    received: datetime | None = None
    #: The server's thread id, a hint only.
    thread: str | None = None


@dataclass(frozen=True, kw_only=True)
class Props(Event):
    """The server's other properties of the remote message ``rid`` (in
    ``mbox``, where remote ids are scoped by mailbox), verbatim; the full
    set, never a diff."""

    op = "props"
    msg: str
    mbox: str | None = None
    rid: str
    props: dict[str, Any]


@dataclass(frozen=True, kw_only=True)
class Setting(Event):
    """An account setting object (``type`` is its JMAP type name, such as
    ``Identity``), verbatim; without ``value``, the object is gone. A Sieve
    script's text is in ``content``."""

    op = "setting"
    type: str
    id: str
    value: dict[str, Any] | None = None
    content: str | None = None


@dataclass(frozen=True, kw_only=True)
class Flags(Event):
    """The full flag set of ``msg`` in ``mbox``, never a diff."""

    op = "flags"
    msg: str
    mbox: str
    set: frozenset[str]


@dataclass(frozen=True, kw_only=True)
class Remove(Event):
    op = "remove"
    msg: str
    mbox: str
    rid: str


@dataclass(frozen=True, kw_only=True)
class Reset(Event):
    """Every remote id in ``mbox`` is void (a new UIDVALIDITY)."""

    op = "reset"
    mbox: str
    uidvalidity: int


@dataclass(frozen=True, kw_only=True)
class Gone(Event):
    """The mailbox ``mbox`` no longer exists on the server; whatever was
    journaled in it is void."""

    op = "gone"
    mbox: str


@dataclass(frozen=True, kw_only=True)
class Promote(Event):
    op = "promote"
    msg: str
    segment: str
    offset: int


@dataclass(frozen=True, kw_only=True)
class Drop(Event):
    op = "drop"
    msg: str


@dataclass(frozen=True, kw_only=True)
class Undrop(Event):
    op = "undrop"
    msg: str


@dataclass(frozen=True, kw_only=True)
class Purge(Event):
    op = "purge"
    msg: str


EVENTS: dict[str, type[Event]] = {
    k.op: k
    for k in (
        Mbox,
        Add,
        Flags,
        Props,
        Remove,
        Reset,
        Gone,
        Promote,
        Drop,
        Undrop,
        Purge,
        Setting,
    )
}


def encode_line(event: Event) -> bytes:
    text = json.dumps(event.to_json(), ensure_ascii=False, separators=(",", ":"))
    return text.encode("utf-8") + b"\n"


def decode_line(line: bytes) -> Event:
    """One line, without its newline; raises ``ValueError``."""
    try:
        obj = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"not JSON: {e}") from None
    return Event.from_json(obj)


# --- state ---------------------------------------------------------------


@dataclass
class Mailbox:
    name: str
    role: str | None = None
    parent: str | None = None
    sort: int | None = None
    subscribed: bool | None = None
    props: dict[str, Any] | None = None
    #: The UIDVALIDITY its remote ids are scoped by, once one was reset.
    uidvalidity: int | None = None


@dataclass(frozen=True)
class Placed:
    """What the server says about a message at (mbox, rid)."""

    received: datetime | None = None
    thread: str | None = None


@dataclass(frozen=True)
class Fate:
    """What happened to a message held in ``incoming/``."""

    status: Literal["promoted", "dropped", "purged"]
    #: When it was journaled; the grace period runs from a drop.
    ts: datetime
    segment: str | None = None
    offset: int | None = None


@dataclass
class State:
    """The replayed journal of one account."""

    seq: int = 0
    mailboxes: dict[str, Mailbox] = field(default_factory=dict)
    #: mbox → rid → msg.
    contents: dict[str, dict[str, str]] = field(default_factory=dict)
    #: msg → mbox → rids; the inverse of ``contents``.
    places: dict[str, dict[str, set[str]]] = field(default_factory=dict)
    #: (mbox, rid) → what the ``add`` said, for every entry of ``contents``.
    placed: dict[tuple[str, str], Placed] = field(default_factory=dict)
    #: (msg, mbox) → the last flag set journaled, if not empty (so a lost
    #: ``flags`` event with no flags changes nothing); kept when the message
    #: leaves the mailbox (clearing it would depend on the order of events);
    #: use ``flags_of``.
    flags: dict[tuple[str, str], frozenset[str]] = field(default_factory=dict)
    #: Stable server id → msg. Kept after removal: it still identifies the body.
    eids: dict[str, str] = field(default_factory=dict)
    #: msg → fate; a message held in ``incoming/`` has none.
    fates: dict[str, Fate] = field(default_factory=dict)
    #: (mbox or None, rid) → the server's other properties; like ``flags``,
    #: kept when the message goes, so readers check that it is still there.
    props: dict[tuple[str | None, str], dict[str, Any]] = field(default_factory=dict)
    #: (type, id) → (value, content) of each setting object.
    settings: dict[tuple[str, str], tuple[dict[str, Any], str | None]] = field(
        default_factory=dict
    )

    def apply(self, event: Event) -> None:
        match event:
            case Mbox(mbox=mbox, name=name):
                box = self.mailboxes.setdefault(mbox, Mailbox(name))
                box.name, box.role = name, event.role
                box.parent, box.sort = event.parent, event.sort
                box.subscribed, box.props = event.subscribed, event.props
            case Add(msg=msg, mbox=mbox, rid=rid, eid=eid):
                self._unplace(mbox, rid)
                self.contents.setdefault(mbox, {})[rid] = msg
                self.places.setdefault(msg, {}).setdefault(mbox, set()).add(rid)
                self.placed[mbox, rid] = Placed(event.received, event.thread)
                if eid is not None:
                    self.eids[eid] = msg
            case Flags(msg=msg, mbox=mbox, set=flags):
                if flags:
                    self.flags[msg, mbox] = flags
                else:
                    self.flags.pop((msg, mbox), None)
            case Props(mbox=mbox, rid=rid, props=props):
                if props:
                    self.props[mbox, rid] = props
                else:
                    self.props.pop((mbox, rid), None)
            case Setting(type=type_, id=id_, value=value, content=content):
                if value is None:
                    self.settings.pop((type_, id_), None)
                else:
                    self.settings[type_, id_] = (value, content)
            case Remove(mbox=mbox, rid=rid):
                self._unplace(mbox, rid)
            case Reset(mbox=mbox, uidvalidity=uidvalidity):
                self._void(mbox)
                self.mailboxes.setdefault(mbox, Mailbox(mbox)).uidvalidity = uidvalidity
            case Gone(mbox=mbox):
                self._void(mbox)
                self.mailboxes.pop(mbox, None)
            case Promote(msg=msg, ts=ts, segment=segment, offset=offset):
                self.fates[msg] = Fate("promoted", ts, segment, offset)
            case Drop(msg=msg, ts=ts):
                self.fates[msg] = Fate("dropped", ts)
            case Undrop(msg=msg):
                self.fates.pop(msg, None)
            case Purge(msg=msg, ts=ts):
                self.fates[msg] = Fate("purged", ts)
            case _:
                raise TypeError(f"not an event: {event!r}")
        self.seq = max(self.seq, event.seq)

    def flags_of(self, msg: str, mbox: str) -> frozenset[str] | None:
        """The flags of ``msg`` in ``mbox``; None if it is not in it."""
        if mbox not in self.places.get(msg, {}):
            return None
        return self.flags.get((msg, mbox), frozenset())

    def _void(self, mbox: str) -> None:
        """Forget every remote id, flag set and ``props`` of ``mbox``."""
        for rid in list(self.contents.get(mbox, {})):
            self._unplace(mbox, rid)
        for key in [k for k in self.flags if k[1] == mbox]:
            del self.flags[key]
        for pkey in [k for k in self.props if k[0] == mbox]:
            del self.props[pkey]

    def _unplace(self, mbox: str, rid: str) -> None:
        msg = self.contents.get(mbox, {}).pop(rid, None)
        if msg is None:
            return
        del self.placed[mbox, rid]
        if not self.contents[mbox]:
            del self.contents[mbox]
        rids = self.places[msg][mbox]
        rids.discard(rid)
        if not rids:
            del self.places[msg][mbox]
            if not self.places[msg]:
                del self.places[msg]


# --- files ---------------------------------------------------------------


def journal_dir(store: Path, account: str) -> Path:
    return store / "state" / account


def file_name(year: int, month: int) -> str:
    return f"journal-{year:04d}-{month:02d}.jsonl"


def journal_files(store: Path, account: str) -> list[Path]:
    """The journal files of an account, oldest first."""
    found = []
    directory = journal_dir(store, account)
    if directory.is_dir():
        for p in directory.iterdir():
            m = _FILE_NAME.fullmatch(p.name)
            if m and 1 <= int(m[2]) <= 12:
                found.append(p)
    return sorted(found)


def _parse(path: Path, *, last: bool) -> tuple[list[Event], int, int]:
    """Parse a file; returns its events, the length of its valid part, and
    the file size.

    A torn tail (lines that do not parse up to the end of the file, an
    unterminated line, zeros) is accepted in the ``last`` file only.
    """
    data = path.read_bytes()
    events: list[Event] = []
    pos = 0
    lineno = 0
    while pos < len(data):
        lineno += 1
        end = data.find(b"\n", pos)
        try:
            if end < 0:
                raise ValueError("unterminated line")
            events.append(decode_line(data[pos:end]))
        except ValueError as e:
            if last and _all_torn(data, end):
                return events, pos, len(data)
            raise JournalError(path, lineno, str(e)) from None
        pos = end + 1
    return events, pos, len(data)


def _all_torn(data: bytes, end: int) -> bool:
    """Whether no line after the one ending at ``end`` parses."""
    if end < 0:
        return True
    for line in data[end + 1 :].split(b"\n"):
        try:
            decode_line(line)
        except ValueError:
            continue
        return False
    return True


def read_events(store: Path, account: str, after: int = 0) -> Iterator[Event]:
    """The events of an account with ``seq`` greater than ``after``, in order."""
    files = journal_files(store, account)
    prev = 0
    for i, path in enumerate(files):
        events, valid, size = _parse(path, last=i == len(files) - 1)
        if valid < size:
            log.warning("%s: ignoring a torn tail of %d bytes", path, size - valid)
        for n, event in enumerate(events, 1):
            if event.seq <= prev:
                raise JournalError(path, n, f"seq {event.seq} after {prev}")
            prev = event.seq
            if event.seq > after:
                yield event


def replay(store: Path, account: str, state: State | None = None) -> State:
    """Apply the events after ``state.seq`` to ``state`` (a new one if None)."""
    state = State() if state is None else state
    for event in read_events(store, account, after=state.seq):
        state.apply(event)
    return state


def recover(path: Path) -> int:
    """Truncate a torn tail of ``path``; returns the number of bytes removed."""
    _, valid, size = _parse(path, last=True)
    if valid == size:
        return 0
    with open(path, "r+b") as f:
        f.truncate(valid)
        f.flush()
        os.fsync(f.fileno())
    log.warning(
        "%s: removed a torn tail of %d bytes at offset %d", path, size - valid, valid
    )
    return size - valid


def _now() -> datetime:
    return datetime.now(UTC)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class JournalWriter:
    """Appends batches of events to the journal of one account.

    On the first batch, the last journal file is recovered and ``seq``
    continues from its last event.
    """

    def __init__(
        self, store: Path, account: str, *, clock: Callable[[], datetime] = _now
    ) -> None:
        self.store = store
        self.account = account
        self.clock = clock
        self.seq: int | None = None

    def _last_seq(self) -> int:
        files = journal_files(self.store, self.account)
        if files:
            recover(files[-1])
        for path in reversed(files):
            events, _, _ = _parse(path, last=False)
            if events:
                return events[-1].seq
        return 0

    def append(self, events: Iterable[Event]) -> list[Event]:
        """Stamp ``seq`` and ``ts`` on the events, append them and fsync.
        Returns the stamped events."""
        if self.seq is None:
            self.seq = self._last_seq()
        ts = self.clock().astimezone(UTC).replace(microsecond=0)
        stamped = [replace(e, seq=self.seq + i, ts=ts) for i, e in enumerate(events, 1)]
        if not stamped:
            return []
        directory = journal_dir(self.store, self.account)
        path = directory / file_name(ts.year, ts.month)
        if not path.exists():
            directory.mkdir(parents=True, exist_ok=True)
            path.touch()
            _fsync_dir(directory)
            _fsync_dir(directory.parent)
            _fsync_dir(directory.parent.parent)
        with open(path, "ab") as f:
            f.write(b"".join(encode_line(e) for e in stamped))
            f.flush()
            os.fsync(f.fileno())
        self.seq = stamped[-1].seq
        return stamped
