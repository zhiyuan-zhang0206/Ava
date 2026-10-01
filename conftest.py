"""Repo-root pytest bootstrap: the suite's fixtures and session hooks load as plugins.

Only `pytest_plugins` belongs here (pytest accepts it in a root conftest only).
Living at the repo root, these plugins apply to every test file in the repository
instead of just the `tests/` tree, so a test in any package directory gets the
same isolation guards and throwaway data plane as one under `tests/`.

Order is load-bearing:

1. `env_bootstrap` first. It redirects `AVA_HOME`, pins the cluster-scope
   environment, and builds Settings; all of that must happen before any project
   module is imported, and it fails the run loudly when a project module was
   already loaded (`_assert_env_precedes_project_imports`).
2. `leak_guard` second, ahead of every other function-scoped autouse fixture:
   pytest tears fixtures down in reverse setup order, so the first one set up
   is the last torn down, and the guard then compares process-global state after
   every function-scoped fixture (`monkeypatch` included) has restored what it
   recorded. It imports only the standard library and pytest, so it may sit
   before the plugins that import project modules.
3. `identity_restore` third, right after the guard and ahead of every other
   autouse fixture: it sets up after the guard, so it tears down before the
   guard compares (the guard then finds the identity slots back), and its window
   covers whatever the autouse fixtures after it do to those slots.
4. `plugin_registrations` before `provisioning` and `guards`: pytest sets
   same-scope autouse fixtures up in registration order, and this one has
   always run first of them.
5. `provisioning` before `guards`: within `guards`, autouse fixtures are set up
   alphabetically, and `_clean_state` (in `provisioning`) sorts ahead of the
   `_guard_*` fixtures. The hooks in `provisioning` also stay registered
   before `collection_guard` and the stall probe, as they were.
6. The opt-in fixture modules and the two hook-only plugins follow.
"""

pytest_plugins = [
    "tests.fixtures.env_bootstrap",
    "tests.fixtures.leak_guard",
    "tests.fixtures.identity_restore",
    "tests.fixtures.plugin_registrations",
    "tests.fixtures.provisioning",
    "tests.fixtures.guards",
    "tests.fixtures.units",
    "tests.fixtures.milvus",
    "tests.fixtures.log_capture",
    "tests.fixtures.retry_waits",
    # Directory-level fixtures that follow their tests (`PATH_SCOPES`), not a conftest.
    "tests.fixtures.path_scopes",
    # Stall forensics (task #3513: the asyncio probe under `-o faulthandler_timeout=N`)
    # and the split-directory collection guard — see each docstring.
    "tests._asyncio_stall_probe",
    "tests.fixtures.collection_guard",
]
