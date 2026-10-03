# Physical backup runner projection audit

## Decision

No `physical_backup` field is bootstrap-served to agent-runners. The physical
backup settings model is built under the gateway, agent, and runner profiles,
but its credentials are host-only files and cannot be projected safely to a
pure runner. The cluster-pinned physical-backup configuration therefore stays
gateway-local through `bootstrap: false`.

The alternative—serving any cluster-pinned physical-backup field through
`GET /api/bootstrap`—was rejected. A runner would then build the domain with
cluster enablement or backend settings while its required local credential files
are absent, allowing the model validator to prevent every runner process from
starting.

## Consequences

- A registry-derived regression test asserts that the bootstrap field set and
  the physical-backup cluster-pinned field set remain disjoint.
- Adding a physical-backup cluster-pinned field requires an explicit decision
  before it can become bootstrap-served.
