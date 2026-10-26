"""``geniza sync``: what every source shares (DESIGN.md, "Sync").

Per store: take the lock, bring the index up to date (for the catalog of
stored messages), and sync each account. After a complete run of an
account, its ``incoming/`` is evaluated and its ``sync.json`` rewritten
with the new cursors and the messages the evaluation found absent; a run
that failed or could not store every message leaves both alone, so the
next run starts from the same cursors. When every account of the store
has completed a sync, the store's initialization ends.

``sync.json`` (DESIGN.md, "State: the journal") holds only what can be
lost: without it, the next run lists everything and downloads nothing
the journal already maps. A damaged one is ignored with a warning.
"""

import argparse
import json
import logging
import os
import re
import secrets
from collections.abc import Callable, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from geniza.config import (
    Account,
    Config,
    ConfigError,
    Store,
    init_store,
    is_initialized,
    load,
    mark_initialized,
    resolve_secret,
)
from geniza.index import reindex
from geniza.store import incoming, journal
from geniza.store.incoming import Applied, evaluate
from geniza.store.ingest import Ingester
from geniza.store.journal import MSG_PREFIX, JournalError, JournalWriter
from geniza.store.lock import StoreLock, StoreLocked
from geniza.sync.jmap import Client, JmapError
from geniza.sync.jmap_sync import JmapSync, Progress

if TYPE_CHECKING:
    from geniza.cli import Output

log = logging.getLogger(__name__)

SYNC_FILE = "sync.json"
_TEMP_NAME = re.compile(r"\.sync\.json\.[0-9a-f]{16}\.tmp")

type Connect = Callable[[str, str], Client]


def _now() -> datetime:
    return datetime.now(UTC)


# --- sync.json -----------------------------------------------------------


@dataclass(frozen=True)
class SyncFile:
    """An account's ``sync.json``. Empty when there is none."""

    #: How messages are matched (DESIGN.md, "Message matching").
    identity: str | None = None
    #: JMAP cursors: ``email_state`` and ``mailbox_state``.
    jmap: dict[str, str] = field(default_factory=dict)
    #: The account's ``sync.exclude`` when the cursors were taken: changes
    #: since then do not cover a mailbox that stopped being excluded, so a
    #: different one means listing everything.
    exclude: tuple[str, ...] | None = None
    #: Held messages in none of the synced mailboxes at the last evaluation.
    absent: frozenset[str] = frozenset()

    def to_json(self) -> dict[str, Any]:
        obj: dict[str, Any] = {}
        if self.identity is not None:
            obj["identity"] = self.identity
        if self.jmap:
            obj["jmap"] = dict(sorted(self.jmap.items()))
        if self.exclude is not None:
            obj["exclude"] = list(self.exclude)
        obj["absent"] = sorted(self.absent)
        return obj

    @classmethod
    def from_json(cls, obj: Any) -> SyncFile:
        """Raises ``ValueError``."""
        if not isinstance(obj, dict):
            raise ValueError("not an object")
        if unknown := obj.keys() - {"identity", "jmap", "exclude", "absent"}:
            raise ValueError(f"unknown keys {sorted(unknown)}")
        identity = obj.get("identity")
        jmap = obj.get("jmap", {})
        exclude = obj.get("exclude")
        absent = obj.get("absent", [])
        if identity is not None and not isinstance(identity, str):
            raise ValueError("bad identity")
        if not isinstance(jmap, dict) or not all(
            k in ("email_state", "mailbox_state") and isinstance(v, str)
            for k, v in jmap.items()
        ):
            raise ValueError("bad jmap")
        if exclude is not None and not (
            isinstance(exclude, list) and all(isinstance(e, str) for e in exclude)
        ):
            raise ValueError("bad exclude")
        if not isinstance(absent, list) or not all(
            isinstance(m, str) and m.startswith(MSG_PREFIX) for m in absent
        ):
            raise ValueError("bad absent")
        return cls(
            identity=identity,
            jmap=jmap,
            exclude=None if exclude is None else tuple(exclude),
            absent=frozenset(absent),
        )


def sync_path(store: Path, account: str) -> Path:
    return journal.journal_dir(store, account) / SYNC_FILE


