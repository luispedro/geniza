"""sshcrypt: the agent protocol, its failure modes, and the wire format."""

from collections.abc import Iterator

import pytest
from agents import FIXTURES, Entry, FakeAgent, fake_agent, fixture_key, real_agent
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from geniza import sshcrypt
from geniza.sshcrypt import Agent, SshcryptError

PLAINTEXT = (FIXTURES / "plaintext.txt").read_bytes()
FIXTURE_FILES = {
    "id_ed25519": "plaintext.ed25519.sshcrypt",
    "id_rsa": "plaintext.rsa.sshcrypt",
}


@pytest.fixture(scope="module")
def keys() -> list[Entry]:
    return [
        Entry(ed25519.Ed25519PrivateKey.generate(), "test-ed25519"),
        Entry(rsa.generate_private_key(public_exponent=65537, key_size=2048), "rsa"),
        Entry(ec.generate_private_key(ec.SECP256R1()), "test-ecdsa"),
    ]


@pytest.fixture(scope="module")
def module_agent(keys: list[Entry]) -> Iterator[FakeAgent]:
    with fake_agent(keys) as agent:
        yield agent


@pytest.fixture
def agent(module_agent: FakeAgent) -> Iterator[FakeAgent]:
    module_agent.mode = "ok"
    module_agent.signs = 0
    yield module_agent
    module_agent.mode = "ok"


def connect(fake: FakeAgent) -> Agent:
    return Agent(str(fake.path))


def test_keys(agent: FakeAgent) -> None:
    with connect(agent) as a:
        keys = a.identities()
    assert [(k.type, k.comment, k.usable) for k in keys] == [
        ("ssh-ed25519", "test-ed25519", True),
        ("ssh-rsa", "rsa", True),
        ("ecdsa-sha2-nistp256", "test-ecdsa", False),
    ]
    assert all(k.fingerprint.startswith("SHA256:") for k in keys)
    assert sshcrypt.find_key(keys, keys[1].fingerprint) == keys[1]
    assert (
        sshcrypt.find_key(keys, keys[1].fingerprint.removeprefix("SHA256:"))
        == (keys[1])
    )
    with pytest.raises(SshcryptError, match="no key"):
        sshcrypt.find_key(keys, "SHA256:nope")


@pytest.mark.parametrize("index", [0, 1])
def test_round_trip(agent: FakeAgent, index: int) -> None:
    with connect(agent) as a:
        key = a.identities()[index]
        blob = sshcrypt.encrypt(a, key, b"hunter2")
        assert agent.signs == 2
        assert sshcrypt.key_id(blob) == key.fingerprint
        text = sshcrypt.encode(blob)
        assert text.startswith("sshcrypt:")
        assert sshcrypt.decrypt(a, sshcrypt.decode(text)) == b"hunter2"


def test_unusable_key_is_refused(agent: FakeAgent) -> None:
    with connect(agent) as a:
        key = a.identities()[2]
        with pytest.raises(SshcryptError, match="signs differently every time"):
            sshcrypt.encrypt(a, key, b"x")
    assert agent.signs == 0


def test_randomising_signer_is_refused(agent: FakeAgent) -> None:
    """Signatures that verify but differ are caught before encrypting."""
    agent.mode = "random"
    with connect(agent) as a:
        key = a.identities()[0]
        sig = a.sign(key, b"data")
        assert a.sign(key, b"data") != sig
        with pytest.raises(SshcryptError, match="signs differently every time"):
            sshcrypt.encrypt(a, key, b"x")


@pytest.mark.parametrize(
    ("mode", "index", "message"),
    [
        ("refuse", 0, "refused to sign"),
        ("wrong-key", 0, "invalid signature"),
        ("garbage", 0, "invalid signature"),
        ("garbage", 1, "expected 'rsa-sha2-512'"),
        ("wrong-alg", 1, "expected 'rsa-sha2-512'"),
        ("trailing", 0, "trailing data"),
        ("truncate", 0, "closed the connection"),
    ],
)
def test_bad_signatures(agent: FakeAgent, mode: str, index: int, message: str) -> None:
    with connect(agent) as a:
        key = a.identities()[index]
    agent.mode = mode
    with connect(agent) as a, pytest.raises(SshcryptError, match=message):
        sshcrypt.encrypt(a, key, b"x")


