"""SSH agents for tests: an in-process fake, and a private real ``ssh-agent``.

Neither touches the developer's agent: both listen on a socket of their
own, on a short path (Unix socket paths are limited to ~108 bytes), and
callers pass that path (or an environment pointing to it) explicitly.
"""

import base64
import hashlib
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa

from geniza.sshcrypt import Agent

FIXTURES = Path(__file__).parent / "fixtures" / "sshcrypt"

type PrivateKey = (
    ed25519.Ed25519PrivateKey | rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey
)


def _string(b: bytes) -> bytes:
    return struct.pack(">I", len(b)) + b


def public_blob(key: PrivateKey) -> bytes:
    line = key.public_key().public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
    )
    return base64.b64decode(line.split()[1])


def fixture_key(name: str) -> PrivateKey:
    key = serialization.load_ssh_private_key((FIXTURES / name).read_bytes(), None)
    assert isinstance(
        key, ed25519.Ed25519PrivateKey | rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey
    )
    return key


@contextmanager
def short_dir() -> Iterator[Path]:
    """A new directory with a path short enough for a socket in it."""
    base = tempfile.gettempdir()
    d = Path(tempfile.mkdtemp(prefix="gz-", dir=base if len(base) < 60 else "/tmp"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


# --- Ed25519 with a random nonce ----------------------------------------------
# A valid signature that differs every time, as a misbehaving agent or token
# might produce (RFC 8032 arithmetic, with r random instead of derived).

_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P


def _add(
    a: tuple[int, int, int, int], b: tuple[int, int, int, int]
) -> tuple[int, int, int, int]:
    p = _P
    aa = (a[1] - a[0]) * (b[1] - b[0]) % p
    bb = (a[1] + a[0]) * (b[1] + b[0]) % p
    cc = 2 * a[3] * b[3] * _D % p
    dd = 2 * a[2] * b[2] % p
    e, f, g, h = bb - aa, dd - cc, dd + cc, bb + aa
    return (e * f % p, g * h % p, f * g % p, e * h % p)


def _mul(s: int, pt: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    q = (0, 1, 1, 0)
    while s:
        if s & 1:
            q = _add(q, pt)
        pt = _add(pt, pt)
        s >>= 1
    return q


def _base() -> tuple[int, int, int, int]:
    y = 4 * pow(5, _P - 2, _P) % _P
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * pow(2, (_P - 1) // 4, _P) % _P
    if x & 1:
        x = _P - x
    return (x, y, 1, x * y % _P)


def _compress(pt: tuple[int, int, int, int]) -> bytes:
    zinv = pow(pt[2], _P - 2, _P)
    x, y = pt[0] * zinv % _P, pt[1] * zinv % _P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def random_ed25519_sign(key: ed25519.Ed25519PrivateKey, data: bytes) -> bytes:
    seed = key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    g = _base()
    pub = _compress(_mul(a, g))
    r = int.from_bytes(os.urandom(64), "little") % _L
    big_r = _compress(_mul(r, g))
    k = int.from_bytes(hashlib.sha512(big_r + pub + data).digest(), "little") % _L
    return big_r + ((r + k * a) % _L).to_bytes(32, "little")


# --- the fake agent -----------------------------------------------------------


@dataclass
class Entry:
    key: PrivateKey
    comment: str


class FakeAgent:
    """An agent protocol server on a Unix socket, in a thread.

    ``mode`` changes how it answers sign requests: ``ok``, ``refuse``,
    ``wrong-key`` (a valid signature by another key), ``garbage``,
    ``random`` (Ed25519 with a random nonce), ``wrong-alg``, ``trailing``,
    ``truncate`` (a reply cut short, then the connection closed) and
    ``raw`` (``raw_reply`` sent as is, then closed). ``refuse-list``
    refuses to list keys. ``signs`` counts sign requests.
    """

    def __init__(self, path: Path, entries: list[Entry]) -> None:
        self.path = path
        self.entries = entries
        self.mode = "ok"
        self.raw_reply = b""
        self.signs = 0
        self._other = ed25519.Ed25519PrivateKey.generate()
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(path))
        self._server.listen()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def close(self) -> None:
        # shutdown wakes the accept() that close alone would leave blocked.
        self._server.shutdown(socket.SHUT_RDWR)
        self._server.close()
        self._thread.join(timeout=5)

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            while True:
                header = _recv(conn, 4)
                if header is None:
                    return
                body = _recv(conn, struct.unpack(">I", header)[0])
                if body is None:
                    return
                if self.mode == "raw":
                    conn.sendall(self.raw_reply)
                    return
                reply = self._answer(body)
                if self.mode == "truncate":
                    conn.sendall(_string(reply)[: 4 + len(reply) // 2])
                    return
                conn.sendall(_string(reply))

    def _answer(self, body: bytes) -> bytes:
        if body[0] == 11:
            if self.mode == "refuse-list":
                return bytes([5])
            out = struct.pack(">I", len(self.entries))
            for e in self.entries:
                out += _string(public_blob(e.key)) + _string(e.comment.encode())
            return bytes([12]) + out
        if body[0] != 13:
            return bytes([5])
        self.signs += 1
        pos = 1
        (n,) = struct.unpack(">I", body[pos : pos + 4])
        blob = body[pos + 4 : pos + 4 + n]
        pos += 4 + n
        (n,) = struct.unpack(">I", body[pos : pos + 4])
        data = body[pos + 4 : pos + 4 + n]
        (flags,) = struct.unpack(">I", body[pos + 4 + n : pos + 8 + n])
        entry = next((e for e in self.entries if public_blob(e.key) == blob), None)
        if entry is None or self.mode == "refuse":
            return bytes([5])
        sig = self._sign(entry.key, data, flags)
        if self.mode == "trailing":
            sig += b"\0"
        return bytes([14]) + _string(sig)

    def _sign(self, key: PrivateKey, data: bytes, flags: int) -> bytes:
        if self.mode == "garbage":
            return _string(b"ssh-ed25519") + _string(os.urandom(64))
        match key:
            case ed25519.Ed25519PrivateKey():
                if self.mode == "wrong-key":
                    raw = self._other.sign(data)
                elif self.mode == "random":
                    raw = random_ed25519_sign(key, data)
                else:
                    raw = key.sign(data)
                return _string(b"ssh-ed25519") + _string(raw)
            case rsa.RSAPrivateKey():
                if self.mode == "wrong-alg":
                    alg, h = b"rsa-sha2-256", hashes.SHA256()
                elif flags & 4:
                    alg, h = b"rsa-sha2-512", hashes.SHA512()
                else:
                    alg, h = b"ssh-rsa", hashes.SHA1()
                return _string(alg) + _string(key.sign(data, padding.PKCS1v15(), h))
            case ec.EllipticCurvePrivateKey():
                raw = key.sign(data, ec.ECDSA(hashes.SHA256()))
                return _string(b"ecdsa-sha2-nistp256") + _string(raw)


def _recv(conn: socket.socket, n: int) -> bytes | None:
    buf = b""
    while len(buf) < n:
        try:
            chunk = conn.recv(n - len(buf))
        except OSError:
            return None
        if not chunk:
            return None
        buf += chunk
    return buf


@contextmanager
def fake_agent(entries: list[Entry]) -> Iterator[FakeAgent]:
    with short_dir() as d:
        agent = FakeAgent(d / "agent", entries)
        try:
            yield agent
        finally:
            agent.close()


# --- a real ssh-agent ---------------------------------------------------------


@dataclass
class RealAgent:
    sock: Path
    env: dict[str, str]
    dir: Path

    def add(self, key_file: Path) -> None:
        """``ssh-add`` a key file (copied, as ssh-add wants mode 0600)."""
        assert self.env["SSH_AUTH_SOCK"] == str(self.sock)
        copy = self.dir / key_file.name
        shutil.copyfile(key_file, copy)
        copy.chmod(0o600)
        subprocess.run(
            ["ssh-add", "-q", str(copy)],
            env=self.env,
            check=True,
            stdin=subprocess.DEVNULL,
            capture_output=True,
        )


@contextmanager
def real_agent() -> Iterator[RealAgent]:
    """A private ``ssh-agent``, started on its own socket and checked to be
    running (and empty) before anything is added to it."""
    exe, add = shutil.which("ssh-agent"), shutil.which("ssh-add")
    if exe is None or add is None:
        pytest.skip("ssh-agent is not installed")
    with short_dir() as d:
        sock = d / "agent"
        proc = subprocess.Popen(
            [exe, "-D", "-a", str(sock)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 10
            while not sock.exists():
                if proc.poll() is not None or time.monotonic() > deadline:
                    pytest.fail("ssh-agent did not start")
                time.sleep(0.02)
            with Agent(str(sock)) as agent:
                assert agent.identities() == []
            env = {k: v for k, v in os.environ.items() if k != "SSH_AGENT_PID"}
            env["SSH_AUTH_SOCK"] = str(sock)
            yield RealAgent(sock, env, d)
        finally:
            proc.terminate()
            proc.wait(timeout=10)
