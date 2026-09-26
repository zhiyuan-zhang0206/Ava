# Split deployment

A split cluster has a gateway that owns the application schema and one or more
runners. Start the gateway before joining runners. Each host has its own home;
the gateway supplies runner connection facts through authenticated bootstrap.

## Gateway first start

Acquire dependencies in the canonical checkout as described in
[the deployment guide](../SKILL.md). Prepare a private dotenv configuration file
outside the home containing the transport policy and source network:

```dotenv
AVA_TRANSPORT_ENCRYPTION=overlay
AVA_TRUSTED_CIDRS=<private-network-source-range>
```

Use `overlay` only when the deployment actually has that encrypted
transport; the other supported declarations are `tls` and `mtls`. Then run:

```bash
.venv/bin/ava start --serve-gateway --no-serve-agent-runner \
  --machine-name machine-1 --machine-host <this-host-addr> \
  --gateway-url http://<reachable-address>:8000 \
  --config-file /absolute/path/gateway.env
```

The gateway-only identity mints the control-plane bearer and independent
Postgres-owner, runner, Redis-admin, and Redis-runtime credentials before
provisioning. The configuration input is bound to this initialization; retry
with the same bytes or omit it. Transfer the bearer through the operator's
secret channel. Do not transfer the gateway environment file to a runner.

For locally owned storage, keep the generated DB and Redis URLs. Bootstrap
projects the runner's DB username and password and rewrites loopback hosts to
the gateway's reachable `machine_host`. The gateway itself dials its local
storage. Off-box reachability requires both the configured private network and
the credential for the caller's role.

Postgres and PgBouncer expose the configured reachable address only with
control-plane authentication enabled. Redis uses the platform's native network
contract: macOS has its host relay, while Linux binds the authenticated native
instance to the configured address. See the [runbook](../../../../conventions/runbook.md)
for binding and firewall ownership. No-secret single-box storage remains local.

## Join each runner

Acquire its dependencies and follow [join a runner](join-a-runner.md). Its one
first-start command validates bootstrap, records identity, registers the host,
and launches its root-owned services. The gateway must reach the runner's
`--machine-host` and ops port; a successful connection from the runner to the
gateway alone does not establish that return path.

Use a distinct health-port block when units share a loopback namespace. A
runner does not cache the gateway's DB/Redis URLs or owner credentials; each
process fetches its runner projection at startup. Model-provider credentials
remain local to the runner.

## External data plane

A gateway may explicitly name a paired foreign Postgres and Redis service in
its first-start config, together with the existing runner DB credential:
`AVA_DB_URL`, `AVA_REDIS_URL`, and `AVA_RUNNER_DB_PASSWORD`. The DB URL must use
the application-owner identity. The three fields are required together; local,
mixed-ownership, or query-redirected endpoints are rejected.

The external service must already provide Ava's baseline schema and runner role.
Start does not initialize or manage its native instances, provision roles, or
apply Redis ACLs. The explicitly supplied DB owner still authorizes ordinary
application migrations. Native instance ownership and application-schema
ownership are separate boundaries.

## Verify both directions

Require successful start and fresh status on the gateway and each runner. Then
request a small task targeted at the new runner through the gateway. This
checks scheduling and runner execution in addition to bootstrap reachability.

On macOS, `ava firewall status` shows the current allowlist, and convergence
reports any manual action needed for the exact serving binaries. A listener
working on loopback is not proof it is reachable from another host.
