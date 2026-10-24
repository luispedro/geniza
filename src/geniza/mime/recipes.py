"""Decoding, encoding recipes and extraction modes
(DESIGN.md, "Attachments / Extraction modes").

A recipe says how to turn decoded content back into the exact encoded bytes
of a part body. It is inferred from the body and then checked by
re-encoding: a part can be stubbed only if the round trip is byte-exact.
"""

import base64
import binascii
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from geniza.mime.classify import Classified, PartClass
from geniza.mime.tree import Part

type Eol = Literal["crlf", "lf"]

EOL_BYTES: dict[Eol, bytes] = {"crlf": b"\r\n", "lf": b"\n"}

KiB = 1024


@dataclass(frozen=True)
class Base64Recipe:
    line: int
    eol: Eol
    final_eol: bool


@dataclass(frozen=True)
class IdentityRecipe:
    #: The encoded form uses CRLF and the content LF (text/* parts only).
    #: Otherwise the content is the body unchanged.
    crlf_to_lf: bool
    #: Line endings of the encoded form, if it has any; used for the stub.
    eol: Eol
    final_eol: bool


type Recipe = Base64Recipe | IdentityRecipe


class Mode(StrEnum):
    STUB = "stub"  # body replaced by a stub; the file is needed to rebuild
    COPY = "copy"  # file written, body kept as is
    INLINE = "inline"  # nothing extracted


@dataclass(frozen=True)
class Policy:
    min_size: int = 1
    decoration_min_size: int = 256 * KiB


@dataclass(frozen=True)
class Decoded:
    """The decoded content of a part, and a recipe if one reproduces the
    body exactly. ``content`` is None when the encoding is not understood."""

    content: bytes | None
    recipe: Recipe | None
    reason: str | None = None


@dataclass(frozen=True)
class PartPlan:
    part: Part
    cls: PartClass
    mode: Mode
    content: bytes | None = None
    recipe: Recipe | None = None
    reason: str | None = None


IDENTITY_ENCODINGS = frozenset({"", "7bit", "8bit", "binary"})


def _eol_style(body: bytes) -> tuple[Eol | None, bool]:
    """The single line-ending style of ``body`` (None if mixed or there is
    no line ending), and whether it ends with one."""
    crlf = body.count(b"\r\n")
    lf = body.count(b"\n")
    cr = body.count(b"\r")
    if lf == 0 and cr == 0:
        return "lf", False
    if crlf == lf == cr:
        return "crlf", body.endswith(b"\r\n")
    if crlf == 0 and cr == 0:
        return "lf", body.endswith(b"\n")
    return None, False


def encode_base64(content: bytes, recipe: Base64Recipe) -> bytes:
    encoded = base64.b64encode(content)
    step = recipe.line
    eol = EOL_BYTES[recipe.eol]
    lines = [encoded[i : i + step] for i in range(0, len(encoded), step)]
    return eol.join(lines) + (eol if recipe.final_eol and lines else b"")


def _decode_base64(body: bytes) -> Decoded:
    eol, final = _eol_style(body)
    if eol is not None:
        lines = body.split(EOL_BYTES[eol])
        if final:
            lines.pop()
        try:
            content = binascii.a2b_base64(b"".join(lines), strict_mode=True)
        except binascii.Error:
            pass
        else:
            if lines and lines[0]:
                recipe = Base64Recipe(len(lines[0]), eol, final)
                if encode_base64(content, recipe) == body:
                    return Decoded(content, recipe)
            return Decoded(content, None, "base64 does not round-trip")
    try:
        return Decoded(binascii.a2b_base64(body), None, "irregular base64")
    except binascii.Error:
        return Decoded(None, None, "invalid base64")


def encode_identity(content: bytes, recipe: IdentityRecipe) -> bytes:
    return content.replace(b"\n", b"\r\n") if recipe.crlf_to_lf else content


def _decode_identity(body: bytes, is_text: bool) -> Decoded:
    eol, final = _eol_style(body)
    if not is_text:
        return Decoded(body, IdentityRecipe(False, eol or "lf", final))
    if eol is None:
        return Decoded(body, None, "mixed line endings")
    recipe = IdentityRecipe(eol == "crlf", eol, final)
    content = body.replace(b"\r\n", b"\n") if recipe.crlf_to_lf else body
    assert encode_identity(content, recipe) == body
    return Decoded(content, recipe)


def _decode_qp(body: bytes, is_text: bool) -> Decoded:
    content = binascii.a2b_qp(body)
    # Soft line breaks cannot be reproduced reliably, so QP is never
    # stubbed. Text is still written with LF, so files are usable directly.
    if is_text and _eol_style(content)[0] == "crlf":
        content = content.replace(b"\r\n", b"\n")
    return Decoded(content, None, "quoted-printable")


def encode(content: bytes, recipe: Recipe) -> bytes:
    match recipe:
        case Base64Recipe():
            return encode_base64(content, recipe)
        case IdentityRecipe():
            return encode_identity(content, recipe)


def decode(data: bytes, part: Part) -> Decoded:
    body = data[part.body_start : part.end]
    cte = part.transfer_encoding
    is_text = part.maintype == "text"
    if cte == "base64":
        return _decode_base64(body)
    if cte == "quoted-printable":
        return _decode_qp(body, is_text)
    if cte in IDENTITY_ENCODINGS:
        return _decode_identity(body, is_text)
    return Decoded(None, None, f"unknown transfer encoding {cte!r}")


def plan_part(data: bytes, c: Classified, policy: Policy) -> PartPlan:
    """Decide what to do with one classified leaf."""
    if c.cls not in (PartClass.ATTACHMENT, PartClass.DECORATION):
        return PartPlan(c.part, c.cls, Mode.INLINE)
    decoded = decode(data, c.part)
    content, recipe = decoded.content, decoded.recipe
    if content is None:
        return PartPlan(c.part, c.cls, Mode.INLINE, reason=decoded.reason)
    if c.cls is PartClass.ATTACHMENT:
        if len(content) < policy.min_size:
            return PartPlan(c.part, c.cls, Mode.INLINE, content, recipe, "too small")
        mode = Mode.STUB if recipe else Mode.COPY
        return PartPlan(c.part, c.cls, mode, content, recipe, decoded.reason)
    if len(content) < policy.decoration_min_size:
        return PartPlan(c.part, c.cls, Mode.INLINE, content, recipe, "too small")
    mode = Mode.STUB if recipe else Mode.INLINE
    return PartPlan(c.part, c.cls, mode, content, recipe, decoded.reason)


def stub_bytes(file: str, recipe: Recipe) -> bytes:
    """The text that replaces a stubbed body, ending like the original."""
    eol = EOL_BYTES[recipe.eol]
    marker = f"[geniza: stored as {file}]".encode()
    return marker + (eol if recipe.final_eol else b"")


def plan_message(data: bytes, policy: Policy) -> list[PartPlan]:
    """Parse, classify and plan every leaf of a raw message."""
    from geniza.mime.classify import classify
    from geniza.mime.tree import parse

    return [plan_part(data, c, policy) for c in classify(parse(data))]
