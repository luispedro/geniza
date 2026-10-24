import logging
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from conftest import corpus_files
from hypothesis import given, settings
from hypothesis import strategies as st

from geniza.mime.classify import PartClass
from geniza.mime.recipes import Mode, Policy
from geniza.mime.strip import reconstruct
from geniza.store import attachments, segment
from geniza.store.ingest import Catalog, Ingester, message_id
from geniza.store.lock import StoreLock
from geniza.store.parts import PartRecord, parse_parts, stubs

T0 = datetime(2026, 10, 23, 14, 2, 11, tzinfo=UTC)
EXTRACT_ALL = Policy(min_size=1, decoration_min_size=1)
CORPUS = {p.stem: p.read_bytes() for p in corpus_files()}


class Clock:
    def __init__(self, t: datetime = T0) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t


def ingester(
    store: Path, policy: Policy = EXTRACT_ALL, **kw: Any
) -> Iterator[Ingester]:
    with StoreLock(store) as lock, Ingester(store, lock, policy=policy, **kw) as ing:
        yield ing


@pytest.fixture
def ing(tmp_path: Path) -> Iterator[Ingester]:
    yield from ingester(tmp_path, clock=Clock())


def rebuild(store: Path, name: str, offset: int) -> bytes:
    """The original message of a record, from its header and files."""
    header, stripped = segment.read(store / name, offset)
    parts = parse_parts(header.parts, header.stripped_size)
    contents = [
        attachments.read(store, p.file, p.blob) for p in parts if p.recipe is not None
    ]
    data = reconstruct(stripped, stubs(parts), contents)
    assert message_id(data) == header.id
    return data


def test_corpus_message_rebuilds(
    tmp_path: Path, corpus_message: tuple[str, bytes]
) -> None:
    _, data = corpus_message
    for policy in (EXTRACT_ALL, Policy()):
        store = tmp_path / str(policy.decoration_min_size)
        for ing in ingester(store, policy):
            added = ing.add(data, T0)
        assert added.new
        assert not added.fallback
        assert rebuild(store, added.segment, added.offset) == data
        header, stripped = segment.read(store / added.segment, added.offset)
        assert header.size == len(data)
        assert header.received == T0
        assert list(header.parts) == [p.to_json() for p in added.parts]
        for p in added.parts:
            assert attachments.read(store, p.file, p.blob)
            if p.mode is Mode.STUB:
                assert stripped[p.offset : p.end].startswith(b"[geniza: stored as ")


def test_parts_are_extracted(ing: Ingester) -> None:
    added = ing.add(CORPUS["base64_irregular"], T0)
    assert [(p.path, p.mode) for p in added.parts] == [
        ("2", Mode.COPY),
        ("3", Mode.COPY),
    ]
    added = ing.add(CORPUS["outlook_related"], T0)
    assert {p.kind for p in added.parts} >= {PartClass.DECORATION}


def test_duplicate_message_is_stored_once(ing: Ingester) -> None:
    data = CORPUS["base64_76_crlf"]
    first = ing.add(data, T0)
    again = ing.add(data, T0)
    assert not again.new
    assert (again.segment, again.offset) == (first.segment, first.offset)
    assert len(list(segment.scan(ing.store / first.segment))) == 1


def test_blob_is_written_once(ing: Ingester) -> None:
    data = CORPUS["base64_76_crlf"]
    other = data.replace(b"Subject: ", b"Subject: Re: ", 1)
    assert other != data
    a, b = ing.add(data, T0), ing.add(other, T0)
    [pa], [pb] = a.parts, b.parts
    assert (pa.blob, pa.file) == (pb.blob, pb.file)
    assert len(list(attachments.scan(ing.store))) == 1
    assert rebuild(ing.store, b.segment, b.offset) == other


def test_blob_from_an_earlier_month_is_reused(tmp_path: Path) -> None:
    data = CORPUS["base64_76_crlf"]
    other = data.replace(b"Subject: ", b"Subject: Re: ", 1)
    for ing in ingester(tmp_path, clock=Clock()):
        first = ing.add(data, T0)
    for ing in ingester(tmp_path, clock=Clock(datetime(2026, 11, 2, tzinfo=UTC))):
        second = ing.add(other, T0)
    assert second.segment == "segments/2026/2026-11.zst"
    assert second.parts[0].file == first.parts[0].file
    assert not (tmp_path / "attachments/2026/11").exists()


