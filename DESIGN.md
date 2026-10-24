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
- IMAP (with CONDSTORE/QRESYNC when available) and JMAP.
- Several stores per configuration, each holding several accounts.
- Full-text search via a rebuildable local index.

Out of scope for now:

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
  period is over. Borg therefore only ever sees new bytes at the end of a
  few files, plus new files.
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
  .lock                                   # flock() target; single writer
  incoming/
    <sha256>.eml                          # recent INBOX mail, raw, unmodified
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
  index.sqlite
```

Dates in `segments/` and `attachments/` paths are the **month the data was
written**, not the message date. This is what makes them sealable: a month
directory or segment never changes once that month is over. Message dates
live in the index.

`store.toml` holds a random store id (UUID) and the on-disk format version.
The index records the id of the store it was built from; on a mismatch (a
cache directory reused for a different or restored store) it is discarded
and rebuilt.

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

[accounts.uni]
store = "work"
type = "imap"
host = "imap.example.edu"
user = "me"
password_command = "pass show mail/uni"

[accounts.uni.sync]
exclude = ["junk"]
```

- Store names and account names are unique within the configuration and are
  used as directory names (`<store>`, `state/<account>/`), so they are
  restricted to `[a-z0-9_-]`.
- Paths default to `$XDG_DATA_HOME/geniza/<store>` and
  `$XDG_CACHE_HOME/geniza/<store>`, with the XDG fallbacks
  (`~/.local/share`, `~/.cache`) when the variables are unset.
- Store settings (`[attachments]`, `[incoming]`) are merged key by key:
  built-in default ← top-level table ← `[stores.<name>.…]`.
