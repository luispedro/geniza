import json
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from geniza.store.journal import (
    Add,
    Drop,
    Event,
    Fate,
    Flags,
    Gone,
    JournalError,
    JournalWriter,
    Mbox,
    Placed,
    Promote,
    Props,
    Purge,
    Remove,
    Reset,
    Setting,
    State,
    Undrop,
    decode_line,
    encode_line,
    journal_files,
    read_events,
    recover,
    replay,
)

T0 = datetime(2026, 10, 24, 9, 0, 3, tzinfo=UTC)
DESIGN = Path(__file__).parent.parent / "DESIGN.md"


class Clock:
    def __init__(self, t: datetime = T0) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t


# Small domains, so that events collide on the same keys.
msgs = st.sampled_from(["sha256:aa", "sha256:bb", "sha256:cc"])
mboxes = st.sampled_from(["INBOX", "Archive", "Mlinkit"])
rids = st.sampled_from(["1", "2", "3"])
flag_sets = st.frozensets(st.sampled_from(["\\Seen", "\\Flagged", "$label1"]))
# Server properties: any JSON object.
json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=5),
    lambda inner: (
        st.lists(inner, max_size=3)
        | st.dictionaries(st.text(max_size=5), inner, max_size=3)
    ),
    max_leaves=6,
)
json_objects = st.dictionaries(st.text(max_size=5), json_values, max_size=3)
times = st.datetimes(
    min_value=datetime(1990, 1, 1),
    max_value=datetime(2030, 1, 1),
    timezones=st.just(UTC),
).map(lambda t: t.replace(microsecond=0))
events = st.one_of(
    st.builds(
        Mbox,
        mbox=mboxes,
        name=st.text(min_size=1, max_size=5),
        role=st.none() | st.just("inbox"),
        parent=st.none() | mboxes,
        sort=st.none() | st.integers(0, 99),
        subscribed=st.none() | st.booleans(),
        props=st.none() | json_objects,
    ),
    st.builds(
        Add,
        msg=msgs,
        mbox=mboxes,
        rid=rids,
        eid=st.none() | st.sampled_from(["E1", "E2"]),
        received=st.none() | times,
        thread=st.none() | st.sampled_from(["T1", "T2"]),
    ),
    st.builds(Flags, msg=msgs, mbox=mboxes, set=flag_sets),
    st.builds(Props, msg=msgs, mbox=st.none() | mboxes, rid=rids, props=json_objects),
    st.builds(
        Setting,
        type=st.sampled_from(["Identity", "SieveScript"]),
        id=st.sampled_from(["S1", "S2"]),
        value=st.none() | json_objects,
        content=st.none() | st.text(max_size=5),
    ),
    st.builds(Remove, msg=msgs, mbox=mboxes, rid=rids),
    st.builds(Reset, mbox=mboxes, uidvalidity=st.integers(0, 3)),
    st.builds(Gone, mbox=mboxes),
    st.builds(
        Promote,
        msg=msgs,
        segment=st.just("segments/2026/2026-10.zst"),
        offset=st.integers(0, 9),
    ),
    st.builds(Drop, msg=msgs),
    st.builds(Undrop, msg=msgs),
    st.builds(Purge, msg=msgs),
)
event_lists = st.lists(events, max_size=30)


def stamp(evs: list[Event], start: int = 1) -> list[Event]:
    return [replace(e, seq=start + i, ts=T0) for i, e in enumerate(evs)]


def state_of(evs: list[Event], state: State | None = None) -> State:
    state = State() if state is None else state
    for e in evs:
        state.apply(e)
    return state


def comparable(state: State) -> dict[str, object]:
    d = dict(vars(state))
    d.pop("seq")
    return d


@given(events)
def test_line_round_trip(event: Event) -> None:
    [event] = stamp([event])
    line = encode_line(event)
    assert line.endswith(b"\n") and line.count(b"\n") == 1
    assert decode_line(line[:-1]) == event


@given(event_lists)
@example(
    [
        Flags(msg="sha256:bb", mbox="INBOX", set=frozenset({"x"})),
        Add(msg="sha256:aa", mbox="INBOX", rid="1"),
        Add(msg="sha256:bb", mbox="INBOX", rid="1"),
    ]
)
def test_replay_is_idempotent(evs: list[Event]) -> None:
    once = state_of(stamp(evs))
    twice = state_of(stamp(evs, start=len(evs) + 1), state_of(stamp(evs)))
    assert comparable(twice) == comparable(once)


