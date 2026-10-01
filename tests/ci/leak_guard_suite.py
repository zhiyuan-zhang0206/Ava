"""The synthetic suite `test_leak_guard.py` runs real pytest on: known leakers, their victims, clean controls.

It is source text, not collected tests: each file is written into a scratch rootdir that has a
stand-in first-party package (`leakdemo`) and its own `tests/` directory, so the guard sees the same
split as in this repository (a production module under the rootdir, test modules below a `tests`
directory). The `leaker_*` tests are named so the driver can assert the guard blames exactly them;
every `clean_*` test is a write-up of an idiom this repository uses and must produce no finding.
"""

from __future__ import annotations

from pathlib import Path

# The stand-in first-party package: what the real leakers touch, in miniature.
PACKAGE = {
    "leakdemo/__init__.py": '"""Stand-in first-party package."""\n',
    # The boot pass force-assigns a derived key, like `load_ava_env`.
    "leakdemo/boot.py": """
import os


def load_env() -> None:
    os.environ["DEMO_HEALTH_PORT"] = "8123"
""",
    # The framework-internal identity slot (the real one is `ava.agent_identity`).
    "leakdemo/identity.py": """
_id: int | None = None


def agent_id() -> int | None:
    return _id
""",
    # A plugin namespace module (stand-in for `ava_builtins.plugins.ava_code._code_namespace`).
    "leakdemo/nsmod.py": '"""A namespace module."""\n',
    # Stand-in for `ava.sdk_surface.plugins.register_namespace`: it marks the module object.
    "leakdemo/registry.py": """
from types import ModuleType


def register_namespace(module: ModuleType, name: str) -> None:
    module._qualname = f"leakdemo.{name}"  # type: ignore[attr-defined]
""",
    # Stand-in for `ava.self`: AGENT_ID is served by the module `__getattr__`, never stored.
    "leakdemo/selfmod.py": """
from leakdemo import identity

AGENT_ID: int


def __getattr__(name: str) -> object:
    if name == "AGENT_ID":
        return identity.agent_id()
    raise AttributeError(name)
""",
    # A lazily-importing package: `__getattr__` imports a submodule and caches the MODULE.
    "leakdemo/lazy_pkg/__init__.py": """
import importlib


def __getattr__(name: str) -> object:
    if name == "sub":
        module = importlib.import_module("leakdemo.lazy_pkg.sub")
        globals()["sub"] = module
        return module
    raise AttributeError(name)
""",
    "leakdemo/lazy_pkg/sub.py": "VALUE = 7\n",
}

PYTEST_INI = """
[pytest]
pythonpath = .
python_files = test_*.py
asyncio_mode = auto
"""

# `LEAK_SUITE_FAULT` injects a fault into the guard itself (a comma-separated list of the stages to break).
CONFTEST = '''
"""Suite-shaped fixtures around the guard: the situations that must NOT be reported."""

import os
from collections.abc import Iterator

import pytest

import leakdemo.identity  # the stand-in slot must be loaded before the first test, whatever file runs
from tests.fixtures import identity_restore, leak_guard

# The suite's own singleton stands in for the agent identity slots, for the guard and the restore alike.
leak_guard.WATCHED_ATTRS = (("leakdemo.identity", ("_id",)),)
leak_guard.WATCHED_CONTEXTVARS = ()
identity_restore.IDENTITY_SLOTS = (("leakdemo.identity", ("_id",)),)
identity_restore.IDENTITY_CONTEXTVARS = ()


class _Boom(list):
    def append(self, _item):
        raise RuntimeError("injected fault")


def _explode(*_args, **_kwargs):
    raise RuntimeError("injected fault")


_FAULTS = set(filter(None, os.environ.get("LEAK_SUITE_FAULT", "").split(",")))
if "snapshot" in _FAULTS:  # a renamed identity slot: the registry names a name that is gone
    leak_guard.WATCHED_ATTRS = (("leakdemo.identity", ("_gone",)),)
if "compare" in _FAULTS:
    leak_guard._RUN.compare = _explode
if "collect" in _FAULTS:  # the controller cannot record what the workers found
    leak_guard._RUN.findings = _Boom()
if "summary" in _FAULTS:
    leak_guard._RUN.stats = _explode


def pytest_runtest_setup(item):
    if "report" in _FAULTS:  # the JUnit property cannot be written
        item.user_properties = _Boom(item.user_properties)


@pytest.fixture(scope="session", autouse=True)
def _lazy_provisioned_env() -> Iterator[None]:
    # Like `_provisioned_db`: sets os.environ inside the FIRST test's setup, undoes it at session end.
    os.environ["DEMO_LAZY_URL"] = "postgresql://demo"
    yield
    os.environ.pop("DEMO_LAZY_URL", None)


@pytest.fixture(autouse=True)
def _per_test_autouse(monkeypatch: pytest.MonkeyPatch) -> None:
    # Like `_clean_state` / `_otlp_export_off`: a function-scoped autouse fixture that patches.
    monkeypatch.setenv("DEMO_PER_TEST", "1")
'''

