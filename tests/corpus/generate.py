"""Generate the synthetic test corpus.

Every message here is invented. Run from the repository root with
``pixi run python tests/corpus/generate.py``; the output is committed, and
``tests/test_corpus.py`` checks that it is up to date.

Messages are assembled by hand, not with ``email.generator``, so that line
endings, base64 line lengths and boundary placement are exactly what each
case needs.
"""

import base64
import quopri
import random
import sys
from collections.abc import Callable
from pathlib import Path

HERE = Path(__file__).parent

CASES: dict[str, Callable[[], bytes]] = {}


def case(name: str) -> Callable[[Callable[[], bytes]], Callable[[], bytes]]:
    def register(fn: Callable[[], bytes]) -> Callable[[], bytes]:
        CASES[name] = fn
        return fn

    return register


def blob(seed: int, size: int, magic: bytes = b"") -> bytes:
    """Deterministic pseudo-random content standing in for a binary file."""
    rng = random.Random(seed)
    return magic + rng.randbytes(size - len(magic))


def pdf(seed: int, size: int = 3000) -> bytes:
    return blob(seed, size, b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")


def png(seed: int, size: int = 1200) -> bytes:
    return blob(seed, size, b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")


def jpeg(seed: int, size: int = 2000) -> bytes:
    return blob(seed, size, b"\xff\xd8\xff\xe0\x00\x10JFIF\x00")


def b64(data: bytes, line: int = 76, eol: str = "\r\n", final_eol: bool = True) -> str:
    """Base64 with a given line length, line ending and final line ending."""
    encoded = base64.b64encode(data).decode("ascii")
    lines = [encoded[i : i + line] for i in range(0, len(encoded), line)]
    return eol.join(lines) + (eol if final_eol else "")


def qp(text: str) -> str:
    return quopri.encodestring(text.encode("utf-8")).decode("ascii")


def crlf(text: str) -> bytes:
    """Encode a template written with LF, converting every LF to CRLF."""
    return text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8")


def lf(text: str) -> bytes:
    return text.encode("utf-8")


def headers(
    subject: str,
    msgid: str | None,
    date: str = "Thu, 22 Oct 2026 10:15:00 +0000",
    sender: str = "Alice Example <alice@example.org>",
    to: str = "Bob Example <bob@example.net>",
) -> str:
    lines = [
        "Received: from mx.example.org (mx.example.org [192.0.2.10])",
        "\tby mail.example.net with ESMTPS id 4Xa9bC1d2Ez;",
        f"\t{date}",
        f"From: {sender}",
        f"To: {to}",
        f"Subject: {subject}",
        f"Date: {date}",
    ]
    if msgid is not None:
        lines.append(f"Message-ID: {msgid}")
    lines.append("MIME-Version: 1.0")
    return "\n".join(lines) + "\n"


def mid(n: int) -> str:
    return f"<corpus-{n:04d}@example.org>"


SIMPLE_TEXT = """\
Hi Bob,

This is a plain message.
From the corpus: a line starting with From, to exercise mbox escaping.

Alice
"""


@case("plain_text_crlf")
def plain_text_crlf() -> bytes:
    return crlf(
        headers("Plain text", mid(1))
        + "Content-Type: text/plain; charset=utf-8\n"
        + "Content-Transfer-Encoding: 7bit\n\n"
        + SIMPLE_TEXT
    )


@case("plain_text_lf")
def plain_text_lf() -> bytes:
    return lf(
        headers("Plain text, LF", mid(2))
        + "Content-Type: text/plain; charset=utf-8\n\n"
        + SIMPLE_TEXT
    )


@case("outlook_related")
def outlook_related() -> bytes:
    """Outlook: signature images named image00N.png, with Content-IDs, in
    multipart/related, next to a real attachment."""
    html = (
        "<html><body><p>Please see the attached report.</p>"
        '<img src="cid:image001.png@01DB0001.ABCDEF00">'
        '<img src="cid:image002.jpg@01DB0001.ABCDEF00"></body></html>\n'
    )
    return crlf(
        headers("Quarterly report", mid(3))
        + 'Content-Type: multipart/mixed;\n\tboundary="_004_OUTLOOK_MIXED_"\n'
        + "\n"
        + "--_004_OUTLOOK_MIXED_\n"
        + 'Content-Type: multipart/related;\n\tboundary="_003_OUTLOOK_REL_";\n'
        + '\ttype="multipart/alternative"\n'
        + "\n"
        + "--_003_OUTLOOK_REL_\n"
        + 'Content-Type: multipart/alternative;\n\tboundary="_000_OUTLOOK_ALT_"\n'
        + "\n"
        + "--_000_OUTLOOK_ALT_\n"
        + 'Content-Type: text/plain; charset="us-ascii"\n'
        + "Content-Transfer-Encoding: quoted-printable\n\n"
        + "Please see the attached report.\n\n"
        + "--_000_OUTLOOK_ALT_\n"
        + 'Content-Type: text/html; charset="us-ascii"\n'
        + "Content-Transfer-Encoding: quoted-printable\n\n"
        + qp(html).replace("\r\n", "\n")
        + "\n"
        + "--_000_OUTLOOK_ALT_--\n"
        + "\n"
        + "--_003_OUTLOOK_REL_\n"
        + 'Content-Type: image/png; name="image001.png"\n'
        + "Content-Description: image001.png\n"
        + 'Content-Disposition: inline; filename="image001.png"; size=1200\n'
        + "Content-ID: <image001.png@01DB0001.ABCDEF00>\n"
        + "Content-Transfer-Encoding: base64\n\n"
        + b64(png(31), eol="\n")
        + "\n"
        + "--_003_OUTLOOK_REL_\n"
        + 'Content-Type: image/jpeg; name="image002.jpg"\n'
        + 'Content-Disposition: inline; filename="image002.jpg"\n'
        + "Content-ID: <image002.jpg@01DB0001.ABCDEF00>\n"
        + "Content-Transfer-Encoding: base64\n\n"
        + b64(jpeg(32), eol="\n")
        + "\n"
        + "--_003_OUTLOOK_REL_--\n"
        + "\n"
        + "--_004_OUTLOOK_MIXED_\n"
        + 'Content-Type: application/pdf; name="quarterly-report.pdf"\n'
        + 'Content-Disposition: attachment; filename="quarterly-report.pdf"\n'
        + "Content-Transfer-Encoding: base64\n\n"
        + b64(pdf(1), eol="\n")
        + "\n"
        + "--_004_OUTLOOK_MIXED_--\n"
    )


@case("apple_inline")
def apple_inline() -> bytes:
    """Apple Mail: real attachments marked inline, text split around them."""
    return crlf(
        headers("Photos from the trip", mid(4))
        + 'Content-Type: multipart/mixed;\n\tboundary="Apple-Mail=_A1B2C3D4-0000"\n'
        + "\n"
        + "\n"
        + "--Apple-Mail=_A1B2C3D4-0000\n"
        + "Content-Transfer-Encoding: 7bit\n"
        + "Content-Type: text/plain;\n\tcharset=us-ascii\n\n"
        + "Here is the itinerary:\n\n"
        + "--Apple-Mail=_A1B2C3D4-0000\n"
        + "Content-Disposition: inline;\n\tfilename=itinerary.pdf\n"
        + 'Content-Type: application/pdf;\n\tx-unix-mode=0644;\n\tname="itinerary.pdf"\n'
        + "Content-Transfer-Encoding: base64\n\n"
        + b64(pdf(2))
        + "--Apple-Mail=_A1B2C3D4-0000\n"
        + "Content-Transfer-Encoding: 7bit\n"
        + "Content-Type: text/plain;\n\tcharset=us-ascii\n\n"
        + "\nand a photo:\n\n"
        + "--Apple-Mail=_A1B2C3D4-0000\n"
        + "Content-Disposition: inline;\n\tfilename=IMG_0042.jpg\n"
        + 'Content-Type: image/jpeg;\n\tx-unix-mode=0644;\n\tname="IMG_0042.jpg"\n'
        + "Content-Transfer-Encoding: base64\n\n"
        + b64(jpeg(3))
        + "--Apple-Mail=_A1B2C3D4-0000\n"
        + "Content-Transfer-Encoding: 7bit\n"
        + "Content-Type: text/plain;\n\tcharset=us-ascii\n\n"
        + "\nAlice\n"
        + "--Apple-Mail=_A1B2C3D4-0000--\n"
    )


@case("inline_image_no_name")
def inline_image_no_name() -> bytes:
    """A pasted screenshot: inline image/* without a filename, outside
    multipart/related."""
    return crlf(
        headers("Screenshot", mid(5))
        + 'Content-Type: multipart/mixed; boundary="sep"\n\n'
        + "--sep\n"
        + "Content-Type: text/plain\n\n"
        + "See below.\n"
        + "--sep\n"
        + "Content-Type: image/png\n"
        + "Content-Disposition: inline\n"
        + "Content-Transfer-Encoding: base64\n\n"
        + b64(png(5))
        + "--sep--\n"
    )


@case("pgp_signed")
def pgp_signed() -> bytes:
    """PGP/MIME signed message with a patch and a PDF inside multipart/signed."""
    patch = (
        "diff --git a/hello.py b/hello.py\n"
        "--- a/hello.py\n"
        "+++ b/hello.py\n"
        "@@ -1 +1 @@\n"
        '-print("hello")\n'
        '+print("hello, world")\n'
    )
    signature = (
        "-----BEGIN PGP SIGNATURE-----\n\n"
        + b64(blob(60, 300), line=64, eol="\n")
        + "=AbCd\n-----END PGP SIGNATURE-----\n"
    )
    return crlf(
        headers("[PATCH] greet the world", mid(6))
        + "Content-Type: multipart/signed; micalg=pgp-sha256;\n"
        + '\tprotocol="application/pgp-signature";\n'
        + '\tboundary="==SIGNED=="\n'
        + "\n"
        + "This is an OpenPGP/MIME signed message (RFC 4880 and 3156)\n"
        + "--==SIGNED==\n"
        + 'Content-Type: multipart/mixed; boundary="==MIXED=="\n'
        + "\n"
        + "--==MIXED==\n"
        + "Content-Type: text/plain; charset=UTF-8\n"
        + "Content-Transfer-Encoding: 7bit\n\n"
        + "Patch attached, and the design notes.\n"
        + "\n"
        + "--==MIXED==\n"
        + 'Content-Type: text/x-patch; charset=UTF-8; name="0001-greet.patch"\n'
        + 'Content-Disposition: attachment; filename="0001-greet.patch"\n'
        + "Content-Transfer-Encoding: 7bit\n\n"
        + patch
        + "\n"
        + "--==MIXED==\n"
        + 'Content-Type: application/pdf; name="notes.pdf"\n'
        + 'Content-Disposition: attachment; filename="notes.pdf"\n'
        + "Content-Transfer-Encoding: base64\n\n"
        + b64(pdf(6), eol="\n")
        + "\n"
        + "--==MIXED==--\n"
        + "\n"
        + "--==SIGNED==\n"
        + 'Content-Type: application/pgp-signature; name="signature.asc"\n'
        + "Content-Description: OpenPGP digital signature\n"
        + 'Content-Disposition: attachment; filename="signature.asc"\n\n'
        + signature
        + "\n"
        + "--==SIGNED==--\n"
    )


@case("smime_signed")
def smime_signed() -> bytes:
    """S/MIME signed message: the signature has a filename but is crypto."""
    return crlf(
        headers("Signed spreadsheet", mid(7))
        + 'Content-Type: multipart/signed; protocol="application/pkcs7-signature";\n'
        + '\tmicalg=sha-256; boundary="----SMIME0123456789"\n'
        + "\n"
        + "This is an S/MIME signed message\n"
        + "\n"
        + "------SMIME0123456789\n"
        + 'Content-Type: multipart/mixed; boundary="----MIXED9876543210"\n'
        + "\n"
        + "------MIXED9876543210\n"
        + "Content-Type: text/plain; charset=utf-8\n\n"
        + "Numbers attached.\n"
        + "\n"
        + "------MIXED9876543210\n"
        + "Content-Type: application/vnd.ms-excel; name=budget.xls\n"
        + "Content-Transfer-Encoding: base64\n"
        + "Content-Disposition: attachment; filename=budget.xls\n\n"
        + b64(blob(7, 2500, b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"), line=64)
        + "\n"
        + "------MIXED9876543210--\n"
        + "\n"
        + "------SMIME0123456789\n"
        + 'Content-Type: application/pkcs7-signature; name="smime.p7s"\n'
        + "Content-Transfer-Encoding: base64\n"
        + 'Content-Disposition: attachment; filename="smime.p7s"\n'
        + "Content-Description: S/MIME Cryptographic Signature\n\n"
        + b64(blob(70, 1800, b"0\x82\x07\x14\x06\t*\x86H\x86\xf7\r\x01\x07\x02"), 64)
        + "\n"
        + "------SMIME0123456789--\n"
        + "\n"
    )


@case("pgp_encrypted")
def pgp_encrypted() -> bytes:
    """PGP/MIME encrypted: the ciphertext part has a filename, but it is
    part of the encryption structure, not an attachment."""
    armored = (
        "-----BEGIN PGP MESSAGE-----\n\n"
        + b64(blob(8, 900), line=64, eol="\n")
        + "=WxYz\n-----END PGP MESSAGE-----\n"
    )
    return crlf(
        headers("Encrypted", mid(8))
        + 'Content-Type: multipart/encrypted; protocol="application/pgp-encrypted";\n'
        + '\tboundary="==ENC=="\n'
        + "\n"
        + "This is an OpenPGP/MIME encrypted message (RFC 4880 and 3156)\n"
        + "--==ENC==\n"
        + "Content-Type: application/pgp-encrypted\n"
        + "Content-Description: PGP/MIME version identification\n\n"
        + "Version: 1\n\n"
        + "--==ENC==\n"
        + 'Content-Type: application/octet-stream; name="encrypted.asc"\n'
        + "Content-Description: OpenPGP encrypted message\n"
        + 'Content-Disposition: inline; filename="encrypted.asc"\n\n'
        + armored
        + "\n"
        + "--==ENC==--\n"
    )


@case("qp_text_attachments")
def qp_text_attachments() -> bytes:
    """Small text attachments in quoted-printable (always copy mode)."""
    script = (
        "#!/usr/bin/env python3\n"
        "# A script with a long line that quoted-printable has to wrap with a "
        "soft line break somewhere around here.\n"
        'print("caf\u00e9 = 3")\n'
    )
    patch = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a = 1\n+a = 2  \n"
    return crlf(
        headers("Two small files", mid(9))
        + 'Content-Type: multipart/mixed; boundary="qpqpqp"\n\n'
        + "--qpqpqp\n"
        + "Content-Type: text/plain; charset=utf-8\n\n"
        + "Script and patch.\n\n"
        + "--qpqpqp\n"
        + 'Content-Type: text/x-python; charset=utf-8; name="script.py"\n'
        + 'Content-Disposition: attachment; filename="script.py"\n'
        + "Content-Transfer-Encoding: quoted-printable\n\n"
        + qp(script)
        + "\n"
        + "--qpqpqp\n"
        + 'Content-Type: text/x-diff; charset=us-ascii; name="fix.patch"\n'
        + 'Content-Disposition: attachment; filename="fix.patch"\n'
        + "Content-Transfer-Encoding: quoted-printable\n\n"
        + qp(patch)
        + "\n"
        + "--qpqpqp--\n"
    )


@case("identity_text_attachments")
def identity_text_attachments() -> bytes:
    """7bit/8bit text attachments: consistent CRLF (stub, written with LF)
    and mixed line endings (copy)."""
    notes = "# Notes\n\n- one\n- two\n"
    mixed = "line one\r\nline two\nline three\r\n"
    utf8 = "Gr\u00fc\u00dfe aus K\u00f6ln\n"
    body = (
        headers("Notes", mid(10))
        + 'Content-Type: multipart/mixed; boundary="idid"\n\n'
        + "--idid\n"
        + "Content-Type: text/plain; charset=utf-8\n\n"
        + "Three files.\n\n"
        + "--idid\n"
        + 'Content-Type: text/markdown; charset=us-ascii; name="notes.md"\n'
        + 'Content-Disposition: attachment; filename="notes.md"\n'
        + "Content-Transfer-Encoding: 7bit\n\n"
        + notes
        + "\n"
        + "--idid\n"
        + 'Content-Type: text/plain; charset=utf-8; name="greeting.txt"\n'
        + 'Content-Disposition: attachment; filename="greeting.txt"\n'
        + "Content-Transfer-Encoding: 8bit\n\n"
        + utf8
        + "\n"
        + "--idid\n"
        + 'Content-Type: text/plain; charset=us-ascii; name="mixed.txt"\n'
        + 'Content-Disposition: attachment; filename="mixed.txt"\n'
        + "Content-Transfer-Encoding: 7bit\n\n"
        + "@@MIXED@@"
        + "\n"
        + "--idid--\n"
    )
    return crlf(body).replace(b"@@MIXED@@", mixed.encode("ascii"))


@case("base64_76_crlf")
def base64_76_crlf() -> bytes:
    return crlf(
        headers("base64 76 CRLF", mid(11))
        + 'Content-Type: multipart/mixed; boundary="b76"\n\n'
        + "--b76\n"
        + "Content-Type: text/plain\n\n"
        + "76 columns, CRLF, final EOL.\n"
        + "--b76\n"
        + 'Content-Type: application/octet-stream; name="data76.bin"\n'
        + "Content-Transfer-Encoding: base64\n\n"
        + b64(blob(11, 4000))
        + "--b76--\n"
    )


@case("base64_72_lf")
def base64_72_lf() -> bytes:
    return lf(
        headers("base64 72 LF", mid(12))
        + 'Content-Type: multipart/mixed; boundary="b72"\n\n'
        + "--b72\n"
        + "Content-Type: text/plain\n\n"
        + "72 columns, LF, no final EOL.\n"
        + "--b72\n"
        + 'Content-Type: application/zip; name="data72.zip"\n'
        + "Content-Disposition: attachment\n"
        + "Content-Transfer-Encoding: Base64\n\n"
        + b64(blob(12, 4000, b"PK\x03\x04"), line=72, eol="\n", final_eol=False)
        + "\n"
        + "--b72--\n"
    )


@case("base64_irregular")
def base64_irregular() -> bytes:
    """Irregular line lengths, and trailing whitespace: both copy mode."""
    data = base64.b64encode(blob(13, 3000)).decode("ascii")
    rng = random.Random(13)
    lines, i = [], 0
    while i < len(data):
        n = rng.choice([60, 64, 76, 70])
        lines.append(data[i : i + n])
        i += n
    irregular = "\n".join(lines) + "\n"
    spaced = b64(pdf(14), line=76, eol=" \n")
    return crlf(
        headers("base64 irregular", mid(13))
        + 'Content-Type: multipart/mixed; boundary="birr"\n\n'
        + "--birr\n"
        + "Content-Type: text/plain\n\n"
        + "Irregular base64 lines.\n"
        + "--birr\n"
        + 'Content-Type: application/octet-stream; name="irregular.bin"\n'
        + "Content-Transfer-Encoding: base64\n\n"
        + irregular
        + "--birr\n"
        + 'Content-Type: application/pdf; name="spaced.pdf"\n'
        + "Content-Transfer-Encoding: base64\n\n"
        + spaced
        + "--birr--\n"
    )


@case("forwarded_rfc822")
def forwarded_rfc822() -> bytes:
    """A forwarded message carrying the same PDF as outlook_related."""
    inner = (
        headers(
            "Quarterly report",
            "<inner-0001@example.com>",
            sender="Carol <carol@example.com>",
            to="Alice <alice@example.org>",
        )
        + 'Content-Type: multipart/mixed; boundary="inner-b"\n\n'
        + "--inner-b\n"
        + "Content-Type: text/plain\n\n"
        + "Original message text.\n"
        + "--inner-b\n"
        + 'Content-Type: application/pdf; name="quarterly-report.pdf"\n'
        + 'Content-Disposition: attachment; filename="quarterly-report.pdf"\n'
        + "Content-Transfer-Encoding: base64\n\n"
        + b64(pdf(1), eol="\n")
        + "--inner-b--\n"
    )
    return crlf(
        headers("Fwd: Quarterly report", mid(14))
        + 'Content-Type: multipart/mixed; boundary="outer-b"\n\n'
        + "--outer-b\n"
        + "Content-Type: text/plain\n\n"
        + "Forwarding this.\n"
        + "--outer-b\n"
        + 'Content-Type: message/rfc822; name="Quarterly report.eml"\n'
        + "Content-Disposition: attachment\n\n"
        + inner
        + "--outer-b--\n"
    )


@case("no_message_id")
def no_message_id() -> bytes:
    return crlf(
        headers("No Message-ID", None)
        + "Content-Type: text/plain\n\n"
        + "This message has no Message-ID header.\n"
    )


@case("duplicate_message_id_a")
def duplicate_message_id_a() -> bytes:
    return crlf(
        headers("Same id", "<shared-id@example.org>")
        + "Content-Type: text/plain\n\n"
        + "The copy as sent.\n"
    )


@case("duplicate_message_id_b")
def duplicate_message_id_b() -> bytes:
    return crlf(
        headers("Same id", "<shared-id@example.org>")
        + "List-Id: <list.example.org>\n"
        + "Content-Type: text/plain\n\n"
        + "The copy as sent.\n"
        + "_______________________________________________\n"
        + "list mailing list\n"
    )


def attachment_part(header_lines: str, seed: int) -> str:
    return (
        "--fnames\n"
        + header_lines
        + "Content-Transfer-Encoding: base64\n\n"
        + b64(blob(seed, 200))
    )


@case("filenames_encoded")
def filenames_encoded() -> bytes:
    """RFC 2231 and RFC 2047 filenames."""
    return crlf(
        headers("Encoded names", mid(15))
        + 'Content-Type: multipart/mixed; boundary="fnames"\n\n'
        + "--fnames\n"
        + "Content-Type: text/plain\n\n"
        + "Names.\n"
        # RFC 2231 with charset
        + attachment_part(
            "Content-Type: application/pdf\n"
            "Content-Disposition: attachment; filename*=UTF-8''%C3%A9t%C3%A9.pdf\n",
            150,
        )
        # RFC 2231 continuations
        + attachment_part(
            "Content-Type: application/octet-stream\n"
            "Content-Disposition: attachment;\n"
            " filename*0*=UTF-8''a%20very%20long%20name%20split;\n"
            " filename*1*=%20over%20two%20lines.txt\n",
            151,
        )
        # RFC 2047 in name= (not standard, but common)
        + attachment_part(
            "Content-Type: application/pdf;\n"
            ' name="=?UTF-8?B?0J7RgtGH0LXRgi5wZGY=?="\n',
            152,
        )
        # RFC 2047 Q in filename=
        + attachment_part(
            "Content-Type: application/pdf\n"
            'Content-Disposition: attachment; filename="=?ISO-8859-1?Q?r=E9sum=E9.pdf?="\n',
            153,
        )
        # NFD form, should be normalised to NFC
        + attachment_part(
            "Content-Type: text/plain\n"
            "Content-Disposition: attachment; filename*=UTF-8''cafe%CC%81.txt\n",
            154,
        )
        + "--fnames--\n"
    )


@case("filenames_hostile")
def filenames_hostile() -> bytes:
    """Path traversal, control characters, NUL, leading dots, very long."""
    long_name = "x" * 300 + ".pdf"
    return crlf(
        headers("Hostile names", mid(16))
        + 'Content-Type: multipart/mixed; boundary="fnames"\n\n'
        + "--fnames\n"
        + "Content-Type: text/plain\n\n"
        + "Names.\n"
        + attachment_part(
            "Content-Type: application/octet-stream\n"
            'Content-Disposition: attachment; filename="../../../etc/passwd"\n',
            160,
        )
        + attachment_part(
            "Content-Type: application/octet-stream\n"
            'Content-Disposition: attachment; filename="=?UTF-8?Q?bell=07esc=1B.txt?="\n',
            161,
        )
        + attachment_part(
            "Content-Type: application/octet-stream\n"
            'Content-Disposition: attachment; filename="=?UTF-8?Q?nul=00byte.txt?="\n',
            162,
        )
        + attachment_part(
            "Content-Type: application/octet-stream\n"
            'Content-Disposition: attachment; filename="...hidden"\n',
            163,
        )
        + attachment_part(
            "Content-Type: application/pdf\n"
            f'Content-Disposition: attachment; filename="{long_name}"\n',
            164,
        )
        + attachment_part(
            "Content-Type: application/pdf\n"
            'Content-Disposition: attachment; filename=".."\n',
            165,
        )
        + attachment_part(
            'Content-Type: image/png\nContent-Disposition: attachment; filename=""\n',
            166,
        )
        + attachment_part(
            "Content-Type: application/octet-stream\n"
            'Content-Disposition: attachment; filename="C:\\\\Users\\\\me\\\\win.doc"\n',
            167,
        )
        # U+202E RIGHT-TO-LEFT OVERRIDE: displays as "invoiceexe.pdf"
        + attachment_part(
            "Content-Type: application/pdf\n"
            "Content-Disposition: attachment; filename*=UTF-8''invoice%E2%80%AEfdp.exe\n",
            168,
        )
        + "--fnames--\n"
    )


@case("single_part_attachment")
def single_part_attachment() -> bytes:
    """A scanner: the whole message is one PDF part."""
    return crlf(
        headers("Scan", mid(17), sender="Scanner <scanner@example.org>")
        + 'Content-Type: application/pdf; name="scan0001.pdf"\n'
        + 'Content-Disposition: attachment; filename="scan0001.pdf"\n'
        + "Content-Transfer-Encoding: base64\n\n"
        + b64(pdf(17))
    )


@case("calendar_alternative")
def calendar_alternative() -> bytes:
    """An invitation: text/calendar inside multipart/alternative, plus an
    .ics attachment with a filename."""
    ics = (
        "BEGIN:VCALENDAR\nVERSION:2.0\nMETHOD:REQUEST\nBEGIN:VEVENT\n"
        "UID:corpus-event-1@example.org\nDTSTART:20261101T100000Z\n"
        "SUMMARY:Meeting\nEND:VEVENT\nEND:VCALENDAR\n"
    )
    return crlf(
        headers("Invitation: Meeting", mid(18))
        + 'Content-Type: multipart/mixed; boundary="cal-mixed"\n\n'
        + "--cal-mixed\n"
        + 'Content-Type: multipart/alternative; boundary="cal-alt"\n\n'
        + "--cal-alt\n"
        + "Content-Type: text/plain; charset=utf-8\n\n"
        + "You are invited.\n"
        + "--cal-alt\n"
        + "Content-Type: text/html; charset=utf-8\n\n"
        + "<p>You are invited.</p>\n"
        + "--cal-alt\n"
        + "Content-Type: text/calendar; charset=utf-8; method=REQUEST\n"
        + "Content-Transfer-Encoding: 7bit\n\n"
        + ics
        + "--cal-alt--\n"
        + "\n"
        + "--cal-mixed\n"
        + 'Content-Type: application/ics; name="invite.ics"\n'
        + 'Content-Disposition: attachment; filename="invite.ics"\n'
        + "Content-Transfer-Encoding: base64\n\n"
        + b64(ics.replace("\n", "\r\n").encode())
        + "--cal-mixed--\n"
    )


@case("html_related_alternative")
def html_related_alternative() -> bytes:
    """Thunderbird/Apple style: alternative { plain, related { html, img } }."""
    return lf(
        headers("Newsletter", mid(19))
        + 'Content-Type: multipart/alternative; boundary="alt"\n\n'
        + "This is a multi-part message in MIME format.\n"
        + "--alt\n"
        + "Content-Type: text/plain; charset=UTF-8; format=flowed\n\n"
        + "News.\n"
        + "--alt\n"
        + 'Content-Type: multipart/related; boundary="rel"\n\n'
        + "--rel\n"
        + "Content-Type: text/html; charset=UTF-8\n\n"
        + '<p>News.</p><img src="cid:logo@example.org">\n'
        + "--rel\n"
        + "Content-Type: image/png\n"
        + "Content-ID: <logo@example.org>\n"
        + "Content-Transfer-Encoding: base64\n\n"
        + b64(png(19), eol="\n")
        + "--rel--\n"
        + "\n"
        + "--alt--\n"
    )


@case("delivery_status")
def delivery_status() -> bytes:
    """A bounce: multipart/report with delivery-status and the original
    headers."""
    return crlf(
        headers(
            "Undelivered Mail Returned to Sender",
            mid(20),
            sender="MAILER-DAEMON@example.net (Mail Delivery System)",
        )
        + "Content-Type: multipart/report; report-type=delivery-status;\n"
        + '\tboundary="DSN.0001"\n\n'
        + "--DSN.0001\n"
        + "Content-Description: Notification\n"
        + "Content-Type: text/plain; charset=us-ascii\n\n"
        + "Your message could not be delivered.\n\n"
        + "--DSN.0001\n"
        + "Content-Description: Delivery report\n"
        + "Content-Type: message/delivery-status\n\n"
        + "Reporting-MTA: dns; mail.example.net\n\n"
        + "Final-Recipient: rfc822; nobody@example.net\n"
        + "Action: failed\nStatus: 5.1.1\n\n"
        + "--DSN.0001\n"
        + "Content-Description: Undelivered Message Headers\n"
        + "Content-Type: text/rfc822-headers\n\n"
        + "From: Alice <alice@example.org>\nSubject: Hello\n\n"
        + "--DSN.0001--\n"
    )


@case("binary_8bit_attachment")
def binary_8bit_attachment() -> bytes:
    """Identity encodings on non-text parts."""
    raw = "tab\tseparated\nvalues \u00e9\n"
    return crlf(
        headers("8bit attachment", mid(21))
        + 'Content-Type: multipart/mixed; boundary="8b"\n\n'
        + "--8b\n"
        + "Content-Type: text/plain\n\n"
        + "Data.\n"
        + "--8b\n"
        + 'Content-Type: application/octet-stream; name="table.tsv"\n'
        + "Content-Transfer-Encoding: 8bit\n\n"
        + raw
        + "--8b--\n"
    )


# --- damaged messages (M1 robustness) ---------------------------------------


@case("damaged_missing_final_boundary")
def damaged_missing_final_boundary() -> bytes:
    return crlf(
        headers("Missing final boundary", mid(30))
        + 'Content-Type: multipart/mixed; boundary="nofinal"\n\n'
        + "--nofinal\n"
        + "Content-Type: text/plain\n\n"
        + "Text.\n"
        + "--nofinal\n"
        + 'Content-Type: application/pdf; name="truncated.pdf"\n'
        + "Content-Transfer-Encoding: base64\n\n"
        + b64(pdf(30))
    )


@case("damaged_boundary_whitespace")
def damaged_boundary_whitespace() -> bytes:
    return crlf(
        headers("Boundary with trailing whitespace", mid(31))
        + 'Content-Type: multipart/mixed; boundary="ws"\n\n'
        + "--ws  \n"
        + "Content-Type: text/plain\n\n"
        + "Text.\n"
        + "--ws\t\n"
        + 'Content-Type: application/pdf; name="ws.pdf"\n'
        + "Content-Transfer-Encoding: base64\n\n"
        + b64(pdf(31))
        + "--ws-- \n"
    )


@case("damaged_mixed_eol")
def damaged_mixed_eol() -> bytes:
    """Headers in CRLF, some parts in LF."""
    head = crlf(
        headers("Mixed line endings", mid(32))
        + 'Content-Type: multipart/mixed; boundary="mx"\n\n'
        + "--mx\n"
        + "Content-Type: text/plain\n\n"
        + "CRLF text.\n"
    )
    tail = lf(
        "--mx\n"
        + 'Content-Type: application/pdf; name="lf.pdf"\n'
        + "Content-Transfer-Encoding: base64\n\n"
        + b64(pdf(32), eol="\n")
        + "--mx--\n"
    )
    return head + tail


@case("damaged_headers_only_part")
def damaged_headers_only_part() -> bytes:
    """A part with headers and no body; a part with no headers at all; and a
    message ending in its header block."""
    return crlf(
        headers("Headers without bodies", mid(33))
        + 'Content-Type: multipart/mixed; boundary="ho"\n\n'
        + "--ho\n"
        + "Content-Type: text/plain\n"
        + "--ho\n"
        + "\n"
        + "A part with no headers.\n"
        + "--ho\n"
        + "Content-Type: application/octet-stream; name=empty.bin\n"
        + "Content-Transfer-Encoding: base64\n"
        + "\n"
        + "--ho--\n"
    )


@case("damaged_headers_no_body")
def damaged_headers_no_body() -> bytes:
    return crlf(headers("Only headers", mid(34)) + "Content-Type: text/plain")


@case("damaged_boundary_prefix")
def damaged_boundary_prefix() -> bytes:
    """The inner boundary has the outer boundary as a prefix, and a body line
    starts with the outer boundary followed by other characters."""
    return crlf(
        headers("Boundary prefixes", mid(35))
        + 'Content-Type: multipart/mixed; boundary="bnd"\n\n'
        + "--bnd\n"
        + 'Content-Type: multipart/alternative; boundary="bnd2"\n\n'
        + "--bnd2\n"
        + "Content-Type: text/plain\n\n"
        + "--bndX is not a boundary.\n"
        + "--bnd2\n"
        + "Content-Type: text/html\n\n"
        + "<p>html</p>\n"
        + "--bnd2--\n"
        + "--bnd\n"
        + 'Content-Type: application/pdf; name="prefix.pdf"\n'
        + "Content-Transfer-Encoding: base64\n\n"
        + b64(pdf(35))
        + "--bnd--\n"
        + "epilogue\n"
    )


@case("damaged_no_boundary_param")
def damaged_no_boundary_param() -> bytes:
    return crlf(
        headers("Multipart without boundary", mid(36))
        + "Content-Type: multipart/mixed\n\n"
        + "--whatever\n"
        + "Content-Type: text/plain\n\n"
        + "Text.\n"
        + "--whatever--\n"
    )


@case("damaged_boundary_not_found")
def damaged_boundary_not_found() -> bytes:
    return crlf(
        headers("Boundary never appears", mid(37))
        + 'Content-Type: multipart/mixed; boundary="absent"\n\n'
        + "Some text but no delimiter lines.\n"
    )


@case("damaged_garbage_headers")
def damaged_garbage_headers() -> bytes:
    """Non-UTF-8 bytes in headers, a header line without a colon, and an
    unknown charset."""
    return (
        crlf(headers("Garbage headers", mid(38)))
        + b"X-Latin1: caf\xe9\r\n"
        + b"This line is not a header\r\n"
        + b"Content-Type: text/plain; charset=x-unknown-charset\r\n"
        + b"\r\n"
        + b"Body \xff\xfe bytes.\r\n"
    )


def main() -> int:
    check = "--check" in sys.argv[1:]
    stale = []
    for name, fn in CASES.items():
        path = HERE / f"{name}.eml"
        data = fn()
        if check:
            if not path.exists() or path.read_bytes() != data:
                stale.append(name)
        else:
            path.write_bytes(data)
    if check and stale:
        print("stale corpus files:", ", ".join(stale))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