@given(event_lists)
def test_state_invariants(evs: list[Event]) -> None:
    state = state_of(stamp(evs))
    inverse: dict[str, dict[str, set[str]]] = {}
    for mbox, contents in state.contents.items():
        assert contents
        for rid, msg in contents.items():
            inverse.setdefault(msg, {}).setdefault(mbox, set()).add(rid)
    assert inverse == state.places
    assert state.placed.keys() == {
        (mbox, rid) for mbox, contents in state.contents.items() for rid in contents
    }
    assert all(state.props.values())


@given(event_lists)
def test_write_read_round_trip(evs: list[Event]) -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        store = Path(d)
        w = JournalWriter(store, "acct", clock=Clock())
        written = w.append(evs[:10]) + w.append(evs[10:])
        assert written == stamp(evs)
        assert list(read_events(store, "acct")) == written
        assert comparable(replay(store, "acct")) == comparable(state_of(written))


def test_design_examples_parse() -> None:
    block = re.search(
        r"## State: the journal.*?```json\n(.*?)```", DESIGN.read_text(), re.S
    )
    assert block
    lines = block[1].strip().splitlines()
    assert len(lines) == 14
    parsed = [
        decode_line(line.replace('"…"', '"2026-10-24T09:00:03Z"').encode())
        for line in lines
    ]
    assert [type(e) for e in parsed] == [
        Mbox,
        Mbox,
        Add,
        Flags,
        Props,
        Remove,
        Reset,
        Gone,
        Promote,
        Drop,
        Undrop,
        Purge,
        Setting,
        Setting,
    ]
    assert parsed[3] == Flags(
        seq=1043,
        ts=T0,
        msg="sha256:…",
        mbox="INBOX",
        set=frozenset({"\\Seen", "$label1"}),
    )


def test_line_format() -> None:
    [e] = stamp([Add(msg="sha256:aa", mbox="Entwürfe", rid="7")])
    line = encode_line(e)
    assert (
        line
        == (
            '{"seq":1,"ts":"2026-10-24T09:00:03Z","op":"add",'
            '"msg":"sha256:aa","mbox":"Entwürfe","rid":"7"}\n'
        ).encode()
    )
    [f] = stamp([Flags(msg="sha256:aa", mbox="A", set=frozenset({"b", "a"}))])
    assert json.loads(encode_line(f))["set"] == ["a", "b"]


@pytest.mark.parametrize(
    "line",
    [
        b"[]",
        b'{"seq":1,"ts":"2026-10-24T09:00:03Z","op":"move","msg":"sha256:aa"}',
        b'{"seq":0,"ts":"2026-10-24T09:00:03Z","op":"drop","msg":"sha256:aa"}',
        b'{"seq":1,"ts":"2026-10-24T09:00:03","op":"drop","msg":"sha256:aa"}',
        b'{"seq":1,"ts":"2026-10-24T09:00:03Z","op":"drop","msg":"md5:aa"}',
        b'{"seq":1,"ts":"2026-10-24T09:00:03Z","op":"drop"}',
        b'{"seq":1,"ts":"2026-10-24T09:00:03Z","op":"drop","msg":"sha256:aa","x":1}',
        b'{"seq":1,"ts":"2026-10-24T09:00:03Z","op":"reset","mbox":"A","uidvalidity":"9"}',
        b'{"seq":1,"ts":"2026-10-24T09:00:03Z","op":"flags","msg":"sha256:aa","mbox":"A","set":"x"}',
        b"\xff",
    ],
)
def test_bad_lines(line: bytes) -> None:
    with pytest.raises(ValueError):
        decode_line(line)


