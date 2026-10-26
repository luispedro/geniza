# geniza — implementation plan

How to get from `DESIGN.md` to a working one-way archiver. The first
release is JMAP only, with Fastmail as the reference server: M0–M8 and M10.
IMAP (M9) comes after it. Each milestone ends
in something that can be run and tested on its own. Milestones 1–3 need no
network and write nothing, and produce the numbers that set the defaults the
design leaves open.

## Status

- **M0–M4 done.** The M3 gate (`stats --parts` on real mail) is passed;
  the defaults and classification changes it led to are in DESIGN.md.
- **M5 done.** Ingest, `debug import-local`, `show`, `export --mbox`
  and `verify` work, the "done when" run on real mail passed, and fault
  injection passes (see M5).
- **M6 done but for the optional attachment text** (`pdftotext`). The
  index (`reindex`, kept up to date from segments, journals and
  `incoming/`), threading, message text, which `show` prints, the body
  pass and `search` work, and the "done when" holds: an updated index
  searches like a rebuilt one (see M6). Attachment text is left for later.
- **M7 done.** Placement, evaluation and its application work and are
  tested against a fake source, `verify` checks `incoming/` and the
  journals against the store, and `show` prints messages only in
  `incoming/` (see M7).
- **M8 in progress.** Secrets are done (`sshcrypt.py`, `resolve_secret`,
  `geniza secret keys|encrypt|check`), and so are the JMAP client
  (`sync/jmap.py`) and `geniza sync` (`sync/base.py`, `sync/jmap_sync.py`:
  mailboxes and emails into the journal, downloads, `sync.json`,
  evaluation, the end of initialization), tested against an in-process
  fake server (`tests/jmapserver.py`), including fault injection of whole
  syncs, and settings (`setting` events for identities, Sieve scripts
  and the vacation response), retries on rate limiting, and parallel
  downloads over kept connections. Left:
  extension properties (`props`), and the run against a real Fastmail
  account (see M8).

## Tooling

- **Python 3.15 only** (`requires-python = ">=3.15"`). No compatibility shims
  for older versions.
- **pixi** manages the environment, with dependencies from **conda-forge**
  and `pixi.lock` committed, so every environment is fully locked. The pixi
  configuration lives in `pyproject.toml` under `[tool.pixi.*]`, so there is
  one project file.
- The package itself is installed into the pixi environment as an editable
  PyPI dependency (`geniza = { path = ".", editable = true }`), built with
  `hatchling`.
- Platforms: `linux-64` to start; others are added with
  `pixi workspace platform add`, which re-locks.

Environments and dependencies (all available on conda-forge for 3.15):

| Feature | Packages | Purpose |
|---|---|---|
| default | `python 3.15.*`, `cryptography` | runtime (`imapclient` joins with M9) |
| `test` | `pytest`, `hypothesis`, `zstd` (CLI), `openssh` | unit and property tests; `zstdcat` compatibility tests; a real `ssh-agent` for sshcrypt tests |
| `lint` | `ruff`, `mypy` | formatting, linting, type checking |

pixi tasks: `test`, `lint`, `fmt`, `typecheck`, and `geniza` itself
(`pixi run geniza …`).

Runtime dependencies stay minimal: `cryptography` is the only third-party
package of the first release, and `imapclient` the only other one planned
(for M9). zstd (`compression.zstd`), TOML (`tomllib`), SQLite and
MIME handling come from the standard library.

## Package layout

```
pyproject.toml          # project metadata + [tool.pixi.*]
pixi.lock
src/geniza/
  __main__.py           # python -m geniza
  cli.py                # argparse subcommands
  config.py             # tomllib config loading and validation
  sshcrypt.py           # ssh-agent client and sshcrypt: secret format
  mime/
    tree.py             # raw-bytes MIME part tree with byte offsets
    classify.py         # body / crypto / decoration / attachment / other
    recipes.py          # base64 / identity / QP round-trip checks
    names.py            # filename decoding and sanitising
    strip.py            # stub out bodies and splice them back
    text.py             # message text: charsets, flowed, HTML to text
  store/
    segment.py          # record framing, writer, reader, tail recovery
    attachments.py      # content-addressed attachment files
    incoming.py         # incoming/ files and evaluation
    journal.py          # append-only JSONL events and replay
    lock.py             # flock on store/.lock
    ingest.py           # strip → verify → commit
    parts.py            # record header part entries
    reader.py           # rebuild messages from records
  index/
    schema.py           # tables of cache/index.sqlite, schema version
    envelope.py         # header fields the index keeps
    reindex.py          # rebuild the index from the store
    bodies.py           # body pass: FTS and attachment names
    threads.py          # union-find threading
  sync/
    base.py             # geniza sync: stores, sync.json, evaluation
    jmap.py             # read-only JMAP client
    jmap_sync.py        # one JMAP account into the journal and store
    imap.py             # M9, after the first release
    matching.py         # stable ids and header-verified matching (M9)
  stats.py              # stats --parts
  show.py               # show ID [--raw]
  search.py             # search QUERY…
  export.py             # export --mbox
  verify.py             # verify
  debug.py              # debug import-local (development only)
tests/
  corpus/               # synthetic messages, one per tricky case
```

