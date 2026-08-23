"""The agent-runner self-update's in-process stop must kill with POST-checkout code.

`cli/commands/_update_agent_runner.py` calls `_ns._do_stop(...)` **in-process**
after the `git checkout` + `uv sync` above it. In-process means the stop executes
whatever is already in `sys.modules`, so whether it kills this host's sessions with
the just-pulled session-kill code or with the pre-pull code is decided entirely by
import timing. Nothing the updater imports before the checkout may reach
`shared.session_backend` or `shared.session_record`: every path to the former
(`cli/commands/_session_lifecycle.py`, `cli/commands/stop.py`) imports it
*method-locally*, and it in turn reaches the platform supervisors method-locally.
`shared.session_record` must be deferred too: `shared.proc` is in the updater's
closure, and its former module-scope import left the old record module resident.
The new `shared.posixproc` then imported `pid_starttime_ticks` from that stale
module after checkout and crashed the stop. So the whole kill chain is first loaded
when the stop actually kills something — after the checkout, off the fresh files on
disk. That is what this module pins.

The arrangement is load-bearing, and its failure mode is invisible. PR #932 fixed a
`winproc.kill_session` that walked past a session boundary and killed the updater's
own session, leaving the Windows agent-runner stopped *and* un-updated. That fix
lands on the very next Windows self-update **because of** the deferred import. Let
anything the updater imports at module scope start reaching the kill chain and the
stop reverts to running pre-pull code — i.e. a rollout that ships a kill fix cannot
benefit from it, which is the whole reason the Windows box got stuck in the first
place. Nothing else in the suite notices; it surfaces only as a failed rollout on
the fleet's one Windows box, diagnosed from Windows logs.

Shape of the assertion, and why it is not vacuous on the POSIX host CI runs on:

- The closure is measured in a **clean interpreter subprocess**. In-process the
  question is unanswerable: the running test session has already imported half the
  tree, so these modules are resident for reasons that have nothing to do with the
  updater.
- The assertion that carries the weight is `shared.session_backend`, not the
  Windows leaf. It is the *sufficient* condition: while session_backend is outside
  the closure, nothing it imports can be inside either, so the freshness of the
  Windows killer holds structurally rather than by the convention that five import
  statements stay indented. And it is checkable at full force on POSIX, where it is
  independently load-bearing — the session the self-update's stop kills on a Mac or
  Linux box is killed by `SessionBackend.kill_session`, whose graceful-then-force
  loop is Python code *in shared/session_backend.py*. A stale dispatcher is a stale
  killer on POSIX too.
- The leaves (`shared.winproc`, `shared.posixproc`, `shared.session_record`) are
  asserted as defence in depth. The first two catch a hoist that reaches a
  supervisor by a route that bypasses session_backend —
  `services/healthchecks/frontend.py` also imports winproc method-locally, and
  `cli/commands/stop.py`'s agent reap goes to `posixproc` via `native_proc()`.
  `shared.session_record` catches the separate stale-module path: `shared.proc`
  is already in the updater's closure, and its former module-scope import poisoned
  `posixproc`'s later import of the new `pid_starttime_ticks` API. Neither platform
  supervisor leaf alone would be a meaningful assertion on POSIX: `shared.winproc`
  is never imported at all off Windows, so its absence there proves nothing by
  itself.
- A **positive control** pins that the subject still exists: `cli.commands.stop`
  (which owns `_do_stop`) must BE resident. That is the invariant's exact shape —
  the stop's entry point is pre-checkout code, the killer underneath it is not yet
  loaded — and it fails if the updater ever stops reaching the stop at all, which
  would otherwise make the absence assertions true for the wrong reason.
- Every name is checked to actually resolve, so renaming a module cannot silently
  disarm an absence assertion into a tautology.

Not expressible as an import-linter contract: grimp builds its graph from the AST
and counts a method-local import as an edge, so a `forbidden: cli -> shared.winproc`
contract reports the chain `cli.commands._session_lifecycle -> shared.session_backend ->
shared.winproc` as broken in the healthy state. It is blind to the module-scope /
function-scope distinction that is the entire content of this invariant, and
silencing it with `ignore_imports` would silence exactly the import whose scope is
the thing being policed.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import textwrap
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

# What the updater has loaded by the time it reaches the checkout: its own
# module-scope imports, plus the `import cli.commands as _ns` it does first (a lazy
# import that avoids a top-level cycle, and the widest thing it pulls in).
_UPDATER_IMPORTS = ("cli.commands", "cli.commands._update_agent_runner")

# The post-checkout stop's dispatcher and platform supervisors, plus
# `session_record`: `shared.proc` is in the updater's closure, so a module-scope
# import there would poison posixproc's later import of the new record API. None
# may be in the closure above.
_MUST_BE_LAZY = (
    "shared.session_backend",
    "shared.winproc",
    "shared.posixproc",
    "shared.session_record",
)

# Positive control — the stop's own entry point, which IS pre-checkout code.
_MUST_BE_RESIDENT = ("cli.commands.stop",)


def _resident(names: tuple[str, ...]) -> set[str]:
    """Which of `names` a clean interpreter has in `sys.modules` after importing
    what the updater imports before its checkout.

    A subprocess, not this interpreter: the running test session's `sys.modules`
    says nothing about the updater's import closure.
    """
    probe = textwrap.dedent(f"""
        import sys

        for name in {list(_UPDATER_IMPORTS)!r}:
            __import__(name)

        for name in {list(names)!r}:
            if name in sys.modules:
                print(name)
    """)
    proc = subprocess.run(  # noqa: S603 — fixed argv, sys.executable is trusted
        [sys.executable, "-c", probe],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        env=dict(os.environ),
        check=False,
    )
    assert proc.returncode == 0, f"import probe failed:\n{proc.stdout}\n{proc.stderr}"
    return set(proc.stdout.split())


def test_every_named_module_resolves() -> None:
    """Anti-tautology guard: a rename must fail loudly, not disarm the assertions.

    `"shared.winproc" not in sys.modules` is trivially true once no module by that
    name exists, so the absence assertions below are only worth anything while every
    name still points at something.
    """
    for name in _MUST_BE_LAZY + _MUST_BE_RESIDENT + _UPDATER_IMPORTS:
        assert importlib.util.find_spec(name) is not None, (
            f"{name} no longer resolves — this test's assertions have gone vacuous. "
            f"Point them at the module that replaced it."
        )


def test_session_kill_chain_is_not_in_the_updaters_import_closure() -> None:
    """No dependency of the session-kill chain may load before the checkout.

    Fails if anything the updater imports at module scope starts reaching
    `shared.session_backend`, or if `from shared import winproc` / `posixproc` is
    hoisted to module scope in a module that is already in the closure. It also
    fails if `shared.proc` imports `shared.session_record` at module scope: the
    freshly loaded posixproc would then see its old API after checkout. Either way
    the in-process `_do_stop` after the checkout kills with pre-pull code or fails.
    """
    eager = _resident(_MUST_BE_LAZY)
    assert eager == set(), (
        f"{sorted(eager)} is loaded before `_run_agent_runner_self_update`'s git "
        f"checkout, so the in-process `_do_stop` after it would kill this host's "
        f"sessions with PRE-PULL code — the failure mode PR #932 fixed on Windows, "
        f"where the stop killed the updater's own session. The kill chain and its "
        f"session-record dependency must stay reachable only through method-local "
        f"imports; see this module's docstring and the comments in "
        f"shared/session_backend.py and shared/proc.py."
    )


def test_the_stop_entrypoint_itself_is_resident() -> None:
    """Positive control for the test above: the updater does still reach the stop.

    `_do_stop` lives in `cli.commands.stop` and is imported at module scope, so it
    is pre-checkout code by construction — only the killer *beneath* it is deferred.
    If this goes red the absence assertions have stopped describing a real path and
    need rewriting against wherever the stop moved.
    """
    assert _resident(_MUST_BE_RESIDENT) == set(_MUST_BE_RESIDENT), (
        "cli.commands.stop is no longer in the updater's import closure — the "
        "in-process stop this file protects has moved or gone away."
    )
