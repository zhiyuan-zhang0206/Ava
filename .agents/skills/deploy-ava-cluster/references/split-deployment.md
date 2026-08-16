## Split across machines

Give each capability its own machine when you want the gateway and the
compute isolated (e.g. a small always-on gateway + beefier runners).

The data-plane posture is uniform — the default is multi-machine, a single box is
just the case where the reachable address is loopback (no single-vs-multi branch).
The agent-facing machine surface (`spawn(machine=)`, `list_machines`, the roster)
is always present — a single box simply sees one machine.

**Auth and reachability move together.** `AVA_CLUSTER_SECRET` is what makes the
data plane reachable off-box at all:

- **No secret** (the single-box default) → pg/redis bind **loopback only**,
  whatever `AVA_MACHINE_HOST` says. An unauthenticated data plane is never
  exposed to the LAN.
- **Secret set** → pg/redis bind loopback **plus** this host's reachable address
  (`AVA_MACHINE_HOST`), de-duplicated — never all interfaces, so only hosts that
  can route to that address see them at all, and they still need the password.

A split deployment therefore always has a secret: `install.sh --role gateway`
mints one for you, and every `ava enroll` requires it.

**On the gateway host** (owns Postgres/Redis + the HTTP gateway):

```bash
mkdir -p ~/.ava
git clone https://github.com/zhiyuan-zhang0206/Ava.git ~/.ava/source
cd ~/.ava/source
./scripts/install.sh --role gateway
# The birth step already wrote ~/.ava/.env (derived urls + a freshly minted
# AVA_CLUSTER_SECRET + the serve flags). EDIT it — do not copy .env.example over it.
# The gateway must advertise an address the runners reach it at (the operator
# declares it — Ava assumes the machines can reach each other, not HOW).
# AVA_MACHINE_HOST defaults to localhost; setting it to this node's real address
# adds that address to the native pg/redis/PgBouncer binds (alongside loopback)
# and lets /api/bootstrap project reachable URL hosts to runners. Keep the born
# AVA_DB_URL and AVA_REDIS_URL unchanged: they remain the gateway's loopback
# self-dial URLs. AVA_TRUSTED_CIDRS is the
# private-network range the runners connect from (the overlay's address block);
# pg_hba requires scram-sha-256 from it:
#   AVA_MACHINE_HOST=<this host's reachable address>
#   AVA_TRUSTED_CIDRS=<runner source range, e.g. the overlay's CGNAT block>
ava start --machine-name machine-1 --serve-gateway \
          --gateway-url http://<reachable-address>:8000
```

With a cluster secret and `AVA_MACHINE_HOST` set to this node's address,
`ava start` binds the native Postgres + Redis + PgBouncer to loopback + that
address, de-duplicated — never a wildcard. Reachability is limited to loopback
and networks that can route to the chosen address; remote clients still need the
credential. pg_hba trusts the local unix socket but requires `scram-sha-256` from the
`AVA_TRUSTED_CIDRS` ranges. The gateway's main Postgres identity and its Redis
ACL user authenticate with `AVA_CLUSTER_SECRET`; the single-tenant Redis
`requirepass` is that same secret. A runner does **not** receive the main
Postgres credential: `/api/bootstrap?role=runner` rewrites `AVA_DB_URL` to the
least-privilege `ava_runner` identity with its independent
`AVA_RUNNER_DB_PASSWORD`, freshly read gateway-side and carried only inside the
served URL. The runner's Redis URL still uses the cluster secret. Every remote
connection therefore needs private-network reachability plus the credential
for its role.

On the gateway, the born URLs stay loopback self-dial URLs. When serving a
runner bootstrap, `shared/config/service_read.py` rewrites their loopback hosts
to `AVA_MACHINE_HOST`; it also projects the independent runner Postgres
credential. Operators never hand-edit the URLs or synchronize their passwords.

Config re-application on `ava start` differs between the two engines:

- **Postgres** — `pg_hba.conf` is rewritten and reloaded, and
  `listen_addresses` re-applied, on every `ava start`, so a changed
  `AVA_MACHINE_HOST` converges the hba immediately. A new *bind* still needs the
  server to restart (`ava stop` then `ava start` brings the pg_ctl-managed
  instance back up under the new config).
- **Redis** — the conf is rewritten only when Ava actually brings a redis up. A
  redis already running is left alone (only its ACL user is re-affirmed), so to
  change redis's bind you must stop it and let the next `ava start` launch a
  fresh one.