## Milestones

### M0 — Project skeleton

- `pyproject.toml` with `[tool.pixi.*]`, `pixi.lock`, the `src/` layout,
  an argparse CLI with no-op subcommands, and the pixi tasks.
- Output conventions, used by every later command: plain-text summaries on
  stdout by default, `--json` for machine-readable output, and diagnostics
  and warnings through stdlib `logging` on stderr (`-v`/`-q` adjust the
  level).
- A test that fails if the SQLite in the environment lacks FTS5, so a bad
  build is caught on the first `pixi run test`.
- `tests/corpus/`: synthetic messages, hand-written or generated, covering
  every case the design names:
  - Outlook (`image001.png` with Content-ID in `multipart/related`);
  - Apple Mail (real attachments marked `inline`);
  - PGP/MIME and S/MIME signed messages, with an attachment inside
    `multipart/signed`;
  - quoted-printable text attachments (`.py`, `.patch`);
  - base64 with 76-column, 72-column and irregular lines; LF and CRLF;
  - forwarded `message/rfc822` containing a PDF;
  - no Message-ID; duplicate Message-IDs; RFC 2231/2047 filenames,
    including hostile ones (`../`, control characters, very long).

  No real mail goes into the repository.

**Done when:** `pixi run test` and `pixi run lint` pass on a fresh clone.

### M1 — MIME part tree with byte offsets

The stdlib `email` package does not expose byte offsets and re-serialises
messages, so the core of everything else is a small parser over raw bytes
that produces a tree of parts, each with the byte ranges of its headers and
body. Stdlib `email` is used only to decode individual header values
(filenames, `Content-Type` parameters).

- It must tolerate real-world damage: missing final boundaries, boundaries
  with trailing whitespace, LF/CRLF mixtures, and headers without bodies.
  On anything it cannot parse, the message is treated as one opaque body
  (nothing is extracted), never rejected.
- Property test (hypothesis): the leaf ranges and the bytes between them
  concatenate back to the input, for every corpus message and for generated
  ones.

**Done when:** every corpus message parses, and the round-trip property
holds.

### M2 — Classification and recipes

- `classify.py` implements the five classes in DESIGN.md order, recursing
  into `message/rfc822`.
- `recipes.py` infers the base64 and identity recipes, re-encodes, and
  reports stub, copy, or inline for each part. The `text/*` CRLF→LF
  conversion goes here.
- `names.py` decodes and sanitises filenames, with the fallback names.

All three are pure functions with no I/O. Tests assert the expected class
and mode for each corpus message.

**Done when:** each corpus case is classified as DESIGN.md says, and every
part reported as stub reconstructs byte-exactly.

### M3 — `geniza stats --parts` (first real output)

- Reads a local **Maildir or mbox** with the stdlib `mailbox` module.
- Reports per class: count, total size, size histogram; stub vs. copy vs.
  inline; resulting file count and inline bytes at `decoration_min_size` of
  16K, 64K, 128K, 256K and 1M; how often identical decorations repeat;
  messages without Message-ID.
- Prints nothing that identifies messages unless `--examples` is given, so
  the output can be pasted into an issue or a design discussion.

**Done when:** it runs over a large real Maildir. Then run it,
choose the defaults, and record the reasoning in DESIGN.md.

### M4 — Storage primitives

- `segment.py`: skippable header frame + body frame, mboxrd escaping, the
  `From ` line, the 256 MiB/month rollover, reading by offset, scanning
  headers without decompressing bodies, and truncating a torn tail.
- `attachments.py`: content-addressed naming, atomic write, read-only mode,
  first-name-wins.
