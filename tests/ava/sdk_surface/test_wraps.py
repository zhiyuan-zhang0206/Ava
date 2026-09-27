"""`ava.extend` wrap primitive behavior guards (`ava/sdk_surface/wraps.py`).

Wrap primitive:
- register + install: `wrap(target, wrapper)` replaces the ava callable
- introspection: `stack(target)` lists (plugin, wrapper) innermost-first;
  `wrappers()` maps every target
- determinism: registration order == plugin load order, last registered outermost
- metadata: rendered signature drops `inner`, docstring is the wrapper's own or
  the inherited one, function-attached members carry through
- control flow: short-circuit (skip inner) + retry (call inner twice)
- plugin attribution via PluginContext
- activation telemetry: a plugin layer that does not call inner exactly once
  records one `plugin_activation`; a transparent or unattributed layer does not
- clear_wraps restores originals + empties the registry (the reload-free teardown)
- target errors: malformed dotted path, non-callable target

A fake `ava.probe` namespace holds the wrap targets so the real SDK is never
patched; the fixture clears wraps then removes the namespace.
"""

import inspect
import types
from collections.abc import Iterator
from typing import Any

import pytest

import ava
from ava.sdk_surface import wraps
from ava.sdk_surface.wraps import wrap
from shared import plugin_activation
from shared.plugin_context import PluginContext


@pytest.fixture
def probe() -> Iterator[tuple[Any, Any]]:
    """A throwaway `ava.probe` namespace with `fn` (documented, with an attached
    member) so wraps target it instead of a real SDK function. Typed `Any` so the
    intentionally dynamic module/function attribute writes stay unchecked."""

    def fn(x, y=1, *, z=2):
        """probe fn doc."""
        return f"fn({x},{y},{z})"

    fn_any: Any = fn
    fn_any.MARKER = "attached"  # function-attached member, like ava.understand.UnderstandError

    ns: Any = types.ModuleType("ava_probe_ns")
    ns.__all_for_ava__ = ["fn"]
    ns.fn = fn
    ava_any: Any = ava
    ava_any.probe = ns  # direct bind — bypasses register_namespace to isolate the wrap primitive
    yield ns, fn
    wraps.clear_wraps()  # restore probe.fn before the namespace disappears
    delattr(ava, "probe")


def test_wrap_installs_and_stack_lists(probe: tuple[Any, Any]):
    """wrap replaces the target; stack reports one (plugin, wrapper) layer."""
    ns, fn = probe

    def w(inner, *a, **k):
        return inner(*a, **k)

    returned = wrap("probe.fn", w)  # pyright: ignore[reportUnknownArgumentType]
    assert returned is w  # returns the wrapper so the caller keeps a reference
    assert ns.fn is not fn  # target replaced by the chained closure
    assert wraps.stack("probe.fn") == [("<unknown>", w)]
    assert ava.probe.fn(9) == "fn(9,1,2)"  # still calls through


def test_wrappers_maps_all_targets(probe: tuple[Any, Any]):
    wrap("probe.fn", lambda inner, *a, **k: inner(*a, **k))  # pyright: ignore[reportUnknownArgumentType]
    allmap = wraps.wrappers()
    assert set(allmap) == {"probe.fn"}
    assert len(allmap["probe.fn"]) == 1


def test_plugin_attribution_from_context(probe: tuple[Any, Any]):
    """Inside PluginContext the layer is attributed to that plugin."""

    def w(inner, *a, **k):
        return inner(*a, **k)

    with PluginContext("myplugin"):
        wrap("probe.fn", w)  # pyright: ignore[reportUnknownArgumentType]
    assert wraps.stack("probe.fn") == [("myplugin", w)]


def test_signature_drops_inner(probe: tuple[Any, Any]):
    """The rendered signature is the wrapper's params minus the leading inner,
    so help()/inspect show the agent-facing arity."""
    ns, _ = probe

    def w(inner, x, y=1, *, z=2):
        return inner(x, y, z=z)

    wrap("probe.fn", w)  # pyright: ignore[reportUnknownArgumentType]
    assert str(inspect.signature(ns.fn)) == "(x, y=1, *, z=2)"


def test_added_kwarg_shows_in_signature(probe: tuple[Any, Any]):
    """A wrapper may add a keyword; it appears in the rendered signature
    (fleet's `label` pattern)."""
    ns, _ = probe

    def w(inner, x, y=1, *, z=2, extra=None):
        return inner(x, y, z=z)

    wrap("probe.fn", w)  # pyright: ignore[reportUnknownArgumentType]
    assert "extra" in inspect.signature(ns.fn).parameters