KNOWN_LEAKS = '''
"""The known leak classes: a leaker, then the victim that only fails because of it."""

import os
from unittest import mock

import pytest

from leakdemo import boot, identity, nsmod, registry, selfmod


# ---- class 1: monkeypatch.setattr on a name the module serves from __getattr__ (PR #3791)
def test_leaker_setattr_getattr_served_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(selfmod, "AGENT_ID", 1)
    assert selfmod.AGENT_ID == 1


def test_victim_identity_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(identity, "_id", 99)  # what establish(99) does
    assert selfmod.AGENT_ID == 99  # a stored AGENT_ID=1 would win over __getattr__


# ---- class 2: delenv(raising=False) on an absent key, then code under test sets it (PR #3793)
def test_leaker_delenv_raising_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEMO_HEALTH_PORT", raising=False)
    boot.load_env()
    assert os.environ["DEMO_HEALTH_PORT"] == "8123"


def test_victim_expects_unset_port() -> None:
    assert "DEMO_HEALTH_PORT" not in os.environ


# ---- class 3: code under test marks a module object and nothing takes the mark off
def test_leaker_register_namespace() -> None:
    registry.register_namespace(nsmod, "code")
    assert nsmod._qualname == "leakdemo.code"  # type: ignore[attr-defined]


def test_victim_plain_namespace_name() -> None:
    assert not hasattr(nsmod, "_qualname")  # help() would render the stale heading


# ---- the same three situations written so they do not leak
def test_clean_setitem_vars_for_getattr_served_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(vars(selfmod), "AGENT_ID", 1)
    assert selfmod.AGENT_ID == 1


def test_clean_mock_patch_object_on_getattr_served_name() -> None:
    with mock.patch.object(selfmod, "AGENT_ID", 1):
        assert selfmod.AGENT_ID == 1


def test_clean_setenv_empty_before_delenv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_HEALTH_PORT", "")
    monkeypatch.delenv("DEMO_HEALTH_PORT")
    boot.load_env()


def test_clean_preregistered_module_attr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(nsmod, "_qualname", "placeholder", raising=False)
    registry.register_namespace(nsmod, "code")


# ---- class 4: the identity slot assigned bare, the pattern 300+ test sites use
def test_leaker_bare_identity_assignment() -> None:
    identity._id = 7


def test_victim_stale_identity() -> None:
    assert identity._id is None  # the session's identity, not the 7 the test before it left


def test_clean_identity_via_monkeypatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(identity, "_id", 8)


def test_clean_identity_try_finally() -> None:
    original = identity._id
    identity._id = 9
    try:
        assert identity._id == 9
    finally:
        identity._id = original
'''

