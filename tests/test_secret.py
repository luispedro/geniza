"""Secret resolution and ``geniza secret keys|encrypt|check``."""

import io
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from agents import Entry, FakeAgent, fake_agent
from cryptography.hazmat.primitives.asymmetric import ec, ed25519

from geniza import cli, sshcrypt
from geniza.config import ConfigError, Secret, resolve_secret


@pytest.fixture
def agent(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeAgent]:
    """A fake agent, made the process's agent for the test (the inherited
    SSH_AUTH_SOCK is restored afterwards and never used)."""
    entries = [
        Entry(ed25519.Ed25519PrivateKey.generate(), "first"),
        Entry(ed25519.Ed25519PrivateKey.generate(), "second"),
        Entry(ec.generate_private_key(ec.SECP256R1()), "ecdsa"),
    ]
    with fake_agent(entries) as fake:
        monkeypatch.setenv("SSH_AUTH_SOCK", str(fake.path))
        yield fake


def keys_of(fake: FakeAgent) -> list[sshcrypt.Key]:
    with sshcrypt.Agent(str(fake.path)) as a:
        return a.identities()


def encrypted(fake: FakeAgent, value: bytes, index: int = 0) -> str:
    with sshcrypt.Agent(str(fake.path)) as a:
        return sshcrypt.encode(sshcrypt.encrypt(a, a.identities()[index], value))


def stdin(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(text))


# --- resolve_secret ----------------------------------------------------------


def test_resolve_sshcrypt(agent: FakeAgent) -> None:
    assert resolve_secret(Secret("sshcrypt", encrypted(agent, b"tok")), "t") == "tok"


def test_resolve_sshcrypt_errors(
    agent: FakeAgent, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = encrypted(agent, b"tok")
    agent.mode = "refuse"
    with pytest.raises(ConfigError, match=r"^t: agent refused to sign"):
        resolve_secret(Secret("sshcrypt", value), "t")
    monkeypatch.delenv("SSH_AUTH_SOCK")
    with pytest.raises(ConfigError, match="SSH_AUTH_SOCK is not set"):
        resolve_secret(Secret("sshcrypt", value), "t")
    with pytest.raises(ConfigError, match="bad magic"):
        resolve_secret(Secret("sshcrypt", "sshcrypt:AAAA"), "t")


@pytest.mark.parametrize(
    ("command", "value"),
    [
        ("printf 'tok\\n'", "tok"),
        ("printf tok", "tok"),
        ("printf 'tok\\n\\n'", "tok\n"),
        ("printf ' t o k '", " t o k "),
        ("printf 'caf\\303\\251'", "caf" + chr(0xE9)),
    ],
)
def test_resolve_command(command: str, value: str) -> None:
    assert resolve_secret(Secret("command", command), "t") == value


@pytest.mark.parametrize(
    ("command", "message"),
    [
        ("printf hidden; exit 3", "exited with status 3"),
        ("printf '\\n'", "empty"),
        ("printf 'hidden\\377'", "not valid UTF-8"),
    ],
)
def test_resolve_command_errors(command: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message) as e:
        resolve_secret(Secret("command", command), "t")
    assert "hidden" not in str(e.value)


# --- the commands ------------------------------------------------------------


def test_keys(agent: FakeAgent, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["secret", "keys"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 3
    assert lines[0].endswith("first  [usable]")
    assert "not usable" in lines[2]
    assert cli.main(["secret", "keys", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [k["usable"] for k in data] == [True, True, False]
    assert data[0]["fingerprint"] == keys_of(agent)[0].fingerprint


def test_keys_without_agent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    assert cli.main(["secret", "keys"]) == 1
    assert "SSH_AUTH_SOCK is not set" in capsys.readouterr().err


def test_encrypt(
    agent: FakeAgent,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    keys = keys_of(agent)
    stdin(monkeypatch, "hunter2\n")
    assert cli.main(["secret", "encrypt"]) == 1
    err = capsys.readouterr().err
    assert "several usable keys" in err and keys[1].fingerprint in err

    stdin(monkeypatch, "hunter2\n")
    assert cli.main(["secret", "encrypt", "--key", keys[1].fingerprint]) == 0
    out = capsys.readouterr().out.strip()
    blob = sshcrypt.decode(out)
    assert sshcrypt.key_id(blob) == keys[1].fingerprint
    with sshcrypt.Agent(str(agent.path)) as a:
        assert sshcrypt.decrypt(a, blob) == b"hunter2"

    stdin(monkeypatch, "x")
    assert cli.main(["secret", "encrypt", "--key", keys[0].fingerprint, "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["key"] == keys[0].fingerprint
    assert data["secret"].startswith("sshcrypt:")


@pytest.mark.parametrize(
    ("argv", "text", "message"),
    [
        (["--key", "SHA256:nope"], "x", "no key SHA256:nope"),
        (["--key", "{ecdsa}"], "x", "signs differently every time"),
        (["--key", "{first}"], "\n", "empty secret"),
    ],
)
def test_encrypt_errors(
    agent: FakeAgent,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    text: str,
    message: str,
) -> None:
    fps = {k.comment: k.fingerprint for k in keys_of(agent)}
    stdin(monkeypatch, text)
    argv = [fps[a[1:-1]] if a.startswith("{") else a for a in argv]
    assert cli.main(["secret", "encrypt", *argv]) == 1
    captured = capsys.readouterr()
    assert message in captured.err
    assert captured.out == ""


def test_encrypt_refuses_randomising_agent(
    agent: FakeAgent,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fp = keys_of(agent)[0].fingerprint
    agent.mode = "random"
    stdin(monkeypatch, "x")
    assert cli.main(["secret", "encrypt", "--key", fp]) == 1
    assert "signs differently every time" in capsys.readouterr().err


def test_check(
    agent: FakeAgent, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    good = encrypted(agent, b"hidden-1")
    blob = sshcrypt.decode(encrypted(agent, b"hidden-2"))
    lost = sshcrypt.encode(blob[:10] + bytes(32) + blob[42:])
    config = tmp_path / "config.toml"
    store = tmp_path / "store"
    accounts = {
        "a-good": f'token = "{good}"',
        "b-lost": f'token = "{lost}"',
        "c-cmd": "token_command = \"printf 'hidden-3\\\\n'\"",
        "d-fail": 'token_command = "printf hidden-4; exit 3"',
    }
    config.write_text(
        f'[stores.s]\npath = "{store}"\ncache = "{tmp_path / "cache"}"\n'
        + "".join(
            f'[accounts.{n}]\nstore = "s"\ntype = "jmap"\nurl = "https://h/jmap"\n{t}\n'
            for n, t in accounts.items()
        )
    )
    assert cli.main(["--config", str(config), "secret", "check"]) == 1
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert lines[0] == "a-good: OK"
    assert lines[1].startswith("b-lost: token: the key SHA256:")
    assert lines[2] == "c-cmd: OK"
    assert lines[3] == "d-fail: token: command exited with status 3"
    assert "hidden" not in captured.out + captured.err

    assert cli.main(["--config", str(config), "secret", "check", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert [data[n]["ok"] for n in accounts] == [True, False, True, False]
    assert not store.exists()
