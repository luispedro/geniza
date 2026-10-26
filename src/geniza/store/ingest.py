"""Ingest: putting one downloaded message into the store (DESIGN.md,
"Extraction modes" and "Write ordering and crash safety").

``Ingester.add`` writes the attachment files of a message, strips it, and
rebuilds the original from the record header as it will be written
(serialised and parsed back) and from the files as they are on disk. Only
if that hashes to the message id is the record appended. If it does not,
the message is stored again with every attachment as a copy, which needs no
rebuilding, and a warning is logged.

While the store is being initialized (DESIGN.md, "On-disk layout"), a
message's record and new attachment files go into the month it was
received in; afterwards, into the month it is stored in.

``Ingester.hold`` puts a message in an account's ``incoming/`` instead
(``store.incoming``), unless a segment has it already.

Journal events come next and are the caller's: a batch of messages goes into
segments, then their events into the journal. The caller holds the store
lock throughout.
"""

import hashlib
import json
import logging
from collections.abc import Callable, Sequence
from compression import zstd
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

from geniza.config import is_initialized
from geniza.mime.classify import PartClass
from geniza.mime.names import attachment_name
from geniza.mime.recipes import Mode, PartPlan, Policy, plan_message
from geniza.mime.strip import Cut, moved, reconstruct, strip
from geniza.store import attachments, incoming, segment
from geniza.store.attachments import AttachmentWriter, blob_id
from geniza.store.journal import MSG_PREFIX
from geniza.store.lock import StoreLock
from geniza.store.parts import PartRecord, parse_parts, stubs
from geniza.store.segment import SEGMENT_CAP, SegmentWriter

log = logging.getLogger(__name__)


class IngestError(Exception):
    """A message that could not be stored without loss."""


def message_id(data: bytes) -> str:
    return MSG_PREFIX + hashlib.sha256(data).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class Catalog:
    """What the store holds: messages by id, attachment files by blob.

    Callers with a cache take it from the index (``index.reindex.catalog``);
    ``scan``ning every segment header gives the same, slowly. A blob found
    only in an attachment file that no record names (orphaned by a crash)
    is not in it; ``AttachmentWriter`` reuses such a file if it is in the
    current month.
    """

    #: Message id → (segment, offset).
    messages: dict[str, tuple[str, int]] = field(default_factory=dict)
    #: Blob → file, both as record headers give them.
    blobs: dict[str, str] = field(default_factory=dict)

    @classmethod
    def scan(cls, store: Path) -> Self:
        """Read every record header of the store. A torn record at the end
        of the last segment must have been removed first."""
        catalog = cls()
        for path in segment.segments(store):
            name = path.relative_to(store).as_posix()
            for offset, header in segment.scan(path):
                catalog.messages.setdefault(header.id, (name, offset))
                for p in parse_parts(header.parts, header.stripped_size):
                    catalog.blobs.setdefault(p.blob, p.file)
        return catalog


@dataclass(frozen=True)
class Added:
    id: str
    segment: str
    offset: int
    #: False if the store already had the message.
    new: bool
    parts: tuple[PartRecord, ...] = ()
    #: True if stubbing failed the rebuild check and copies were used.
    fallback: bool = False


@dataclass(frozen=True)
class Held:
    id: str
    path: Path
    #: False if the account's ``incoming/`` already had the message.
    new: bool


@dataclass(frozen=True)
class _Extracted:
    plan: PartPlan
    blob: str
    file: str


