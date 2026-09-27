# Terminate has no closed state; it can take the agent's shell sessions with it

## Context

Task #3911 (PR #2823) gave termination a second, stronger form: `terminate
--final` / `kill --final` / `ava.agents.terminate(final=True)` stamped
`agents_meta.closed_at`, and every automatic resurrection path — delivery
chat, compact, the delivery watchdog's terminated-owner retry, hosted-turn
recovery, the billing batch — refused a closed agent, while the claim node's
newer-work veto stopped protecting its death. Only an explicit manual
resurrect reopened it (audited as `reopened` / `agent_reopened`).

The user ruled on 2026-09-27: "Terminate is terminate." A terminated agent is
one thing. The motivating case for closure was an agent being woken again by
its own sessions, chiefly watchers. A watcher is just a shell session (a
same-day ruling, decisions/2026-09-27-watchers-are-never-restarted.md: it is
never restarted and has no database record), and its wake is an ordinary chat
that resurrects a terminated owner like any other message. The way to stop an agent's own sessions from waking
it is to end those sessions, not to add a lifecycle state that every
resurrection path must consult.

## Decision

- **No closed state.** The `final` request field, the `closed` result field,
  the closure gates (resurrect endpoint, the home runner's final CAS, the
  delivery watchdog selector, the claim veto exemption, the billing whitelist
  clause and its `closed` refusal), the explicit resurrect's `reopened`
  payload and the `agent_reopened` event are removed. A terminated agent keeps
  today's ordinary behavior: any new inbound message may resurrect it,
  subject only to the gates that remain (wake suppression, the recovery
  breaker, the system-notice rule, the force-terminate fence, the billing
  whitelist's streak / reason / termination-source checks).
- **`kill_all_shell_sessions` instead.** `ava agents terminate|kill
  --kill-all-shell-sessions`, `ava.agents.terminate(...,
  kill_all_shell_sessions=True)`, and the `kill_all_shell_sessions` field of
  `POST /api/agents/{id}/terminate` kill every shell session the agent owns on
  its home machine — explicit shells and watchers alike. The enumeration is
  the existing `ops.cluster_status.agent_shell_sessions` rule
  (`…-agent-<id>-shell-<sid>[-<name>]`), so another agent's sessions and the
  agent's own process are never touched. `ava.ui.serve` page servers also run
  in agent shells, labeled `page-<slug>`; the kill spares them (a page keeps
  its own lifecycle). The label is one grammar with one owner,
  `shared.sessions.page_session`: the page-server daemon builds the names
  with it, and `ava.shell.sessions` now refuses the `page-` label for ordinary
  shells so the label names page sessions exactly. It is an owner-level kill:
  no per-session notice. Without the option, sessions are left alone as
  before.
- **Timing.**
  - A graceful terminate of a live agent writes the request into its
    terminate command's payload, in the same statement as the command, after
    locking the agent row. The home runtime's apply
    (`agent.hosted_ownership.apply_hosted_lifecycle`) kills the sessions
    under that same row lock, after the agent's last step returned and before
    the `terminated` write commits. A crash anywhere in between leaves either
    no committed death (the retry kills again — the kill is idempotent) or a
    death whose sessions are already gone; there is no committed death with
    the requested sessions still alive. Any unapplied terminate of the
    current life carrying the request counts, so the kill also rides a death
    that a different terminate (e.g. the agent's own) won acceptance for.
  - A force terminate kills the sessions right after its fence commits. A
    step still draining at that moment could create a new shell or watcher
    afterwards, so the request is also recorded on the force command, and
    the host sweeps again when it observes the force quiescent
    (`shared.hosted_force.original_host_force`, or the boot recovery of an
    orphaned force), before it records that observation. A force supersedes
    an unapplied graceful terminate together with that terminate's kill
    request: a force kills only when asked itself (the delivery watchdog's
    wedged-turn recovery forces and then resurrects the agent, which must not
    cost it its sessions).
  - A terminate that finds the agent already terminated (including one that
    becomes terminated while the request waits for the row lock) kills the
    sessions at once — the analogue of the removed already-terminated form of
    `--final`.
- **Placement.** Sessions are host-local, so every kill runs on the agent's
  home machine: the terminate op already executes on its ops server
  (`_forward_to_home_machine`), and the graceful kill runs inside that
  machine's agent host.
- **Result.** The response's `shell_sessions` is `{"when": "now", "killed":
  [ids]}` (empty list = there were none) or `{"when": "at_exit", "killed":
  []}`; `null` when no kill was requested — or, for a requested one, when the
  home runner predates the option (the CLI says so explicitly).
- **TTL rows.** For a `now` kill the gateway deletes the killed sessions'
  `agent_shell_ttls` rows right away, as the TTL reaper does after its own
  kills (the runner role holds no DELETE on that table). Rows of sessions an
  `at_exit` kill removed are retired by the TTL reaper at their deadline with
  its silent `absent` verdict.
- **Failures.** A failed synchronous kill fails the request after the
  termination is already durable; repeating the command retries the kill. A
  failed at-exit kill is logged at ERROR and the termination still applies —
  a session-kill failure must not turn a death into a crashed turn.

## Alternatives rejected

- **Keep `--final`.** Two terminated states that every resurrection path, the
  claim veto, the watchdog, the billing batch and the audit trail must keep
  telling apart, for a problem that is really about the agent's own sessions.
  It also made queued work to a closed agent silently wait for a dead-letter
  gate, which is the opposite of "a message wakes a terminated agent".
- **Make every terminate stop all automatic resurrection.** It would break
  the ordinary contract the ruling keeps: a user or peer message to a
  terminated agent must bring it back.
- **Kill the sessions at acceptance time for a graceful terminate.** It would
  pull the sessions out from under the agent's last step.
- **Kill the sessions after the termination commits (host or gateway
  sweep).** A crash between the commit and the kill, or the sweep interval,
  leaves a terminated agent with live watchers that can wake it — exactly
  what the option exists to prevent.

## Consequences

- The `agents_meta.closed_at` column (and its schema comment) stays in this
  change: code simply stops reading and writing it, per the expand-contract
  rule (`shared/migrations/reset-generations.ava.okf.md`). Its DROP is a
  separate later migration, after this code is deployed everywhere.
- Agents closed before this change become ordinary terminated agents on
  deploy: the next message to one resurrects it, and a stale stamp stays in
  the column until the DROP. An operator who wants one to stay quiet can run
  `ava agents terminate <id> --kill-all-shell-sessions` on it (the
  already-terminated form) and make sure nobody else writes to it.
- Peer, user and watcher messages still wake a terminated agent. The option
  only removes the agent's own sessions as a source; anyone else who writes
  to the agent still resurrects it.
- `terminate --kill-all-shell-sessions` followed by a plain `kill` kills no
  session: the force superseded the graceful request. Pass the option on the
  `kill` as well.
- The kill covers shell sessions only. Page servers (`ava.ui.serve`) keep
  their own lifecycle and are not affected. A user shell created before this
  change with a `page-` name is spared like a page until it ends (a shell
  session lives at most 24 hours).
- A killed watcher stays dead: with no watcher registry, no boot reconcile
  rebuilds it and no reclamation notice follows it.
