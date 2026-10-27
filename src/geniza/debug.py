"""``geniza debug …``: development-only commands.

``debug import-local PATH`` ingests a Maildir (or a tree of them, as
offlineimap writes them) or an mbox into a store, to exercise ingest on real
volume before there is any network code. It is not a migration path: it
writes no journal events, so the store knows nothing of mailboxes or flags.

Local files rarely carry the server's received date (offlineimap leaves a
Maildir file's modification time at the download), so it is taken from the
message, as ``received_date`` describes. Messages are ingested oldest
first, as an initial sync would.
"""

import argparse
import logging
import mailbox
import os
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime, timedelta
from email import policy
from email.parser import BytesHeaderParser
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

from geniza.config import init_store, load
from geniza.index import reindex
from geniza.mime.recipes import Mode
from geniza.stats import maildir_files
from geniza.store import attachments, segment
from geniza.store.ingest import Ingester
from geniza.store.lock import StoreLock, StoreLocked

if TYPE_CHECKING:
    from geniza.cli import Output

log = logging.getLogger(__name__)

#: Header blocks are read in chunks of this size, up to HEADER_MAX.
HEADER_CHUNK = 8 * 1024
HEADER_MAX = 256 * 1024

EARLIEST = datetime(1980, 1, 1, tzinfo=UTC)

type Source = Iterator[tuple[str, bytes | OSError, datetime]]


def _header_end(data: bytes) -> int | None:
    ends = [
        i + len(sep) for sep in (b"\r\n\r\n", b"\n\n") if (i := data.find(sep)) >= 0
    ]
    return min(ends, default=None)


def _header_block(data: bytes) -> bytes:
    return data[: _header_end(data) or len(data)]


def _read_head(f: BinaryIO) -> bytes:
    """The start of a file, up to the end of its header block."""
    head = b""
    while len(head) < HEADER_MAX and _header_end(head) is None:
        chunk = f.read(HEADER_CHUNK)
        if not chunk:
            break
        head += chunk
    return head


def _date(value: object, now: datetime) -> datetime | None:
    """A header date, if it parses and is plausible; UTC if it has no zone."""
    try:
        d = parsedate_to_datetime(" ".join(str(value).split()))
    except TypeError, ValueError, IndexError, OverflowError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    d = d.astimezone(UTC)
    return d if EARLIEST <= d <= now + timedelta(days=1) else None


def received_date(
    data: bytes,
    *,
    hint: datetime | None = None,
    fallback: datetime,
    now: datetime | None = None,
) -> datetime:
    """When the message arrived, as best the message tells.

    In order: the first ``Received:`` line with a usable date (the last hop,
    which accepted the message for the mailbox), ``Delivery-date:``, the
    ``hint`` (an mbox ``From`` line's date), ``Date:`` (the sender's clock,
    and all that sent mail and drafts have), else ``fallback``. Dates
    before 1980 or more than a day in the future are skipped.
    """
    now = now or datetime.now(UTC)
    try:
        headers = BytesHeaderParser(policy=policy.compat32).parsebytes(
            _header_block(data)
        )
        received = [str(r) for r in headers.get_all("Received", [])]
        delivery, sent = headers["Delivery-date"], headers["Date"]
    except Exception:  # a date is not worth failing an import over
        received, delivery, sent = [], None, None
    for line in received:
        if ";" in line and (d := _date(line.rsplit(";", 1)[1], now)):
            return d
    if delivery is not None and (d := _date(delivery, now)):
        return d
    if hint is not None and EARLIEST <= hint <= now + timedelta(days=1):
        return hint
    if sent is not None and (d := _date(sent, now)):
        return d
    return fallback


def maildir_messages(root: Path) -> Source:
    """``(path, data or error, received)``, oldest first."""
    found = []
    for path in maildir_files(root):
        try:
            with open(path, "rb") as f:
                head = _read_head(f)
                mtime = os.fstat(f.fileno()).st_mtime
        except OSError as e:
            yield path, e, datetime.now(UTC)
            continue
        fallback = datetime.fromtimestamp(int(mtime), UTC)
        found.append((received_date(head, fallback=fallback), path))
    for received, path in sorted(found):
        try:
            with open(path, "rb") as f:
                data: bytes | OSError = f.read()
        except OSError as e:
            data = e
        yield path, data, received


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
    """``(key, data, received)`` in mbox order."""
    box = mailbox.mbox(path, create=False)
    try:
        for key in box.iterkeys():
            with box.get_file(key, from_=True) as f:
                hint = _from_date(f.readline())
            data = box.get_bytes(key)
            now = datetime.now(UTC)
            yield f"{path}#{key}", data, received_date(data, hint=hint, fallback=now)
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
            with closing(reindex.update(store, store_config.cache)) as conn:
                catalog = reindex.catalog(conn)
            policy = store_config.attachments
            with Ingester(store, lock, policy=policy, catalog=catalog) as ing:
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
        f"{counts['messages']:,} messages ({size:,} bytes):"
        f" {counts['new']:,} new, {counts['already stored']:,} already stored",
        f"store: {stored:,} bytes in segments and attachment files",
    ]
    parts = {k: v for k, v in sorted(counts.items()) if k.endswith(tuple(Mode))}
    if parts:
        lines.append("parts: " + ", ".join(f"{v:,} {k}" for k, v in parts.items()))
    for key in ("fallbacks", "read errors"):
        if counts[key]:
            lines.append(f"{key}: {counts[key]:,}")
    out.result("\n".join(lines), report)
    return 1 if counts["read errors"] else 0
