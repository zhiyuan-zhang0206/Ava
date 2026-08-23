# Loki data-read semantics hardening

The data audit found several readers relying on retention or label timing as
implicit correctness boundaries. This change makes those boundaries explicit:

- the shared event selector excludes the archival stream, while cutover reads
  use explicit legacy/indexed label-presence flags;
- every ledger-plus-Loki reader follows one gap-day plan, so a retained newest
  ledger day is reread from live events rather than trusted as final;
- retention and Loki query concurrency have one code declaration, checked
  against the rendered native configuration.

The archive count remains exposed as a frozen historical estimate rather than
as a live operational growth gauge. The accompanying tests lock the
closed-day late-write and cutover-boundary cases that motivated the change.