- `journal.py`: event types from DESIGN.md, `seq`, monthly files, `fsync`,
  replay into an in-memory state.
- `lock.py`: one writer per store.
- `config.py`: stores and accounts, XDG path defaults, key-by-key merging
  of store settings, name validation, `store.toml` creation and the store
  id check against the cache.

Tests:

- `zstdcat segment | python -c 'mailbox.mbox(…)'` yields the expected
  messages. This checks the claim that a segment is both a valid zstd
  stream and a valid mboxrd file with no geniza code.
- A segment truncated at every byte offset of its last record recovers to
  the previous record.
- Journal replay is idempotent: replaying twice gives the same state.

**Done when:** the tests above pass.

### M5 — Ingest, show, export, verify

- `ingest.py`: classify → write attachment files → build stripped message →
  reconstruct (from the serialised record header) and hash → append
  record → journal. Follows the write ordering in DESIGN.md exactly.
- `geniza show ID --raw`, `export --mbox`, `verify`.
- A development-only `geniza debug import-local PATH` (Maildir/mbox) to
  exercise the pipeline on real volume before any network code. It is not a
  migration path: stores meant for real use are always filled by sync.
- Fault injection: a test hook that raises after each step of the write
  ordering, followed by startup recovery, must leave a store that passes
  `verify`.

**Done when:** importing a large real Maildir into a scratch store,
then running `export --mbox`, reproduces every message byte for byte (hashes
compared), and `verify` passes.

**Result (2026-10-25):** passed. The import had no fallbacks or read
errors, a second import stored nothing new, `verify` found no problems,
and `export --mbox` completed.

Fault injection (`tests/test_faults.py`): a first-sync-like scenario
(store creation, batches into segments and the journal, the end of
initialization, a batch after it) runs in a forked child that exits at
each `fsync`, `rename` and `link` (before and after; before an `fsync`,
also with the unsynced end torn or zero-filled). After a writer opens the
store, `verify` passes; running the scenario again gives what a crash-free
run gives, with no orphans or temp files left. It found that temp files
of `store.toml` and `initialized` could be left behind once `store.toml`
existed; `init_store` now always removes them. M8 adds a sync scenario
that reaches `sync.json` (step 4); see M8.

### M6 — Index and search

- Schema from DESIGN.md, `reindex` (headers-only pass, then FTS pass),
  incremental updates from ingest and journal.
- Replaces the stand-ins that scan every segment header: `ingest.Catalog`
  (known message ids and blob files) and the id lookup in `show`. The
  index goes in `cache/index.sqlite`, which `config.open_cache` already
  removes on a store id mismatch.
- Message text (charset, QP/base64, HTML to text) for search and for
  `show`, which prints it.
- `geniza search` over FTS5, with a small query syntax for
  `from:`/`to:`/`subject:`/`mailbox:`/`date:` mapped onto columns.
- Union-find threading; tests for out-of-order replies and thread merges.
- Optional attachment text extraction (`pdftotext` if on `PATH`), cached by
  blob.

**Done when:** deleting `cache/` and running `reindex` reproduces the same
search results as the incrementally maintained index.

Progress: `index/schema.py` (all tables but `fts`) and `reindex`'s headers-only pass, which fills messages, `incoming`,
attachment files (without MIME types and names, which need the body pass),
mailboxes (with their hierarchy), membership (with the server's received
dates and thread ids), current flags and each account's last `seq`.

The index records how far it has read each segment and is brought up to
date before use, reading only the records appended since (or rebuilt if a
segment shrank or vanished); `debug import-local` takes its catalog from
it and `show` looks ids up in it. `Catalog.scan` stays as the slow
reference the tests compare against.

Journals and `incoming/` are read again per account when they change
(the index keeps the sizes of the journal files it replayed), rather than
keeping replay state: they are small next to the segments. A message
purged from `incoming/` leaves the index, and its thread is made again
from the messages left in it. A property test runs random sequences of
what sync and incoming/ evaluation do (files held with and without their
journal event, drops, undrops, purges, promotions, flags, removals) with
updates in between and compares the whole index with a rebuild's; a
search test checks the "done when" through the CLI (search, change the
store, search, delete `cache/`, `reindex`, search).

Threads are computed by the same pass, from `References` and
`In-Reply-To`, as a union-find kept in the index (`threads`): an update
joins the new records' ids to the threads already there. Tests check the
threads against the connected components of the links for random
messages in random order, and that threads from updates over several
batches (replies before their parents, a message joining two threads)
are those of a rebuild.

