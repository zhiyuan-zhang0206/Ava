# Enabling the central fetch (`fetch_via_gateway`)

How to move a cluster onto the **central-fetch topology** — the gateway as the
cluster's single wall-crossing fetcher, every agent-runner fetching the cluster
source from the gateway — and how to roll it back. Read this before flipping
`settings.general.fetch_via_gateway`; `conventions/runbook.md` documents the
ordinary cluster-update path and
`cli/commands/rollout-boundary/rollout-boundary.ava.okf.md` the rollout phases.

## What the switch changes

- **Off (default):** every node fetches its own `origin`; Phase 0 asks each
  runner to confirm `git fetch origin` and nothing more.
- **On:** the gateway is the cluster's only wall-crossing fetcher. The
  gateway's own GitHub fetch runs in the rollout preflight, before Phase 0;
  every non-gateway participant's Phase-0 fetch must then come from the
  gateway. A runner whose `origin` still addresses a wall host (github.com)
  **refuses** in `ops.ops_cluster.cluster_fetch_op`, which aborts the rollout
  before anything is paused and names the refusing host. The refusal is the
  point: flipping the switch before a machine is rewired fails that rollout
  loudly instead of letting the machine fetch GitHub behind the topology's
  back. There is deliberately **no source fallback** — a runner that cannot
  reach the gateway does not fall back to GitHub.

## Preconditions (verify per machine, before flipping)

1. **The gateway source is reachable over ssh from every agent-runner.** Use
   the raw `user@host:port` form — never an ssh-config alias: an alias can be
   hijacked by a fake-ip resolver and fail in a way raw addressing does not.

   ```bash
   ssh -i <identity> -p <gateway-ssh-port> <gateway-user>@<gateway-host> true
   git ls-remote ssh://<gateway-user>@<gateway-host>:<gateway-ssh-port><gateway-source-path> \
     refs/heads/<track-branch> refs/remotes/origin/<track-branch>
   ```

   `ls-remote` must print the gateway checkout's fetched tip
   (`refs/remotes/origin/<track-branch>` — the same sha the gateway's own
   `git rev-parse origin/<track-branch>` shows). The gateway's
   `refs/heads/<branch>` only moves at its own pull, mid-rollout; the served
   tip for runners is its `refs/remotes/origin/<branch>`.

2. **Each runner's `origin` fetch URL points at the gateway; pushes still
   target GitHub.** Record the old values first (they are the rollback):

   ```bash
   cd <runner-source>
   git remote get-url origin          # record for rollback
   git remote set-url origin ssh://<gateway-user>@<gateway-host>:<gateway-ssh-port><gateway-source-path>
   git remote get-url --push origin   # must still be the push target — leave it alone
   git fetch origin                   # must succeed and land the gateway's tips
   ```

   `pushurl` stays as it was: the switch changes the fetch source only.

3. **The gateway itself keeps fetching GitHub** (its `origin` stays the wall
   route) — it is the one host the topology expects to cross the wall.

## What Phase 0 proves under the switch (timing)

The gateway's GitHub fetch runs in the rollout preflight, before Phase 0; the
pinned target is resolved from that fetch. A runner's Phase-0 fetch reaches the
gateway and imports its current branch tips (pre-pull — the gateway's branch
tip becomes the pin at its own pull in Phase A). The target's objects are
therefore carried by the runners' **update-time fetches** (`spawn_update`'s
validate fetch and the checkout's own `git fetch origin`), which run after the
gateway's pull. So under the switch, Phase 0 proves *reachability and
fetchability of the gateway source* on the same URL the later fetches use —
not, as in the direct topology, "the pin's objects are already on this host".
Both guarantees abort before any pause when they fail.

## Flip and verify

1. Set `fetch_via_gateway=true` (alias `AVA_FETCH_VIA_GATEWAY`) through the
   standard settings path for a cluster-pinned field; it is distributed to
   agent-runners via `/api/bootstrap`, and its `restart_required` is `all` —
   fold a restart/rollout into the change.
2. Run a rollout. Phase 0 prints the central-fetch line; every host must
   acknowledge. A host still on a wall source refuses with its origin named —
   fix that machine and re-run (nothing was paused).
3. To leave the topology: set the switch **off** first, then restore each
   machine's recorded `origin` URL (`git remote set-url origin <old>`).

## Failure semantics (treat unreachability as normal)

- Each Phase-0 fetch is bounded (fetch timeout) and the gateway retries a
  transient transport failure with bounded backoff; a host that keeps failing
  reads `unreachable` / failed in the per-host verdicts and aborts the rollout
  before pause, with the abort message naming the phase and host.
- Tailnet relays (DERP) can appear mid-flight and turn a LAN fetch into a slow
  one; the failure is a loud abort, not a silent reroute. Do not "fix" a
  refusing rollout by relaxing the check — fix the machine's route.
- No component self-dials for this flow (the gateway fetches GitHub; runners
  dial the gateway). When testing from the gateway itself, use loopback — a
  self-dial through the host's own tailnet IP can blackhole.
