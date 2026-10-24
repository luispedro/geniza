"""One writer per store (DESIGN.md, "Write ordering and crash safety").

A writer holds ``flock(LOCK_EX)`` on ``store/.lock`` for as long as it
writes. The file stays empty: it is never rewritten, so backups see it
once. The kernel drops the lock when the process exits, however it exits,
so there is no stale lock to clean up after a crash.

The segment, attachment and journal writers do not lock by themselves;
whoever drives them (ingest, sync) holds a ``StoreLock`` around them.
Readers do not lock.
"""

import fcntl
import os
from pathlib import Path
from types import TracebackType
from typing import Self

LOCK_FILE = ".lock"


class StoreLocked(Exception):
    """Another process holds the store's lock."""

    def __init__(self, store: Path) -> None:
        super().__init__(f"{store}: store is in use by another geniza process")
        self.store = store


class StoreLock:
    """``flock`` on ``store/.lock``; a context manager.

    The store directory is created if it does not exist yet, so that a new
    store's ``store.toml`` can be written under the lock. Without ``wait``,
    acquiring a lock that another process holds raises ``StoreLocked``;
    with it, ``acquire`` blocks until the lock is free.

    Two ``StoreLock`` objects for the same store exclude each other even in
    one process, as each opens the file anew.
    """

    def __init__(self, store: Path, *, wait: bool = False) -> None:
        self.store = Path(store)
        self.wait = wait
        self._fd: int | None = None

    @property
    def locked(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        if self._fd is not None:
            raise RuntimeError(f"{self.store}: lock already held")
        self.store.mkdir(parents=True, exist_ok=True)
        fd = os.open(
            self.store / LOCK_FILE, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644
        )
        try:
            fcntl.flock(
                fd, fcntl.LOCK_EX if self.wait else fcntl.LOCK_EX | fcntl.LOCK_NB
            )
        except BlockingIOError:
            os.close(fd)
            raise StoreLocked(self.store) from None
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        # Closing the only descriptor of the open file releases the lock.
        os.close(fd)

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()
