# Verified releases keep the existing deployment authority

## Context

Production currently executes an editable source checkout. A prepared wheel
generation removes that dependency, but packaging alone does not authorize it to
migrate a database, stop writers, or become the serving image. In particular,
the migration runner trusts Git's tracked-file set and a checkout-owned home;
an absolute `AVA_HOME` supplied to a wheel is not equivalent evidence.

## Decision

Use the existing deployment operation and lease as the cluster-wide transition
authority. Use one local release selector as the unit's active-image authority.
The operation records its expected predecessor and verified candidate; it cannot
override a contradictory selector or manufacture a successful observation.

Candidate execution binds to its final generation path once. Before migration,
the transition validates the candidate's prepared manifest digest, complete SQL
inventory, canonical owning unit home, and the database's gateway machine/home
tuple. Development checkout protections remain unchanged; environment overrides
do not grant release authority. Authority precedes even legacy migration-history
conversion.

Prepare and verify the candidate, a genuinely bootable last-known-good image,
the recovery point, and platform prerequisites while the old image serves.
Only then enter maintenance and obtain positive stop/upgrade acknowledgement
from every possible legacy writer, including remote hosts and OS jobs. An
unreachable or unacknowledged writer blocks protocol promotion. Schema changes
must remain compatible with both candidate and rollback image. Activation uses
an expected-predecessor selector commit, followed by actual service and claim
verification. Recovery uses the same operation and verified rollback image.

The first transition must execute a verified new transition entry point; an
already imported old orchestrator does not gain new behavior from a checkout
change. The global launcher and OS jobs must stop selecting the old source in
the same cutover. No startup Git/uv fallback or second repair writer is added.

## Alternatives

A new host daemon or deployment database would create a second authority without
removing the current one. A source backup does not prove a bootable rollback.
An environment flag bypassing checkout ownership would recreate the original
development-to-production migration defect. These alternatives are rejected.

## Consequences and proof boundary

Preparation/consumer CI is not deployment acceptance. Native macOS proof must
name its architecture and Python ABI; loopback CI does not prove Tailnet ingress
or firewall approval for a new interpreter path. Such approval must be checked
before stopping the old image, without disabling OS protections.

Crash-point tests must cover candidate preparation, writer barrier, migration,
selector commit and failed start. The old serving generation must survive failed
preparation. The old-orchestrator handoff and rollback image must be exercised,
not inferred from file existence. Until these gates have concrete evidence,
activation and protocol-v1 promotion remain disabled.
