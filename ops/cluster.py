"""Multi-machine `ava cluster update` orchestration — gateway side. Entry surface.

A pure re-export facade: every name this module ever exposed is still an
attribute of it, so `from ops.cluster import ...` and
`monkeypatch.setattr("ops.cluster.spawn_update", ...)` keep working unchanged.
The implementation lives in four modules beside it, layered so that nothing
imports this facade back:

- `ops/cluster_session.py` — the session + orchestration-liveness primitives
  every cluster op stands on (the leaf; imports none of the others).
- `ops/cluster_pause.py` — the pause/unpause lifecycle and the `is_paused` flag read.
- `ops/cluster_status.py` — this host's `ClusterStatus` snapshot.
- `ops/cluster_deploy.py` — the rollout preflight and the three detached-session
  triggers, with in-flight refusal and stall reaping.

**These four modules reach the state-touching names they share through the module
that OWNS them** — `shared.cluster.session_name(...)`,
`shared.host_deploy_state` posture row, `cluster_session._has_orchestration_session(...)` —
rather than from-importing them. A from-imported name is resolved from the module
the *reader* is defined in, so moving a function between these four silently took
it out of reach of a patch aimed at its old home: the split that produced this
facade cost 81 `setattr` repoints for exactly that reason, and re-exporting from
here fixed the importers, not the resolution inside moved code. Constants,
exception classes, models and pure formatters stay from-imported — nothing stubs
them, so they carry no patch surface to keep in place. Full rule:
`conventions/python-conventions.md` ("Reach a stubbable name through its
owning module").

Every entry point here launches or probes sessions through the platform
session backend (`get_backend()`; POSIX: the native process supervisor — the
S7 migration retired the last legacy backend in the cluster path), so the sessions
always land in this cluster's own `$AVA_HOME/run/sessions/` records, never in
someone else's namespace.

Handlers and middleware implement pure logic in this module;
`gateway/app.py` handles the FastAPI endpoint / middleware wrapping
(same pattern as `ops/agents.py`).
"""

from __future__ import annotations

# These re-exports MUST stay eager — do not convert them to lazy / `__getattr__`
# imports for startup time. `tests/conftest.py`'s `_guard_cluster_spawn` stubs the
# spawn entry points through `_stub_everywhere`, which rebinds every alias of the
# real function by object identity but only across modules **the run has already
# imported** ("Nothing is imported to find them"). Importing `ops.cluster` eagerly
# pulls all four submodules in, so that scan always reaches the definition site.
# Made lazy, the scan would reach whichever submodules an earlier test happened to
# import — the guard would silently cover three of four, and a test could spawn a
# real `ava cluster update` with nothing failing to say so.
from ops._update_shell import (
    _restart_recovery_cmd as _restart_recovery_cmd,
)
from ops.cluster_deploy import (
    _UPDATE_LOG_KEEP as _UPDATE_LOG_KEEP,
)
from ops.cluster_deploy import (
    _VALIDATE_FETCH_TIMEOUT_S as _VALIDATE_FETCH_TIMEOUT_S,
)
from ops.cluster_deploy import (
    ClusterUpdateInProgress as ClusterUpdateInProgress,
)
from ops.cluster_deploy import (
    NothingToUpdate as NothingToUpdate,
)
from ops.cluster_deploy import (
    _assert_no_orchestration_in_flight as _assert_no_orchestration_in_flight,
)
from ops.cluster_deploy import (
    _new_update_log as _new_update_log,
)
from ops.cluster_deploy import (
    spawn_restart as spawn_restart,
)
from ops.cluster_deploy import (
    spawn_rollout as spawn_rollout,
)
from ops.cluster_deploy import (
    spawn_update as spawn_update,
)
from ops.cluster_pause import (
    is_paused as is_paused,
)
from ops.cluster_pause import (
    pause_local_cluster as pause_local_cluster,
)
from ops.cluster_pause import (
    unpause_local_cluster as unpause_local_cluster,
)
from ops.cluster_session import (
    _CLUSTER_RESTART_SERVICE as _CLUSTER_RESTART_SERVICE,
)
from ops.cluster_session import (
    _ORCHESTRATION_KINDS as _ORCHESTRATION_KINDS,
)
from ops.cluster_session import (
    _REPO_ROOT as _REPO_ROOT,
)
from ops.cluster_session import (
    _ROLLOUT_SERVICE as _ROLLOUT_SERVICE,
)
from ops.cluster_session import (
    _UPDATER_SERVICE as _UPDATER_SERVICE,
)
from ops.cluster_session import (
    OrchestrationKind as OrchestrationKind,
)
from ops.cluster_session import (
    OrchestrationSpawnFailed as OrchestrationSpawnFailed,
)
from ops.cluster_session import (
    _has_orchestration_session as _has_orchestration_session,
)
from ops.cluster_session import (
    _native_arg as _native_arg,
)
from ops.cluster_session import (
    _spawn_detached_session as _spawn_detached_session,
)
from ops.cluster_session import (
    current_orchestration as current_orchestration,
)
from ops.cluster_status import (
    _AGENT_SESSION_RE as _AGENT_SESSION_RE,
)
from ops.cluster_status import (
    _AGENT_SHELL_RE as _AGENT_SHELL_RE,
)
from ops.cluster_status import (
    _CLUSTER_SESSION_PREFIX as _CLUSTER_SESSION_PREFIX,
)
from ops.cluster_status import (
    ClusterStatus as ClusterStatus,
)
from ops.cluster_status import (
    _check_pidfile as _check_pidfile,
)
from ops.cluster_status import (
    _collect_sessions as _collect_sessions,
)
from ops.cluster_status import (
    _count_agent_shells as _count_agent_shells,
)
from ops.cluster_status import (
    _group_agent_sessions as _group_agent_sessions,
)
from ops.cluster_status import (
    agent_shell_sessions as agent_shell_sessions,
)
from ops.cluster_status import (
    status_snapshot as status_snapshot,
)
from ops.rpc_schemas import SessionInfo as SessionInfo
from ops.update_check import (
    UpdateCheck as UpdateCheck,
)
from ops.update_check import (
    _git_ro as _git_ro,
)
from ops.update_check import (
    update_check as update_check,
)
from ops.updater_reap import (
    _UPDATER_STALL_TIMEOUT_S as _UPDATER_STALL_TIMEOUT_S,
)
from ops.updater_reap import (
    REAP_CLEARED_QUALIFIER as REAP_CLEARED_QUALIFIER,
)
from ops.updater_reap import (
    _reap_stalled_updater as _reap_stalled_updater,
)
from ops.updater_reap import (
    _updater_hung as _updater_hung,
)
from ops.updater_reap import (
    reap_stalled_updater_if_hung as reap_stalled_updater_if_hung,
)

__all__ = [
    "REAP_CLEARED_QUALIFIER",
    "ClusterStatus",
    "ClusterUpdateInProgress",
    "NothingToUpdate",
    "OrchestrationSpawnFailed",
    "SessionInfo",
    "UpdateCheck",
    "current_orchestration",
    "is_paused",
    "pause_local_cluster",
    "reap_stalled_updater_if_hung",
    "spawn_restart",
    "spawn_rollout",
    "spawn_update",
    "status_snapshot",
    "unpause_local_cluster",
    "update_check",
]
