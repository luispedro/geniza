"""The text of a message: decoding, charsets, flowed text and HTML."""

import base64
import quopri

import pytest
from conftest import CORPUS
from hypothesis import given
from hypothesis import strategies as st

from geniza.mime.text import clean, decode, html_to_text, texts, unflow

#: Text without control characters (other than newline) or surrogates.
TEXT = st.text(
    st.characters(
        exclude_categories=("Cc", "Cs"), include_characters="\n", codec="utf-8"
    )
)


def text_of(name: str) -> list[tuple[str, str]]:
    return [(t.path, t.text) for t in texts((CORPUS / f"{name}.eml").read_bytes())]


def message(headers: str, body: bytes) -> bytes:
    return (
        b"From: a@example.org\r\nSubject: t\r\nMIME-Version: 1.0\r\n"
        + headers.encode("ascii")
        + b"\r\n"
        + body
    )


def test_corpus(corpus_message: tuple[str, bytes]) -> None:
    _, data = corpus_message
    for t in texts(data):
        assert t.text == clean(t.text) == t.text.strip()


def test_plain() -> None:
    [(path, text)] = text_of("plain_text_crlf")
    assert path == ""
    assert text.startswith("Hi Bob,\n\nThis is a plain message.\n")
    assert "\r" not in text
    assert text_of("plain_text_lf") == text_of("plain_text_crlf")


def test_alternative_prefers_plain() -> None:
    assert text_of("html_related_alternative") == [("", "News.")]
    assert text_of("calendar_alternative") == [("", "You are invited.")]


def test_mixed_joins_body_parts() -> None:
    assert text_of("apple_inline") == [
        ("", "Here is the itinerary:\n\nand a photo:\n\nAlice")
    ]


def test_attached_message() -> None:
    assert text_of("forwarded_rfc822") == [
        ("", "Forwarding this."),
        ("2", "Original message text."),
    ]


def test_no_text() -> None:
    assert text_of("single_part_attachment") == [("", "")]
    assert text_of("pgp_encrypted") == [("", "")]


def test_html_meta_charset() -> None:
    assert text_of("html_only_meta_charset") == [
        (
            "",
            "Café & “bistro”\nsecond line\n\nA list:\n\n- one\n- two\n\n"
            "cell 1 cell 2\n\n  kept\n    as is\n\néé end",
        )
    ]


def test_flowed() -> None:
    assert text_of("flowed_unlabelled_utf8") == [
        (
            "",
            "Olá Bob, this paragraph was wrapped by a client that sends flowed"
            " text.\n\n> Quoted text that was wrapped too.\n"
            "From a space-stuffed line.\n-- \nAlice",
        )
    ]


def test_unflow_without_delsp() -> None:
    assert unflow("one \ntwo\n", delsp=False) == "one two\n"
    assert unflow("one \n> two\n", delsp=False) == "one \n> two\n"


@pytest.mark.parametrize(
    ("content", "charset", "text"),
    [
        (b"caf\xc3\xa9", "", "café"),
        (b"caf\xe9", "", "café"),  # not UTF-8: Windows-1252
        (b"caf\xc3\xa9", "us-ascii", "café"),  # mislabelled UTF-8
        (b"\x93q\x94", "iso-8859-1", "“q”"),  # Windows-1252 quotes
        (b"\x93q\x94", '"ISO-8859-1"', "“q”"),
        (b"caf\xe9", "utf-8", "caf\N{REPLACEMENT CHARACTER}"),
        (b"caf\xe9", "x-no-such-charset", "café"),
        (b"caf\xe9", "base64", "café"),  # a codec, not a text encoding
        ("中文".encode("gb18030"), "gb2312", "中文"),
        ("Привет".encode("koi8-r"), "KOI8-R", "Привет"),
    ],
)
def test_decode(content: bytes, charset: str, text: str) -> None:
    assert decode(content, charset) == text


def test_transfer_encodings() -> None:
    plain = "Content-Type: text/plain; charset=utf-8\r\n"
    data = message(
        plain + "Content-Transfer-Encoding: base64\r\n",
        b"Y2Fmw6k",  # no padding
    )
    assert texts(data)[0].text == "café"
    data = message(
        plain + "Content-Transfer-Encoding: base64\r\n",
        b"Y2F\r\n!mw6k=\r\n",  # stray characters
    )
    assert texts(data)[0].text == "café"
    data = message(plain + "Content-Transfer-Encoding: x-uuencode\r\n", b"raw\r\n")
    assert texts(data)[0].text == "raw"


def test_empty_plain_alternative_falls_back_to_html() -> None:
    data = message(
        'Content-Type: multipart/alternative; boundary="b"\r\n',
        b"--b\r\nContent-Type: text/plain\r\n\r\n \r\n"
        b"--b\r\nContent-Type: text/html\r\n\r\n<p>Only here</p>\r\n--b--\r\n",
    )
    assert texts(data)[0].text == "Only here"


def test_controls_removed() -> None:
    esc = chr(0x1B)
    data = message(
        "Content-Type: text/plain\r\n", f"a{esc}[31mred\tb\x00c\r\n".encode()
    )
    assert texts(data)[0].text == "a[31mred\tbc"


def test_html_skips_and_breaks() -> None:
    assert html_to_text("<script/>a<br><br><br>b") == "a\n\nb"
    assert html_to_text("<style>x") == ""
    assert html_to_text("a <b>bold</b>, <i>c</i>") == "a bold, c"
    assert html_to_text("<p>a</p><p>b</p>") == "a\n\nb"
    assert html_to_text("<div>a</div><div>b</div>") == "a\nb"
    assert html_to_text("<ol><li>x<li>y</ol>") == "- x\n- y"


@given(TEXT, st.sampled_from(["base64", "quoted-printable", "8bit"]))
def test_plain_round_trip(text: str, cte: str) -> None:
    raw = text.replace("\n", "\r\n").encode("utf-8")
    match cte:
        case "base64":
            body = base64.encodebytes(raw)
        case "quoted-printable":
            body = quopri.encodestring(raw)
        case _:
            body = raw
    data = message(
        "Content-Type: text/plain; charset=utf-8\r\n"
        f"Content-Transfer-Encoding: {cte}\r\n",
        body,
    )
    assert texts(data)[0].text == text.strip()


@given(TEXT.filter(lambda s: "<" not in s and "&" not in s))
def test_html_plain_text(text: str) -> None:
    assert html_to_text(text) == " ".join(text.split())


@given(st.text())
def test_html_never_raises(html: str) -> None:
    assert html_to_text(html) == html_to_text(html).strip()


@given(st.binary())
def test_never_raises(data: bytes) -> None:
    for t in texts(data):
        assert t.text == clean(t.text)
