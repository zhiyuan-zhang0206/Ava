"""Complete a stranded update hold — `python -m cli.commands._hold_recover`.

Spawned by the pause controller (`ops.hold_recovery.spawn_hold_recovery`, task
#3142) after the episode's single bounded attempt was reserved in
`host_deploy_state`. This entry runs the operator recipe from
`conventions/graceful-maintenance.md` ("Recovering a stuck maintenance
operation"), in-process inside the sanctioned detached session:

- `stopping` — complete the stop (the update leg's own stop call: same service
  selection, `keep_infra` / `keep_terminals`, browser retained), then start;
- `stopped` / `starting` — start;
- then `resume` — release the hold and return the posture to idle.

Each leg re-verifies its own preconditions under its own locks; this entry
adds the stranded-hold gate on top: the hold must still carry the exact
`(holder, acquired_at)` capability, its phase must still be a post-stop one,
the stranded verdict must still stand, and the kill-switch must still be on.
Which holds may be completed at all (update-armed, post-stop, never a gateway
unit) is the spawner's decision — this entry only re-verifies the license it
was spawned under.

Exit codes: 0 = the hold completed (services up, hold released); 1 = the
attempt failed, or the conditions that licensed it no longer hold. Either way
the attempt is spent and the outcome is recorded in
`host_deploy_state.stranded_hold_recovery_note`.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime

from ops import hold_recovery
from shared.config import settings


class RefusedError(RuntimeError):
    """A precondition of the licensed attempt no longer holds."""


def _verify(holder: str, acquired_at: datetime) -> str:
    """Return the hold's phase once every precondition still holds.

    The generation check (`matches`) is the same capability the operator
    commands require: a hold that was released, re-acquired, or re-phased
    between the reservation and this run is not ours to complete.
    """
    from ops.controllers.stranded_pause import stranded_hold_verdict
    from shared import maintenance

    current = maintenance.snapshot()
    if current is None or not current.matches(holder, acquired_at):
        raise RefusedError("this unit is not held by the supplied maintenance generation")
    assert current.maintenance is not None  # noqa: S101 — snapshot carries a hold
    phase = current.maintenance.phase
    if phase not in hold_recovery.RECOVERABLE_PHASES:
        raise RefusedError(f"hold phase {phase!r} is not a post-stop completion phase")
    if not settings.gateway.stranded_hold_recovery:
        raise RefusedError("the stranded-hold recovery switch is off")
    verdict = stranded_hold_verdict()
    if verdict.kind != "stranded":
        raise RefusedError(f"the stranded-hold verdict is {verdict.kind!r}, not 'stranded'")
    return phase


def _complete_stop() -> None:
    """Re-run the stop the update leg was executing when it died.

    `cli.commands.stop._do_stop` with the self-update leg's arguments
    (`keep_infra`, terminals and browser retained) and its service selection
    from the roster — the same call `_update_agent_runner` makes, which is why
    the recovery session itself and the browser daemon survive it.
    """
    from cli.commands._repo import _repo_root
    from cli.commands.stop import _do_stop

    rc = _do_stop(
        _repo_root(),
        require_confirmation=False,
        keep_infra=True,
        timeout=settings.gateway.update_quiesce_timeout_seconds,
    )
    if rc != 0:
        raise RuntimeError(f"stop leg exited {rc}")


def _start_leg(holder: str, acquired_at: datetime) -> None:
    """Bring the unit back up under its hold (`maintenance start`)."""
    from cli.commands._maintenance import _start

    rc = _start(holder, acquired_at)
    if rc != 0:
        raise RuntimeError(f"start leg exited {rc}")


def _resume_leg(holder: str, acquired_at: datetime) -> None:
    """Release the hold and return the posture to idle (`maintenance resume`)."""
    from cli.commands._maintenance import _resume

    _resume(holder, acquired_at, cancel=False)


def _record(note: str) -> None:
    """Persist this attempt's outcome; a failed write never changes the verdict."""
    from shared.host_deploy_state import finish_stranded_recovery

    try:
        finish_stranded_recovery(note)
    except Exception as exc:
        print(f"[hold-recover] outcome not recorded: {exc!r}")


def run(holder: str, acquired_at: datetime) -> int:
    step = "verify"
    try:
        phase = _verify(holder, acquired_at)
        print(f"[hold-recover] completing a stranded hold at phase {phase!r}")
        if phase == "stopping":
            step = "stop"
            _complete_stop()
        step = "start"
        _start_leg(holder, acquired_at)
        step = "resume"
        _resume_leg(holder, acquired_at)
    except RefusedError as exc:
        print(f"[hold-recover] refused: {exc}")
        _record(f"refused: {exc}")
        return 1
    except Exception as exc:
        print(f"[hold-recover] {step} leg FAILED: {exc!r}")
        _record(f"failed at {step}: {exc!r}"[:500])
        return 1
    print("[hold-recover] completed: services up, hold released")
    _record("completed: stop/start/resume finished")
    return 0


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m cli.commands._hold_recover")
    parser.add_argument("--operation", required=True, help="the hold's holder capability")
    parser.add_argument(
        "--acquired-at", required=True, help="the hold's timezone-aware acquisition timestamp"
    )
    args = parser.parse_args(argv[1:])
    acquired_at = datetime.fromisoformat(args.acquired_at)
    if acquired_at.tzinfo is None or not args.operation.strip():
        raise ValueError("hold recovery requires a nonempty operation and timezone-aware timestamp")
    return run(args.operation, acquired_at)


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
