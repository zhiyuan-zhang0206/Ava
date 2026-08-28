"""Guard for `ava.register_namespace` + `clear_registered_namespaces` behavior.

Plugin actively adds SDK submodule to ava top level; during framework reload (graph rebuild /
test reload) it cleans up thoroughly — so the next register doesn't conflict on name.

Coverage matrix (after 5 reviewer review-fix additions):
- attach + __all_for_ava__ + help(ava) visible (ModuleType and SimpleNamespace equivalent)
- Exception hierarchy (InvalidName / InvalidModule / FrameworkConflict / PluginConflict
  all under RegisterNamespaceError, mutually exclusive)
- Module type validation (int/None/str → InvalidNamespaceModuleError, fail-fast)
- Name validation (invalid identifier / underscore prefix, raise separately)
- Name collision (framework built-in / top-level attr served by __getattr__ like DB_URL / another plugin)
- clear + reload scenario (clean up, leave framework untouched, linked with clear_plugin_registrations)
- sys.modules['ava'] consistency
- `import ava.<name>` / `from ava import <name>` resolve through sys.modules
  (ModuleType and SimpleNamespace equivalent; clear drops the entry again)
"""

import io
import sys
from contextlib import redirect_stdout
from importlib import import_module
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

import ava
from agent.state import clear_plugin_registrations


@pytest.fixture(autouse=True)
def _isolate_namespaces():
    """Before/after each test clear plugin-registered namespaces to prevent cross-test leaks,
    ensuring ava surface matches pre-test state after completion."""
    _saved_ns = dict(ava._REGISTERED_NAMESPACES)
    ava.clear_registered_namespaces()
    yield
    ava.clear_registered_namespaces()
    for _name, _obj in _saved_ns.items():
        setattr(ava, _name, _obj)
        if _name not in ava._REGISTERED_NAMESPACES:
            ava._REGISTERED_NAMESPACES[_name] = "<restored>"
        if _name not in ava.__all_for_ava__:
            ava.__all_for_ava__.append(_name)


def _make_module(name: str) -> ModuleType:
    """Build a temporary module to serve as plugin namespace."""
    mod = ModuleType(f"_test_{name}")
    mod.greet = lambda: f"hello from {name}"  # type: ignore[attr-defined]
    mod.__doc__ = f"Plugin {name} description."
    return mod


# ── Basic attach behavior ──────────────────────────────────────────────


def test_register_namespace_attaches_to_ava():
    """After registration, ava.<name> is the passed-in module; agent calls resolve."""
    mod = _make_module("code")
    ava.register_namespace("code", mod)

    assert ava.code is mod  # type: ignore[attr-defined]
    assert ava.code.greet() == "hello from code"  # type: ignore[attr-defined]


def test_register_namespace_adds_to_all():
    """Added to __all_for_ava__ — help(ava) whitelist enumerates submodule."""
    ava.register_namespace("code", _make_module("code"))
    assert "code" in ava.__all_for_ava__


@pytest.mark.parametrize(
    "make_module",
    [
        pytest.param(lambda: _make_module("code"), id="ModuleType"),
        pytest.param(
            lambda: SimpleNamespace(greet=lambda: "hello from code", __doc__="ns plugin"),
            id="SimpleNamespace",
        ),
    ],
)
def test_register_namespace_visible_in_help(make_module):
    """After registering with ModuleType or SimpleNamespace, `help(ava)` output includes
    `from . import code` submodule reference — `_public_members` whitelist
    trust mode no longer hard-filters ismodule, SimpleNamespace is also expanded (review-fix #192:
    previously SimpleNamespace was silently skipped by help).
    """
    ava.register_namespace("code", make_module())

    buf = io.StringIO()
    with redirect_stdout(buf):
        ava.help()
    output = buf.getvalue()

    assert "from . import code" in output, (
        f"help(ava) missing code submodule reference, actual output:\n{output}"
    )


# ── Name validation (S1: two cases raised separately) ──────────────────


def test_register_namespace_invalid_identifier_raises():
    """Invalid identifier → InvalidNamespaceNameError (subclass of RegisterNamespaceError)."""
    with pytest.raises(ava.InvalidNamespaceNameError, match="valid Python identifier"):
        ava.register_namespace("not-an-identifier", _make_module("x"))


