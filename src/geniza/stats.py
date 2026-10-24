"""``geniza stats --parts``: dry-run part classification over local mail.

Reads a Maildir (or a tree of Maildirs, as offlineimap writes them) or an
mbox, classifies every part as ingest would, and reports aggregate numbers:
nothing is stored. Without ``--examples`` the output identifies no message,
so it can be pasted into an issue or a design discussion.
"""

import argparse
import hashlib
import logging
import mailbox
import os
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from geniza.mime.classify import PartClass, classify
from geniza.mime.names import attachment_name
from geniza.mime.recipes import Mode, Policy, decode, plan_part
from geniza.mime.tree import parse

if TYPE_CHECKING:
    from geniza.cli import Output

log = logging.getLogger(__name__)

KiB = 1024
MiB = 1024 * KiB

THRESHOLDS = [16 * KiB, 64 * KiB, 128 * KiB, 256 * KiB, 1 * MiB]
HISTOGRAM = [1 * KiB, 4 * KiB, 16 * KiB, 64 * KiB, 256 * KiB, 1 * MiB, 4 * MiB]
EXAMPLES_PER_CATEGORY = 5

# Length of "[geniza: stored as attachments/YYYY/MM/<16 hex>_" + "]".
_STUB_OVERHEAD = len("[geniza: stored as attachments/2026/10/") + 16 + 1 + 1


# --- per-message analysis (runs in worker processes) ------------------------


@dataclass(slots=True, frozen=True)
class PartInfo:
    cls: PartClass
    content_type: str
    encoded_size: int
    #: None when the transfer encoding is not understood.
    decoded_size: int | None
    #: sha256 of the decoded content, for attachments and decorations.
    digest: bytes | None
    #: Whether a recipe reproduces the body (stub possible).
    stubbable: bool
    #: Attachment mode (decorations depend on the threshold).
    mode: Mode
    reason: str | None
    has_filename: bool
    stub_size: int


@dataclass(slots=True, frozen=True)
class MessageInfo:
    source: str
    size: int
    digest: bytes
    message_id: str | None
    defects: tuple[str, ...]
    parts: tuple[PartInfo, ...]


def analyse(source: str, data: bytes) -> MessageInfo:
    root = parse(data)
    # Plan with every decoration extractable; thresholds are applied later.
    policy = Policy(min_size=1, decoration_min_size=0)
    parts = []
    for c in classify(root):
        part = c.part
        encoded_size = part.end - part.body_start
        digest = None
        decoded_size: int | None = encoded_size
        stubbable = False
        mode, reason = Mode.INLINE, None
        stub_size = 0
        if c.cls in (PartClass.ATTACHMENT, PartClass.DECORATION):
            plan = plan_part(data, c, policy)
            content = plan.content
            decoded_size = None if content is None else len(content)
            if content is not None:
                digest = hashlib.sha256(content).digest()
            stubbable = plan.recipe is not None
            mode, reason = plan.mode, plan.reason
            name = attachment_name(part.filename, part.path, part.content_type)
            stub_size = _STUB_OVERHEAD + len(name.encode()) + 2
        elif part.transfer_encoding in ("base64", "quoted-printable"):
            content = decode(data, part).content
            decoded_size = None if content is None else len(content)
        parts.append(
            PartInfo(
                cls=c.cls,
                content_type=part.content_type,
                encoded_size=encoded_size,
                decoded_size=decoded_size,
                digest=digest,
                stubbable=stubbable,
                mode=mode,
                reason=reason,
                has_filename=bool(part.filename),
                stub_size=stub_size,
            )
        )
    defects = tuple(d for node in root.walk() for d in node.defects)
    message_id = root.header("message-id")
    return MessageInfo(
        source=source,
        size=len(data),
        digest=hashlib.sha256(data).digest(),
        message_id=message_id.strip() if message_id and message_id.strip() else None,
        defects=defects,
        parts=tuple(parts),
    )


def _analyse_file(path: str) -> MessageInfo | str:
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as exc:
        return f"{path}: {exc.strerror}"
    return analyse(path, data)


def _analyse_bytes(item: tuple[str, bytes]) -> MessageInfo:
    return analyse(*item)


# --- sources ----------------------------------------------------------------


def _is_maildir(path: Path) -> bool:
    return (path / "cur").is_dir() and (path / "new").is_dir()


