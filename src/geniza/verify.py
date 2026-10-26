"""``geniza verify``: check a store end to end.

Every record is read in full (zstd checksum, stripped size), its message
rebuilt from the attachment files and hashed against its id. Every
attachment file is re-hashed in full; files that no record names (orphaned
by a crash before their message was committed) are reported but are not
problems. Every file in ``incoming/`` is hashed against its name; files
of messages also in a segment (left by a crash, or by another account's
promotion) and temp files are reported but are not problems. Every journal
is read through and replayed: each message in an account's mailboxes must
be in a segment or in its ``incoming/`` (unless purged), and each
promotion must name a record of its message.
"""

import argparse
import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from geniza.config import load, read_store
from geniza.store import attachments, incoming, segment
from geniza.store.attachments import BLOB_PREFIX, PREFIX_LEN
from geniza.store.journal import MSG_PREFIX, JournalError, State, read_events
from geniza.store.parts import parse_parts
from geniza.store.reader import RebuildError, rebuild
from geniza.store.segment import SegmentError

if TYPE_CHECKING:
    from geniza.cli import Output

log = logging.getLogger(__name__)


@dataclass
class Report:
    records: int = 0
    messages: int = 0
    bytes: int = 0
    attachment_files: int = 0
    orphans: list[str] = field(default_factory=list)
    #: Files in ``incoming/`` named like a held message.
    held: int = 0
    held_bytes: int = 0
    #: Of those, the files of messages that are also in a segment.
    held_in_segment: list[str] = field(default_factory=list)
    temp_files: list[str] = field(default_factory=list)
    journal_events: int = 0
    accounts: int = 0
    problems: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "records": self.records,
            "messages": self.messages,
            "bytes": self.bytes,
            "attachment_files": self.attachment_files,
            "orphans": self.orphans,
            "held": self.held,
            "held_bytes": self.held_bytes,
            "held_in_segment": self.held_in_segment,
            "temp_files": self.temp_files,
            "journal_events": self.journal_events,
            "accounts": self.accounts,
            "problems": self.problems,
        }

    def text(self) -> str:
        lines = [
            f"records: {self.records} ({self.messages} messages, {self.bytes} bytes)",
            f"attachment files: {self.attachment_files}"
            f" ({len(self.orphans)} not referenced by any record)",
            f"incoming: {self.held} files ({self.held_bytes} bytes,"
            f" {len(self.held_in_segment)} also in a segment,"
            f" {len(self.temp_files)} temp files)",
            f"journal: {self.journal_events} events in {self.accounts} accounts",
            f"problems: {len(self.problems)}",
        ]
        lines += [f"  {p}" for p in self.problems]
        return "\n".join(lines) + "\n"


class _Files:
    """Attachment file hashes, each file hashed once."""

    def __init__(self, store: Path) -> None:
        self.store = store
        self.hashes: dict[str, str] = {}

    def hash(self, file: str) -> str:
        if (h := self.hashes.get(file)) is None:
            with open(self.store / file, "rb") as f:
                h = BLOB_PREFIX + hashlib.file_digest(f, "sha256").hexdigest()
            self.hashes[file] = h
        return h

    def read(self, file: str) -> bytes:
        content = (self.store / file).read_bytes()
        self.hashes.setdefault(file, attachments.blob_id(content))
        return content


def _check_record(
    store: Path, files: _Files, path: Path, offset: int, report: Report
) -> str | None:
    """Check one record and every file it names; returns its id, or None
    after reporting its problems."""
    where = f"{path.relative_to(store).as_posix()} at {offset}"
    try:
        header, stripped = segment.read(path, offset)
        parts = parse_parts(header.parts, header.stripped_size)
    except (SegmentError, ValueError) as e:
        report.problems.append(f"{where}: {e}")
        return None
    # A stub's file is read (and hashed) once, for the rebuild too.
    contents: dict[str, bytes] = {}
    ok = True
    for p in parts:
        try:
            if p.recipe is not None:
                contents[p.file] = files.read(p.file)
            if files.hash(p.file) != p.blob:
                report.problems.append(
                    f"{where}: {p.file}: content does not match {p.blob}"
                )
                ok = False
        except OSError as e:
            report.problems.append(f"{where}: {p.file}: {e.strerror}")
            ok = False
    if not ok:
        return None
    try:
        data = rebuild(store, header, stripped, lambda file, _: contents[file])
    except RebuildError as e:
        report.problems.append(f"{where}: {e}")
        return None
    report.bytes += len(data)
    return header.id