def test_register_namespace_underscore_prefix_raises():
    """Starts with `_` → InvalidNamespaceNameError (framework/private convention)."""
    with pytest.raises(ava.InvalidNamespaceNameError, match="cannot start with underscore"):
        ava.register_namespace("_private", _make_module("x"))


# ── Module type validation (C2: fail-fast added per review-fix) ────────


@pytest.mark.parametrize("bad_value", [123, None, "string", {"k": "v"}, [1, 2, 3]])
def test_register_namespace_invalid_module_raises(bad_value):
    """Non-ModuleType/SimpleNamespace → InvalidNamespaceModuleError raised immediately,
    prevents plugin authors passing wrong value leading to AttributeError months later when agent runs, losing root cause."""
    with pytest.raises(ava.InvalidNamespaceModuleError, match="ModuleType or SimpleNamespace"):
        ava.register_namespace("code", bad_value)


def test_register_namespace_invalid_module_no_side_effect():
    """On module type validation failure, ava should not be polluted — name doesn't enter __all_for_ava__/sys.modules."""
    with pytest.raises(ava.InvalidNamespaceModuleError):
        ava.register_namespace("code", 123)
    assert not hasattr(ava, "code")
    assert "code" not in ava.__all_for_ava__


# ── Name collision (C4 layered exception + I1 case-sensitive test) ─────


def test_register_namespace_conflict_with_framework_raises():
    """Framework built-in ava.files → FrameworkNamespaceConflictError."""
    with pytest.raises(ava.FrameworkNamespaceConflictError, match=r"ava\.files"):
        ava.register_namespace("files", _make_module("files"))


def test_register_namespace_conflict_with_getattr_served_attr_raises():
    """`DB_URL` is a top-level attr served by `__getattr__` (not in dir(ava) but hasattr True);
    collision also is FrameworkNamespaceConflictError."""
    with pytest.raises(ava.FrameworkNamespaceConflictError, match=r"ava\.DB_URL"):
        ava.register_namespace("DB_URL", _make_module("x"))


def test_register_namespace_case_sensitive_check():
    """`db_url` lowercase and `DB_URL` are not the same name (case-sensitive). Design intent: Python
    attributes are case-sensitive; plugin author writing lowercase `db_url` should not be
    ambiguously blocked by framework — it receives a real new namespace. This is by design, not a bug."""
    ava.register_namespace("db_url", _make_module("db_url"))
    assert ava.db_url.greet() == "hello from db_url"  # type: ignore[attr-defined]


def test_register_namespace_double_register_raises_with_plugin_name():
    """Duplicate register with same name → PluginNamespaceConflictError, message includes plug-in name that already occupies it."""
    ava.register_namespace("code", _make_module("code"))

    with pytest.raises(ava.PluginNamespaceConflictError, match="already registered by plugin"):
        ava.register_namespace("code", _make_module("code2"))


# ── Exception hierarchy (C4 + I4: mutually exclusive) ─────────────────


def test_exception_hierarchy_parents():
    """All register exceptions are under RegisterNamespaceError — plugin author can
    broadly catch with a single `except RegisterNamespaceError` to catch all."""
    assert issubclass(ava.InvalidNamespaceNameError, ava.RegisterNamespaceError)
    assert issubclass(ava.InvalidNamespaceModuleError, ava.RegisterNamespaceError)
    assert issubclass(ava.NamespaceConflictError, ava.RegisterNamespaceError)
    assert issubclass(ava.FrameworkNamespaceConflictError, ava.NamespaceConflictError)
    assert issubclass(ava.PluginNamespaceConflictError, ava.NamespaceConflictError)


def test_exception_hierarchy_not_overlapping():
    """InvalidName / InvalidModule / NamespaceConflict three groups mutually exclusive —
    plugin `except InvalidNamespaceNameError` won't accidentally swallow module type error."""
    assert not issubclass(ava.NamespaceConflictError, ava.InvalidNamespaceNameError)
    assert not issubclass(ava.NamespaceConflictError, ava.InvalidNamespaceModuleError)
    assert not issubclass(ava.InvalidNamespaceModuleError, ava.InvalidNamespaceNameError)
    # Also not swallowed by builtin ValueError (old design ValueError path removed)
    assert not issubclass(ava.RegisterNamespaceError, ValueError)