def maildir_files(root: Path) -> Iterator[str]:
    """Message files of every Maildir under ``root`` (including ``root``)."""
    for dirpath, dirnames, _ in os.walk(root):
        here = Path(dirpath)
        if _is_maildir(here):
            for sub in ("cur", "new"):
                with os.scandir(here / sub) as entries:
                    for entry in entries:
                        if entry.is_file() and not entry.name.startswith("."):
                            yield entry.path
        dirnames[:] = sorted(d for d in dirnames if d not in ("cur", "new", "tmp"))


def mbox_messages(path: Path) -> Iterator[tuple[str, bytes]]:
    box = mailbox.mbox(path, create=False)
    try:
        for key in box.iterkeys():
            yield f"{path}#{key}", box.get_bytes(key)
    finally:
        box.close()


def scan(path: Path, jobs: int) -> Iterator[MessageInfo | str]:
    chunksize = 64
    if path.is_dir():
        files: Iterable[str] = maildir_files(path)
        if jobs <= 1:
            yield from map(_analyse_file, files)
            return
        with ProcessPoolExecutor(jobs) as pool:
            yield from pool.map(_analyse_file, files, chunksize=chunksize)
    else:
        messages = mbox_messages(path)
        if jobs <= 1:
            yield from map(_analyse_bytes, messages)
            return
        with ProcessPoolExecutor(jobs) as pool:
            yield from pool.map(_analyse_bytes, messages, chunksize=chunksize)


# --- aggregation ------------------------------------------------------------


def _bucket(size: int) -> str:
    for limit in HISTOGRAM:
        if size < limit:
            return f"<{_human(limit)}"
    return f">={_human(HISTOGRAM[-1])}"


def _human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(n) < 1024 or unit == "GiB":
            return f"{n:.0f}{unit}" if unit == "B" or n == int(n) else f"{n:.1f}{unit}"
        n /= 1024
    raise AssertionError


@dataclass
class ClassStats:
    count: int = 0
    encoded_bytes: int = 0
    decoded_bytes: int = 0
    histogram: Counter[str] = field(default_factory=Counter)
    modes: Counter[str] = field(default_factory=Counter)
    reasons: Counter[str] = field(default_factory=Counter)
    content_types: Counter[str] = field(default_factory=Counter)


