"""``geniza debug …``: development-only commands.

``debug import-local PATH`` ingests a Maildir (or a tree of them, as
offlineimap writes them) or an mbox into a store, to exercise ingest on real
volume before there is any network code. It is not a migration path: it
writes no journal events, so the store knows nothing of mailboxes or flags.

The received date is a Maildir file's modification time (offlineimap sets it
to the server's INTERNALDATE) or the date of an mbox ``From`` line.
Maildir messages are ingested oldest first, as an initial sync would.
"""

import argparse
import logging
import mailbox
import os
import time
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from geniza.config import init_store, load
from geniza.mime.recipes import Mode
from geniza.stats import maildir_files
from geniza.store import attachments, segment
from geniza.store.ingest import Ingester
from geniza.store.lock import StoreLock, StoreLocked

if TYPE_CHECKING:
    from geniza.cli import Output

log = logging.getLogger(__name__)

type Source = Iterator[tuple[str, bytes | OSError, datetime]]


def maildir_messages(root: Path) -> Source:
    """``(path, data or error, received)``, oldest first."""
    found = []
    for path in maildir_files(root):
        try:
            mtime = os.stat(path).st_mtime
        except OSError as e:
            yield path, e, datetime.now(UTC)
            continue
        found.append((mtime, path))
    for mtime, path in sorted(found):
        try:
            with open(path, "rb") as f:
                data: bytes | OSError = f.read()
        except OSError as e:
            data = e
        yield path, data, datetime.fromtimestamp(int(mtime), UTC)


def _from_date(line: bytes) -> datetime | None:
    """The date of an mbox ``From sender date`` line, taken as UTC."""
    fields = line.decode("ascii", "replace").split(None, 2)
    if len(fields) < 3:
        return None
    try:
        t = time.strptime(" ".join(fields[2].split()[:5]), "%a %b %d %H:%M:%S %Y")
    except ValueError:
        return None
    return datetime(*t[:6], tzinfo=UTC)


def mbox_messages(path: Path) -> Source:
    box = mailbox.mbox(path, create=False)
    try:
        for key in box.iterkeys():
            with box.get_file(key, from_=True) as f:
                received = _from_date(f.readline()) or datetime.now(UTC)
            yield f"{path}#{key}", box.get_bytes(key), received
    finally:
        box.close()


def _disk_usage(store: Path) -> int:
    total = sum(p.stat().st_size for p in segment.segments(store))
    for _, file in attachments.scan(store):
        total += (store / file).stat().st_size
    return total


def import_local(args: argparse.Namespace, out: Output) -> int:
    source: Path = args.path
    if not source.exists():
        log.error("%s: no such file or directory", source)
        return 1
    store_config = load(args.config).store(args.store)
    store = store_config.path
    messages = mbox_messages(source) if source.is_file() else maildir_messages(source)
    counts: Counter[str] = Counter()
    size = 0
    try:
        with StoreLock(store) as lock:
            init_store(store)
            policy = store_config.attachments
            with Ingester(store, lock, policy=policy) as ing:
                for name, data, received in messages:
                    if isinstance(data, OSError):
                        log.warning("cannot read %s: %s", name, data.strerror)
                        counts["read errors"] += 1
                        continue
                    added = ing.add(data, received)
                    counts["messages"] += 1
                    size += len(data)
                    counts["new" if added.new else "already stored"] += 1
                    counts["fallbacks"] += added.fallback
                    for p in added.parts:
                        counts[f"{p.kind} {p.mode}"] += 1
                    if counts["messages"] % 10000 == 0:
                        log.info("%d messages", counts["messages"])
    except StoreLocked as e:
        log.error("%s", e)
        return 1
    stored = _disk_usage(store)
    report = {"messages": counts["messages"], "bytes": size, "store_bytes": stored}
    report |= {k: v for k, v in sorted(counts.items()) if k != "messages"}
    lines = [
        f"{counts['messages']} messages ({size} bytes):"
        f" {counts['new']} new, {counts['already stored']} already stored",
        f"store: {stored} bytes in segments and attachment files",
    ]
    parts = {k: v for k, v in sorted(counts.items()) if k.endswith(tuple(Mode))}
    if parts:
        lines.append("parts: " + ", ".join(f"{v} {k}" for k, v in parts.items()))
    for key in ("fallbacks", "read errors"):
        if counts[key]:
            lines.append(f"{key}: {counts[key]}")
    out.result("\n".join(lines), report)
    return 1 if counts["read errors"] else 0
