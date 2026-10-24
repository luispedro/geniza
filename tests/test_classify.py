"""Expected class and mode for every corpus message (PLAN.md, M2)."""

import hashlib

import pytest
from conftest import CORPUS, corpus_files

from geniza.mime.recipes import Mode, Policy, plan_message
from geniza.mime.strip import Cut, reconstruct, strip

B, C, D, A, OTHER = "body", "crypto", "decoration", "attachment", "other"
STUB, COPY, INLINE = "stub", "copy", "inline"

# (path, class, mode) per leaf, with decoration_min_size = 1 so that
# decorations are extracted whenever they can be.
EXPECTED: dict[str, list[tuple[str, str, str]]] = {
    "apple_inline": [
        ("1", B, INLINE),
        ("2", A, STUB),  # inline disposition, but a filename
        ("3", B, INLINE),
        ("4", A, STUB),  # inline image *with* a filename
        ("5", B, INLINE),
    ],
    "base64_72_lf": [("1", B, INLINE), ("2", A, STUB)],
    "base64_76_crlf": [("1", B, INLINE), ("2", A, STUB)],
    "base64_irregular": [("1", B, INLINE), ("2", A, COPY), ("3", A, COPY)],
    "binary_8bit_attachment": [("1", B, INLINE), ("2", A, STUB)],
    "calendar_alternative": [
        ("1.1", B, INLINE),
        ("1.2", B, INLINE),
        ("1.3", OTHER, INLINE),  # text/calendar without a filename
        ("2", A, STUB),
    ],
    "damaged_boundary_not_found": [("1", OTHER, INLINE)],
    "damaged_boundary_prefix": [("1.1", B, INLINE), ("1.2", B, INLINE), ("2", A, STUB)],
    "damaged_boundary_whitespace": [("1", B, INLINE), ("2", A, STUB)],
    "damaged_garbage_headers": [("1", B, INLINE)],
    "damaged_headers_no_body": [("1", B, INLINE)],
    "damaged_headers_only_part": [
        ("1", B, INLINE),
        ("2", B, INLINE),
        ("3", A, INLINE),  # empty
    ],
    "damaged_missing_final_boundary": [("1", B, INLINE), ("2", A, STUB)],
    "damaged_mixed_eol": [("1", B, INLINE), ("2", A, STUB)],
    "damaged_no_boundary_param": [("1", OTHER, INLINE)],
    "delivery_status": [("1", B, INLINE), ("2", OTHER, INLINE), ("3", OTHER, INLINE)],
    "duplicate_message_id_a": [("1", B, INLINE)],
    "duplicate_message_id_b": [("1", B, INLINE)],
    "filenames_encoded": [("1", B, INLINE)] + [(str(i), A, STUB) for i in range(2, 7)],
    "filenames_hostile": [("1", B, INLINE)] + [(str(i), A, STUB) for i in range(2, 11)],
    "forwarded_rfc822": [
        ("1", B, INLINE),
        ("2.1", B, INLINE),  # body of the attached message
        ("2.2", A, STUB),
    ],
    "html_related_alternative": [
        ("1", B, INLINE),
        ("2.1", B, INLINE),  # the root of multipart/related
        ("2.2", D, STUB),
    ],
    "identity_text_attachments": [
        ("1", B, INLINE),
        ("2", A, STUB),
        ("3", A, STUB),
        ("4", A, COPY),  # mixed line endings
    ],
    "inline_image_no_name": [("1", B, INLINE), ("2", D, STUB)],
    "no_message_id": [("1", B, INLINE)],
    "outlook_related": [
        ("1.1.1", B, INLINE),
        ("1.1.2", B, INLINE),
        ("1.2", D, STUB),  # image001.png: a filename, but a decoration
        ("1.3", D, STUB),
        ("2", A, STUB),
    ],
    "pgp_encrypted": [("1", C, INLINE), ("2", C, INLINE)],
    "pgp_signed": [
        ("1.1", B, INLINE),
        ("1.2", A, STUB),
        ("1.3", A, STUB),
        ("2", C, INLINE),
    ],
    "plain_text_crlf": [("1", B, INLINE)],
    "related_non_image_content_id": [
        ("1", B, INLINE),
        ("2", D, STUB),
        ("3", A, STUB),  # a Content-ID, but not an image
        ("4", OTHER, INLINE),  # no filename either
    ],
    "plain_text_lf": [("1", B, INLINE)],
    "qp_text_attachments": [("1", B, INLINE), ("2", A, COPY), ("3", A, COPY)],
    "single_part_attachment": [("1", A, STUB)],
    "smime_signed": [("1.1", B, INLINE), ("1.2", A, STUB), ("2", C, INLINE)],
}

