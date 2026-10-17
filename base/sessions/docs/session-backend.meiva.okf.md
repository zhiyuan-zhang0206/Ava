---
type: doc
title: Session backend and process supervision
description: The native named-session and persistent PTY backends, their routing and caller contracts.
tags: [base, process, supervision]
---

# Session backend and process supervision

`base/sessions/protocol.py:SessionBackend` defines the named-session interface.
`base/sessions/backend.py` provides two entry points:

- `get_backend()` selects `PosixProcSessionBackend` or the configured macOS
  permissions-helper backend. Surviving pause probes and cold-maintenance
  consumers use this native named-session interface. Current root-supervised
  service custody is separate.
- `get_shell_backend()` selects `PtySessionBackend` for interactive agent shells,
  watchers and their session consumers. The pty-sessions service owns these
  shells, so agent exits and agent-host restarts do not end them; stopping the
  owning service does.

Native sessions use `base/sessions/record.py:SessionRecord` under the home's
`run/sessions/` and logs under `logs/`. No PTY is allocated. Native backends
raise `NotImplementedError` for terminal operations. The protocol's default
bulk timestamp method falls back to individual reads; backends without a
source return None rather than inventing uptime.

`base/sessions/env_forwarding.py` supplies child environments and virtualenv
activation. Native login shells rebuild PATH before the virtualenv prefix is
restored. Complete child environments travel separately from argv. Compound
commands must carry their own exec on the supervised stage (`build && exec
serve`); `exec_into` rejects commands it cannot safely rewrite. The shell-layer
contract below explains why the outer exec is also required.

PTY operations use short requests through `base/sessions/pty/client.py`; a
restarted caller dials the service again. Environment and command travel in
the request body of its 0600 socket, never an argv prefix. The service's base
environment excludes `MEIVA_PROCESS_PROFILE` and `VIRTUAL_ENV` before overlaying
caller values. The command is submitted once the login shell is ready. See
[[base/sessions/pty/docs/pty_sessions/pty_sessions.meiva.okf.md|pty sessions]].

Each home owns its named-session namespace. `base/cluster/derive.py:session_name`
derives service names; agent shells use `meiva-agent-<id>-shell-<n>[-<name>]`.
The session's lifetime belongs to its supervisor, not to the agent that created it.
Stopping and native process identity remain in [[stopping.meiva.okf.md|stopping]].
The macOS firewall audit has its own owner:
[[base/sessions/docs/firewall-audit.meiva.okf.md]].

## Backend implementation contracts

### Native shell launch

```meiva-contract
id = "native-shell-launch"
kind = "invariant"
statement = "Login-shell native sessions always exec the outer shell and exec the inner shell only when exec_cmd=True. They pass the caller's complete environment separately from argv. Without login_shell the command is forwarded unchanged."
code = ["base/sessions/backend.py:PosixProcSessionBackend.new_session"]
tests = [
  "base/sessions/tests/test_posix_backend.py::test_new_session_login_shell_wraps",
  "base/sessions/tests/test_posix_backend.py::test_new_session_no_login_shell_passthrough",
  "base/sessions/tests/test_posix_backend.py::test_login_shell_rebuilds_path_with_venv_prefix",
  "base/sessions/tests/test_posix_backend.py::test_no_login_shell_preserves_env_path_exactly",
]
```

The outer exec is unconditional because `/bin/sh` may be dash, which does not
replace itself for a lone command as macOS bash does. The inner exec is controlled
by `exec_cmd`: a caller that retains its login shell still needs the supervisor's
outer shell replaced. Local `posixproc` imports keep backend selection from loading
that implementation for PTY-only callers.

### Native kill parity

```meiva-contract
id = "native-kill-parity"
kind = "invariant"
statement = "The native backend accepts the shared expected keyword without changing its supervisor's kill verdict. It has no separate force-escalation warning to quiet."
code = ["base/sessions/backend.py:PosixProcSessionBackend.kill_session"]
tests = ["base/sessions/tests/test_posix_backend.py::test_kill_session_accepts_expected_for_interface_parity"]
```

### Backend routing

```meiva-contract
id = "backend-routing"
kind = "invariant"
statement = "The named-session backend selects the helper only on macOS with both helper flags enabled; unreadable settings retain the POSIX route. Interactive shells select the PTY backend independently."
code = [
  "base/sessions/backend.py:helper_spawn_enabled",
  "base/sessions/backend.py:get_backend",
  "base/sessions/backend.py:get_shell_backend",
]
tests = [
  "tests/harness/test_helperproc.py::test_backend_route_matrix",
  "tests/harness/test_helperproc.py::test_backend_route_fails_closed_when_settings_are_unreadable",
  "base/sessions/tests/test_backend.py::test_get_shell_backend_returns_platform_appropriate_backend",
]
```

The settings-read fallback precedes spawn selection. Once a helper launch is
selected, a failed helper call remains loud instead of silently changing the
process's macOS permission ancestry; the helper failure test covers that separate
contract.

### PTY allocation

```meiva-contract
id = "pty-allocation"
kind = "invariant"
statement = "PTY sessions require a login shell, carry the child environment through the service request, and report service allocation refusal as False. The shared exec_cmd parameter is intentionally unused because an interactive shell stays alive."
code = ["base/sessions/backend.py:PtySessionBackend.new_session"]
tests = [
  "base/sessions/tests/test_pty_backend.py::test_new_session_login_shell_false_raises",
  "base/sessions/tests/test_pty_backend.py::test_the_creator_env_reaches_the_shell_in_the_request_not_on_an_argv",
  "base/sessions/tests/test_pty_backend.py::test_new_session_refused_by_the_service_returns_false_and_warns",
  "base/sessions/tests/test_backend.py::test_pty_child_virtual_env_projection",
]
```

`ARG002` is limited to the inherited `exec_cmd` keyword. The parameter stays in
place so callers can use the common SessionBackend shape without changing the
interactive shell lifetime.

### PTY failure verdict

```meiva-contract
id = "pty-failure-verdict"
kind = "invariant"
statement = "A missing PTY service has no sessions, so kill is a no-op. A service that accepts a connection but does not answer has an unknown kill outcome: the backend returns failure and conservatively reports interruption. Send and capture failures raise RuntimeError naming the operation and session."
code = [
  "base/sessions/backend.py:PtySessionBackend._call",
  "base/sessions/backend.py:PtySessionBackend.kill_session_with_verdict",
]
tests = [
  "base/sessions/tests/test_pty_backend.py::test_a_home_without_a_service_holds_no_sessions",
  "base/sessions/tests/test_pty_backend.py::test_a_service_that_does_not_answer_fails_loudly_not_as_empty",
]
```

The service owns graceful-stop timing; client-side `timeout` and `expected` do
not override that authority. An unconfirmed kill cannot prove the session idle.

### PTY timestamp batch

```meiva-contract
id = "pty-timestamp-batch"
kind = "invariant"
statement = "A timestamp batch answers every requested name from one service listing; absent names map to None and an empty request returns an empty mapping."
code = ["base/sessions/backend.py:PtySessionBackend.session_started_ats"]
tests = ["base/sessions/tests/test_pty_backend.py::test_started_ats_answers_every_name_from_the_service_epochs"]
```

One listing avoids a separate round trip per session in a status snapshot. The
bound test checks the returned epochs and absent/empty cases; it does not count
network calls.
