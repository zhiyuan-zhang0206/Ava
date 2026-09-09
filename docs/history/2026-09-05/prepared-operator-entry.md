# Prepared operator entry

The retained-image operator entry was rebuilt on the current publication model
instead of replaying its former branch history. The safe boundary validates one
private request against the loaded unit and its complete normal and recovery
images, then refuses before any shared-state effect. It does not connect to the
database, create a pending operation, stop or start services, migrate, select a
release, publish, or finalize.

The prepared receipt and observer inventory remain separate identities. Receipt
lookup and full-byte verification use `prepared_receipt_digest`; the embedded
`ExpectedUnitWriters` tuple must independently reproduce `inventory_digest`.
Local validation binds both facts but is not promoted to participant evidence.

The earlier dispatch draft had two invalid assumptions. First, a pure runner's
`ava_runner` database projection cannot update `deployment_state`; owner-backed
tests hid that production permission boundary. Second, a stage-only pending
record has no checked abort or forward transition, so it would continue freezing
admission and later updates after the request deadline. The rebuild therefore
does not write prepared dispatch state. Future work requires an authenticated
gateway/coordinator adoption boundary and an exact no-effect pre-stop abort
before an all-unit barrier can be enabled.
