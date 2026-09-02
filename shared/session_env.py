"""Session env forwarding — the MECHANISM for handing env to session children.

Three jobs, all mechanism (no policy):
- `forward_env_dict` builds the full child env dict for a daemon/service session
  (the registry's session forward view + PATH + venv activation). The backend
  hands it to the child as its real environment — nothing ever lands on an argv
  (issue #974: the old handoff put the cluster secret and every provider key
  on the client's command line; the 0600 env-file handoff that replaced it is
  gone too, since the native supervisors take a dict out-of-band).
- `venv_activation_prefix` re-activates this checkout's `.venv` inside a session
  command (the login shell's profile rebuilds PATH, dropping a forwarded venv
  prefix).
- `exec_into` makes the login shell hand its pid to the daemon, so the supervisor's
  recorded pid IS the daemon and a graceful SIGTERM reaches it.
- `_session_forward_env` is the registry projection both build on.

**The env POLICY lives elsewhere** (Task #856 Phase C + R2 design convergence
point A): which keys a child receives is the `child_env(role, platform)`
projection of the env registry (`shared/env_registry.py` — host-scope facts +
AVA_HOME for daemon/session children, plus agent-scope knobs and guide keys for
agent children, NOT "everything AVA_* minus a drop set"; the old denylist
forwarded AVA_AGENT_ID and every non-cluster knob into daemon sessions, a
leak that made a prod gateway carry AVA_AGENT_ID and load the whole
agent stack, +11MB resident). The builders here stay thin mechanisms over that
projection — iterate + build the dict, plus the venv activation (VIRTUAL_ENV +
PATH) a login shell's profile would otherwise drop; the allow/drop decision is
the data in the registry. The agent-child builder lives with its sole consumer
in `ops.agent_launch.agent_spawn_env_dict`; the daemon-session builders stay
here because five callers across four layers (ava SDK shell sessions, shared
service respawn, cli start, ops operator shells, gateway schedule runner) need
identical semantics and layering forbids a lower layer importing a process
package. The child re-sources cluster-scope values at its own boot (gateway
fetch on a runner, its own .env on a gateway-capable unit), so a spawner's
frozen copy is redundant and a third-party library that reads os.environ
directly would only ever see the stale one.

This module reads os.environ by nature (it forwards the live env), so it is on
lint_no_os_environ's allowlist.
"""

from __future__ import annotations

import os
import re
import shlex

from shared.paths import repo_root
from shared.platform import IS_WINDOWS
from shared.platform_backend import get_backend

_FRONTEND_TOOLCHAIN_DIRS = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
)


def frontend_toolchain_path(inherited_path: str) -> str:
    """Return a POSIX PATH that can resolve the provisioned Node toolchain.

    `scripts/provision/node.sh` installs Node through Homebrew on macOS and
    through apt on Linux. Non-login remote shells do not reliably inherit the
    Homebrew directories, so the frontend cannot rely on a shell profile to
    find `npm`. Keep the provisioned locations ahead of the inherited path,
    while preserving every caller-specific directory after them. Windows keeps
    its inherited PATH because its `npm.cmd` lookup uses the native shell.
    """
    if IS_WINDOWS:
        return inherited_path
    seen: set[str] = set()
    dirs = (*_FRONTEND_TOOLCHAIN_DIRS, *inherited_path.split(os.pathsep))
    return os.pathsep.join(
        directory
        for directory in dirs
        if directory and not (directory in seen or seen.add(directory))
    )


def frontend_toolchain_env() -> dict[str, str]:
    """The inherited environment for a direct frontend-toolchain subprocess.

    `npm ci` already inherited this process's complete environment; copying it
    preserves npm's user cache and credential behavior while replacing only its
    PATH with the deterministic Node search path.
    """
    env = dict(os.environ)
    env["PATH"] = frontend_toolchain_path(env.get("PATH", ""))
    return env


