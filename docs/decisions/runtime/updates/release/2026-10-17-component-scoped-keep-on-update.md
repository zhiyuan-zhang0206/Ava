# Component-scoped keep on a source-mode update: restart only what the window touches

## Context

The networked cluster updates by full stop and start
([2026-09-30-networked-cluster-stays-on-source-updates](2026-09-30-networked-cluster-stays-on-source-updates.md)):
every unit runs `ava stop -y`, switches its checkout, starts again. Its recorded
consequence stands — every agent drains, schedules close, the data plane restarts —
and two of the stopped resources hold state that costs more than a bounce: the
pty-sessions service holds every live agent shell, and `ava stop` (unlike `restart`)
also takes the shared headed Chrome and its MCP upstream down.

Most windows touch a small slice of runtime code, and the stateful components sit on
narrow dependency surfaces. Measured on the 2026-10-17 checkout (`2c3b7c366e`): the
pty-sessions surface is 31 files; the browser surface is 152 (it reaches
`base/config` 36, `base/telemetry` 24, `base/host` 24) — broad, but still far from the
7 200-file checkout. A window that changes neither should not clear either.

`ava stop` already carries its own keep set: repeatable `--keep-service NAME`
(`ava pause` was deleted for exactly this shape,
[2026-10-03-delete-ava-pause](../../../agents/graph/2026-10-03-delete-ava-pause.md)).
The three names this decision needs — `pty-sessions`, `browser`, `browser-mcp` — are
`requires_db=False`, so no `--keep-infra` coupling, and every host's roster carries
their specs unconditionally, so the flag names are valid on browserless hosts too.

User ruling, 2026-10-17 (direct): restart per component by change surface — unchanged
keeps (live shells stay mounted, the Chrome window stays up), changed or undecidable
restarts as before (the conservative side); the surface must come from dependency-graph
generation, not a hand-kept list. A window that does reach the pty surface still closes
its sessions with the owners' busy notice and no grace period.

## Decision

1. `cli.fleet_update down` runs a keep decision before anything stops.
   `scripts/host_ops/fleet_update_keep.py`, run from the operator's development
   checkout on the interpreter that runs `down` and against the same checkout being
   switched, checks both revisions out into scratch worktrees, takes each component's
   file closure from `meizel.contracts.analysis` facts at both revisions, and restarts
   a component when any changed path of `OLD..NEW` lies in the union of its closures
   (a file deleted in NEW counts: the old revision's closure has it). The verdicts ride
   every stop as `--keep-service` flags.
2. The component table lives in one place (the script's `COMPONENTS`):
   - **pty-sessions** — dirs `services/agent_runner/pty_sessions`, `base/sessions/pty`;
     watch `services/supervision/healthchecks/protocol_probe.py`; keeps
     `pty-sessions`.
   - **browser** — dirs `services/desktop/browser`, `ava_builtins/mcps/chrome`; watch
     `ava/mcps/_browser.py`, `services/supervision/healthchecks/browser.py`,
     `services/supervision/healthchecks/browser_mcp.py`; keeps `browser` and
     `browser-mcp` together (the browser phase watches `browser` in the keep set, and
     `browser-mcp` is a service of its own).
   - Dirs expand to a closure; tests and docs under them are not roots; watch entries
     are files, never expanded. Every other component (gateway, data plane, agent
     host, schedules) restarts on every window, exactly as before.
3. Fail closed everywhere. A missing script (a checkout older than this change), a
   timeout, a non-zero exit, unusable JSON, a revision that does not resolve — the
   decision returns no keeps, prints a WARNING with the cause, and the stop restarts
   everything: the pre-decision behavior, never a guess. `--restart-all` skips the
   decision on purpose. A component whose dirs match no tracked file at either
   revision restarts (the table is out of date).
4. Hosts already on NEW have no window to judge: `down` passes NEW as both revisions,
   the empty diff keeps every component — the rerun of an interrupted `down` keeps.
5. The decision tool lives under `scripts/host_ops/` and `down` runs it as a
   subprocess: `cli` may not import `meizel` (locality roles). `meizel sync --declare`
   exported `meizel.contracts` and made the script an export of `scripts`/`scripts.host_ops`
   for the same edge — the checker's own mechanical route for a new consumer.

## Alternatives rejected

- **A hand-kept file list per component.** The user rejected it in the same ruling: a
  list drifts from the tree silently, and the facts it would imitate are exactly what
  the locality gate already derives. The surface is generated from that analysis.
- **Glob heuristics** (only paths under the component's own dirs restart it). The
  measured browser surface reaches `base/config`, `base/events` and `base/telemetry`;
  a window changing the events declarations or agent prompts would run the old browser
  code against changed shared code. The closure is what keeps such a window from a
  silent keep.
- **`restart` semantics as the update's stop.** A restart keeps Chrome but closes
  every terminal; the update needs the per-component choice, not one preset.
  `--keep-service` already gives the stop the whole matrix.
- **Keep-everything-always, or flags in the operator's hands.** A window that changes
  `base/sessions/pty` would keep old pty-sessions code against new callers on the same
  host — the conservative half of the ruling exists for this. Manual per-update flags
  would make every attended window a judgment call.
- **Deciding inside `ava stop` on each host.** At decision time hosts run OLD code and
  cannot judge a window whose NEW code they do not have yet. The operator's checkout
  holds both revisions and the checker.
- **Routing the analysis through a new `meizel/__init__.py` door.** A plain re-export
  makes every `import meizel.*` (every lint script, every hook) load the whole checker
  engine; the checker's `sync --declare` names the package's exports as the mechanical
  fix, so `meizel.contracts` was exported instead.

## Consequences

- Unrelated windows stop clearing live shells and taking the shared Chrome down. On
  the 2026-10-17 window (`8c7a1e0813..2c3b7c366e`, 271 changed files) the decision was
  pty-sessions keep / browser restart (2 hits: `base/config/domains/agent/prompt.py`,
  `base/events/declarations/maintenance.py`).
- The browser surface is broad today, so browser keeps will be the minority until the
  surface is narrowed. The `COMPONENTS` table and the reach of the base layers it
  pulls are the tuning levers; nothing else changes the judgment.
- The decision costs two scratch worktrees and two whole-repo analyses on the operator
  checkout (about 10-20 s measured); an all-on-NEW rerun short-circuits to
  keep-everything without any analysis.
- The behavior activates at the next `down` run from a checkout that contains this
  code. Hosts need no update first: `--keep-service` and these names exist in the
  current hosts' `ava stop`.
- Docs follow the behavior: the runbook's update section (with the stale "restart
  keeps persistent PTYs" line corrected), `graceful-maintenance.md` (the writer-barrier
  caveat for a kept pty-sessions), `services/desktop/browser/docs/operations.md`
  (browser lifetime), and forward links on the two decisions this one amends.
