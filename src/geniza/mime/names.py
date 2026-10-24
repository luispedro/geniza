"""Attachment filenames: decoding and sanitising (DESIGN.md, "File naming")."""

import mimetypes
import unicodedata
from email.header import decode_header, make_header

MAX_NAME_BYTES = 120
MAX_EXTENSION_BYTES = 16

# Bidirectional controls can make "invoice<U+202E>fdp.exe" display as
# "invoiceexe.pdf"; they are dropped along with control characters.
_BIDI_CONTROLS = frozenset(
    map(chr, [0x061C, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A)])
)


def _decode_encoded_words(name: str) -> str:
    """Decode RFC 2047 encoded words left in a parameter value.

    The stdlib default policy already decodes them; this covers values that
    came through the compat32 fallback.
    """
    if "=?" not in name:
        return name
    try:
        return str(make_header(decode_header(name)))
    except Exception:
        return name


def _truncate_utf8(text: str, limit: int) -> str:
    return text.encode("utf-8")[:limit].decode("utf-8", "ignore")


def _truncate(name: str, limit: int) -> str:
    if len(name.encode("utf-8")) <= limit:
        return name
    stem, dot, ext = name.rpartition(".")
    suffix = dot + ext
    if stem and len(suffix.encode("utf-8")) <= MAX_EXTENSION_BYTES:
        return _truncate_utf8(stem, limit - len(suffix.encode("utf-8"))) + suffix
    return _truncate_utf8(name, limit)


def sanitize(name: str) -> str:
    """Make a decoded filename safe as the last component of a path.

    NFC-normalises, keeps only what follows the last ``\\`` (Windows
    clients send full paths), removes ``/``, NUL and other control characters
    (including bidirectional controls), strips leading dots and surrounding
    whitespace, and truncates to 120 bytes of UTF-8, keeping the extension.
    May return an empty string.
    """
    name = unicodedata.normalize("NFC", _decode_encoded_words(name))
    name = name.rpartition("\\")[2]
    name = "".join(
        ch
        for ch in name
        if ch != "/"
        and ch not in _BIDI_CONTROLS
        and unicodedata.category(ch) not in ("Cc", "Cs")
    )
    name = name.strip()
    while name.startswith("."):
        name = name.lstrip(".").lstrip()
    return _truncate(name, MAX_NAME_BYTES).rstrip()


def fallback(path: str, content_type: str) -> str:
    """``part-<path>.<ext>``, the name used when a part has no usable one."""
    ext = mimetypes.guess_extension(content_type, strict=False) or ".bin"
    return f"part-{path}{ext}"


def attachment_name(filename: str | None, path: str, content_type: str) -> str:
    return sanitize(filename or "") or fallback(path, content_type)