Message text (`mime/text.py`, DESIGN.md "Message text") decodes the body
parts (transfer encoding, charset with browser-style supersets and a
UTF-8/Windows-1252 guess when unlabelled, `format=flowed`, HTML to text
with `html.parser`) for `show` now and the FTS pass next. Property tests
round-trip plain text through each transfer encoding and check that
arbitrary bytes never raise; two corpus cases cover an HTML-only message
whose charset is in a `<meta>` tag and unlabelled flowed UTF-8.

The body pass (`index/bodies.py`) reads each message once, from the
stripped message (a test checks that it has the original's text and part
headers for every corpus message), into a contentless FTS5 table, and
fills `attachments.mime_type` and `attachment_names`. It commits in
batches and resumes after an interruption (tested, as is a damaged body
reported once); `reindex` always runs it, `update` only when asked
(`text=True`, for `search`). The update tests compare the index's terms,
by message id, with a rebuild's.

`search` (`search.py`, DESIGN.md "Search") turns a notmuch-like query
into SQL: words, phrases and prefixes, optionally limited to a column,
make one FTS5 `MATCH` (each quoted, so user input is never FTS5 syntax),
and `mailbox:`, `date:` and `thread:` are conditions on the other tables;
any term can be negated. Results are ordered by `Date` (else received
time), through an expression index. Tests cover the lexer (property test:
any input is FTS5-safe), date ranges, each field, negation, mailbox paths
with a parent loop, dropped messages and the text output.

The journal also records what servers keep outside the messages (mailbox
hierarchy, order and subscription, per-account received dates, server
thread ids, other server properties verbatim as `props`, and account
settings), so that the store is enough to rebuild an account. Format 1
was amended for this rather than bumped: no store had journal events yet.

### M7 — Incoming

- Placement at ingest, the evaluation pass, promotion, the two-run drop,
  grace, `undrop`, `purge`, `on_delete = "keep"`, and the mass-drop guard.
- Driven by journal state, not by any server, so it is tested against a
  **fake source**: a scripted sequence of mailbox snapshots (arrive, move,
  trash, expunge, restore, empty listing).
- `Ingester.add` writes no journal events; its caller writes them per
  batch, with a `flags` event for every `add`. `verify` learns to check
  `incoming/`.

**Done when:** every scenario in the evaluation section of DESIGN.md has a
test, including the IMAP move race (scripted in the fake source, since the
two-run rule applies to every source), an empty listing tripping the
guard, a message held in two accounts' `incoming/` (one drops it, the other
promotes it; a later promotion writes nothing), and a promotion interrupted
after the segment write.

