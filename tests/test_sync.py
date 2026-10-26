"""JMAP sync (DESIGN.md, "Sync" and "JMAP") against the in-process fake
server: journal events, downloads, sync.json, incoming/ evaluation and
the end of initialization."""

import json
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from jmapserver import SETTINGS, TOKEN, FakeJmap, fake_jmap

from geniza import cli
from geniza.config import Config, is_initialized, parse
from geniza.store import incoming, journal, segment
from geniza.store.ingest import message_id
from geniza.store.journal import Add, Flags, Gone, Mbox, Remove, Setting
from geniza.sync.base import Report, SyncFile, read_sync, sync_path, sync_store
from geniza.sync.jmap import READ_ONLY, Client
from geniza.verify import verify

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
DAY = timedelta(days=1)
OLD = NOW - 100 * DAY


def message(n: int) -> bytes:
    return (
        f"From: a@example.org\r\nTo: b@example.net\r\n"
        f"Message-ID: <m{n}@example.org>\r\nSubject: {n}\r\n\r\nbody {n}\r\n"
    ).encode()


def make_config(
    root: Path, fakes: dict[str, FakeJmap], **extra: dict[str, Any]
) -> Config:
    data: dict[str, Any] = {
        "stores": {"s": {"path": str(root / "store"), "cache": str(root / "cache")}},
        "accounts": {
            name: {
                "store": "s",
                "type": "jmap",
                "url": fake.url,
                "token_command": f"printf %s {TOKEN}",
            }
            | extra.get(name, {})
            for name, fake in fakes.items()
        },
    }
    return parse(data, root / "config.toml", {})


def connect(url: str, token: str) -> Client:
    return Client.connect(url, token, timeout=10)


@dataclass
class Env:
    root: Path
    fake: FakeJmap
    config: Config
    inbox: str
    archive: str
    trash: str

    @property
    def store(self) -> Path:
        return self.config.stores["s"].path

    def sync(self, now: datetime = NOW, **kw: Any) -> Report:
        [report] = self.sync_all(now, **kw)
        return report

    def sync_all(self, now: datetime = NOW, **kw: Any) -> list[Report]:
        config = self.config
        return sync_store(
            config,
            config.stores["s"],
            list(config.accounts.values()),
            connect=connect,
            clock=lambda: now,
            **kw,
        )

    def exclude(self, *names: str) -> None:
        self.config = make_config(
            self.root, {"acct": self.fake}, acct={"sync": {"exclude": list(names)}}
        )

    def state(self) -> journal.State:
        return journal.replay(self.store, "acct")

    def events(self) -> list[journal.Event]:
        return list(journal.read_events(self.store, "acct"))


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    with fake_jmap() as fake:
        inbox = fake.add_mailbox("Inbox", "inbox")
        archive = fake.add_mailbox("Archive", "archive")
        trash = fake.add_mailbox("Trash", "trash")
        config = make_config(tmp_path, {"acct": fake})
        yield Env(tmp_path, fake, config, inbox, archive, trash)


