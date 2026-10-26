"""The store commands (import-local, export, verify, show) through the CLI."""

import email
import email.policy
import json
import mailbox
import os
from compression import bz2, gzip, lzma, zstd
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest
from conftest import corpus_files

from geniza import cli, show
from geniza.config import init_store
from geniza.debug import _from_date, received_date
from geniza.mime.text import texts
from geniza.store import attachments, incoming, reader, segment
from geniza.store.attachments import AttachmentWriter
from geniza.store.ingest import message_id
from geniza.store.journal import Drop, JournalWriter
from geniza.store.lock import StoreLock
from geniza.store.segment import unescape

MSGS = [p.read_bytes() for p in corpus_files()]


@pytest.fixture
def config(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    store, cache = tmp_path / "store", tmp_path / "cache"
    path.write_text(f'[stores.test]\npath = "{store}"\ncache = "{cache}"\n')
    return path


@pytest.fixture
def maildir(tmp_path: Path) -> Path:
    """The corpus as a tree of Maildirs, with mtimes newest first."""
    root = tmp_path / "Mail"
    for sub in ("INBOX", "Archive/2020"):
        for d in ("cur", "new", "tmp"):
            (root / sub / d).mkdir(parents=True)
    for i, data in enumerate(MSGS):
        folder = "INBOX/cur" if i % 2 else "Archive/2020/new"
        path = root / folder / f"{i:04d}.host:2,S"
        path.write_bytes(data)
        t = 1_700_000_000 - i * 3600
        os.utime(path, (t, t))
    (root / "INBOX/cur/.hidden").write_bytes(b"not mail")
    return root


def run(config: Path, *argv: str | Path) -> int:
    return cli.main(["--config", str(config), *map(str, argv)])


def store_of(config: Path) -> Path:
    return config.parent / "store"


@pytest.fixture
def store(config: Path, maildir: Path) -> Path:
    assert run(config, "debug", "import-local", maildir) == 0
    return store_of(config)


def stored(store: Path) -> list[bytes]:
    """The corpus messages in the order of the store's records."""
    by_id = {message_id(m): m for m in MSGS}
    return [by_id[h.id] for p in segment.segments(store) for _, h in segment.scan(p)]


def test_import_local(
    config: Path, maildir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(config, "--json", "debug", "import-local", maildir) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["messages"] == report["new"] == len(MSGS)
    assert report["bytes"] == sum(map(len, MSGS))
    assert report["attachment stub"] > 0
    store = store_of(config)
    assert (store / "store.toml").exists()
    [path] = segment.segments(store)
    headers = [h for _, h in segment.scan(path)]
    assert sorted(h.id for h in headers) == sorted(message_id(m) for m in MSGS)
    # Dated from the messages, mtime only as a fallback, and oldest first.
    for h, data in zip(headers, stored(store), strict=True):
        files = [p for p in maildir.rglob("*:2,S") if p.read_bytes() == data]
        mtime = files[0].stat().st_mtime
        fallback = datetime.fromtimestamp(int(mtime), UTC)
        assert h.received == received_date(data, fallback=fallback)
    assert [h.received for h in headers] == sorted(h.received for h in headers)

    assert run(config, "--json", "debug", "import-local", maildir) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["already stored"] == len(MSGS)
    assert "new" not in report
    assert len(list(segment.scan(path))) == len(MSGS)


def test_import_local_mbox(config: Path, tmp_path: Path) -> None:
    sent = b"Subject: sent\nDate: Sun, 2 Mar 2025 23:59:00 -0800\n\nbody\n"
    delivered = b"Received: from a; Fri, 9 May 2025 08:07:06 +0000\n" + sent
    mbox = tmp_path / "in.mbox"
    box = mailbox.mbox(mbox)
    for data in (sent, delivered):
        m = mailbox.mboxMessage(data)
        m.set_from("someone@example.org Mon Jan  5 10:11:12 2015")
        box.add(m)
    box.close()
    assert run(config, "debug", "import-local", mbox) == 0
    store = store_of(config)
    got = {
        h.id: (p.relative_to(store).as_posix(), h.received)
        for p in segment.segments(store)
        for _, h in segment.scan(p)
    }
    assert got == {
        message_id(sent): (
            "segments/2015/2015-01.zst",
            datetime(2015, 1, 5, 10, 11, 12, tzinfo=UTC),
        ),
        message_id(delivered): (
            "segments/2025/2025-05.zst",
            datetime(2025, 5, 9, 8, 7, 6, tzinfo=UTC),
        ),
    }
    assert reader.message(store, "segments/2015/2015-01.zst", 0) == sent


def test_from_date() -> None:
    assert _from_date(b"From a@example.org Fri Oct 23 14:02:11 2026\n") == datetime(
        2026, 10, 23, 14, 2, 11, tzinfo=UTC
    )
    assert _from_date(b"From a Fri Oct 23 14:02:11 2026 +0200\n") is not None
    assert _from_date(b"From a yesterday\n") is None
    assert _from_date(b"From \n") is None


def test_import_local_needs_the_lock(config: Path, maildir: Path) -> None:
    with StoreLock(store_of(config)):
        assert run(config, "debug", "import-local", maildir) == 1


def test_import_local_missing_source(config: Path, tmp_path: Path) -> None:
    assert run(config, "debug", "import-local", tmp_path / "nothing") == 1


def test_export_mbox(store: Path, config: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.mbox"
    assert run(config, "export", "--mbox", out) == 0
    box = mailbox.mbox(out, create=False)
    got = [unescape(box.get_bytes(k)) for k in box.iterkeys()]
    want = [m if m.endswith(b"\n") else m + b"\n" for m in stored(store)]
    assert got == want
    # An existing file is not overwritten.
    before = out.read_bytes()
    assert run(config, "export", "--mbox", out) == 1
    assert out.read_bytes() == before


@pytest.mark.parametrize(
    ("suffix", "module"),
    [(".gz", gzip), (".bz2", bz2), (".xz", lzma), (".zst", zstd), (".ZSTD", zstd)],
)
def test_export_compressed(
    store: Path, config: Path, tmp_path: Path, suffix: str, module: ModuleType
) -> None:
    plain, packed = tmp_path / "out.mbox", tmp_path / f"out.mbox{suffix}"
    assert run(config, "export", "--mbox", plain) == 0
    assert run(config, "export", "--mbox", packed) == 0
    assert packed.read_bytes() != plain.read_bytes()
    assert module.decompress(packed.read_bytes()) == plain.read_bytes()


def test_export_to_stdout(
    store: Path, config: Path, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    assert run(config, "export", "--mbox", "-") == 0
    out = capsysbinary.readouterr().out
    assert out.count(b"\nFrom geniza ") == len(MSGS) - 1
    assert out.startswith(b"From geniza ")


def test_export_reports_a_message_it_cannot_rebuild(
    store: Path, config: Path, tmp_path: Path
) -> None:
    [(_, file), *_] = attachments.scan(store)
    (store / file).unlink()
    out = tmp_path / "out.mbox"
    assert run(config, "export", "--mbox", out) == 1
    box = mailbox.mbox(out, create=False)
    assert 0 < len(box) < len(MSGS)


def test_verify_clean_store(
    store: Path, config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    assert run(config, "--json", "verify") == 0
    report = json.loads(capsys.readouterr().out)
    assert report["records"] == report["messages"] == len(MSGS)
    assert report["bytes"] == sum(map(len, MSGS))
    assert report["attachment_files"] == len(list(attachments.scan(store)))
    assert report["orphans"] == report["problems"] == []


def verify(config: Path, capsys: pytest.CaptureFixture[str]) -> dict:
    capsys.readouterr()
    code = run(config, "--json", "verify")
    report = json.loads(capsys.readouterr().out)
    assert code == (1 if report["problems"] else 0)
    return report


def test_verify_reports_an_orphan(
    store: Path, config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with StoreLock(store):
        stored = AttachmentWriter(store).put(b"never committed", "orphan.txt")
    report = verify(config, capsys)
    assert report["orphans"] == [stored.file]
    assert report["problems"] == []


def test_verify_reports_a_missing_or_changed_file(
    store: Path, config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    files = [file for _, file in attachments.scan(store)]
    (store / files[0]).unlink()
    changed = store / files[1]
    changed.chmod(0o644)
    changed.write_bytes(changed.read_bytes() + b"x")
    report = verify(config, capsys)
    problems = "\n".join(report["problems"])
    assert files[0] in problems
    assert f"{files[1]}: content does not match its name" in problems
    assert report["records"] == len(MSGS)
    assert report["messages"] < len(MSGS)


def test_verify_reports_a_damaged_record(
    store: Path, config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    [path] = segment.segments(store)
    offsets = [o for o, _ in segment.scan(path)]
    data = bytearray(path.read_bytes())
    data[offsets[2] - 3] ^= 0xFF  # inside the second record's body frame
    path.write_bytes(data)
    report = verify(config, capsys)
    [problem] = report["problems"]
    assert f"at {offsets[1]}" in problem
    assert report["records"] == len(MSGS)
    assert report["messages"] == len(MSGS) - 1


def test_verify_reports_a_damaged_header(
    store: Path, config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    [path] = segment.segments(store)
    offsets = [o for o, _ in segment.scan(path)]
    data = bytearray(path.read_bytes())
    data[offsets[3]] ^= 0xFF  # the fourth record's magic
    path.write_bytes(data)
    report = verify(config, capsys)
    assert len(report["problems"]) == 1
    assert report["records"] == 3


def test_verify_reads_journals(
    store: Path, config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from geniza.store.journal import JournalWriter, Mbox

    with StoreLock(store):
        w = JournalWriter(store, "acct")
        w.append([Mbox(mbox="m1", name="INBOX")])
        w.append([Mbox(mbox="m2", name="Sent")])
    report = verify(config, capsys)
    assert (report["accounts"], report["journal_events"]) == (1, 2)
    [file] = (store / "state/acct").iterdir()
    file.write_bytes(file.read_bytes().replace(b'"seq":1', b'"seq":5', 1))
    report = verify(config, capsys)
    assert len(report["problems"]) == 1


def test_commands_need_a_store(
    config: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    assert run(config, "verify") == 2
    assert run(config, "export", "--mbox", tmp_path / "x.mbox") == 2
    assert not (tmp_path / "x.mbox").exists()
    assert run(tmp_path / "missing.toml", "verify") == 2


def test_show_raw(
    store: Path, config: Path, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    capsysbinary.readouterr()
    for data in MSGS[:5]:
        id = message_id(data)
        for query in (id, id.removeprefix("sha256:"), id[:20].upper()):
            assert run(config, "show", "--raw", query) == 0
            assert capsysbinary.readouterr().out == data


def where(store: Path, id: str) -> tuple[str, int]:
    """The segment and offset of the first record of ``id``."""
    for path in segment.segments(store):
        for offset, header in segment.scan(path):
            if header.id == id:
                return path.relative_to(store).as_posix(), offset
    raise KeyError(id)


def test_show_summary(
    store: Path, config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    with_parts = with_attached = 0
    for data in MSGS:
        id = message_id(data)
        assert run(config, "--json", "show", id) == 0
        info = json.loads(capsys.readouterr().out)
        assert info["id"] == id
        assert info["size"] == len(data)
        assert (info["segment"], info["offset"]) == where(store, id)
        subject = email.message_from_bytes(data, policy=email.policy.default)["Subject"]
        if subject is not None:
            assert info["headers"]["Subject"] == [" ".join(str(subject).split())]
        assert run(config, "show", id) == 0
        out = capsys.readouterr().out
        assert out.startswith(f"id: {id}\n")
        for p in info["parts"]:
            assert f"  {p['path']}: {p['kind']} {p['mode']} {p['file']}\n" in out
        with_parts += bool(info["parts"])
        message, *attached = texts(data)
        assert info["text"] == message.text
        assert [(m["path"], m["text"]) for m in info["attached"]] == [
            (m.path, m.text) for m in attached
        ]
        if message.text:
            assert f"\n\n{message.text}\n" in out
        for m in attached:
            assert f"\n--- attached message {m.path} ---\n" in out
            with_attached += 1
    assert with_parts > 0
    assert with_attached > 0


def test_show_after_another_import(
    config: Path, tmp_path: Path, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    """The index that the first import built is brought up to date."""
    for i, data in enumerate(MSGS[:2]):
        box = tmp_path / f"box{i}"
        for d in ("cur", "new", "tmp"):
            (box / d).mkdir(parents=True)
        (box / "new" / "1.host").write_bytes(data)
        assert run(config, "debug", "import-local", box) == 0
        assert run(config, "show", "--raw", message_id(data)) == 0
        assert capsysbinary.readouterr().out.endswith(data)
    assert (config.parent / "cache" / "index.sqlite").exists()


def test_show_held(config: Path, capsysbinary: pytest.CaptureFixture[bytes]) -> None:
    """A message only in incoming/ is read from the file there: the first
    account's whose file has the right hash."""
    store = store_of(config)
    data = next(m for m in MSGS if texts(m)[0].text)
    id = message_id(data)
    received = datetime(2026, 10, 1, 12, tzinfo=UTC)
    with StoreLock(store):
        init_store(store)
        for account in ("a", "b"):
            incoming.open_dir(store, account)
            incoming.write(store, account, id, data, received)
    JournalWriter(store, "b").append([Drop(msg=id)])
    capsysbinary.readouterr()
    assert run(config, "--json", "show", id[7:20]) == 0
    info = json.loads(capsysbinary.readouterr().out)
    assert info["location"] == "incoming"
    assert info["file"] == f"incoming/a/{id[7:]}.eml"
    assert (info["state"], info["parts"]) == ("live", [])
    assert info["received"] == "2026-10-01T12:00:00Z"
    assert (info["size"], info["text"]) == (len(data), texts(data)[0].text)
    assert run(config, "show", id) == 0
    out = capsysbinary.readouterr().out.decode()
    assert out.startswith(
        f"id: {id}\nheld: incoming/a/{id[7:]}.eml\n"
        f"received: 2026-10-01T12:00:00Z\nsize: {len(data)} bytes\n"
    )
    assert "record:" not in out
    # A's file holds another message: b's is read instead.
    first = incoming.file_path(store, "a", id)
    first.unlink()
    first.write_bytes(data + b"x")
    assert run(config, "show", id) == 0
    assert f"held: incoming/b/{id[7:]}.eml\n" in capsysbinary.readouterr().out.decode()
    assert run(config, "show", "--raw", id) == 0
    assert capsysbinary.readouterr().out == data
    # Dropped by every account holding it.
    JournalWriter(store, "a").append([Drop(msg=id)])
    assert run(config, "show", id) == 0
    assert f"held: incoming/b/{id[7:]}.eml (dropped)\n" in (
        capsysbinary.readouterr().out.decode()
    )
    incoming.file_path(store, "b", id).unlink()
    assert run(config, "show", id) == 1
    err = capsysbinary.readouterr().err.decode()
    assert "not in incoming/" in err
    assert "holds another message" in err


def test_show_errors(
    store: Path,
    config: Path,
    capsysbinary: pytest.CaptureFixture[bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = sorted(map(message_id, MSGS))
    assert run(config, "show", "sha256:" + "0" * 64) == 1
    assert "no such message" in capsysbinary.readouterr().err.decode()
    # 36 ids over 16 first digits: some digit starts two of them.
    monkeypatch.setattr(show, "MIN_PREFIX", 1)
    first = [id.removeprefix("sha256:")[0] for id in ids]
    shared = next(d for d in first if first.count(d) > 1)
    assert run(config, "show", shared) == 1
    assert "ambiguous" in capsysbinary.readouterr().err.decode()
    monkeypatch.undo()
    assert run(config, "show", "sha256:abc") == 2  # too short
    assert run(config, "show", "xyz12345") == 2
    assert run(config, "show", "--raw", "--json", ids[0]) == 2
    # A missing attachment file stops the rebuild.
    [(_, file), *_] = attachments.scan(store)
    (store / file).unlink()
    codes = {run(config, "show", "--raw", id) for id in ids}
    assert codes == {0, 1}