OTHER_STATE = '''
"""cwd / signal handler / sys.path: a leaker for each, plus the idiom that does not leak."""

import os
import signal
import sys

import pytest


def test_leaker_cwd(tmp_path) -> None:
    os.chdir(tmp_path)


def test_clean_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)


def test_clean_cwd_try_finally(tmp_path) -> None:
    before = os.getcwd()
    os.chdir(tmp_path)
    try:
        assert os.getcwd() == str(tmp_path.resolve())
    finally:
        os.chdir(before)


def _handler(signum, frame) -> None:
    raise KeyboardInterrupt


def test_leaker_signal_handler() -> None:
    signal.signal(signal.SIGUSR1, _handler)


def test_clean_signal_handler_restored() -> None:
    previous = signal.signal(signal.SIGUSR2, _handler)
    signal.signal(signal.SIGUSR2, previous)


def test_note_syspath_leak(tmp_path) -> None:
    sys.path.insert(0, str(tmp_path))


def test_clean_syspath_prepend(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
'''

CLEAN_PATTERNS = '''
"""Everything here is clean and must produce ZERO findings (false-positive controls)."""

import asyncio
import os
from collections.abc import Iterator
from unittest import mock

import pytest

from leakdemo import selfmod


@pytest.fixture(scope="module")
def module_env() -> Iterator[None]:
    # A module-scoped fixture that sets env for its tests and undoes it in its own finalizer.
    os.environ["DEMO_MODULE_SCOPED"] = "1"
    yield
    os.environ.pop("DEMO_MODULE_SCOPED", None)


@pytest.fixture(scope="class")
def class_env() -> Iterator[None]:
    os.environ["DEMO_CLASS_SCOPED"] = "1"
    yield
    os.environ.pop("DEMO_CLASS_SCOPED", None)


@pytest.fixture
def restoring_env_fixture() -> Iterator[None]:
    os.environ["DEMO_FIXTURE_SCOPED"] = "1"
    yield
    del os.environ["DEMO_FIXTURE_SCOPED"]


@pytest.fixture
def finalizer_fixture(request: pytest.FixtureRequest) -> None:
    os.environ["DEMO_FINALIZER"] = "1"
    request.addfinalizer(lambda: os.environ.pop("DEMO_FINALIZER", None))


def test_module_scoped_first_user(module_env) -> None:
    assert os.environ["DEMO_MODULE_SCOPED"] == "1"


def test_module_scoped_last_user(module_env) -> None:  # the finalizer runs in THIS test's teardown
    assert os.environ["DEMO_MODULE_SCOPED"] == "1"


class TestClassScoped:
    def test_a(self, class_env) -> None:
        assert os.environ["DEMO_CLASS_SCOPED"] == "1"

    def test_b(self, class_env) -> None:
        assert os.environ["DEMO_CLASS_SCOPED"] == "1"


class TestSetupMethods:
    def setup_method(self) -> None:
        os.environ["DEMO_SETUP_METHOD"] = "1"

    def teardown_method(self) -> None:
        del os.environ["DEMO_SETUP_METHOD"]

    def test_state_is_put_back_by_teardown_method(self) -> None:
        assert os.environ["DEMO_SETUP_METHOD"] == "1"


def test_session_fixture_state_is_in_the_baseline() -> None:
    assert os.environ["DEMO_LAZY_URL"].startswith("postgresql")


def test_autouse_function_fixture_with_monkeypatch() -> None:
    assert os.environ["DEMO_PER_TEST"] == "1"


def test_function_fixture_that_restores_its_env(restoring_env_fixture) -> None:
    assert os.environ["DEMO_FIXTURE_SCOPED"] == "1"


def test_fixture_that_registers_a_finalizer(finalizer_fixture) -> None:
    assert os.environ["DEMO_FINALIZER"] == "1"


def test_monkeypatch_setenv_delenv_chdir(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("DEMO_A", "1")
    monkeypatch.setenv("DEMO_PER_TEST", "2")
    monkeypatch.delenv("DEMO_PER_TEST")
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))


def test_monkeypatch_setitem_on_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(os.environ, "DEMO_SETITEM", "1")


def test_patch_dict_environ() -> None:
    with mock.patch.dict(os.environ, {"DEMO_B": "1"}):
        assert os.environ["DEMO_B"] == "1"


def test_patch_dict_clear_environ() -> None:
    with mock.patch.dict(os.environ, {"DEMO_ONLY": "1"}, clear=True):
        assert list(os.environ) == ["DEMO_ONLY"]


def test_in_body_env_with_finally() -> None:
    os.environ["DEMO_C"] = "1"
    try:
        assert os.environ["DEMO_C"] == "1"
    finally:
        del os.environ["DEMO_C"]


def test_lazy_submodule_import_binds_into_parent() -> None:
    from leakdemo import lazy_pkg

    assert lazy_pkg.sub.VALUE == 7  # `sub` (a module) lands in lazy_pkg.__dict__: not a leak


def test_lazy_import_of_new_first_party_module() -> None:
    import importlib

    importlib.import_module("leakdemo.lazy_pkg.sub")


def test_setattr_on_ordinary_attribute(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(selfmod, "identity", object())


def test_tmp_path_and_capsys(tmp_path, capsys) -> None:
    (tmp_path / "x.txt").write_text("x", encoding="utf-8")
    print("hello")
    assert capsys.readouterr().out == "hello\\n"


@pytest.mark.parametrize("n", [1, 2, 3])
def test_parametrized(n: int, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_PARAM", str(n))


async def test_async_test(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_ASYNC", "1")
    await asyncio.sleep(0)


@pytest.mark.xfail(strict=True, reason="a failing test must not add findings of its own")
def test_failing_test_is_quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_FAIL", "1")
    raise AssertionError("expected")


@pytest.mark.skip(reason="skipped tests never run the guard body")
def test_skipped() -> None:
    raise AssertionError("never runs")
'''

