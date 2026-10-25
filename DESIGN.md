# geniza — design

A one-way archiver that pulls mail from IMAP and/or JMAP servers into a local
store designed to be:

1. friendly to deduplicating backup tools (borg, restic),
2. searchable,
3. made of few, large files rather than many small ones,
4. keeping attachments as real, browsable files.

## Scope

In scope (v1):

- One-way sync: server → local. Nothing is ever written to the server.
- JMAP, with Fastmail as the reference server: it is the only server the
  first release is tested against. Other JMAP servers may work.
- Several stores per configuration, each holding several accounts.
- Full-text search via a rebuildable local index.

Out of scope for now:

- IMAP (with CONDSTORE/QRESYNC when available), including Gmail. It is
  designed below and comes after the first release. The on-disk formats
  already cover it (`rid`/`eid`, `reset` events, the `imap` and `identity`
  fields of `sync.json`), so adding it needs no format version bump.
- Pushing local changes (flags, moves, deletions) back to the server.
- Serving mail to local clients (Maildir view, local JMAP/IMAP server).
  The format must not preclude this, but nothing is designed for it yet.
- Migrating existing local copies (offlineimap Maildirs etc.). A store is
  always filled by syncing from the server.

Target: Python ≥ 3.15.

## Core principle

Split everything into two trees:

- **`store/`** — the archive. Backed up. Almost everything in it is
  append-only, and files are *sealed* (never modified again) once their
  period is over and the store is initialized. Borg therefore only ever
  sees new bytes at the end of a few files, plus new files.
- **`cache/`** — everything derivable from `store/`: the SQLite/FTS index,
  remote-id maps, thread ids, extracted attachment text. Contains a
  `CACHEDIR.TAG` so `borg create --exclude-caches` skips it.
  `geniza reindex` rebuilds it from scratch.

The single exception to "append-only" is `incoming/` (see below), which
holds a bounded, small, deliberately volatile set of recent messages.

## On-disk layout

```
store/                                    # default: $XDG_DATA_HOME/geniza/<store>/
  store.toml                              # store id and format version; written once
  initialized                             # time initialization ended; written once
  .lock                                   # flock() target; single writer
  incoming/
    <account>/<sha256>.eml                # recent INBOX mail, raw, unmodified
  segments/
    2026/2026-10.zst                      # sealed after its month ends or at 256 MiB
    2026/2026-10.1.zst                    # rollover within a month
  attachments/
    2026/10/3fa9c1e07b22d4a1_quarterly-report.pdf
  state/
    <account>/journal-2026-10.jsonl       # membership/flag events, append-only
    <account>/sync.json                   # small cursor file, rewritten atomically

cache/                                    # default: $XDG_CACHE_HOME/geniza/<store>/
  CACHEDIR.TAG
  store-id                                # id of the store the cache was built from
  index.sqlite
```

Dates in `segments/` and `attachments/` paths are the **month the data was
written**, not the message date, once the store is **initialized**. This is
what makes them sealable: a month directory or segment never changes once
that month is over.

