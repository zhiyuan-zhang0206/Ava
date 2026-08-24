# Encrypted checkpoint backups use gzip plus AES-CBC

## Context

LangGraph checkpoint tables are the only copy of Ava conversation history.
Their previous exclusion from the daily dump made a database restore lose every
conversation. The backup command also exposed its full database URL, including
the password, in process argv for as long as an hour.

The original encryption ruling specified `openssl enc -aes-256-gcm -pbkdf2
-salt -kfile`. Both available implementations rejected it: LibreSSL 3.3.6
failed the encryption attempt, and OpenSSL 3.6.3 reports that `enc` does not
support AEAD ciphers. `enc` has no artifact format for persisting GCM's
authentication tag, and the available CMS password-recipient mode also rejected
GCM.

## Decision

The daily backup includes every checkpoint table, writes a custom `pg_dump`,
compresses it with gzip, and encrypts the compressed dump using:

```text
openssl enc -aes-256-cbc -pbkdf2 -salt -kfile <private-derived-key-file>
```

The published artifact is `<db>-<utc>.dump.gz.enc`. The password is passed only
as `PGPASSWORD` in the `pg_dump` child environment. The encryption key file
contains the SHA-256 hex digest of the cluster secret, is mode 0600, and is
deleted after each transform. Local artifacts are mode 0600; the backup
directory is mode 0700. The encrypted artifact is copied to the writable
Google Drive sync folder before both local and remote retention prune to seven
managed artifacts.

## Alternatives rejected

- **AES-256-GCM through `openssl enc`.** Required the original format but is
  not executable on either installed OpenSSL implementation.
- **A different OpenSSL GCM container.** The available password-recipient CMS
  path also rejects GCM. Adding a new crypto dependency or custom container
  would violate the zero-dependency operational requirement.
- **Leave artifacts plaintext.** Fails to protect conversation contents at
  rest and would make the Drive copy expose them.

## Consequences

- AES-CBC provides confidentiality but not an authenticated ciphertext format.
  Gzip CRC and `pg_restore` detect ordinary corruption; the restore drill is
  the operational proof that an artifact remains usable.
- The private key file and `PGPASSWORD` avoid exposing either secret in argv.
- gzip reduces artifact size before encryption and makes restore a required
  decrypt → gunzip → `pg_restore` sequence.
- The Drive copy is best-effort: an unavailable folder warns while preserving
  the local encrypted artifact. A remote object store remains a future
  replacement option for hosts without Drive access.
