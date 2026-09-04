# Pending normal release continuation reconstruction

PR #1566 was reconstructed on top of the authoritative pending-publication
activation branch rather than rebasing its historical parent stack. The retained
scope connects the existing restricted per-unit updater to selector version two,
prepared normal service startup, exact service readback, and the pending
publication journal. Historical publication, inventory, and bootstrap commits
were not replayed.

The continuation reuses one updater process, flock, handoff generation,
operation, and challenge. It prepares the complete service plan before stopping
bootstrap, pins dependency order before mutable roster state can change, and
requires fresh pending authorization before every selector, stop, start, and
readback effect. Current publication by the all-unit coordinator remains the
terminal condition; a local successful launch is not publication authority.

The reconstruction preserves both evidence boundaries introduced by its
parents. Receipt lookup and selector version two use `prepared_receipt_digest`,
the complete sealed receipt SHA-256. The receipt's `inventory_digest` remains
the narrower `ExpectedUnitWriters` tuple digest and must match the planned unit.
Normal crash evidence is nested under the versioned bootstrap recovery envelope
only after exact candidate-ready proof. It never contaminates ordinary spawn
ownership, and any non-committed normal phase prevents generic recovery from
discarding the retained bootstrap and selector evidence.

Normal/source first handoff, complete non-session launcher quiesce, checked
normal rollback, distributed coordination, and unsupported native readiness
adapters remain outside this reconstruction. They require new explicit evidence;
an expired operation or retained journal does not authorize compensation.

## Safety review update

Adversarial review found that the planned activation could crash after selector,
stop, or spawn effects without checked forward recovery. Service spawn also has
an ambiguous interval after fork and before the session record exists. The
reconstruction therefore keeps plan construction and evidence schemas but
disables every activation entry point before updater ownership or service
effects. Activation requires a later design with stage-specific recovery and an
exact durable spawn receipt.

The retained envelope was tightened independently: bootstrap records a planned
normal continuation before its first journal write; bootstrap rewrites cannot
discard nested normal evidence; normal writes preserve identity and advance only
through legal phases; and generic manual or automatic recovery refuses any
planned, malformed, or unfinished normal evidence. This is a retention fence,
not recovery authority.