> **Ava owns this Postgres.** Every cluster runs its own Postgres instance that
> Ava `initdb`s under `$AVA_HOME`, so it provisions the per-cluster role +
> database over that instance's own initdb superuser on its private unix socket
> (loopback `trust`). There is no external/managed-Postgres knob — the data
> plane is always Ava-owned and per-cluster.

> **macOS — allow the serving binaries through the Application Firewall.** Binding
> the overlay interface is necessary but not sufficient on macOS: with the Application
> Firewall on (System Settings → Network → Firewall), incoming connections to a binary
> macOS does not recognise are dropped, so a remote agent-runner (or even this host
> dialing its *own* private-network IP) times out, while `127.0.0.1` still works —
> loopback is never filtered. Three binaries serve off-box ports and none is
> Developer-ID signed (the uv interpreter is `adhoc, linker-signed`, so the
> "automatically allow downloaded signed software" default does not cover it):
>
> | Binary | Serves | Present on |
> |---|---|---|
> | the venv's python (`readlink -f .venv/bin/python`) | the gateway's HTTP port; an agent-runner's ops port | both capabilities |
> | `postgres` (the vendored `~/.ava/runtime/pg/<ver>/bin`, else the brew keg) | the cluster's pg port | gateway |
> | `redis-server` (`$(brew --prefix redis)/bin`, a symlink onto `Cellar/redis/<ver>`) | the cluster's redis port | gateway |
>
> **Do not hand-transcribe these paths — ask for them.** `ava converge` (run
> automatically by `ava start`) audits this host, repairs what it can through a
> passwordless `sudo -n`, and prints the exact `--add` / `--unblockapp` pair for
> anything left, which matters because all three paths are version-stamped:
>
> ```bash
> ava converge          # audits + repairs where it has a grant, else prints the commands
> ava firewall status   # read-only: audit the host and diff against the allowlist manifest
> ```
>
> Whatever route applies the rule, **restart the affected service** — an
> already-bound socket keeps the policy it was accepted under, so the rule alone
> changes nothing until the process re-binds.
>
> **This recurs, by design of the Application Firewall.** The rule is stored against
> the path it was added with, and a `uv python` bump (or a vendored-Postgres version
> bump) moves the binary to a *new* version-stamped path that has never been heard of —
> the old rule is not invalidated, it is orphaned. Symptom: the gateway answers
> `127.0.0.1` perfectly and every runner reports it unreachable. `ava cluster update`
> names this `OFF_BOX_UNREACHABLE` and reprints the repair. Converge cannot always
> apply it itself — `socketfilterfw` refuses a non-root caller, and a password-prompting
> `sudo` inside converge would hang an unattended `ava start`, so the repair is
> attempted with `sudo -n` and degrades to printing the commands.
>
> A **single-box** macOS gateway with no remote runners needs none of this: with
> no cluster secret the data plane binds loopback only, and loopback is never
> filtered.

**On each agent-runner host** (no local data plane — points at the gateway):

```bash
mkdir -p ~/.ava
git clone https://github.com/zhiyuan-zhang0206/Ava.git ~/.ava/source
cd ~/.ava/source
./scripts/install.sh --role agent-runner
# enroll against the gateway (writes ~/.ava/.env: gateway URL, machine name,
# role, cluster secret — the db/redis URLs are fetched from /api/bootstrap,
# never cached). Read/export the bearer secret without echoing it or putting it
# in shell history/argv:
printf 'Cluster secret: ' >&2
IFS= read -rs AVA_CLUSTER_SECRET
printf '\n' >&2
export AVA_CLUSTER_SECRET
ava enroll --gateway <gateway-url> --machine-name machine-2 \
           --machine-host <this-host-addr>
unset AVA_CLUSTER_SECRET
ava start
```

The gateway dials each runner's ops server (`http://<runner-host>:<ops_port>`)
over the cluster's private network, so an agent-runner host must also declare its
reachable address (`--machine-host` — required; Ava does not auto-detect it, so
it makes no assumption about the network). Co-located gateway + runner units on
one machine are also possible (the gateway home at `~/.ava_gateway`, the runner
at `~/.ava`), but a single `gateway,agent-runner` unit is simpler.

Full enroll detail — what the bootstrap bundle carries, the optional flags, and
why startup order matters — is in [`enroll-a-runner.md`](enroll-a-runner.md).
