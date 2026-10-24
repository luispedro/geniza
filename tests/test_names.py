import unicodedata

import pytest
from hypothesis import given
from hypothesis import strategies as st

from geniza.mime.names import MAX_NAME_BYTES, attachment_name, fallback, sanitize


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("report.pdf", "report.pdf"),
        ("../../../etc/passwd", "etcpasswd"),
        ("bell\x07esc\x1b.txt", "bellesc.txt"),
        ("nul\x00byte.txt", "nulbyte.txt"),
        ("...hidden", "hidden"),
        (". .x", "x"),
        ("..", ""),
        ("", ""),
        ("invoice\u202efdp.exe", "invoicefdp.exe"),
        ("cafe\u0301.txt", "caf\u00e9.txt"),
        ("=?UTF-8?B?0J7RgtGH0LXRgi5wZGY=?=", "Отчет.pdf"),
        ("  spaced.txt  ", "spaced.txt"),
    ],
)
def test_sanitize(raw: str, expected: str) -> None:
    assert sanitize(raw) == expected


def test_long_names_keep_extension() -> None:
    name = sanitize("x" * 300 + ".pdf")
    assert name.endswith(".pdf") and len(name.encode()) == MAX_NAME_BYTES
    multibyte = sanitize("é" * 200 + ".txt")
    assert multibyte.endswith(".txt") and len(multibyte.encode()) <= MAX_NAME_BYTES


def test_fallback() -> None:
    assert fallback("1.2", "image/png") == "part-1.2.png"
    assert fallback("3", "application/x-geniza-unknown") == "part-3.bin"
    assert attachment_name("..", "7", "application/pdf") == "part-7.pdf"
    assert attachment_name(None, "2", "application/pdf") == "part-2.pdf"


@given(st.text(max_size=400))
def test_sanitize_is_always_safe(raw: str) -> None:
    name = sanitize(raw)
    assert "/" not in name and "\x00" not in name
    assert not name.startswith(".")
    assert len(name.encode("utf-8")) <= MAX_NAME_BYTES
    assert not any(unicodedata.category(c) == "Cc" for c in name)
    assert unicodedata.normalize("NFC", name) == name or not name
    assert sanitize(name) == name  # idempotent
