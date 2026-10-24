"""Classify leaf MIME parts (DESIGN.md, "Attachments / Classification").

Every leaf falls into exactly one class, checked in order: body, crypto,
decoration, attachment, other. Classification recurses into
message/rfc822, with the body rule applied relative to the attached message.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum

from geniza.mime.tree import Kind, Part


class PartClass(StrEnum):
    BODY = "body"
    CRYPTO = "crypto"
    DECORATION = "decoration"
    ATTACHMENT = "attachment"
    OTHER = "other"


CRYPTO_TYPES = frozenset(
    {
        "application/pgp-signature",
        "application/pkcs7-signature",
        "application/x-pkcs7-signature",
        "application/pgp-encrypted",
        "application/pgp-keys",
        "application/pkcs7-mime",
        "application/x-pkcs7-mime",
    }
)

BODY_TYPES = frozenset({"text/plain", "text/html"})

# Containers whose members are all looked through for body text. Unknown
# multipart subtypes are treated as multipart/mixed (RFC 2046, 5.1.7).
_NOT_MIXED = frozenset(
    {
        "multipart/signed",
        "multipart/related",
        "multipart/encrypted",
        "multipart/report",
        "multipart/digest",
    }
)


@dataclass(frozen=True)
class Classified:
    part: Part
    cls: PartClass


def has_filename(part: Part) -> bool:
    return bool(part.filename)


def _is_attachment_disposition(part: Part) -> bool:
    return part.disposition == "attachment"


def _related_root(node: Part) -> Part | None:
    """The root part of a multipart/related: the one named by ``start``, or
    the first."""
    if not node.children:
        return None
    try:
        start = node.headers.get_param("start")
    except Exception:
        start = None
    if isinstance(start, str):
        wanted = start.strip()
        for child in node.children:
            if child.content_id == wanted:
                return child
    return node.children[0]


def body_parts(message_root: Part) -> set[int]:
    """``id()`` of the leaves that form the text of a message.

    These are the text/plain and text/html leaves without a filename or
    ``Content-Disposition: attachment``, reachable from the message root
    through multipart/mixed and multipart/alternative (all members),
    multipart/signed (the signed content), multipart/report (the
    human-readable first part) and multipart/related (the root part).
    Attached messages are not entered.
    """
    found: set[int] = set()

    def visit(node: Part) -> None:
        match node.kind:
            case Kind.LEAF:
                if (
                    node.content_type in BODY_TYPES
                    and not has_filename(node)
                    and not _is_attachment_disposition(node)
                ):
                    found.add(id(node))
            case Kind.MULTIPART:
                ctype = node.content_type
                if ctype in ("multipart/signed", "multipart/report"):
                    if node.children:
                        visit(node.children[0])
                elif ctype == "multipart/related":
                    root = _related_root(node)
                    if root is not None:
                        visit(root)
                elif ctype not in _NOT_MIXED:
                    for child in node.children:
                        visit(child)
            case Kind.MESSAGE:
                pass

    visit(message_root)
    return found


def classify(root: Part) -> list[Classified]:
    """Classify every leaf of the tree, in byte order."""
    out: list[Classified] = []

    def visit(node: Part, body: set[int], in_related: bool, in_encrypted: bool) -> None:
        match node.kind:
            case Kind.LEAF:
                out.append(
                    Classified(node, _leaf_class(node, body, in_related, in_encrypted))
                )
            case Kind.MULTIPART:
                related = in_related or node.content_type == "multipart/related"
                encrypted = in_encrypted or node.content_type == "multipart/encrypted"
                for child in node.children:
                    visit(child, body, related, encrypted)
            case Kind.MESSAGE:
                inner = node.children[0]
                visit(inner, body_parts(inner), False, False)

    visit(root, body_parts(root), False, False)
    return out


def _leaf_class(
    part: Part, body: set[int], in_related: bool, in_encrypted: bool
) -> PartClass:
    if id(part) in body:
        return PartClass.BODY
    if part.content_type in CRYPTO_TYPES or in_encrypted:
        return PartClass.CRYPTO
    if in_related and part.content_id:
        return PartClass.DECORATION
    if (
        part.maintype == "image"
        and part.disposition in (None, "inline")
        and not has_filename(part)
    ):
        return PartClass.DECORATION
    if has_filename(part) or _is_attachment_disposition(part):
        return PartClass.ATTACHMENT
    return PartClass.OTHER


def iter_class(classified: list[Classified], cls: PartClass) -> Iterator[Part]:
    return (c.part for c in classified if c.cls is cls)
