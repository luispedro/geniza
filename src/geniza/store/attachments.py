"""Attachment files (DESIGN.md, "Content addressing" and "File naming").

A blob is decoded part content, identified by its sha256. It is stored once,
as ``attachments/YYYY/MM/<first 16 hex of the hash>_<name>``, in the month
it was first written, under the first name it was seen with. Files are
written to a temp file, fsynced, made read-only and renamed into place.

Which blobs already exist is the index's business; ``scan`` lists the files
for ``reindex``. The writer itself only notices files of the current month,
which is enough to reuse a file orphaned by a crash before its message was
committed.
"""

import hashlib
import logging
import os
import re
import secrets
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from geniza.mime.names import sanitize

log = logging.getLogger(__name__)

PREFIX_LEN = 16
BLOB_PREFIX = "sha256:"

_FILE_NAME = re.compile(r"([0-9a-f]{16})_(.+)", re.DOTALL)
_TEMP_NAME = re.compile(r"\.[0-9a-f]{16}\.[0-9a-f]{16}\.tmp")


class AttachmentError(Exception):
    """An attachment file that cannot be written or does not hold its blob."""


@dataclass(frozen=True)
class Stored:
    blob: str
    #: Relative to the store root, as stubs and the index refer to it.
    file: str
    #: False if the file was already there.
    new: bool


def blob_id(content: bytes) -> str:
    return BLOB_PREFIX + hashlib.sha256(content).hexdigest()


def _hex(blob: str) -> str:
    if not blob.startswith(BLOB_PREFIX):
        raise ValueError(f"not a blob id: {blob!r}")
    return blob.removeprefix(BLOB_PREFIX)


def file_name(blob: str, name: str) -> str:
    """``<first 16 hex>_<name>``; ``name`` must already be sanitised."""
    if not name or sanitize(name) != name:
        raise ValueError(f"unsanitised attachment name: {name!r}")
    return f"{_hex(blob)[:PREFIX_LEN]}_{name}"


def parse_file_name(filename: str) -> tuple[str, str] | None:
    """``(hash prefix, name)`` of an attachment file name, or None."""
    m = _FILE_NAME.fullmatch(filename)
    return (m[1], m[2]) if m else None


def month_dir(year: int, month: int) -> str:
    return f"attachments/{year:04d}/{month:02d}"


def scan(store: Path) -> Iterator[tuple[str, str]]:
    """Yield ``(hash prefix, file)`` for every attachment file, in path order.

    ``file`` is relative to the store root. Temp files and anything else
    that is not named like an attachment are skipped.
    """
    root = store / "attachments"
    for year in sorted(root.glob("[0-9][0-9][0-9][0-9]")):
        for month in sorted(year.glob("[0-9][0-9]")):
            for p in sorted(month.iterdir()):
                parsed = parse_file_name(p.name)
                if parsed and p.is_file():
                    yield parsed[0], p.relative_to(store).as_posix()


def read(store: Path, file: str, blob: str | None = None) -> bytes:
    """The content of an attachment file, checked against ``blob`` if given."""
    content = (store / file).read_bytes()
    if blob is not None and blob_id(content) != blob:
        raise AttachmentError(f"{file}: content does not match {blob}")
    return content


def _now() -> datetime:
    return datetime.now(UTC)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class AttachmentWriter:
    """Writes new blobs into the month directory of the current month (UTC).

    The caller looks a blob up (in the index) before calling ``put``; ``put``
    only checks the current month directory. When a month directory is
    first used, temp files left there by a crash are removed.
    """

    def __init__(self, store: Path, *, clock: Callable[[], datetime] = _now) -> None:
        self.store = store
        self.clock = clock
        self._month: tuple[int, int] | None = None
        #: Hash prefix → file name, for the files of the current month.
        self._files: dict[str, str] = {}

    def put(self, content: bytes, name: str, *, blob: str | None = None) -> Stored:
        """Store ``content`` as a file named ``name`` unless the current month
        already has it (then the first name wins).

        ``blob`` saves hashing when the caller has already done it; it is
        trusted. A different blob with the same hash prefix raises
        ``AttachmentError``.
        """
        blob = blob or blob_id(content)
        target = file_name(blob, name)
        prefix = target[:PREFIX_LEN]
        year, month = self.clock().astimezone(UTC).timetuple()[:2]
        if self._month != (year, month):
            self._open(year, month)
        directory = self.store / month_dir(year, month)
        if (existing := self._files.get(prefix)) is not None:
            file = f"{month_dir(year, month)}/{existing}"
            if blob_id((directory / existing).read_bytes()) != blob:
                raise AttachmentError(f"{file}: hash prefix shared with {blob}")
            return Stored(blob, file, new=False)
        tmp = directory / f".{prefix}.{secrets.token_hex(8)}.tmp"
        try:
            with open(tmp, "xb") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
                os.fchmod(f.fileno(), 0o444)
            os.rename(tmp, directory / target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        _fsync_dir(directory)
        self._files[prefix] = target
        return Stored(blob, f"{month_dir(year, month)}/{target}", new=True)

    def _open(self, year: int, month: int) -> None:
        directory = self.store / month_dir(year, month)
        if not directory.exists():
            directory.mkdir(parents=True)
            _fsync_dir(directory.parent)
            _fsync_dir(directory.parent.parent)
        self._files = {}
        for p in sorted(directory.iterdir()):
            if _TEMP_NAME.fullmatch(p.name):
                log.warning("%s: removing a temp file left by a crash", p)
                p.unlink()
            elif parsed := parse_file_name(p.name):
                # Two files with one prefix can only come from outside
                # geniza; keep the first in name order.
                self._files.setdefault(parsed[0], p.name)
        self._month = (year, month)