# ── clear behavior + reload interaction ──────────────────────────────


def test_clear_registered_namespaces_removes_attr_and_all():
    """clear removes attr + __all_for_ava__ entries, ava surface restored to pristine."""
    ava.register_namespace("code", _make_module("code"))
    ava.register_namespace("ext", _make_module("ext"))

    ava.clear_registered_namespaces()

    assert not hasattr(ava, "code")
    assert not hasattr(ava, "ext")
    assert "code" not in ava.__all_for_ava__
    assert "ext" not in ava.__all_for_ava__


def test_clear_does_not_touch_framework_namespaces():
    """clear only removes plugin-registered ones, framework built-in submodules untouched."""
    framework_before = set(ava.__all_for_ava__)

    ava.register_namespace("code", _make_module("code"))
    ava.clear_registered_namespaces()

    assert set(ava.__all_for_ava__) == framework_before
    assert hasattr(ava, "files")


def test_clear_plugin_registrations_calls_namespace_cleanup():
    """`agent.state.clear_plugin_registrations` is the _load_extensions entry point; must
    link with `ava.clear_registered_namespaces` so plugin reload cleans namespace to avoid
    next register name collision."""
    ava.register_namespace("code", _make_module("code"))

    clear_plugin_registrations()

    assert not hasattr(ava, "code")
    assert "code" not in ava.__all_for_ava__


def test_register_after_clear_reuses_name():
    """After clear, plugin can re-register with same name — required for reload scenario."""
    ava.register_namespace("code", _make_module("code-v1"))
    ava.clear_registered_namespaces()

    ava.register_namespace("code", _make_module("code-v2"))
    assert ava.code.greet() == "hello from code-v2"  # type: ignore[attr-defined]


def test_register_namespace_visible_via_sys_modules():
    """sys.modules['ava'].<name> is also the registered module — any import ava in the same process
    sees the same module object (sys.modules singleton)."""
    mod = _make_module("code")
    ava.register_namespace("code", mod)

    ava_from_sys = sys.modules["ava"]
    assert ava_from_sys.code is mod  # type: ignore[attr-defined]


# ── register_namespace_member (attach a callable under an existing namespace) ─


def _noop(text: str) -> None:
    """A sample member. Update something."""


def test_register_member_attaches_to_existing_namespace():
    """Member lands on the parent module + its __all_for_ava__ exactly once (so help
    lists it, no duplicate), callable through ava.<namespace>.<name>."""
    ava.register_namespace_member("self", "sample_member", _noop)
    assert ava.self.sample_member is _noop  # type: ignore[attr-defined]
    assert ava.self.__all_for_ava__.count("sample_member") == 1


def test_register_member_visible_in_help():
    """The member's product purpose is discoverability — it must render in
    help(ava.self), not merely sit in __all_for_ava__."""
    ava.register_namespace_member("self", "sample_member", _noop)
    buf = io.StringIO()
    with redirect_stdout(buf):
        ava.help(ava.self)
    assert "sample_member" in buf.getvalue()


def test_register_member_cleared_on_clear():
    """clear_registered_namespaces tears the member off the parent + __all_for_ava__."""
    ava.register_namespace_member("self", "sample_member", _noop)
    ava.clear_registered_namespaces()
    assert not hasattr(ava.self, "sample_member")
    assert "sample_member" not in ava.self.__all_for_ava__


def test_register_member_reload_round_trip():
    """register -> clear -> register again succeeds with no __all_for_ava__ duplicate.
    This is the reload cycle _REGISTERED_MEMBERS exists for: the unconditional
    `__all_for_ava__.append` would otherwise accumulate duplicates (or the second
    register would hit MemberConflictError if teardown left the attr behind)."""
    ava.register_namespace_member("self", "sample_member", _noop)
    ava.clear_registered_namespaces()
    ava.register_namespace_member("self", "sample_member", _noop)
    assert callable(ava.self.sample_member)  # type: ignore[attr-defined]
    assert ava.self.__all_for_ava__.count("sample_member") == 1