class Ingester:
    """Stores messages into segments and attachment files.

    ``lock`` must be held for ``store`` while the ingester is used.
    ``initializing`` defaults to whether the store's initialization is
    still going on. On creation, a torn record is removed: at the end of
    any segment while initializing (records then go to any month), else at
    the end of the last one. Without a ``catalog``, one is then built by
    scanning the segments. A catalog from the index can be taken before
    the ingester is created: a torn record is never in the index.
    """

    def __init__(
        self,
        store: Path,
        lock: StoreLock,
        *,
        policy: Policy | None = None,
        catalog: Catalog | None = None,
        cap: int = SEGMENT_CAP,
        level: int = zstd.COMPRESSION_LEVEL_DEFAULT,
        clock: Callable[[], datetime] = _now,
        initializing: bool | None = None,
    ) -> None:
        if not lock.locked or lock.store.resolve() != store.resolve():
            raise RuntimeError(f"{store}: ingest needs the store lock")
        self.store = store
        self.lock = lock
        self.policy = policy or Policy()
        self.clock = clock
        if initializing is None:
            initializing = not is_initialized(store)
        self.initializing = initializing
        if initializing:
            segment.recover_all(store)
        else:
            segment.recover_last(store)
        self.catalog = catalog if catalog is not None else Catalog.scan(store)
        self.segments = SegmentWriter(store, cap=cap, level=level, clock=clock)
        self.attachments = AttachmentWriter(store, clock=clock)
        #: Accounts whose ``incoming/`` was opened (temp files removed).
        self._incoming: set[str] = set()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.segments.close()

    def add(self, data: bytes, received: datetime) -> Added:
        """Store a message unless the store already has it."""
        id = message_id(data)
        if (where := self.catalog.messages.get(id)) is not None:
            return Added(id, *where, new=False)
        month = self._month(received)
        plans = plan_message(data, self.policy)
        stripped, parts, bodies = self._strip(data, plans, month)
        entries = [p.to_json() for p in parts]
        fallback = not self._rebuilds(data, id, stripped, entries, bodies)
        if fallback:
            log.warning("%s: stripped message does not rebuild; using copies", id)
            plans = [_without_stub(p) for p in plans]
            stripped, parts, bodies = self._strip(data, plans, month)
            entries = [p.to_json() for p in parts]
            if not self._rebuilds(data, id, stripped, entries, bodies):
                raise IngestError(f"{id}: message does not rebuild from copies")
        location = self.segments.append(
            stripped,
            id=id,
            size=len(data),
            received=received,
            parts=entries,
            month=month,
        )
        self.catalog.messages[id] = (location.segment, location.offset)
        return Added(
            id, location.segment, location.offset, True, tuple(parts), fallback
        )

    def hold(self, data: bytes, received: datetime, account: str) -> Added | Held:
        """Hold a message in ``account``'s ``incoming/`` (DESIGN.md,
        "Placement at ingest"), unless a segment has it (an ``Added`` that
        is not new) or that ``incoming/`` has it already."""
        id = message_id(data)
        if (where := self.catalog.messages.get(id)) is not None:
            return Added(id, *where, new=False)
        if account not in self._incoming:
            incoming.open_dir(self.store, account)
            self._incoming.add(account)
        path = incoming.file_path(self.store, account, id)
        if path.exists():
            return Held(id, path, new=False)
        return Held(id, incoming.write(self.store, account, id, data, received), True)

    def _month(self, received: datetime) -> tuple[int, int] | None:
        """The month (year, month) to write a message into while
        initializing: when it was received, but never after now. None
        (the month it is stored in) afterwards."""
        if not self.initializing:
            return None
        year, month = min(received, self.clock()).astimezone(UTC).timetuple()[:2]
        return year, month

    def _file(self, plan: PartPlan, blob: str, month: tuple[int, int] | None) -> str:
        """The attachment file of ``blob``, written if the store lacks it."""
        if (file := self.catalog.blobs.get(blob)) is not None:
            return file
        assert plan.content is not None
        part = plan.part
        name = attachment_name(part.filename, part.path, part.content_type)
        file = self.attachments.put(plan.content, name, blob=blob, month=month).file
        self.catalog.blobs[blob] = file
        return file

    def _strip(
        self, data: bytes, plans: Sequence[PartPlan], month: tuple[int, int] | None
    ) -> tuple[bytes, list[PartRecord], list[tuple[int, int]]]:
        """Write the files of the extracted parts and strip the stubs out.
        Returns the stripped message, its part records and where each part's
        body was in ``data``."""
        extracted = []
        for p in plans:
            if p.mode is not Mode.INLINE:
                assert p.content is not None
                blob = blob_id(p.content)
                extracted.append(_Extracted(p, blob, self._file(p, blob, month)))
        extracted.sort(key=lambda e: e.plan.part.body_start)
        cuts = [
            Cut(e.plan.part.body_start, e.plan.part.end, e.file, e.plan.recipe)
            for e in extracted
            if e.plan.mode is Mode.STUB and e.plan.recipe is not None
        ]
        stripped, made = strip(data, cuts)
        stub_at = {cut.start: stub for cut, stub in zip(cuts, made, strict=True)}
        parts = []
        for e in extracted:
            p = e.plan
            if p.mode is Mode.STUB:
                stub = stub_at[p.part.body_start]
                offset, length = stub.offset, stub.length
            else:
                offset = moved(p.part.body_start, cuts, made)
                length = p.part.end - p.part.body_start
            parts.append(
                PartRecord(
                    path=p.part.path,
                    kind=p.cls,
                    mode=p.mode,
                    offset=offset,
                    length=length,
                    blob=e.blob,
                    file=e.file,
                    recipe=p.recipe if p.mode is Mode.STUB else None,
                )
            )
        bodies = [(e.plan.part.body_start, e.plan.part.end) for e in extracted]
        return stripped, parts, bodies

    def _rebuilds(
        self,
        data: bytes,
        id: str,
        stripped: bytes,
        entries: list[dict[str, Any]],
        bodies: list[tuple[int, int]],
    ) -> bool:
        """Whether the record header, as it will be read back, and the files
        on disk rebuild the message, and each copy's range holds its body.
        A file that is missing or does not hold its blob is damage, not a
        quirk of the message, and raises."""
        try:
            parsed = parse_parts(json.loads(json.dumps(entries)), len(stripped))
        except ValueError as e:
            log.debug("%s: %s", id, e)
            return False
        for p, (start, end) in zip(parsed, bodies, strict=True):
            if p.mode is Mode.COPY and stripped[p.offset : p.end] != data[start:end]:
                return False
        contents = [attachments.read(self.store, p.file, p.blob) for p in parsed]
        rebuilt = reconstruct(
            stripped,
            stubs(parsed),
            [c for p, c in zip(parsed, contents, strict=True) if p.recipe is not None],
        )
        return message_id(rebuilt) == id


def _without_stub(plan: PartPlan) -> PartPlan:
    """The plan with a stub turned into what it is without a recipe: a copy
    for an attachment, inline for a decoration."""
    if plan.mode is not Mode.STUB:
        return plan
    mode = Mode.COPY if plan.cls is PartClass.ATTACHMENT else Mode.INLINE
    return replace(plan, mode=mode, recipe=None)
