"""`ava.sdk_surface.install` — the one writer of the `ava` module.

A plugin declares its SDK surface as values (`PluginContributions`); `install(registry)` applies
every plugin's declaration in registry order and `uninstall()` takes all of it back. These tests pin
what only the whole install can promise: a one-plugin round trip leaves `ava` exactly as it found
it, a plugin that cannot be applied is rolled back whole and reported (the others stay), the SDK-usage
recorder sits outermost over plugin wraps, and wrap layers stack in plugin order.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import ava
from ava.sdk_surface import install, metering, skill_sources, wraps
from ava.sdk_surface import plugins as sdk_plugins
from ava.sdk_surface.plugins import (
    FrameworkNamespaceConflictError,
    InvalidNamespaceNameError,
    PluginNamespaceConflictError,
)
from ava.sdk_surface.sdk_disable import _DisabledSDKModule
from base.agents.sdk import call_policy
from base.agents.sdk import telemetry as sdk_usage_telemetry
from base.packages.plugin_config_images import PluginConfigChangedError
from base.packages.plugins import flags, load_report
from base.packages.plugins.config_registration import DuplicateRegistration
from base.packages.plugins.extensions import (
    ExtensionRegistry,
    PluginContributions,
    SdkMember,
    SdkNamespace,
    SdkWrap,
)


@pytest.fixture(autouse=True)
def _surface(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Start from no installation and bare callables, and leave no installation behind."""
    assert install.installed() is None
    monkeypatch.setattr(call_policy, "policy", call_policy.SamplingPolicy)
    yield
    install.uninstall()


@pytest.fixture
def load_failures(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, BaseException]]:
    """Every plugin load failure the install reports, as (plugin, exception)."""
    failures: list[tuple[str, BaseException]] = []

    def spy(name: str, exc: BaseException) -> None:
        failures.append((name, exc))

    monkeypatch.setattr(load_report, "report_plugin_load_failure", spy)
    return failures


def _registry(*plugins: tuple[str, PluginContributions]) -> ExtensionRegistry:
    return ExtensionRegistry(plugins)


def _namespace(**members: Callable[..., Any]) -> SimpleNamespace:
    return SimpleNamespace(__doc__="An installed test namespace.", **members)


def _ping() -> str:
    """Return pong."""
    return "pong"


def _extra() -> str:
    """Return extra."""
    return "extra"


def _tag(name: str) -> Callable[..., Any]:
    """A wrapper that surrounds the inner result with `name(...)`."""

    def wrapper(inner: Callable[..., Any], *args: Any, **kwargs: Any) -> str:
        return f"{name}({inner(*args, **kwargs)})"

    return wrapper


