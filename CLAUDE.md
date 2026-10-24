# geniza

One-way archiver: pulls mail from IMAP/JMAP servers into a local store that
is borg-friendly, searchable, made of few large files, and keeps attachments
as real files.

- `DESIGN.md` is the specification: on-disk formats, sync, matching, config.
- `PLAN.md` is the milestone plan (M0–M10). Work milestone by milestone and
  stop at the gate after M3 (`geniza stats --parts` on real mail).

When behaviour or a format changes, update `DESIGN.md` in the same commit.
If the design turns out to be wrong or ambiguous, raise it rather than
silently diverging.

## Environment

- Python 3.15 only (`requires-python = ">=3.15"`); use current language and
  stdlib features freely, no compatibility shims.
- pixi manages everything; dependencies come from conda-forge and
  `pixi.lock` is committed. Configuration lives in `pyproject.toml` under
  `[tool.pixi.*]`.
- Run things through pixi:
  - `pixi run test` — pytest (with hypothesis)
  - `pixi run lint` / `pixi run fmt` — ruff
  - `pixi run typecheck` — mypy
  - `pixi run geniza …` — the CLI
- Add dependencies with `pixi add` (or `pixi add --feature test …`) so the
  lock file is updated. Runtime dependencies are deliberately minimal
  (`imapclient` only); ask before adding another one.

## Invariants (do not break)

- **Never modify the server.** IMAP: `EXAMINE`, never `SELECT`; fetch with
  `BODY.PEEK[…]`; never `STORE`, `COPY`, `MOVE`, `EXPUNGE`, `APPEND`,
  `CREATE`, `DELETE`. JMAP: no `*/set` or `*/copy` methods.
- **Byte-exactness.** Messages are hashed and stored as raw bytes. All MIME
  byte-range work happens on raw bytes; never re-serialise an
  `email.message` object into the store. Every stripped message is
  reconstructed and its hash checked before it is committed.
- **Append-only store.** Outside `incoming/`, nothing in a store is
  rewritten or deleted by sync. Segments and month directories are sealed
  once their month is over. `sync.json` is the only file rewritten (atomically).
- **Write ordering** (attachments → segment/incoming → journal → `sync.json`,
  each with `fsync`) is what crash recovery relies on; keep it.
- **The cache is disposable.** Anything in `cache/` must be rebuildable by
  `geniza reindex`; nothing may be stored only there.
- **Format changes** to segments, journal events, `store.toml` or attachment
  naming need a format version bump and a `DESIGN.md` update.
- **No unverified matches.** Prefer an extra download over assuming two
  messages are the same (see "Message matching" in `DESIGN.md`); a wrong
  match is silent data loss.

## Tests

- No real mail in the repository. Test messages in `tests/corpus/` are
  synthetic; add one for each new tricky case (client quirk, encoding,
  malformed structure) rather than relying on mail found locally.
- Round trips (MIME tree, recipes, segments, journal replay) get hypothesis
  property tests, not just examples.
- Network code is tested against in-process fakes (JMAP) or a Dovecot
  container when available (IMAP); integration tests that need a container
  skip themselves otherwise.

## Security and privacy

- Secrets come only from `*_command` config entries; never log them, put
  them in exceptions, or write them to the store or cache.
- Logs and `--json` output may contain addresses and subjects at `-v`;
  default output should be summaries.
