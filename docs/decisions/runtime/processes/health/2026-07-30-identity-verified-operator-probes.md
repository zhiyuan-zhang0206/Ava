# Operator probes verify identity; the browser's identity is its profile, not CDP

## Context

Ava had two answers to "is this service up", and they disagreed.

The **watchdog** stopped believing a bare HTTP 200 after the 2026-07-24 outage,
where a pytest-leaked restarter daemon fell back to prod's default 8102 and
answered 200 for 98 minutes while prod's own restarter was dead. Since then
`shared.daemon_health.probe_daemon` believes a 200 only when the body's
`name`/`home`/`pid` identify this unit's own daemon, and since
`decisions/2026-07-29-identity-mismatch-is-terminal.md` a mismatch is a
terminal verdict rather than a respawn trigger.

The **operator surfaces** — `ava status`, `ava cluster health-probe`'s
per-service check, `ava start`'s readiness gate — still decided on a plain 2xx
(`cli/commands/_probe.py:_probe_service`). So on 2026-07-26, when both of the
`win` unit's monitored daemons were dead and its watchdog had correctly stopped
retrying, `ava status` would have read **green**: the WSL2 unit's daemons were
answering those ports through the localhost relay. The only signal was one ERROR
line among 402, inside a 53 MB watchdog log on a Windows box.

The weaker check was on the surface a human reads and the surface alerts fire
from. That is the wrong way round.

The **browser** was worse than weak: it had no identity check available at all.
`ops/spec.py` probed it by dialling Chrome's DevTools Protocol
(`/json/version`), which is not ours to extend, so any Chrome answering that port
read as this unit's healthy browser — including, on a WSL2 box, another unit's.
Agents would then drive the wrong browser, which is worse than a red check
because nothing looks wrong.

## Decision

**One field on the roster carries the identity check, and every consumer runs
it.** `ServiceSpec.identity_probe` is a `Callable[[], DaemonProbe]` set for every
service whose endpoint can prove who answered:

- the `/healthz` daemons — `probe_daemon` (name + home + pid);
- the gateway — a new `shared.daemon_health.probe_home` (home only; uvicorn's
  reload fork means a healthy gateway routinely answers with a pid its own
  pidfile never recorded). The gateway healthcheck's private copy of this logic
  was collapsed into it, so there is one definition;
- the browser — `services/browser/probe.py` (below).

`None` means the endpoint carries no identity — Next.js, milvus's gRPC, a
watchdog's pidfile — and the consumers **print which of the two a row got**
(`✓ (identity)` vs `✓ (http)`), so "liveness only" is a stated property rather
than something that looks like an oversight. A failing row carries the probe's
`detail`, and the health probe's owner alert carries it too: "answering, but its
home is `/home/ava/.ava`" is actionable where "not responding" is not.

The surfaces report `alive` only. **`terminal` stays the watchdog's business** —
"can a respawn win" is a question only the thing that would respawn needs.

The `detail` travels to **every** consumer, including `ops.observe` — its
`ServiceStatus` carries it beside the verdict rather than leaving the observation
layer to re-dial to learn why. A bare `alive=False` flattens "nothing is
listening" into "another cluster's gateway holds this port", and those are the
two cases this whole change exists to separate.

**Consumers must tolerate a probe that is not total.** The three built-in
wrappers convert every failure into a `DaemonProbe`, but `identity_probe` is a
roster field a plugin can set, and nothing enforces that a plugin's is a total
wrapper. `_probe_service` therefore catches around the call and reports the
exception's type and message as the row's detail. Fixed-string swallowing was
rejected for the usual reason: it trades a crash for a mystery, and "this probe
is broken" and "this daemon is down" need different actions.

**The browser is identified by its profile plus the listening socket.** A Chrome
whose argv carries this cluster's `--user-data-dir=$AVA_HOME/chrome-profile` (the
strictly positive token `services/browser/orphan.py` already established) **and**
which holds the LISTEN socket on the CDP port being dialled. Both halves are
load-bearing: the profile half rejects another unit's browser, and the socket
half rejects our own Chrome merely *existing* — on a box with two localhost
namespaces our Chrome can be alive with a dead DevTools endpoint while a relayed
one answers. Unreadable sockets are reported as unconfirmed (`PORT_TAKEN`, loud
and idle), never assumed healthy.

## Alternatives rejected

**Read the identity back through CDP.** This was the open question, and it is
answered empirically rather than by assumption: on Chrome 150, `/json/version`
returns `Browser`, `Protocol-Version`, `User-Agent`, `V8-Version`,
`WebKit-Version` and a `webSocketDebuggerUrl` whose uuid is minted per launch and
recorded nowhere we can read back. `DevToolsActivePort` — the file Chrome writes
into the user-data dir, which *would* have tied the endpoint to the profile — is
written only for an auto-assigned port; with an explicit
`--remote-debugging-port` it is absent (verified against this cluster's own
profile, where Chrome was running, and against a throwaway one).
`Browser.getBrowserCommandLine` exists but is experimental, needs a websocket
round trip and a dependency we do not have, and would still be answering with the
same profile token the process table gives for free.

**Give the browser daemon its own `/healthz`.** The strongest-looking option, and
the one the issue leaned toward: put the identity check where identity lives.
Rejected for now because on POSIX the daemon `os.execvp`s into Chrome — the session
pane's process *is* Chrome, which is what makes "kill the pane, kill the browser"
true and what `ava stop` depends on. Serving a `/healthz` requires keeping a
Python supervisor alive there, changing the teardown contract, the
`SingletonLock` handoff reasoning and the Windows/POSIX split all at once. That
is a browser-architecture change, not a probe change, and it should not ride
inside an observability fix. Left as a proposal.

**Per-unit CDP port.** Removes one *source* of collision; makes CDP no more
identity-verifiable than before. It belongs to the port-allocation design
question (issue #977), which is deliberately held.

**Let each consumer re-derive the identity check.** The bug being fixed *is* two
consumers deriving different answers for the same port. A field on the single
roster is what makes divergence impossible rather than merely discouraged.

**Have `ops` restate the browser's identity.** `ops/spec.py` otherwise imports
only `shared`. Restating either the profile path or the process-table
identification there would give this cluster's Chrome two definitions, so
`ops/spec.py` instead takes one lazy, documented import of `services.browser`
(itself a `shared`-only package) from a single function.

## Consequences

- Every operator surface now performs real identity work per probed service. That
  is one loopback HTTP call plus, for the browser, one process-table pass — the
  same cost the watchdog already pays every 60s.
- An occupant on a unit's port now reads ✗ on `ava status` and fails
  `ava cluster health-probe`'s check 5 (alert-only, so it still cannot trigger an
  auto-rollback). Anyone who was reading green through such a condition will start
  seeing red; that is the point.
- The browser healthcheck gained a terminal branch: another unit's Chrome on the
  port is reported at ERROR with `EXIT_PORT_TAKEN` and never respawned against,
  matching every other service's policy.
- The browser's identity check is **machine-local by construction** (it walks this
  host's process table), so it cannot be asked remotely. Every current caller runs
  beside the browser it asks about.
- `ServiceSpec` equality now spans callables, as it already did for `gate`.
