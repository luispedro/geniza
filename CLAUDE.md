# geniza

One-way archiver: pulls mail from IMAP/JMAP servers into a local store that
is borg-friendly, searchable, made of few large files, and keeps attachments
as real files.

- `DESIGN.md` is the specification: on-disk formats, sync, matching, config.
- `PLAN.md` is the milestone plan (M0–M10) and its "Status" section says
  where things stand; keep it current. Work milestone by milestone. The
  first release is JMAP only (Fastmail as reference): M9 (IMAP) comes
  after it.
- `geniza debug import-local` is a testing tool and stays under `debug`;
  don't promote it to a top-level command (stores are filled by sync).

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
  (`cryptography`; `imapclient` is added with IMAP in M9); ask before
  adding another one.

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
  once their month is over and the store is initialized (until then they
  are laid out by received month; DESIGN.md, "On-disk layout").
  `sync.json` is the only file rewritten (atomically).
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
- **Never touch the developer's ssh-agent.** Tests (and any scripts) that
  need an agent start their own, on a short socket path (Unix socket paths
  are limited to ~108 bytes, so not under a deep temp directory), check that
  it is running and that `SSH_AUTH_SOCK` points to it, and only then load
  keys. Never run `ssh-add -D` or `-d` against an inherited `SSH_AUTH_SOCK`.
- `tests/fixtures/sshcrypt/` holds throwaway keys and ciphertexts made with
  the original `sshcrypt.py`; never regenerate them with real keys.
- The corpus is written by `tests/corpus/generate.py`; after editing it,
  run `pixi run python tests/corpus/generate.py` (a test checks the files
  are up to date). Each corpus file needs an entry in `EXPECTED` in
  `tests/test_classify.py`, and new cases change the counts asserted in
  `tests/test_stats.py`. Addresses in From/To/Cc use example.org,
  example.net or example.com (checked by a test).
- Tests that write raw bytes to stdout need `capsysbinary` (bytes under
  `capsys` break capture for the rest of the session). The `geniza` logger
  does not propagate, so check stderr, not `caplog`.

## Development notes

- The store writers (`SegmentWriter`, `AttachmentWriter`, `JournalWriter`)
  do not lock; their callers hold a `StoreLock`, and create `store.toml`
  under it.
- Iterate `mailbox.mbox` with `iterkeys()`: plain iteration yields
  messages, so ruff's SIM118 "fix" breaks the code.
- Invisible characters (bidi controls, combining marks) written as
  `\u…` escapes have ended up as literal characters in source files.
  Prefer `chr()` with code points, and check with
  `grep -rnP '[\x{200e}\x{200f}\x{202a}-\x{202e}\x{2066}-\x{2069}\x{0301}]' src tests`.
- If `import geniza` fails in the pixi environment, run
  `pixi reinstall geniza` (the editable install).

## Security and privacy

- Secrets come only from `sshcrypt:` values or `*_command` config entries
  (DESIGN.md, "Secrets"); plain-text values are rejected. Never log them, put
  them in exceptions, or write them to the store or cache.
- Logs and `--json` output may contain addresses and subjects at `-v`;
  default output should be summaries.
- When running anything on the developer's real mail, print only counts
  and structure (sizes, line lengths, part types), never headers, names
  or content, and keep the outputs out of git.
- Nothing from the developer's mail goes into tracked files or commit
  messages, not even in passing: no message or attachment counts, mailbox
  sizes, mail paths or folder names, dates or senders. Record conclusions
  and the reasoning behind a default, not the figures.
