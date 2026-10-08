"""Guard for the namespace / member install primitives (`ava.sdk_surface.plugins`).

A plugin declares `SdkNamespace` / `SdkMember` values; `ava.sdk_surface.install` applies each through
`install_namespace` / `install_member`, which attach to the `ava` module and return the undo that
reverses it. These tests drive the primitives directly (one declaration, one undo) so a failure points
at the primitive; the whole-registry behavior (rollback, ordering, uninstall) is in
`ava/sdk_surface/tests/test_install.py`.

Coverage matrix:
- attach + __all_for_ava__ + help(ava) visible (ModuleType and SimpleNamespace equivalent)
- Exception hierarchy (InvalidName / InvalidModule / FrameworkConflict / PluginConflict
  all under RegisterNamespaceError, mutually exclusive)
- Module type validation (int/None/str -> InvalidNamespaceModuleError, fail-fast)
- Name validation (invalid identifier / underscore prefix, raise separately)
- Name collision (framework built-in / top-level attr served by __getattr__ like DB_URL / a name an
  earlier plugin already holds)
- undo + reinstall scenario (cleans up, leaves the framework untouched, the name is reusable)
- sys.modules['ava'] consistency
- `import ava.<name>` / `from ava import <name>` resolve through sys.modules
  (ModuleType and SimpleNamespace equivalent; the undo drops the entry again)
"""

import io
import sys
from collections.abc import Callable, Iterator, Mapping
from contextlib import redirect_stdout
from importlib import import_module
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

import ava
from ava.sdk_surface import plugins

PLUGIN = "test-plugin"


class _Installer:
    """Installs through the primitives and keeps every undo so teardown can run them."""

    def __init__(self) -> None:
        self.undos: list[Callable[[], None]] = []

    def namespace(
        self, name: str, module: Any, taken: Mapping[str, str] | None = None
    ) -> Callable[[], None]:
        undo = plugins.install_namespace(PLUGIN, name, module, {} if taken is None else taken)
        self.undos.append(undo)
        return undo

    def member(self, namespace: str, name: str, fn: Any) -> Callable[[], None]:
        undo = plugins.install_member(PLUGIN, namespace, name, fn)
        self.undos.append(undo)
        return undo


@pytest.fixture(autouse=True)
def installer() -> Iterator[_Installer]:
    """Undo everything a test installed (newest first), so the ava surface (package attrs,
    `__all_for_ava__`, sys.modules aliases) matches its pre-test state after completion. Every undo
    is safe to run again, so a test may call its own undo too."""
    inst = _Installer()
    yield inst
    while inst.undos:
        inst.undos.pop()()


def _make_module(name: str) -> ModuleType:
    """Build a temporary module to serve as plugin namespace."""
    mod = ModuleType(f"_test_{name}")
    mod.greet = lambda: f"hello from {name}"  # type: ignore[attr-defined]
    mod.__doc__ = f"Plugin {name} description."
    return mod


# ── Basic attach behavior ──────────────────────────────────────────────


def test_install_namespace_attaches_to_ava(installer: _Installer):
    """After the install, ava.<name> is the passed-in module; agent calls resolve."""
    mod = _make_module("code")
    installer.namespace("code", mod)

    assert ava.code is mod  # type: ignore[attr-defined]
    assert ava.code.greet() == "hello from code"  # type: ignore[attr-defined]


def test_install_namespace_adds_to_all(installer: _Installer):
    """Added to __all_for_ava__ — help(ava) whitelist enumerates submodule."""
    installer.namespace("code", _make_module("code"))
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
def test_install_namespace_visible_in_help(installer: _Installer, make_module):
    """After installing a ModuleType or SimpleNamespace, `help(ava)` output includes
    `from . import code` submodule reference — `_public_members` whitelist
    trust mode no longer hard-filters ismodule, SimpleNamespace is also expanded (review-fix #192:
    previously SimpleNamespace was silently skipped by help).
    """
    installer.namespace("code", make_module())

    buf = io.StringIO()
    with redirect_stdout(buf):
        ava.help()
    output = buf.getvalue()

    assert "from . import code" in output, (
        f"help(ava) missing code submodule reference, actual output:\n{output}"
    )


# ── Name validation (S1: two cases raised separately) ──────────────────