def read_sync(store: Path, account: str) -> SyncFile:
    """An account's ``sync.json``; empty if missing, or (with a warning)
    damaged."""
    path = sync_path(store, account)
    try:
        return SyncFile.from_json(json.loads(path.read_bytes()))
    except FileNotFoundError:
        return SyncFile()
    except ValueError as e:
        log.warning("%s: ignored (%s); listing everything", path, e)
        return SyncFile()


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_sync(store: Path, account: str, sync: SyncFile) -> None:
    """Replace an account's ``sync.json`` atomically (temp, fsync,
    rename), removing temp files a crash left."""
    path = sync_path(store, account)
    directory = path.parent
    if not directory.exists():
        directory.mkdir(parents=True)
        _fsync_dir(directory.parent)
        _fsync_dir(directory.parent.parent)
    for p in directory.iterdir():
        if _TEMP_NAME.fullmatch(p.name):
            log.warning("%s: removing a temp file left by a crash", p)
            p.unlink()
    tmp = directory / f".{SYNC_FILE}.{secrets.token_hex(8)}.tmp"
    try:
        with open(tmp, "x", encoding="utf-8") as f:
            json.dump(sync.to_json(), f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    _fsync_dir(directory)


# --- runs ----------------------------------------------------------------


@dataclass
class Report:
    """One account's run."""

    account: str
    progress: Progress = field(default_factory=Progress)
    #: Whether the run completed, so incoming/ was evaluated and
    #: ``sync.json`` rewritten.
    complete: bool = False
    #: Why the run stopped, if it did.
    error: str | None = None
    applied: Applied | None = None
    #: Why drops were withheld (the mass-drop guard), and how many.
    guard: str | None = None
    withheld: int = 0

    @property
    def problems(self) -> list[str]:
        applied = self.applied.problems if self.applied else []
        return self.progress.problems + applied + self.progress.setting_problems

    @property
    def ok(self) -> bool:
        return self.complete and not self.problems

    def to_json(self) -> dict[str, Any]:
        p, a = self.progress, self.applied
        return {
            "account": self.account,
            "complete": self.complete,
            "error": self.error,
            "full": p.full,
            "mailboxes": p.mailboxes,
            "emails": p.emails,
            "downloaded": p.downloaded,
            "bytes": p.bytes,
            "stored": p.stored,
            "held": p.held,
            "events": p.events,
            "settings": p.settings,
            "incoming": None
            if a is None
            else {
                "promoted": a.promoted,
                "archived": a.archived,
                "dropped": a.dropped,
                "undropped": a.undropped,
                "purged": a.purged,
            },
            "guard": self.guard,
            "withheld": self.withheld,
            "problems": self.problems,
        }

    def text(self) -> str:
        p, a = self.progress, self.applied
        listing = "full listing, " if p.full else ""
        lines = [
            f"{self.account}: {listing}{p.mailboxes} mailbox changes,"
            f" {p.emails} emails changed, {p.downloaded} downloaded"
            f" ({p.bytes} bytes): {p.stored} stored, {p.held} held;"
            f" {p.settings} setting changes; {p.events} events"
        ]
        if a is not None:
            lines.append(
                f"  incoming: {a.promoted} promoted, {a.dropped} dropped,"
                f" {a.undropped} undropped, {a.purged} purged"
            )
        if self.guard:
            lines.append(f"  not dropped: {self.guard}; see --allow-mass-drop")
        lines += [f"  problem: {problem}" for problem in self.problems]
        if self.error:
            lines.append(f"  error: {self.error}")
        elif not self.complete:
            lines.append("  incomplete: incoming/ not evaluated, cursors kept")
        return "\n".join(lines)


def _connect(url: str, token: str) -> Client:
    return Client.connect(url, token)


def sync_account(
    ing: Ingester,
    store: Store,
    account: Account,
    *,
    allow_mass_drop: bool = False,
    connect: Connect = _connect,
    clock: Callable[[], datetime] = _now,
) -> Report:
    """One run of an account, its evaluation and its ``sync.json``. The
    caller holds the store lock (``ing.lock``)."""
    report = Report(account.name)
    path = store.path
    now = clock()
    previous = read_sync(path, account.name)
    try:
        token = resolve_secret(account.token, f"accounts.{account.name}.token")
        client = connect(account.url, token)
        state = journal.replay(path, account.name)
        writer = JournalWriter(path, account.name, clock=clock)
        run = JmapSync(
            client, ing, writer, state,
            settings=store.incoming, exclude=account.exclude, now=now,
        )  # fmt: skip
        report.progress = run.progress
        cursors = previous.jmap if previous.exclude == account.exclude else {}
        new = run.run(cursors.get("email_state"), cursors.get("mailbox_state"))
    except (ConfigError, JmapError, JournalError) as e:
        report.error = str(e)
        return report
    if report.progress.problems:
        return report
    evaluation = evaluate(
        state,
        incoming.held(path, account.name),
        ing.catalog.messages,
        store.incoming,
        now=now,
        absent_before=previous.absent,
        exclude=account.exclude,
        allow_mass_drop=allow_mass_drop,
    )
    report.applied = incoming.apply(ing, writer, evaluation)
    report.guard, report.withheld = evaluation.guard, len(evaluation.withheld)
    write_sync(
        path,
        account.name,
        SyncFile(
            identity="jmap",
            jmap=new.to_json(),
            exclude=account.exclude,
            absent=evaluation.absent,
        ),
    )
    report.complete = True
    return report


def sync_store(
    config: Config,
    store: Store,
    accounts: Sequence[Account],
    *,
    allow_mass_drop: bool = False,
    connect: Connect = _connect,
    clock: Callable[[], datetime] = _now,
) -> list[Report]:
    """Sync ``accounts`` (all of ``store``) under the store lock, then end
    the store's initialization if every account of it has completed a
    sync. Raises ``StoreLocked``."""
    reports = []
    with StoreLock(store.path) as lock:
        init_store(store.path)
        with closing(reindex.update(store.path, store.cache)) as conn:
            catalog = reindex.catalog(conn)
        with Ingester(
            store.path, lock, policy=store.attachments, catalog=catalog, clock=clock
        ) as ing:
            for account in accounts:
                report = sync_account(
                    ing, store, account,
                    allow_mass_drop=allow_mass_drop, connect=connect, clock=clock,
                )  # fmt: skip
                if report.guard:
                    log.warning("%s: %s; nothing dropped", account.name, report.guard)
                reports.append(report)
        if not is_initialized(store.path) and all(
            read_sync(store.path, a.name).jmap for a in config.accounts_of(store.name)
        ):
            mark_initialized(store.path, clock=clock)
            log.info("%s: initialization over", store.name)
    return reports


def _selected(config: Config, args: argparse.Namespace) -> dict[str, list[Account]]:
    """The accounts to sync, by store: those named (all by default),
    limited to ``--store`` if given."""
    names: list[str] = args.accounts or list(config.accounts)
    for name in names:
        if name not in config.accounts:
            raise ConfigError(f"no account {name!r}")
    if args.store is not None:
        store = config.store(args.store).name
        if args.accounts and (
            other := [n for n in names if config.accounts[n].store != store]
        ):
            raise ConfigError(f"accounts {other} are not in store {store!r}")
        names = [n for n in names if config.accounts[n].store == store]
    if not names:
        raise ConfigError("no accounts to sync")
    selected: dict[str, list[Account]] = {}
    for name in dict.fromkeys(names):
        account = config.accounts[name]
        selected.setdefault(account.store, []).append(account)
    return selected


def run(args: argparse.Namespace, out: Output) -> int:
    config = load(args.config)
    reports: list[Report] = []
    failed = False
    for store_name, accounts in _selected(config, args).items():
        try:
            reports += sync_store(
                config,
                config.stores[store_name],
                accounts,
                allow_mass_drop=args.allow_mass_drop,
            )
        except StoreLocked as e:
            log.error("%s", e)
            failed = True
    out.result(
        "\n".join(r.text() for r in reports),
        {"accounts": [r.to_json() for r in reports]},
    )
    return 1 if failed or not all(r.ok for r in reports) else 0
