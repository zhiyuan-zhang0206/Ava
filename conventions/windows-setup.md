# Windows setup

Run Ava inside WSL2 using the Linux lifecycle. Native Windows application
startup is unavailable until a native root adapter exists: `ava start` refuses
instead of selecting the retired service-session launcher. Native process and
terminal utilities remaining in the repository do not establish a supported
Windows cluster startup path. Native Windows runner support remains a required
target; its root transport and Job custody are unfinished implementation, not
an approved platform removal. WSL2 is the currently available Linux path.

A WSL2 distro can run a gateway, a runner, or both. Follow the ordinary
[deployment procedure](../.agents/skills/deploy-ava-cluster/SKILL.md) inside the
distro. For unattended operation, use the explicit distribution anchor and
Linux boot owner in [WSL gateway boot](wsl-gateway-boot.md).

## Prepare the Linux environment

Clone into the distro's Linux filesystem, such as `~/.ava/source`, rather than
`/mnt/c/`. Install uv with `scripts/provision/toolchain.sh`, which fetches the
pinned release and verifies its checksum; never use the rolling astral installer.
Acquire Git, Python 3.12, and the repository's frontend Node requirements. A gateway using local storage also needs Postgres 17 with
pgvector, Redis 8.2, and PgBouncer. Run native Postgres under a non-root Linux
user. Dependency acquisition does not create a cluster or start services.

For a single box, run from the canonical source checkout:

```bash
.venv/bin/ava start --serve-gateway --serve-agent-runner --machine-name wsl-1
```

For a runner joining another gateway, use the
[runner first-start procedure](../.agents/skills/deploy-ava-cluster/references/join-a-runner.md).
Provide the bearer through the environment, and declare a reachable
`--machine-host`. Model-provider credentials remain private to the Linux home.

## Network identity and ports

WSL2 needs its own private-network identity. Install and join the VPN or overlay
inside the distro when the deployment requires it; the Windows host's address
is not automatically a reachable address for the Linux unit. See
[dev setup](dev-setup.md#wsl2-needs-its-own-private-network-identity).

Windows loopback forwarding and mirrored networking can make otherwise separate
units share a port collision domain. A pure WSL2 runner applies its reserved
health-port base when no explicit value exists. Two WSL2 distros may still need
different `--health-port-base` values. Select an unused block on the grid in
`shared/port_block.py`; check actual host listeners and local reservations.
Root readiness refuses a listener belonging to another home.

The first start binds the selected ports and identity durably. Bare repeated
start retains them. A new flag is not a way to rewrite an existing unit's
identity or move a live listener.

## Boot ownership

Windows owns starting and retaining the selected WSL distribution. Linux owns
starting Ava and supervising its root. A systemd boot unit uses the ordinary
start readiness result and adopts the captured root as its main process. No
macOS permissions helper runs on Linux. Interactive Windows logon tasks and
legacy watchdog probes are not substitutes for this lifecycle.

A sleeping or stopped WSL distribution cannot execute agent work. Verify both
the distribution boot trigger and Linux root readiness after a reboot; a
registered task by itself is not evidence that Ava is serving.
