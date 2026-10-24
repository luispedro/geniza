import json
import re
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from geniza.mime.classify import PartClass
from geniza.mime.names import attachment_name
from geniza.mime.recipes import (
    Base64Recipe,
    IdentityRecipe,
    Mode,
    Policy,
    Recipe,
    plan_message,
)
from geniza.mime.strip import Cut, reconstruct, strip
from geniza.store.attachments import blob_id, file_name
from geniza.store.parts import PartRecord, parse_parts, stubs

DESIGN = Path(__file__).parent.parent / "DESIGN.md"
EXTRACT_ALL = Policy(min_size=1, decoration_min_size=1)

BLOB = "sha256:" + "3fa9c1e07b22d4a1" + "0" * 48
FILE = "attachments/2026/10/3fa9c1e07b22d4a1_quarterly-report.pdf"

eols = st.sampled_from(["crlf", "lf"])
base64_recipes = st.builds(
    Base64Recipe,
    line=st.integers(1, 2047),
    eol=eols,
    final_eol=st.booleans(),
    layout=st.one_of(
        st.just(()),
        st.lists(st.tuples(st.integers(0, 2047), st.integers(1, 3000)), min_size=1).map(
            tuple
        ),
    ),
    pad_bits=st.integers(0, 0b1111),
)
identity_recipes = st.builds(
    IdentityRecipe, crlf_to_lf=st.booleans(), eol=eols, final_eol=st.booleans()
)
names = st.text(min_size=1, max_size=40).filter(lambda s: "/" not in s)


@st.composite
def part_records(draw: st.DrawFn) -> PartRecord:
    blob = blob_id(draw(st.binary(max_size=20)))
    name = attachment_name(draw(names), "2", "application/pdf")
    year, month = draw(st.integers(1970, 9999)), draw(st.integers(1, 12))
    recipe: Recipe | None = draw(st.one_of(st.none(), base64_recipes, identity_recipes))
    kind = PartClass.ATTACHMENT
    if recipe is not None:
        kind = draw(st.sampled_from([PartClass.ATTACHMENT, PartClass.DECORATION]))
    return PartRecord(
        path=draw(st.text(max_size=12)),
        kind=kind,
        mode=Mode.COPY if recipe is None else Mode.STUB,
        offset=draw(st.integers(0, 2**40)),
        length=draw(st.integers(0, 2**32)),
        blob=blob,
        file=f"attachments/{year:04d}/{month:02d}/{file_name(blob, name)}",
        recipe=recipe,
    )


@given(part_records())
def test_round_trip(part: PartRecord) -> None:
    text = json.dumps(part.to_json(), ensure_ascii=False, separators=(",", ":"))
    assert PartRecord.from_json(json.loads(text)) == part


def design_part_keys() -> list[str]:
    """The keys of a ``parts`` entry, in the order the DESIGN.md record
    header example lists them (commented-out ones included)."""
    text = DESIGN.read_text()
    block = text[text.index("### Record header") :]
    block = block[block.index('"parts": [') :]
    block = block[: block.index("```")]
    return list(dict.fromkeys(re.findall(r'"(\w+)":', block)[1:]))


@given(part_records())
def test_keys_follow_design_order(part: PartRecord) -> None:
    order = design_part_keys()
    keys = list(part.to_json())
    assert [k for k in order if k in keys] == keys


def test_design_lists_every_key() -> None:
    recipe = Base64Recipe(76, "crlf", True, ((76, 3), (12, 1)), 2)
    keys = set(entry(recipe=recipe)) | {"crlf_to_lf"}
    assert set(design_part_keys()) == keys


def entry(**changes: Any) -> dict[str, Any]:
    recipe = changes.pop("recipe", Base64Recipe(76, "crlf", True))
    part = PartRecord(
        path="2",
        kind=PartClass.ATTACHMENT,
        mode=Mode.STUB,
        offset=1544,
        length=112,
        blob=BLOB,
        file=FILE,
        recipe=recipe,
    )
    obj = part.to_json()
    for key, value in changes.items():
        if value is None:
            del obj[key]
        else:
            obj[key] = value
    return obj


def test_example_entry() -> None:
    assert entry() == {
        "path": "2",
        "kind": "attachment",
        "mode": "stub",
        "offset": 1544,
        "length": 112,
        "blob": BLOB,
        "file": FILE,
        "enc": "base64",
        "line": 76,
        "eol": "crlf",
        "final_eol": True,
    }


def test_optional_fields_only_when_set() -> None:
    plain = IdentityRecipe(False, "lf", False)
    assert "crlf_to_lf" not in entry(recipe=plain)
    assert entry(recipe=IdentityRecipe(True, "crlf", True))["crlf_to_lf"] is True
    obj = entry(recipe=Base64Recipe(60, "lf", False, ((60, 2), (0, 1)), 3))
    assert obj["layout"] == [[60, 2], [0, 1]] and obj["pad_bits"] == 3