- Secrets never appear in the file in plain text (see Secrets).
- Renaming an account would orphan its `state/<account>/` directory, so a
  rename is an explicit `geniza account rename OLD NEW`, which moves the
  directory and updates the configuration.

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
      "eol": "crlf",             // line endings of the encoded form
      "final_eol": true
    }
  ]
}
```

`frame_size` lets a scanner skip bodies without decompressing them, so the
index can be rebuilt from headers alone (bodies are needed only for FTS).

### Segment size

Segments are sealed at the end of their month or when they reach 256 MiB,
whichever comes first. The month is the UTC month of the `stored` time. A
record that would take the open segment past 256 MiB starts the next
rollover segment instead (`2026-10.1.zst`, `2026-10.2.zst`, …), unless the
segment is empty, so a single larger message still fits. The cap is not driven by borg: when the open segment
grows, borg re-reads it (sub-second for 256 MiB on local disk) but only
stores the new chunks at its end. Removing a record during a future
`compact` shifts the bytes after it, and borg's content-defined chunking
resynchronises past the gap, so even rewritten segments mostly deduplicate.
The cap mainly limits the damage of a corrupted file.

In normal operation the cap is rarely reached (at ~5 KB per compressed
stripped message, 50 messages/day is ~7.5 MB/month). It matters for the
**initial import**, which writes the whole history into the current month's
rollover files. To keep those files useful for `zstdgrep` and locality, the
initial import downloads messages **sorted by received date across all
mailboxes**. This is free: dates (INTERNALDATE, `receivedAt`) are fetched
before bodies anyway.

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
   `.wmz` drawings) go on to the next rules: in real mail some of them are
   attachments, and there are few enough that extracting them all costs
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

The default comes from `geniza stats --parts` on a personal Maildir
(331k unique messages, 52.5 GB; 42k attachments, 26 GB decoded; 63k
decorations, 6.3 GB decoded, but only 13k distinct blobs). File counts
and bytes count each distinct blob once; inline bytes are what stays in
message records, before compression:

| `decoration_min_size` | files  | decoration files | file bytes | inline bytes |
|-----------------------|--------|------------------|------------|--------------|
| 16 KiB                | 41,968 | 7,861            | 24.18 GB   | 9.68 GB      |
| 64 KiB                | 38,748 | 4,465            | 24.06 GB   | 10.30 GB     |
| 128 KiB               | 37,388 | 3,030            | 23.94 GB   | 11.09 GB     |
| 256 KiB               | 36,215 | 1,790            | 23.72 GB   | 11.99 GB     |
| 1 MiB                 | 34,946 | 449              | 23.06 GB   | 14.94 GB     |

Lowering the threshold saves much more inline than it adds in files,
because a pasted image is re-sent with every reply in a thread and is
stored once as a file. Net saving per extra file: about 1.8 MB from 1 MiB
to 256 KiB, 530 KB from 256 KiB to 64 KiB, and 150 KB from 64 KiB to
16 KiB, where the extra files are mostly signature logos. 64 KiB is the
knee: below it, each extra file saves little, and at 16 KiB decorations
would make up a fifth of `attachments/`. zstd recovers part of the base64
overhead of inline images, so the real differences are somewhat smaller.

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
  or padding inside the text) falls back to copy. Layouts seen in real
  mail: one line split in two (Outlook for Mac), content encoded in
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
  with LF (`eol: "crlf"` records the conversion), so a `.py` or `.patch`
  file is usable directly. A `text/*` part with mixed line endings is
  stubbed without conversion (`eol` is then that of its last line): the
  file is the body as is, which is what copy mode would write too, so
  copy would only keep a second copy of the same bytes in the message.
  Writing such a file with LF would need a record of which lines had
  CRLF, a format change not worth making: no such part occurred among the
  42k attachments in the measured mailbox.
- `quoted-printable`: soft line breaks cannot be reproduced reliably, so QP
  parts are always copy mode. This covers many small text attachments; for
  `text/*` parts whose decoded content uses CRLF consistently, the file is
  written with LF, as for `identity`.
- Any other `Content-Transfer-Encoding` (`x-uuencode`, …) is not decoded,
  and the part stays inline whatever its class.

After stripping, the full message is reconstructed from the stripped bytes
plus blobs and hashed; the record is only committed if the hash equals `id`.
Lossless storage is thus checked on every write, not assumed.

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

- `YYYY/MM` is the month the blob was first written.
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
- The hash prefix lets `reindex` map files to blobs without re-hashing;
  `verify` re-hashes in full.
- The index records each blob's `kind`, so tools can hide decorations.

## Incoming: recent INBOX mail

Mail that has just arrived in INBOX is likely to be deleted (notifications,
spam that slipped through) or moved soon. Putting it straight into a segment
would permanently archive things the user threw away within minutes, and
would extract and store their attachments forever.

Instead, such messages are held in `incoming/` as plain `<sha256>.eml` files —
raw, uncompressed, attachments inline — until their fate is clear.

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

A newly downloaded message goes to `incoming/` iff it is currently in one of
the `incoming.mailboxes` **and** its received date is younger than `max_age`.
Everything else goes directly to a segment. Hence, on a first sync of an old
INBOX, only the last week lands in `incoming/`.

If the message's hash already exists in a segment, nothing is stored; only
state events are written.

### Evaluation

After every **complete** sync run of all accounts (a failed or partial run
skips this step), each message in `incoming/` is evaluated. A *filed
mailbox* is any synced mailbox that is not in `incoming.mailboxes` and does
not have the Trash or Junk role (`\Trash`/`\Junk` special-use, JMAP roles
`trash`/`junk`).

- **Promote** if it is in a filed mailbox (the user filed it), or it is older
  than `max_age` and still in any synced mailbox (including Trash/Junk).
  Promotion = extract attachments → append segment record → `fsync` →
  journal `promote` → unlink the `.eml`.
- **Drop** if it has been in no synced mailbox of any account at the end of
  two consecutive complete runs. Journal `drop`. The `.eml` stays in
  `incoming/` for `grace`, hidden from search by default, and is then
  unlinked (journal `purge`).
- Otherwise, leave it.

The two-run requirement covers the IMAP race where a message is moved from
INBOX to a folder that was already synced earlier in the same run: it looks
absent everywhere for one run, then reappears in its new folder on the next
(see [Message matching](#message-matching), so not re-downloaded).

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
`max_drop_fraction` of `incoming/` or more than `max_drop_count` messages, it
drops nothing and reports why. `geniza sync --allow-mass-drop` overrides.

### Trash and Junk

All mailboxes are synced by default, Trash and Junk included. Moving a
message to Trash or Junk is therefore not "filing" it: it stays in
`incoming/` and is promoted once it reaches `max_age`, unless the Trash is
emptied (or the server purges it) first, in which case it is dropped.

Spam delivered by the server directly to Junk never passes through INBOX and
goes straight to a segment. Adding `"junk"` to `incoming.mailboxes` delays
it like INBOX mail; excluding Junk from sync (see Sync) skips it entirely.

### Properties

- `incoming/` is bounded by roughly `max_age + grace` of INBOX mail: hundreds
  to a few thousand files. That is the only place with per-message files and
  the only place where files are deleted.
- It is backed up like the rest of `store/`. Its churn is small and bounded.
- Messages that have reached a segment are never removed by sync (archive
  semantics). Deleting them later is the job of an explicit, future
  `compact` command.
- Crash recovery: an `incoming/` file whose id already appears in a segment
  is simply unlinked at startup.

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
- Current state = replay of all journal files, cached in the index.
  Replaying millions of lines takes seconds.
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

One writer per store, enforced with `flock(store/.lock)`.

1. Attachment files: temp + `fsync` + `rename`.
2. Segment record (or `incoming/` file): append/write + `fsync`.
3. Journal events: append + `fsync`.
4. `sync.json`: temp + `fsync` + `rename`.

A crash at any point leaves at worst orphaned attachments (picked up by
`verify`/`compact`) or a torn record at the end of the open segment.
When the writer opens a segment, it removes a torn record at its end: the
records' headers are walked (without decompressing bodies) and the last
record is checked in full (complete, decompresses, checksum and
`stripped_size` match). A record that runs past the end of the file, a
last record that fails the check, or a zero-filled tail is truncated, with
a warning. Since one record is in flight at a time and each is fsynced,
damage anywhere else cannot come from a crash: it is reported and the file
is left alone.

## Sync

Every mailbox of every account is synced unless excluded:

```toml
[accounts.work.sync]
exclude = []              # mailbox names (IMAP) or roles, e.g. ["junk"]
```

### IMAP (per mailbox)

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
   and are always downloaded.

With 1–3 the id is recorded as `eid` in the journal, and an `eid` seen
before maps straight to its stored message with no further fetch.

The chosen method is recorded as `identity` in `sync.json`.

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
  `location` ∈ {segment, incoming}; `state` ∈ {live, dropped}.
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
geniza sync [ACCOUNT…]         # sync (all accounts by default), then evaluate incoming/
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
- IMAP: `imapclient` (CONDSTORE/QRESYNC response parsing).
- Secrets: `cryptography` (AES-GCM, HKDF, Ed25519/RSA signature
  verification), none of which the standard library provides.
- These two are the only planned third-party dependencies.

## Open questions

- Defaults for `max_age` and the mass-drop guard are guesses. `stats
  --parts` cannot inform them: a Maildir has no server receive dates or
  deletion history.
- In the measured mailbox, about 1.4% of base64 attachments (600 of 43k)
  and 475 decorations did not round-trip with a fixed line length. With
  `layout` all but 48 stub; in a sample of those, the run cap counted
  lines and was too tight for 2047-character lines, and some encoders
  left non-zero padding bits (both now handled, see "Recipes"). What is
  left after that is not yet measured.

## Not planned (for now)

- A pack for small repeated parts (inline logos below the threshold). Their
  duplication is accepted.
- Perceptual deduplication of re-encoded images.
- Pushing changes to the server; serving local clients.