# The ordering experiment: the same guard registered first or last among two function-scoped autouse
# plugins. Registered last it compares BEFORE the other plugin's monkeypatch undo.
ORDER_PLUGIN = """
import pytest


@pytest.fixture(autouse=True)
def _other_autouse(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_ORDER", "1")
"""

ORDER_CONFTEST = """
pytest_plugins = {plugins!r}
"""

ORDER_TESTS = """
import os

import pytest

from leakdemo import selfmod


def test_clean_uses_the_other_fixture() -> None:
    assert os.environ["DEMO_ORDER"] == "1"


def test_leaker_setattr_getattr_served_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(selfmod, "AGENT_ID", 1)


def test_clean_monkeypatch_setenv_in_the_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_IN_BODY", "1")
"""


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.lstrip("\n"), encoding="utf-8")


def write_suite(root: Path) -> None:
    """The main suite: every leak class, its victim, the other-state leakers and the clean controls."""
    for rel, text in PACKAGE.items():
        _write(root, rel, text)
    _write(root, "pytest.ini", PYTEST_INI)
    _write(root, "leakdemo/tests/conftest.py", CONFTEST)
    _write(root, "leakdemo/tests/test_known_leaks.py", KNOWN_LEAKS)
    _write(root, "leakdemo/tests/test_other_state.py", OTHER_STATE)
    _write(root, "leakdemo/tests/test_clean_patterns.py", CLEAN_PATTERNS)


def write_order_suite(root: Path, plugins: list[str]) -> None:
    """The ordering experiment, with the plugins registered in `plugins` order."""
    for rel, text in PACKAGE.items():
        _write(root, rel, text)
    _write(root, "pytest.ini", PYTEST_INI)
    _write(root, "order_other.py", ORDER_PLUGIN)
    _write(root, "conftest.py", ORDER_CONFTEST.format(plugins=plugins))
    _write(root, "leakdemo/tests/test_order.py", ORDER_TESTS)
