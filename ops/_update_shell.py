"""cmd.exe restart-with-recovery fragment for the detached update chain.

The updater's `ava restart` ladder on Windows. POSIX no longer has a shell
ladder at all: its detached `ava-updater` session runs the in-process
self-update (`python -m cli.commands._update_agent_runner`, R1-6 Task #1021 —
execution-shape convergence) instead of a hand-built `if/else` chain, so the
decline-vs-failure verdict travels as `[session-exit] rc=` and
`ops.updater_outcome` reads the rc. Windows keeps this cmd.exe fragment because
the in-process path holds `python.exe` open out of the very venv `uv sync` is
about to rewrite.

**The verdict travels here too, branch by branch.** cmd.exe cannot expand the
errorlevel at the end of a command line without delayed expansion — but it does
not have to, because this ladder has already *branched on* it: each arm knows
which outcome it is, so each states its own literal rc (`native_exit_line`).
Before that, a Windows host's log carried the decline sentence and nothing else,
so a run that died at `git fetch` and a run that finished were the same reading —
`unknown` — and Phase B spent its whole 15-minute bound on both.

Pure string construction, no side effects, so it is easy to unit test in
isolation. `RESTART_DECLINED_EXIT_CODE` lives in `shared/exit_codes.py`.

Why the ladder exists (still the cmd.exe contract, and the reasoning the POSIX
side now lives out in Python): the chain used to be `if <checkout && sync>;
then ava restart; else ... ava start; fi`, so the recovery branch could only
fire when the CHECKOUT failed. On 2026-07-28 the checkout and sync both
succeeded on wsl and the `ava restart` itself failed — no fallback ran at all,
and the host sat with HEAD on the target and its processes on the old code
until a human noticed. Recovering unconditionally would be worse: `ava restart`
refuses when its validate-before-kill preflight fails and leaves the host
SERVING. So the two are told apart by exit code:
  RESTART_DECLINED_EXIT_CODE -> nothing was stopped, host still serving -> say so, do NOT start.
  any other non-zero         -> the stop already happened, host may be DOWN -> `ava start`.
"""

from __future__ import annotations

from ops.updater_outcome import native_exit_line
from shared.exit_codes import RESTART_DECLINED_EXIT_CODE

# The source-switch window's ladder steps (`shared/source_switch.py`): `ON`
# opens the window ahead of the checkout, `OFF` closes it at the chain tail /
# abort arm. Both are fail-soft (`|| ver>nul`) so the first rollout that ships
# the marker — whose pre-checkout tree predates the module — cannot break on
# ModuleNotFoundError; a crash in between leaves the marker to expire on its
# TTL. Kept as constants merged into neighbouring ladder lines (rather than
# standalone steps) to hold cluster_deploy.py inside its 800-line budget.
SOURCE_SWITCH_ON = " & (python -m cli.commands._source_switch_marker on || ver>nul)"
SOURCE_SWITCH_OFF = " & (python -m cli.commands._source_switch_marker off || ver>nul)"


def _restart_recovery_cmd(
    *, quiesce: bool = False, mode: str = "smooth", force_reap: bool = False
) -> str:
    """cmd.exe equivalent of the retired POSIX ladder (same flags; `if errorlevel N`
    is ">= N", so the ladder is ordered high-to-low: >3 and 1..2 recover, exactly
    3 declines, 0 falls through).

    Every arm is terminal and closes with its own verdict, including the `else` that
    the errorlevel-0 fall-through used to reach silently — a ladder where one exit
    says nothing is a ladder whose reader cannot tell that path from a death.

    The two recovery arms report rc=1 rather than the rc of the `ava start` behind
    them. That is the branch's own meaning: it was entered because `ava restart`
    failed, so this run did not do what it was asked. Whether the `ava start` then
    rescued the host is a different question with a better answer — the posture row
    the started services write, which Phase B reads first and which outranks this
    line anyway (a host back at `idle` is reported converged, whatever its log says).
    """
    flags = ""
    if quiesce:
        flags += f" --quiesce --mode {mode}"
    if force_reap:
        flags += " --force-reap"
    final_marker = "(python -m cli.commands._updater_stage final || ver>nul)"
    recovery_start = (
        "(python -m cli.commands._updater_stage start || ver>nul) & ava start --persist-services"
    )
    # The recovery `ava start` arms run as an INTERNAL child start
    # (`--persist-services`), never as an operator start: a Phase-B updater
    # runs under the cluster-wide executing deploy lease, and an operator start
    # is refused by the rollout boundary (start._rollout_child_window) — the
    # exact shape that stranded win on 2026-09-02 (a restart refused by a
    # co-located unit's health port fell into the recovery arm, the arm's
    # operator-mode start was refused by the lease, and the updater exited rc=1
    # before its services ever started). The POSIX in-process updater already
    # starts internally under that same lease; the hidden flag is the cmd.exe
    # ladder's way to say the same thing, and it also preserves the operator's
    # durable --disable-service marker across the restart.
    return (
        f"ava restart{flags}"
        f" & if errorlevel {RESTART_DECLINED_EXIT_CODE + 1} ({recovery_start} & {final_marker} & {native_exit_line(1)})"
        f" else if errorlevel {RESTART_DECLINED_EXIT_CODE} ("
        f"echo [updater] restart DECLINED by its own preflight -- host still serving, not starting over it"
        f" & {final_marker} & {native_exit_line(RESTART_DECLINED_EXIT_CODE)})"
        f" else if errorlevel 1 ({recovery_start} & {final_marker} & {native_exit_line(1)})"
        f" else ({final_marker} & {native_exit_line(0)})"
    )