def _session_forward_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """The caller-built env dict a daemon/session child receives: the
    registry's session forward view (`child_env`) plus `extra`.

    Daemons belong to the gateway/runner process profiles, whose session view
    is identical (host-scope settings aliases + AVA_HOME, the ambient display
    passthroughs and temp-dir vars non-empty only). POSITIVE allowlist, not a
    drop list (Task #856 Phase C, audit F-s3-4): a non-modeled knob
    (AVA_AGENT_ID, ...) or agent-scope override never rides into a daemon
    session. The allow/drop decision is the DATA in shared/env_registry.py;
    this function is the mechanism that applies it.
    """
    from shared.env_registry import child_env

    forward = child_env("gateway", "windows" if IS_WINDOWS else "posix")
    forward.update(extra or {})
    return forward


def forward_env_dict() -> dict[str, str]:
    """The full child env dict for a daemon/service session (the Windows analog
    of `forward_env_prefix`; POSIX callers also use it where the backend takes a
    dict).

    There is no shared server env to freeze on Windows, so each
    child gets a built dict: the registry's session forward view
    (`_session_forward_env`) — host-scope AVA_* config + the ambient display
    passthroughs — *and* PATH, which the child genuinely needs. Cluster-scope
    values and agent-scope/non-modeled knobs are not carried (positive
    allowlist, Task #856 Phase C / audit F-s3-4): the child re-sources them at
    its own boot, so a spawner's frozen copy is redundant and a third-party
    library that reads os.environ directly would only ever see a stale one.

    Also activates the venv the way `uv run` used to: service cmds now exec the
    venv interpreter directly (`.venv/bin/python -m …`, no resident `uv run`
    parent per daemon), so this reproduces what `uv run` injected — VIRTUAL_ENV +
    the venv bin dir prepended to PATH — so a daemon that shells out to bare
    `python` / `ava` still resolves into the venv. The provisioned Node
    locations follow it, so the frontend's bare `npm` is found even when a
    remote shell starts with only system PATH entries. Symmetric with the agent
    activation in `ops.agent_launch._launch_agent_process`.

    The dict is handed to the session backend as the child's real environment
    (`new_session(..., env=...)`) on both platforms — the out-of-band env handoff
    that replaced the old env-file handoff (issue #974), so the activation
    here is authoritative. On **POSIX** the
    daemon session still runs under a login shell (`bash -lc`) whose profile /
    macOS `path_helper` rebuilds PATH and drops this venv prefix — so POSIX PATH
    activation is re-applied inside the session command via
    `venv_activation_prefix` instead.
    """
    env = _session_forward_env()
    # Venv activation (the `uv run` injection this dict reproduces): the
    # allowlist never carried PATH/VIRTUAL_ENV, and on Windows the env block is
    # a wholesale replacement, so the child must get them here. (The temp-dir
    # vars and the Windows system keys ride in `child_env` already.)
    venv = repo_root() / ".venv"
    venv_bin = venv / get_backend().venv_bin_dir_name()
    env["VIRTUAL_ENV"] = str(venv)
    env["PATH"] = str(venv_bin) + os.pathsep + frontend_toolchain_path(os.environ.get("PATH", ""))
    return env


def venv_activation_prefix() -> str:
    """A POSIX-shell snippet that activates this checkout's `.venv` for a session
    daemon command. Prepend it INSIDE the session command (right after
    `cd <repo>`), e.g. ``cd <repo> && {venv_activation_prefix()}<cmd>``.

    Why in the command and not via the forwarded env: daemon sessions run under
    a login shell (`bash -lc`, so the profile is sourced), and the login profile
    — macOS `path_helper` especially — rebuilds PATH from scratch, dropping any
    venv prefix the forwarded env carried (VIRTUAL_ENV survives, PATH does not). Re-exporting here, after the
    profile has run, is what makes a daemon that execs a bare binary off PATH
    work — `services.milvus.daemon` execvp's `milvus-lite`, and a daemon that
    shells out to bare `ava` / `python` resolves them into the venv. It also
    injects the provisioned Node locations after the venv, because the frontend
    session runs bare `npm` after that profile has had a chance to discard PATH.
    Mirrors the child-env activation `ops.agent_launch` does for agents.
    """
    venv = repo_root() / ".venv"
    bindir = venv / get_backend().venv_bin_dir_name()
    toolchain_path = frontend_toolchain_path("")
    toolchain_suffix = f"{os.pathsep}{toolchain_path}" if toolchain_path else ""
    return (
        f"export VIRTUAL_ENV={shlex.quote(str(venv))} && "
        f'export PATH={shlex.quote(str(bindir))}{toolchain_suffix}:"$PATH" && '
    )


