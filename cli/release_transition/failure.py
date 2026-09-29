"""What a release executor routes as an operation failure, and how it records one.

Each executor routes a phase failure to a journaled outcome before it acts on
it or exits. The fleet coordinator aborts before the fence, recovers a failing
candidate once, or else holds with a `held` alert; a remote unit's follower
answers `failed`; PITR keeps its phase with the error. The route depends on
the phase, never on the error's class, so `OperationFailure` is every
`Exception`: a database error, an HTTP error and a bug in our own code alike.
A class left out would escape with no journal entry and no alert, and from
`stopping` on the executor is the home's only alert path: the home would sit
stopped under its maintenance hold with nothing telling the operator.

Routing swallows nothing. The journal, the decision and the alert keep the
failure's class and message (`failure_detail`), the executor logs the
traceback of a failure it does not re-raise, and a hold re-raises it.

Only a `BaseException` outside `Exception` (`KeyboardInterrupt`, `SystemExit`,
`GeneratorExit`) passes undecided: the executor is being ended, not failing,
and a continuation resumes from the journaled phase, exactly as after the
process dies and no handler runs at all.
"""

from __future__ import annotations

OperationFailure = Exception
# The journal's `error` and a decision's `reason` hold at most this much.
_DETAIL_LIMIT = 2048


def failure_detail(exc: BaseException) -> str:
    """The failure as the journal and its alerts keep it: class, then message."""
    return f"{type(exc).__name__}: {exc}"[:_DETAIL_LIMIT]