def copy_entry() -> dict[str, Any]:
    obj = entry(mode="copy")
    for key in ("enc", "line", "eol", "final_eol"):
        del obj[key]
    return obj


def test_copy_has_no_recipe() -> None:
    part = PartRecord.from_json(copy_entry())
    assert part.mode is Mode.COPY and part.recipe is None
    assert part.to_json() == copy_entry()


@pytest.mark.parametrize(
    "obj",
    [
        entry(extra=1),
        entry(path=None),
        entry(offset=None),
        entry(eol=None),
        entry(final_eol=None),
        entry(line=None),
        entry(enc="quoted-printable"),
        entry(enc=["base64"]),
        entry(kind="body"),
        entry(kind=["attachment"]),
        entry(mode="inline"),
        entry(offset=-1),
        entry(length=1.5),
        entry(offset=True),
        entry(final_eol=1),
        entry(eol="cr"),
        entry(line=0),
        entry(layout=[]),
        entry(layout=[[76]]),
        entry(layout=[[76, 0]]),
        entry(layout=[[-1, 2]]),
        entry(layout=[[True, 2]]),
        entry(layout={"76": 2}),
        entry(pad_bits=0),
        entry(pad_bits=16),
        entry(crlf_to_lf=True),
        entry(recipe=IdentityRecipe(False, "lf", True), crlf_to_lf=False),
        entry(recipe=IdentityRecipe(False, "lf", True), line=76),
        entry(recipe=IdentityRecipe(False, "lf", True), layout=[[1, 1]]),
        entry(blob="sha256:" + "0" * 63),
        entry(blob="md5:" + "0" * 32),
        entry(file="attachments/2026/10/0000000000000000_quarterly-report.pdf"),
        entry(file="attachments/2026/10/3fa9c1e07b22d4a1_../x"),
        entry(file="attachments/2026/10/../3fa9c1e07b22d4a1_x"),
        entry(file="/etc/3fa9c1e07b22d4a1_x"),
        entry(file="attachments/2026/10/3fa9c1e07b22d4a1_.hidden"),
        entry(file="attachments/2026/10/3fa9c1e07b22d4a1_ x"),
        entry(mode="copy"),
        {**copy_entry(), "kind": "decoration"},
        {**copy_entry(), "crlf_to_lf": True},
        [],
    ],
)
def test_rejects(obj: object) -> None:
    with pytest.raises(ValueError):
        PartRecord.from_json(obj)


def test_parse_parts_checks_order_and_range() -> None:
    a = entry(offset=10, length=5)
    b = entry(offset=15, length=5)
    assert [p.offset for p in parse_parts([a, b], 20)] == [10, 15]
    with pytest.raises(ValueError):
        parse_parts([b, a], 20)
    with pytest.raises(ValueError):
        parse_parts([a, entry(offset=14, length=5)], 20)
    with pytest.raises(ValueError):
        parse_parts([a, b], 19)


def test_corpus_reconstructs_from_serialised_parts(
    corpus_message: tuple[str, bytes],
) -> None:
    """Strip every extractable part, write the parts as a header would, and
    rebuild the message from the parsed-back header and the decoded files."""
    _, data = corpus_message
    plans = [p for p in plan_message(data, EXTRACT_ALL) if p.mode is not Mode.INLINE]
    files: dict[str, bytes] = {}
    records, cuts = [], []
    for p in plans:
        assert p.content is not None
        blob = blob_id(p.content)
        name = attachment_name(p.part.filename, p.part.path, p.part.content_type)
        file = f"attachments/2026/10/{file_name(blob, name)}"
        files[file] = p.content
        if p.recipe is not None:
            cuts.append(Cut(p.part.body_start, p.part.end, file, p.recipe))
        records.append((p, blob, file))
    stripped, made = strip(data, cuts)
    stub_at = {cut.start: stub for cut, stub in zip(cuts, made, strict=True)}

    def shift(pos: int) -> int:
        """Where ``pos`` (not inside a cut) lands in the stripped message."""
        return pos + sum(
            stub_at[c.start].length - (c.end - c.start) for c in cuts if c.end <= pos
        )

    entries = []
    for p, blob, file in records:
        if p.recipe is not None:
            stub = stub_at[p.part.body_start]
            offset, length = stub.offset, stub.length
        else:
            offset, length = shift(p.part.body_start), p.part.end - p.part.body_start
        part = PartRecord(
            path=p.part.path,
            kind=p.cls,
            mode=p.mode,
            offset=offset,
            length=length,
            blob=blob,
            file=file,
            recipe=p.recipe,
        )
        entries.append(part.to_json())
    parsed = parse_parts(json.loads(json.dumps(entries)), len(stripped))
    for part, (p, _, _) in zip(parsed, records, strict=True):
        if part.mode is Mode.COPY:
            body = data[p.part.body_start : p.part.end]
            assert stripped[part.offset : part.end] == body
    contents = [files[part.file] for part in parsed if part.recipe is not None]
    assert reconstruct(stripped, stubs(parsed), contents) == data