def test_state() -> None:
    a, b = "sha256:aa", "sha256:bb"
    state = state_of(
        stamp(
            [
                Mbox(mbox="INBOX", name="INBOX", role="inbox"),
                Add(msg=a, mbox="INBOX", rid="1", eid="E1"),
                Flags(msg=a, mbox="INBOX", set=frozenset({"\\Seen"})),
                Add(msg=a, mbox="INBOX", rid="1"),
                Add(msg=b, mbox="Archive", rid="5"),
                Remove(msg=a, mbox="INBOX", rid="1"),
                Add(msg=a, mbox="Archive", rid="6"),
                Drop(msg=b),
                Reset(mbox="Archive", uidvalidity=99),
                Add(msg=b, mbox="Archive", rid="1"),
                Undrop(msg=b),
                Promote(msg=a, segment="segments/2026/2026-10.zst", offset=0),
            ]
        )
    )
    assert state.seq == 12
    assert state.contents == {"Archive": {"1": b}}
    assert state.places == {b: {"Archive": {"1"}}}
    assert state.flags_of(a, "INBOX") is None
    assert state.flags == {(a, "INBOX"): frozenset({"\\Seen"})}
    assert state.eids == {"E1": a}
    assert state.mailboxes["INBOX"].role == "inbox"
    assert state.mailboxes["Archive"].uidvalidity == 99
    assert state.fates == {a: Fate("promoted", T0, "segments/2026/2026-10.zst", 0)}


def test_state_of_server_metadata() -> None:
    a = "sha256:aa"
    t = datetime(2026, 10, 24, 8, 59, 40, tzinfo=UTC)
    state = state_of(
        stamp(
            [
                Mbox(mbox="P1", name="Archive", sort=3, props={"x": 1}),
                Mbox(mbox="P2", name="2020", parent="P1", subscribed=True),
                Add(msg=a, mbox="P2", rid="M1", received=t, thread="T1"),
                Add(msg=a, mbox="P1", rid="M1"),
                Props(msg=a, rid="M1", props={"snoozed": None}),
                Setting(
                    type="SieveScript", id="S1", value={"name": "x"}, content="keep;"
                ),
                Setting(type="Identity", id="I1", value={"email": "a@example.org"}),
                Mbox(mbox="P1", name="Old"),
                Setting(type="Identity", id="I1"),
            ]
        )
    )
    assert state.mailboxes["P2"].parent == "P1"
    assert state.mailboxes["P2"].subscribed is True
    # An mbox event says everything about the mailbox.
    assert (state.mailboxes["P1"].sort, state.mailboxes["P1"].props) == (None, None)
    assert state.placed == {("P2", "M1"): Placed(t, "T1"), ("P1", "M1"): Placed()}
    assert state.props == {(None, "M1"): {"snoozed": None}}
    assert state.settings == {("SieveScript", "S1"): ({"name": "x"}, "keep;")}
    state.apply(replace(Props(msg=a, rid="M1", props={}), seq=10, ts=T0))
    assert state.props == {}


def test_gone_mailbox() -> None:
    a, b = "sha256:aa", "sha256:bb"
    state = state_of(
        stamp(
            [
                Mbox(mbox="P1", name="Old"),
                Mbox(mbox="P2", name="Keep"),
                Add(msg=a, mbox="P1", rid="1"),
                Flags(msg=a, mbox="P1", set=frozenset({"x"})),
                Props(msg=a, mbox="P1", rid="1", props={"y": 1}),
                Add(msg=b, mbox="P2", rid="2"),
                Reset(mbox="P1", uidvalidity=7),
                Add(msg=a, mbox="P1", rid="3"),
                Gone(mbox="P1"),
                Gone(mbox="P3"),
            ]
        )
    )
    assert list(state.mailboxes) == ["P2"]
    assert state.contents == {"P2": {"2": b}}
    assert state.places == {b: {"P2": {"2"}}}
    assert state.flags == {} and state.props == {}
    # A mailbox of the same key can come back.
    state.apply(replace(Mbox(mbox="P1", name="New"), seq=11, ts=T0))
    assert state.mailboxes["P1"].name == "New"
    assert state.mailboxes["P1"].uidvalidity is None


def test_readding_keeps_flags() -> None:
    a = "sha256:aa"
    evs = [
        Add(msg=a, mbox="INBOX", rid="1"),
        Flags(msg=a, mbox="INBOX", set=frozenset({"x"})),
    ]
    state = state_of(stamp(evs + evs[:1]))
    assert state.flags_of(a, "INBOX") == frozenset({"x"})
    assert state.flags_of(a, "Archive") is None


def test_empty_flags_are_no_flags() -> None:
    a = "sha256:aa"
    add = Add(msg=a, mbox="INBOX", rid="1")
    none = Flags(msg=a, mbox="INBOX", set=frozenset())
    some = Flags(msg=a, mbox="INBOX", set=frozenset({"x"}))
    assert state_of(stamp([add, none])).flags == {}
    assert state_of(stamp([add, some, none])).flags == {}
    assert state_of(stamp([add, none])).flags_of(a, "INBOX") == frozenset()


