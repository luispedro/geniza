import hashlib
import mailbox
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import corpus_files
from hypothesis import given
from hypothesis import strategies as st

from geniza.store.segment import (
    HEAD_MAX,
    SEGMENT_CAP,
    Header,
    SegmentError,
    SegmentWriter,
    encode_record,
    escape,
    header_end,
    month_segments,
    read,
    records,
    recover,
    scan,
    scan_heads,
    segment_name,
    segments,
    unescape,
)

T0 = datetime(2026, 10, 23, 14, 2, 11, tzinfo=UTC)

# Lines that mboxrd treats specially, mixed with ordinary ones.
lines = st.lists(
    st.sampled_from(
        [b"From ", b">From ", b">>From x", b"From", b" From ", b"x", b"", b"\r"]
    )
    | st.binary(max_size=20),
    max_size=12,
)
messages = st.builds(
    lambda ls, eol: eol.join(ls), lines, st.sampled_from([b"\n", b"\r\n"])
)


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class Clock:
    def __init__(self, t: datetime = T0) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t


def write(
    store: Path, msgs: list[bytes], cap: int = SEGMENT_CAP
) -> list[tuple[str, int]]:
    with SegmentWriter(store, cap=cap, clock=Clock()) as w:
        return [
            (loc.segment, loc.offset)
            for m in msgs
            for loc in [w.append(m, id=sha(m), size=len(m), received=T0)]
        ]


@given(messages)
def test_escape_round_trip(data: bytes) -> None:
    escaped = escape(data)
    assert unescape(escaped) == data
    assert not any(line.startswith(b"From ") for line in escaped.split(b"\n"))


@given(messages, st.lists(st.dictionaries(st.text(), st.integers()), max_size=3))
def test_record_round_trip(
    tmp_path_factory: pytest.TempPathFactory, data: bytes, parts: list[dict[str, int]]
) -> None:
    path = tmp_path_factory.mktemp("seg") / "s.zst"
    record, header = encode_record(
        data, id=sha(data), size=len(data) + 7, received=T0, stored=T0, parts=parts
    )
    path.write_bytes(record + record)
    assert read(path, 0) == (header, data)
    assert read(path, len(record)) == (header, data)
    assert header.stripped_size == len(data)
    assert header.size == len(data) + 7
    assert list(header.parts) == parts
    assert list(scan(path)) == [(0, header), (len(record), header)]


def test_header_is_compact_utf8_json(tmp_path: Path) -> None:
    record, header = encode_record(
        b"x\n", id=sha(b"x"), size=2, received=T0, stored=T0, parts=[{"file": "ü.pdf"}]
    )
    assert record[:4] == bytes.fromhex("5e2a4d18")
    n = int.from_bytes(record[4:8], "little")
    text = record[8 : 8 + n].decode("utf-8")
    assert text.startswith('{"v":1,"id":"sha256:')
    assert '"received":"2026-10-23T14:02:11Z"' in text
    assert '"ü.pdf"' in text
    assert len(record) == 8 + n + header.frame_size


def test_header_rejects_unknown_version() -> None:
    _, header = encode_record(b"x", id=sha(b"x"), size=1, received=T0, stored=T0)
    obj = header.to_json()
    assert Header.from_json(obj) == header
    with pytest.raises(ValueError, match="version"):
        Header.from_json(obj | {"v": 2})
    with pytest.raises(ValueError, match="size"):
        Header.from_json(obj | {"size": True})


def test_naive_dates_are_rejected() -> None:
    with pytest.raises(ValueError, match="naive"):
        encode_record(
            b"x", id=sha(b"x"), size=1, received=T0.replace(tzinfo=None), stored=T0
        )


@pytest.mark.skipif(shutil.which("zstdcat") is None, reason="no zstdcat")
def test_zstdcat_gives_mboxrd(tmp_path: Path) -> None:
    """A segment read without geniza: zstdcat, then the stdlib mbox reader."""
    msgs = [p.read_bytes() for p in corpus_files()]
    msgs.append(b"Subject: tricky\n\nFrom here\n>From there\n\nFrom \n")
    msgs.append(b"Subject: no final newline\r\n\r\nbody")
    store = tmp_path / "store"
    [(segment, _), *_] = write(store, msgs)
    out = tmp_path / "all.mbox"
    with open(out, "wb") as f:
        subprocess.run(["zstdcat", store / segment], stdout=f, check=True)
    box = mailbox.mbox(out, create=False)
    got = [unescape(box.get_bytes(k)) for k in box.iterkeys()]
    # mbox has no length field: a message without a final newline gains one.
    assert got == [m if m.endswith(b"\n") else m + b"\n" for m in msgs]
    assert box[next(box.iterkeys())].get_from() == "geniza Fri Oct 23 14:02:11 2026"