While the store is being initialized, they are instead the month the
message was **received** (never later than the month it is written in), so
the history loaded into a new store is laid out by date: `zstdgrep` over
`segments/2019/` searches 2019's mail. Initialization ends with the first
sync that completes for every account of the store, which then writes
`store/initialized` (once, under the lock, through a temp file; its
content is the UTC time). `debug import-local` never ends it. From then on
the month is the month of writing, so mail that arrives late (a newly
added account's history, an old message moved out of an excluded mailbox)
goes into the current month rather than reopening an old one. Message
dates live in the index.

`store.toml` holds a random store id (UUID), the on-disk format version
and the creation time:

```toml
# geniza store; written once, do not edit.
format = 1
id = "0b5c4f0e-6a43-4c55-9a0e-4c1f3d1e2a77"
created = 2026-10-25T09:30:01Z
```

It is written once, through a temp file, by the first writer that takes
the store lock, and never replaced. A format newer than geniza knows, or
an unknown key, is an error. A store directory that has files other than
`.lock` but no `store.toml` is refused rather than given a new id: it is
either the wrong directory or a store that lost its `store.toml`.

The cache records the id of the store it was built from in
`cache/store-id`. On a mismatch or a missing id (a cache directory reused
for a different or restored store), the files built from a store
(`index.sqlite` and its `-wal`/`-shm`/`-journal` files) are removed and
the index is rebuilt; nothing else in the directory is touched. The cache
directory is created with mode 0700 and tagged with `CACHEDIR.TAG`. A
non-empty directory without a `CACHEDIR.TAG` is refused rather than
tagged, so a mistyped cache path cannot make backups skip it.

## Configuration

One configuration file, `$XDG_CONFIG_HOME/geniza/config.toml` (overridable
with `--config`), describes any number of **stores**, and each **account**
belongs to exactly one store. The configuration is not part of any store:
nothing in it is needed to read an archive, only to sync into one.

```toml
default_store = "personal"   # optional; otherwise --store is needed with several stores

# Defaults for every store; each key can be overridden per store.
[attachments]
min_size = 1
decoration_min_size = "64KiB"

[incoming]
mailboxes = ["INBOX"]
max_age = "7d"

[stores.personal]
# path  = "$XDG_DATA_HOME/geniza/personal"     (default)
# cache = "$XDG_CACHE_HOME/geniza/personal"    (default)

[stores.work]
path = "/srv/archive/work"

[stores.work.incoming]
max_age = "14d"

[accounts.fastmail]
store = "personal"
type = "jmap"
url = "https://api.fastmail.com/jmap/session"
token = "sshcrypt:U1NIQ1JZUFQyAG3k…"           # see Secrets

[accounts.uni]                # IMAP: after the first release
store = "work"
type = "imap"
host = "imap.example.edu"
user = "me"
password_command = "pass show mail/uni"

[accounts.uni.sync]
exclude = ["junk"]
```

- Store names and account names are unique within the configuration and are
  used as directory names (`<store>`, `state/<account>/`,
  `incoming/<account>/`), so they are restricted to `[a-z0-9_-]`.
- Paths default to `$XDG_DATA_HOME/geniza/<store>` and
  `$XDG_CACHE_HOME/geniza/<store>`, with the XDG fallbacks
  (`~/.local/share`, `~/.cache`) when the variables are unset.
- Store settings (`[attachments]`, `[incoming]`) are merged key by key:
  built-in default ← top-level table ← `[stores.<name>.…]`.
- Paths are absolute or start with `~/`. A store's path and cache must
  not contain each other, nor overlap with another store's.
- Sizes are integers (bytes) or strings with a binary unit: `"64KiB"`,
  `"2MiB"`, `"1GiB"`. Durations are strings of number–unit pairs with
  units `s`, `m`, `h`, `d`, `w`: `"7d"`, `"12h"`, `"1w2d"`.
- Unknown keys are errors, so a misspelt key is not silently ignored.
- A JMAP `url` must be `https`; plain `http` is accepted only for loopback
  hosts (local test servers), so the token never crosses a network in
  clear.
- Secrets never appear in the file in plain text (see Secrets).
- Until IMAP is implemented, an account with `type = "imap"` is rejected
  with an error saying so, rather than ignored.
- Renaming an account would orphan its `state/<account>/` and
  `incoming/<account>/` directories, so a rename is an explicit
  `geniza account rename OLD NEW`, which moves both directories and updates
  the configuration.

## Secrets

Each secret field (`password` for IMAP, `token` for JMAP) is given in one
of two ways, and exactly one must be present:

- **`<field> = "sshcrypt:<base64>"`** — encrypted in the config file, and
  decryptable only while the right SSH key is loaded in `ssh-agent`.
- **`<field>_command = "…"`** — run through the shell; its stdout, minus a
  trailing newline, is the secret. For users of `pass`, keyrings, etc.

A plain-text value (anything without the `sshcrypt:` prefix) is rejected
with an error that points to `geniza secret encrypt`.

### sshcrypt

The key is derived from an SSH signature, so it exists only while the SSH key
is in the agent. geniza talks to the agent socket (`SSH_AUTH_SOCK`)
directly, so there is no code path that reads a private key off disk.

- **Derivation:** ask the agent to sign `MAGIC ‖ salt`, verify the signature
  against the public key (so a rogue agent cannot return bytes it chose),
  then `key = HKDF-SHA256(sig, salt, info)`. The secret is encrypted with
  AES-256-GCM.
- **Deterministic signatures only:** Ed25519 and RSA (`rsa-sha2-512`, PKCS#1
  v1.5). ECDSA randomises every signature and `sk-*` (FIDO) keys embed a
  counter, so the key could never be derived again; they are refused.
- **Binary format** (base64-encoded after the `sshcrypt:` prefix):

  ```
  MAGIC "SSHCRYPT2\0" (10) ‖ sha256(public key blob) (32) ‖ salt (16) ‖ nonce (12) ‖ ciphertext+tag
  ```

  This is the format of the existing standalone `sshcrypt.py` tool, so the
  two remain interchangeable: `sshcrypt.py dec` can decrypt a geniza secret,
  and vice versa.

geniza reimplements the scheme as a library module rather than vendoring the
script. Compared to `sshcrypt.py`, the module:

- checks bounds when parsing agent replies and ciphertexts, raising a
  single error type instead of `struct.error`/`IndexError`, or silently
  short reads;
- on encryption, signs twice and refuses the key if the two signatures
  differ. This catches agents or tokens that randomise Ed25519 or RSA
  signatures before anything is encrypted to them, rather than when the
  secret can no longer be decrypted;
- takes the key to encrypt to as an explicit choice (`--key FINGERPRINT`)
  instead of the agent's first usable key;
- never prints; output belongs to the CLI.

Commands:

```
geniza secret keys                         # keys in the agent, and whether they are usable
geniza secret encrypt [--key FINGERPRINT]  # reads the secret with getpass, prints "sshcrypt:…"
geniza secret check                        # decrypts every secret in the config, prints OK/error per account, never the secret
```

`encrypt` reads the secret from the terminal (or stdin when it is not a
terminal), never from arguments, which would leave it in shell history and
`ps`.

### Properties and limits

- Secrets are decrypted only when needed (once per account per run), held in
  memory, and never logged, put in exceptions, or written to the store or
  cache.
- **Anyone who can use the agent can decrypt.** With agent forwarding, that
  includes root on every host the agent is forwarded to, if they also get the
  config file.
- **Unattended syncs** (systemd timers, cron) need `SSH_AUTH_SOCK` in their
  environment, and a key added with `ssh-add -c` (confirm every use) will
  prompt on each run. Either is the user's choice; `geniza secret check`
  shows whether a given environment can decrypt.
- **Losing the SSH key loses the secrets.** That is acceptable, since mail
  passwords and tokens can be reissued. Re-encrypting to a new key is
  `secret encrypt` again.
- The ciphertext is not bound to its account. Copying one account's value to
  another account's field works, and decrypts. Binding it would not add
  protection: anyone who can edit the config can already set a
  `*_command`, which runs arbitrary code.

## Message identity

A message's id is `sha256:<hex>` of its raw RFC 5322 bytes exactly as
downloaded. Consequences:

- A message in several mailboxes (IMAP copies, Gmail labels, JMAP
  `mailboxIds`) or several accounts is stored once.
- Moves are pure state changes; no body is rewritten.
- `geniza verify` can check every message end to end.

## Message storage: segments

A segment is a concatenation of *records*. Each record is two zstd frames:

```
record       := header_frame body_frame
header_frame := u32le 0x184D2A5E      # zstd skippable-frame magic
                u32le N
                N bytes: UTF-8 JSON header
body_frame   := one standard zstd frame, compressed independently
```

Because skippable frames are part of the zstd format, a segment is a valid
zstd stream: `zstdcat 2026-10.zst` ignores the headers and emits the bodies.
The body frame's content is chosen so that this output is a valid **mboxrd**
file:

```
From geniza <asctime of received date>\n
<stripped message, mboxrd-escaped: /^>*From / gets one more '>'>
\n
```

So `zstdcat segments/*/*.zst > all.mbox` and `zstdgrep` work with no
geniza code at all. Line endings inside the message are left as
downloaded (CRLF or LF); only the `From ` line and separator use `\n`.

Each body frame is compressed on its own (no shared dictionary), so appending
never changes earlier bytes, and any record can be read by offset alone.
Body frames carry zstd's content checksum, so damage is caught on read.

The date in the `From ` line is the received date in UTC, in `asctime`
form (`From geniza Fri Oct 23 14:02:11 2026`). mbox has no length field,
so a message that does not end with a newline gains one when read as mbox;
geniza itself reads exactly `stripped_size` bytes and loses nothing.

### Record header

```json
{
  "v": 1,
  "id": "sha256:…",              // hash of the ORIGINAL message
  "size": 48213,                 // original size in bytes
  "stripped_size": 1902,         // size of stripped message before escaping
  "frame_size": 911,             // compressed size of body_frame
  "received": "2026-10-23T14:02:11Z",
  "stored": "2026-10-24T09:00:03Z",
  "parts": [                     // extracted parts, in byte order
    {
      "path": "2",               // MIME part path, informational
      "kind": "attachment",      // "attachment" | "decoration"
      "mode": "stub",            // "stub" | "copy" (see Attachments)
      "offset": 1544,            // stub: byte range of the stub;
      "length": 112,             // copy: byte range of the encoded body
      "blob": "sha256:…",        // hash of DECODED content
      "file": "attachments/2026/10/3fa9c1e07b22d4a1_quarterly-report.pdf",
      "enc": "base64",           // "base64" | "identity"
      "line": 76,                // base64 only: line length
      // "layout": [[72, 217], [60, 1], [12, 1], [72, 33], [28, 1]],
      //                         // base64 only, if lines are irregular
      // "pad_bits": 6,          // base64 only, if not zero
      // "crlf_to_lf": true,     // identity only, if the file has LF
      //                         // where the encoded form has CRLF
      "eol": "crlf",             // line endings of the encoded form
      //                         // (of its last line, if mixed)
      "final_eol": true
    }
  ]
}
```

Offsets are in the stripped message. Entries are in byte order and do not
overlap. The recipe fields (`enc` to `final_eol`) appear only on stubs: a
copy's encoded body is still in the message and needs no re-encoding, so
its entry ends at `file`. Decorations are never copies. Optional fields are
written only when they differ from their default (no `layout`, `pad_bits`
0, `crlf_to_lf` false), and a reader rejects an entry that is not exactly
what a writer would write (unknown keys, an optional field set to its
default, a `file` outside `attachments/` or not named for its blob).

`frame_size` lets a scanner skip bodies without decompressing them, so the
index can be rebuilt from headers alone (bodies are needed only for FTS).

### Segment size

Segments are sealed at the end of their month or when they reach 256 MiB,
whichever comes first, once the store is initialized. The month is the UTC
month of the `stored` time, or of the `received` time while the store is
being initialized (see [the layout](#on-disk-layout)). A
record that would take the open segment past 256 MiB starts the next
rollover segment instead (`2026-10.1.zst`, `2026-10.2.zst`, …), unless the
segment is empty, so a single larger message still fits. The cap is not driven by borg: when the open segment
grows, borg re-reads it (sub-second for 256 MiB on local disk) but only
stores the new chunks at its end. Removing a record during a future
`compact` shifts the bytes after it, and borg's content-defined chunking
resynchronises past the gap, so even rewritten segments mostly deduplicate.
The cap mainly limits the damage of a corrupted file.

In normal operation the cap is rarely reached (at ~5 KB per compressed
stripped message, 50 messages/day is ~7.5 MB/month). The **initial import**
writes each message into its received month, so the cap matters there
only for months with a lot of mail. The initial import downloads messages
**sorted by received date across all mailboxes**, so each month's segments
are written in order and mostly one after the other. This is free: dates
(INTERNALDATE, `receivedAt`) are fetched before bodies anyway.

## Attachments

### Classification

Every leaf MIME part falls into exactly one class, checked in this order:

1. **Body** — the text parts that form the message text: `text/plain` and
   `text/html` parts without a filename or `Content-Disposition:
   attachment`, reached from the top of the message through
   `multipart/mixed` and `multipart/alternative` (all members),
   `multipart/signed` (the signed content), `multipart/report` (its
   human-readable first part) and `multipart/related` (its root part, so
   the HTML of `alternative { plain, related { html, images } }` is body).
   Unknown multipart subtypes count as `multipart/mixed` (RFC 2046).
   Never extracted.
2. **Crypto** — `application/pgp-signature`, `application/pkcs7-signature`,
   `application/x-pkcs7-signature`, `application/pgp-encrypted`,
   `application/pgp-keys`, `application/pkcs7-mime`,
   `application/x-pkcs7-mime`, and every part of a `multipart/encrypted`
   (the PGP/MIME ciphertext part is usually `encrypted.asc`, but it is not
   an attachment). Never extracted.
3. **Decoration** — an `image/*` part that either has a `Content-ID`
   inside `multipart/related` (images referenced from HTML via `cid:`) or
   is inline without a filename. Logos, signature images, pasted
   screenshots. This rule comes before the next one because Outlook gives
   decorations filenames (`image001.png`). Non-image parts with a
   `Content-ID` (a PDF shown inline, `application/octet-stream`, Outlook's
   `.wmz` drawings) go on to the next rules: some of them are
   attachments, and they are rare enough that extracting them all costs
   little.
4. **Attachment** — any other part with a filename (`Content-Disposition`
   `filename` or `Content-Type` `name`) or with `Content-Disposition:
   attachment`. The filename, not the disposition, is the main signal:
   Apple Mail marks real attachments `inline`.
5. **Other** — everything else (`text/calendar` without a filename, delivery
   status reports, …). Never extracted.

Attached messages (`message/rfc822`) are not extracted as `.eml` files.
Classification recurses into them, with rule 1 applied relative to the
attached message, so a forwarded PDF is extracted (and deduplicated against
the original).

Policy:

```toml
[attachments]
min_size = 1                     # attachments: extract even a 300-byte .patch
decoration_min_size = "64KiB"    # decorations: pasted images, not small logos
```

Attachments are what the sender meant to send, so they are extracted
regardless of size: small `.py`, `.patch`, `.md` files are valuable on disk
as-is. Decorations are extracted only when large.

The default was chosen by running `geniza stats --parts` on a large
mailbox and comparing the file count and the bytes left inline at
several thresholds (16 KiB to 1 MiB). Lowering the threshold saves much
more inline than it adds in files, because a pasted image is re-sent with
every reply in a thread and is stored once as a file. The saving per
extra file falls as the threshold drops; 64 KiB is the knee: below it the
extra files are mostly signature logos, each saving little, and
decorations would become a large share of `attachments/`. zstd recovers
part of the base64 overhead of inline images, so the real differences are
somewhat smaller.

### Extraction modes

An extracted part is decoded and hashed, and an encoding recipe is
inferred. Then:

- **stub** (preferred): re-encode the decoded bytes with the recipe. If the
  result is byte-identical to the original part body, replace the body
  (headers untouched) with a stub such as
  `[geniza: stored as attachments/2026/10/3fa9…_quarterly-report.pdf]`,
  followed by the part's EOL if the original body ended with one. The file is now needed to reconstruct the message.
- **copy** (fallback, attachments only): if re-encoding does not reproduce
  the original exactly, the file is still written, but the message keeps
  the original encoded part. This costs duplicate bytes but guarantees that
  every true attachment exists as a real file, while the message stays
  self-contained. Decorations that cannot be stubbed simply stay inline.

A part's body ends before the line ending that precedes the next boundary
delimiter: per RFC 2046 that line ending belongs to the delimiter. So
`final_eol` is true only when the encoded body has a line ending of its own
before it (a blank line before the boundary, or the end of a single-part
message).

Recipes:

- `base64`: line length, EOL style, final EOL. The canonical base64 text
  is cut into lines of `line` characters, the last one possibly shorter.
  When the lines are not like that, the record also has a `layout`: every
  line's length as `[length, count]` runs, blank lines being runs of
  length 0; `line` then holds the most common length, for information
  only. The layout may have at most 8 runs plus one per KiB of base64
  text (about 1% of the body); anything more irregular (or any whitespace
  or padding inside the text) falls back to copy. Layouts that clients
  produce: one line split in two (Outlook for Mac), content encoded in
  chunks that each end in a short line, a blank line between chunks
  (Roundcube; Coremail with 8 KiB chunks of 2047-character lines), and
  blank lines before or after the text (several webmail clients). A
  blank line after every line is too irregular and stays copy.
  `pad_bits` holds the bits that padding discards from the last character
  before the `=`: canonical base64 has them zero, but some encoders do not
  (Evolution 2.x, Entourage), and the decoded content cannot tell.
  An empty base64 body has nothing to extract and stays inline.
- `identity` (`7bit`, `8bit`, `binary`): the body is the content. For
  `text/*` parts with consistent CRLF line endings, the file is written
  with LF (`crlf_to_lf: true` records the conversion), so a `.py` or
  `.patch` file is usable directly. `eol` alone cannot record it, since
  unconverted bodies (non-text parts, mixed line endings) can have CRLF
  too; reconstruction never guesses from the file's content. A `text/*`
  part with mixed line endings is
  stubbed without conversion (`eol` is then that of its last line): the
  file is the body as is, which is what copy mode would write too, so
  copy would only keep a second copy of the same bytes in the message.
  Writing such a file with LF would need a record of which lines had
  CRLF, a format change not worth making for parts this rare.
- `quoted-printable`: soft line breaks cannot be reproduced reliably, so QP
  parts are always copy mode. This covers many small text attachments; for
  `text/*` parts whose decoded content uses CRLF consistently, the file is
  written with LF, as for `identity`.
- Any other `Content-Transfer-Encoding` (`x-uuencode`, …) is not decoded,
  and the part stays inline whatever its class.

After stripping, the full message is reconstructed from the stripped bytes
plus blobs and hashed; the record is only committed if the hash equals `id`.
Reconstruction works from the record header as it will be written
(serialised and parsed back), not from in-memory recipes, and from the
attachment files as they are on disk, so a header that lacks something
reconstruction needs fails here. Each copy's range is checked to hold the
encoded body too. Lossless storage is thus checked on every write, not
assumed. If the check fails (a recipe bug), the message is stored with
every attachment as a copy and decorations inline, which needs no
reconstruction, and a warning is logged. An attachment file that is
missing or does not hold its blob is store damage, not a quirk of the
message: ingest stops with an error.

Extraction happens inside `multipart/signed` content too: since
reconstruction is byte-exact, signatures still verify.

### Content addressing

Attachment files are content-addressed: the decoded-content hash is a
blob's identity, and a blob is written only the first time it is seen.
Every later occurrence references the existing file, so a newsletter banner
that passes `decoration_min_size` is stored once no matter how many issues
contain it. The lookup goes through the index; after a `reindex` it is
rebuilt from the hash prefix in each file name.

This only catches byte-identical content. A logo re-encoded by the sender on
each send has different bytes and is stored each time; perceptual matching
is not planned.

### File naming

`attachments/YYYY/MM/<first 16 hex of blob hash>_<name>`

- `YYYY/MM` is the month the blob was first written, or, while the store
  is being initialized, the received month of the first message it was
  written for (see [the layout](#on-disk-layout)).
- `<name>` is the decoded filename (RFC 2231/2047), NFC-normalised, cut to
  what follows its last `\` (Windows clients send full paths such as
  `C:\Users\me\report.doc`), with `/`,
  NUL, control characters, bidirectional controls (U+202E can make
  `…fdp.exe` display as `…exe.pdf`), leading dots and surrounding
  whitespace removed, truncated to 120 bytes of UTF-8 (preserving an
  extension of up to 16 bytes). Fallback: `part-<path>.<ext from MIME
  type>`, with `.bin` when the type has no known extension.
- The first name seen wins; other names are recorded in the index.
- Files are written via temp file + `fsync` + `rename`, then made read-only.
  The temp file is `.<hash prefix>.<random>.tmp` in the month directory;
  ones left by a crash are removed when the writer first uses that
  directory.
- Before writing, the writer also looks in the target month directory for
  a file with the same hash prefix (one orphaned by a crash before its
  message was committed, which the index does not know yet) and reuses it
  after checking its content hash. A file with that prefix but other
  content (a 64-bit prefix collision) is an error, not a match.
- The hash prefix lets `reindex` map files to blobs without re-hashing;
  `verify` re-hashes in full.
- The index records each blob's `kind`, so tools can hide decorations.

## Incoming: recent INBOX mail

Mail that has just arrived in INBOX is likely to be deleted (notifications,
spam that slipped through) or moved soon. Putting it straight into a segment
would permanently archive things the user threw away within minutes, and
would extract and store their attachments forever.

Instead, such messages are held in `incoming/<account>/` as plain
`<sha256>.eml` files — raw, uncompressed, attachments inline — until their
fate is clear. Each account has its own `incoming/` directory, evaluated
on its own, while segments and attachments stay shared by the store, so a
message that several accounts archive is still stored once.

Configuration:

```toml
[incoming]
mailboxes = ["INBOX"]     # by role for JMAP ("inbox"), by name for IMAP
max_age = "7d"            # measured from server receivedAt / INTERNALDATE
on_delete = "drop"        # "drop" | "keep"
grace = "30d"             # how long a dropped message is still held
max_drop_fraction = 0.5   # mass-drop guard, see below
max_drop_count = 200
```

### Placement at ingest

A newly downloaded message goes to the account's `incoming/<account>/` iff
it is currently in one of the `incoming.mailboxes` **and** its received date
is younger than `max_age`. Everything else goes directly to a segment.
Hence, on a first sync of an old INBOX, only the last week lands in
`incoming/`.

If the message's hash already exists in a segment, or in this account's
`incoming/`, nothing is stored; only state events are written. A message
held only in *another* account's `incoming/` gets its own copy: sharing
the file would tie one account's evaluation to the other's. The duplicate
is short-lived, since whichever account promotes the message first writes
the segment record and the others then promote it without writing (see
below).

### Evaluation

Each account's `incoming/` is evaluated after every **complete** sync run
of that account. A failed or partial run skips this step for that account
only; other accounts are evaluated as usual. Everything below concerns the
account being evaluated: its mailboxes, its journal, its runs. A *filed
mailbox* is any of its synced mailboxes that is not in
`incoming.mailboxes` and does not have the Trash or Junk role
(`\Trash`/`\Junk` special-use, JMAP roles `trash`/`junk`).

- **Already archived:** if the message is already in a segment (written by
  another account, or by a promotion that a crash interrupted), it is
  promoted without writing anything: journal `promote` with the location
  of the existing record → unlink the `.eml`. This applies before the rules
  below and to dropped messages too: once the bytes are in a segment, the
  `.eml` is only a duplicate.
- **Promote** if it is in a filed mailbox (the user filed it), or it is older
  than `max_age` and still in any synced mailbox (including Trash/Junk).
  Promotion = extract attachments → append segment record → `fsync` →
  journal `promote` → unlink the `.eml`.
- **Drop** if it has been in none of the account's synced mailboxes at the
  end of two consecutive complete runs of the account. Journal `drop`. The
  `.eml` stays in `incoming/<account>/` for `grace`, hidden from search by
  default, and is then unlinked (journal `purge`).
- Otherwise, leave it.

The two-run requirement covers the IMAP race where a message is moved from
INBOX to a folder that was already synced earlier in the same run: it looks
absent everywhere for one run, then reappears in its new folder on the next
(see [Message matching](#message-matching), so not re-downloaded).

Accounts do not consult each other. A message that both hold in
`incoming/` may be dropped by one and promoted by the other; the archive
keeps it. A message moved from one account to another by a client is
dropped by the first and downloaded as a new message by the second
(message matching is per account), so it is archived once.

A dropped message that reappears in a synced mailbox during its grace period
(restored on the server) is un-dropped (journal `undrop`) and evaluated
normally again.

With `on_delete = "keep"`, nothing is dropped: messages are promoted at
`max_age` whether or not they still exist on the server, and the journal
records that they were deleted. This gives a literal record of everything
received, at the cost of archiving all mail, spam included.

### Why the grace period and the guard

The risk with dropping is not the user deleting mail, but mail being deleted
*for* them: a bad filter rule, a misbehaving client, a compromised account,
a server bug. Older mail is safe in segments, but without a grace period the
last week would vanish from the archive within two runs, and it is precisely
the mail that exists nowhere else. The grace period turns the archive into
an undo buffer for it.

The **mass-drop guard** covers the same failures acting all at once, plus
servers that wrongly return an empty listing. If a run would drop more than
`max_drop_fraction` of an account's `incoming/` or more than
`max_drop_count` of its messages, it drops nothing for that account and
reports why. `geniza sync --allow-mass-drop` overrides.

### Trash and Junk

All mailboxes are synced by default, Trash and Junk included. Moving a
message to Trash or Junk is therefore not "filing" it: it stays in
`incoming/` and is promoted once it reaches `max_age`, unless the Trash is
emptied (or the server purges it) first, in which case it is dropped.

Spam delivered by the server directly to Junk never passes through INBOX and
goes straight to a segment. Adding `"junk"` to `incoming.mailboxes` delays
it like INBOX mail; excluding Junk from sync (see Sync) skips it entirely.

### Properties

- Each account's `incoming/` is bounded by roughly `max_age + grace` of its
  INBOX mail: hundreds to a few thousand files. That is the only place with
  per-message files and the only place where files are deleted.
- It is backed up like the rest of `store/`. Its churn is small and bounded.
- Messages that have reached a segment are never removed by sync (archive
  semantics). Deleting them later is the job of an explicit, future
  `compact` command.
- Crash recovery: an `incoming/` file whose id already appears in a segment
  is promoted without writing at the account's next evaluation (see
  above). Until then it is a harmless duplicate; readers use the segment
  record.

## State: the journal

Per account, append-only JSONL, one file per month, `fsync`ed after each
sync batch. Each line is one event:

```json
{"seq": 1041, "ts": "2026-10-24T09:00:03Z", "op": "mbox", "mbox": "INBOX", "name": "INBOX", "role": "inbox"}
{"seq": 1042, "ts": "…", "op": "add",     "msg": "sha256:…", "mbox": "INBOX", "rid": "1234", "eid": "M6d99ac3275bb4e"}
{"seq": 1043, "ts": "…", "op": "flags",   "msg": "sha256:…", "mbox": "INBOX", "set": ["\\Seen", "$label1"]}
{"seq": 1044, "ts": "…", "op": "remove",  "msg": "sha256:…", "mbox": "INBOX", "rid": "1234"}
{"seq": 1045, "ts": "…", "op": "reset",   "mbox": "INBOX", "uidvalidity": 99}
{"seq": 1046, "ts": "…", "op": "promote", "msg": "sha256:…", "segment": "segments/2026/2026-10.zst", "offset": 81920}
{"seq": 1047, "ts": "…", "op": "drop",    "msg": "sha256:…"}
{"seq": 1048, "ts": "…", "op": "undrop",  "msg": "sha256:…"}
{"seq": 1049, "ts": "…", "op": "purge",   "msg": "sha256:…"}
```

- `mbox` is an account-local mailbox key: the name for IMAP, the mailbox id
  for JMAP (with `mbox` events mapping ids to names and roles).
- `rid` is the remote id: IMAP UID (scoped by the mailbox's current
  UIDVALIDITY) or JMAP email id.
- `eid` is the server's stable, account-wide message id when it has one
  (JMAP `emailId`, IMAP `EMAILID`, Gmail `X-GM-MSGID`); see
  [Message matching](#message-matching). Absent otherwise.
- `flags` always carries the **full** flag/keyword set, never a diff, so
  replay is idempotent.
- Every event assigns a value without looking at what was there before:
  `add`/`remove` set or clear the message at (`mbox`, `rid`); `flags` sets
  the flags of (`msg`, `mbox`); `reset` voids every `rid` and flag set of
  its mailbox; `promote`/`drop`/`purge` set a message's fate in the
  account's `incoming/` and `undrop` clears it. Replaying events that were
  already applied (twice, or re-derived after a crash) changes nothing.
- Fates are per account, like `incoming/`: a message can be dropped from
  one account's `incoming/` and still held in another's.
- Flags belong to a message in a mailbox, so identical copies in one
  mailbox share one set. They are not cleared when the message leaves the
  mailbox (that would make the result depend on event order); readers
  ignore flags of a message that is not there, and a sync writes a `flags`
  event with every `add`.
- An `eid` keeps pointing to its message after the message is removed.
- `seq` starts at 1 and increases by one per event across all files of
  the account. Events are written in batches; a batch shares one `ts` and
  goes to the file of that UTC month. Lines are compact UTF-8 JSON with
  keys in the order shown. An unknown `op` or field is an error, not
  skipped (the format version is in `store.toml`).
- Current state = replay of all journal files, cached in the index (with
  the last `seq` applied, so only newer events are replayed).
  Replaying millions of lines takes seconds.
- Only the end of the last journal file can be torn (lines that do not
  parse up to the end of the file, an unterminated line, zeros). The
  writer truncates it, with a warning, before its first batch, even if it
  then writes to a new month's file; readers skip it with a warning.
  Damage anywhere else is reported and the file is left alone.
- The journal doubles as history: when a message was filed, read, deleted.

`sync.json` holds only cursors and is rewritten atomically (temp + rename):

```json
{
  "identity": "objectid",       // "jmap" | "objectid" | "gmail" | "headers"
  "imap": {"INBOX": {"uidvalidity": 99, "uidnext": 1235, "highestmodseq": 77812}},
  "jmap": {"email_state": "…", "mailbox_state": "…"}
}
```

If it is lost, the next sync is a full resync; bodies are not re-downloaded
because of [message matching](#message-matching).

## Write ordering and crash safety

One writer per store, enforced with `flock(store/.lock)`. The lock file
stays empty and is never rewritten; the kernel releases the lock when its
holder exits, so a crash leaves no stale lock. A second writer fails at
once with "store is in use" rather than waiting. Taking the lock creates
the store directory if it is missing, so a new store's `store.toml` is
written under the lock, by exactly one process. Readers (`search`, `show`, `export`, `verify`) do not lock.

1. Attachment files: temp + `fsync` + `rename`.
2. Segment record (or `incoming/` file): append/write + `fsync`.
3. Journal events: append + `fsync`.
4. `sync.json`: temp + `fsync` + `rename`.

A crash at any point leaves at worst orphaned attachments (picked up by
`verify`/`compact`), a torn record at the end of the last segment, or a
torn tail of the last journal file (see [the journal](#state-the-journal)).
When the writer opens a segment, it removes a torn record at its end;
before its first record it does the same for the store's last segment,
even if it then writes to a new month's (a crash on the last day of a
month leaves the tear in that month's segment). While the store is being
initialized, records go to any month, so it does this for every segment. The records' headers are
walked (without decompressing bodies) and the last record is checked in
full (complete, decompresses, checksum and
`stripped_size` match). A record that runs past the end of the file, a
last record that fails the check, or a zero-filled tail is truncated, with
a warning. Since one record is in flight at a time and each is fsynced,
damage anywhere else cannot come from a crash: it is reported and the file
is left alone.

## Sync

Every mailbox of every account is synced unless excluded. The first sync
that completes for every account of a store ends the store's
initialization (see [the layout](#on-disk-layout)).

```toml
[accounts.work.sync]
exclude = []              # mailbox names (IMAP) or roles, e.g. ["junk"]
```

### IMAP (per mailbox)

Not in the first release.

1. `EXAMINE` (read-only, so not even `\Recent` is cleared; with `QRESYNC`
   when supported). If UIDVALIDITY changed, journal `reset` and treat every
   message as new (deduplication avoids downloads).
2. **New messages:** `UID FETCH <uidnext>:* (UID RFC822.SIZE INTERNALDATE FLAGS
   BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])`, plus `EMAILID` or `X-GM-MSGID`
   when available. Match against stored messages as described in
   [Message matching](#message-matching); download with `BODY.PEEK[]` and
   ingest only what does not match.
3. **Flag changes:** `UID FETCH 1:* (FLAGS) (CHANGEDSINCE <modseq>)` with
   CONDSTORE, otherwise fetch all flags and diff against the index.
4. **Expunges:** `VANISHED` with QRESYNC, otherwise `UID SEARCH ALL` and diff.

Gmail (later): sync only `[Gmail]/All Mail`, map `X-GM-LABELS` to mailbox
membership and use `X-GM-MSGID` as the remote id.

### JMAP

Authentication is a Bearer API token (Fastmail's API tokens; a read-only
token with only the Email scope is enough and recommended). Limits such as
`maxObjectsInGet` come from the session object, never hard-coded.

1. Fetch the session; `Mailbox/changes` (or `Mailbox/get` initially) → `mbox` events.
2. `Email/changes` since `email_state` (fall back to a full `Email/query` on
   `cannotCalculateChanges`).
3. `Email/get` on created and updated ids with
   `[blobId, mailboxIds, keywords, receivedAt, size, messageId]`, batched
   by `maxObjectsInGet`.
4. Download raw blobs for unseen messages and ingest; emit
   `add`/`remove`/`flags` events from the `mailboxIds`/`keywords` diff.
   Destroyed ids → `remove` from every mailbox.

JMAP state strings are atomic, so the IMAP move race does not arise; the
two-run drop rule is applied uniformly anyway.

### Message matching

When a sync sees a message under a remote id it has not seen before (a new
UID after a move, a UIDVALIDITY reset, a resync after losing `sync.json`),
it must decide whether the body is already stored. A wrong "yes" is
**silent data loss**: the new message is never downloaded and its mailbox
membership and flags are attached to another message. A wrong "no" only
costs a download. Matching is therefore exact wherever the server allows,
and verified where it does not.

In order of preference, decided per account from the server's capabilities:

1. **`jmap`** — JMAP `emailId`, which is immutable across moves.
2. **`objectid`** — IMAP OBJECTID (RFC 8474) `EMAILID`, stable across moves
   and copies (Dovecot, Cyrus, Fastmail, …).
3. **`gmail`** — `X-GM-MSGID`.
4. **`headers`** — no stable id. A stored message is a *candidate* if all of
   these hold:
   - the new message has a Message-ID, and it equals the stored one;
   - its `RFC822.SIZE` and `INTERNALDATE` equal those **the same account**
     previously reported for the stored message (never matched across
     accounts, whose servers count sizes differently);

   and a candidate is accepted only after fetching `BODY.PEEK[HEADER]` and
   comparing it byte for byte with the stored header block (which is never
   altered by attachment stripping). `Received:` lines carry per-hop queue
   ids and timestamps, so distinct messages with identical header blocks do
   not occur in practice. Messages without a Message-ID are never matched
   and are always downloaded. (Distinct messages share Message-IDs
   often enough, and some messages have none, that a Message-ID alone is
   no match key.)

With 1–3 the id is recorded as `eid` in the journal, and an `eid` seen
before maps straight to its stored message with no further fetch.

The chosen method is recorded as `identity` in `sync.json`.

The first release is JMAP only, so it implements only `jmap`; the other
three, and the warning below, come with IMAP.

#### Warning when there is no reliable id

When an account falls back to `headers`, `geniza sync` prints a warning
on that account's first sync, and again whenever the method gets worse
(e.g. a server migration loses OBJECTID):

```
warning: account "work": server provides no stable message id
  (no JMAP, OBJECTID or X-GM-MSGID). Messages moved between folders
  will be matched by Message-ID, size and date, then verified by fetching
  their full headers. Messages without a Message-ID will be downloaded
  again after every move. A full resync fetches the headers of every
  message.
```

Setting `accounts.<name>.no_stable_id_ok = true` silences it. The per-run
summary also counts messages that had to be downloaded again because they
could not be matched.

## Index (cache)

`cache/index.sqlite`, rebuilt by `geniza reindex` from segment headers,
segment bodies (for FTS), `incoming/`, `attachments/` and the journals.

- `messages(id, location, segment, offset, frame_size, size, received, date,
  from, to, cc, subject, message_id, in_reply_to, thread_id, state)` —
  `location` ∈ {segment, incoming} (segment if any record holds it);
  `state` ∈ {live, dropped}, dropped only if it is in no segment and every
  account holding it in `incoming/` has dropped it.
- `incoming(account, msg, state)` — one row per `incoming/<account>/` file.
- `membership(account, mbox, msg, rid)`, `flags(account, msg, flag)`.
- `mailboxes(account, mbox, name, role)`.
- `attachments(blob, file, kind, mime_type, size)`,
  `attachment_names(blob, msg, name)`.
- `server_threads(account, msg, thread_id)` — JMAP `threadId` / Gmail
  `X-GM-THRID`, kept as hints only.
- `fts` — FTS5 over subject, addresses, body text (HTML converted to text),
  and optionally attachment text (`attachment_text(blob, text)`, extracted
  with external tools such as `pdftotext`, keyed by blob so it is done once).

SQLite with FTS5 is required; its availability is checked at startup.

### Threads

Server thread ids are not usable as the primary grouping: JMAP `threadId`
semantics differ between servers and are per account, IMAP `THREAD`
(RFC 5256) is per mailbox and not universally supported, and `X-GM-THRID` is
Gmail-only. The index spans all mailboxes and accounts, so threads are
computed locally, from headers only (as notmuch does):

- Each message links its own Message-ID with every id in its `References`
  and `In-Reply-To`. A thread is a connected component of that graph,
  maintained incrementally with union-find.
- A reply arriving before its parent still joins the right thread, because
  it already names the parent's id. A message that connects two threads
  merges them.
- Messages without a Message-ID get a synthetic one derived from their hash.
- Different messages sharing a Message-ID (a sent copy and the mailing-list
  copy with a footer) are distinct messages in the same thread.
- No subject-based merging: wrongly joined threads are worse than split ones.

Thread ids live only in the cache, so the algorithm can change later with a
`reindex` and no migration.

## CLI (initial)

```
geniza sync [ACCOUNT…]         # sync (all accounts by default); each then evaluates its incoming/
geniza search QUERY            # FTS5 query, prints ids + summaries
geniza show ID [--raw]         # reconstruct the original message byte-exactly
geniza export --mbox …         # full reconstruction to mbox
geniza reindex                 # rebuild cache/ from store/
geniza verify                  # re-hash messages and attachments
geniza stats --parts           # dry-run part classification on a mailbox
geniza account rename OLD NEW
```

Global options: `--config PATH`, and `--store NAME` for commands that act on
one store (`search`, `show`, `export`, `reindex`, `verify`). It may be
omitted when there is only one store or `default_store` is set. `sync`
without arguments syncs every account of every store; with account names,
only those (and only their stores are locked).

`show ID` takes a message id (`sha256:…`) or a prefix of its hex digest,
with or without `sha256:`, of at least 4 digits; a prefix that matches
more than one message is an error that lists them. The message is rebuilt
and checked against its id. With `--raw` it is written to stdout byte for
byte; otherwise `show` prints a summary: the record's segment and offset,
its received and stored times and sizes, the message's `From`, `To`,
`Cc`, `Date`, `Subject` and `Message-ID`, and each extracted part with
its kind, mode and file (`--json` gives the same as an object, with the
parts as the record header has them). Until the index exists, the id is
found by reading every segment header.

`export --mbox PATH` writes every message of the store, rebuilt and
checked against its id, to a new mboxrd file (`-` for stdout; an existing
file is not overwritten), in store order and each message once, with the
same `From` line and escaping as segment bodies. A file name ending in
`.gz`, `.bz2`, `.xz`, `.zst` or `.zstd` is compressed with gzip, bzip2, xz
or zstd. A message that cannot be rebuilt is reported and left out, and
the exit status is non-zero.

`verify` reads every record in full, re-hashes every attachment file a
record names and checks it against the record, rebuilds every message and
compares its hash with its id, and reads every journal through. Each
problem is reported (and the exit status is non-zero); damage to one
record does not stop the others from being checked, though damage to a
record's header hides the records after it in that segment. Attachment
files that no record names (orphans of a crash before their message was
committed) are listed but are not problems.

`debug import-local PATH` (development only) ingests a Maildir tree or an
mbox into a store to exercise ingest on real volume before any network
code exists. It writes no journal events, so it is not a migration path.
Local files rarely carry the server's received date (offlineimap leaves a
Maildir file's modification time at the download), so it is taken from the
message: the first `Received:` line with a usable date (the last hop),
else `Delivery-date:`, else an mbox `From ` line's date, else `Date:` (all
that sent mail and drafts have), else the file's modification time. Dates
before 1980 or more than a day in the future are skipped. Maildir messages
go in oldest first. An mbox is read as it is: whether `>From ` in a body is
an escape cannot be told, so it is kept. Running it again stores nothing new, so a second run
reporting every message as already stored, followed by a clean `verify`,
shows that the store holds every source message byte for byte.

`stats --parts` scans a mailbox (remote, or an existing Maildir/mbox) without
storing anything and reports, per class, the number and size of parts, how
many would be stubbed vs. copied, the file count and inline bytes at
several `decoration_min_size` values, and how often identical decorations
repeat. It is meant to be written early, to set the defaults from real mail.

## Implementation notes (Python ≥ 3.15)

- zstd: stdlib `compression.zstd` (added in 3.14). Skippable frames are
  written by hand (8-byte header + payload).
- Config: stdlib `tomllib`. Hashing: `hashlib.file_digest`.
- MIME: stdlib `email` with `policy.default` for analysis. All byte-range
  work (stub offsets, splicing) is done on the raw bytes, never on a
  re-serialised `email.message` object.
- JMAP: plain HTTP + JSON; stdlib `urllib` is enough, with downloads streamed
  to temp files.
- Secrets: `cryptography` (AES-GCM, HKDF, Ed25519/RSA signature
  verification), none of which the standard library provides. It is the
  only third-party dependency of the first release.
- IMAP (after the first release): `imapclient` (CONDSTORE/QRESYNC response
  parsing), the only other planned dependency.

## Open questions

- Defaults for `max_age` and the mass-drop guard are guesses. `stats
  --parts` cannot inform them: a Maildir has no server receive dates or
  deletion history.
- A small share of base64 parts does not round-trip with a fixed line
  length. With `layout` and `pad_bits` (see "Recipes") nearly all of them
  stub; the rest have periodic layouts (a blank line after every line;
  lines alternating 64 and 8 characters), which runs cannot express.
  They stay copy unless the pattern turns out to be common.

## Not planned (for now)

- A pack for small repeated parts (inline logos below the threshold). Their
  duplication is accepted.
- Perceptual deduplication of re-encoded images.
- Pushing changes to the server; serving local clients.
