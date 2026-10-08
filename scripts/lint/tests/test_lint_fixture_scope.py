"""`scripts/lint/fixture_scope.py` — a fixture's scope versus the blast radius of
what it mutates.

The cases cover both directions of the scope rules, the provisioning exemption,
setup environment-key discovery and unreadable directory members. Synthetic
fixtures preserve the session-scope mutation and missing-package defects.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

_lint = importlib.import_module("scripts.lint.fixture_scope")


def _findings(src: str, rel: str = "tests/sub/conftest.py", *, init: bool = False) -> list[str]:
    return [msg for _, msg in _lint.findings_in_source(src, rel, has_package_init=init)]


# ---- Rule 1: session scope + a process-global mutation, outside the provisioning plugin ----


def test_session_scoped_env_write_in_a_subdirectory_conftest_is_flagged() -> None:
    # The shape of the real defect, reduced: the restore is present and correct, and
    # fires at end-of-session rather than on leaving tests/sub/.
    src = (
        '@pytest.fixture(scope="session", autouse=True)\n'
        "def _process_env():\n"
        '    prev = os.environ.get("AVA_HOME")\n'
        '    os.environ["AVA_HOME"] = "/tmp/x"\n'
        "    try:\n"
        "        yield\n"
        "    finally:\n"
        '        os.environ["AVA_HOME"] = prev\n'
    )
    found = _findings(src)
    assert len(found) == 1
    assert "os.environ['AVA_HOME']" in found[0]


def test_session_scoped_settings_attribute_write_is_flagged() -> None:
    # Not every process global is an env var — `base.config.settings` is a
    # module-load singleton, and the real fixture reassigned a field on it too.
    src = (
        '@pytest.fixture(scope="session")\n'
        "def _channel():\n"
        "    settings.data_plane.events_channel = 'x'\n"
        "    yield\n"
    )
    assert "settings.data_plane.events_channel" in _findings(src)[0]


def test_environ_pop_is_flagged_although_it_is_a_method_call_not_an_assignment() -> None:
    # `os.environ.pop(...)` unsets a var without any Store node to find, which is why
    # the check does not rely on assignment syntax alone.
    src = (
        '@pytest.fixture(scope="session")\n'
        "def _unset():\n"
        '    os.environ.pop("AVA_MACHINE_SERVE_GATEWAY", None)\n'
        "    yield\n"
    )
    assert "os.environ.pop(...)" in _findings(src)[0]


def test_an_unrecognised_environ_mutator_is_flagged_by_default() -> None:
    # Allow-list polarity: `update` is nowhere in the script. It is caught because
    # it is not on the read-only list, not because anyone predicted it.
    src = (
        '@pytest.fixture(scope="session")\n'
        "def _bulk():\n"
        '    os.environ.update({"AVA_HOME": "/tmp/x"})\n'
        "    yield\n"
    )
    assert "os.environ.update(...)" in _findings(src)[0]


def test_reading_the_environment_is_not_a_mutation() -> None:
    src = (
        '@pytest.fixture(scope="session")\n'
        "def _read():\n"
        '    headed = os.environ.get("HEADED") == "1"\n'
        '    keys = [k for k in os.environ if k.startswith("AVA_")]\n'
        "    yield (headed, keys)\n"
    )
    assert _findings(src) == []


def test_writing_through_an_alias_of_the_environment_is_flagged() -> None:
    # The obvious bypass. `env = os.environ` binds a view, not a copy.
    src = (
        '@pytest.fixture(scope="session")\n'
        "def _aliased():\n"
        "    env = os.environ\n"
        '    env["AVA_HOME"] = "/tmp/x"\n'
        "    yield\n"
    )
    assert "env['AVA_HOME']" in _findings(src)[0]


def test_a_local_object_returned_by_a_call_is_not_an_alias() -> None:
    # `conn = psycopg.connect(...)` is a fresh object; `conn.autocommit = True` is not
    # a process-global write. Treating every attribute assignment on a local as one
    # would make the lint fire on most fixture bodies.
    src = (
        '@pytest.fixture(scope="session")\n'
        "def _conn():\n"
        "    conn = psycopg.connect(settings.data_plane.db_url)\n"
        "    conn.autocommit = True\n"
        "    yield conn\n"
    )
    assert _findings(src) == []


def test_a_module_global_rebound_by_a_global_statement_is_flagged() -> None:
    src = (
        '@pytest.fixture(scope="session")\n'
        "def _cache():\n"
        "    global _CACHED_URL\n"
        '    _CACHED_URL = "postgresql://x"\n'
        "    yield\n"
    )
    assert "global _CACHED_URL" in _findings(src)[0]


def test_a_session_fixture_owning_only_an_expensive_resource_is_not_flagged() -> None:
    # `playwright_browser` / `frontend_proc` / `webkit_browser`: session scope is right
    # (the resource's lifetime IS the session) and the value is handed back through the
    # return, not through a global. This is the case the lint must not tax, or it would
    # need silencing on every legitimate session fixture in the repo.
    src = (
        '@pytest.fixture(scope="session")\n'
        "def playwright_browser(playwright_runtime):\n"
        "    browser = playwright_runtime.chromium.launch(headless=True)\n"
        "    try:\n"
        "        yield browser\n"
        "    finally:\n"
        "        browser.close()\n"
    )
    assert _findings(src) == []


def test_a_function_scoped_fixture_may_mutate_the_environment() -> None:
    # `scenario_env`'s shape: its teardown runs at the end of the test that used it,
    # so nothing downstream ever sees the value. Flagging this would be wrong.
    src = (
        "@pytest.fixture\n"
        "def scenario_env():\n"
        '    prev = os.environ.get("AVA_LLM_OVERRIDE")\n'
        '    os.environ["AVA_LLM_OVERRIDE"] = "x"\n'
        "    try:\n"
        "        yield\n"
        "    finally:\n"
        '        os.environ["AVA_LLM_OVERRIDE"] = prev\n'
    )
    assert _findings(src) == []


# ---- the provisioning-plugin exemption is by LOCATION, not by name ----


_PROVISIONED_DB = (
    '@pytest.fixture(scope="session", autouse=True)\n'
    "def _provisioned_db():\n"
    "    with postgres() as url:\n"
    "        settings.data_plane.db_url = url\n"
    '        os.environ["AVA_DB_URL"] = url\n'
    "        yield url\n"
)


def test_the_provisioning_plugins_session_fixtures_are_not_flagged() -> None:
    # `tests/fixtures/provisioning.py:_provisioned_db` mutates two process globals at
    # session scope and never restores them. That is correct: the repo-root conftest.py
    # loads it once per process, so "the session" and "my directory" are the same blast
    # radius, and it is establishing the session's baseline rather than claiming to
    # clean up after itself. A lint that failed here would be wrong about the one
    # legitimate case in the repo.
    assert (
        _lint.findings_in_source(
            _PROVISIONED_DB, "tests/fixtures/provisioning.py", has_package_init=False
        )
        == []
    )


def test_the_same_fixture_moved_into_a_subdirectory_conftest_is_flagged() -> None:
    # Same bytes, different location — proving the exemption is the position in the
    # tree and not the fixture's name or its lack of a restore.
    found = _findings(_PROVISIONED_DB)
    assert len(found) == 1
    assert "os.environ['AVA_DB_URL']" in found[0]
    assert "settings.data_plane.db_url" in found[0]


# ---- Rule 2: scope="package" is a synonym for session without an __init__.py ----


_PACKAGE_FIXTURE = '@pytest.fixture(scope="package")\ndef _pkg():\n    yield\n'


def test_package_scope_without_an_init_py_is_flagged() -> None:
    # The trap that made the first draft of the real fix a no-op, and the reason this
    # case exists: the diff reads `session` -> `package` and looks complete, while
    # `get_scope_package` falls through to the session node with no warning.
    found = _findings(_PACKAGE_FIXTURE, init=False)
    assert len(found) == 1
    assert "tests/sub/__init__.py does not exist" in found[0]


def test_package_scope_with_an_init_py_is_not_flagged() -> None:
    assert _findings(_PACKAGE_FIXTURE, init=True) == []


def test_package_scope_without_an_init_py_is_also_judged_as_session_scope() -> None:
    # Both rules fire, because the fixture really is session-scoped: the __init__.py
    # finding explains the mechanism and the Rule 1 finding names what leaks.
    src = (
        '@pytest.fixture(scope="package")\n'
        "def _pkg_env():\n"
        '    os.environ["AVA_HOME"] = "/tmp/x"\n'
        "    yield\n"
    )
    assert len(_findings(src, init=False)) == 2
    assert _findings(src, init=True) == []


# ---- polarity: an unreadable scope is reported, not assumed harmless ----


def test_a_non_literal_scope_is_reported() -> None:
    src = "@pytest.fixture(scope=_SCOPE)\ndef _dyn():\n    yield\n"
    assert "non-literal `scope=`" in _findings(src)[0]


def test_a_fixture_with_no_scope_argument_is_function_scoped_and_silent() -> None:
    src = '@pytest.fixture\ndef _f():\n    os.environ["AVA_HOME"] = "/x"\n    yield\n'
    assert _findings(src) == []
    src_called = '@pytest.fixture()\ndef _g():\n    os.environ["AVA_HOME"] = "/x"\n    yield\n'
    assert _findings(src_called) == []


def test_a_plain_function_that_is_not_a_fixture_is_ignored() -> None:
    src = 'def helper():\n    os.environ["AVA_HOME"] = "/x"\n'
    assert _findings(src) == []


# ---- setup_env_keys: the restore-completeness primitive ----


def test_setup_env_keys_ignores_the_teardown_restore_loop() -> None:
    # Writes after the `yield` ARE the restore. Counting them would report the loop
    # variable as an unresolvable key and make the guard in
    # tests/harness/test_home_isolation.py pass vacuously.
    src = (
        '@pytest.fixture(scope="package")\n'
        "def _env():\n"
        "    prev = {k: os.environ.get(k) for k in _env_keys}\n"
        '    os.environ["AVA_HOME"] = "/tmp/x"\n'
        "    try:\n"
        "        yield\n"
        "    finally:\n"
        "        for k, v in prev.items():\n"
        "            os.environ[k] = v\n"
    )
    literal, dynamic = _lint.setup_env_keys(src, "_env")
    assert literal == frozenset({"AVA_HOME"})
    assert dynamic == frozenset()


def test_setup_env_keys_reports_a_computed_setup_key_as_dynamic() -> None:
    src = (
        '@pytest.fixture(scope="package")\n'
        "def _env():\n"
        '    os.environ[f"AVA_{name.upper()}"] = "1"\n'
        "    yield\n"
    )
    literal, dynamic = _lint.setup_env_keys(src, "_env")
    assert literal == frozenset()
    assert dynamic == frozenset({"f'AVA_{name.upper()}'"})


def test_setup_env_keys_covers_pop_as_well_as_assignment() -> None:
    src = (
        '@pytest.fixture(scope="package")\n'
        "def _env():\n"
        '    os.environ["A"] = "1"\n'
        '    os.environ.pop("B", None)\n'
        '    del os.environ["C"]\n'
        "    yield\n"
    )
    literal, _ = _lint.setup_env_keys(src, "_env")
    assert literal == frozenset({"A", "B", "C"})


def test_directory_with_unreadable_member_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unreadable *.py member under tests/ (non-UTF-8 / dangling symlink)
    must be skipped like any unreadable entry — the scan must not crash on it,
    and a violating sibling fixture is still reported."""
    monkeypatch.setattr(_lint, "_REPO_ROOT", tmp_path)
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "ok.py").write_text("value = 1\n", encoding="utf-8")
    (tests / "bad_utf8.py").write_bytes(b"\xff\xfe\x00bad")
    (tests / "dangling.py").symlink_to(tests / "missing.py")
    assert _lint.main([str(tests / "bad_utf8.py")]) == 0
    assert _lint.main([str(tests)]) == 0
    assert _lint.main([]) == 0
    (tests / "sub").mkdir()
    (tests / "sub" / "conftest.py").write_text(
        '@pytest.fixture(scope="session")\n'
        "def _process_env():\n"
        '    os.environ["AVA_HOME"] = "/tmp/x"\n'
        "    yield\n",
        encoding="utf-8",
    )
    assert _lint.main([str(tests)]) == 1
    assert "AVA_HOME" in capsys.readouterr().out


def test_explicit_missing_target_is_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A typo'd explicit path must fail the gate, not pass as a silent empty scan."""
    good = tmp_path / "conftest.py"
    good.write_text("value = 1\n", encoding="utf-8")
    missing = tmp_path / "typo.py"
    assert _lint.main([str(missing)]) == 1
    assert str(missing) in capsys.readouterr().err
    assert _lint.main([str(good), str(missing)]) == 1
