"""The header fields of a message that the index keeps (DESIGN.md, "Index
(cache)"), read from its header block alone.

Values are decoded for display and search (RFC 2047 words, folded lines
joined); nothing here is used to rebuild a message. Parsing never raises:
a header that cannot be decoded is kept as best it can be, or left out.
"""

import contextlib
import re
from dataclasses import dataclass
from datetime import UTC
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime
from typing import Self

from geniza.mime import tree
from geniza.store.segment import format_time

_MSG_ID = re.compile(r"<([^<>\s]+)>")


@dataclass(frozen=True)
class Envelope:
    date: str | None = None
    from_addr: str | None = None
    to_addr: str | None = None
    cc_addr: str | None = None
    subject: str | None = None
    #: Without the angle brackets.
    message_id: str | None = None
    #: The first message id in ``In-Reply-To``.
    in_reply_to: str | None = None

    @classmethod
    def parse(cls, head: bytes) -> Self:
        """The envelope of a message from its start (at least its header
        block; the rest is ignored)."""
        headers = tree.parse(head).headers
        return cls(
            date=_date(_raw(headers, "Date")),
            from_addr=_joined(headers, "From"),
            to_addr=_joined(headers, "To"),
            cc_addr=_joined(headers, "Cc"),
            subject=_joined(headers, "Subject"),
            message_id=_msg_id(_raw(headers, "Message-ID"), bare=True),
            in_reply_to=_msg_id(_raw(headers, "In-Reply-To"), bare=False),
        )


def _text(value: object) -> str:
    """A header value as display text, on one line."""
    text = str(value)
    if "=?" in text:
        # The compat32 fallback leaves encoded words alone.
        with contextlib.suppress(Exception):
            text = str(make_header(decode_header(text)))
    return _line(text)


def _line(text: str) -> str:
    """``text`` unfolded, and valid Unicode: undecodable bytes the parser
    kept as surrogates are decoded as UTF-8, or replaced."""
    try:
        raw = text.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:
        raw = text.encode("utf-8", "replace")
    return " ".join(raw.decode("utf-8", "replace").split())


def _values(headers: Message, name: str) -> list[str]:
    values = []
    try:
        found = headers.get_all(name) or []
    except Exception:
        return []
    for value in found:
        try:
            text = _text(value)
        except Exception:  # e.g. a header the default policy cannot render
            text = _line(repr(value))
        if text:
            values.append(text)
    return values


def _raw(headers: Message, name: str) -> list[str]:
    """The values of a header as they are in the message, unfolded: for
    fields whose parsed form the default policy may have mended."""
    name = name.lower()
    try:
        return [_line(str(v)) for k, v in headers.raw_items() if k.lower() == name]
    except Exception:
        return []


def _joined(headers: Message, name: str) -> str | None:
    return ", ".join(_values(headers, name)) or None


def _date(values: list[str]) -> str | None:
    """The first ``Date`` that parses, in UTC; one without a zone is taken
    as UTC."""
    for value in values:
        try:
            d = parsedate_to_datetime(value)
            if d.tzinfo is None:
                d = d.replace(tzinfo=UTC)
            return format_time(d)
        except TypeError, ValueError, IndexError, OverflowError:
            continue
    return None


def _msg_id(values: list[str], *, bare: bool) -> str | None:
    """The first ``<id>`` in the values. With ``bare``, a value without
    brackets that is a single word counts too (some clients omit them)."""
    for value in values:
        if m := _MSG_ID.search(value):
            return m[1]
        if bare and value and len(value.split()) == 1:
            return value.strip("<>") or None
    return None
