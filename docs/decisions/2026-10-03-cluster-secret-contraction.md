# The human cluster secret stays with the gateway, the CLI and the root

## Context

`AVA_CLUSTER_SECRET` is the human bearer: the API login, the frontend login, the root of the
telemetry token. Machine callers already present per-generation machine API tokens
(`base/cluster/authority/api.py`), and the gateway accepts them on every API route. A read-only
audit (2026-10-02) found three non-gateway components still holding the secret itself:
im_bridge (it logged in to the gateway with it and accepted it as the bearer of its `/send`),
the callers of that `/send` (alert delivery, the health probe's owner alert), and the heartbeat's
observability-station probe (it derived the OTLP telemetry token from it).

## Decision

Outside the gateway, the CLI and the root, no component holds the secret.

- **im_bridge** presents its delivered machine API token (`gateway_auth_headers()`) and never
  logs in; `/send` accepts the active write generation's two machine tokens
  (`base.cluster.machine.daemon_acceptance`, the acceptor half of `gateway_bearer`, shared with the
  ops server). The `source_verified_by` fact of an IM-origin inbound becomes
  `machine_token:gateway` instead of `user_session`: it is audit evidence, not an authorization
  input.
- **Callers of `/send`** send `gateway_auth_headers()`.
- **The telemetry token** is derived by the root that holds the secret (the gateway home's start)
  and written to a private file (`$AVA_HOME/db-authority/telemetry-token`, 0600); the heartbeat's
  station probe reads that file. It does not travel in an environment variable.
- **Exception: a remote-managed data plane** (`settings.data_plane.is_remote`) keeps no write
  generations and issues no token, so there its gateway-local services present, and its daemons
  accept, the human secret (`daemon_acceptance`, `gateway_bearer`). The live deployment's data
  plane is local; the exception is written in the code and runs only on that branch.

## Alternatives rejected

- **A token delivered to the heartbeat in its launch environment**, like the API token: the
  telemetry token outlives every write generation, so an environment copy would sit in every
  process dump and `ps eww` of a long-lived daemon; a private file the root rewrites at each start
  has the same lifetime as the secret's rotation and no wider exposure.
- **Keeping the human secret in im_bridge's `/send` acceptance during a transition**: it would keep
  the one component outside the trust boundary able to authenticate with the secret, which is the
  property being removed.
- **Giving im_bridge a session instead of a bearer**: the gateway already accepts machine tokens on
  every route im_bridge uses; a login adds a cookie lifecycle and a retry loop for nothing.

## Consequences

- A write-generation rotation reaches im_bridge through its restart (it reads the token from its
  environment); a rotated human secret reaches the station probe at the next start, which
  republishes the telemetry token.
- A cluster whose gateway process holds no delivered token while its secret is set would present
  the secret to `/send` and be refused; the root delivers the gateway-class token to the gateway
  services, so this is a launch defect to fix, not a state to accommodate.
