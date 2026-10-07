# Secrets never ride argv: session env goes through a 0600 file

## Context

Every long-running session Ava started carried its whole config on a command
line. An `-e KEY=VAL` argv splice was the mechanism (the session-env module
built the args; the session backend, the healthcheck respawn, the cluster
orchestration sessions, the schedule manager and the agents' own shells all
passed them), and agent processes carried `--config-overlay <json>` the same
way. That put the cluster secret, the pg/redis URLs that embed it, and every
provider API key into `ps -eo command` output — readable by **any** local user
or process on macOS and Linux alike, where an environment (`/proc/pid/environ`,
`ps -E`) is owner-only. Found incidentally on 2026-07-30 when a process
enumeration during an unrelated incident printed full command lines, key
material included (issue #974).

The declared boundary ([`2026-07-29-security-model-host-isolation-not-sandbox.md`](2026-07-29-security-model-host-isolation-not-sandbox.md))
is the deployment host: a same-host malicious process is already outside the
threat model, so this is not a boundary break. But the same document commits to
describing the real posture honestly, and "every secret is world-readable to
every process on the box" is a far weaker claim than "the host is the
boundary" — a second OS user on a shared box, or any unprivileged process an
agent spawns, reads them without an exploit. Between fixing and documenting,
fixing was cheap.

## Decision

**No launch path puts secret material on a command line.** Delivery is by
channel, per platform:

- **POSIX sessions:** `env_load_prefix` writes the
  forwarded map to a 0600 file under `$AVA_HOME/run/session-env/` and returns a
  shell prefix — `set -a; . <file>; set +a; rm -f <file>;` — that the session's
  command starts with. The file is unlinked by the shell that reads it (stale
  ones swept by age on the next write); argv carries only its path. The env
  lands **before** the pane's login shell, which is where the splice put it, so the
  profile still rebuilds PATH on top (`venv_activation_prefix` stays necessary
  and unchanged).
- **agent processes:** already a child env dict (`posixproc` / `winproc`), so
  both per-agent maps move there — `$AVA_AGENT_CONFIG_OVERLAY` /
  `$AVA_AGENT_BIRTH_CONFIG`, popped by `agent.loop.run` so the agent's own
  children don't inherit them. `agent.db.schedule_self_respawn` — the atexit
  fallback that replaces an agent when the restarter is paused mid-rollout, the
  one launch path outside `ops/agent_launch.py` — carries the same two maps
  the same way.
- **the cluster's redis:** `requirepass` through a 0600 `redis.conf`,
  `redis-cli` through `$REDISCLI_AUTH`.
- **Windows:** unaffected — the supervisor hands the env to `CreateProcess`.

The invariant is deny-by-default: the *whole* map goes through the file, not a
classified "secret" subset. Nothing has to be labelled correctly for the
property to hold, so a new provider key added tomorrow is covered by
construction. `tests/shared/test_no_secrets_on_argv.py` drives every launcher
with a sentinel secret and asserts the argv it builds is clean.

## Alternatives rejected

- **`set-environment` after creating the session.** Same leak: it is argv
  on the client. Also too late for the pane already running.
- **Set the vars in the server's *global* environment, create the session,
  then unset.** No argv exposure, and the session inherits properly — but the
  window between set and unset is shared by every concurrent creator on that
  socket. Agents on one cluster share a session server, so a racing
  `ava.shell.new()` could inherit another agent's `AVA_AGENT_ID` and bind a
  shell to the wrong agent. A correctness hazard traded for a security fix.
- **File-descriptor handoff** (one of the shapes the issue floated). Panes
  are spawned by the *server*, not by the client that asked for the session, so
  there is no descriptor to inherit. Workable for the agent-process path, which
  did not need it — that env was never on argv.
- **Forward only non-secret keys and let the child re-read `$AVA_HOME/.env`.**
  Smallest diff, and the child does already read that file when
  `AVA_CONFIG_SOURCE=local`. Rejected on two counts: it needs a per-field
  secret/non-secret classification that a new field can silently fall on the
  wrong side of, and it changes which value wins — today the spawner's live env
  is authoritative over `.env`, which is what keeps a worktree cluster and a
  test container from picking up the prod file.

  *Amended 2026-08-02:* the 2026-08-01 config refactor removed both grounds.
  Classification is now by the field's `scope` metadata (cluster vs host),
  derived not hand-listed, and the value-winner question is decided by the
  role-derived bootstrap fetch — a pure runner's child fetches authoritative
  values at its own boot, and a worktree cluster / test container is pinned to
  its own home by the checkout-anchored `AVA_HOME` resolution + contradiction
  check (`shared/dotenv_boot.py`), not by a forwarded cluster value winning
  over `.env`. The session-env handoffs therefore drop the cluster-scope keys
  entirely (`shared.session_env._SESSION_ENV_DROP`).
- **Leave it and document the exposure in `SECURITY.md`.** The honest option,
  and legitimate under the declared boundary. Rejected because the fix is one
  shared helper and five call sites; documenting a weakness that costs less to
  remove than to describe is the wrong trade.

## Consequences

- A session's environment is no longer visible in a `show-environment` read,
  because the vars are set in the pane's process rather than the session
  record. Nothing reads that (no code creates a second window/pane in an Ava session),
  but a human debugging a session must now read the pane's `/proc/pid/environ`
  or `ps -E` instead.
- The pane's shell for `ava.shell` sessions is now started by an explicit
  `exec "${SHELL:-/bin/bash}" -l` rather than by the server's own default-shell
  handling — the only way to have the env in place before the interactive shell
  starts.
- A value containing a newline is now forwardable (it is shell-quoted into the
  file); `-e` had to reject it.
- Secrets touch the disk in one more place, briefly. It is the same host and
  the same 0600 posture as `$AVA_HOME/.env`, which already holds all of them
  permanently, and the file is removed as the session starts.