def test_install_namespace_invalid_identifier_raises(installer: _Installer):
    """Invalid identifier → InvalidNamespaceNameError (subclass of RegisterNamespaceError)."""
    with pytest.raises(ava.InvalidNamespaceNameError, match="valid Python identifier"):
        installer.namespace("not-an-identifier", _make_module("x"))


def test_install_namespace_underscore_prefix_raises(installer: _Installer):
    """Starts with `_` → InvalidNamespaceNameError (framework/private convention)."""
    with pytest.raises(ava.InvalidNamespaceNameError, match="cannot start with underscore"):
        installer.namespace("_private", _make_module("x"))


# ── Module type validation (C2: fail-fast added per review-fix) ────────


@pytest.mark.parametrize("bad_value", [123, None, "string", {"k": "v"}, [1, 2, 3]])
def test_install_namespace_invalid_module_raises(installer: _Installer, bad_value):
    """Non-ModuleType/SimpleNamespace → InvalidNamespaceModuleError raised immediately,
    prevents plugin authors passing wrong value leading to AttributeError months later when agent runs, losing root cause."""
    with pytest.raises(ava.InvalidNamespaceModuleError, match="ModuleType or SimpleNamespace"):
        installer.namespace("code", bad_value)


def test_install_namespace_invalid_module_no_side_effect(installer: _Installer):
    """On module type validation failure, ava should not be polluted — name doesn't enter __all_for_ava__/sys.modules."""
    with pytest.raises(ava.InvalidNamespaceModuleError):
        installer.namespace("code", 123)
    assert not hasattr(ava, "code")
    assert "code" not in ava.__all_for_ava__
    assert "ava.code" not in sys.modules


# ── Name collision (C4 layered exception + I1 case-sensitive test) ─────


def test_install_namespace_conflict_with_framework_raises(installer: _Installer):
    """Framework built-in ava.files → FrameworkNamespaceConflictError."""
    with pytest.raises(ava.FrameworkNamespaceConflictError, match=r"ava\.files"):
        installer.namespace("files", _make_module("files"))


def test_install_namespace_conflict_with_getattr_served_attr_raises(installer: _Installer):
    """`DB_URL` is a top-level attr served by `__getattr__` (not in dir(ava) but hasattr True);
    collision also is FrameworkNamespaceConflictError."""
    with pytest.raises(ava.FrameworkNamespaceConflictError, match=r"ava\.DB_URL"):
        installer.namespace("DB_URL", _make_module("x"))


def test_install_namespace_refuses_disabled_sentinel(installer: _Installer):
    """AVA_SDK_DISABLE swaps sys.modules['ava.<name>'] for a disabled-module
    sentinel at import time (before plugin load); the install must not
    clobber it — the namespace stays disabled (framework-owned, not
    overridable) and the sentinel keeps raising its legible error."""
    from ava.sdk_surface.sdk_disable import _DisabledSDKModule

    sentinel = _DisabledSDKModule("ava.code")
    sys.modules["ava.code"] = sentinel
    try:
        with pytest.raises(ava.FrameworkNamespaceConflictError, match="AVA_SDK_DISABLE"):
            installer.namespace("code", _make_module("code"))
        # The sentinel survives: the namespace stays disabled for downstream
        # `ava.code.anything` access.
        assert sys.modules["ava.code"] is sentinel
        assert not hasattr(ava, "code")
        assert "code" not in ava.__all_for_ava__
    finally:
        sys.modules.pop("ava.code", None)


def test_install_namespace_case_sensitive_check(installer: _Installer):
    """`db_url` lowercase and `DB_URL` are not the same name (case-sensitive). Design intent: Python
    attributes are case-sensitive; plugin author writing lowercase `db_url` should not be
    ambiguously blocked by framework — it receives a real new namespace. This is by design, not a bug."""
    installer.namespace("db_url", _make_module("db_url"))
    assert ava.db_url.greet() == "hello from db_url"  # type: ignore[attr-defined]


def test_install_namespace_taken_by_another_plugin_raises_with_plugin_name(installer: _Installer):
    """A name an earlier plugin of the same install already holds → PluginNamespaceConflictError,
    whose message names the plugin that occupies it; nothing is attached."""
    with pytest.raises(
        ava.PluginNamespaceConflictError, match="already installed by plugin 'first'"
    ):
        installer.namespace("code", _make_module("code2"), taken={"code": "first"})
    assert not hasattr(ava, "code")


