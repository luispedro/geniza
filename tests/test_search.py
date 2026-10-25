"""``geniza search``: the query language and the command."""

import json
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from geniza import cli, search
from geniza.config import init_store
from geniza.index.reindex import update
from geniza.search import QueryError, Term, date_range, lex
from geniza.store.ingest import Ingester, message_id
from geniza.store.journal import Add, Drop, JournalWriter, Mbox
from geniza.store.lock import StoreLock

T0 = datetime(2026, 10, 23, 14, 2, 11, tzinfo=UTC)


def msg(
    subject: str,
    date: str | None,
    body: str,
    *,
    sender: str = "Alice Example <alice@example.org>",
    extra: str = "",
) -> bytes:
    head = f"From: {sender}\nTo: Bob <bob@example.net>\nSubject: {subject}\n"
    if date is not None:
        head += f"Date: {date}\n"
    return f"{head}{extra}\n{body}\n".encode()


MSGS = {
    "old": msg("Annual report", "Mon, 04 Mar 2024 10:00:00 +0000", "The budget."),
    "late": msg(
        "Late night",
        # 2024-04-01 in UTC
        "Sun, 31 Mar 2024 23:30:00 -0100",
        "Report attached.",
        sender="Carol <carol@example.com>",
        extra="Message-ID: <late@example.org>\n",
    ),
    "reply": msg(
        "Re: Late night",
        "Wed, 05 Jun 2024 09:00:00 +0000",
        "Thanks, the café was fine.",
        extra="Message-ID: <reply@example.org>\nIn-Reply-To: <late@example.org>\n",
    ),
    "new": msg("Holiday plans", "Fri, 10 Jan 2025 08:00:00 +0000", "AND NEAR ^ ok"),
    # No Date: when it was received (T0) instead.
    "undated": msg("No date here", None, "budget again"),
}
IDS = {name: message_id(m) for name, m in MSGS.items()}
NAMES = {id: name for name, id in IDS.items()}
#: Only in incoming/, and dropped.
GONE = msg("Dropped one", "Tue, 01 Oct 2024 00:00:00 +0000", "budget gone")


@pytest.fixture
def config(tmp_path: Path) -> Path:
    store, cache = tmp_path / "store", tmp_path / "cache"
    with StoreLock(store) as lock:
        init_store(store)
        with Ingester(store, lock, clock=lambda: T0) as ing:
            for m in MSGS.values():
                ing.add(m, T0)
    path = (
        store / "incoming" / "work" / f"{message_id(GONE).removeprefix('sha256:')}.eml"
    )
    path.parent.mkdir(parents=True)
    path.write_bytes(GONE)
    os.utime(path, (T0.timestamp(), T0.timestamp()))
    JournalWriter(store, "work", clock=lambda: T0).append(
        [
            Mbox(mbox="m1", name="INBOX", role="inbox"),
            Mbox(mbox="m2", name="Projects"),
            Mbox(mbox="m3", name="Reports", parent="m2"),
            # A loop of parents (a broken server): names still match.
            Mbox(mbox="m4", name="Loop", parent="m5"),
            Mbox(mbox="m5", name="Loop2", parent="m4"),
            Add(msg=IDS["new"], mbox="m1", rid="1"),
            Add(msg=IDS["old"], mbox="m3", rid="2"),
            Add(msg=IDS["late"], mbox="m4", rid="3"),
            Drop(msg=message_id(GONE)),
        ]
    )
    JournalWriter(store, "home", clock=lambda: T0).append(
        [Mbox(mbox="i", name="Inbox"), Add(msg=IDS["reply"], mbox="i", rid="9")]
    )
    config = tmp_path / "config.toml"
    config.write_text(f'[stores.test]\npath = "{store}"\ncache = "{cache}"\n')
    return config


def run(config: Path, *argv: str) -> int:
    return cli.main(["--config", str(config), *argv])


def found(config: Path, capsys: pytest.CaptureFixture[str], *query: str) -> list[str]:
    """The names of the messages a search finds, in its order."""
    assert run(config, "--json", "search", "-n", "0", *query) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["total"] == len(result["messages"])
    return [NAMES.get(m["id"], "gone") for m in result["messages"]]


# --- the query language ------------------------------------------------------


def test_lex() -> None:
    assert lex('  from:ann "a b"* -subject:"x y" re:z w* -  "open') == [
        Term("ann", "from"),
        Term("a b", quoted=True, prefix=True),
        Term("x y", "subject", negated=True, quoted=True),
        Term("re:z"),
        Term("w", prefix=True),
        Term("-"),
        Term("open", quoted=True),
    ]


@given(st.text())
def test_lex_never_raises(query: str) -> None:
    for t in lex(query):
        # Every term is FTS5-safe, whatever it holds.
        search._fts(t)


