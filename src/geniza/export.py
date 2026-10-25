"""``geniza export --mbox PATH``: every message of a store, rebuilt, as mboxrd.

Messages are written in store order, each once, with the same ``From``
line and escaping as segment bodies (DESIGN.md, "Message storage"). mbox
has no length field, so a message that does not end with a newline gains
one when the file is read back.

A file name ending in ``.gz``, ``.bz2``, ``.xz``, ``.zst`` or ``.zstd`` is
compressed accordingly.
"""

import argparse
import io
import logging
import os
import sys
from collections.abc import Callable
from compression import bz2, gzip, lzma, zstd
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

from geniza.config import load, read_store
from geniza.store import reader
from geniza.store.attachments import AttachmentError
from geniza.store.reader import RebuildError
from geniza.store.segment import SegmentError, escape, from_line

if TYPE_CHECKING:
    from geniza.cli import Output

log = logging.getLogger(__name__)

type Compressor = Callable[[BinaryIO], io.BufferedIOBase]

#: Compressing wrappers by file suffix; closing one leaves the file open.
COMPRESSORS: dict[str, Compressor] = {
    ".gz": lambda f: gzip.GzipFile(fileobj=f, mode="wb"),
    ".bz2": lambda f: bz2.BZ2File(f, "wb"),
    ".xz": lambda f: lzma.LZMAFile(f, "wb"),
    ".zst": lambda f: zstd.ZstdFile(
        f, "wb", options={zstd.CompressionParameter.checksum_flag: 1}
    ),
}
COMPRESSORS[".zstd"] = COMPRESSORS[".zst"]


def write_mbox(store: Path, out: BinaryIO | io.BufferedIOBase) -> tuple[int, int, int]:
    """Write the store's messages to ``out``; returns (messages, bytes,
    errors). A message that cannot be rebuilt is logged and left out."""
    seen: set[str] = set()
    messages = size = errors = 0
    for record in reader.records(store):
        header = record.header
        if header.id in seen:
            continue
        seen.add(header.id)
        try:
            data = reader.rebuild(store, header, record.stripped)
        except (RebuildError, AttachmentError, OSError) as e:
            log.error("%s", e)
            errors += 1
            continue
        out.write(from_line(header.received) + escape(data) + b"\n")
        messages += 1
        size += len(data)
    return messages, size, errors


def run(args: argparse.Namespace, out: Output) -> int:
    store = load(args.config).store(args.store).path
    read_store(store)
    target: Path = args.mbox
    try:
        if str(target) == "-":
            messages, size, errors = write_mbox(store, sys.stdout.buffer)
            sys.stdout.buffer.flush()
        else:
            compress = COMPRESSORS.get(target.suffix.lower())
            with open(target, "xb") as f:
                try:
                    if compress is None:
                        messages, size, errors = write_mbox(store, f)
                    else:
                        with compress(f) as z:
                            messages, size, errors = write_mbox(store, z)
                    f.flush()
                    os.fsync(f.fileno())
                except BaseException:
                    target.unlink(missing_ok=True)
                    raise
    except FileExistsError:
        log.error("%s: already exists", target)
        return 1
    except SegmentError as e:
        log.error("%s", e)
        return 1
    summary = f"exported {messages} messages ({size} bytes)"
    if errors:
        summary += f"; {errors} could not be rebuilt"
    if str(target) == "-":
        log.info("%s", summary)
    else:
        out.result(summary, {"messages": messages, "bytes": size, "errors": errors})
    return 1 if errors else 0
