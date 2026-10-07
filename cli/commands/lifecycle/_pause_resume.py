"""Automatic resume for the ordinary start command's existing pause journal."""

from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps

from base.db import Database
from base.deploy.lifecycle import start_serving
from base.deploy.maintenance import admission
from base.events.live.bus import EventBus


@dataclass(frozen=True)
class StartDelegation:
    """A different process owns startup, including readiness and resume."""

    run: Callable[[], int]


def exclusive_resources[**P, R](operation: Callable[P, R]) -> Callable[P, R]:
    """Serialize whole local start/stop operations, beyond short journal writes."""

    @wraps(operation)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        from base.deploy.lifecycle.home_lifecycle_locks import resource_lock

        with resource_lock(purpose=f"cli.{operation.__name__}"):
            return operation(*args, **kwargs)

    return wrapped


def resume_after_start[**P](start: Callable[P, int | StartDelegation]) -> Callable[P, int]:
    """Keep admission closed until a real start passes its serving gate."""

    @exclusive_resources
    def start_locked(*args: P.args, **kwargs: P.kwargs) -> int | StartDelegation:
        current = admission.snapshot()
        if current is None:
            return start(*args, **kwargs)
        if admission.start_authorized():
            return start(*args, **kwargs)
        assert current.holder is not None and current.acquired_at is not None  # noqa: S101
        with admission.authorized_start(current.holder, current.acquired_at):
            result = start(*args, **kwargs)
        if result == 0 and start_serving.is_serving():
            from cli.commands.lifecycle._failed_receipts import settle_failed_receipts
            from ops.cluster.pause import unpause_local_cluster

            settle_failed_receipts()

            unpause_local_cluster(Database.from_settings(), EventBus.from_settings())
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
