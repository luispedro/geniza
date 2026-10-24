# geniza

**Goals**: This is intended as primarily a personal project, but it is
open-source and, should you find it useful, you are welcome to use it and pull
requests/feature suggestions are welcome (nowadays, a good feature suggestion
is basically a pull request).

A one-way mail archiver: it pulls mail from your server into a local store
that is

- **friendly to deduplicating backups** (borg, restic): almost everything is
  append-only, and files are sealed once their month is over, so a backup
  only ever sees new files and new bytes at the end of a few files;
- **searchable**, through a local full-text index that can always be
  rebuilt from the store;
- **made of few, large files**: messages go into monthly zstd segments
  instead of one file per message;
- **keeping attachments as real files** you can browse, while every message
  can still be reconstructed byte for byte.

geniza never modifies the server: it only reads.

> **Status: under development, not yet usable for archiving.** The storage
> primitives (segments, attachment files, journal) are done; sync, search
> and the rest of the CLI are not. The only working command is
> `geniza stats --parts`. The first release will support JMAP, with
> Fastmail as the reference server; IMAP comes after it.

## How it works

A store is split in two trees:

```
store/          # the archive: back this up
  segments/       # raw messages, attachments stripped out, one zstd file per month
  attachments/    # attachments as ordinary, content-addressed files
  incoming/       # recent INBOX mail, kept unmodified for a few days
  state/          # per-account journal of mailbox membership and flags
cache/          # the search index; derived from store/, safe to delete
```

Messages are hashed as raw bytes. When an attachment is taken out of a
message, geniza records how to put it back and checks that the original
bytes (and hash) are reproduced before anything is committed. `cache/`
carries a `CACHEDIR.TAG`, so `borg create --exclude-caches` skips it, and
`geniza reindex` rebuilds it.

Secrets in the configuration are either encrypted with an SSH key held in
`ssh-agent` (`sshcrypt:` values) or fetched by a command (`token_command`,
e.g. from `pass`); plain-text passwords are rejected.

See [`DESIGN.md`](DESIGN.md) for the full specification (on-disk formats,
sync, message matching, configuration) and [`PLAN.md`](PLAN.md) for the
milestone plan.

## Trying it

geniza needs Python 3.15 and is managed with [pixi](https://pixi.sh):

```sh
git clone https://github.com/luispedro/geniza.git
cd geniza
pixi run geniza --help
```

`stats --parts` scans an existing Maildir or mbox, writes nothing, and
reports how its MIME parts would be classified and stored (counts, sizes,
which attachments would become files):

```sh
pixi run geniza stats --parts ~/Maildir
```

Its output identifies no messages unless `--examples` is given.

## Development

```sh
pixi run test        # pytest, with hypothesis property tests
pixi run lint        # ruff check + format check
pixi run fmt         # ruff format + fix
pixi run typecheck   # mypy --strict
```

Runtime dependencies are deliberately minimal: `cryptography` is the only
third-party one; zstd, TOML, SQLite and MIME handling come from the
standard library. Test messages in `tests/corpus/` are synthetic; please
don't add real mail. `CLAUDE.md` lists the invariants that changes must
keep (never write to the server, byte-exactness, append-only store, write
ordering).

## License

MIT; see [`LICENSE`](LICENSE).
