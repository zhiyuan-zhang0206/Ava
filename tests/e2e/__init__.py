"""Present so pytest collects `tests/e2e/` as a `Package`, not a plain `Dir`.

**Do not delete this file as cruft.** It is one half of a fix, and removing it re-opens
the leak silently — no error, no warning, and the `scope="package"` keyword in
`conftest.py` still reading correctly.

`conftest.py:_e2e_process_env` reassigns a set of process-global env vars (`AVA_HOME`,
`AVA_GATEWAY_URL`, ...) — its `_env_keys` tuple is the list — and restores them on
teardown. It is `scope="package"` so that
teardown fires when pytest *leaves this directory* rather than at end-of-session —
otherwise every test collected after `tests/e2e/` in the same process keeps running
with the e2e values installed, which is what broke `tests/test_home_isolation.py` on
every serial run (on `main`, while CI stayed green because its backend job passes
`--ignore=tests/e2e`).

But `_pytest.fixtures.get_scope_package` resolves a package-scoped fixture by walking
the node's parents for a `Package` whose nodeid matches, and **falls back to
`node.session` when it finds none**. pytest builds a `Package` node only for a
directory containing `__init__.py`. No other test directory in this repo has one, so
without this file `tests/e2e/` is a `Dir`, the lookup falls through, and
`scope="package"` becomes an exact synonym for `scope="session"`.

`tests/test_home_isolation.py` asserts this file exists, for that reason.
"""
