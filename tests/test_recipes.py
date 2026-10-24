import base64
from dataclasses import replace
from email.message import EmailMessage
from itertools import groupby

from hypothesis import given
from hypothesis import strategies as st

from geniza.mime.recipes import (
    LAYOUT_BASE_RUNS,
    LAYOUT_BYTES_PER_RUN,
    Base64Recipe,
    IdentityRecipe,
    decode,
    encode,
    encode_base64,
    stub_bytes,
)
from geniza.mime.strip import Cut, reconstruct, strip
from geniza.mime.tree import Kind, Part


def leaf(body: bytes, cte: str, ctype: str = "application/octet-stream") -> Part:
    headers = EmailMessage()
    headers["Content-Type"] = ctype
    headers["Content-Transfer-Encoding"] = cte
    return Part("1", 0, 0, 0, len(body), Kind.LEAF, ctype, headers)


base64_recipes = st.builds(
    Base64Recipe,
    line=st.integers(4, 120),
    eol=st.sampled_from(["crlf", "lf"]),
    final_eol=st.booleans(),
)


@given(st.binary(min_size=1, max_size=3000), base64_recipes)
def test_base64_round_trip(content: bytes, recipe: Base64Recipe) -> None:
    body = encode_base64(content, recipe)
    decoded = decode(body, leaf(body, "base64"))
    assert decoded.content == content
    assert decoded.recipe is not None
    assert encode(content, decoded.recipe) == body


@st.composite
def base64_layouts(draw: st.DrawFn, content: bytes) -> list[bytes]:
    """Base64 text for ``content`` cut into lines of a usual length, with
    some lines of arbitrary length and blank lines anywhere."""
    encoded, lines = base64.b64encode(content), []
    usual = draw(st.integers(1, 120))
    lengths = st.one_of(st.just(usual), st.integers(0, 120))
    while encoded:
        n = draw(lengths)
        lines.append(encoded[:n])
        encoded = encoded[n:]
    return lines + draw(st.lists(st.just(b""), max_size=3))


@given(st.binary(min_size=1, max_size=3000), st.data())
def test_base64_layout_round_trip(content: bytes, data: st.DataObject) -> None:
    """Any cut of the base64 text into lines is either reproduced exactly or,
    if its layout is too irregular to be worth storing, copied."""
    lines = data.draw(base64_layouts(content))
    eol = data.draw(st.sampled_from([b"\r\n", b"\n"]))
    body = eol.join(lines) + data.draw(st.sampled_from([b"", eol]))
    decoded = decode(body, leaf(body, "base64"))
    assert decoded.content == content
    if decoded.recipe is None:
        runs = len(list(groupby(len(x) for x in lines)))
        assert runs > LAYOUT_BASE_RUNS + len(b"".join(lines)) // LAYOUT_BYTES_PER_RUN
    else:
        assert encode(content, decoded.recipe) == body


@given(
    st.binary(min_size=1, max_size=3000).filter(lambda b: len(b) % 3),
    base64_recipes,
    st.data(),
)
def test_base64_pad_bits_round_trip(
    content: bytes, recipe: Base64Recipe, data: st.DataObject
) -> None:
    """Non-zero bits before the padding, as some encoders leave them, are
    reproduced exactly."""
    bits = data.draw(st.integers(1, 0b1111 if len(content) % 3 == 1 else 0b11))
    body = encode_base64(content, replace(recipe, pad_bits=bits))
    assert body != encode_base64(content, recipe)
    decoded = decode(body, leaf(body, "base64"))
    assert decoded.content == content
    assert isinstance(decoded.recipe, Base64Recipe)
    assert decoded.recipe.pad_bits == bits
    assert encode(content, decoded.recipe) == body


def test_empty_base64() -> None:
    decoded = decode(b"", leaf(b"", "base64"))
    assert decoded.content == b""
    assert decoded.recipe is None
    assert decoded.reason == "empty"


@given(st.binary(min_size=1, max_size=3000), base64_recipes, st.data())
def test_base64_damage_never_stubs_wrongly(
    content: bytes, recipe: Base64Recipe, data: st.DataObject
) -> None:
    """Whatever is done to the body, a recipe is only reported if it
    reproduces the body exactly."""
    body = bytearray(encode_base64(content, recipe))
    pos = data.draw(st.integers(0, len(body)))
    body[pos:pos] = data.draw(st.sampled_from([b" ", b"\n", b"\r\n", b"=", b"A"]))
    damaged = bytes(body)
    decoded = decode(damaged, leaf(damaged, "base64"))
    if decoded.recipe is not None:
        assert decoded.content is not None
        assert encode(decoded.content, decoded.recipe) == damaged


lines = st.lists(
    st.binary(max_size=40).filter(lambda b: b"\r" not in b and b"\n" not in b)
)


@given(lines, st.sampled_from([b"\r\n", b"\n"]), st.booleans())
def test_identity_text_round_trip(parts: list[bytes], eol: bytes, final: bool) -> None:
    body = eol.join(parts) + (eol if final and parts else b"")
    decoded = decode(body, leaf(body, "7bit", "text/plain"))
    assert decoded.recipe is not None and decoded.content is not None
    assert b"\r" not in decoded.content
    assert encode(decoded.content, decoded.recipe) == body


@given(st.binary(max_size=500))
def test_identity_binary_is_unchanged(body: bytes) -> None:
    decoded = decode(body, leaf(body, "binary"))
    assert decoded.content == body
    assert isinstance(decoded.recipe, IdentityRecipe)
    assert encode(body, decoded.recipe) == body


@given(st.binary(max_size=500))
def test_identity_text_always_round_trips(body: bytes) -> None:
    decoded = decode(body, leaf(body, "8bit", "text/x-python"))
    assert decoded.recipe is not None and decoded.content is not None
    assert encode(decoded.content, decoded.recipe) == body
    assert stub_bytes("f", decoded.recipe).endswith(
        b"\n" if body.endswith(b"\n") else b"]"
    )


def test_qp_is_never_stubbed() -> None:
    body = b"caf=C3=A9 =3D 3\r\n"
    decoded = decode(body, leaf(body, "quoted-printable", "text/plain"))
    assert decoded.recipe is None and decoded.content == "café = 3\n".encode()


def test_unknown_encoding_is_not_decoded() -> None:
    decoded = decode(b"begin 644 x\n", leaf(b"begin 644 x\n", "x-uuencode"))
    assert decoded.content is None


@given(st.data())
def test_strip_reconstruct_round_trip(data: st.DataObject) -> None:
    """Splicing arbitrary stubs in and out restores the original bytes."""
    contents = data.draw(st.lists(st.binary(min_size=1, max_size=200), max_size=5))
    recipes = [data.draw(base64_recipes) for _ in contents]
    fillers = data.draw(
        st.lists(
            st.binary(max_size=50),
            min_size=len(contents) + 1,
            max_size=len(contents) + 1,
        )
    )
    message, cuts = bytearray(fillers[0]), []
    for i, (content, recipe) in enumerate(zip(contents, recipes, strict=True)):
        body = encode_base64(content, recipe)
        cuts.append(Cut(len(message), len(message) + len(body), f"f{i}", recipe))
        message += body + fillers[i + 1]
    stripped, stubs = strip(bytes(message), cuts)
    assert reconstruct(stripped, stubs, contents) == bytes(message)