EXTRACT_ALL = Policy(min_size=1, decoration_min_size=1)


def test_every_corpus_message_has_expectations() -> None:
    assert sorted(EXPECTED) == sorted(p.stem for p in corpus_files())


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_classification(name: str) -> None:
    data = (CORPUS / f"{name}.eml").read_bytes()
    got = [(p.part.path, p.cls, p.mode) for p in plan_message(data, EXTRACT_ALL)]
    assert got == EXPECTED[name]


def test_default_policy_keeps_small_decorations_inline() -> None:
    data = (CORPUS / "outlook_related.eml").read_bytes()
    modes = {p.part.path: p.mode for p in plan_message(data, Policy())}
    assert modes["1.2"] is Mode.INLINE and modes["1.3"] is Mode.INLINE
    assert modes["2"] is Mode.STUB


def test_min_size() -> None:
    data = (CORPUS / "pgp_signed.eml").read_bytes()
    plans = {p.part.path: p for p in plan_message(data, Policy(min_size=1000))}
    assert plans["1.2"].mode is Mode.INLINE  # the 120-byte patch
    assert plans["1.3"].mode is Mode.STUB


def strip_all(data: bytes) -> tuple[bytes, bytes]:
    plans = [p for p in plan_message(data, EXTRACT_ALL) if p.mode is Mode.STUB]
    cuts = []
    for i, p in enumerate(plans):
        assert p.recipe is not None
        cuts.append(
            Cut(p.part.body_start, p.part.end, f"attachments/x/{i}.bin", p.recipe)
        )
    stripped, stubs = strip(data, cuts)
    contents = [p.content for p in plans]
    assert all(c is not None for c in contents)
    return stripped, reconstruct(stripped, stubs, contents)  # type: ignore[arg-type]


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_stubs_reconstruct_byte_exactly(name: str) -> None:
    data = (CORPUS / f"{name}.eml").read_bytes()
    stripped, rebuilt = strip_all(data)
    assert hashlib.sha256(rebuilt).digest() == hashlib.sha256(data).digest()
    if any(m == STUB for _, _, m in EXPECTED[name]):
        assert b"[geniza: stored as attachments/x/0.bin]" in stripped


def test_identity_text_written_with_lf() -> None:
    data = (CORPUS / "identity_text_attachments.eml").read_bytes()
    plans = {p.part.path: p for p in plan_message(data, EXTRACT_ALL)}
    assert plans["2"].content == b"# Notes\n\n- one\n- two\n"
    assert plans["3"].content == "Grüße aus Köln\n".encode()
    # Copy mode keeps the content as it is.
    assert plans["4"].content == b"line one\r\nline two\nline three\r\n"


def test_qp_text_decoded_with_lf() -> None:
    data = (CORPUS / "qp_text_attachments.eml").read_bytes()
    plans = {p.part.path: p for p in plan_message(data, EXTRACT_ALL)}
    script = plans["2"].content
    assert script is not None
    assert b"\r" not in script and 'print("café = 3")'.encode() in script
    assert plans["3"].content == b"--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a = 1\n+a = 2  \n"


def test_forwarded_pdf_matches_original() -> None:
    def pdf(name: str, path: str) -> bytes | None:
        data = (CORPUS / f"{name}.eml").read_bytes()
        return {p.part.path: p for p in plan_message(data, EXTRACT_ALL)}[path].content

    assert pdf("forwarded_rfc822", "2.2") == pdf("outlook_related", "2")
