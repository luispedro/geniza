# sshcrypt compatibility fixtures

Generated once with the original standalone `sshcrypt.py`, to pin geniza's
reimplementation to the same wire format.

- `id_ed25519`, `id_rsa`: **throwaway test keys** with no passphrase, made
  only for these fixtures. They protect nothing.
- `plaintext.txt`: the encrypted content.
- `plaintext.ed25519.sshcrypt`, `plaintext.rsa.sshcrypt`: `sshcrypt.py enc`
  output (raw binary, not the `sshcrypt:` base64 form used in config files),
  encrypted with each key.

A test loads a key into its own private `ssh-agent` and checks that geniza
decrypts the matching file to `plaintext.txt`.
