"""Automatic resume for the ordinary start command's existing pause journal."""

from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps

from cli.cutover_hold import CutoverHold, held_start_command, standing_hold
from shared import maintenance, pause_owner, start_serving


@dataclass(frozen=True)
class StartDelegation:
    """A different process owns startup, including readiness and resume."""

    run: Callable[[], int]


def exclusive_resources[**P, R](operation: Callable[P, R]) -> Callable[P, R]:
    """Serialize whole local start/stop operations, beyond short journal writes."""

    @wraps(operation)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        from shared.home_lifecycle_locks import resource_lock

        with resource_lock(purpose=f"cli.{operation.__name__}"):
            return operation(*args, **kwargs)

    return wrapped


def _start_held_for_cutover(
    cutover: CutoverHold,
    current: pause_owner.PauseOwnerSnapshot,
    start: Callable[[], int | StartDelegation],
) -> int | StartDelegation:
    """Start inside the standing cutover hold and keep it; the go/no-go gate releases it.

    Before the cutover's held first start (phase `stopped`) the unit refuses:
    on a gateway that start must follow the database-records repair. A start
    that passes readiness completes a `starting` hold to `ready`, the held first
    start's own last step, so a failed first start is finished by the next one
    that serves (the autostart after a reboot included).
    """
    from shared.paths import ava_home

    assert current.maintenance is not None  # noqa: S101 — snapshot() only returns maintenance holds
    phase = current.maintenance.phase
    if phase not in {"starting", "ready"}:
        raise RuntimeError(
            f"the cutover hold {cutover.holder} stands in phase {phase}; "
            f"its first start is {held_start_command(ava_home())} (a remote unit adds "
            "`--db-capability BUNDLE`), and an ordinary start never releases it"
        )
    with maintenance.authorized_start(cutover.holder, cutover.acquired_at):
        result = start()
    if result != 0:
        return result
    if not start_serving.is_serving():
        print(
            f"\n→ cutover hold {cutover.holder} kept in phase {phase}: the unit is not "
            "serving, so its release waits for a start that passes readiness"
        )
        return result
    if phase == "starting":
        maintenance.set_phase(cutover.holder, cutover.acquired_at, "ready")
    print(
        f"\n→ cutover hold {cutover.holder} kept: business stays closed until the "
        f"go/no-go gate releases it with `{cutover.resume_command()}`"
    )
    return result


def resume_after_start[**P](start: Callable[P, int | StartDelegation]) -> Callable[P, int]:
    """Keep admission closed until a real start passes its serving gate."""

    @exclusive_resources
    def start_locked(*args: P.args, **kwargs: P.kwargs) -> int | StartDelegation:
        from shared.paths import ava_home
        from shared.release_operation import require_start_authorized

        operation_hold = require_start_authorized(ava_home())
        current = maintenance.snapshot()
        if current is None:
            if operation_hold is not None:
                raise RuntimeError("release startup requires its exact maintenance hold")
            return start(*args, **kwargs)
        if current.maintenance is not None and current.maintenance.unsettled_failures():
            raise RuntimeError(
                "start cannot release failed continuation/flush receipts; hold retained"
            )
        if operation_hold is not None:
            # Operation startup restores services while its executor retains
            # the hold. Only that executor may resume after observation.
            with maintenance.authorized_start(*operation_hold):
                return start(*args, **kwargs)
        if maintenance.start_authorized():
            return start(*args, **kwargs)
        assert current.holder is not None and current.acquired_at is not None  # noqa: S101
        if (cutover := standing_hold(ava_home())) is not None:
            return _start_held_for_cutover(cutover, current, lambda: start(*args, **kwargs))
        with maintenance.authorized_start(current.holder, current.acquired_at):
            result = start(*args, **kwargs)
        if result == 0 and start_serving.is_serving():
            from ops.cluster_pause import unpause_local_cluster

            unpause_local_cluster()
            # start()'s status snapshot was taken under the hold, so it read paused.
            print(
                "\n→ maintenance hold released: admission reopened, the cluster is serving "
                "(the status above predates the release)"
            )
        return result

    @wraps(start)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> int:
        result = start_locked(*args, **kwargs)
        # The GUI child takes this same lock. The observer must leave both the
        # lock and its maintenance authorization before kicking/waiting on it.
        return result.run() if isinstance(result, StartDelegation) else result

    return wrapped
