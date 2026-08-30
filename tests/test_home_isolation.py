"""The test process must never hold the operator's real home or credentials.

`tests/conftest.py` redirects AVA_HOME to a tmpfs dir *before* the first project
import, because `shared.dotenv_boot` binds `AVA_ENV_PATH` once at import time and
`_enforce_cluster_env_authority()` then force-assigns every `derived_env_keys()`
entry from whatever `.env` that path resolved to. Get the ordering wrong and the
suite silently runs against `~/.ava` — production cluster secret, db/redis URLs
and gateway URL all live in the test process.

The conftest assertion catches the specific mistake of importing a project module
too early. These tests catch the *outcome* regardless of mechanism, so a future
change that reintroduces the leak by some other route (a plugin that imports
early, a new `.env` reader, a pytest plugin loaded ahead of conftest) still fails
loudly. Both halves matter: the assertion says "you broke the rule", these say
"the rule stopped being true".

History: three separate `tests/cli` runs on one dev box rewrote the operator's
real `~/.ava/.env` on 2026-07-28/29, one of them during a production rollout.

**These tests only speak when something runs before them in the same process.** Two
of the four assert equality against a sentinel that any earlier test could overwrite,
which is what makes them a leak detector rather than a self-check — and also what
makes them silent in the run shape that gates merges: CI's backend job passes
`--ignore=tests/e2e`, so the package most able to leak never shares a process with
this file there, and under `-n auto` a shared process is not guaranteed for anything.
On 2026-07-29 that combination let a real leak (`tests/e2e/conftest.py`'s
`_e2e_process_env` declared `scope="session"`, so its env restore fired at end of
*session* rather than on leaving `tests/e2e/`) fail this file on every serial local
run while CI stayed green — and the only reader who saw the red learned to write it
off. Keeping it fixed takes two things that are easy to mistake for one:
`test_the_e2e_env_fixture_tears_down_when_pytest_leaves_its_package` below (the
package scope AND the `__init__.py` that makes package scope mean anything), and the
e2e CI job running `tests/e2e/ tests/test_home_isolation.py` in one serial worker so
this file can fail where merges are gated.
"""

from __future__ import annotations

import ast
import importlib
import os
from collections.abc import Mapping
from pathlib import Path

import shared.dotenv_boot

_E2E_CONFTEST = Path(__file__).parent / "e2e" / "conftest.py"


def _real_home_env() -> Path:
    return Path.home() / ".ava" / ".env"


def _e2e_restore_list() -> frozenset[str]:
    """The `_env_keys` tuple `_e2e_process_env` saves and restores, read out of the
    source rather than imported: importing `tests.e2e.conftest` pulls in playwright
    and the rest of the e2e stack, which the backend job has no reason to load."""
    tree = ast.parse(_E2E_CONFTEST.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "_env_keys" for t in node.targets):
            continue
        assert isinstance(node.value, ast.Tuple), "_env_keys is no longer a literal tuple"
        keys = {
            e.value
            for e in node.value.elts
            if isinstance(e, ast.Constant) and isinstance(e.value, str)
        }
        assert len(keys) == len(node.value.elts), "_env_keys holds a non-string entry"
        return frozenset(keys)
    raise AssertionError(f"no `_env_keys` assignment found in {_E2E_CONFTEST}")


def test_env_path_is_not_the_operators_real_dotenv() -> None:
    """The bound `.env` path is the session's tmpfs home, not `~/.ava/.env`.

    This is the single fact everything else follows from — `cli.enroll` writes
    to `AVA_ENV_PATH`, so if it points at the real home a test can rewrite the
    operator's live cluster config (the pre-2026-08-01 `cli.start_refresh` did
    the same on every `ava start`).
    """
    bound = Path(shared.dotenv_boot.AVA_ENV_PATH)
    assert bound != _real_home_env(), (
        f"shared.dotenv_boot.AVA_ENV_PATH is the operator's real .env ({bound}). "
        "AVA_HOME was set too late — see the env block at the top of tests/conftest.py."
    )
    assert bound.parent == Path(os.environ["AVA_HOME"])


