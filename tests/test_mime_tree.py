import email
from dataclasses import dataclass, field
from email import policy

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from geniza.mime.tree import Kind, Part, parse


def check_layout(data: bytes, root: Part) -> None:
    """Leaf bodies are in order, do not overlap and lie inside the input, and
    every node's ranges nest inside its parent's."""
    pos = 0
    for leaf in root.leaves():
        assert pos <= leaf.body_start <= leaf.end <= len(data)
        pos = leaf.end

    def nest(node: Part) -> None:
        assert node.start <= node.header_end <= node.body_start <= node.end
        for child in node.children:
            assert node.body_start <= child.start and child.end <= node.end
            nest(child)

    nest(root)
    # The leaf bodies and the bytes between them concatenate back to the
    # input.
    pieces, pos = [], 0
    for leaf in root.leaves():
        pieces += [data[pos : leaf.body_start], data[leaf.body_start : leaf.end]]
        pos = leaf.end
    pieces.append(data[pos:])
    assert b"".join(pieces) == data


def test_corpus_layout(corpus_message: tuple[str, bytes]) -> None:
    _, data = corpus_message
    check_layout(data, parse(data))


def stdlib_leaves(data: bytes) -> list[tuple[str, bytes | None]]:
    """Leaves as the stdlib parser sees them. It parses
    message/delivery-status into header blocks; that part is reported as a
    leaf with no body to compare."""
    out: list[tuple[str, bytes | None]] = []

    def visit(part: email.message.Message) -> None:
        if part.get_content_type() == "message/delivery-status":
            out.append((part.get_content_type(), None))
            return
        payload = part._payload  # raw, unlike get_payload() for 8bit parts
        if isinstance(payload, list):
            for sub in payload:
                visit(sub)
            return
        out.append(
            (part.get_content_type(), payload.encode("ascii", "surrogateescape"))
        )

    visit(email.message_from_bytes(data, policy=policy.compat32))
    return out


def test_corpus_matches_stdlib(corpus_message: tuple[str, bytes]) -> None:
    """On the corpus, the leaves and their raw bodies agree with the stdlib
    parser (where the stdlib parser produces a structure at all)."""
    name, data = corpus_message
    theirs = stdlib_leaves(data)
    ours: list[tuple[str, bytes | None]] = [
        (
            leaf.content_type,
            None
            if leaf.content_type == "message/delivery-status"
            else data[leaf.body_start : leaf.end],
        )
        for leaf in parse(data).leaves()
    ]
    if name in {"damaged_no_boundary_param", "damaged_boundary_not_found"}:
        # Both treat the body as one opaque part.
        assert len(ours) == len(theirs) == 1
        return
    if name == "damaged_headers_only_part":
        # The stdlib parser keeps the header-only part's header lines as the
        # body of a part without headers; we parse them as headers.
        assert [t for t, _ in ours] == [
            "text/plain",
            "text/plain",
            "application/octet-stream",
        ]
        return
    if name == "damaged_missing_final_boundary":
        # Without a close delimiter, the stdlib parser drops the last line
        # ending of the last part; we keep it.
        last_type, last_body = ours[-1]
        assert last_body is not None
        ours[-1] = (last_type, last_body.removesuffix(b"\r\n"))
    assert ours == theirs


def test_paths() -> None:
    from conftest import CORPUS

    root = parse((CORPUS / "forwarded_rfc822.eml").read_bytes())
    assert [(n.path, n.kind) for n in root.walk()] == [
        ("", Kind.MULTIPART),
        ("1", Kind.LEAF),
        ("2", Kind.MESSAGE),
        ("2", Kind.MULTIPART),
        ("2.1", Kind.LEAF),
        ("2.2", Kind.LEAF),
    ]
    single = parse((CORPUS / "single_part_attachment.eml").read_bytes())
    assert single.path == "1" and single.kind is Kind.LEAF


def test_damaged_structure() -> None:
    from conftest import CORPUS

    def tree(name: str) -> list[tuple[str, str, int]]:
        data = (CORPUS / f"{name}.eml").read_bytes()
        return [
            (leaf.path, leaf.content_type, leaf.end - leaf.body_start)
            for leaf in parse(data).leaves()
        ]

    # The truncated last part runs to the end of the message.
    assert [p for p, _, _ in tree("damaged_missing_final_boundary")] == ["1", "2"]
    assert [t for _, t, _ in tree("damaged_boundary_whitespace")] == [
        "text/plain",
        "application/pdf",
    ]
    # "--bndX" in the text is not a delimiter, and the inner boundary
    # "bnd2" is not mistaken for the outer "bnd".
    assert [(p, t) for p, t, _ in tree("damaged_boundary_prefix")] == [
        ("1.1", "text/plain"),
        ("1.2", "text/html"),
        ("2", "application/pdf"),
    ]
    assert tree("damaged_headers_no_body") == [("1", "text/plain", 0)]
    no_boundary = parse((CORPUS / "damaged_no_boundary_param.eml").read_bytes())
    assert no_boundary.kind is Kind.LEAF and no_boundary.defects


