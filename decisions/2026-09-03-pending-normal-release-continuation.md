# Reuse the existing updater for pending normal release effects

The deployment row already serializes rollout ownership, and the per-unit updater
already owns a local flock and durable handoff. Normal immutable release startup
therefore extends those mechanisms instead of introducing another controller,
callback listener, permission registry, or startup git/uv repair path.

Pending evidence freezes births. Adopted all-unit closure and an observed
migration set authorize an exact selector CAS and prepared normal services under
the live operation. Actual service readbacks permit current publication; normal
agent admission is separate and remains frozen before completion. Requiring
current publication to start these infrastructure services would make first
publication circular, while allowing all ordinary startup during pending would
discard the barrier.

The filesystem selector and native process effects cannot share a PostgreSQL
transaction. Short fresh authorization checks, exact identities, immutable
command plans and retained recovery evidence bound this external-effect seam;
they do not make it atomic. Lost authority never becomes automatic rollback
permission. A new operation must explicitly adopt recovery and reverse the
selector against its exact predecessor after compatibility and closure checks.