Progress: `store/incoming.py` holds messages (`Ingester.hold`: temp file,
read-only, dated with the server's received date), decides placement,
evaluates an account's `incoming/` from its replayed journal and files
(`evaluate`, pure) and applies the result (`apply`: segment records, one
journal batch, unlinks). The two-run rule takes the messages the previous
evaluation found absent, which sync will keep in `sync.json`. The tests
drive a fake source (a JMAP-like account whose mailboxes a test script
changes) through a small sync that journals it, covering every scenario
above and the partial run, a purge interrupted before the unlink, a
message restored after its purge, excluded mailboxes and a damaged held
file; a property test runs random server changes and runs and checks that
nothing downloaded is lost, that drops follow two absent runs (never with
`keep`), that purges wait for the grace period, and that filed or old
messages are not left in `incoming/`. `verify` hashes the held files,
lists temp files and held files of messages also in a segment, and
replays each journal: messages in its mailboxes must be stored (in a
segment or the account's `incoming/`, unless purged) and promotions must
name a record of their message; the property test runs it after every
evaluation. A held copy is unlinked only after its segment record has
been rebuilt and hashed, and the fraction rule of the mass-drop guard
applies only above 10 drops. `show` reads a message only in `incoming/`
from the first account's file that has its hash.

### M8 — JMAP sync

The only sync of the first release: JMAP ids are exact and its change
tracking is simple. Fastmail is the reference server; nothing
Fastmail-specific is hard-coded (limits come from the session), but no other
server is tested.

- Session discovery, `Mailbox/get`/`changes`, `Email/changes`, `Email/get`
  in `maxObjectsInGet` batches, streamed blob downloads, the
  `cannotCalculateChanges` fallback.
- Server metadata into the journal (DESIGN.md, "State: the journal"):
  mailbox `parent`/`sort`/`subscribed`, `received` and `thread` on `add`,
  `props` for other non-derived properties, and `setting` events for
  identities, Sieve scripts and the vacation response. Check first which
  of these Fastmail offers, under which token scopes, and which Fastmail
  extension properties exist.
- **Secrets first**, since this is the first code that needs one
  (DESIGN.md, "Secrets"):
  - `sshcrypt.py`: a reimplementation of the standalone `sshcrypt.py` as a
    module, with the same wire format, plus bounds-checked parsing, the
    sign-twice determinism check, and an explicit key choice.
  - `token`/`password` (`sshcrypt:`) and `*_command` resolution in
    `config.py`, with plain-text values rejected.
  - `geniza secret keys`, `encrypt`, `check`.
  - Tests: a fake agent (an in-process Unix-socket server holding Ed25519
    and RSA keys) for the protocol and its failure modes (refusal, a wrong
    or invalid signature, a randomising signer, truncated replies); a real
    `ssh-agent` for one end-to-end round trip; and a fixture encrypted by
    the original `sshcrypt.py` with a throwaway test key (committed with
    the test) that must still decrypt, to pin compatibility.
- Authentication with a Bearer API token. A read-only token is recommended
  in the docs.
- Matching uses `emailId` only (`identity = "jmap"`); `type = "imap"`
  accounts are rejected by `config.py`.
- The first sync that completes for every account of a store calls
  `config.mark_initialized` (DESIGN.md, "On-disk layout"); until then
  `Ingester` lays records out by received month.
- Tests against an in-process fake JMAP server (stdlib `http.server`).
  Manual testing against a real Fastmail account with a read-only token.
- The fault-injection scenario (`tests/test_faults.py`) gains the
  `sync.json` write (step 4 of the write ordering), driven by the fake
  server.

**Done when:** the first sync of a real Fastmail account completes, a second
sync with no changes downloads nothing, and moves and flag changes show up
as journal events only.

Progress: secrets. `sshcrypt.py` speaks the agent protocol with
bounds-checked parsing (any reply, however malformed, is an
`SshcryptError`), verifies every signature, signs twice when encrypting,
and keeps the wire format of the original tool; `config.resolve_secret`
decrypts an `sshcrypt:` value or runs a `*_command`; `geniza secret keys`,
`encrypt` (`--key`, or the only usable key) and `check` work. The tests
use a fake agent (`tests/agents.py`: refusals, wrong, invalid, randomising
and truncated signatures, arbitrary replies), the committed fixtures
(decrypted by the fake agent and by a private real `ssh-agent`) and an
end-to-end round trip through that real agent.

Progress: the JMAP client. `sync/jmap.py` fetches the session (primary
mail account, limits from it, insecure `apiUrl`/`downloadUrl` refused),
allow-lists read-only methods, refuses redirects, batches `*/get` by
`maxObjectsInGet` and `maxCallsInRequest`, merges `*/changes` pages
(raising `CannotCalculateChanges`), lists every email through anchored
`Email/query` pages, and downloads blobs. `tests/jmapserver.py` is the
fake server (stdlib `http.server` on loopback, with a change log so
`*/changes` answers from any state, and knobs for limits, page sizes,
forgotten changes and meddling during a listing); the changes semantics
and byte-exact downloads are hypothesis properties.

Progress: `geniza sync`. `sync/jmap_sync.py` journals the mailboxes
(changes, or all of them; counters left out of `props`; destroyed ones
`gone` after the removes of their messages), then the emails changed
since the cursor, or all of them (first run, `cannotCalculateChanges`,
lost or damaged `sync.json`, a changed `exclude`), in batches: each
email's `mailboxIds` and `keywords` are diffed against the replayed
journal, and its body is downloaded only if its `eid` maps to no message
the store holds for the account (so a purged message comes back). A
mailbox an email names that the journal lacks is fetched first.
Excluded mailboxes are journaled but get no adds or downloads. A download
that fails or is not the size the server gave makes the run partial:
`sync/base.py` then neither evaluates `incoming/` nor rewrites
`sync.json`. After a complete run it evaluates (two-run drops from
`sync.json`'s `absent`, the mass-drop guard, `--allow-mass-drop`) and
writes `sync.json`; once every account of the store has one, the store's
initialization ends. Tests cover each case against the fake server
(with knobs for truncated and failing downloads), and a property test
runs random server changes, forgotten changes and lost `sync.json`
files, checking after every sync that the journal equals the server,
that no body is downloaded twice, and that `verify` passes.

Progress: fault injection of sync. `tests/test_faults.py` gains a second
scenario: four stages of changes on the fake server (the first sync and
the end of initialization, held mail, a held message gone from the
server, a promotion, a drop and a purge, a destroyed mailbox), each
followed by `geniza sync`, so a crash can hit every step from the store's
creation to `sync.json`. The children start their own server; after a
crash, the server is made again at the stage reached and the remaining
syncs run. The store must pass `verify` and end like a crash-free run:
the same messages, replayed journal, `sync.json` and `incoming/`. It
found that a crash while the cache was first created (its
`CACHEDIR.TAG` temp file left, before the rename) made every later run
refuse the cache directory as not a geniza cache; such temp files no
longer count. It also found that a torn journal tail could keep an `add`
and lose the empty `flags` event after it, leaving a replayed state that
differed from a crash-free run's only in that empty set; replay now
treats an empty set as none, as it already did for `props`.

Progress: settings. After the emails, a run lists the identities, Sieve
scripts and vacation response, each when the session offers its
capability, and journals each object that is new or changed (a script
with its text, downloaded only when its object changed) and each one
gone. Their failures (a `get` refused, a script that cannot be
downloaded or is not UTF-8) are reported without making the run
incomplete. The fake server serves the three types, checks each
method's capability in `using` and on the account, and the tests cover
changes, a missing capability (journaled settings stay), the failures,
random settings changes in the property test, and settings in the fault
scenario.

Progress: retries, for the real run. The client sends a request again
after HTTP 429, 502, 503 or 504, up to five times, waiting as
`Retry-After` asks (seconds or a date) or with a doubling backoff, and
gives up at once if asked to wait more than five minutes. The fake
server's `busy` knob turns requests away; tests cover the API, the
session and downloads, giving up, and statuses that are not retried.

Progress: download speed. The first real run was slow, and measuring it
showed the time went to round trips, not to ingest or bandwidth: `urllib`
opened a new connection (TCP and TLS) for each message, and messages
were downloaded one after another. The client now uses `http.client`
with a pool of kept connections (a connection the server closed while
idle is replaced), and `Client.downloads` fetches a batch's bodies over
`[accounts.<name>.sync] connections` connections (default 4), yielding
them in order and running at most 64 MiB ahead. The sync plans every
email of a batch before downloading, so storing and journaling stay
serial and in order. The fake server speaks HTTP/1.1 and counts
connections and concurrent downloads; tests cover reuse, a server that
hangs up, failures within a parallel batch, the byte bound, closing
early, and order (a hypothesis property).

### M9 — IMAP sync (after the first release)

- `imapclient`; capability detection; CONDSTORE/QRESYNC, with plain
  fallbacks; UIDVALIDITY resets.
- Message matching in DESIGN.md order (OBJECTID, X-GM-MSGID, verified
  headers), with the warning when there is no stable id, and the
  `identity` field in `sync.json`.
- Gmail via `[Gmail]/All Mail` and `X-GM-LABELS`.
- Integration tests against a real Dovecot in a container (podman/docker),
  run only when one is available; they are skipped otherwise. Dovecot can be
  configured with and without OBJECTID, which covers both matching paths.

**Done when:** an IMAP account and a JMAP account sync into the same store,
and a message moved between folders is matched without re-downloading.

### M10 — Hardening and the borg check

- A script that measures goal 1 directly: create a store, `borg create`,
  sync a day's mail, `borg create` again, and report the deduplicated size
  added. It should be close to the size of the new mail.
- `compact` stays out of scope; `verify` reports orphaned attachments.
- If a first sync is still too slow: measure first. Downloads were the
  bottleneck (see M8, "download speed"), far ahead of ingest; only if
  ingest becomes it, split `Ingester.add` into a pure planning step
  (parse, classify, strip, check) run in worker processes and a commit
  step in the main one.
- First release (JMAP only; does not wait for M9).

## Order and gates

M0 → M1 → M2 → M3 is the critical path. **After M3, stop and run it on real
mail**: the defaults, and possibly the classification rules, change with what
it finds. M4–M7 then need no network. M8 and M10 complete the first release;
M9 (IMAP) follows it.
