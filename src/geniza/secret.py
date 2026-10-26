"""``geniza secret keys|encrypt|check`` (DESIGN.md, "Secrets").

``encrypt`` reads the secret from the terminal with ``getpass``, or from
stdin (minus a trailing newline) when stdin is not a terminal, never from
arguments. Nothing here prints or logs a secret: ``check`` reports only
whether each one could be obtained.
"""

import argparse
import getpass
import logging
import sys
from typing import TYPE_CHECKING

from geniza import sshcrypt
from geniza.config import ConfigError, load, resolve_secret
from geniza.sshcrypt import Agent, Key, SshcryptError

if TYPE_CHECKING:
    from geniza.cli import Output

log = logging.getLogger(__name__)


def keys(args: argparse.Namespace, out: Output) -> int:
    try:
        with Agent() as agent:
            found = agent.identities()
    except SshcryptError as e:
        log.error("%s", e)
        return 1
    lines = [
        f"{k.fingerprint}  {k.type:<12} {k.comment}"
        f"  [{'usable' if k.usable else 'not usable: signatures differ every time'}]"
        for k in found
    ]
    if not found:
        lines = ["no keys in the agent (add one with ssh-add)"]
    out.result(
        "\n".join(lines),
        [
            {
                "fingerprint": k.fingerprint,
                "type": k.type,
                "comment": k.comment,
                "usable": k.usable,
            }
            for k in found
        ],
    )
    return 0


def _choose(found: list[Key], want: str | None) -> Key:
    """``--key``, else the only usable key."""
    if want is not None:
        key = sshcrypt.find_key(found, want)
        if not key.usable:
            raise SshcryptError(
                f"{key.fingerprint}: a {key.type} key signs differently every"
                " time; use an RSA or Ed25519 key"
            )
        return key
    usable = [k for k in found if k.usable]
    if not usable:
        raise SshcryptError("no RSA or Ed25519 key in the agent (see ssh-add)")
    if len(usable) > 1:
        raise SshcryptError(
            "several usable keys in the agent; choose one with --key: "
            + ", ".join(k.fingerprint for k in usable)
        )
    return usable[0]


def _read_secret() -> bytes:
    if sys.stdin.isatty():
        text = getpass.getpass("secret: ")
    else:
        text = sys.stdin.read().removesuffix("\n")
    return text.encode("utf-8")


def encrypt(args: argparse.Namespace, out: Output) -> int:
    try:
        with Agent() as agent:
            key = _choose(agent.identities(), args.key)
            data = _read_secret()
            if not data:
                log.error("empty secret: nothing encrypted")
                return 1
            log.info("encrypting to %s (%s)", key.fingerprint, key.comment)
            text = sshcrypt.encode(sshcrypt.encrypt(agent, key, data))
    except SshcryptError as e:
        log.error("%s", e)
        return 1
    out.result(text, {"secret": text, "key": key.fingerprint})
    return 0


def check(args: argparse.Namespace, out: Output) -> int:
    config = load(args.config)
    accounts = sorted(config.accounts.values(), key=lambda a: a.name)
    if args.store is not None:
        accounts = [a for a in accounts if a.store == config.store(args.store).name]
    results: dict[str, str | None] = {}
    for account in accounts:
        try:
            resolve_secret(account.token, "token")
            results[account.name] = None
        except ConfigError as e:
            results[account.name] = str(e)
    lines = [f"{name}: {err or 'OK'}" for name, err in results.items()]
    out.result(
        "\n".join(lines) or "no accounts configured",
        {name: {"ok": err is None, "error": err} for name, err in results.items()},
    )
    return 1 if any(results.values()) else 0
