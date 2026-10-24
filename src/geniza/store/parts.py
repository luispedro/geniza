"""Extracted parts as the record header lists them (DESIGN.md, "Record
header").

A ``PartRecord`` says where an extracted part sits in the stripped message
and which attachment file holds its decoded content. A stub also carries the
recipe that re-encodes the file into the original body. A copy keeps the
encoded body in the message, so it has no recipe: its range is that body.

Reconstruction depends on these fields, so ``from_json`` accepts exactly
what ``to_json`` writes and nothing else. ``file`` is checked to be a path
inside ``attachments/`` named for its blob, so a damaged header cannot point
a reader elsewhere.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Self

from geniza.mime.classify import PartClass
from geniza.mime.names import sanitize
from geniza.mime.recipes import (
    EOL_BYTES,
    Base64Recipe,
    Eol,
    IdentityRecipe,
    Layout,
    Mode,
    Recipe,
)
from geniza.mime.strip import Stub
from geniza.store.attachments import PREFIX_LEN

_BLOB = re.compile(r"sha256:([0-9a-f]{64})")
_FILE = re.compile(r"attachments/\d{4}/\d{2}/([0-9a-f]{16})_([^/]+)")

KINDS = frozenset({PartClass.ATTACHMENT, PartClass.DECORATION})
MODES = frozenset({Mode.STUB, Mode.COPY})

_COMMON = ("path", "kind", "mode", "offset", "length", "blob", "file")
_BASE64 = {"enc", "line", "layout", "pad_bits", "eol", "final_eol"}
_IDENTITY = {"enc", "crlf_to_lf", "eol", "final_eol"}


@dataclass(frozen=True, kw_only=True)
class PartRecord:
    #: MIME part path; informational.
    path: str
    kind: PartClass
    mode: Mode
    #: Stub: the range of the stub in the stripped message. Copy: the range
    #: of the encoded body, which is still there.
    offset: int
    length: int
    blob: str
    #: Relative to the store root.
    file: str
    #: Stubs only.
    recipe: Recipe | None = None

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"bad kind {self.kind!r}")
        if self.mode not in MODES:
            raise ValueError(f"bad mode {self.mode!r}")
        if (self.mode is Mode.STUB) != (self.recipe is not None):
            raise ValueError("a stub needs a recipe and a copy has none")
        if self.kind is PartClass.DECORATION and self.mode is Mode.COPY:
            raise ValueError("decorations are never copied")

    @property
    def end(self) -> int:
        return self.offset + self.length

    def to_json(self) -> dict[str, Any]:
        obj: dict[str, Any] = {
            "path": self.path,
            "kind": str(self.kind),
            "mode": str(self.mode),
            "offset": self.offset,
            "length": self.length,
            "blob": self.blob,
            "file": self.file,
        }
        match self.recipe:
            case Base64Recipe() as r:
                obj |= {"enc": "base64", "line": r.line}
                if r.layout:
                    obj["layout"] = [[length, count] for length, count in r.layout]
                if r.pad_bits:
                    obj["pad_bits"] = r.pad_bits
                obj |= {"eol": r.eol, "final_eol": r.final_eol}
            case IdentityRecipe() as r:
                obj["enc"] = "identity"
                if r.crlf_to_lf:
                    obj["crlf_to_lf"] = True
                obj |= {"eol": r.eol, "final_eol": r.final_eol}
        return obj

    @classmethod
    def from_json(cls, obj: object) -> Self:
        """Parse one entry of a header's ``parts``; raises ``ValueError`` if
        it is not exactly what ``to_json`` would write."""
        if not isinstance(obj, dict):
            raise ValueError("part is not an object")
        for key in _COMMON:
            if key not in obj:
                raise ValueError(f"part: missing {key}")
        path, file = obj["path"], obj["file"]
        if not isinstance(path, str):
            raise ValueError("part: bad path")
        kind = PartClass(_choice(obj, "kind", KINDS))
        mode = Mode(_choice(obj, "mode", MODES))
        blob = obj["blob"]
        if not isinstance(blob, str) or not (m := _BLOB.fullmatch(blob)):
            raise ValueError("part: bad blob")
        if not isinstance(file, str) or not (f := _FILE.fullmatch(file)):
            raise ValueError("part: bad file")
        if f[1] != m[1][:PREFIX_LEN] or sanitize(f[2]) != f[2]:
            raise ValueError("part: file does not match blob")
        recipe = _recipe(obj) if mode is Mode.STUB else None
        extra = obj.keys() - set(_COMMON)
        if recipe is None and extra:
            raise ValueError(f"part: unexpected {', '.join(sorted(extra))}")
        return cls(
            path=path,
            kind=kind,
            mode=mode,
            offset=_count(obj, "offset"),
            length=_count(obj, "length"),
            blob=blob,
            file=file,
            recipe=recipe,
        )


def _count(obj: dict[str, Any], key: str, minimum: int = 0) -> int:
    n = obj.get(key)
    if type(n) is not int or n < minimum:
        raise ValueError(f"part: bad {key}")
    return n


def _choice(obj: dict[str, Any], key: str, choices: frozenset[str]) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f"part: bad {key}")
    return value


def _flag(obj: dict[str, Any], key: str) -> bool:
    value = obj.get(key)
    if type(value) is not bool:
        raise ValueError(f"part: bad {key}")
    return value


def _recipe(obj: dict[str, Any]) -> Recipe:
    enc = _choice(obj, "enc", frozenset({"base64", "identity"}))
    allowed = _BASE64 if enc == "base64" else _IDENTITY
    if extra := obj.keys() - set(_COMMON) - allowed:
        raise ValueError(f"part: unexpected {', '.join(sorted(extra))}")
    eol: Eol = "crlf" if _choice(obj, "eol", frozenset(EOL_BYTES)) == "crlf" else "lf"
    final_eol = _flag(obj, "final_eol")
    if enc == "identity":
        # Written only when true, so false is not what to_json writes.
        if "crlf_to_lf" in obj and obj["crlf_to_lf"] is not True:
            raise ValueError("part: bad crlf_to_lf")
        return IdentityRecipe("crlf_to_lf" in obj, eol, final_eol)
    layout: Layout = ()
    if "layout" in obj:
        runs = obj["layout"]
        if not isinstance(runs, list) or not runs:
            raise ValueError("part: bad layout")
        layout = tuple(_run(run) for run in runs)
    pad_bits = _count(obj, "pad_bits", 1) if "pad_bits" in obj else 0
    if pad_bits > 0b1111:
        raise ValueError("part: bad pad_bits")
    return Base64Recipe(_count(obj, "line", 1), eol, final_eol, layout, pad_bits)


def _run(run: object) -> tuple[int, int]:
    match run:
        case [int(length), int(count)] if (
            type(length) is type(count) is int and length >= 0 and count >= 1
        ):
            return length, count
    raise ValueError("part: bad layout")


def parse_parts(entries: Sequence[object], stripped_size: int) -> list[PartRecord]:
    """Parse a header's ``parts``: each entry, and that they are in byte
    order, do not overlap and lie within the stripped message."""
    parts = [PartRecord.from_json(e) for e in entries]
    pos = 0
    for p in parts:
        if p.offset < pos or p.end > stripped_size:
            raise ValueError("parts overlap or are out of range")
        pos = p.end
    return parts


def stubs(parts: Sequence[PartRecord]) -> list[Stub]:
    """The stubs among ``parts`` (in byte order), for ``reconstruct``."""
    return [Stub(p.offset, p.length, p.recipe) for p in parts if p.recipe is not None]
