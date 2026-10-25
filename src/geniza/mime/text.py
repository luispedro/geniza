"""The text of a message, for reading and search (DESIGN.md, "Message
text").

The text is that of the body parts (``classify.body_parts``), decoded from
their transfer encoding and charset; HTML is converted to plain text with
the stdlib parser and ``format=flowed`` lines are joined. Within a
multipart/alternative, plain text is preferred to HTML. This is best
effort and never raises: whatever cannot be decoded is replaced, and
control characters (other than tab and newline) are removed, so the text is
safe to print on a terminal.

Nothing here is used to rebuild a message.
"""

import binascii
import re
from dataclasses import dataclass
from html.parser import HTMLParser

from geniza.mime import tree
from geniza.mime.classify import body_parts
from geniza.mime.tree import Kind, Part

#: Charsets decoded as the superset that clients mean by them (as browsers
#: do, after the WHATWG Encoding Standard).
SUPERSETS = {
    "iso-8859-1": "cp1252",
    "latin1": "cp1252",
    "latin-1": "cp1252",
    "iso-8859-9": "cp1254",
    "iso-8859-8-i": "iso-8859-8",
    "iso-8859-11": "cp874",
    "tis-620": "cp874",
    "gb2312": "gb18030",
    "gbk": "gb18030",
    "euc-kr": "cp949",
    "ks_c_5601-1987": "cp949",
    "shift_jis": "cp932",
    "big5": "big5hkscs",
}

#: Labels that say nothing about 8-bit bytes: UTF-8 if it decodes, else
#: Windows-1252.
_UNLABELLED = frozenset(
    {"", "us-ascii", "ascii", "unknown", "unknown-8bit", "x-unknown", "default"}
)

_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_NOT_BASE64 = re.compile(rb"[^A-Za-z0-9+/]")
_META_CHARSET = re.compile(rb"""<meta[^>]*?charset\s*=\s*["']?\s*([\w.:-]+)""", re.I)
#: Where an HTML part's ``<meta>`` charset is looked for.
META_SCAN = 4096


@dataclass(frozen=True)
class MessageText:
    #: "" for the message itself, else the path of the message/rfc822 part.
    path: str
    #: The root of the (attached) message: its headers are message headers.
    root: Part
    text: str


def texts(data: bytes, root: Part | None = None) -> list[MessageText]:
    """The text of a message, then that of each message attached to it
    (at any depth), in byte order."""
    if root is None:
        root = tree.parse(data)
    out = [MessageText("", root, message_text(data, root))]
    for node in root.walk():
        if node.kind is Kind.MESSAGE:
            inner = node.children[0]
            out.append(MessageText(node.path, inner, message_text(data, inner)))
    return out


def message_text(data: bytes, root: Part) -> str:
    """The text of the message whose root is ``root``, without that of
    the messages attached to it."""
    body = body_parts(root)
    return "\n\n".join(t for _, t in _collect(data, root, body))


def _collect(data: bytes, node: Part, body: set[int]) -> list[tuple[str, str]]:
    """The non-blank texts under ``node``, with their content types."""
    match node.kind:
        case Kind.LEAF:
            if id(node) not in body:
                return []
            text = part_text(data, node).strip()
            return [(node.content_type, text)] if text else []
        case Kind.MESSAGE:
            return []
        case Kind.MULTIPART:
            found = [_collect(data, c, body) for c in node.children]
            if node.content_type == "multipart/alternative":
                found = [f for f in found if f]
                plain = [f for f in found if any(t == "text/plain" for t, _ in f)]
                # The last is the richest (RFC 2046, 5.1.4).
                return (plain or found)[-1] if found else []
            return [t for f in found for t in f]


def part_text(data: bytes, part: Part) -> str:
    """The text of one text/plain or text/html leaf."""
    try:
        content = _content(data, part)
        html = part.content_type == "text/html"
        text = decode(content, _charset(part, content, html))
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if html:
            text = html_to_text(text)
        elif _param(part, "format") == "flowed":
            text = unflow(text, delsp=_param(part, "delsp") == "yes")
        return clean(text)
    except Exception:  # best effort: never fail on a body
        return ""


def _content(data: bytes, part: Part) -> bytes:
    body = data[part.body_start : part.end]
    match part.transfer_encoding:
        case "base64":
            # Leniently: stray characters dropped, padding mended.
            b64 = _NOT_BASE64.sub(b"", body)
            if len(b64) % 4 == 1:
                b64 = b64[:-1]
            return binascii.a2b_base64(b64 + b"=" * (-len(b64) % 4))
        case "quoted-printable":
            return binascii.a2b_qp(body)
        case _:
            return body


