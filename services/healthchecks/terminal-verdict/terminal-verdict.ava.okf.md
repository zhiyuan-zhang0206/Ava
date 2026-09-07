---
type: doc
title: Terminal verdict — a port another unit holds is not respawnable
description: The three-way probe verdict (alive / down / port-taken), where the line between "a respawn fixes this" and "no respawn can" is drawn and why, what observes the terminal state, and why it needs no stored state to self-clear.
tags:
- ops
---

# Terminal verdict — a port another unit holds is not respawnable

## Three verdicts, not a boolean

`shared.daemon_health.ProbeVerdict` — `ALIVE` / `DOWN` / `PORT_TAKEN`. `DaemonProbe.alive` and `DaemonProbe.terminal` are **derived** from it, so "is it serving?" and "can I fix it?" cannot drift apart.

`DOWN` and `PORT_TAKEN` are both "not alive", and the difference is not cosmetic: one is a condition a respawn cures, the other is a condition a respawn cannot touch. Callers branch on the verdict, never on the wording of `detail` — a `startswith("identity mismatch")` read would be a second definition of the same fact in every caller that wanted it.

## Where the line is: can this unit's own kill-session free the port?

A respawn kills `ava-<svc>` on **this unit's** session backend first, then re-spawns it. The session records live under `$AVA_HOME/run/sessions` (`shared.session_backend`), so the reach is scoped to the UNIT, not the cluster: two agent-runners of one cluster sharing a machine's localhost namespace are as unkillable to each other as two clusters would be. That is why the `home` row below says *unit* — reading it as *cluster* is what sent the 2026-07-26 diagnosis after port allocation, which was never the mechanism ([[cli/commands/start-readiness/start-readiness.ava.okf.md]] for the pre-bind gate that now stops it).

| probe result | verdict | why |
|---|---|---|
| `home` mismatch | `PORT_TAKEN` | another unit's daemon, on its own home — unkillable from here |
| `name` mismatch | `PORT_TAKEN` | a different daemon kind; killing `ava-<svc>` frees nothing |
| body is not an Ava `/healthz` | `PORT_TAKEN` | not a process this unit supervises at all |
| `pid` not in our pidfile | `DOWN` | reached only after name+home matched — a stray of *this* unit, which kill-session does clear |
| no pidfile, but answering | `DOWN` | same: same home, same kind |
| unreachable / 503 / non-2xx | `DOWN` | free port, or our own daemon with a wedged loop — respawn is the cure |
| probe raised unexpectedly | `DOWN` | fail closed *toward retrying*; an unforeseen failure is not evidence of an occupant |
| CDP answers, no Chrome of ours listens | `PORT_TAKEN` | the browser's version of `home` mismatch — another unit's Chrome, and `services/browser/daemon.py` refuses to launch while the port is served |
| CDP answers, our Chrome's sockets unreadable | `PORT_TAKEN` | cannot confirm ownership; CDP *is* served, so a respawn meets that same refusal whoever owns it |

The browser reaches those two rows without a `/healthz`: CDP carries no field we control, so its identity is a Chrome on this unit's `--user-data-dir` holding the LISTEN socket on the CDP port (`services/browser/probe.py`). The probe asks in BOTH directions — the profile walk asks each of our Chromes whether it listens, and a global-TCP-table read asks who owns the LISTEN socket and whether THAT pid is ours (by the walk, or by its own argv). Either direction may win; the second exists because the walk direction alone has blind spots (an unreadable argv, a failed socket read) that would misread our own listener as a foreign occupant.

## What "stops retrying" means

`shared.service_respawn.run_keepalive` is the one place the policy lives — the shared body of every daemon healthcheck's `main()`. On a terminal verdict it logs at **ERROR** naming the occupying home, then exits `EXIT_PORT_TAKEN` (3) — the one exit code `run_keepalive` still raises. The watchdog's `_run_check` catches `SystemExit` and logs the code, so the distinction lands in that capability's watchdog log.

**A failed respawn is no longer an exit** (task #1941): a respawn that does not verify alive schedules an exponential backoff (`base * 2^n`, base = the 60s watchdog round, capped at 30min by `watchdog_respawn_backoff_cap_seconds`) and the round returns — `EXIT_RESPAWN_FAILED` remains the browser healthcheck's own code, which does not use `run_keepalive`; the per-process state makes later rounds probe but skip the respawn until the window elapses, so a condition a respawn cannot cure stops being hammered. Once `watchdog_respawn_breaker_rounds` (default 5) consecutive rounds go by without a probe-alive verdict, the breaker opens: respawns hold, each round WARNINGs the hold age, and ONE `respawn_breaker_open` event fires through the unified events pipeline. Any probe-alive round resets everything.

It is **loud-and-stop, never silent-no-op**: a healthcheck that quietly declines to heal is the shape of the 98-minute outage ([[services/healthchecks/probe-contract/probe-contract.ava.okf.md]]).

**Loud once, not loud forever** (the 2026-08-13 browser-storm fix, ~1.8k ERROR lines/day across machine-1 + win). The browser healthcheck episode-gates its own ERROR (first round, condition change, one 6h reminder; DEBUG otherwise), and the watchdog de-duplicates its failure line per `(check, exit code)`. The episode record gates ONLY reporting — never a reap or a respawn, exit codes unchanged. Full contract: [[services/healthchecks/terminal-verdict/episode-gated-reporting.ava.okf.md]].

`respawn_and_verify` also ends its poll the moment a probe returns terminal, instead of spending the 20s deadline waiting for a process that will not yield.

## No state, so nothing to clear

The VERDICT persists nothing. When the occupant leaves, the next round's probe simply reads `DOWN` and the normal respawn runs. (The browser healthcheck keeps a reporting-only episode record for its error de-noising — see above — which gates no verdict, no reap and no respawn, and is deleted by the first healthy round.)

## The operator surfaces ask the same question

`ava status`, `ava cluster health-probe`'s per-service check, `ava start`'s readiness gate and `ops.observe` all run `cli/commands/_probe.py:_probe_service`, which takes its verdict from `ServiceSpec.identity_probe` — the same `probe_daemon` / `probe_home` / `probe_browser` the watchdog calls. They report `alive` only; **`terminal` stays the watchdog's business**, because "can a respawn win" is a question only the thing that would respawn needs to answer, while a human reading a roster needs to know the port is not serving them and why.

They do surface the `detail`, which is the actionable half: `✗ (identity) -- identity mismatch on http://localhost:8106/healthz: home='/home/ava/.ava' != …` names the occupant, and the health probe's owner alert carries the same string. Rows whose endpoint carries no identity at all (Next.js, milvus gRPC, a watchdog's pidfile) print their weaker label instead, so a `✓ (http)` is never mistaken for a `✓ (identity)`.

The health probe is still gateway-only, so an agent-runner's only observer remains its watchdog log ([[services/watchdog/watchdog.ava.okf.md]]).

## Key Dependencies
- [[services/healthchecks/probe-contract/probe-contract.ava.okf.md]] — the rule this sits inside: a probe always returns a verdict
- [[services/watchdog/watchdog.ava.okf.md]] — the caller that turns the exit code into a log line
