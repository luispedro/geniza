"""Configuration, ``store.toml`` and the cache directory
(DESIGN.md, "Configuration", "On-disk layout").

``load`` reads and checks the configuration file and returns plain
dataclasses; nothing else in geniza looks at the TOML. Secrets are kept as
the configured ``sshcrypt:`` value or command; ``resolve_secret`` turns one
into the secret when it is needed (DESIGN.md, "Secrets").

``init_store`` writes a new store's ``store.toml`` (once, under the store
lock) or checks an existing one; ``open_cache`` makes sure a cache
directory belongs to the store it is used for.
"""

import ipaddress
import logging
import os
import re
import secrets
import subprocess
import tomllib
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from geniza.mime.recipes import Policy

log = logging.getLogger(__name__)

#: On-disk format version of a store, recorded in ``store.toml``.
FORMAT_VERSION = 1

STORE_FILE = "store.toml"
INITIALIZED_FILE = "initialized"
CACHEDIR_TAG = "CACHEDIR.TAG"
CACHE_ID_FILE = "store-id"
#: Files in a cache that are built from one store; removed when the cache
#: turns out to belong to another.
CACHE_FILES = (
    "index.sqlite",
    "index.sqlite-wal",
    "index.sqlite-shm",
    "index.sqlite-journal",
)

_CACHEDIR_SIGNATURE = "Signature: 8a477f597d28d172789f06886806bc55\n"
_CACHEDIR_TAG_TEXT = (
    _CACHEDIR_SIGNATURE
    + "# This file is a cache directory tag created by geniza.\n"
    + "# For information about cache directory tags, see https://bford.info/cachedir/\n"
)

_NAME = re.compile(r"[a-z0-9_-]+")
_SIZE = re.compile(r"(\d+)\s*(B|KiB|MiB|GiB)?")
_SIZE_UNITS = {None: 1, "B": 1, "KiB": 1 << 10, "MiB": 1 << 20, "GiB": 1 << 30}
_DURATION = re.compile(r"(?:\d+[smhdw])+")
_DURATION_PART = re.compile(r"(\d+)([smhdw])")
_DURATION_UNITS = {
    "s": timedelta(seconds=1),
    "m": timedelta(minutes=1),
    "h": timedelta(hours=1),
    "d": timedelta(days=1),
    "w": timedelta(weeks=1),
}
#: Temp files of ``_write_new`` in a store's root.
_TEMP_NAME = re.compile(r"\.(?:store\.toml|initialized)\.[0-9a-f]{16}\.tmp")


class ConfigError(Exception):
    """The configuration, a ``store.toml`` or a cache directory is not
    usable. Messages name the file and key, never a secret value."""


@dataclass(frozen=True, kw_only=True)
class IncomingSettings:
    mailboxes: tuple[str, ...] = ("INBOX",)
    max_age: timedelta = timedelta(days=7)
    on_delete: Literal["drop", "keep"] = "drop"
    grace: timedelta = timedelta(days=30)
    max_drop_fraction: float = 0.5
    max_drop_count: int = 200


@dataclass(frozen=True)
class Secret:
    """How to obtain a secret: an ``sshcrypt:`` value, or a shell command
    whose stdout is the secret. Not the secret itself."""

    kind: Literal["sshcrypt", "command"]
    value: str = field(repr=False)


@dataclass(frozen=True, kw_only=True)
class Store:
    name: str
    path: Path
    cache: Path
    attachments: Policy
    incoming: IncomingSettings


@dataclass(frozen=True, kw_only=True)
class Account:
    name: str
    store: str
    type: Literal["jmap"]
    url: str
    token: Secret
    exclude: tuple[str, ...] = ()
    no_stable_id_ok: bool = False


@dataclass(frozen=True, kw_only=True)
class Config:
    path: Path
    stores: dict[str, Store]
    accounts: dict[str, Account]
    default_store: str | None = None

    def store(self, name: str | None = None) -> Store:
        """The store called ``name`` (``--store``), else ``default_store``,
        else the only store."""
        if name is None:
            name = self.default_store
        if name is None:
            if len(self.stores) == 1:
                return next(iter(self.stores.values()))
            if not self.stores:
                raise ConfigError(f"{self.path}: no stores configured")
            raise ConfigError(
                f"{self.path}: several stores and no default_store; use --store"
            )
        try:
            return self.stores[name]
        except KeyError:
            raise ConfigError(f"{self.path}: no store {name!r}") from None

    def accounts_of(self, store: str) -> list[Account]:
        return [a for a in self.accounts.values() if a.store == store]


