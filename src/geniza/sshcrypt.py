"""Secrets encrypted to a key held in ``ssh-agent`` (DESIGN.md, "sshcrypt").

The encryption key is derived from the agent's signature over
``MAGIC ‖ salt``, so it exists only while the SSH key is loaded in the
agent. geniza talks to the agent socket directly: there is no code path
that reads a private key off disk. Every signature is verified against the
key's public half before it is used, so a rogue agent cannot return bytes
it chose.

The binary format is that of the standalone ``sshcrypt.py`` tool::

    MAGIC "SSHCRYPT2\\0" (10) ‖ sha256(public key blob) (32) ‖ salt (16)
        ‖ nonce (12) ‖ AES-256-GCM ciphertext and tag

In the configuration it is written base64-encoded after ``sshcrypt:``
(``encode``/``decode``). Compared to that tool, agent replies and
ciphertexts are parsed with bounds checks (every failure is an
``SshcryptError``), encryption signs twice and refuses a key whose
signatures differ, the key to encrypt to is chosen by the caller, and
nothing is printed.
"""

import base64
import binascii
import hashlib
import os
import socket
import struct
from dataclasses import dataclass, field
from types import TracebackType
from typing import Self

from cryptography.exceptions import InvalidSignature, InvalidTag, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAGIC = b"SSHCRYPT2\0"
INFO = b"sshcrypt-v2 aes-256-gcm"
PREFIX = "sshcrypt:"

KEY_ID_LEN = 32
SALT_LEN = 16
NONCE_LEN = 12
TAG_LEN = 16
HEADER_LEN = len(MAGIC) + KEY_ID_LEN + SALT_LEN + NONCE_LEN

SSH_AGENT_FAILURE = 5
SSH_AGENTC_REQUEST_IDENTITIES = 11
SSH_AGENT_IDENTITIES_ANSWER = 12
SSH_AGENTC_SIGN_REQUEST = 13
SSH_AGENT_SIGN_RESPONSE = 14
SSH_AGENT_RSA_SHA2_512 = 4

#: Key types whose signatures are deterministic, and the one signature
#: algorithm accepted back from the agent for each. ECDSA randomises every
#: signature and the ``sk-*`` (FIDO) types embed a use counter, so a key
#: derived from them could never be derived again.
SIG_ALG = {b"ssh-ed25519": b"ssh-ed25519", b"ssh-rsa": b"rsa-sha2-512"}

#: Largest agent reply read; a key list or a signature is far smaller.
MAX_REPLY = 1 << 20


class SshcryptError(Exception):
    """The agent, a key, a signature or a ciphertext is not usable. Messages
    never contain a secret or a signature."""


class _Reader:
    """SSH wire-format fields, bounds-checked."""

    def __init__(self, data: bytes, what: str) -> None:
        self.data = data
        self.pos = 0
        self.what = what

    def take(self, n: int) -> bytes:
        if n > len(self.data) - self.pos:
            raise SshcryptError(f"{self.what} is truncated")
        v = self.data[self.pos : self.pos + n]
        self.pos += n
        return v

    def uint32(self) -> int:
        return int.from_bytes(self.take(4))

    def string(self) -> bytes:
        return self.take(self.uint32())

    def done(self) -> bool:
        return self.pos == len(self.data)


def _string(b: bytes) -> bytes:
    return struct.pack(">I", len(b)) + b


def fingerprint(blob: bytes) -> str:
    """``SHA256:…`` as ``ssh-keygen -l`` prints it."""
    return _fingerprint_of(hashlib.sha256(blob).digest())


def _fingerprint_of(digest: bytes) -> str:
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


@dataclass(frozen=True)
class Key:
    """A public key the agent holds."""

    blob: bytes = field(repr=False)
    comment: str

    @property
    def type(self) -> str:
        try:
            return _Reader(self.blob, "key").string().decode("ascii", "replace")
        except SshcryptError:
            return "?"

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.blob)

    @property
    def usable(self) -> bool:
        """Whether its signatures are deterministic (RSA or Ed25519)."""
        return self.type.encode() in SIG_ALG


