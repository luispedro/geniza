"""A MIME part tree over raw bytes, with byte offsets.

The stdlib :mod:`email` package re-serialises messages and does not expose
where each part lives in the original bytes, so this module splits a message
itself and uses :mod:`email` only to decode individual header blocks.

Every node records the byte ranges of its header block and its body. For a
part inside a multipart, the body ends *before* the line ending that precedes
the next delimiter line: per RFC 2046 that line ending belongs to the
delimiter, not to the part.

The parser never raises on malformed input. Structure it cannot make sense of
(a multipart without a usable boundary, nesting that is too deep) becomes an
opaque leaf with a defect recorded, and if anything unexpected goes wrong the
whole message is returned as a single opaque leaf.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage, Message
from email.parser import BytesHeaderParser
from enum import StrEnum

MAX_DEPTH = 32

# A header field line: a field name (printable ASCII except ':') then ':'.
_HEADER_LINE = re.compile(rb"[\x21-\x39\x3b-\x7e]+[ \t]*:")

# Encodings under which a message/rfc822 body is itself a parseable message.
_IDENTITY_ENCODINGS = frozenset({"", "7bit", "8bit", "binary"})


class Kind(StrEnum):
    LEAF = "leaf"
    MULTIPART = "multipart"
    MESSAGE = "message"  # message/rfc822: one child, the embedded message


@dataclass(eq=False)
class Part:
    """One node of the MIME tree.

    ``start <= header_end <= body_start <= end``. ``header_end`` is the end of
    the last header line (including its line ending); between it and
    ``body_start`` is the blank separator line, if there is one.
    """

    path: str
    start: int
    header_end: int
    body_start: int
    end: int
    kind: Kind
    content_type: str
    headers: Message
    children: list[Part] = field(default_factory=list)
    defects: list[str] = field(default_factory=list)
    #: True for the root of a message (the top level or a message/rfc822
    #: body); its headers are message headers.
    is_message_root: bool = False

    @property
    def maintype(self) -> str:
        return self.content_type.partition("/")[0]

    @property
    def body_range(self) -> tuple[int, int]:
        return self.body_start, self.end

    def walk(self) -> Iterator[Part]:
        yield self
        for child in self.children:
            yield from child.walk()

    def leaves(self) -> Iterator[Part]:
        for part in self.walk():
            if part.kind is Kind.LEAF:
                yield part

    def header(self, name: str) -> str | None:
        """A header value as a string, or None if absent or undecodable."""
        try:
            value = self.headers.get(name)
        except Exception:
            return None
        return None if value is None else str(value)

    @property
    def filename(self) -> str | None:
        """The decoded filename (Content-Disposition, then Content-Type name)."""
        try:
            name = self.headers.get_filename()
        except Exception:
            return None
        return None if name is None else str(name)

    @property
    def disposition(self) -> str | None:
        try:
            value = self.headers.get_content_disposition()
        except Exception:
            return None
        return value

    @property
    def transfer_encoding(self) -> str:
        value = self.header("content-transfer-encoding")
        return (value or "").strip().lower()

    @property
    def content_id(self) -> str | None:
        value = self.header("content-id")
        return value.strip() if value else None


def _parse_header_block(data: bytes, start: int, end: int) -> tuple[int, int]:
    """Find the header block of the part spanning ``data[start:end]``.

    Returns ``(header_end, body_start)``. The header block ends at the first
    blank line, which is skipped. A line that is neither a header field nor a
    continuation also ends it; then there is no separator and the body starts
    at that line.
    """
    pos = start
    seen_field = False
    while pos < end:
        nl = data.find(b"\n", pos, end)
        line_end = end if nl < 0 else nl + 1
        line = data[pos:line_end].rstrip(b"\r\n")
        if not line:
            return pos, line_end
        if line[:1] in (b" ", b"\t") and seen_field:
            pass
        elif _HEADER_LINE.match(line):
            seen_field = True
        else:
            return pos, pos
        pos = line_end
    return pos, pos


_header_parser = BytesHeaderParser(policy=policy.default)
_compat_parser = BytesHeaderParser(policy=policy.compat32)


def _decode_headers(raw: bytes) -> Message:
    try:
        msg: Message = _header_parser.parsebytes(raw)
        # Force the headers that are used later to be parsed now, so a
        # header the default policy cannot handle falls back here rather
        # than failing later.
        msg.get_content_type()
        msg.get_filename()
        msg.get_content_disposition()
        return msg
    except Exception:
        return _compat_parser.parsebytes(raw)


def _content_type(headers: Message, default: str) -> str:
    if default != "text/plain":
        headers.set_default_type(default)
    try:
        return headers.get_content_type()
    except Exception:
        return default


def _delimiter_pattern(boundary: bytes) -> re.Pattern[bytes]:
    return re.compile(
        rb"^--" + re.escape(boundary) + rb"(?P<close>--)?[ \t]*\r?$", re.MULTILINE
    )


def _eol_before(data: bytes, pos: int, floor: int) -> int:
    """Start of the line ending just before ``pos`` (never below ``floor``)."""
    if pos - 2 >= floor and data[pos - 2 : pos] == b"\r\n":
        return pos - 2
    if pos - 1 >= floor and data[pos - 1 : pos] == b"\n":
        return pos - 1
    return pos


def _split_multipart(
    data: bytes, body_start: int, end: int, boundary: bytes
) -> list[tuple[int, int]] | None:
    """Byte ranges of the parts of a multipart body, or None if no delimiter
    line occurs. Delimiters after the close delimiter (in the epilogue) are
    ignored; without a close delimiter, the last part runs to ``end``."""
    ranges: list[tuple[int, int]] = []
    part_start: int | None = None
    found = False
    for m in _delimiter_pattern(boundary).finditer(data, body_start, end):
        found = True
        if part_start is not None:
            part_end = max(part_start, _eol_before(data, m.start(), part_start))
            ranges.append((part_start, part_end))
            part_start = None
        if m.group("close"):
            break
        # The delimiter's own line ending; the next part starts after it.
        after = m.end()
        if data[after : after + 1] == b"\n" and after < end:
            after += 1
        part_start = after
    if part_start is not None:
        ranges.append((part_start, end))
    return ranges if found else None


def _child_path(prefix: str, n: int) -> str:
    return f"{prefix}.{n}" if prefix else str(n)


class _Parser:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def message(self, start: int, end: int, prefix: str, depth: int) -> Part:
        """Parse a message (headers + body) spanning ``data[start:end]``.

        ``prefix`` is the IMAP part path of the message: "" at the top level,
        the message/rfc822 part's path otherwise.
        """
        return self.part(start, end, prefix, depth, "text/plain", message_root=True)

    def part(
        self,
        start: int,
        end: int,
        path: str,
        depth: int,
        default_type: str,
        *,
        message_root: bool = False,
    ) -> Part:
        data = self.data
        header_end, body_start = _parse_header_block(data, start, end)
        headers = _decode_headers(data[start:header_end])
        ctype = _content_type(headers, default_type)
        node = Part(
            path=path,
            start=start,
            header_end=header_end,
            body_start=body_start,
            end=end,
            kind=Kind.LEAF,
            content_type=ctype,
            headers=headers,
            is_message_root=message_root,
        )
        maintype = node.maintype
        if maintype == "multipart":
            self._multipart(node, depth)
        elif ctype in ("message/rfc822", "message/global"):
            self._embedded(node, depth)
        # A single-part message is its own (only) leaf; IMAP calls it part 1.
        if message_root and node.kind is Kind.LEAF:
            node.path = _child_path(path, 1)
        return node

    def _multipart(self, node: Part, depth: int) -> None:
        if depth >= MAX_DEPTH:
            node.defects.append("too deeply nested")
            return
        try:
            boundary = node.headers.get_boundary()
        except Exception:
            boundary = None
        if not boundary:
            node.defects.append("multipart without boundary")
            return
        try:
            bnd = boundary.encode("utf-8", "surrogateescape")
        except UnicodeError:
            node.defects.append("undecodable boundary")
            return
        ranges = _split_multipart(self.data, node.body_start, node.end, bnd)
        if ranges is None:
            node.defects.append("boundary not found")
            return
        node.kind = Kind.MULTIPART
        child_default = (
            "message/rfc822"
            if node.content_type == "multipart/digest"
            else "text/plain"
        )
        for i, (s, e) in enumerate(ranges, start=1):
            node.children.append(
                self.part(s, e, _child_path(node.path, i), depth + 1, child_default)
            )

    def _embedded(self, node: Part, depth: int) -> None:
        if node.transfer_encoding not in _IDENTITY_ENCODINGS:
            node.defects.append("encoded message/rfc822")
            return
        if depth >= MAX_DEPTH:
            node.defects.append("too deeply nested")
            return
        node.kind = Kind.MESSAGE
        node.children.append(
            self.message(node.body_start, node.end, node.path, depth + 1)
        )


def parse(data: bytes) -> Part:
    """Parse a raw RFC 5322 message into a part tree. Never raises."""
    try:
        return _Parser(data).message(0, len(data), "", 0)
    except Exception as exc:  # pragma: no cover - defensive
        return opaque(data, f"parser error: {type(exc).__name__}")


def opaque(data: bytes, reason: str) -> Part:
    """The whole message as one opaque leaf: nothing will be extracted."""
    return Part(
        path="1",
        start=0,
        header_end=0,
        body_start=0,
        end=len(data),
        kind=Kind.LEAF,
        content_type="application/octet-stream",
        headers=EmailMessage(),
        defects=[reason],
        is_message_root=True,
    )
