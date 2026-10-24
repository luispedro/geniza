from datetime import UTC, datetime, timedelta

from hypothesis import given
from hypothesis import strategies as st

from geniza.debug import received_date

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
FALLBACK = datetime(2024, 6, 1, tzinfo=UTC)


def date(data: bytes, hint: datetime | None = None) -> datetime:
    return received_date(data, hint=hint, fallback=FALLBACK, now=NOW)


def test_first_received_line_wins() -> None:
    data = (
        b"Received: from mx.example.org by imap.example.org;\r\n"
        b"\tMon, 3 Mar 2025 10:00:05 +0100\r\n"
        b"Received: from out.example.net by mx.example.org;"
        b" Mon, 3 Mar 2025 09:00:01 +0000\r\n"
        b"Delivery-date: Tue, 4 Mar 2025 00:00:00 +0000\r\n"
        b"Date: Sun, 2 Mar 2025 23:59:00 -0800\r\n"
        b"\r\n"
        b"body\r\n"
    )
    assert date(data) == datetime(2025, 3, 3, 9, 0, 5, tzinfo=UTC)


def test_received_lines_without_a_usable_date_are_skipped() -> None:
    data = (
        b"Received: by localhost (no date)\n"
        b"Received: from a; Thu, 1 Jan 1970 00:00:00 +0000\n"
        b"Received: from b; Sat, 1 Jan 2050 00:00:00 +0000\n"
        b"Received: from c; garbage\n"
        b"Received: from d; Fri, 9 May 2025 08:07:06 -0000\n"
        b"\n"
    )
    assert date(data) == datetime(2025, 5, 9, 8, 7, 6, tzinfo=UTC)


def test_then_delivery_date_hint_and_date() -> None:
    sent = b"Date: Sun, 2 Mar 2025 23:59:00 -0800\n"
    delivery = b"Delivery-date: Tue, 4 Mar 2025 00:00:00 +0000\n"
    hint = datetime(2025, 3, 3, tzinfo=UTC)
    assert date(delivery + sent + b"\n", hint) == datetime(2025, 3, 4, tzinfo=UTC)
    assert date(sent + b"\n", hint) == hint
    assert date(sent + b"\n") == datetime(2025, 3, 3, 7, 59, tzinfo=UTC)
    assert date(sent + b"\n", datetime(1975, 1, 1, tzinfo=UTC)) == datetime(
        2025, 3, 3, 7, 59, tzinfo=UTC
    )


def test_fallback() -> None:
    assert date(b"Subject: no dates\n\n") == FALLBACK
    assert date(b"Date: tomorrow\n\n") == FALLBACK
    assert date(b"") == FALLBACK
    future = (NOW + timedelta(days=2)).strftime("%a, %d %b %Y %H:%M:%S +0000")
    assert date(f"Date: {future}\n\n".encode()) == FALLBACK


def test_only_the_header_block_counts() -> None:
    data = b"Subject: x\n\nReceived: from a; Fri, 9 May 2025 08:07:06 +0000\n"
    assert date(data) == FALLBACK


@given(st.binary(max_size=400))
def test_never_fails(data: bytes) -> None:
    d = date(b"Received: " + data + b"\nDate: " + data[::-1] + b"\n\n" + data)
    assert d.tzinfo is not None
    assert datetime(1980, 1, 1, tzinfo=UTC) <= d <= NOW + timedelta(days=1)