@dataclass(frozen=True)
class StoreInfo:
    """The contents of ``store.toml``."""

    id: uuid.UUID
    format: int
    created: datetime


# --- paths -------------------------------------------------------------------


def _home(env: Mapping[str, str]) -> Path:
    home = env.get("HOME")
    return Path(home) if home else Path.home()


def _xdg(env: Mapping[str, str], var: str, fallback: str) -> Path:
    """An XDG base directory; relative values are ignored, as the XDG
    specification requires."""
    value = env.get(var)
    if value and Path(value).is_absolute():
        return Path(value)
    return _home(env) / fallback


def default_config_path(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    return _xdg(env, "XDG_CONFIG_HOME", ".config") / "geniza" / "config.toml"


def default_store_path(name: str, env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    return _xdg(env, "XDG_DATA_HOME", ".local/share") / "geniza" / name


def default_cache_path(name: str, env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    return _xdg(env, "XDG_CACHE_HOME", ".cache") / "geniza" / name


def _nested(a: Path, b: Path) -> bool:
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


# --- values ------------------------------------------------------------------


class _Table:
    """A TOML table being read: typed getters that name the key on error,
    and a check that every key was used."""

    def __init__(self, file: Path, where: str, data: Any) -> None:
        self.file = file
        self.where = where
        if not isinstance(data, dict):
            raise self.error(None, "must be a table")
        self.data: dict[str, Any] = data
        self.used: set[str] = set()

    def key(self, key: str | None) -> str:
        if key is None:
            return self.where or "top level"
        return f"{self.where}.{key}" if self.where else key

    def error(self, key: str | None, message: str) -> ConfigError:
        return ConfigError(f"{self.file}: {self.key(key)}: {message}")

    def has(self, key: str) -> bool:
        return key in self.data

    def raw(self, key: str) -> Any:
        self.used.add(key)
        return self.data.get(key)

    def table(self, key: str) -> _Table:
        value = self.raw(key)
        return _Table(self.file, self.key(key), {} if value is None else value)

    def string(self, key: str, default: str | None = None) -> str | None:
        value = self.raw(key)
        if value is None:
            return default
        if not isinstance(value, str) or not value:
            raise self.error(key, "must be a non-empty string")
        return value

    def required_string(self, key: str) -> str:
        value = self.string(key)
        if value is None:
            raise self.error(key, "missing")
        return value

    def boolean(self, key: str, default: bool) -> bool:
        value = self.raw(key)
        if value is None:
            return default
        if not isinstance(value, bool):
            raise self.error(key, "must be true or false")
        return value

    def integer(self, key: str, default: int) -> int:
        value = self.raw(key)
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise self.error(key, "must be a non-negative integer")
        return value

    def fraction(self, key: str, default: float) -> float:
        value = self.raw(key)
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise self.error(key, "must be a number between 0 and 1")
        if not 0 <= value <= 1:
            raise self.error(key, "must be a number between 0 and 1")
        return float(value)

    def str_list(self, key: str, default: tuple[str, ...]) -> tuple[str, ...]:
        value = self.raw(key)
        if value is None:
            return default
        if not isinstance(value, list) or not all(
            isinstance(v, str) and v for v in value
        ):
            raise self.error(key, "must be a list of non-empty strings")
        return tuple(value)

    def size(self, key: str, default: int) -> int:
        value = self.raw(key)
        if value is None:
            return default
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        if isinstance(value, str) and (m := _SIZE.fullmatch(value.strip())):
            return int(m[1]) * _SIZE_UNITS[m[2]]
        raise self.error(
            key, f"bad size {value!r} (an integer, or e.g. '64KiB', '2MiB')"
        )

    def duration(self, key: str, default: timedelta) -> timedelta:
        value = self.raw(key)
        if value is None:
            return default
        if isinstance(value, str) and _DURATION.fullmatch(value.strip()):
            return sum(
                (int(n) * _DURATION_UNITS[u] for n, u in _DURATION_PART.findall(value)),
                timedelta(),
            )
        raise self.error(key, f"bad duration {value!r} (e.g. '7d', '12h', '1w2d')")

    def path(self, key: str, env: Mapping[str, str]) -> Path | None:
        value = self.string(key)
        if value is None:
            return None
        if value == "~" or value.startswith("~/"):
            value = str(_home(env)) + value[1:]
        path = Path(value)
        if not path.is_absolute():
            raise self.error(key, "must be an absolute path (or start with ~/)")
        return path

    def finish(self) -> None:
        unknown = sorted(set(self.data) - self.used)
        if unknown:
            raise self.error(unknown[0], "unknown key")


def _secret(t: _Table, field_name: str) -> Secret:
    """``<field>`` (an ``sshcrypt:`` value) or ``<field>_command``; exactly
    one. Errors never include the value."""
    command_key = f"{field_name}_command"
    have = [k for k in (field_name, command_key) if t.has(k)]
    if not have:
        raise t.error(field_name, f"missing (give {field_name} or {command_key})")
    if len(have) == 2:
        raise t.error(field_name, f"give either {field_name} or {command_key}")
    value = t.raw(have[0])
    if not isinstance(value, str) or not value:
        raise t.error(have[0], "must be a non-empty string")
    if have[0] == command_key:
        return Secret("command", value)
    if not value.startswith("sshcrypt:") or value == "sshcrypt:":
        raise t.error(
            field_name,
            "plain-text secrets are not accepted; encrypt it with "
            f"`geniza secret encrypt`, or use {command_key}",
        )
    return Secret("sshcrypt", value)


def _url(t: _Table, key: str) -> str:
    """An https URL, or http to a loopback host (for local test servers);
    the token must not travel in clear over a network."""
    url = t.required_string(key)
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        raise t.error(key, f"bad URL {url!r}") from None
    if not host:
        raise t.error(key, f"bad URL {url!r}")
    if parts.scheme == "https":
        return url
    if parts.scheme == "http":
        try:
            loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
        if loopback:
            return url
    raise t.error(key, f"must be an https URL, not {url!r}")


def _name(t: _Table, name: str, what: str) -> str:
    if not _NAME.fullmatch(name):
        raise t.error(name, f"{what} names may only use a-z, 0-9, '_' and '-'")
    return name


def _attachments(t: _Table, base: Policy) -> Policy:
    policy = Policy(
        min_size=t.size("min_size", base.min_size),
        decoration_min_size=t.size("decoration_min_size", base.decoration_min_size),
    )
    t.finish()
    return policy


def _incoming(t: _Table, base: IncomingSettings) -> IncomingSettings:
    on_delete: Literal["drop", "keep"]
    match t.string("on_delete"):
        case None:
            on_delete = base.on_delete
        case "drop" | "keep" as value:
            on_delete = value
        case _:
            raise t.error("on_delete", "must be 'drop' or 'keep'")
    settings = replace(
        base,
        mailboxes=t.str_list("mailboxes", base.mailboxes),
        max_age=t.duration("max_age", base.max_age),
        on_delete=on_delete,
        grace=t.duration("grace", base.grace),
        max_drop_fraction=t.fraction("max_drop_fraction", base.max_drop_fraction),
        max_drop_count=t.integer("max_drop_count", base.max_drop_count),
    )
    t.finish()
    return settings


def _account(t: _Table, name: str, stores: Mapping[str, Store]) -> Account:
    store = t.required_string("store")
    if store not in stores:
        raise t.error("store", f"no store {store!r}")
    type_ = t.required_string("type")
    if type_ == "imap":
        raise t.error("type", "IMAP accounts are not supported yet")
    if type_ != "jmap":
        raise t.error("type", "must be 'jmap'")
    url = _url(t, "url")
    token = _secret(t, "token")
    sync = t.table("sync")
    exclude = sync.str_list("exclude", ())
    sync.finish()
    no_stable_id_ok = t.boolean("no_stable_id_ok", False)
    t.finish()
    return Account(
        name=name,
        store=store,
        type="jmap",
        url=url,
        token=token,
        exclude=exclude,
        no_stable_id_ok=no_stable_id_ok,
    )


def parse(data: Mapping[str, Any], path: Path, env: Mapping[str, str]) -> Config:
    """Check a parsed configuration and build a ``Config``. ``path`` is
    only used in error messages."""
    top = _Table(path, "", dict(data))
    attachments = _attachments(top.table("attachments"), Policy())
    incoming = _incoming(top.table("incoming"), IncomingSettings())

    stores: dict[str, Store] = {}
    stores_table = top.table("stores")
    for name in list(stores_table.data):
        _name(stores_table, name, "store")
        st = stores_table.table(name)
        store = Store(
            name=name,
            path=st.path("path", env) or default_store_path(name, env),
            cache=st.path("cache", env) or default_cache_path(name, env),
            attachments=_attachments(st.table("attachments"), attachments),
            incoming=_incoming(st.table("incoming"), incoming),
        )
        st.finish()
        if _nested(store.path, store.cache):
            raise st.error(None, "path and cache must not contain each other")
        for other in stores.values():
            for a in (store.path, store.cache):
                for b in (other.path, other.cache):
                    if _nested(a, b):
                        raise st.error(
                            None, f"{a} overlaps with {b} of store {other.name!r}"
                        )
        stores[name] = store
    stores_table.finish()

    accounts: dict[str, Account] = {}
    accounts_table = top.table("accounts")
    for name in list(accounts_table.data):
        _name(accounts_table, name, "account")
        accounts[name] = _account(accounts_table.table(name), name, stores)
    accounts_table.finish()

    default = top.string("default_store")
    if default is not None and default not in stores:
        raise top.error("default_store", f"no store {default!r}")
    top.finish()
    return Config(path=path, stores=stores, accounts=accounts, default_store=default)


def load(path: Path | None = None, env: Mapping[str, str] | None = None) -> Config:
    """Read the configuration (``--config``, else the XDG default)."""
    env = os.environ if env is None else env
    path = default_config_path(env) if path is None else Path(path)
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"{path}: no configuration file") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from None
    return parse(data, path, env)


def resolve_secret(secret: Secret, where: str) -> str:
    """The secret itself: an ``sshcrypt:`` value decrypted with the agent
    (``SSH_AUTH_SOCK``), or the stdout of a ``*_command`` run through the
    shell, minus a trailing newline. ``where`` names it in errors, which
    never include the secret or the command's output."""
    from geniza import sshcrypt

    if secret.kind == "sshcrypt":
        try:
            blob = sshcrypt.decode(secret.value)
            with sshcrypt.Agent() as agent:
                data = sshcrypt.decrypt(agent, blob)
        except sshcrypt.SshcryptError as e:
            raise ConfigError(f"{where}: {e}") from None
    else:
        try:
            # stderr and stdin stay the terminal's, for pass, pinentry etc.
            done = subprocess.run(
                secret.value, shell=True, stdout=subprocess.PIPE, check=False
            )
        except OSError as e:
            raise ConfigError(f"{where}: command failed: {e.strerror}") from None
        if done.returncode != 0:
            raise ConfigError(f"{where}: command exited with status {done.returncode}")
        data = done.stdout.removesuffix(b"\n")
    try:
        value = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ConfigError(f"{where}: not valid UTF-8") from None
    if not value:
        raise ConfigError(f"{where}: empty")
    return value


# --- store.toml --------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_new(path: Path, text: str, *, replace_existing: bool) -> None:
    """Write ``path`` through a fsynced temp file. Without
    ``replace_existing``, an existing file is never overwritten
    (``FileExistsError``)."""
    tmp = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    try:
        with open(tmp, "x", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if replace_existing:
            os.rename(tmp, path)
        else:
            os.link(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    _fsync_dir(path.parent)


def read_store(path: Path) -> StoreInfo:
    """Read and check ``store.toml`` in the store at ``path``."""
    file = Path(path) / STORE_FILE
    try:
        with open(file, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"{path}: not a geniza store (no {STORE_FILE})") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{file}: {e}") from None
    t = _Table(file, "", data)
    version = t.raw("format")
    if isinstance(version, bool) or not isinstance(version, int):
        raise t.error("format", "must be an integer")
    if version > FORMAT_VERSION:
        raise t.error(
            "format",
            f"store format {version} is newer than this geniza supports "
            f"({FORMAT_VERSION})",
        )
    if version != FORMAT_VERSION:
        raise t.error("format", f"unknown store format {version}")
    try:
        store_id = uuid.UUID(t.required_string("id"))
    except ValueError:
        raise t.error("id", "must be a UUID") from None
    created = t.raw("created")
    if not isinstance(created, datetime) or created.tzinfo is None:
        raise t.error("created", "must be a date-time with a time zone")
    t.finish()
    return StoreInfo(id=store_id, format=version, created=created)


def init_store(path: Path, *, clock: Callable[[], datetime] = _now) -> StoreInfo:
    """Check the store's ``store.toml``, writing it first if the store is
    new. The caller holds the ``StoreLock`` (which creates the directory).
    Temp files left in the store's root by a crash are removed.

    A directory that has other files but no ``store.toml`` is refused: it
    is either some other directory or a store that lost its ``store.toml``,
    and giving it a new id would hide that.
    """
    path = Path(path)
    for p in path.iterdir():
        if _TEMP_NAME.fullmatch(p.name):
            log.warning("%s: removing a temp file left by a crash", p)
            p.unlink()
    if (path / STORE_FILE).exists():
        return read_store(path)
    if any(p.name != ".lock" for p in path.iterdir()):
        raise ConfigError(
            f"{path}: has files but no {STORE_FILE}; not creating a store here"
        )
    created: datetime = clock().astimezone(UTC).replace(microsecond=0)
    text = (
        "# geniza store; written once, do not edit.\n"
        f"format = {FORMAT_VERSION}\n"
        f'id = "{uuid.uuid4()}"\n'
        f"created = {created.strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
    )
    _write_new(path / STORE_FILE, text, replace_existing=False)
    return read_store(path)


def is_initialized(path: Path) -> bool:
    """Whether the store's initialization is over (DESIGN.md, "On-disk
    layout"): until then, records and attachment files go into the month
    their message was received in."""
    return (Path(path) / INITIALIZED_FILE).exists()


def mark_initialized(path: Path, *, clock: Callable[[], datetime] = _now) -> None:
    """End the store's initialization, if it is not over already. Written
    once, under the store lock, after a complete first sync."""
    file = Path(path) / INITIALIZED_FILE
    if file.exists():
        return
    when = clock().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    _write_new(file, f"{when}\n", replace_existing=False)


# --- cache -------------------------------------------------------------------


def open_cache(path: Path, store_id: uuid.UUID) -> bool:
    """Prepare the cache directory at ``path`` for the store ``store_id``.

    Creates it (private) with a ``CACHEDIR.TAG``. If the cache was built
    from another store (or its id is missing), the files built from a
    store are removed and the id is replaced; the caller then rebuilds
    the index. Returns whether that happened.

    A non-empty directory without a ``CACHEDIR.TAG`` is refused rather
    than tagged, so a mistyped cache path cannot make backups skip it.
    """
    path = Path(path)
    tag = path / CACHEDIR_TAG
    if path.exists() and not tag.exists() and any(path.iterdir()):
        raise ConfigError(
            f"{path}: not a geniza cache (no {CACHEDIR_TAG}) and not empty"
        )
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not tag.exists():
        _write_new(tag, _CACHEDIR_TAG_TEXT, replace_existing=True)
    elif not tag.read_text(encoding="utf-8", errors="replace").startswith(
        _CACHEDIR_SIGNATURE
    ):
        raise ConfigError(f"{tag}: not a cache directory tag")

    id_file = path / CACHE_ID_FILE
    try:
        if id_file.read_text(encoding="utf-8").strip() == str(store_id):
            return False
    except FileNotFoundError:
        pass
    for name in CACHE_FILES:
        (path / name).unlink(missing_ok=True)
    _write_new(id_file, f"{store_id}\n", replace_existing=True)
    return True
