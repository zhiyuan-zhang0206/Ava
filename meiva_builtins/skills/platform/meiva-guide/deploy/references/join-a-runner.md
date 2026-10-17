# Join a runner through `meiva init`

A runner joins an already-serving gateway. Its identity is its local home and
gateway URL; its database login, machine API token and telemetry token arrive
in a capability bundle the gateway operator issues for this unit (the login and
API token are the write generation's, shared by every runner unit, so guard the
bundle and its transport key accordingly). It never holds the gateway's human
cluster secret, and it creates no local cluster data plane.

On the gateway, issue the unit's capability bundle (0600) and note the transport
key it prints once:

```bash
meiva cluster db-authority issue-unit --machine machine-2 \
  --home <the runner's absolute $MEIVA_HOME> --out machine-2.bundle
```

Carry the bundle to the runner and the key separately. Acquire dependencies in
the canonical source checkout as described in
[the deployment guide](../SKILL.md), then run:

```bash
printf 'Capability transport key: ' >&2
IFS= read -rs MEIVA_DB_CAPABILITY_KEY
printf '\n' >&2
export MEIVA_DB_CAPABILITY_KEY
.venv/bin/meiva init --serve-agent-runner --no-serve-gateway \
  --gateway-url http://<gateway-host>:8000 \
  --machine-name machine-2 --machine-host <this-host-addr> \
  --db-capability /path/to/machine-2.bundle
unset MEIVA_DB_CAPABILITY_KEY
.venv/bin/meiva start
```

Transfer only the bundle and its key through the operator's secret channel.
Never copy the gateway's `.env`: it contains credentials a runner must not
hold. A runner home whose `.env` still records `MEIVA_CLUSTER_SECRET` refuses to
start.

`meiva init` fetches `GET /api/bootstrap` with the bundle's API token
(configuration only: `MEIVA_DB_URL` is the credential-free endpoint and the human
secret is never served), rejects a remote gateway returning loopback storage
URLs, and installs the capability: the bundle must authenticate under
the key, name this machine and home and the endpoint the gateway serves, be
unexpired, carry a generation not older than an installed one, and log in. It
writes `$MEIVA_HOME/db-authority/unit.json` (0600) and deletes the bundle. A runner with no installed capability refuses. It records
the local identity; host convergence, registration, and root startup are the first `meiva start`'s. `--machine-host` must be reachable from the gateway: the gateway calls
the runner's `/ops` there with its gateway API token (the runner holds only
that token's digest). A remote join requires a non-loopback address and a
bundle issued by an authenticated gateway.

The bootstrap response is not cached into the runner `.env`. Every process
fetches current connection facts at Settings construction; gateway unavailability
fails startup. The gateway serves the runtime Redis ACL credential but no
database login; its schema owner never logs in and its Redis-admin password
never leaves the gateway. A later write generation reaches the runner only
through a new bundle; networked release operations refuse until that exchange
is automated. Install it on the stopped unit with `meiva cluster db-authority
install-unit <bundle>` (the transport key in `MEIVA_DB_CAPABILITY_KEY` again), then
`meiva start`: the same join as `meiva init --db-capability`, for an initialized home.

Optional `meiva init` inputs:

- `--ssl-cert-file PATH`: a trusted CA bundle for the gateway connection.
- `--config-file PATH`: initial Settings configuration outside the home,
  such as the declared transport encryption mode.

After `meiva init`, use bare `.venv/bin/meiva start` to start and later resume the same
unit. `meiva start` takes no identity input and `meiva init` refuses an initialized home;
neither is an identity-edit API.
The root owns application service lifetime. PTY sessions have separate custody.