def _passthrough(inner: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return inner(*args, **kwargs)


def _surface_snapshot() -> dict[str, Any]:
    """Everything an install may write that a test can observe from outside."""
    return {
        "all": list(ava.__all_for_ava__),
        "self_all": list(ava.self.__all_for_ava__),
        "files_read": ava.files.read,
        "self_attrs": set(vars(ava.self)),
        "wrappers": dict(wraps.wrappers()),
        "roots": skill_sources.roots(),
        "modules": {k for k in sys.modules if k.startswith("ava.")},
    }


# ── one plugin, every piece, fully removed ─────────────────────────────────────


def _assert_namespaces_visible() -> None:
    assert ava.install_ns.ping() == "w(pong)"  # type: ignore[attr-defined]
    assert ava.install_ns.extra() == "extra"  # type: ignore[attr-defined]
    assert "extra" in ava.install_ns.__all_for_ava__  # type: ignore[attr-defined]
    assert "install_ns" in ava.__all_for_ava__
    assert sys.modules["ava.install_ns"] is ava.install_ns  # type: ignore[attr-defined]
    assert ava.self.install_probe() == "extra"  # type: ignore[attr-defined]
    assert "install_probe" in ava.self.__all_for_ava__


def _assert_wraps_and_roots(root: Path) -> None:
    assert [p for p, _w in wraps.stack("install_ns.ping")] == ["demo"]
    assert [p for p, _w in wraps.stack("files.read")] == ["demo"]
    assert root in skill_sources.roots()


def test_a_one_plugin_install_is_fully_removed_by_uninstall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    before = _surface_snapshot()
    registry = _registry(
        (
            "demo",
            PluginContributions(
                sdk_namespaces=(SdkNamespace("install_ns", _namespace(ping=_ping), expand=True),),
                sdk_members=(
                    SdkMember("install_ns", "extra", _extra),
                    SdkMember("self", "install_probe", _extra),
                ),
                sdk_expansions=("shell.sessions",),
                sdk_wraps=(
                    SdkWrap("install_ns.ping", _tag("w")),
                    SdkWrap("files.read", _passthrough),
                ),
                skill_sources=(lambda: [root],),
            ),
        )
    )

    admitted = install.install(registry)

    assert admitted == registry
    installation = install.installed()
    assert installation is not None and installation.registry == registry
    _assert_namespaces_visible()
    _assert_wraps_and_roots(root)
    assert install.expansions() == ("install_ns", "shell.sessions")

    install.uninstall()

    assert install.installed() is None
    assert install.expansions() == ()
    assert not hasattr(ava, "install_ns")
    assert not hasattr(ava.self, "install_probe")
    assert _surface_snapshot() == before


def test_uninstall_without_an_installation_is_a_no_op() -> None:
    before = _surface_snapshot()
    install.uninstall()
    assert install.installed() is None
    assert _surface_snapshot() == before


def test_installing_twice_raises_until_uninstall() -> None:
    registry = _registry(
        ("demo", PluginContributions(sdk_namespaces=(SdkNamespace("install_ns", _namespace()),)))
    )
    install.install(registry)

    with pytest.raises(RuntimeError, match="already installed"):
        install.install(_registry())

    installation = install.installed()
    assert installation is not None and installation.registry == registry
    assert hasattr(ava, "install_ns")

    install.uninstall()
    assert install.install(_registry()) == _registry()


# ── a plugin the install refuses ───────────────────────────────────────────────


def test_a_second_plugin_declaring_a_taken_namespace_is_refused_and_the_first_stays(
    load_failures: list[tuple[str, BaseException]],
) -> None:
    first_ns, second_ns = _namespace(ping=_ping), _namespace(ping=_extra)
    first = PluginContributions(sdk_namespaces=(SdkNamespace("install_ns", first_ns),))
    second = PluginContributions(
        sdk_namespaces=(SdkNamespace("install_ns", second_ns),),
        sdk_members=(SdkMember("self", "second_member", _extra),),
    )

    admitted = install.install(_registry(("first", first), ("second", second)))

    assert [name for name, _c in admitted.plugins] == ["first"]
    [(name, exc)] = load_failures
    assert name == "second"
    assert isinstance(exc, PluginNamespaceConflictError)
    assert "'first'" in str(exc)
    assert ava.install_ns.ping() == "pong"  # type: ignore[attr-defined]  # the first plugin's module
    assert not hasattr(ava.self, "second_member")
    installation = install.installed()
    assert installation is not None and installation.registry == admitted

    install.uninstall()
    assert not hasattr(ava, "install_ns")


def _declaring(failing: str) -> PluginContributions:
    """A plugin whose pieces apply in order — namespace, member, expansion, wrap, skill source —
    until the one named by `failing` cannot be applied."""
    base: dict[str, Any] = {
        "sdk_namespaces": (SdkNamespace("rolled_ns", _namespace(ping=_ping), expand=True),),
        "sdk_members": (SdkMember("self", "rolled_member", _extra),),
        "sdk_wraps": (SdkWrap("files.read", _passthrough),),
        "skill_sources": (lambda: [Path("/nonexistent-rolled-skill-root")],),
    }
    if failing == "wrap":
        base["sdk_wraps"] += (SdkWrap("files.does_not_exist", _tag("x")),)
    elif failing == "expansion":
        base["sdk_expansions"] = ("_private",)
    elif failing == "flag":
        base["flags"] = ("bogus",)
    else:
        raise AssertionError(failing)
    return PluginContributions(**base)


@pytest.mark.parametrize(
    ("failing", "error"),
    [
        pytest.param("wrap", wraps.WrapTargetError, id="wrap-target-does-not-resolve"),
        pytest.param("expansion", InvalidNamespaceNameError, id="expansion-path-invalid"),
        pytest.param("flag", flags.UnknownFlag, id="flag-unknown-applied-last"),
    ],
)
def test_a_plugin_whose_later_piece_fails_is_rolled_back_whole(
    load_failures: list[tuple[str, BaseException]], failing: str, error: type[BaseException]
) -> None:
    good = PluginContributions(
        sdk_namespaces=(SdkNamespace("good_ns", _namespace(ping=_ping), expand=True),),
        sdk_wraps=(SdkWrap("good_ns.ping", _tag("g")),),
    )
    before = _surface_snapshot()

    admitted = install.install(_registry(("bad", _declaring(failing)), ("good", good)))

    [(name, exc)] = load_failures
    assert name == "bad"
    assert isinstance(exc, error)
    assert [n for n, _c in admitted.plugins] == ["good"]
    # Nothing of the bad plugin survives, including the pieces applied before the failing one ...
    assert not hasattr(ava, "rolled_ns")
    assert "rolled_ns" not in ava.__all_for_ava__
    assert "ava.rolled_ns" not in sys.modules
    assert not hasattr(ava.self, "rolled_member")
    assert wraps.stack("files.read") == []
    assert not any(str(r).startswith("/nonexistent-rolled") for r in skill_sources.roots())
    assert install.expansions() == ("good_ns",)
    # ... and the plugin after it is installed normally.
    assert ava.good_ns.ping() == "g(pong)"  # type: ignore[attr-defined]

    install.uninstall()
    assert _surface_snapshot() == before


def test_a_namespace_disabled_by_the_sdk_disable_sentinel_refuses_the_plugin(
    load_failures: list[tuple[str, BaseException]],
) -> None:
    sentinel = _DisabledSDKModule("ava.disabled_ns")
    sys.modules["ava.disabled_ns"] = sentinel
    try:
        admitted = install.install(
            _registry(
                (
                    "wants_disabled",
                    PluginContributions(
                        sdk_namespaces=(SdkNamespace("disabled_ns", _namespace(ping=_ping)),)
                    ),
                )
            )
        )

        [(name, exc)] = load_failures
        assert name == "wants_disabled"
        assert isinstance(exc, FrameworkNamespaceConflictError)
        assert "AVA_SDK_DISABLE" in str(exc)
        assert admitted.plugins == ()
        # The sentinel the disable machinery put there is not clobbered.
        assert sys.modules["ava.disabled_ns"] is sentinel
        assert not hasattr(ava, "disabled_ns")
    finally:
        sys.modules.pop("ava.disabled_ns", None)


# ── the recorder, wrap layering, views ─────────────────────────────────────────


def test_the_recorder_is_outermost_over_plugin_wraps_and_gone_after_uninstall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def emit(fn: str, *_args: Any, **_kwargs: Any) -> None:
        calls.append(fn)

    monkeypatch.setattr(sdk_usage_telemetry, "emit", emit)
    original_read = ava.files.read
    registry = _registry(
        (
            "demo",
            PluginContributions(
                sdk_namespaces=(SdkNamespace("install_ns", _namespace(ping=_ping)),),
                sdk_wraps=(
                    SdkWrap("install_ns.ping", _tag("w")),
                    SdkWrap("files.read", _passthrough),
                ),
            ),
        )
    )

    install.install(registry)

    ping = ava.install_ns.ping  # type: ignore[attr-defined]
    assert metering.is_recorder(ping)  # the recorder is the outermost layer
    assert not metering.is_recorder(ping.__wrapped__)  # ... with the plugin's layer under it
    assert metering.is_recorder(ava.files.read)
    assert ping() == "w(pong)"
    assert calls == ["install_ns.ping"]  # one count per agent call, not one per layer

    install.uninstall()

    # The recorder came off before the plugin wraps were undone, so the framework callable is
    # restored to the very object it was, not to a recorder proxy or a stale chain.
    assert ava.files.read is original_read
    assert not metering.is_recorder(original_read)
    assert wraps.wrappers() == {}


def test_wrap_layers_stack_in_plugin_order_later_outermost() -> None:
    wrapper_a, wrapper_b, wrapper_c = _tag("a"), _tag("b"), _tag("c")
    registry = _registry(
        (
            "alpha",
            PluginContributions(
                sdk_namespaces=(SdkNamespace("install_ns", _namespace(ping=_ping)),),
                sdk_wraps=(SdkWrap("install_ns.ping", wrapper_a),),
            ),
        ),
        (
            "beta",
            PluginContributions(
                sdk_wraps=(
                    SdkWrap("install_ns.ping", wrapper_b),
                    SdkWrap("install_ns.ping", wrapper_c),
                )
            ),
        ),
    )

    install.install(registry)

    assert ava.install_ns.ping() == "c(b(a(pong)))"  # type: ignore[attr-defined]
    # The curated `ava.extend` views report the same chain, innermost first.
    assert ava.extend.stack("install_ns.ping") == [
        ("alpha", wrapper_a),
        ("beta", wrapper_b),
        ("beta", wrapper_c),
    ]
    assert ava.extend.wrappers()["install_ns.ping"] == ava.extend.stack("install_ns.ping")

    install.uninstall()

    assert ava.extend.stack("install_ns.ping") == []
    assert "install_ns.ping" not in ava.extend.wrappers()


def test_expansions_reflect_namespaces_marked_expand_and_declared_paths_in_registry_order() -> None:
    install.install(
        _registry(
            (
                "alpha",
                PluginContributions(
                    sdk_namespaces=(
                        SdkNamespace("ns_one", _namespace(), expand=True),
                        SdkNamespace("ns_quiet", _namespace()),
                    ),
                    sdk_expansions=("shell.sessions",),
                ),
            ),
            (
                "beta",
                PluginContributions(
                    sdk_namespaces=(SdkNamespace("ns_two", _namespace(), expand=True),),
                    sdk_expansions=("files",),
                ),
            ),
        )
    )

    # Per plugin: its expanded namespaces, then its explicit paths; a namespace not marked
    # `expand` is installed but not promoted.
    assert install.expansions() == ("ns_one", "shell.sessions", "ns_two", "files")
    assert hasattr(ava, "ns_quiet")

    install.uninstall()
    assert install.expansions() == ()


@pytest.mark.parametrize(
    "error",
    [
        AttributeError("binding implementation bug"),
        ValueError("binding invariant bug"),
        RuntimeError("binding runtime bug"),
        OSError("binding disk failure"),
        PluginConfigChangedError("unhandled config write conflict"),
        DuplicateRegistration("config binding invariant violated"),
        KeyboardInterrupt("binding cancelled"),
    ],
)
def test_unknown_binding_failure_rolls_back_every_plugin_and_propagates_identity(
    monkeypatch: pytest.MonkeyPatch,
    load_failures: list[tuple[str, BaseException]],
    error: BaseException,
) -> None:
    from pydantic import BaseModel

    from base.packages.plugins import config_registration

    def fail_binding(plugin: str, cls: type[BaseModel]) -> Callable[[], None]:
        raise error

    monkeypatch.setattr(config_registration, "bind_plugin_config", fail_binding)
    before = _surface_snapshot()
    failing = PluginContributions(
        sdk_namespaces=(SdkNamespace("failed_ns", _namespace(ping=_ping)),),
        sdk_members=(SdkMember("self", "failed_member", _extra),),
        sdk_wraps=(SdkWrap("files.read", _passthrough),),
        skill_sources=(lambda: [Path("/nonexistent-failed")],),
        flags=("general.message_timestamps",),
        config=BaseModel,
    )
    prior = PluginContributions(sdk_namespaces=(SdkNamespace("prior_ns", _namespace()),))
    later = PluginContributions(sdk_namespaces=(SdkNamespace("later_ns", _namespace()),))

    with pytest.raises(type(error)) as caught:
        install.install(_registry(("prior", prior), ("failed", failing), ("later", later)))

    assert caught.value is error
    assert install.installed() is None
    assert load_failures == []
    assert _surface_snapshot() == before
    assert not hasattr(ava, "failed_ns") and not hasattr(ava, "prior_ns")
    assert not hasattr(ava, "later_ns")
    assert flags.declared_flags("failed") == frozenset()


@pytest.mark.parametrize("refused", [False, True], ids=["unknown-primary", "typed-refusal"])
def test_rollback_attempts_all_undos_and_cleanup_failure_is_never_success(
    monkeypatch: pytest.MonkeyPatch,
    load_failures: list[tuple[str, BaseException]],
    refused: bool,
) -> None:
    from pydantic import BaseModel

    from base.packages.plugins import config_registration

    primary = RuntimeError("original bind failure")
    cleanup = OSError("member cleanup failed")
    cancelled = KeyboardInterrupt("namespace cleanup interrupted")
    calls: list[str] = []
    install_member = sdk_plugins.install_member
    install_namespace = sdk_plugins.install_namespace

    def member(*args: Any, **kwargs: Any) -> Callable[[], None]:
        undo = install_member(*args, **kwargs)

        def undo_member() -> None:
            undo()
            calls.append("member")
            raise cleanup

        return undo_member

    def namespace(plugin: str, *args: Any, **kwargs: Any) -> Callable[[], None]:
        undo = install_namespace(plugin, *args, **kwargs)

        def undo_namespace() -> None:
            undo()
            calls.append(plugin)
            if plugin == "failed":
                raise cancelled

        return undo_namespace

    def fail_binding(plugin: str, cls: type[BaseModel]) -> Callable[[], None]:
        raise primary

    monkeypatch.setattr(sdk_plugins, "install_member", member)
    monkeypatch.setattr(sdk_plugins, "install_namespace", namespace)
    monkeypatch.setattr(config_registration, "bind_plugin_config", fail_binding)
    before = _surface_snapshot()
    failing = PluginContributions(
        sdk_namespaces=(SdkNamespace("failed_ns", _namespace()),),
        sdk_members=(SdkMember("self", "failed_member", _extra),),
        flags=("not_a_flag",) if refused else (),
        config=None if refused else BaseModel,
    )
    prior = PluginContributions(sdk_namespaces=(SdkNamespace("prior_ns", _namespace()),))
    later = PluginContributions(sdk_namespaces=(SdkNamespace("later_ns", _namespace()),))
    expected = cleanup if refused else primary

    with pytest.raises(type(expected)) as caught:
        install.install(_registry(("prior", prior), ("failed", failing), ("later", later)))

    assert caught.value is expected
    assert calls == ["member", "failed", "prior"]
    notes = "\n".join(caught.value.__notes__)
    assert "namespace cleanup interrupted" in notes
    if not refused:
        assert "member cleanup failed" in notes
    assert load_failures == []
    assert install.installed() is None
    assert _surface_snapshot() == before