# --- generated messages -----------------------------------------------------


@dataclass
class Gen:
    """A generated message together with what parsing it should yield."""

    data: bytes
    leaves: list[tuple[str, bytes]] = field(default_factory=list)


SAFE_LINE = st.text(
    alphabet=st.characters(
        codec="utf-8", exclude_characters="\r\n", exclude_categories=["Cs"]
    ),
    max_size=30,
).filter(lambda s: not s.startswith("-"))

LEAF_TYPES = st.sampled_from(
    ["text/plain", "text/html", "application/pdf", "image/png", "text/x-patch"]
)
BOUNDARY = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789_=.", min_size=1, max_size=20
)


@st.composite
def bodies(draw: st.DrawFn, eol: bytes) -> bytes:
    lines = draw(st.lists(SAFE_LINE, max_size=5))
    text = eol.join(line.encode("utf-8") for line in lines)
    if lines and draw(st.booleans()):
        text += eol
    return text


@st.composite
def leaf(draw: st.DrawFn, eol: bytes) -> Gen:
    ctype = draw(LEAF_TYPES)
    body = draw(bodies(eol))
    head = b"Content-Type: " + ctype.encode() + eol
    if draw(st.booleans()):
        head += b'Content-Disposition: attachment; filename="f.bin"' + eol
    return Gen(head + eol + body, [(ctype, body)])


@st.composite
def node(draw: st.DrawFn, eol: bytes, depth: int, used: frozenset[str]) -> Gen:
    if depth >= 3 or draw(st.integers(0, 2)) == 0:
        return draw(leaf(eol))
    if draw(st.integers(0, 4)) == 0:
        inner = draw(node(eol, depth + 1, used))
        head = b"Content-Type: message/rfc822" + eol
        return Gen(head + eol + b"Subject: inner" + eol + inner.data, inner.leaves)
    # Boundaries are distinct and no boundary is a prefix of another, as
    # RFC 2046 requires of a well-formed message.
    boundary = draw(
        BOUNDARY.filter(
            lambda b: all(not (b.startswith(u) or u.startswith(b)) for u in used)
        )
    )
    used = used | {boundary}
    bnd = boundary.encode()
    children = draw(st.lists(node(eol, depth + 1, used), min_size=1, max_size=4))
    out = b'Content-Type: multipart/mixed; boundary="' + bnd + b'"' + eol + eol
    if draw(st.booleans()):
        out += draw(bodies(eol)) + eol  # preamble
    gen = Gen(b"")
    for child in children:
        out += b"--" + bnd + draw(st.sampled_from([b"", b" ", b"\t "])) + eol
        out += child.data + eol
        gen.leaves += child.leaves
    out += b"--" + bnd + b"--"
    if draw(st.booleans()):
        out += eol + draw(bodies(eol))  # epilogue
    gen.data = out
    return gen


@st.composite
def messages(draw: st.DrawFn) -> Gen:
    eol = draw(st.sampled_from([b"\r\n", b"\n"]))
    top = draw(node(eol, 0, frozenset()))
    head = b"From: a@example.org" + eol + b"Subject: generated" + eol
    return Gen(head + top.data, top.leaves)


@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
@given(messages())
def test_generated_messages_round_trip(gen: Gen) -> None:
    root = parse(gen.data)
    check_layout(gen.data, root)
    got = [
        (leaf.content_type, gen.data[leaf.body_start : leaf.end])
        for leaf in root.leaves()
    ]
    assert got == gen.leaves


@settings(max_examples=300)
@given(st.binary(max_size=2000))
def test_arbitrary_bytes_never_raise(data: bytes) -> None:
    check_layout(data, parse(data))


@settings(max_examples=200)
@given(st.data())
def test_damaged_corpus_never_raises(data: st.DataObject) -> None:
    from conftest import corpus_files

    path = data.draw(st.sampled_from(corpus_files()))
    raw = path.read_bytes()
    cut = data.draw(st.integers(0, len(raw)))
    junk = data.draw(st.binary(max_size=50))
    damaged = raw[:cut] + junk + raw[cut + data.draw(st.integers(0, 100)) :]
    check_layout(damaged, parse(damaged))