def test_monthly_files_and_seq(tmp_path: Path) -> None:
    clock = Clock(datetime(2026, 9, 30, 23, 59, 59, tzinfo=UTC))
    w = JournalWriter(tmp_path, "acct", clock=clock)
    w.append([Drop(msg="sha256:aa")])
    clock.t += timedelta(seconds=1)
    w.append([Drop(msg="sha256:bb"), Undrop(msg="sha256:bb")])
    assert [p.name for p in journal_files(tmp_path, "acct")] == [
        "journal-2026-09.jsonl",
        "journal-2026-10.jsonl",
    ]
    # A new writer continues the sequence.
    [e] = JournalWriter(tmp_path, "acct", clock=clock).append([Purge(msg="sha256:aa")])
    assert e.seq == 4
    assert [e.seq for e in read_events(tmp_path, "acct", after=1)] == [2, 3, 4]
    assert (
        replay(tmp_path, "acct", state_of(list(read_events(tmp_path, "acct"))[:2])).seq
        == 4
    )
    assert w.append([]) == []


def _two_batches(store: Path) -> tuple[Path, int]:
    w = JournalWriter(store, "acct", clock=Clock())
    w.append([Add(msg="sha256:aa", mbox="INBOX", rid="1")])
    path = journal_files(store, "acct")[-1]
    first = path.stat().st_size
    w.append([Add(msg="sha256:bb", mbox="INBOX", rid="2"), Drop(msg="sha256:bb")])
    return path, first


def test_every_truncation_of_the_last_batch(tmp_path: Path) -> None:
    path, first = _two_batches(tmp_path)
    data = path.read_bytes()
    second_line = data.index(b"\n", first) + 1
    for cut in range(first, len(data)):
        path.write_bytes(data[:cut])
        kept = 2 if cut >= second_line else 1
        assert len(list(read_events(tmp_path, "acct"))) == kept
        recover(path)
        assert path.read_bytes() == data[: second_line if kept == 2 else first]
        [e] = JournalWriter(tmp_path, "acct", clock=Clock()).append(
            [Purge(msg="sha256:bb")]
        )
        assert e.seq == kept + 1


def test_zero_filled_tail(tmp_path: Path) -> None:
    path, _ = _two_batches(tmp_path)
    data = path.read_bytes()
    path.write_bytes(data + b"\0" * 4096)
    assert len(list(read_events(tmp_path, "acct"))) == 3
    assert recover(path) == 4096
    assert path.read_bytes() == data
    assert recover(path) == 0


def test_damage_before_the_tail_is_not_truncated(tmp_path: Path) -> None:
    path, first = _two_batches(tmp_path)
    data = bytearray(path.read_bytes())
    data[first + 2] = ord("X")
    path.write_bytes(data)
    with pytest.raises(JournalError) as e:
        list(read_events(tmp_path, "acct"))
    assert e.value.line == 2
    with pytest.raises(JournalError):
        recover(path)
    assert path.read_bytes() == data


def test_torn_tail_only_in_the_last_file(tmp_path: Path) -> None:
    clock = Clock(datetime(2026, 9, 30, tzinfo=UTC))
    w = JournalWriter(tmp_path, "acct", clock=clock)
    w.append([Drop(msg="sha256:aa")])
    old = journal_files(tmp_path, "acct")[0]
    with open(old, "ab") as f:
        f.write(b'{"seq":2,')
    # The next writer repairs it, even though it writes to a new month.
    clock.t = T0
    [e] = JournalWriter(tmp_path, "acct", clock=clock).append([Purge(msg="sha256:aa")])
    assert e.seq == 2
    assert [e.seq for e in read_events(tmp_path, "acct")] == [1, 2]
    with open(old, "ab") as f:
        f.write(b'{"seq":2,')
    with pytest.raises(JournalError):
        list(read_events(tmp_path, "acct"))


def test_seq_must_increase(tmp_path: Path) -> None:
    w = JournalWriter(tmp_path, "acct", clock=Clock())
    w.append([Drop(msg="sha256:aa")])
    w.seq = 0
    w.append([Drop(msg="sha256:bb")])
    with pytest.raises(JournalError, match="seq 1 after 1"):
        list(read_events(tmp_path, "acct"))