def test_decrypt_failures(agent: FakeAgent) -> None:
    with connect(agent) as a:
        blob = sshcrypt.encrypt(a, a.identities()[0], b"secret")
        tampered = blob[:-1] + bytes([blob[-1] ^ 1])
        with pytest.raises(SshcryptError, match="tampered"):
            sshcrypt.decrypt(a, tampered)
        other = blob[:10] + bytes(32) + blob[42:]
        with pytest.raises(SshcryptError, match="is not in the agent"):
            sshcrypt.decrypt(a, other)
        with pytest.raises(SshcryptError, match="bad magic"):
            sshcrypt.decrypt(a, b"SSHCRYPT1\0" + blob[10:])
        with pytest.raises(SshcryptError, match="truncated"):
            sshcrypt.decrypt(a, blob[:85])
    agent.mode = "refuse-list"
    with connect(agent) as a, pytest.raises(SshcryptError, match="refused to list"):
        sshcrypt.decrypt(a, blob)


@pytest.mark.parametrize(
    "text", ["sshcrypt", "sshcrypt:!!!", "sshcrypt:U1NIQ1JZUFQy", "SSHCRYPT:abc"]
)
def test_decode_rejects(text: str) -> None:
    with pytest.raises(SshcryptError):
        sshcrypt.decode(text)


def test_no_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    with pytest.raises(SshcryptError, match="not set"):
        Agent()
    with pytest.raises(SshcryptError, match="cannot connect"):
        Agent("/nonexistent/agent.sock")


@settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(data=st.binary(max_size=200), index=st.sampled_from([0, 1]))
def test_round_trip_property(agent: FakeAgent, data: bytes, index: int) -> None:
    with connect(agent) as a:
        key = a.identities()[index]
        blob = sshcrypt.encrypt(a, key, data)
        assert sshcrypt.decrypt(a, sshcrypt.decode(sshcrypt.encode(blob))) == data


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(cut=st.integers(0, 200), flip=st.integers(0, 200))
def test_damaged_ciphertexts(agent: FakeAgent, cut: int, flip: int) -> None:
    """Truncated or altered ciphertexts fail with SshcryptError, never
    another exception and never a wrong plaintext."""
    with connect(agent) as a:
        blob = sshcrypt.encrypt(a, a.identities()[0], b"secret value")
        damaged = bytearray(blob[: len(blob) - cut % (len(blob) + 1)])
        if damaged:
            damaged[flip % len(damaged)] ^= 0x40
        with pytest.raises(SshcryptError):
            sshcrypt.decrypt(a, bytes(damaged))


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(reply=st.binary(max_size=64), sign=st.booleans())
def test_arbitrary_agent_replies(agent: FakeAgent, reply: bytes, sign: bool) -> None:
    """Whatever the agent sends back, the only failure is SshcryptError."""
    with connect(agent) as a:
        key = a.identities()[0]
    agent.mode, agent.raw_reply = "raw", reply
    try:
        with connect(agent) as a:
            if sign:
                a.sign(key, b"data")
            else:
                a.identities()
    except SshcryptError:
        pass
    finally:
        agent.mode = "ok"


@pytest.mark.parametrize("name", sorted(FIXTURE_FILES))
def test_fixture_decrypts_with_fake_agent(name: str) -> None:
    """Ciphertexts made by the original sshcrypt.py still decrypt."""
    blob = (FIXTURES / FIXTURE_FILES[name]).read_bytes()
    with fake_agent([Entry(fixture_key(name), name)]) as agent, connect(agent) as a:
        assert sshcrypt.decrypt(a, blob) == PLAINTEXT


def test_real_agent() -> None:
    """End to end against a private ssh-agent: the fixtures decrypt, and a
    secret encrypted with each key decrypts again."""
    with real_agent() as agent:
        for name in FIXTURE_FILES:
            agent.add(FIXTURES / name)
        with Agent(str(agent.sock)) as a:
            keys = a.identities()
            assert sorted(k.type for k in keys) == ["ssh-ed25519", "ssh-rsa"]
            for file in FIXTURE_FILES.values():
                assert sshcrypt.decrypt(a, (FIXTURES / file).read_bytes()) == PLAINTEXT
            for key in keys:
                blob = sshcrypt.encrypt(a, key, b"token")
                assert sshcrypt.decrypt(a, blob) == b"token"
