"""Stripping stubbed parts out of a message and putting them back.

Both directions work on raw bytes only. ``strip`` replaces each stubbed body
with a short stub and records where the stub is; ``reconstruct`` re-encodes
the content with its recipe and splices it back in.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from geniza.mime.recipes import Recipe, encode, stub_bytes


@dataclass(frozen=True)
class Cut:
    """A body to replace: ``data[start:end]`` becomes a stub naming ``file``."""

    start: int
    end: int
    file: str
    recipe: Recipe


@dataclass(frozen=True)
class Stub:
    """Where a stub sits in the stripped message, and how to rebuild what
    it replaced."""

    offset: int
    length: int
    recipe: Recipe


def strip(data: bytes, cuts: Sequence[Cut]) -> tuple[bytes, list[Stub]]:
    """Replace the given bodies with stubs. Cuts must not overlap."""
    out = bytearray()
    stubs: list[Stub] = []
    pos = 0
    for cut in sorted(cuts, key=lambda c: c.start):
        if cut.start < pos:
            raise ValueError("overlapping cuts")
        out += data[pos : cut.start]
        stub = stub_bytes(cut.file, cut.recipe)
        stubs.append(Stub(len(out), len(stub), cut.recipe))
        out += stub
        pos = cut.end
    out += data[pos:]
    return bytes(out), stubs


def reconstruct(
    stripped: bytes, stubs: Sequence[Stub], contents: Sequence[bytes]
) -> bytes:
    """Rebuild the original message; ``contents[i]`` belongs to ``stubs[i]``."""
    out = bytearray()
    pos = 0
    for stub, content in zip(stubs, contents, strict=True):
        if stub.offset < pos or stub.offset + stub.length > len(stripped):
            raise ValueError("stub out of range")
        out += stripped[pos : stub.offset]
        out += encode(content, stub.recipe)
        pos = stub.offset + stub.length
    out += stripped[pos:]
    return bytes(out)


def moved(pos: int, cuts: Sequence[Cut], stubs: Sequence[Stub]) -> int:
    """Where ``pos`` of the original message (not inside a cut) is in the
    stripped one; ``cuts`` and ``stubs`` as ``strip`` sorted and made them."""
    return pos + sum(
        stub.length - (cut.end - cut.start)
        for cut, stub in zip(cuts, stubs, strict=True)
        if cut.end <= pos
    )
