import json
import mailbox
import shutil
from pathlib import Path

import pytest
from conftest import corpus_files

from geniza import cli


def make_maildir(root: Path) -> Path:
    """The corpus as an offlineimap-style tree of Maildirs, with one message
    copied into a second folder."""
    for folder in ("INBOX", "Archive.2026"):
        for sub in ("cur", "new", "tmp"):
            (root / folder / sub).mkdir(parents=True)
    for i, path in enumerate(corpus_files()):
        shutil.copy(path, root / "INBOX" / "cur" / f"{i}.host:2,S")
    shutil.copy(corpus_files()[0], root / "Archive.2026" / "new" / "copy.host")
    return root


def run_json(capsys: pytest.CaptureFixture[str], *argv: str) -> dict:
    assert cli.main(["stats", "--parts", "--json", "-j", "1", *argv]) == 0
    return json.loads(capsys.readouterr().out)


def test_maildir(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = make_maildir(tmp_path / "mail")
    r = run_json(capsys, str(root))
    n = len(corpus_files())
    assert r["messages"] == n + 1
    assert r["unique_messages"] == n
    assert r["no_message_id"] == 1
    assert r["duplicate_message_ids"] == 1
    assert r["with_defects"] == 2
    classes = r["classes"]
    assert classes["crypto"]["count"] == 4
    assert classes["decoration"]["count"] == 4
    att = classes["attachment"]
    assert att["modes"] == {"stub": 32, "copy": 5, "inline": 1}
    # The forwarded PDF is the same blob as the original.
    assert r["attachments"]["unique_blobs"] == r["attachments"]["occurrences"] - 1
    # With every decoration below 16 KiB, no threshold extracts one.
    assert {t["decoration_files"] for t in r["thresholds"]} == {0}
    assert all(t["inline_bytes"] < r["unique_bytes"] for t in r["thresholds"])


def test_mbox_matches_maildir(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    box = mailbox.mbox(tmp_path / "corpus.mbox")
    for path in corpus_files():
        box.add(path.read_bytes())
    box.close()
    r = run_json(capsys, str(tmp_path / "corpus.mbox"))
    assert r["messages"] == len(corpus_files())
    assert r["classes"]["attachment"]["modes"]["stub"] == 32


def test_parallel_matches_serial(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_maildir(tmp_path / "mail")
    serial = run_json(capsys, str(root))
    assert cli.main(["stats", "--parts", "--json", "-j", "2", str(root)]) == 0
    assert json.loads(capsys.readouterr().out) == serial


def test_output_identifies_nothing_without_examples(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_maildir(tmp_path / "mail")
    for extra in ([], ["--json"]):
        assert cli.main(["stats", "--parts", "-j", "1", *extra, str(root)]) == 0
        text = capsys.readouterr().out
        assert "corpus-" not in text and "example.org" not in text
        assert str(tmp_path) not in text
    assert cli.main(["stats", "--parts", "-j", "1", "--examples", str(root)]) == 0
    assert "<corpus-0013@example.org>" in capsys.readouterr().out


def test_missing_path(tmp_path: Path) -> None:
    assert cli.main(["stats", "--parts", str(tmp_path / "nope")]) == 1
    assert cli.main(["stats", "--parts", str(tmp_path)]) == 1