def test_catalog_scan_matches_ingest(tmp_path: Path) -> None:
    for ing in ingester(tmp_path, clock=Clock()):
        for data in CORPUS.values():
            ing.add(data, T0)
        built = ing.catalog
    assert Catalog.scan(tmp_path) == built
    assert set(built.messages) == {message_id(d) for d in CORPUS.values()}
    for ing in ingester(tmp_path, clock=Clock()):
        assert ing.catalog == built
        assert not ing.add(CORPUS["plain_text_lf"], T0).new


def test_header_that_does_not_rebuild_falls_back_to_copies(
    ing: Ingester, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    to_json = PartRecord.to_json

    def wrong(self: PartRecord) -> dict[str, Any]:
        obj = to_json(self)
        if "final_eol" in obj:
            obj["final_eol"] = not obj["final_eol"]
        return obj

    monkeypatch.setattr(PartRecord, "to_json", wrong)
    data = CORPUS["outlook_related"]
    with caplog.at_level(logging.WARNING):
        added = ing.add(data, T0)
    assert added.fallback
    assert "does not rebuild" in caplog.text
    assert added.parts
    assert all(p.mode is Mode.COPY for p in added.parts)
    assert all(p.kind is PartClass.ATTACHMENT for p in added.parts)
    _, stripped = segment.read(ing.store / added.segment, added.offset)
    assert stripped == data
    assert rebuild(ing.store, added.segment, added.offset) == data


def test_header_that_does_not_parse_falls_back_to_copies(
    ing: Ingester, monkeypatch: pytest.MonkeyPatch
) -> None:
    to_json = PartRecord.to_json

    def lacking(self: PartRecord) -> dict[str, Any]:
        obj = to_json(self)
        obj.pop("final_eol", None)
        return obj

    monkeypatch.setattr(PartRecord, "to_json", lacking)
    added = ing.add(CORPUS["base64_76_crlf"], T0)
    assert added.fallback
    assert [p.mode for p in added.parts] == [Mode.COPY]


def test_missing_attachment_file_is_an_error(tmp_path: Path) -> None:
    data = CORPUS["base64_76_crlf"]
    other = data.replace(b"Subject: ", b"Subject: Re: ", 1)
    for ing in ingester(tmp_path, clock=Clock()):
        [part] = ing.add(data, T0).parts
        (tmp_path / part.file).unlink()
        with pytest.raises(FileNotFoundError):
            ing.add(other, T0)
    assert len(list(segment.scan(tmp_path / "segments/2026/2026-10.zst"))) == 1


def test_ingest_needs_the_lock(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="lock"):
        Ingester(tmp_path, StoreLock(tmp_path))
    with StoreLock(tmp_path / "other") as lock, pytest.raises(RuntimeError):
        Ingester(tmp_path, lock)


def test_torn_record_is_removed_before_the_catalog_is_built(tmp_path: Path) -> None:
    for ing in ingester(tmp_path, clock=Clock()):
        a = ing.add(CORPUS["plain_text_lf"], T0)
        b = ing.add(CORPUS["base64_76_crlf"], T0)
    path = tmp_path / a.segment
    path.write_bytes(path.read_bytes()[: b.offset + 20])
    for ing in ingester(tmp_path, clock=Clock(datetime(2026, 11, 2, tzinfo=UTC))):
        assert set(ing.catalog.messages) == {a.id}
        again = ing.add(CORPUS["base64_76_crlf"], T0)
    assert again.new
    assert again.segment == "segments/2026/2026-11.zst"
    assert path.stat().st_size == b.offset


@settings(max_examples=25, deadline=None)
@given(st.lists(st.sampled_from(sorted(CORPUS)), max_size=8), st.integers(1, 6000))
def test_any_sequence_of_messages_rebuilds(names: list[str], cap: int) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = Path(tmp)
        where: dict[str, tuple[str, int]] = {}
        for ing in ingester(store, cap=cap, clock=Clock()):
            for name in names:
                added = ing.add(CORPUS[name], T0)
                assert added.new == (name not in where)
                where.setdefault(name, (added.segment, added.offset))
        for name, (seg, offset) in where.items():
            assert rebuild(store, seg, offset) == CORPUS[name]
        assert Catalog.scan(store).messages == {
            message_id(CORPUS[n]): w for n, w in where.items()
        }
