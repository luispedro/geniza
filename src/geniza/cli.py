"""Command-line interface.

Output conventions, shared by every command:

- results go to stdout, as a plain-text summary by default or as JSON with
  ``--json``;
- diagnostics and warnings go through :mod:`logging` to stderr; ``-v`` and
  ``-q`` adjust the level. Addresses and subjects may appear at ``-v`` but
  never at the default level.
"""

import argparse
import json
import logging
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from geniza import __version__
from geniza.config import ConfigError

log = logging.getLogger("geniza")


@dataclass
class Output:
    """Where a command sends its result."""

    as_json: bool

    def result(self, summary: str, data: Any) -> None:
        """Print ``summary`` as text, or ``data`` as JSON with ``--json``."""
        if self.as_json:
            json.dump(data, sys.stdout, indent=2)
            sys.stdout.write("\n")
        else:
            sys.stdout.write(summary)
            if summary and not summary.endswith("\n"):
                sys.stdout.write("\n")


type Handler = Callable[[argparse.Namespace, Output], int]


def _not_implemented(args: argparse.Namespace, out: Output) -> int:
    log.error("%s: not implemented yet", args.command_name)
    return 2


def _cmd_sync(args: argparse.Namespace, out: Output) -> int:
    from geniza.sync import base

    return base.run(args, out)


def _cmd_stats(args: argparse.Namespace, out: Output) -> int:
    from geniza import stats

    return stats.run(args, out)


def _cmd_search(args: argparse.Namespace, out: Output) -> int:
    from geniza import search

    return search.run(args, out)


def _cmd_show(args: argparse.Namespace, out: Output) -> int:
    from geniza import show

    return show.run(args, out)


def _cmd_export(args: argparse.Namespace, out: Output) -> int:
    from geniza import export

    return export.run(args, out)


def _cmd_verify(args: argparse.Namespace, out: Output) -> int:
    from geniza import verify

    return verify.run(args, out)


def _cmd_reindex(args: argparse.Namespace, out: Output) -> int:
    from geniza.index import reindex

    return reindex.run(args, out)


def _cmd_secret(args: argparse.Namespace, out: Output) -> int:
    from geniza import secret

    handler: Handler = getattr(secret, args.secret_command)
    return handler(args, out)


def _cmd_import_local(args: argparse.Namespace, out: Output) -> int:
    from geniza import debug

    return debug.import_local(args, out)


def _setup_logging(verbosity: int) -> None:
    level = {-1: logging.ERROR, 0: logging.WARNING, 1: logging.INFO}.get(
        verbosity, logging.DEBUG if verbosity > 1 else logging.ERROR
    )
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    root = logging.getLogger("geniza")
    root.handlers[:] = [handler]
    root.setLevel(level)
    root.propagate = False


def _common_options(*, top: bool) -> argparse.ArgumentParser:
    """Global options, accepted before and after the subcommand.

    Subcommand copies default to SUPPRESS so that they do not overwrite a
    value given before the subcommand.
    """

    def default(value: Any) -> Any:
        return value if top else argparse.SUPPRESS

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config",
        type=Path,
        metavar="PATH",
        default=default(None),
        help="configuration file",
    )
    common.add_argument(
        "--store", metavar="NAME", default=default(None), help="store to act on"
    )
    common.add_argument(
        "--json",
        action="store_true",
        default=default(False),
        help="machine-readable output on stdout",
    )
    common.add_argument(
        "-v", "--verbose", action="count", default=default(0), help="more diagnostics"
    )
    common.add_argument(
        "-q", "--quiet", action="count", default=default(0), help="fewer diagnostics"
    )
    return common