def _param(part: Part, name: str) -> str | None:
    try:
        value = part.headers.get_param(name)
    except Exception:
        return None
    return value.strip().lower() if isinstance(value, str) else None


def _charset(part: Part, content: bytes, html: bool) -> str:
    charset = _param(part, "charset")
    if not charset and html and (m := _META_CHARSET.search(content[:META_SCAN])):
        charset = m[1].decode("ascii").lower()
    return charset or ""


def decode(content: bytes, charset: str) -> str:
    """``content`` as text in ``charset`` (a label as found in mail),
    with what does not decode replaced."""
    charset = charset.strip().strip("\"'").lower()
    if charset in _UNLABELLED:
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError:
            return content.decode("cp1252", "replace")
    try:
        return content.decode(SUPERSETS.get(charset, charset), "replace")
    # Unknown, not a text encoding ("base64"), or one that cannot replace.
    except LookupError, UnicodeError:
        return decode(content, "")


def clean(text: str) -> str:
    """``text`` without control characters other than tab and newline."""
    return _CONTROLS.sub("", text)


def unflow(text: str, *, delsp: bool) -> str:
    """Join the lines of ``format=flowed`` text (RFC 3676): a line ending
    in a space continues on the next one at the same quote depth."""
    out: list[str] = []
    current: str | None = None
    depth = 0
    for line in text.split("\n"):
        d = len(line) - len(line.lstrip(">"))
        content = line[d:].removeprefix(" ")  # space-stuffing
        soft = content.endswith(" ") and content != "-- "
        if soft and delsp:
            content = content[:-1]
        if current is not None and d == depth:
            current += content
        else:
            if current is not None:
                out.append(_quoted(depth, current))
            current, depth = content, d
        if not soft:
            out.append(_quoted(depth, current))
            current = None
    if current is not None:
        out.append(_quoted(depth, current))
    return "\n".join(out)


def _quoted(depth: int, line: str) -> str:
    return ">" * depth + " " + line if depth else line


def html_to_text(html: str) -> str:
    """Plain text from HTML: the text of the elements, with line breaks
    for block elements and ``<br>``; scripts, styles and titles left out."""
    parser = _HTMLText()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # keep what was converted
        pass
    return parser.text()


#: Elements whose content is not text.
_SKIP = frozenset({"script", "style", "title", "template"})
#: Elements set apart by a blank line.
_PARAGRAPH = frozenset(
    {"p", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "table"}
    | {"ul", "ol", "dl", "hr"}
)
#: Elements that start on a line of their own.
_BLOCK = frozenset(
    {"div", "tr", "li", "dt", "dd", "section", "article", "header", "footer"}
    | {"address", "center", "form", "fieldset", "figure", "main", "nav", "aside"}
    | {"caption", "figcaption", "body", "html"}
)
#: Most line breaks kept in a row: one blank line.
_MAX_BREAKS = 2


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0
        self.pre = 0
        #: Line breaks owed before the next text.
        self.breaks = 0
        #: Whether a space is owed before the next text.
        self.space = False

    def _block(self, breaks: int) -> None:
        self.breaks = max(self.breaks, breaks)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP:
            self.skip += 1
        elif tag == "br":
            self.breaks += 1
        elif tag == "li":
            self._block(1)
            self._emit("- ")
        elif tag in _PARAGRAPH:
            self._block(2)
        elif tag in _BLOCK:
            self._block(1)
        elif tag in ("td", "th"):
            self.space = True
        if tag == "pre":
            self.pre += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in _SKIP:  # <script/> has no content to skip
            self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in _PARAGRAPH:
            self._block(2)
        elif tag in _BLOCK:
            self._block(1)
        if tag == "pre":
            self.pre = max(0, self.pre - 1)

    def handle_data(self, data: str) -> None:
        if self.skip:
            return
        if self.pre:
            self._emit(data.replace("\r\n", "\n"))
            return
        words = data.split()
        if not words:
            self.space = self.space or bool(data)
            return
        if data[0].isspace():
            self.space = True
        self._emit(" ".join(words))
        self.space = data[-1].isspace()

    def _emit(self, text: str) -> None:
        if self.out:
            if self.breaks:
                self.out.append("\n" * min(self.breaks, _MAX_BREAKS))
            elif self.space and not self.out[-1][-1:].isspace():
                self.out.append(" ")
        self.breaks = 0
        self.space = False
        self.out.append(text)

    def text(self) -> str:
        lines = [line.rstrip() for line in "".join(self.out).split("\n")]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