def test_cluster_secret_is_the_test_secret_not_the_real_one() -> None:
    """The production cluster secret is not in this process.

    `AVA_CLUSTER_SECRET` is in `derived_env_keys()`, which
    `_enforce_cluster_env_authority()` assigns unconditionally (`os.environ[key] =
    val`) — so a real `.env` on the resolved path does not merely win a tie, it
    overwrites the value conftest set moments earlier.
    """
    assert os.environ["AVA_CLUSTER_SECRET"] == "test-cluster-secret"  # noqa: S105 — the fixture value


def test_data_plane_and_gateway_urls_are_not_the_real_ones() -> None:
    """db/redis/gateway URLs are the suite's own, not whatever this box is enrolled with.

    All three are `derived_env_keys()`. The gateway URL is the one that reaches off
    the box, so it is asserted against the reserved-by-RFC-2606 `.invalid` host
    the suite pins rather than merely "not the real value".
    """
    assert os.environ["AVA_GATEWAY_URL"] == "http://test-gateway.invalid:8000"
    for key in ("AVA_DB_URL", "AVA_REDIS_URL"):
        assert "127.0.0.1" in os.environ[key] or "localhost" in os.environ[key]


def test_config_fetch_is_skipped() -> None:
    """No import-time fetch to a live gateway.

    The config source is role-derived (AVA_CONFIG_SOURCE is gone): this test
    process is agent-runner-only, so without the suite's pin its Settings build
    would call `inject_config_from_gateway()` and make a real authenticated GET
    to whatever gateway URL leaked into the env — at import, before any fixture
    exists, so the autouse `_guard_bootstrap_fetch` cannot reach it. The suite
    pins AVA_CONFIG_FETCH=skip in the conftest env block; asserting it here is
    the guard that the pin stays.
    """
    assert os.environ.get("AVA_CONFIG_FETCH") == "skip"


def test_the_e2e_env_fixture_tears_down_when_pytest_leaves_its_package() -> None:
    """Both halves of the 2026-07-29 fix, asserted together because either alone is a
    no-op.

    `tests/e2e/conftest.py:_e2e_process_env` reassigns `AVA_HOME` / `AVA_GATEWAY_URL`
    and restores them in a `finally`. For that restore to fire when pytest *leaves*
    `tests/e2e/` rather than at end-of-session, two independent things must hold:

    1. the fixture declares `scope="package"`, and
    2. `tests/e2e/` contains an `__init__.py`.

    (2) is the trap. `_pytest.fixtures.get_scope_package` looks for a parent node that
    is a `Package` whose nodeid matches the fixture's, and **returns `node.session`
    when it finds none** — no warning, no error. pytest only builds a `Package` node
    for a directory containing `__init__.py`; every other test directory in this repo
    is collected as a plain `Dir`. So on a package-less directory `scope="package"` is
    a synonym for `scope="session"`, and asserting the keyword alone would pass while
    the leak it claims to prevent is fully intact — which is how this test was first
    written, and it is the same "green test certifying a protection that does not
    exist" shape as the leak itself.

    **What this still does NOT prove**: that the env is clean right now. A second,
    differently-named session-scoped fixture mutating `AVA_HOME` would leave this
    green. The three assertions above are what prove that, and they only speak when
    something ran before them in the same process — which is why the e2e CI job runs
    `tests/e2e/ tests/test_home_isolation.py` in one serial worker. This test is the
    order-independent partner: it fails in every job that collects this file.
    """
    conftest = importlib.import_module("tests.e2e.conftest")
    # `_fixture_function_marker` is where pytest records the declared scope; reading it
    # IS the assertion, so the private access is deliberate rather than a shortcut
    # around a public API (pytest exposes no public reader for it).
    scope = conftest._e2e_process_env._fixture_function_marker.scope
    assert scope == "package", (
        f"_e2e_process_env is {scope!r}-scoped. It reassigns AVA_HOME / "
        "AVA_GATEWAY_URL and restores them on teardown; at session scope that "
        "teardown fires after every other test in the process, so this file's "
        "sentinel assertions fail and the leak guard is disarmed. Keep it 'package'."
    )

    # Resolved from this file rather than `conftest.__file__` (which is `str | None`):
    # the assertion is about `tests/e2e/__init__.py` specifically, and this states it.
    pkg_init = Path(__file__).parent / "e2e" / "__init__.py"
    assert pkg_init.is_file(), (
        f"{pkg_init} is missing, so pytest collects tests/e2e as a plain Dir rather "
        "than a Package. `get_scope_package` then falls back to the SESSION node and "
        "the package-scoped fixture above silently behaves as session-scoped — the "
        "leak is back with the keyword still reading 'package'."
    )