def test_wrapper_docstring_becomes_contract(probe: tuple[Any, Any]):
    """A wrapper that writes its own docstring supplies the new contract."""
    ns, _ = probe

    def w(inner, *a, **k):
        """enhanced doc."""
        return inner(*a, **k)

    wrap("probe.fn", w)  # pyright: ignore[reportUnknownArgumentType]
    assert inspect.getdoc(ns.fn) == "enhanced doc."


def test_transparent_wrapper_inherits_docstring(probe: tuple[Any, Any]):
    """A wrapper with no docstring inherits the wrapped function's."""
    ns, _ = probe

    def w(inner, *a, **k):
        return inner(*a, **k)

    wrap("probe.fn", w)  # pyright: ignore[reportUnknownArgumentType]
    assert inspect.getdoc(ns.fn) == "probe fn doc."


def test_function_attached_member_carries_through(probe: tuple[Any, Any]):
    """Function-attached members (e.g. ava.understand.UnderstandError) survive
    the wrap so the agent's documented attribute access keeps working."""
    ns, _ = probe
    wrap("probe.fn", lambda inner, *a, **k: inner(*a, **k))  # pyright: ignore[reportUnknownArgumentType]
    assert ns.fn.MARKER == "attached"


def test_stack_last_registered_is_outermost(probe: tuple[Any, Any]):
    """Two layers nest in registration order — last registered wraps outermost."""
    ns, _ = probe
    calls: list[str] = []

    def inner_layer(inner, *a, **k):
        calls.append("inner-pre")
        r = inner(*a, **k)
        calls.append("inner-post")
        return r

    def outer_layer(inner, *a, **k):
        calls.append("outer-pre")
        r = inner(*a, **k)
        calls.append("outer-post")
        return r

    with PluginContext("plugin_a"):
        wrap("probe.fn", inner_layer)  # pyright: ignore[reportUnknownArgumentType]
    with PluginContext("plugin_b"):
        wrap("probe.fn", outer_layer)  # pyright: ignore[reportUnknownArgumentType]
    assert [p for p, _ in wraps.stack("probe.fn")] == ["plugin_a", "plugin_b"]
    ns.fn(0)
    assert calls == ["outer-pre", "inner-pre", "inner-post", "outer-post"]


def test_short_circuit_skips_inner(probe: tuple[Any, Any]):
    """A wrapper that does not call inner short-circuits (block / replace)."""
    ns, _ = probe
    wrap("probe.fn", lambda _inner, *_a, **_k: "blocked")  # pyright: ignore[reportUnknownArgumentType]
    assert ns.fn(1) == "blocked"


def test_retry_calls_inner_twice(probe: tuple[Any, Any]):
    """A wrapper may call inner multiple times (retry)."""
    ns, _ = probe

    def double(inner, *a, **k):
        return f"{inner(*a, **k)}|{inner(*a, **k)}"

    wrap("probe.fn", double)  # pyright: ignore[reportUnknownArgumentType]
    assert ns.fn(1) == "fn(1,1,2)|fn(1,1,2)"


# ── activation telemetry (issue #40) ────────────────────────────────────────


@pytest.fixture
def activations(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str, str]]:
    """Capture (plugin, surface, identifier, detail) per recorded activation.

    Keeps the real `record`'s attribution gate — an unattributed firing is
    dropped rather than captured — so these tests read the emitted stream, not
    the call log."""
    recorded: list[tuple[str, str, str, str]] = []

    def spy(plugin: str | None, surface: str, identifier: str, *, detail: str = "") -> None:
        if plugin is not None:
            recorded.append((plugin, surface, identifier, detail))

    monkeypatch.setattr(plugin_activation, "record", spy)
    return recorded


def test_short_circuit_records_an_activation(
    probe: tuple[Any, Any], activations: list[tuple[str, str, str, str]]
):
    """A plugin layer that skipped inner changed control flow — that is the fact
    philosophy §6 measures, keyed by the ledger's own surface/identifier."""
    ns, _ = probe
    with PluginContext("myplugin"):
        wrap("probe.fn", lambda _inner, *_a, **_k: "blocked")  # pyright: ignore[reportUnknownArgumentType]

    assert ns.fn(1) == "blocked"
    assert activations == [("myplugin", "sdkWraps", "probe.fn", "inner_calls=0")]


def test_retry_records_an_activation(
    probe: tuple[Any, Any], activations: list[tuple[str, str, str, str]]
):
    ns, _ = probe

    def double(inner, *a, **k):
        return f"{inner(*a, **k)}|{inner(*a, **k)}"

    with PluginContext("myplugin"):
        wrap("probe.fn", double)  # pyright: ignore[reportUnknownArgumentType]

    ns.fn(1)
    assert activations == [("myplugin", "sdkWraps", "probe.fn", "inner_calls=2")]


