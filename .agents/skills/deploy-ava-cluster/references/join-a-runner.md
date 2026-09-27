# Join a runner through first start

A runner joins an already-serving gateway. Its identity is its local home and
gateway URL; its database login, machine API token and telemetry token are a
per-unit capability the gateway operator issues. It never holds the gateway's
human cluster secret, and it creates no local cluster data plane.

On the gateway, issue the unit's capability bundle (0600) and note the transport
key it prints once:

```bash
ava cluster db-authority issue-unit --machine machine-2 \
  --home <the runner's absolute $AVA_HOME> --out machine-2.bundle
```

Carry the bundle to the runner and the key separately. Acquire dependencies in
the canonical source checkout as described in
[the deployment guide](../SKILL.md), then run:

```bash
printf 'Capability transport key: ' >&2
IFS= read -rs AVA_DB_CAPABILITY_KEY
printf '\n' >&2
export AVA_DB_CAPABILITY_KEY
.venv/bin/ava start --serve-agent-runner --no-serve-gateway \
  --gateway-url http://<gateway-host>:8000 \
  --machine-name machine-2 --machine-host <this-host-addr> \
  --db-capability /path/to/machine-2.bundle
unset AVA_DB_CAPABILITY_KEY
```

Transfer only the bundle and its key through the operator's secret channel.
Never copy the gateway's `.env`: it contains credentials a runner must not
hold. A runner home whose `.env` still records `AVA_CLUSTER_SECRET` refuses to
start.

First start fetches `GET /api/bootstrap` with the bundle's API token
(configuration only: `AVA_DB_URL` is the credential-free endpoint and the human
secret is never served), rejects a remote gateway returning loopback storage
URLs, and installs the capability: the bundle must authenticate under
the key, name this machine and home and the endpoint the gateway serves, be
unexpired, carry a generation not older than an installed one, and log in. It
writes `$AVA_HOME/db-authority/unit.json` and `enrollment.json` (0600) and
deletes the bundle. A runner with no installed capability refuses. It records
the local identity before host convergence, registration, and root startup. `--machine-host` must be reachable from the gateway: the gateway calls
the runner's `/ops` there with its gateway API token (the runner holds only
that token's digest). A remote join requires a non-loopback address and a
bundle issued by an authenticated gateway.

The bootstrap response is not cached into the runner `.env`. Every process
fetches current connection facts at Settings construction; gateway unavailability
fails startup. The gateway serves the runtime Redis ACL credential but no
database login; its schema owner never logs in and its Redis-admin password
never leaves the gateway. A later write generation reaches the runner only
through a new bundle; networked release operations refuse until that exchange
is automated.

Optional first-start inputs:

- `--ssl-cert-file PATH`: a trusted CA bundle for the gateway connection.
- `--health-port-base N`: a distinct daemon health-port block if multiple units
  share the host's loopback namespace. Choose an unused block on the grid in
  `shared/port_block.py`. WSL2 applies its reserved default when omitted.
- `--config-file PATH`: first-start Settings configuration outside the home,
  such as the declared transport encryption mode.

After successful first start, use bare `.venv/bin/ava start` to resume the same
unit. Conflicting identity inputs refuse; they are not an identity-edit API.
The root owns application service lifetime. PTY sessions have separate custody.