# Shell operators that chain a command line into more than one command, so a
# blanket `exec ` prefix would apply to the wrong stage (`exec cd foo && bar`
# execs `cd` and never runs `bar`). Matched as shlex TOKENS, not substrings, so
# an `&` or `|` inside a quoted argument (a URL, a regex) is not mistaken for
# one. Redirections are deliberately absent: `exec cmd > log` is a single
# command and behaves exactly as intended.
_CHAINING_TOKENS = frozenset({"&&", "||", ";", "|", "&", "(", ")", "{", "}"})

# Splits a command line into its chained stages. Quote-blind on purpose: it only
# LOCATES an existing hand-off, so a mis-split can at worst fail to find one —
# which falls through to the quote-aware compound check below, i.e. toward the
# loud error and never toward a silent pass-through.
_CHAIN_SPLIT = re.compile(r"&&|\|\||;")


def _execs_its_final_stage(cmd: str) -> bool:
    """True when `cmd` already hands its pid over on the stage that ends up
    supervised — the LAST one in a `&&` / `||` / `;` chain.

    Anchored to that stage rather than matching `exec` anywhere in the line. A
    bare `exec` somewhere else (inside a `-c` script, or mid-chain where it would
    strand every later stage) is not a hand-off, and treating it as one would
    return the command unwrapped — restoring exactly the invisible failure this
    module exists to prevent: a wrapper shell that outlives its daemon and
    swallows the graceful-stop SIGTERM. Anything this does not recognise as a
    hand-off is either prefixed or rejected, both of which are loud.
    """
    return _CHAIN_SPLIT.split(cmd)[-1].lstrip().startswith("exec ")


def exec_into(cmd: str) -> str:
    """Make the session's login shell `exec` into `cmd`, so the wrapper shell
    does not survive as a process between the supervisor and the daemon.

    Without this the supervisor records the *shell's* pid while the daemon runs
    as its child, and a graceful stop's SIGTERM lands on the shell: a
    non-interactive bash waiting on a foreground child neither forwards the
    signal nor exits before that child does, so the daemon never learns it was
    asked to stop. Every graceful stop then burns its full timeout and ends in
    the SIGKILL fallback — no daemon's cleanup ever runs. `exec` collapses the
    wrapper so the recorded pid IS the daemon and the supervisor's whole kill
    layer ("SIGTERM the recorded pid, let it run its finally") holds for
    services the way it already holds for agents, which are launched from an
    argv with no shell in between.

    A compound command must place its own `exec` on the stage it wants
    supervised (`build && exec serve`) — prefixing one here would exec the
    first stage. That is a hard error, not a silent pass-through: the failure it
    would otherwise cause is invisible (a service that stops 15s slower and
    never runs its cleanup), so it has to surface at launch. For the same reason
    only an `exec` on the FINAL stage counts as already handing the pid over
    (`_execs_its_final_stage`); one anywhere else is not a hand-off and does not
    buy the command a pass.

    Raises:
        ValueError: `cmd` is compound and does not `exec` anything itself.
    """
    if _execs_its_final_stage(cmd):
        return cmd
    try:
        tokens = shlex.split(cmd)
    except ValueError as exc:  # unbalanced quotes — not a command shape we can reason about
        raise ValueError(f"session command is not parseable as a shell command: {cmd!r}") from exc
    chained = any(tok in _CHAINING_TOKENS for tok in tokens)
    # Belt-and-braces for an unspaced chain (`build&&serve`), which survives
    # tokenisation as one word. These never occur inside a real argument.
    chained = chained or any(op in cmd for op in ("&&", "||", "\n"))
    if chained:
        raise ValueError(
            f"session command is compound but never execs, so the wrapper shell would "
            f"outlive it and swallow the graceful-stop SIGTERM: {cmd!r}. Put `exec` on "
            f"the stage that should be supervised (e.g. `npm run build && exec npm run start`)."
        )
    return f"exec {cmd}"