def test_register_member_rejects_bad_name():
    with pytest.raises(ava.InvalidNamespaceNameError):
        ava.register_namespace_member("self", "_private", _noop)
    with pytest.raises(ava.InvalidNamespaceNameError):
        ava.register_namespace_member("self", "not an ident", _noop)


def test_register_member_rejects_non_callable():
    with pytest.raises(ava.InvalidNamespaceMemberError):
        ava.register_namespace_member("self", "sample_member", 123)


def test_register_member_unknown_namespace():
    with pytest.raises(ava.UnknownNamespaceError):
        ava.register_namespace_member("does_not_exist", "sample_member", _noop)


def test_register_member_conflict_with_existing():
    """A name already on the namespace (e.g. self.terminate) is not overridable."""
    with pytest.raises(ava.MemberConflictError):
        ava.register_namespace_member("self", "terminate", _noop)


# ── importable submodule (import ava.<name>) ─────────────────────────────


def test_import_statement_resolves_registered_namespace():
    """`import ava.code` succeeds once the namespace is registered — the LLM
    habit the import fix targets (agent bug: `import ava.memory` raised
    ModuleNotFoundError while `ava.memory.write` attribute access worked)."""
    ava.register_namespace("code", _make_module("code"))

    namespace: dict[str, Any] = {}
    exec("import ava.code", namespace)

    assert namespace["ava"].code is ava.code
    assert ava.code.greet() == "hello from code"


def test_from_import_resolves_registered_namespace():
    """`from ava import code` returns the registered namespace object."""
    ava.register_namespace("code", _make_module("code"))

    namespace: dict[str, Any] = {}
    exec("from ava import code", namespace)

    assert namespace["code"] is ava.code


def test_importlib_import_matches_package_attribute():
    """importlib.import_module exercises the same import machinery and serves
    the same object the package attribute holds."""
    mod = _make_module("code")
    ava.register_namespace("code", mod)

    assert import_module("ava.code") is mod
    assert sys.modules["ava.code"] is mod


def test_simple_namespace_registration_is_importable_module():
    """A SimpleNamespace namespace is materialized as a real module: importable
    via `import ava.<name>`, and the import returns the same object the
    package attribute serves."""
    ava.register_namespace("probe", SimpleNamespace(ping=lambda: "pong", __doc__="ns plugin"))

    assert isinstance(ava.probe, ModuleType)
    assert sys.modules["ava.probe"] is ava.probe

    namespace: dict[str, Any] = {}
    exec("import ava.probe as probe", namespace)

    assert namespace["probe"] is ava.probe
    assert ava.probe.ping() == "pong"


def test_materialized_namespace_help_renders_members():
    """Materializing a SimpleNamespace must not change what help(ava.<name>)
    renders — members stay discoverable through the synthesized
    __all_for_ava__ surface (same names the namespace's vars() exposed)."""
    ava.register_namespace("code", SimpleNamespace(greet=lambda: "hello", __doc__="ns plugin"))

    buf = io.StringIO()
    with redirect_stdout(buf):
        ava.help(ava.code)

    assert "def greet" in buf.getvalue()


def test_clear_removes_sys_modules_entry():
    """Teardown drops the importable alias: after clear, `import ava.code`
    fails again instead of serving a stale module."""
    ava.register_namespace("code", _make_module("code"))
    ava.clear_registered_namespaces()

    assert "ava.code" not in sys.modules
    with pytest.raises(ModuleNotFoundError):
        import_module("ava.code")


def test_register_clear_register_import_returns_fresh_module():
    """The reload round-trip stays consistent: after re-registration the import
    serves the new object, never the stale one cleared earlier."""
    ava.register_namespace("code", _make_module("code-v1"))
    first = sys.modules["ava.code"]
    ava.clear_registered_namespaces()

    ava.register_namespace("code", _make_module("code-v2"))
    second = sys.modules["ava.code"]

    assert first is not second
    assert import_module("ava.code") is second
    assert ava.code.greet() == "hello from code-v2"
