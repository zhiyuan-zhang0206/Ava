# Join a runner through first start

A runner joins an already-serving gateway. Its identity is its local home,
gateway URL, and cluster bearer. It creates no local cluster data plane.

Acquire dependencies in the canonical source checkout as described in
[the deployment guide](../SKILL.md), then run:

```bash
printf 'Cluster secret: ' >&2
IFS= read -rs AVA_CLUSTER_SECRET
printf '\n' >&2
export AVA_CLUSTER_SECRET
.venv/bin/ava start --serve-agent-runner --no-serve-gateway \
  --gateway-url http://<gateway-host>:8000 \
  --machine-name machine-2 --machine-host <this-host-addr>
unset AVA_CLUSTER_SECRET
```

Transfer only the bearer through the operator's secret channel. Never copy the
gateway's `.env`: it contains owner credentials a runner must not hold.

First start fetches `GET /api/bootstrap?role=runner`, requires a runner-class
DB login (the active write generation's `ava_g<n>_runner`, or a remote-managed
plane's `ava_runner`), and rejects a remote gateway returning loopback storage
URLs. It
records the local identity before host convergence, registration, and root
startup. `--machine-host` must be reachable from the gateway: the gateway calls
the runner's `/ops` there with the cluster bearer. A remote join requires a
non-loopback address and nonempty bearer.

The bootstrap response is not cached into the runner `.env`. Every process
fetches current connection facts at Settings construction; gateway unavailability
fails startup. The gateway projects its active runner login and the runtime
Redis ACL credential; its schema owner never logs in and its Redis-admin
password never leaves the gateway.

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
