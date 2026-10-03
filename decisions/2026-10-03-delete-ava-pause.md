# `ava pause` is deleted; the stop's own keep set replaces it

## Context

`ava pause` was the normal drain followed by a service stop that kept the data plane (PostgreSQL,
Redis, PgBouncer), the browser, the permissions helper and the persistent shells. `ava stop -y
--keep-infra --keep-service ...` runs the same kernel (`_do_stop` into `_temporary_stop.stop`);
the two differed only in which resources the call retained and in whether it asked for
confirmation. The verb had its own parser, handler, command function, journal operation label,
help text and tests for that difference alone.

Its one production consumer, `ava restart` (smooth), never went through the verb: it called the
stop kernel directly with the retained set. Nothing else in the tree invoked `ava pause`: the
fleet update stops with `ava stop -y`, the update ladder restarts with `ava restart`, and no
watchdog, self-update or maintenance path spelled it. The verb was a second operator-facing name
for a stop, and its name collided with `ava cluster pause` (machine membership), the `paused`
posture, the maintenance "pause" journal and `ava.self.pause_heartbeat`, each a different thing.

## Decision

1. Delete the verb: its parser and handler, `cmd_pause`, the `pause` journal operation label, and
   the shared option helper that only existed so two parsers could carry the same flags. No alias,
   no deprecation shim; `ava pause` is an invalid choice.
2. A retained set is the stop's own parameters. Services and the browser are `--keep-service NAME`
   (repeatable), the data plane is `--keep-infra`, and persistent shells are the stop's `terminals`
   phase, which a call skips with `reap_agents=False` (`ava maintenance stop --keep-terminals` for
   the explicit steps). `ava restart` is the caller that spells the full set, now at its call site:
   `keep_infra`, `keep_browser`, `reap_agents=False` and `teardown_extras=False`, so a smooth
   restart replaces the application services and leaves the data plane, browser, helper and shells
   alone. A test drives the real stop kernel through `cmd_restart` and fails when any one of the
   four flips.
3. The word "pause" stays where it names something else: the maintenance hold and its journal
   (`pause_owner`, `ops.agent_pause`, `_pause_resume`), the `paused` posture, machine pause, and
   heartbeat pause. Only the verb and its prose are removed.

## Alternatives rejected

- **Keep `ava pause` as an alias of a preset stop.** A second spelling that hides the keep set
  behind a name, and the name is already overloaded. An alias is also the thing the deletion is
  meant to remove.
- **Give `ava stop` a `--keep-terminals` (and an extras flag) now.** The per-session shell host has
  no roster name to retain, so the flag would be a new mechanism for a model that is about to be
  replaced by a roster service, after which `--keep-service` expresses shells like any other
  service. Until then the shell-retaining stop is `ava restart` or the explicit maintenance steps.
- **Rename the hold internals along with the verb.** `pause_owner`, `ops.agent_pause` and the
  `paused` posture are shared by stop, restart and update. They name the hold, not the verb, and a
  rename touches the journal file name and the schema vocabulary for no behavioural change.

## Consequences

- There is no one-command "stop the services but keep the shells and the helper" for an operator:
  `ava stop` closes terminals and retires the helper (`ava start` recreates it); only `ava restart`
  and `ava maintenance stop --keep-terminals` leave shells alone. This is accepted until shells are
  a roster service.
- A script that ran `ava pause` fails at argument parsing. None exists in the repository.

Forward link (2026-10-03): `ava maintenance stop --keep-terminals` no longer exists; see
[the manual maintenance verbs deletion](2026-10-03-delete-manual-maintenance-verbs.md).