@pytest.mark.parametrize(
    ("value", "bounds"),
    [
        ("2024", ("2024-01-01", "2025-01-01")),
        ("2024-12", ("2024-12-01", "2025-01-01")),
        ("2024-02-29", ("2024-02-29", "2024-03-01")),
        ("2024-03..2024-06", ("2024-03-01", "2024-07-01")),
        ("..2024", (None, "2025-01-01")),
        ("2024-12-31..", ("2024-12-31", None)),
    ],
)
def test_date_range(value: str, bounds: tuple[str | None, str | None]) -> None:
    assert date_range(value) == tuple(
        None if b is None else b + "T00:00:00Z" for b in bounds
    )


@pytest.mark.parametrize("value", ["", "..", "24", "2024-13", "2023-02-29", "x..y"])
def test_date_range_errors(value: str) -> None:
    with pytest.raises(QueryError):
        date_range(value)


# --- the command -------------------------------------------------------------


def test_words_and_fields(config: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert found(config, capsys, "budget") == ["undated", "old"]  # newest first
    assert found(config, capsys, "report") == ["late", "old"]
    assert found(config, capsys, "subject:report") == ["old"]
    assert found(config, capsys, "body:report") == ["late"]
    assert found(config, capsys, "from:carol") == ["late"]
    assert found(config, capsys, "to:bob", "from:carol") == ["late"]
    assert found(config, capsys, "rep*") == ["late", "old"]
    assert found(config, capsys, "cafe") == ["reply"]
    assert found(config, capsys, '"late night"') == ["reply", "late"]
    assert found(config, capsys, '"night late"') == []
    assert found(config, capsys, "report -subject:annual") == ["late"]
    assert found(config, capsys, "--", "-budget", "date:2024") == ["reply", "late"]
    # FTS5 syntax is searched for as words.
    assert found(config, capsys, "AND", "NEAR", "^") == ["new"]
    assert found(config, capsys, 'x"y') == []
    # Punctuation alone is not a term.
    assert found(config, capsys, "budget", "@") == ["undated", "old"]


def test_dates(config: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert found(config, capsys, "date:2024") == ["reply", "late", "old"]
    assert found(config, capsys, "date:2024-03") == ["old"]
    assert found(config, capsys, "date:2024-04-01") == ["late"]  # in UTC
    assert found(config, capsys, "date:2024-04..") == [
        "undated",
        "new",
        "reply",
        "late",
    ]
    assert found(config, capsys, "date:..2024-04-01") == ["late", "old"]
    assert found(config, capsys, "--", "-date:..2025") == ["undated"]  # received, 2026


def test_mailboxes_and_threads(
    config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert found(config, capsys, "mailbox:inbox") == ["new", "reply"]  # role, name
    assert found(config, capsys, "mailbox:INBOX") == ["new", "reply"]
    assert found(config, capsys, "mailbox:reports") == ["old"]
    assert found(config, capsys, "mailbox:projects/reports") == ["old"]
    assert found(config, capsys, "mailbox:loop") == ["late"]
    assert found(config, capsys, "budget -mailbox:reports") == ["undated"]
    assert run(config, "search", "mailbox:nowhere") == 0
    assert capsys.readouterr() == ("", "WARNING: mailbox:nowhere: no such mailbox\n")
    late = IDS["late"].removeprefix("sha256:")[:8]
    assert found(config, capsys, f"thread:{late}") == ["reply", "late"]
    assert found(config, capsys, "--", f"-thread:{late}", "date:2024") == ["old"]


def test_dropped(config: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert found(config, capsys, "budget") == ["undated", "old"]
    assert found(config, capsys, "--dropped", "budget") == ["undated", "gone", "old"]


def test_text_output_and_limit(
    config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(config, "search", "--dropped", "-n", "2", "budget") == 0
    out, err = capsys.readouterr()
    undated, gone = IDS["undated"], message_id(GONE)
    assert out == (
        f"{undated[7:19]}  2026-10-23  Alice Example <alice@example.org>"
        "  No date here\n"
        f"{gone[7:19]}  2024-10-01  Alice Example <alice@example.org>"
        "  Dropped one  [dropped]\n"
    )
    assert "2 of 3 messages shown" in err
    # The short id is enough for show.
    assert run(config, "show", IDS["old"][7:19]) == 0


def test_errors(config: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(config, "search", "@") == 2
    assert "nothing to search for" in capsys.readouterr().err
    assert run(config, "search", "date:2024-13") == 2
    assert run(config, "search", "thread:zz") == 2
    assert run(config, "search", "thread:" + "0" * 8) == 2
    assert "no such message" in capsys.readouterr().err
    assert run(config, "search", "-n", "-1", "x") == 2
    assert run(config, "search", "mailbox:") == 2


def test_thread_of_an_unthreaded_message(
    config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store, cache = config.parent / "store", config.parent / "cache"
    update(store, cache).close()
    with closing(sqlite3.connect(cache / "index.sqlite")) as conn:
        conn.execute("UPDATE messages SET thread_id = NULL")
        conn.commit()
    assert found(config, capsys, "thread:" + IDS["late"][7:15]) == ["late"]


def test_search_reads_bodies(config: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Search brings the index up to date, texts included."""
    store, cache = config.parent / "store", config.parent / "cache"
    update(store, cache).close()  # headers only
    assert found(config, capsys, "budget") == ["undated", "old"]