def test_transparent_wrap_records_nothing(
    probe: tuple[Any, Any], activations: list[tuple[str, str, str, str]]
):
    """A layer that calls inner exactly once always runs once installed, so
    counting it would measure the installation rather than the shim."""
    ns, _ = probe
    with PluginContext("myplugin"):
        wrap("probe.fn", lambda inner, *a, **k: inner(*a, **k))  # pyright: ignore[reportUnknownArgumentType]

    ns.fn(1)
    assert activations == []


def test_unattributed_wrap_records_nothing(
    probe: tuple[Any, Any], activations: list[tuple[str, str, str, str]]
):
    """A wrap installed outside a PluginContext is nobody's contribution — it
    stays in `stack()` but is absent from the ledger and from telemetry."""
    ns, _ = probe
    wrap("probe.fn", lambda _inner, *_a, **_k: "blocked")  # pyright: ignore[reportUnknownArgumentType]

    assert ns.fn(1) == "blocked"
    assert activations == []


def test_counting_proxy_keeps_inner_transparent(probe: tuple[Any, Any]):
    """The activation counter hands the wrapper a proxy for `inner`; it must
    present as the callable it replaces, or a wrapper that introspects `inner`
    (signature, attached members) would break under telemetry."""
    ns, _ = probe
    seen: dict[str, Any] = {}

    def w(inner: Any, *a: Any, **k: Any) -> Any:
        seen["signature"] = str(inspect.signature(inner))
        seen["marker"] = inner.MARKER
        return inner(*a, **k)

    with PluginContext("myplugin"):
        wrap("probe.fn", w)  # pyright: ignore[reportUnknownArgumentType]

    assert ns.fn(1) == "fn(1,1,2)"
    assert seen == {"signature": "(x, y=1, *, z=2)", "marker": "attached"}


def test_activation_recording_never_perturbs_the_call(
    probe: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch
):
    """Side-channel contract: a broken event sink must not change what the
    wrapped call returns."""
    ns, _ = probe

    def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("sink down")

    monkeypatch.setattr(plugin_activation, "emit", boom)
    with PluginContext("myplugin"):
        wrap("probe.fn", lambda _inner, *_a, **_k: "blocked")  # pyright: ignore[reportUnknownArgumentType]

    assert ns.fn(1) == "blocked"


def test_clear_wraps_restores_and_empties(probe: tuple[Any, Any]):
    """clear_wraps restores the original callable and empties the registry."""
    ns, fn = probe
    wrap("probe.fn", lambda _inner, *_a, **_k: "wrapped")  # pyright: ignore[reportUnknownArgumentType]
    assert ns.fn(1) == "wrapped"

    wraps.clear_wraps()
    assert ns.fn is fn  # restored to the captured original
    assert wraps.stack("probe.fn") == []
    assert wraps.wrappers() == {}


def test_wrap_captures_the_base_callable_below_a_metering_recorder(
    probe: tuple[Any, Any],
) -> None:
    """Task #3427: the SDK metering recorder (installed at SDK import, before
    plugins load) is not a wrap layer. A wrap captures and chains over the base
    callable below it — so clear_wraps restores the base, and no stale recorder
    stays alive inside the wrap chain."""
    from ava import sdk_metering

    ns, fn = probe
    ns.fn = sdk_metering._make_recorder(fn, "probe.fn")

    with PluginContext("myplugin"):
        wrap("probe.fn", lambda inner, *a, **kw: inner(*a, **kw))  # pyright: ignore[reportUnknownArgumentType]

    assert wraps._ORIGINALS["probe.fn"] is fn  # the base, not the proxy
    assert ns.fn("x") == "fn(x,1,2)"  # the chain still runs

    wraps.clear_wraps()
    assert ns.fn is fn


def test_wrap_invalid_target_raises(probe: tuple[Any, Any]):
    with pytest.raises(wraps.WrapTargetError, match="dotted path"):
        wrap("probe..fn", lambda inner: inner())  # pyright: ignore[reportUnknownArgumentType]
    with pytest.raises(wraps.WrapTargetError, match="dotted path"):
        wrap("_private.fn", lambda inner: inner())  # pyright: ignore[reportUnknownArgumentType]


def test_wrap_noncallable_target_raises(probe: tuple[Any, Any]):
    ns, _ = probe
    ns.value = 3
    with pytest.raises(wraps.WrapTargetError, match="not callable"):
        wrap("probe.value", lambda inner: inner())  # pyright: ignore[reportUnknownArgumentType]