def verify(key: Key, data: bytes, sig: bytes) -> None:
    """Check that ``sig`` (SSH signature format) is a valid signature over
    ``data`` by ``key``, with the expected algorithm and nothing after it."""
    keytype = key.type.encode()
    if keytype not in SIG_ALG:
        raise SshcryptError(f"key type {key.type!r} is not usable")
    r = _Reader(sig, "signature")
    alg, raw = r.string(), r.string()
    if alg != SIG_ALG[keytype]:
        raise SshcryptError(
            f"agent signed with {alg.decode('ascii', 'replace')!r},"
            f" expected {SIG_ALG[keytype].decode()!r}"
        )
    if not r.done():
        raise SshcryptError("agent returned trailing data after the signature")
    try:
        pub = serialization.load_ssh_public_key(
            keytype + b" " + base64.b64encode(key.blob)
        )
    except ValueError, UnsupportedAlgorithm:
        raise SshcryptError(f"{key.fingerprint}: not a valid public key") from None
    try:
        match pub:
            case rsa.RSAPublicKey():
                # Some agents strip leading zero bytes; OpenSSL wants full width.
                raw = raw.rjust((pub.key_size + 7) // 8, b"\0")
                pub.verify(raw, data, padding.PKCS1v15(), hashes.SHA512())
            case ed25519.Ed25519PublicKey():
                pub.verify(raw, data)
            case _:
                raise SshcryptError(f"key type {key.type!r} is not usable")
    except InvalidSignature:
        raise SshcryptError("agent returned an invalid signature") from None


class Agent:
    """A connection to ``ssh-agent``; a context manager.

    ``path`` defaults to ``SSH_AUTH_SOCK``. Signing may block for as long
    as the agent waits for a confirmation (``ssh-add -c``).
    """

    def __init__(self, path: str | None = None) -> None:
        if path is None:
            path = os.environ.get("SSH_AUTH_SOCK")
        if not path:
            raise SshcryptError("SSH_AUTH_SOCK is not set: no agent running")
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.sock.connect(path)
        except OSError as e:
            self.sock.close()
            raise SshcryptError(
                f"cannot connect to the agent at {path}: {e.strerror or e}"
            ) from None

    def close(self) -> None:
        self.sock.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def _recv(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            try:
                chunk = self.sock.recv(n - len(buf))
            except OSError as e:
                raise SshcryptError(f"agent connection failed: {e}") from None
            if not chunk:
                raise SshcryptError("agent closed the connection")
            buf += chunk
        return bytes(buf)

    def _request(self, payload: bytes) -> tuple[int, _Reader]:
        try:
            self.sock.sendall(_string(payload))
        except OSError as e:
            raise SshcryptError(f"agent connection failed: {e}") from None
        (n,) = struct.unpack(">I", self._recv(4))
        if not 1 <= n <= MAX_REPLY:
            raise SshcryptError(f"agent sent a reply of {n} bytes")
        body = self._recv(n)
        return body[0], _Reader(body[1:], "agent reply")

    def identities(self) -> list[Key]:
        kind, r = self._request(bytes([SSH_AGENTC_REQUEST_IDENTITIES]))
        if kind != SSH_AGENT_IDENTITIES_ANSWER:
            raise SshcryptError("agent refused to list its keys")
        count = r.uint32()
        keys = []
        for _ in range(count):
            blob, comment = r.string(), r.string()
            keys.append(Key(blob, comment.decode("utf-8", "replace")))
        return keys

    def sign(self, key: Key, data: bytes) -> bytes:
        """The agent's signature over ``data``, verified."""
        if not key.usable:
            raise SshcryptError(
                f"{key.fingerprint}: a {key.type} key signs differently every"
                " time, so the secret could never be decrypted; use an RSA or"
                " Ed25519 key"
            )
        flags = SSH_AGENT_RSA_SHA2_512 if key.type == "ssh-rsa" else 0
        kind, r = self._request(
            bytes([SSH_AGENTC_SIGN_REQUEST])
            + _string(key.blob)
            + _string(data)
            + struct.pack(">I", flags)
        )
        if kind != SSH_AGENT_SIGN_RESPONSE:
            raise SshcryptError(
                f"agent refused to sign with {key.fingerprint}"
                " (was a confirmation denied?)"
            )
        sig = r.string()
        verify(key, data, sig)
        return sig


def find_key(keys: list[Key], want: str) -> Key:
    """The key whose fingerprint is ``want`` (with or without ``SHA256:``)."""
    want = want.removeprefix("SHA256:")
    for key in keys:
        if key.fingerprint.removeprefix("SHA256:") == want:
            return key
    raise SshcryptError(f"no key SHA256:{want} in the agent")


def _derive(sig: bytes, salt: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=INFO).derive(sig)


def encrypt(agent: Agent, key: Key, data: bytes) -> bytes:
    """Encrypt ``data`` to ``key``. The agent signs twice and the key is
    refused if the two signatures differ, before anything is encrypted to
    it."""
    salt, nonce = os.urandom(SALT_LEN), os.urandom(NONCE_LEN)
    sig = agent.sign(key, MAGIC + salt)
    if agent.sign(key, MAGIC + salt) != sig:
        raise SshcryptError(
            f"{key.fingerprint}: the agent signs differently every time, so"
            " the secret could never be decrypted"
        )
    ct = AESGCM(_derive(sig, salt)).encrypt(nonce, data, None)
    return MAGIC + hashlib.sha256(key.blob).digest() + salt + nonce + ct


def key_id(blob: bytes) -> str:
    """The fingerprint of the key a ciphertext was encrypted to."""
    _check_header(blob)
    return _fingerprint_of(blob[len(MAGIC) : len(MAGIC) + KEY_ID_LEN])


def _check_header(blob: bytes) -> None:
    if not blob.startswith(MAGIC):
        raise SshcryptError("not an sshcrypt secret (bad magic)")
    if len(blob) < HEADER_LEN + TAG_LEN:
        raise SshcryptError("sshcrypt secret is truncated")


def decrypt(agent: Agent, blob: bytes) -> bytes:
    _check_header(blob)
    pos = len(MAGIC)
    want = blob[pos : pos + KEY_ID_LEN]
    pos += KEY_ID_LEN
    salt = blob[pos : pos + SALT_LEN]
    pos += SALT_LEN
    nonce = blob[pos : pos + NONCE_LEN]
    ct = blob[pos + NONCE_LEN :]
    for key in agent.identities():
        if hashlib.sha256(key.blob).digest() == want:
            break
    else:
        raise SshcryptError(
            f"the key {_fingerprint_of(want)} this secret was encrypted to"
            " is not in the agent"
        )
    sig = agent.sign(key, MAGIC + salt)
    try:
        return AESGCM(_derive(sig, salt)).decrypt(nonce, ct, None)
    except InvalidTag:
        raise SshcryptError(
            "decryption failed: the secret is corrupt or was tampered with"
        ) from None


def encode(blob: bytes) -> str:
    """The configuration form, ``sshcrypt:<base64>``."""
    return PREFIX + base64.b64encode(blob).decode("ascii")


def decode(text: str) -> bytes:
    if not text.startswith(PREFIX):
        raise SshcryptError(f"does not start with {PREFIX!r}")
    try:
        blob = base64.b64decode(text.removeprefix(PREFIX).strip(), validate=True)
    except binascii.Error:
        raise SshcryptError("not valid base64") from None
    _check_header(blob)
    return blob
