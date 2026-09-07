"""Automatic resume for the ordinary start command's existing pause journal."""

from collections.abc import Callable
from functools import wraps

from shared import maintenance, start_serving


def exclusive_resources[**P, R](operation: Callable[P, R]) -> Callable[P, R]:
    """Serialize whole local start/stop operations, beyond short journal writes."""

    @wraps(operation)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        from shared.platform import file_lock
        from shared.ui_update_state import lifecycle_lock_path

        with file_lock(lifecycle_lock_path(), timeout_s=0.1):
            return operation(*args, **kwargs)

    return wrapped


def resume_after_start[**P](start: Callable[P, int]) -> Callable[P, int]:
    """Keep admission closed until a real start passes its serving gate."""

    @wraps(start)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> int:
        current = maintenance.snapshot()
        if current is None:
            return start(*args, **kwargs)
        if current.maintenance is not None and current.maintenance.failures:
            raise RuntimeError(
                "start cannot release failed continuation/flush receipts; hold retained"
            )
        if maintenance.start_authorized():
            return start(*args, **kwargs)
        assert current.holder is not None and current.acquired_at is not None  # noqa: S101
        with maintenance.authorized_start(current.holder, current.acquired_at):
            result = start(*args, **kwargs)
        if result == 0 and start_serving.is_serving():
            from ops.cluster_pause import unpause_local_cluster

            unpause_local_cluster()
        return result

    return exclusive_resources(wrapped)
