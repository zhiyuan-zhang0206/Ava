---
type: doc
title: "Path-scoped Test Fixtures"
description: "Existing fixture visibility during migration from directory conftests."
tags:
- evaluation
- quality-assurance
---

# Path-scoped Test Fixtures

- A conftest's fixtures reach only the tests below it, so a test moved into a package's `tests/` directory would silently lose them. The modules in `tests/path_scoped/` (one per former conftest, plus `api_keys` and `pty_reaper` that several take) are registered for the directories or single test files named by the `path_scopes.toml` files next to the tests (`PATH_SCOPES`, read by `tests/fixtures/path_scopes.py`) when pytest starts collecting that node (`parsefactories(holder=, node=)`, the interface pytest uses for a conftest). A governed test therefore sees exactly the fixture closure it saw under the conftest: the same autouse names in the same order, the same visibility, and a session-scoped autouse fixture (the provider-plugin load for the gateway tests) instantiated only when a governed test runs.
- The table is a migration device: the end state is each package's tests declaring the environment they need, or a local conftest providing it, so entries only come out. Moving a test edits the `path_scopes.toml` of the directory it leaves and of the one it enters (a moved directory carries its own file); `tests/ci/test_path_scopes.py` fails when a listed directory no longer holds a test file, and a listed path that no longer exists stops the run at configure. No count of test files is recorded (two moves that each adjusted it collided on every merge), so a test moved out of a listed directory without its new directory's `path_scopes.toml` naming it loses these fixtures silently: name it in the same change. The test also locks the pytest interface the plugin relies on; a pytest upgrade must re-check it.
- Hooks (`collect_ignore`, `pytest_configure`) are not fixtures and stay in their conftest: `lifecycle/native_root` (its `pytest_configure` must work without the repo-root conftest; the directory holds only that conftest today, and `tests/harness/test_home_isolation.py` asserts the hook defers to the root one), `e2e`.