def test_the_e2e_fixture_restores_every_env_key_it_assigns() -> None:
    """`_env_keys` must cover every env var `_e2e_process_env` assigns during setup.

    Correct scope only makes the restore fire at the right *time*; it says nothing
    about the restore being complete. `_env_keys` is a hand-written tuple sitting
    thirty lines above a body that assigns keys one at a time, and the two drifted:
    `AVA_GATEWAY_HEALTH_URL` and `AVA_LAUNCH_CONFIRM_TIMEOUT_SECONDS` were assigned
    and never listed, so they leaked out of the package exactly as `AVA_HOME` did —
    a correctly-scoped teardown restoring seven of ten keys.

    Derived from the body by AST rather than compared against a second hand-written
    list, for the same reason the fixture derives `_health_port_keys` from
    `_HEALTH_PORT_OVERRIDES`: a list maintained by hand in two places has already
    drifted once. Setup assignments only — the writes after the `yield` are the
    restore itself.
    """
    lint = importlib.import_module("scripts.lint_fixture_scope")
    assigned, dynamic = lint.setup_env_keys(
        _E2E_CONFTEST.read_text(encoding="utf-8"), "_e2e_process_env"
    )
    assert not dynamic, (
        f"_e2e_process_env assigns os.environ under a computed key ({sorted(dynamic)}), so "
        "the set of keys needing restoration cannot be derived and this guard would pass "
        "vacuously. Assign literal keys, or restore by snapshotting the whole environment."
    )
    restored = _e2e_restore_list()
    assert assigned <= restored, (
        f"_e2e_process_env assigns {sorted(assigned - restored)} but does not list them in "
        "`_env_keys`, so its teardown leaves them installed for every test that runs after "
        "tests/e2e/ in the same process. Add them to the tuple."
    )


def test_no_e2e_value_for_a_restored_key_survives_into_this_file(
    pristine_env: Mapping[str, str],
) -> None:
    """Every key `_e2e_process_env` restores is back at its pre-fixture value here.

    The partner to the two tests above, and the only one that observes the restore
    actually happening: they read the declaration (scope, `__init__.py`, key list),
    this one reads the environment. It is therefore the one that would still fail if
    the leak returned by some route nobody predicted — a second fixture touching the
    same keys, a teardown that raises before its restore, a pytest change to package
    node resolution.

    Like the sentinel assertions above it only speaks when something ran before it in
    the same process, which is why the e2e CI job runs `tests/e2e/
    tests/test_home_isolation.py` in one serial worker. In the backend job (which
    passes `--ignore=tests/e2e`) nothing has touched these keys and it passes trivially
    — that is the correct behavior, not a gap, because the two order-independent tests
    above cover that job.

    Baseline is `pristine_env` from the root conftest rather than literal expected
    values: the assertion then holds for keys added to `_env_keys` later without this
    test being edited, and it names the actual divergence instead of a guess at one.
    """
    for key in sorted(_e2e_restore_list()):
        assert os.environ.get(key) == pristine_env.get(key), (
            f"{key} is {os.environ.get(key)!r}, not the {pristine_env.get(key)!r} this "
            "session started with. Something with a scope broader than the directory it "
            "was written for assigned it and did not put it back before pytest got here "
            "— see tests/e2e/conftest.py:_e2e_process_env for the shape, and "
            "scripts/lint_fixture_scope.py for the lint that is supposed to prevent it."
        )
