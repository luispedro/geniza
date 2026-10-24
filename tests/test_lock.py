import subprocess
import sys
import threading
from pathlib import Path

import pytest

from geniza.store.lock import LOCK_FILE, StoreLock, StoreLocked

# Holds flock on argv[1] until stdin closes; says so on stdout first.
HOLDER = """
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT)
fcntl.flock(fd, fcntl.LOCK_EX)
print("locked", flush=True)
sys.stdin.read()
"""


def hold_in_subprocess(store: Path) -> subprocess.Popen[str]:
    store.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(store / LOCK_FILE)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline() == "locked\n"
    return proc


def test_creates_store_and_empty_lock_file(tmp_path: Path) -> None:
    store = tmp_path / "a" / "store"
    with StoreLock(store) as lock:
        assert lock.locked
        assert (store / LOCK_FILE).read_bytes() == b""
    assert not lock.locked
    assert (store / LOCK_FILE).read_bytes() == b""


def test_excludes_second_lock_in_same_process(tmp_path: Path) -> None:
    with StoreLock(tmp_path):
        with pytest.raises(StoreLocked) as e:
            StoreLock(tmp_path).acquire()
        assert e.value.store == tmp_path
    with StoreLock(tmp_path):
        pass


def test_release_on_exception(tmp_path: Path) -> None:
    with pytest.raises(ValueError), StoreLock(tmp_path):
        raise ValueError
    with StoreLock(tmp_path):
        pass


def test_acquire_twice_and_release_twice(tmp_path: Path) -> None:
    lock = StoreLock(tmp_path)
    lock.acquire()
    with pytest.raises(RuntimeError):
        lock.acquire()
    lock.release()
    lock.release()
    lock.acquire()
    lock.release()


def test_other_process_holds_lock(tmp_path: Path) -> None:
    proc = hold_in_subprocess(tmp_path)
    try:
        with pytest.raises(StoreLocked):
            StoreLock(tmp_path).acquire()
    finally:
        proc.kill()
        proc.wait()
    # The kernel releases the lock of a killed process.
    with StoreLock(tmp_path):
        pass


def test_wait_blocks_until_released(tmp_path: Path) -> None:
    proc = hold_in_subprocess(tmp_path)
    got = threading.Event()

    def waiter() -> None:
        with StoreLock(tmp_path, wait=True):
            got.set()

    t = threading.Thread(target=waiter)
    t.start()
    try:
        assert not got.wait(0.2)
    finally:
        proc.communicate("")
    t.join(10)
    assert got.is_set()
