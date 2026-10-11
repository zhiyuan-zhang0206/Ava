"""Repo-root pytest bootstrap: the suite's fixtures and session hooks load as plugins.

Only `pytest_plugins` belongs here (pytest accepts it in a root conftest only).
Living at the repo root, these plugins apply to every test file in the repository
instead of just the `tests/` tree, so a test in any package directory gets the
same isolation guards as one under `tests/`. Ordinary fixture dependencies
admit configuration and throwaway resources only for their real consumers.

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

pytest_plugins = [
    "tests.fixtures.env_bootstrap",
    "tests.fixtures.leak_guard",
    "tests.fixtures.identity_restore",
    "tests.fixtures.plugin_registrations",
    "tests.fixtures.static_environment",
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
