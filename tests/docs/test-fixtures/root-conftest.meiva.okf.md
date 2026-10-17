---
type: doc
title: "Root Conftest Fixture Re-export"
description: "Why the repo-root conftest imports the data-plane fixtures by name, and what may be imported there."
tags:
- evaluation
- quality-assurance
---

# Root Conftest Fixture Re-export

- The repo-root `conftest.py` imports, by name, the `env_bootstrap` (`process_config`, `session_ports`, `distinct_free_ports`), `provisioning` and `guards` fixtures that tests outside `tests/` request (`db_url`, `db_conn`, `database`, `redis_url`, `event_bus`, `postgres_access`, `redis_access`, ...) and lists them in `__all__`. Names are public: a private (underscore) fixture would be a private-name use across files. A request from a package's test then resolves to an ancestor conftest, a lexical scope, and not to a module under `tests/`: the package takes no dependency on the `tests/` tree for them. A fixture newly requested from outside `tests/` is added to that import.
- Only **non-autouse** fixtures may be imported. The conftest module registers before every plugin, so an autouse fixture imported there would set up ahead of `leak_guard` and tear down after the guard compares. `tests/fixtures/tests/test_root_conftest_fixtures.py` enforces it. `provisioning` and `guards` stay in `pytest_plugins` for their session hooks and autouse fixtures, which keeps the registration order in [[test-fixtures.meiva.okf.md]].
- The conftest's first import is from `tests.fixtures.env_bootstrap`, because `provisioning` imports project modules at its top and environment isolation must precede every project import (`_assert_env_precedes_project_imports`). The plugins it imports early (`env_bootstrap`, `static_environment`, `provisioning`, `guards`) are registered for assertion rewriting with `pytest.register_assert_rewrite`, ahead of those imports; without it pytest warns that they were already imported and stops rewriting their asserts.
