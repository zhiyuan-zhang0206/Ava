# Provider plugins retain complete pricing semantics

Provider plugins remain the sole runtime price source for chat models. The
archive remains the reviewed reconciliation ledger and the runtime source only
for catalog-only services.

The rejected boundary was to send historical, tiered, and scheduled chat
lookups back to the archive after plugin registration. That would make the
source depend on the requested instant and recreate two runtime authorities.
Instead, each plugin declaration mirrors the archive's complete effective-
period, token-tier, and recurring-window lattice. Registration converts that
declaration to the archive shape and reuses the same parser and selector.

The flat rate fields stay as an additive compatibility shortcut for older
plugins and as a readable current-base-tier summary. The provider API remains
version 2 because the new fields are optional. The pricing synchronization bot
now replaces and compares the complete declaration, allowing future boundaries
to take effect at runtime without waiting for another bot run.