@dataclass
class Totals:
    messages: int = 0
    total_bytes: int = 0
    unique_bytes: int = 0
    read_errors: int = 0
    unique: set[bytes] = field(default_factory=set)
    no_message_id: int = 0
    message_ids: dict[str, set[bytes]] = field(default_factory=lambda: defaultdict(set))
    with_defects: int = 0
    defects: Counter[str] = field(default_factory=Counter)
    classes: dict[PartClass, ClassStats] = field(
        default_factory=lambda: {c: ClassStats() for c in PartClass}
    )
    # threshold -> [stubbed body bytes removed, stub bytes added]
    inline_delta: dict[int, list[int]] = field(
        default_factory=lambda: {t: [0, 0] for t in THRESHOLDS}
    )
    attachment_blobs: Counter[bytes] = field(default_factory=Counter)
    attachment_blob_sizes: dict[bytes, int] = field(default_factory=dict)
    # decorations that can be stubbed: digest -> (decoded size, occurrences)
    decoration_blobs: Counter[bytes] = field(default_factory=Counter)
    decoration_blob_sizes: dict[bytes, int] = field(default_factory=dict)
    decorations_with_filename: int = 0
    examples: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    decoration_examples: dict[bytes, str] = field(default_factory=dict)

    def example(self, category: str, msg: MessageInfo) -> None:
        bucket = self.examples[category]
        entry = f"{msg.source}  {msg.message_id or '(no Message-ID)'}"
        if len(bucket) < EXAMPLES_PER_CATEGORY and entry not in bucket:
            bucket.append(entry)

    def add(self, msg: MessageInfo) -> None:
        self.messages += 1
        self.total_bytes += msg.size
        if msg.digest in self.unique:
            return  # an identical copy (another folder) is stored once
        self.unique.add(msg.digest)
        self.unique_bytes += msg.size
        if msg.message_id is None:
            self.no_message_id += 1
            self.example("no Message-ID", msg)
        else:
            self.message_ids[msg.message_id].add(msg.digest)
        if msg.defects:
            self.with_defects += 1
            self.defects.update(set(msg.defects))
            for d in set(msg.defects):
                self.example(f"defect: {d}", msg)
        for p in msg.parts:
            self._add_part(msg, p)

    def _add_part(self, msg: MessageInfo, p: PartInfo) -> None:
        cs = self.classes[p.cls]
        cs.count += 1
        cs.encoded_bytes += p.encoded_size
        size = p.decoded_size if p.decoded_size is not None else p.encoded_size
        cs.decoded_bytes += size
        cs.histogram[_bucket(size)] += 1
        cs.content_types[p.content_type] += 1
        if p.cls is PartClass.ATTACHMENT:
            cs.modes[p.mode] += 1
            if p.reason:
                cs.reasons[p.reason] += 1
            if p.mode is Mode.COPY:
                self.example(f"attachment copy: {p.reason}", msg)
            elif p.decoded_size is None:
                self.example(f"attachment not decoded: {p.reason}", msg)
            if p.mode in (Mode.STUB, Mode.COPY) and p.digest is not None:
                self.attachment_blobs[p.digest] += 1
                self.attachment_blob_sizes[p.digest] = size
            if p.mode is Mode.STUB:
                for delta in self.inline_delta.values():
                    delta[0] += p.encoded_size
                    delta[1] += p.stub_size
        elif p.cls is PartClass.DECORATION:
            cs.modes["stubbable" if p.stubbable else "never stubbable"] += 1
            if p.reason:
                cs.reasons[p.reason] += 1
            if p.has_filename:
                self.decorations_with_filename += 1
            if p.stubbable and p.digest is not None:
                self.decoration_blobs[p.digest] += 1
                self.decoration_blob_sizes[p.digest] = size
                self.decoration_examples.setdefault(p.digest, msg.source)
                for t, delta in self.inline_delta.items():
                    if size >= t:
                        delta[0] += p.encoded_size
                        delta[1] += p.stub_size

    # --- report ---

    def report(self) -> dict[str, Any]:
        dup_ids = sum(1 for ds in self.message_ids.values() if len(ds) > 1)
        thresholds = []
        for t in THRESHOLDS:
            removed, added = self.inline_delta[t]
            files = set(self.attachment_blobs)
            files.update(d for d, s in self.decoration_blob_sizes.items() if s >= t)
            file_bytes = sum(
                self.attachment_blob_sizes.get(d) or self.decoration_blob_sizes[d]
                for d in files
            )
            thresholds.append(
                {
                    "decoration_min_size": t,
                    "files": len(files),
                    "file_bytes": file_bytes,
                    "inline_bytes": self.unique_bytes - removed + added,
                    "decoration_files": sum(
                        1 for s in self.decoration_blob_sizes.values() if s >= t
                    ),
                }
            )
        repeats = Counter(self.decoration_blobs.values())
        saved = sum(
            (n - 1) * self.decoration_blob_sizes[d]
            for d, n in self.decoration_blobs.items()
        )
        return {
            "messages": self.messages,
            "bytes": self.total_bytes,
            "unique_messages": len(self.unique),
            "unique_bytes": self.unique_bytes,
            "read_errors": self.read_errors,
            "no_message_id": self.no_message_id,
            "duplicate_message_ids": dup_ids,
            "with_defects": self.with_defects,
            "defects": dict(self.defects.most_common()),
            "classes": {
                str(c): {
                    "count": s.count,
                    "encoded_bytes": s.encoded_bytes,
                    "decoded_bytes": s.decoded_bytes,
                    "histogram": {
                        b: s.histogram[b]
                        for b in [_bucket(h - 1) for h in HISTOGRAM]
                        + [_bucket(HISTOGRAM[-1])]
                    },
                    "modes": dict(s.modes),
                    "reasons": dict(s.reasons.most_common()),
                    "top_content_types": dict(s.content_types.most_common(8)),
                }
                for c, s in self.classes.items()
            },
            "attachments": {
                "occurrences": sum(self.attachment_blobs.values()),
                "unique_blobs": len(self.attachment_blobs),
            },
            "decorations": {
                "with_filename": self.decorations_with_filename,
                "stubbable_occurrences": sum(self.decoration_blobs.values()),
                "unique_blobs": len(self.decoration_blobs),
                "repeat_histogram": {str(k): v for k, v in sorted(repeats.items())},
                "bytes_saved_by_dedup": saved,
            },
            "thresholds": thresholds,
        }