def build_parser() -> argparse.ArgumentParser:
    common = _common_options(top=False)

    parser = argparse.ArgumentParser(
        prog="geniza",
        description="One-way mail archiver.",
        parents=[_common_options(top=True)],
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def add(
        name: str, help: str, handler: Handler = _not_implemented
    ) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help, parents=[common])
        p.set_defaults(handler=handler, command_name=name)
        return p

    p = add("sync", "sync accounts, then evaluate incoming/", _cmd_sync)
    p.add_argument(
        "accounts",
        nargs="*",
        metavar="ACCOUNT",
        help="accounts to sync (default: all, or all of --store)",
    )
    p.add_argument(
        "--allow-mass-drop",
        action="store_true",
        help="drop messages from incoming/ even past the mass-drop guard",
    )

    p = add("search", "search messages, newest first", _cmd_search)
    p.add_argument(
        "query",
        nargs="+",
        metavar="QUERY",
        help='words, "phrases" and prefix* (all must match), FIELD:VALUE for'
        " FIELD in from, to, cc, subject, body, attachment, mailbox, date"
        " (2024, 2024-03..2024-06) or thread (a message id); -TERM negates"
        " (after --, or inside a quoted query)",
    )
    p.add_argument(
        "-n",
        "--limit",
        type=int,
        default=50,
        metavar="N",
        help="show at most N messages (0: all; default: %(default)s)",
    )
    p.add_argument(
        "--dropped",
        action="store_true",
        help="include messages dropped from incoming/, during their grace period",
    )

    p = add("show", "reconstruct a message", _cmd_show)
    p.add_argument("id", metavar="ID", help="message id, or a prefix of its digest")
    p.add_argument(
        "--raw", action="store_true", help="write the original message to stdout"
    )

    p = add("export", "export messages", _cmd_export)
    p.add_argument(
        "--mbox",
        type=Path,
        metavar="PATH",
        required=True,
        help="mboxrd file to create ('-' for stdout); compressed if it ends"
        " in .gz, .bz2, .xz, .zst or .zstd",
    )

    add("reindex", "rebuild cache/ from store/", _cmd_reindex)
    add("verify", "re-hash messages and attachments", _cmd_verify)

    p = add("stats", "dry-run part classification on a Maildir or mbox", _cmd_stats)
    p.add_argument("--parts", action="store_true", required=True)
    p.add_argument("path", type=Path, metavar="PATH", help="Maildir or mbox")
    p.add_argument(
        "--examples",
        action="store_true",
        help="also list identifying examples (paths, Message-IDs)",
    )
    p.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=0,
        metavar="N",
        help="worker processes (default: one per CPU)",
    )

    p = add("account", "manage accounts")
    asub = p.add_subparsers(dest="account_command", required=True)
    r = asub.add_parser("rename", help="rename an account", parents=[common])
    r.add_argument("old", metavar="OLD")
    r.add_argument("new", metavar="NEW")
    r.set_defaults(handler=_not_implemented, command_name="account rename")

    p = add("secret", "manage sshcrypt secrets")
    ssub = p.add_subparsers(dest="secret_command", required=True)
    for name, help in [
        ("keys", "list agent keys and whether they are usable"),
        ("encrypt", "encrypt a secret read from the terminal"),
        ("check", "check that every secret in the config decrypts"),
    ]:
        s = ssub.add_parser(name, help=help, parents=[common])
        s.set_defaults(handler=_cmd_secret, command_name=f"secret {name}")
        if name == "encrypt":
            s.add_argument(
                "--key",
                metavar="FINGERPRINT",
                help="key to encrypt to (default: the only usable key)",
            )

    p = add("debug", "development-only commands")
    dsub = p.add_subparsers(dest="debug_command", required=True)
    d = dsub.add_parser(
        "import-local",
        help="ingest a Maildir or mbox into a store (testing only)",
        parents=[common],
    )
    d.add_argument("path", type=Path, metavar="PATH", help="Maildir or mbox")
    d.set_defaults(handler=_cmd_import_local, command_name="debug import-local")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose - args.quiet)
    handler: Handler = args.handler
    try:
        return handler(args, Output(as_json=args.json))
    except ConfigError as e:
        log.error("%s", e)
        return 2
    except KeyboardInterrupt:
        log.error("interrupted")
        return 130
