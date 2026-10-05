# Logical-backup passphrase: minted at birth, pinned, independent of the cluster secret

## Context

Every logical backup artifact (`pg-backup`, snapshots, the pre-activation
floor) is encrypted with `openssl enc -aes-256-cbc -pbkdf2` under one
passphrase. It was `sha256(AVA_CLUSTER_SECRET)` until the secret first
rotated, when the rotation pinned that value to
`$AVA_HOME/backups/logical-backup.passphrase`. Two defects followed from
deriving the key from the bearer:

- An empty secret (the single-box default) made the key `sha256("")`, a public
  constant: those artifacts had no confidentiality, and a configured off-site
  store received them as they were.
- Any change of the secret that skipped the rotation command's pin (a config
  write, a hand edit) silently orphaned every earlier unpinned artifact.

## Decision

User ruling, 2026-09-28:

1. A gateway home's birth mints a random passphrase (256 bits), writes it 0600
   and pins it (`passphrase.ensure_minted`, run when the start intent
   publishes a gateway claim). From then on the key has nothing to do with the
   cluster secret, and an empty-secret single box encrypts for real.
2. A home born earlier pins, once, in the cutover's `api` step
   (`scripts/cutover_db_authority.py`), the passphrase it has encrypted under
   so far: `sha256(secret)`, so its earlier artifacts keep decrypting. A
   single box, which keeps its bearer, pins without rotating; a networked home
   pins inside its one bearer rotation.
3. Readers and writers never derive: a home without a pin refuses to back up
   or restore, and names the cutover.

For an existing empty-secret home the two halves of the ruling collide:
pinning `sha256("")` keeps its old artifacts restorable but encrypts every
future one under a public constant. The coordinator's ruling for this case:
the cutover pins a minted passphrase, and the old artifacts decrypt only
through an explicit restore option, `scripts/restore_drill.py
--legacy-empty-secret-passphrase`, which is never tried as a fallback. A
decryption failure names that option. Production clusters carry a secret and
are not affected.

## Alternatives rejected

- **Keep deriving from the secret and pin on every change.** Leaves the empty
  secret's public key in place and depends on every writer of the secret
  remembering to pin.
- **For an existing empty-secret home, pin `sha256("")` (A).** Old artifacts
  stay restorable through the ordinary path, but every later backup keeps a
  public key, which the ruling excludes.
- **Mint and pin, with no way back to the old artifacts (B alone).** Old
  artifacts had no confidentiality anyway, but the repository's own restore
  tool could no longer open them.
- **Mint, pin and re-encrypt the old local artifacts at the cutover (C).**
  Much more code inside the cutover, and off-site copies would stay under the
  public key regardless.
- **Refuse off-site publication while the key derives from an empty secret.**
  Closes only the exfiltration half and leaves local artifacts unprotected.

## Consequences

- The pinned file is the only key to every logical backup of a home: losing
  it loses them all. It must be exported and kept with the gateway's backup
  keys (see the backup OKF).
- Rotating the cluster secret, by the command or otherwise, never affects
  backups.
- Homes born by this branch before the change (development and preview homes)
  have no pin until they run the cutover's `api` step or are recreated.
- An empty-secret home's artifacts from before its cutover remain effectively
  plaintext wherever they were copied.