def _text_report(r: dict[str, Any]) -> str:
    h = _human
    lines = [
        f"messages: {r['messages']} ({h(r['bytes'])}); "
        f"unique: {r['unique_messages']} ({h(r['unique_bytes'])})",
        f"without Message-ID: {r['no_message_id']}; "
        f"Message-IDs shared by different messages: {r['duplicate_message_ids']}",
        f"messages with parse defects: {r['with_defects']}",
    ]
    if r["read_errors"]:
        lines.append(f"unreadable files: {r['read_errors']}")
    for name, n in r["defects"].items():
        lines.append(f"  {name}: {n}")
    lines.append("")
    lines.append("parts (unique messages):")
    lines.append(f"  {'class':<11} {'count':>9} {'encoded':>10} {'decoded':>10}")
    for c, s in r["classes"].items():
        lines.append(
            f"  {c:<11} {s['count']:>9} {h(s['encoded_bytes']):>10} "
            f"{h(s['decoded_bytes']):>10}"
        )
    lines.append("")
    buckets = list(next(iter(r["classes"].values()))["histogram"])
    lines.append("size histogram (decoded size):")
    lines.append(f"  {'class':<11} " + " ".join(f"{b:>8}" for b in buckets))
    for c, s in r["classes"].items():
        lines.append(
            f"  {c:<11} " + " ".join(f"{s['histogram'][b]:>8}" for b in buckets)
        )
    lines.append("")
    a, d = r["attachments"], r["decorations"]
    for c in ("attachment", "decoration"):
        s = r["classes"][c]
        modes = ", ".join(f"{k} {v}" for k, v in s["modes"].items()) or "none"
        lines.append(f"{c}s: {modes}")
        for reason, n in s["reasons"].items():
            lines.append(f"  {reason}: {n}")
        types = ", ".join(f"{k} {v}" for k, v in s["top_content_types"].items())
        lines.append(f"  types: {types}")
        if c == "attachment":
            lines.append(
                f"  files: {a['unique_blobs']} unique of {a['occurrences']} extracted"
            )
    lines.append(
        f"  with a filename: {d['with_filename']}; "
        f"stubbable: {d['unique_blobs']} unique of {d['stubbable_occurrences']}"
    )
    rep = ", ".join(f"{k}x: {v}" for k, v in d["repeat_histogram"].items())
    lines.append(f"  identical decorations, blobs by repeat count: {rep or 'none'}")
    lines.append(f"  bytes saved by deduplicating them: {h(d['bytes_saved_by_dedup'])}")
    lines.append("")
    lines.append("by decoration_min_size:")
    lines.append(
        f"  {'threshold':>9} {'files':>8} {'file bytes':>11} {'decor.':>7} "
        f"{'inline bytes':>13}"
    )
    for t in r["thresholds"]:
        lines.append(
            f"  {h(t['decoration_min_size']):>9} {t['files']:>8} "
            f"{h(t['file_bytes']):>11} {t['decoration_files']:>7} "
            f"{h(t['inline_bytes']):>13}"
        )
    return "\n".join(lines) + "\n"


def _examples_report(totals: Totals) -> str:
    lines = ["", "examples:"]
    for category in sorted(totals.examples):
        lines.append(f"  {category}:")
        lines.extend(f"    {e}" for e in totals.examples[category])
    top = totals.decoration_blobs.most_common(EXAMPLES_PER_CATEGORY)
    if top:
        lines.append("  most repeated decorations:")
        for digest, n in top:
            size = _human(totals.decoration_blob_sizes[digest])
            lines.append(f"    {n}x {size}  {totals.decoration_examples[digest]}")
    return "\n".join(lines) + "\n"


def run(args: argparse.Namespace, out: Output) -> int:
    path: Path = args.path
    if not path.exists():
        log.error("%s: no such file or directory", path)
        return 1
    if path.is_dir() and not any(True for _ in maildir_files(path)):
        log.error("%s: no Maildir messages found", path)
        return 1
    jobs = args.jobs or os.process_cpu_count() or 1
    totals = Totals()
    for result in scan(path, jobs):
        if isinstance(result, str):
            totals.read_errors += 1
            log.warning("cannot read %s", result)
            continue
        totals.add(result)
        if totals.messages % 10000 == 0:
            log.info("%d messages", totals.messages)
    report = totals.report()
    text = _text_report(report)
    if args.examples:
        report["examples"] = dict(totals.examples)
        text += _examples_report(totals)
    out.result(text, report)
    return 0