def _check_incoming(store: Path, ids: set[str], report: Report) -> dict[str, set[str]]:
    """Check every file in ``incoming/``; returns the messages held by
    each account (those whose file is readable)."""
    root = store / incoming.INCOMING_DIR
    held: dict[str, set[str]] = {}
    if not root.is_dir():
        return held
    for directory in sorted(root.iterdir()):
        if not directory.is_dir():
            report.problems.append(
                f"{directory.relative_to(store).as_posix()}: not a directory"
            )
            continue
        account = held.setdefault(directory.name, set())
        files = incoming.files(store, directory.name)
        names = {p.name for p in files.values()}
        for p in sorted(directory.iterdir()):
            if p.name in names:
                continue
            where = p.relative_to(store).as_posix()
            if incoming.is_temp(p.name):
                report.temp_files.append(where)
            else:
                report.problems.append(f"{where}: not a held message")
        for msg, p in files.items():
            where = p.relative_to(store).as_posix()
            try:
                with open(p, "rb") as f:
                    digest = hashlib.file_digest(f, "sha256").hexdigest()
                    size = f.tell()
            except OSError as e:
                report.problems.append(f"{where}: {e.strerror}")
                continue
            report.held += 1
            report.held_bytes += size
            if MSG_PREFIX + digest != msg:
                report.problems.append(f"{where}: does not hash to its name")
                continue
            account.add(msg)
            if msg in ids:
                report.held_in_segment.append(where)
    return held


def _check_journal(
    state: State,
    account: str,
    held: set[str],
    records: dict[tuple[str, int], str],
    report: Report,
) -> None:
    """Check that an account's replayed journal agrees with the store."""
    stored = set(records.values())
    missing = set()
    for rids in state.contents.values():
        for msg in rids.values():
            if msg in stored or msg in held:
                continue
            fate = state.fates.get(msg)
            if fate is None or fate.status != "purged":
                missing.add(msg)
    report.problems += [
        f"journal of {account}: {msg} is in a mailbox but not stored"
        for msg in sorted(missing)
    ]
    for msg, fate in sorted(state.fates.items()):
        if fate.status != "promoted":
            continue
        assert fate.segment is not None and fate.offset is not None
        if records.get((fate.segment, fate.offset)) != msg:
            report.problems.append(
                f"journal of {account}: {msg} promoted to {fate.segment}"
                f" at {fate.offset}, which is not a record of it"
            )


def verify(store: Path) -> Report:
    report = Report()
    files = _Files(store)
    ids: set[str] = set()
    #: (segment, offset) → id, from the record headers.
    records: dict[tuple[str, int], str] = {}
    for path in segment.segments(store):
        offsets = []
        name = path.relative_to(store).as_posix()
        try:
            for offset, header in segment.scan(path):
                offsets.append(offset)
                records[name, offset] = header.id
        except SegmentError as e:
            report.problems.append(str(e))
        for offset in offsets:
            report.records += 1
            if (id := _check_record(store, files, path, offset, report)) is not None:
                ids.add(id)
            if report.records % 10000 == 0:
                log.info("%d records", report.records)
    report.messages = len(ids)
    for prefix, file in attachments.scan(store):
        report.attachment_files += 1
        referenced = file in files.hashes
        try:
            h = files.hash(file)
        except OSError as e:
            report.problems.append(f"{file}: {e.strerror}")
            continue
        if h.removeprefix(BLOB_PREFIX)[:PREFIX_LEN] != prefix:
            report.problems.append(f"{file}: content does not match its name")
        elif not referenced:
            report.orphans.append(file)
    held = _check_incoming(store, ids, report)
    state = store / "state"
    accounts = (
        sorted(p.name for p in state.iterdir() if p.is_dir()) if state.is_dir() else []
    )
    for account in accounts:
        report.accounts += 1
        replayed = State()
        try:
            for event in read_events(store, account):
                report.journal_events += 1
                replayed.apply(event)
        except JournalError as e:
            report.problems.append(str(e))
        _check_journal(replayed, account, held.get(account, set()), records, report)
    return report


def run(args: argparse.Namespace, out: Output) -> int:
    store = load(args.config).store(args.store).path
    read_store(store)
    report = verify(store)
    out.result(report.text(), report.to_json())
    return 1 if report.problems else 0