def records(store: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in segment.segments(store):
        for _, header in segment.scan(path):
            counts[header.id] = counts.get(header.id, 0) + 1
    return counts


def check_journal(store: Path, fake: FakeJmap, exclude: tuple[str, ...] = ()) -> None:
    """The replayed journal has the server's mailboxes, and each email in
    each synced mailbox with its keywords."""
    state = journal.replay(store, "acct")
    assert {k: b.name for k, b in state.mailboxes.items()} == {
        k: m["name"] for k, m in fake.mailboxes.items()
    }
    synced = {k for k, b in state.mailboxes.items() if not incoming.matches(b, exclude)}
    expected = {
        (m, e.id): (message_id(e.blob), frozenset(e.keywords))
        for e in fake.emails.values()
        for m in e.mailboxes & synced
    }
    got = {
        (m, rid): (msg, state.flags_of(msg, m))
        for m, rids in state.contents.items()
        if m in synced
        for rid, msg in rids.items()
    }
    assert got == expected


def check_settings(store: Path, fake: FakeJmap) -> None:
    """The replayed journal has the server's settings, with the text of
    each Sieve script."""
    expected = {
        (t, id_): (obj, fake.sieve_blobs[id_].decode() if t == "SieveScript" else None)
        for t, objects in fake.settings.items()
        for id_, obj in objects.items()
    }
    assert journal.replay(store, "acct").settings == expected


def read_only(fake: FakeJmap) -> bool:
    return all(m.endswith(READ_ONLY) for m in fake.methods)


# --- first and second sync -----------------------------------------------


def test_first_sync(env: Env) -> None:
    fake = env.fake
    old = fake.add_email(message(1), {env.inbox}, {"$seen"}, OLD)
    recent = fake.add_email(message(2), {env.inbox}, set(), NOW - DAY)
    filed = fake.add_email(message(3), {env.archive, env.trash}, {"$flagged"}, NOW)
    report = env.sync()
    assert report.ok and report.progress.full
    assert (report.progress.downloaded, report.progress.stored) == (3, 2)
    assert report.progress.held == 1
    assert records(env.store) == {message_id(message(1)): 1, message_id(message(3)): 1}
    assert set(incoming.files(env.store, "acct")) == {message_id(message(2))}
    check_journal(env.store, fake)
    state = env.state()
    assert state.mailboxes[env.inbox].role == "inbox"
    assert state.eids == {
        old: message_id(message(1)),
        recent: message_id(message(2)),
        filed: message_id(message(3)),
    }
    assert state.placed[env.inbox, old] == journal.Placed(OLD, "T" + old[1:])
    sync = read_sync(env.store, "acct")
    assert sync.identity == "jmap" and sync.exclude == ()
    assert set(sync.jmap) == {"email_state", "mailbox_state"}
    assert is_initialized(env.store)
    assert verify(env.store).problems == []
    assert read_only(fake)


def test_mailbox_events(env: Env) -> None:
    env.fake.add_mailbox("2020", parent=env.archive, sortOrder=3, color="#3a7")
    env.sync()
    boxes = {e.name: e for e in env.events() if isinstance(e, Mbox)}
    assert boxes["Inbox"] == Mbox(
        mbox=env.inbox, name="Inbox", role="inbox", sort=10, subscribed=True,
        props={"myRights": {"mayReadItems": True}},
        seq=boxes["Inbox"].seq, ts=boxes["Inbox"].ts,
    )  # fmt: skip
    assert boxes["2020"].parent == env.archive and boxes["2020"].sort == 3
    assert boxes["2020"].props == {"myRights": {"mayReadItems": True}, "color": "#3a7"}


@pytest.mark.parametrize("connections", [1, 3])
def test_downloads_in_parallel(env: Env, connections: int) -> None:
    for i in range(10):
        env.fake.add_email(message(i), {env.inbox}, received=OLD)
    env.fake.download_delay = 0.02
    env.config = make_config(
        env.root, {"acct": env.fake}, acct={"sync": {"connections": connections}}
    )
    report = env.sync()
    assert report.ok and report.progress.downloaded == 10
    assert 1 <= env.fake.max_downloads <= connections
    check_journal(env.store, env.fake)


def test_second_sync_downloads_nothing(env: Env) -> None:
    for i in range(5):
        env.fake.add_email(message(i), {env.inbox}, received=OLD)
    env.sync()
    downloads, events = len(env.fake.downloads), len(env.events())
    report = env.sync()
    assert report.ok and not report.progress.full
    assert len(env.fake.downloads) == downloads
    assert len(env.events()) == events
    assert report.progress.events == 0


def test_moves_and_flags_are_journal_events_only(env: Env) -> None:
    fake = env.fake
    e1 = fake.add_email(message(1), {env.inbox}, received=OLD)
    e2 = fake.add_email(message(2), {env.inbox}, received=OLD)
    env.sync()
    downloads, seq = len(fake.downloads), env.state().seq
    fake.update_email(e1, mailboxes={env.archive})
    fake.update_email(e2, keywords={"$seen", "$label1"})
    report = env.sync()
    assert report.ok and len(fake.downloads) == downloads
    m1, m2 = message_id(message(1)), message_id(message(2))
    new = [e for e in env.events() if e.seq > seq]
    assert [(type(e), e.msg, e.mbox) for e in new] == [  # type: ignore[attr-defined]
        (Remove, m1, env.inbox),
        (Add, m1, env.archive),
        (Flags, m1, env.archive),
        (Flags, m2, env.inbox),
    ]
    assert new[-1].set == {"$seen", "$label1"}  # type: ignore[attr-defined]
    check_journal(env.store, fake)


def test_mailbox_changes(env: Env) -> None:
    fake = env.fake
    lists = fake.add_mailbox("Lists")
    e1 = fake.add_email(message(1), {lists}, received=OLD)
    fake.add_email(message(2), {lists, env.archive}, received=OLD)
    env.sync()
    seq = env.state().seq
    fake.update_mailbox(env.inbox, totalEmails=12, unreadEmails=3)
    fake.update_mailbox(env.archive, name="Old mail")
    report = env.sync()
    new = [e for e in env.events() if e.seq > seq]
    assert [(type(e), e.mbox) for e in new] == [(Mbox, env.archive)]  # type: ignore[attr-defined]
    assert report.progress.mailboxes == 1
    # A destroyed mailbox: the messages still in it are removed, then gone.
    seq = env.state().seq
    fake.destroy_email(e1)
    fake.destroy_mailbox(lists)
    env.sync()
    new = [e for e in env.events() if e.seq > seq]
    assert [(type(e), e.mbox) for e in new] == [  # type: ignore[attr-defined]
        (Remove, lists),
        (Remove, lists),
        (Gone, lists),
    ]
    assert len({e.ts for e in new}) == 1
    check_journal(env.store, fake)
    assert lists not in env.state().mailboxes


def test_mailbox_unknown_to_the_mailbox_sync(env: Env) -> None:
    """An email in a mailbox created after the mailboxes were synced (here
    one ``Mailbox/changes`` misses) gets its mailbox fetched first."""
    fake = env.fake
    env.sync()
    late = "M9999"
    fake.mailboxes[late] = fake.mailboxes[env.archive] | {"id": late, "name": "Late"}
    fake.add_email(message(1), {late}, received=OLD)
    report = env.sync()
    assert report.ok
    state = env.state()
    assert state.mailboxes[late].name == "Late"
    assert state.contents[late]


# --- full listings -------------------------------------------------------


def test_cannot_calculate_changes_lists_everything(env: Env) -> None:
    fake = env.fake
    ids = [fake.add_email(message(i), {env.inbox}, received=OLD) for i in range(4)]
    env.sync()
    fake.update_email(ids[0], mailboxes={env.archive}, keywords={"$seen"})
    fake.destroy_email(ids[1])
    fake.add_email(message(9), {env.archive}, received=OLD)
    fake.destroy_mailbox(env.trash)
    fake.forget_changes()
    downloads = len(fake.downloads)
    report = env.sync()
    assert report.ok and report.progress.full
    assert len(fake.downloads) == downloads + 1
    check_journal(env.store, fake)
    assert verify(env.store).problems == []


def test_lost_sync_json_downloads_nothing(env: Env) -> None:
    for i in range(3):
        env.fake.add_email(message(i), {env.inbox}, received=OLD)
    env.sync()
    sync_path(env.store, "acct").unlink()
    downloads, events = len(env.fake.downloads), len(env.events())
    report = env.sync()
    assert report.ok and report.progress.full
    assert len(env.fake.downloads) == downloads
    assert len(env.events()) == events


def test_damaged_sync_json_is_ignored(env: Env) -> None:
    env.fake.add_email(message(1), {env.inbox}, received=OLD)
    env.sync()
    sync_path(env.store, "acct").write_text('{"jmap": 3}')
    assert read_sync(env.store, "acct") == SyncFile()
    report = env.sync()
    assert report.ok and report.progress.full
    assert len(env.fake.downloads) == 1


@given(
    st.builds(
        SyncFile,
        identity=st.none() | st.just("jmap"),
        jmap=st.dictionaries(
            st.sampled_from(["email_state", "mailbox_state"]), st.text()
        ),
        exclude=st.none() | st.tuples(st.text()) | st.tuples(),
        absent=st.frozensets(st.text("0123456789abcdef").map(lambda h: "sha256:" + h)),
    )
)
def test_sync_file_round_trip(sync: SyncFile) -> None:
    assert SyncFile.from_json(json.loads(json.dumps(sync.to_json()))) == sync


# --- exclusion -----------------------------------------------------------


def test_excluded_mailboxes(env: Env) -> None:
    fake = env.fake
    junk = fake.add_mailbox("Junk", "junk")
    spam = fake.add_email(message(1), {junk}, received=OLD)
    both = fake.add_email(message(2), {junk, env.archive}, received=OLD)
    env.exclude("junk")
    report = env.sync()
    assert report.ok and report.progress.downloaded == 1
    state = env.state()
    assert junk in state.mailboxes and junk not in state.contents
    check_journal(env.store, fake, ("junk",))
    # Leaving an excluded mailbox is journaled; nothing is added to one.
    fake.update_email(both, mailboxes={env.archive})
    env.sync()
    check_journal(env.store, fake, ("junk",))
    # No longer excluded: everything is listed again, so Junk is synced.
    env.exclude()
    report = env.sync()
    assert report.progress.full and report.progress.downloaded == 1
    check_journal(env.store, fake)
    assert env.state().eids[spam] == message_id(message(1))


# --- failures ------------------------------------------------------------


@pytest.mark.parametrize(
    ("knob", "error"),
    [
        ("truncate", f"downloaded {len(message(2)) - 1} bytes, the server said"),
        ("fail", "HTTP 500"),
    ],
)
def test_download_problems_leave_the_run_incomplete(
    env: Env, knob: str, error: str
) -> None:
    fake = env.fake
    fake.add_email(message(1), {env.inbox}, received=OLD)
    bad = fake.add_email(message(2), {env.inbox}, received=OLD)
    getattr(fake, knob).add(bad)
    report = env.sync()
    assert not report.complete and not report.ok
    [problem] = report.problems
    assert problem.startswith(f"email {bad}: ") and error in problem
    assert "incomplete" in report.text()
    assert not sync_path(env.store, "acct").exists()
    assert not is_initialized(env.store)
    assert records(env.store) == {message_id(message(1)): 1}
    getattr(fake, knob).clear()
    downloads = len(fake.downloads)
    report = env.sync()
    assert report.ok and len(fake.downloads) == downloads + 1
    check_journal(env.store, fake)
    assert is_initialized(env.store)


def test_bad_token(env: Env) -> None:
    env.config = make_config(
        env.root, {"acct": env.fake}, acct={"token_command": "printf %s wrong"}
    )
    report = env.sync()
    assert not report.complete
    assert report.error is not None and "authentication failed" in report.error
    assert "wrong" not in report.error


def test_failing_token_command_does_not_stop_other_accounts(tmp_path: Path) -> None:
    with fake_jmap() as a, fake_jmap() as b:
        for fake in (a, b):
            fake.add_email(message(1), {fake.add_mailbox("Inbox", "inbox")})
        config = make_config(tmp_path, {"a": a, "b": b}, a={"token_command": "false"})
        reports = sync_store(
            config, config.stores["s"], list(config.accounts.values()),
            connect=connect, clock=lambda: NOW,
        )  # fmt: skip
        assert [r.ok for r in reports] == [False, True]
        assert reports[0].error == "accounts.a.token: command exited with status 1"
        assert not is_initialized(config.stores["s"].path)


# --- incoming/ and initialization ---------------------------------------


def test_held_message_destroyed_is_dropped_after_two_runs(env: Env) -> None:
    fake = env.fake
    e1 = fake.add_email(message(1), {env.inbox}, received=NOW - DAY)
    env.sync()
    m1 = message_id(message(1))
    fake.destroy_email(e1)
    report = env.sync(NOW + DAY)
    assert report.applied is not None and report.applied.dropped == 0
    assert read_sync(env.store, "acct").absent == {m1}
    report = env.sync(NOW + 2 * DAY)
    assert report.applied is not None and report.applied.dropped == 1
    assert env.state().fates[m1].status == "dropped"
    assert read_sync(env.store, "acct").absent == frozenset()


def test_held_message_filed_is_promoted(env: Env) -> None:
    fake = env.fake
    e1 = fake.add_email(message(1), {env.inbox}, received=NOW - DAY)
    env.sync()
    fake.update_email(e1, mailboxes={env.archive})
    report = env.sync(NOW + DAY)
    assert report.applied is not None and report.applied.promoted == 1
    assert incoming.files(env.store, "acct") == {}
    assert records(env.store) == {message_id(message(1)): 1}


def test_purged_message_seen_again_is_downloaded_again(env: Env) -> None:
    fake = env.fake
    e1 = fake.add_email(message(1), {env.inbox}, received=NOW - DAY)
    env.sync()
    fake.update_email(e1, mailboxes=set())
    for days in (1, 2, 40):
        env.sync(NOW + days * DAY)
    m1 = message_id(message(1))
    assert env.state().fates[m1].status == "purged"
    assert incoming.files(env.store, "acct") == {}
    fake.update_email(e1, mailboxes={env.archive})
    downloads = len(fake.downloads)
    report = env.sync(NOW + 41 * DAY)
    assert report.ok and len(fake.downloads) == downloads + 1
    assert records(env.store) == {m1: 1}
    assert verify(env.store).problems == []


def test_mass_drop_guard(env: Env) -> None:
    fake = env.fake
    ids = [
        fake.add_email(message(i), {env.inbox}, received=NOW - DAY) for i in range(12)
    ]
    env.sync()
    for e in ids:
        fake.destroy_email(e)
    env.sync(NOW + DAY)
    report = env.sync(NOW + 2 * DAY)
    assert report.ok and report.guard is not None and report.withheld == 12
    assert "not dropped" in report.text()
    report = env.sync(NOW + 3 * DAY, allow_mass_drop=True)
    assert report.applied is not None and report.applied.dropped == 12


def test_two_accounts_share_the_store(tmp_path: Path) -> None:
    with fake_jmap() as a, fake_jmap() as b:
        for fake in (a, b):
            fake.add_email(message(1), {fake.add_mailbox("Archive")}, received=OLD)
        config = make_config(tmp_path, {"a": a, "b": b})
        store = config.stores["s"]
        [ra] = sync_store(
            config, store, [config.accounts["a"]], connect=connect, clock=lambda: NOW
        )
        assert ra.ok and not is_initialized(store.path)
        [rb] = sync_store(
            config, store, [config.accounts["b"]], connect=connect, clock=lambda: NOW
        )
        assert rb.ok and rb.progress.stored == 0 and rb.progress.downloaded == 1
        assert is_initialized(store.path)
        assert records(store.path) == {message_id(message(1)): 1}
        assert verify(store.path).problems == []


def test_records_go_by_received_month_until_initialized(env: Env) -> None:
    env.fake.add_email(
        message(1), {env.archive}, received=datetime(2019, 5, 2, tzinfo=UTC)
    )
    env.sync()
    env.fake.add_email(
        message(2), {env.archive}, received=datetime(2019, 5, 3, tzinfo=UTC)
    )
    env.sync()
    assert [
        p.relative_to(env.store).as_posix() for p in segment.segments(env.store)
    ] == [
        "segments/2019/2019-05.zst",
        "segments/2026/2026-10.zst",
    ]


# --- settings ------------------------------------------------------------


def settings_events(env: Env) -> list[Setting]:
    return [e for e in env.events() if isinstance(e, Setting)]


def test_settings(env: Env) -> None:
    fake = env.fake
    fake.enable_settings()
    identity = fake.set_setting("Identity", name="A", email="a@example.org")
    other = fake.set_setting("Identity", name="B", email="b@example.org")
    script = fake.set_sieve(b'require "fileinto";\r\nkeep;\r\n')
    fake.set_setting("VacationResponse", "singleton", isEnabled=False, subject=None)
    report = env.sync()
    assert report.ok and report.progress.settings == 4
    check_settings(env.store, fake)
    [event] = [e for e in settings_events(env) if e.type == "SieveScript"]
    assert event.content == 'require "fileinto";\r\nkeep;\r\n'

    # Nothing changed: no events, and the script is not downloaded again.
    downloads = len(fake.downloads)
    report = env.sync()
    assert report.ok and report.progress.settings == 0
    assert len(fake.downloads) == downloads

    fake.set_setting("Identity", identity, name="A2", email="a@example.org")
    fake.destroy_setting("Identity", other)
    fake.set_sieve(b"discard;\n", script)
    fake.set_setting("VacationResponse", "singleton", isEnabled=True, subject="Away")
    report = env.sync()
    assert report.ok and report.progress.settings == 4
    check_settings(env.store, fake)
    assert any(e.id == other and e.value is None for e in settings_events(env))
    assert len(fake.downloads) == downloads + 1
    assert read_only(fake)


def test_settings_without_their_capability(env: Env) -> None:
    fake = env.fake
    fake.set_setting("Identity", name="A")
    assert env.sync().ok
    assert not any(m.split("/")[0] in SETTINGS for m in fake.methods)
    assert settings_events(env) == []

    # Journaled settings stay when the capability goes away.
    fake.enable_settings("Identity", "VacationResponse")
    assert env.sync().ok
    assert len(settings_events(env)) == 1
    assert not any(m.startswith("SieveScript/") for m in fake.methods)
    fake.extra_capabilities.clear()
    fake.settings["Identity"].clear()
    assert env.sync().ok
    assert len(settings_events(env)) == 1
    assert len(env.state().settings) == 1


def test_setting_problems_leave_the_run_complete(env: Env) -> None:
    fake = env.fake
    fake.enable_settings()
    fake.add_email(message(1), {env.inbox}, received=OLD)
    script = fake.set_sieve(b"keep;\n")
    bad = fake.set_sieve(b"# caf\xe9\nkeep;\n", name="latin-1")
    fake.set_setting("VacationResponse", "singleton", isEnabled=False)
    fake.fail.add(script)
    fake.forbidden.add("Identity/get")
    report = env.sync()
    assert report.complete and not report.ok
    assert len(report.problems) == 3, report.problems
    assert any("Identity" in p and "forbidden" in p for p in report.problems)
    assert any(bad in p and "not UTF-8" in p for p in report.problems)
    # The other settings, the mail and the cursors are synced all the same.
    assert [e.type for e in settings_events(env)] == ["VacationResponse"]
    assert read_sync(env.store, "acct").jmap
    assert "problem: SieveScript" in report.text()

    fake.fail.clear()
    fake.forbidden.clear()
    fake.destroy_setting("SieveScript", bad)
    assert env.sync().ok
    check_settings(env.store, fake)


# --- the command ---------------------------------------------------------


def test_cli(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.fake.add_email(message(1), {env.inbox}, received=OLD)
    config = env.root / "config.toml"
    config.write_text(
        f'[stores.s]\npath = "{env.store}"\ncache = "{env.root / "cache"}"\n'
        f'[accounts.acct]\nstore = "s"\ntype = "jmap"\nurl = "{env.fake.url}"\n'
        f'token_command = "printf %s {TOKEN}"\n'
    )
    assert cli.main(["--config", str(config), "sync"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("acct: full listing, 3 mailbox changes, 1 emails changed")
    assert cli.main(["--config", str(config), "--json", "sync", "acct"]) == 0
    [report] = json.loads(capsys.readouterr().out)["accounts"]
    assert report["complete"] and report["downloaded"] == 0 and not report["full"]
    assert cli.main(["--config", str(config), "sync", "other"]) == 2
    assert "no account 'other'" in capsys.readouterr().err


# --- property test -------------------------------------------------------

N = 5
BOX_NAMES = ["Inbox", "Archive", "Trash", "Junk"]
email = st.integers(0, N - 1)
box = st.integers(0, len(BOX_NAMES) - 1)
action = st.one_of(
    st.tuples(st.just("add"), email, st.sets(box, max_size=2)),
    st.tuples(st.just("move"), email, st.sets(box, min_size=1, max_size=2)),
    st.tuples(
        st.just("keywords"), email, st.sets(st.sampled_from(["$seen", "$flagged", "x"]))
    ),
    st.tuples(st.just("destroy"), email),
    st.tuples(st.just("rename"), box),
    st.tuples(st.just("identity"), st.integers(0, 2), st.sampled_from(["A", "B"])),
    st.tuples(st.just("unidentity"), st.integers(0, 2)),
    st.tuples(st.just("sieve"), st.integers(0, 1), st.sampled_from(["keep;", "é"])),
    st.tuples(st.just("unsieve"), st.integers(0, 1)),
    st.tuples(st.just("forget")),
    st.tuples(st.just("lose")),
    st.tuples(st.just("sync")),
)


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(action, max_size=20), st.booleans())
def test_sync_follows_the_server(actions: list[tuple[Any, ...]], exclude: bool) -> None:
    """After any changes on the server, a sync leaves the journal equal to
    the server, and no body is downloaded twice."""
    with tempfile.TemporaryDirectory() as tmp, fake_jmap() as fake:
        root = Path(tmp)
        fake.enable_settings()
        identities: dict[int, str] = {}
        scripts: dict[int, str] = {}
        boxes = [
            fake.add_mailbox(n, n.lower() if n != "Archive" else None)
            for n in BOX_NAMES
        ]
        excluded = ("junk",) if exclude else ()
        config = make_config(
            root, {"acct": fake}, acct={"sync": {"exclude": list(excluded)}}
        )
        store = config.stores["s"]
        emails: dict[int, str] = {}

        def sync() -> None:
            [report] = sync_store(
                config, store, [config.accounts["acct"]], connect=connect,
                clock=lambda: NOW,
            )  # fmt: skip
            assert report.ok, report.text()
            check_journal(store.path, fake, excluded)
            check_settings(store.path, fake)

        for a in [*actions, ("sync",)]:
            match a:
                case ("add", i, where) if i not in emails:
                    # A new message each time: the same bytes under a new
                    # email id are downloaded again, rightly.
                    emails[i] = fake.add_email(
                        message(len(fake.emails) + 100 * len(fake.methods)),
                        {boxes[b] for b in where} or {boxes[0]},
                        received=OLD,
                    )
                case ("move", i, where) if i in emails:
                    fake.update_email(emails[i], mailboxes={boxes[b] for b in where})
                case ("keywords", i, kw) if i in emails:
                    fake.update_email(emails[i], keywords=kw)
                case ("destroy", i) if i in emails:
                    fake.destroy_email(emails.pop(i))
                case ("rename", b):
                    fake.update_mailbox(
                        boxes[b], name=f"{BOX_NAMES[b]}-{len(fake.methods)}"
                    )
                case ("identity", i, name):
                    identities[i] = fake.set_setting(
                        "Identity", identities.get(i), name=name
                    )
                case ("unidentity", i) if i in identities:
                    fake.destroy_setting("Identity", identities.pop(i))
                case ("sieve", i, text):
                    scripts[i] = fake.set_sieve(text.encode(), scripts.get(i))
                case ("unsieve", i) if i in scripts:
                    fake.destroy_setting("SieveScript", scripts.pop(i))
                case ("forget",):
                    fake.forget_changes()
                case ("lose",):
                    sync_path(store.path, "acct").unlink(missing_ok=True)
                case ("sync",):
                    sync()
        # Each body once, unless excluded at first (never downloaded then);
        # a script's text is the same blob when set again to the same text.
        bodies = [d for d in fake.downloads if d.startswith("G")]
        assert len(bodies) == len(set(bodies))
        assert read_only(fake)
        assert verify(store.path).problems == []