# ── Exception hierarchy (C4 + I4: mutually exclusive) ─────────────────


def test_exception_hierarchy_parents():
    """All install exceptions are under RegisterNamespaceError — plugin author can
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


# ── undo behavior + reinstall ────────────────────────────────────────


def test_namespace_undo_removes_attr_and_all(installer: _Installer):
    """The undo removes the attr + __all_for_ava__ entry, ava surface restored to pristine."""
    undo_code = installer.namespace("code", _make_module("code"))
    undo_ext = installer.namespace("ext", _make_module("ext"))

    undo_ext()
    undo_code()

    assert not hasattr(ava, "code")
    assert not hasattr(ava, "ext")
    assert "code" not in ava.__all_for_ava__
    assert "ext" not in ava.__all_for_ava__


def test_namespace_undo_does_not_touch_framework_namespaces(installer: _Installer):
    """The undo only removes what it installed, framework built-in submodules untouched."""
    framework_before = set(ava.__all_for_ava__)

    installer.namespace("code", _make_module("code"))()

    assert set(ava.__all_for_ava__) == framework_before
    assert hasattr(ava, "files")


def test_install_after_undo_reuses_name(installer: _Installer):
    """After the undo, a plugin can install the same name again — required for reload scenario."""
    installer.namespace("code", _make_module("code-v1"))()

    installer.namespace("code", _make_module("code-v2"))
    assert ava.code.greet() == "hello from code-v2"  # type: ignore[attr-defined]


def test_install_namespace_visible_via_sys_modules(installer: _Installer):
    """sys.modules['ava'].<name> is also the installed module — any import ava in the same process
    sees the same module object (sys.modules singleton)."""
    mod = _make_module("code")
    installer.namespace("code", mod)

    ava_from_sys = sys.modules["ava"]
    assert ava_from_sys.code is mod  # type: ignore[attr-defined]


# ── install_member (attach a callable under an existing namespace) ─


def _noop(text: str) -> None:
    """A sample member. Update something."""


def test_install_member_attaches_to_existing_namespace(installer: _Installer):
    """Member lands on the parent module + its __all_for_ava__ exactly once (so help
    lists it, no duplicate), callable through ava.<namespace>.<name>."""
    installer.member("self", "sample_member", _noop)
    assert ava.self.sample_member is _noop  # type: ignore[attr-defined]
    assert ava.self.__all_for_ava__.count("sample_member") == 1


def test_install_member_visible_in_help(installer: _Installer):
    """The member's product purpose is discoverability — it must render in
    help(ava.self), not merely sit in __all_for_ava__."""
    installer.member("self", "sample_member", _noop)
    buf = io.StringIO()
    with redirect_stdout(buf):
        ava.help(ava.self)
    assert "sample_member" in buf.getvalue()


def test_member_undo_tears_it_off_the_parent(installer: _Installer):
    """The undo takes the member off the parent + __all_for_ava__."""
    installer.member("self", "sample_member", _noop)()
    assert not hasattr(ava.self, "sample_member")
    assert "sample_member" not in ava.self.__all_for_ava__


def test_install_member_reload_round_trip(installer: _Installer):
    """install -> undo -> install again succeeds with no __all_for_ava__ duplicate: the
    undo takes both the attr and the surface entry, so the second install neither hits
    MemberConflictError nor accumulates a duplicate."""
    installer.member("self", "sample_member", _noop)()
    installer.member("self", "sample_member", _noop)
    assert callable(ava.self.sample_member)  # type: ignore[attr-defined]
    assert ava.self.__all_for_ava__.count("sample_member") == 1


def test_member_on_a_plugin_namespace_goes_with_it(installer: _Installer):
    """A member may hang on a namespace the same install added; undoing newest-first removes
    the member and then the namespace, and undoing the namespace first is also safe (the module
    is being discarded, so the member goes with it)."""
    # A SimpleNamespace is materialized with a synthesized `__all_for_ava__`, which is what lets it host members.
    undo_ns = installer.namespace("code", SimpleNamespace(greet=lambda: "hello", __doc__="ns"))
    undo_member = installer.member("code", "extra", _noop)
    assert ava.code.extra is _noop  # type: ignore[attr-defined]
    assert "extra" in ava.code.__all_for_ava__  # type: ignore[attr-defined]

    undo_ns()
    undo_member()  # parent already gone: nothing to do, no error
    assert not hasattr(ava, "code")


def test_install_member_rejects_bad_name(installer: _Installer):
    with pytest.raises(ava.InvalidNamespaceNameError):
        installer.member("self", "_private", _noop)
    with pytest.raises(ava.InvalidNamespaceNameError):
        installer.member("self", "not an ident", _noop)


def test_install_member_rejects_non_callable(installer: _Installer):
    with pytest.raises(ava.InvalidNamespaceMemberError):
        installer.member("self", "sample_member", 123)


def test_install_member_unknown_namespace(installer: _Installer):
    with pytest.raises(ava.UnknownNamespaceError):
        installer.member("does_not_exist", "sample_member", _noop)


def test_install_member_conflict_with_existing(installer: _Installer):
    """A name already on the namespace (e.g. self.terminate) is not overridable."""
    with pytest.raises(ava.MemberConflictError):
        installer.member("self", "terminate", _noop)


# ── importable submodule (import ava.<name>) ─────────────────────────────


def test_import_statement_resolves_installed_namespace(installer: _Installer):
    """`import ava.code` succeeds once the namespace is installed — the LLM
    habit the import fix targets (agent bug: `import ava.memory` raised
    ModuleNotFoundError while `ava.memory.write` attribute access worked)."""
    installer.namespace("code", _make_module("code"))

    namespace: dict[str, Any] = {}
    exec("import ava.code", namespace)

    assert namespace["ava"].code is ava.code
    assert ava.code.greet() == "hello from code"


def test_from_import_resolves_installed_namespace(installer: _Installer):
    """`from ava import code` returns the installed namespace object."""
    installer.namespace("code", _make_module("code"))

    namespace: dict[str, Any] = {}
    exec("from ava import code", namespace)

    assert namespace["code"] is ava.code


def test_importlib_import_matches_package_attribute(installer: _Installer):
    """importlib.import_module exercises the same import machinery and serves
    the same object the package attribute holds."""
    mod = _make_module("code")
    installer.namespace("code", mod)

    assert import_module("ava.code") is mod
    assert sys.modules["ava.code"] is mod


def test_simple_namespace_install_is_importable_module(installer: _Installer):
    """A SimpleNamespace namespace is materialized as a real module: importable
    via `import ava.<name>`, and the import returns the same object the
    package attribute serves."""
    installer.namespace("probe", SimpleNamespace(ping=lambda: "pong", __doc__="ns plugin"))

    assert isinstance(ava.probe, ModuleType)
    assert sys.modules["ava.probe"] is ava.probe

    namespace: dict[str, Any] = {}
    exec("import ava.probe as probe", namespace)

    assert namespace["probe"] is ava.probe
    assert ava.probe.ping() == "pong"


def test_materialized_namespace_help_renders_members(installer: _Installer):
    """Materializing a SimpleNamespace must not change what help(ava.<name>)
    renders — members stay discoverable through the synthesized
    __all_for_ava__ surface (same names the namespace's vars() exposed)."""
    installer.namespace("code", SimpleNamespace(greet=lambda: "hello", __doc__="ns plugin"))

    buf = io.StringIO()
    with redirect_stdout(buf):
        ava.help(ava.code)

    assert "def greet" in buf.getvalue()


def test_undo_removes_sys_modules_entry(installer: _Installer):
    """The undo drops the importable alias: afterwards `import ava.code`
    fails again instead of serving a stale module."""
    installer.namespace("code", _make_module("code"))()

    assert "ava.code" not in sys.modules
    with pytest.raises(ModuleNotFoundError):
        import_module("ava.code")


def test_install_undo_install_import_returns_fresh_module(installer: _Installer):
    """The reload round-trip stays consistent: after the second install the import
    serves the new object, never the stale one undone earlier."""
    undo = installer.namespace("code", _make_module("code-v1"))
    first = sys.modules["ava.code"]
    undo()

    installer.namespace("code", _make_module("code-v2"))
    second = sys.modules["ava.code"]

    assert first is not second
    assert import_module("ava.code") is second
    assert ava.code.greet() == "hello from code-v2"
