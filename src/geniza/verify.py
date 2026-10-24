"""``geniza verify``: check a store end to end.

Every record is read in full (zstd checksum, stripped size), its message
rebuilt from the attachment files and hashed against its id. Every
attachment file is re-hashed in full; files that no record names (orphaned
by a crash before their message was committed) are reported but are not
problems. Every journal is read through.
"""

import argparse
import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from geniza.config import load, read_store
from geniza.store import attachments, segment
from geniza.store.attachments import BLOB_PREFIX, PREFIX_LEN
from geniza.store.journal import JournalError, read_events
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
            "journal_events": self.journal_events,
            "accounts": self.accounts,
            "problems": self.problems,
        }

    def text(self) -> str:
        lines = [
            f"records: {self.records} ({self.messages} messages, {self.bytes} bytes)",
            f"attachment files: {self.attachment_files}"
            f" ({len(self.orphans)} not referenced by any record)",
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


def verify(store: Path) -> Report:
    report = Report()
    files = _Files(store)
    ids: set[str] = set()
    for path in segment.segments(store):
        offsets = []
        try:
            for offset, _ in segment.scan(path):
                offsets.append(offset)
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
    state = store / "state"
    accounts = (
        sorted(p.name for p in state.iterdir() if p.is_dir()) if state.is_dir() else []
    )
    for account in accounts:
        report.accounts += 1
        try:
            for _ in read_events(store, account):
                report.journal_events += 1
        except JournalError as e:
            report.problems.append(str(e))
    return report


def run(args: argparse.Namespace, out: Output) -> int:
    store = load(args.config).store(args.store).path
    read_store(store)
    report = verify(store)
    out.result(report.text(), report.to_json())
    return 1 if report.problems else 0
