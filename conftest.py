"""Repo-root pytest bootstrap: the suite's fixtures and session hooks load as plugins.

`pytest_plugins` (pytest accepts it in a root conftest only) and the re-export of
the data-plane fixtures belong here.
Living at the repo root, these plugins apply to every test file in the repository
instead of just the `tests/` tree, so a test in any package directory gets the
same isolation guards as one under `tests/`. Ordinary fixture dependencies
admit configuration and throwaway resources only for their real consumers.

The data-plane and guard fixtures that tests outside `tests/` request are imported by
name from `provisioning` and `guards` (after `env_bootstrap`, the first statement of this file), so
the root conftest is their lexical scope: a package's tests reach them as an
ancestor conftest's fixtures and take no dependency on the `tests/` tree.
`provisioning` and `guards` stay plugins as well, because they own session hooks and
autouse fixtures whose registration order is fixed below. Only non-autouse fixtures
may be imported here: this module registers before every plugin, which would put
an autouse fixture ahead of `leak_guard`.

Order is load-bearing:

1. `env_bootstrap` first. It redirects `AVA_HOME`, pins the cluster-scope
   environment without building Settings; isolation must happen before any project
   module is imported, and it fails the run loudly when a project module was
   already loaded (`_assert_env_precedes_project_imports`).
2. `leak_guard` second, ahead of every other function-scoped autouse fixture:
   pytest tears fixtures down in reverse setup order, so the first one set up
   is the last torn down, and the guard then compares process-global state after
   every function-scoped fixture (`monkeypatch` included) has restored what it
   recorded. It imports only the standard library and pytest, so it may sit
   before the plugins that import project modules.
3. `identity_restore` and `plugin_registrations` before `provisioning` and
   `guards`: pytest sets same-scope autouse fixtures up in registration order,
   so these two restore-after-every-test fixtures see (and put back) whatever
   the later autouse fixtures do. The identity guard receives its SDK context
   slot through a consumer-local fixture override; unrelated tests have no slot.
4. `static_environment` loads before its mode consumer `provisioning`.
   `guards` also owns the shared native driver boundary.
   The default driver refusal applies to every test; resource fixtures open
   their own access and cleanup windows. Static processes never admit resources.
   Heavy CLI and Gateway behavior guards bind beside their actual consumers.
5. The remaining hook plugins and pytester subprocess harness follow.
   Opt-in unit, SDK, model, serving, logging and retry fixtures bind beside consumers.
"""

import pytest

# These plugins are imported below, ahead of the `pytest_plugins` pass that would have
# marked them; registering them here keeps pytest's assertion rewriting for them.
pytest.register_assert_rewrite(
    "tests.fixtures.env_bootstrap",
    "tests.fixtures.unit.static_environment",
    "tests.fixtures.provisioning",
    "tests.fixtures.guards",
)

import tests.fixtures.env_bootstrap  # noqa: E402, F401  # pyright: ignore[reportUnusedImport] -- isolation first
from tests.fixtures.guards import (  # noqa: E402
    guard_service_readiness,
    homes_get_their_own_ports,
    permissions_helper_native_io,
    postgres_access,
    redis_access,
)
from tests.fixtures.provisioning import (  # noqa: E402
    adb_conn,
    aops_pool,
    cluster_defaults_unset,
    database,
    database_gate,
    db_conn,
    db_url,
    event_bus,
    gateway_data_plane,
    insert_inbound,
    publish_wake,
    redis_url,
)

__all__ = [
    "adb_conn",
    "aops_pool",
    "cluster_defaults_unset",
    "database",
    "database_gate",
    "db_conn",
    "db_url",
    "event_bus",
    "gateway_data_plane",
    "guard_service_readiness",
    "homes_get_their_own_ports",
    "insert_inbound",
    "permissions_helper_native_io",
    "postgres_access",
    "publish_wake",
    "redis_access",
    "redis_url",
]

pytest_plugins = [
    "tests.fixtures.env_bootstrap",
    "tests.fixtures.leak_guard",
    "tests.fixtures.identity_restore",
    "tests.fixtures.unit.plugin_registrations",
    "tests.fixtures.unit.static_environment",
    "tests.fixtures.provisioning",
    "tests.fixtures.guards",
    # Directory-level fixtures that follow their tests (`PATH_SCOPES`), not a conftest.
    "tests.fixtures.path_scopes",
    # Stall forensics (task #3513: the asyncio probe under `-o faulthandler_timeout=N`)
    # and the split-directory collection guard — see each docstring.
    "tests._asyncio_stall_probe",
    "tests.fixtures.collection_guard",
    "pytester",
]