def test_writer_locations(tmp_path: Path) -> None:
    msgs = [b"Subject: %d\n\nbody\n" % i for i in range(5)]
    locs = write(tmp_path, msgs)
    assert {s for s, _ in locs} == {"segments/2026/2026-10.zst"}
    for (segment, offset), m in zip(locs, msgs, strict=True):
        header, stripped = read(tmp_path / segment, offset)
        assert stripped == m
        assert header.id == sha(m)
        assert header.stored == T0
    path = tmp_path / "segments/2026/2026-10.zst"
    assert [o for o, _ in scan(path)] == [o for _, o in locs]
    assert [m for _, _, m in records(path)] == msgs
    assert [o for o, _ in scan(path, locs[2][1])] == [o for _, o in locs[2:]]


def test_rollover_and_months(tmp_path: Path) -> None:
    clock = Clock()
    msg = b"Subject: x\n\n" + bytes(range(256)) * 4
    with SegmentWriter(tmp_path, cap=3000, clock=clock) as w:
        names = [
            w.append(msg, id=sha(msg), size=1, received=T0).segment for _ in range(5)
        ]
        clock.t = T0 + timedelta(days=10)
        names.append(w.append(msg, id=sha(msg), size=1, received=T0).segment)
        clock.t = datetime(2027, 1, 1, tzinfo=UTC)
        names.append(w.append(msg, id=sha(msg), size=1, received=T0).segment)
    record_size = len(
        encode_record(msg, id=sha(msg), size=1, received=T0, stored=T0)[0]
    )
    per_segment = 3000 // record_size
    assert per_segment >= 1
    assert names[:5] == [segment_name(2026, 10, i // per_segment) for i in range(5)]
    assert names[5:] == ["segments/2026/2026-11.zst", "segments/2027/2027-01.zst"]
    for name in set(names):
        assert (tmp_path / name).stat().st_size <= 3000
    # A new writer continues in the last rollover segment of the month.
    clock.t = T0
    with SegmentWriter(tmp_path, cap=10**6, clock=clock) as w:
        assert w.append(msg, id=sha(msg), size=1, received=T0).segment == names[4]
    assert [i for i, _ in month_segments(tmp_path, 2026, 10)] == sorted(
        {i // per_segment for i in range(5)}
    )


def test_oversized_record_gets_its_own_segment(tmp_path: Path) -> None:
    big = b"".join(hashlib.sha256(bytes([i])).digest() for i in range(200))
    locs = write(tmp_path, [b"x\n", big, b"y\n"], cap=1000)
    assert [s for s, _ in locs] == [
        "segments/2026/2026-10.zst",
        "segments/2026/2026-10.1.zst",
        "segments/2026/2026-10.2.zst",
    ]


def three_records(tmp_path: Path) -> tuple[Path, int, bytes]:
    msgs = [b"Subject: %d\n\nFrom me\n" % i * (i + 1) for i in range(3)]
    locs = write(tmp_path, msgs)
    path = tmp_path / locs[0][0]
    return path, locs[2][1], path.read_bytes()


def test_recover_every_truncation_of_the_last_record(tmp_path: Path) -> None:
    path, last, full = three_records(tmp_path)
    assert recover(path) == 0
    for n in range(last, len(full)):
        path.write_bytes(full[:n])
        assert recover(path) == n - last
        assert path.read_bytes() == full[:last]
        assert len(list(records(path))) == 2


def test_recover_zero_filled_tail(tmp_path: Path) -> None:
    path, last, full = three_records(tmp_path)
    path.write_bytes(full + bytes(4096))
    assert recover(path) == 4096
    assert path.read_bytes() == full
    # The last record's body is there in length but not in content.
    _, body_start = scan_body_start(path, last)
    damaged = full[:body_start] + bytes(len(full) - body_start)
    path.write_bytes(damaged)
    assert recover(path) == len(full) - last
    assert path.read_bytes() == full[:last]


def scan_body_start(path: Path, offset: int) -> tuple[Header, int]:
    header = dict(scan(path))[offset]
    return header, path.stat().st_size - header.frame_size


def test_damage_before_the_tail_is_not_truncated(tmp_path: Path) -> None:
    path, last, full = three_records(tmp_path)
    damaged = bytearray(full)
    damaged[last - 3] ^= 0xFF  # inside the second record's body
    damaged[2] ^= 0xFF  # the first record's magic
    path.write_bytes(damaged)
    with pytest.raises(SegmentError, match="offset 0"):
        recover(path)
    assert path.read_bytes() == damaged
    damaged[2] ^= 0xFF
    path.write_bytes(damaged)
    assert recover(path) == 0  # headers are fine; only the last body is checked
    with pytest.raises(SegmentError, match="bad body frame"):
        list(records(path))


def test_writer_recovers_a_torn_segment(tmp_path: Path) -> None:
    path, last, full = three_records(tmp_path)
    path.write_bytes(full[: last + 10])
    [(segment, offset)] = write(tmp_path, [b"after\n"])
    assert tmp_path / segment == path
    assert offset == last
    assert [m for _, _, m in records(path)][-1] == b"after\n"


def test_writer_recovers_an_earlier_months_segment(tmp_path: Path) -> None:
    path, last, full = three_records(tmp_path)
    path.write_bytes(full[: last + 10])
    november = Clock(datetime(2026, 11, 1, tzinfo=UTC))
    with SegmentWriter(tmp_path, clock=november) as w:
        loc = w.append(b"after\n", id=sha(b"after\n"), size=6, received=T0)
    assert loc.segment == "segments/2026/2026-11.zst"
    assert path.read_bytes() == full[:last]


def test_segments_are_listed_in_order(tmp_path: Path) -> None:
    names = [
        "segments/2025/2025-12.zst",
        "segments/2026/2026-02.zst",
        "segments/2026/2026-02.1.zst",
        "segments/2026/2026-02.10.zst",
        "segments/2026/2026-02.2.zst",
        "segments/2026/2026-10.zst",
    ]
    for name in [*reversed(names), "segments/2026/2025-01.zst", "segments/x.zst"]:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).touch()
    assert [p.relative_to(tmp_path).as_posix() for p in segments(tmp_path)] == [
        names[0],
        names[1],
        names[2],
        names[4],
        names[3],
        names[5],
    ]
    assert segments(tmp_path / "empty") == []


def test_given_month(tmp_path: Path) -> None:
    msgs = [b"Subject: %d\n\nbody\n" % i for i in range(4)]
    months = [(2019, 3), (2026, 10), (2019, 3), None]
    with SegmentWriter(tmp_path, clock=Clock()) as w:
        locs = [
            w.append(m, id=sha(m), size=len(m), received=T0, month=month)
            for m, month in zip(msgs, months, strict=True)
        ]
    assert [loc.segment for loc in locs] == [
        "segments/2019/2019-03.zst",
        "segments/2026/2026-10.zst",
        "segments/2019/2019-03.zst",
        "segments/2026/2026-10.zst",
    ]
    for loc, m in zip(locs, msgs, strict=True):
        header, stripped = read(tmp_path / loc.segment, loc.offset)
        assert stripped == m
        assert header.stored == T0
    assert [m for _, _, m in records(tmp_path / locs[0].segment)] == msgs[0:3:2]


def expected_head(data: bytes) -> bytes:
    end = header_end(data)
    return data[:end] if end is not None and end <= HEAD_MAX else data


@given(st.lists(messages, min_size=1, max_size=4))
def test_scan_heads(
    tmp_path_factory: pytest.TempPathFactory, msgs: list[bytes]
) -> None:
    store = tmp_path_factory.mktemp("store")
    [(name, _), *_] = write(store, msgs)
    heads = list(scan_heads(store / name))
    assert [(o, h) for o, h, _, _ in heads] == list(scan(store / name))
    assert [head for _, _, head, _ in heads] == [expected_head(m) for m in msgs]
    # Each record ends where the next starts, the last at the end of the file.
    ends = [o for o, *_ in heads[1:]] + [(store / name).stat().st_size]
    assert [end for *_, end in heads] == ends


@pytest.mark.parametrize(
    "data",
    [
        b"A: b\n",  # the separator line is not a blank line of the message
        b"A: b\n\n",
        b"A: b\r\n\r\nbody\n" + b"x" * (3 * HEAD_MAX),
        b"From: x\n" + b"X-Long: " + b"y" * (2 * HEAD_MAX) + b"\n\nbody\n",
        b"A: b\n" * (HEAD_MAX // 4) + b"\nbody\n",
    ],
    ids=["no-body", "empty-body", "big-body", "long-line", "big-header"],
)
def test_scan_heads_limits(tmp_path: Path, data: bytes) -> None:
    [(name, _)] = write(tmp_path, [data])
    [(_, _, head, _)] = scan_heads(tmp_path / name)
    assert isinstance(head, bytes)
    assert data.startswith(head)
    if (end := header_end(data)) is not None and end <= HEAD_MAX:
        assert head == data[:end]
    else:
        assert len(head) <= HEAD_MAX and (not head or head.endswith(b"\n"))


def test_scan_heads_damaged_body(tmp_path: Path) -> None:
    msgs = [b"A: 1\n\nbody one\n" * 50, b"A: 2\n\nbody two\n"]
    [(name, _), (_, second)] = write(tmp_path, msgs)
    path = tmp_path / name
    data = bytearray(path.read_bytes())
    data[second - 8 : second - 4] = b"\xff" * 4  # inside the first body frame
    path.write_bytes(data)
    [(_, _, first_head, _), (offset, _, second_head, _)] = scan_heads(path)
    assert isinstance(first_head, SegmentError)
    assert (offset, second_head) == (second, b"A: 2\n\n")
