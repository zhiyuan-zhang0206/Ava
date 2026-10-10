"""Wrap primitive behavior guards (`ava/sdk_surface/wraps.py`).

Wrap primitive:
- install: `apply_wrap(target, wrapper, plugin, layers=)` replaces the ava callable, records the
  layer in the ledger, and returns the undo
- introspection: `stack(target)` lists (plugin, wrapper) innermost-first;
  `wrappers()` maps every target
- determinism: install order == plugin load order, last installed outermost
- metadata: rendered signature drops `inner`, docstring is the wrapper's own or
  the inherited one, function-attached members carry through
- control flow: short-circuit (skip inner) + retry (call inner twice)
- plugin attribution: the layer is recorded under the plugin that declared it
- activation telemetry: a plugin layer that does not call inner exactly once
  records one `plugin_activation`; a transparent layer does not
- the undo restores the original callable and empties the registry
- target errors: malformed dotted path, non-callable target

A fake `ava.probe` namespace holds the wrap targets so the real SDK is never
patched; the fixtures undo every installed wrap then remove the namespace.
"""

import inspect
import types
from collections.abc import Callable, Iterator
from typing import Any

import pytest

import ava
from ava.sdk_surface import wraps
from base.agents.sdk import call_policy
from base.packages.plugins import activation


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
    ava_any.probe = ns  # direct bind — bypasses the namespace install to isolate the wrap primitive
    yield ns, fn
    delattr(ava, "probe")


class Probe:
    """The fixture's wrap layer ledger and the installs it holds.

    `probe(...)` installs one layer (into the fixture's own ledger — the install passes
    its ledger explicitly) and returns its undo; `stack` / `wrappers` read the ledger
    back with the same projection `wraps.stack` applies to an installation's map, so a
    test can assert the registry view without an installation in the process.
    """

    def __init__(self) -> None:
        self.layers: dict[str, list[wraps.WrapLayer]] = {}
        self._undos: list[Callable[[], None]] = []

    def __call__(
        # `Any` deliberately (the alias the fixture replaced was `Callable[..., ...]`):
        # the tests pass quick untyped lambdas as wrappers, and a concrete slot type
        # makes pyright report their partially-unknown types at 20 call sites.
        self,
        wrapper: Any,
        plugin: str = "myplugin",
        target: str = "probe.fn",
    ) -> Callable[[], None]:
        undo = wraps.apply_wrap(target, wrapper, plugin, layers=self.layers)
        done = [False]

        def once() -> None:
            if not done[0]:
                done[0] = True
                undo()

        self._undos.append(once)
        return once

    def stack(self, target: str) -> list[tuple[str, Callable[..., Any]]]:
        return [(layer.plugin, layer.wrapper) for layer in self.layers.get(target, ())]

    def wrappers(self) -> dict[str, list[tuple[str, Callable[..., Any]]]]:
        return {target: self.stack(target) for target in self.layers}

    def tear_down(self) -> None:
        for undo in reversed(self._undos):
            undo()


@pytest.fixture
def apply(probe: tuple[Any, Any]) -> Iterator[Probe]:
    """`apply(wrapper, plugin="myplugin", target="probe.fn")` installs one layer and returns its
    undo; whatever is still installed is undone newest-first before `ava.probe` goes away."""
    fixture = Probe()
    yield fixture
    fixture.tear_down()


def test_wrap_installs_and_stack_lists(probe: tuple[Any, Any], apply: Probe):
    """wrap replaces the target; stack reports one (plugin, wrapper) layer."""
    ns, fn = probe

    def w(inner, *a, **k):
        return inner(*a, **k)

    undo = apply(w, "myplugin")
    assert callable(undo)  # the undo that reverses this layer
    assert ns.fn is not fn  # target replaced by the chained closure
    assert apply.stack("probe.fn") == [("myplugin", w)]
    assert ava.probe.fn(9) == "fn(9,1,2)"  # still calls through


def test_wrappers_maps_all_targets(probe: tuple[Any, Any], apply: Probe):
    apply(lambda inner, *a, **k: inner(*a, **k))
    allmap = apply.wrappers()
    assert set(allmap) == {"probe.fn"}
    assert len(allmap["probe.fn"]) == 1


def test_layers_are_attributed_to_their_declaring_plugin(probe: tuple[Any, Any], apply: Probe):
    """Each layer is recorded under the plugin that declared it."""

    def w(inner, *a, **k):
        return inner(*a, **k)

    apply(w, "myplugin")
    assert apply.stack("probe.fn") == [("myplugin", w)]


def test_signature_drops_inner(probe: tuple[Any, Any], apply: Probe):
    """The rendered signature is the wrapper's params minus the leading inner,
    so help()/inspect show the agent-facing arity."""
    ns, _ = probe

    def w(inner, x, y=1, *, z=2):
        return inner(x, y, z=z)

    apply(w)
    assert str(inspect.signature(ns.fn)) == "(x, y=1, *, z=2)"


def test_added_kwarg_shows_in_signature(probe: tuple[Any, Any], apply: Probe):
    """A wrapper may add a keyword; it appears in the rendered signature
    (fleet's `label` pattern)."""
    ns, _ = probe

    def w(inner, x, y=1, *, z=2, extra=None):
        return inner(x, y, z=z)

    apply(w)
    assert "extra" in inspect.signature(ns.fn).parameters


def test_wrapper_docstring_becomes_contract(probe: tuple[Any, Any], apply: Probe):
    """A wrapper that writes its own docstring supplies the new contract."""
    ns, _ = probe

    def w(inner, *a, **k):
        """enhanced doc."""
        return inner(*a, **k)

    apply(w)
    assert inspect.getdoc(ns.fn) == "enhanced doc."


def test_transparent_wrapper_inherits_docstring(probe: tuple[Any, Any], apply: Probe):
    """A wrapper with no docstring inherits the wrapped function's."""
    ns, _ = probe

    def w(inner, *a, **k):
        return inner(*a, **k)

    apply(w)
    assert inspect.getdoc(ns.fn) == "probe fn doc."


def test_function_attached_member_carries_through(probe: tuple[Any, Any], apply: Probe):
    """Function-attached members (e.g. ava.understand.UnderstandError) survive
    the wrap so the agent's documented attribute access keeps working."""
    ns, _ = probe
    apply(lambda inner, *a, **k: inner(*a, **k))
    assert ns.fn.MARKER == "attached"


def test_stack_last_registered_is_outermost(probe: tuple[Any, Any], apply: Probe):
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

    apply(inner_layer, "plugin_a")
    apply(outer_layer, "plugin_b")
    assert [p for p, _ in apply.stack("probe.fn")] == ["plugin_a", "plugin_b"]
    ns.fn(0)
    assert calls == ["outer-pre", "inner-pre", "inner-post", "outer-post"]


def test_short_circuit_skips_inner(probe: tuple[Any, Any], apply: Probe):
    """A wrapper that does not call inner short-circuits (block / replace)."""
    ns, _ = probe
    apply(lambda _inner, *_a, **_k: "blocked")
    assert ns.fn(1) == "blocked"


def test_retry_calls_inner_twice(probe: tuple[Any, Any], apply: Probe):
    """A wrapper may call inner multiple times (retry)."""
    ns, _ = probe

    def double(inner, *a, **k):
        return f"{inner(*a, **k)}|{inner(*a, **k)}"

    apply(double)
    assert ns.fn(1) == "fn(1,1,2)|fn(1,1,2)"


# ── activation telemetry (issue #40) ────────────────────────────────────────


@pytest.fixture
def activations(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str, str]]:
    """Capture (plugin, surface, identifier, detail) per recorded activation.

    Keeps the real `record`'s attribution gate — an unattributed firing is
    dropped rather than captured — so these tests read the emitted stream, not
    the call log."""
    recorded: list[tuple[str, str, str, str]] = []

    def spy(
        plugin: str | None, surface: str, identifier: str, *, detail: str = "", model: str = ""
    ) -> None:
        if plugin is not None:
            recorded.append((plugin, surface, identifier, detail))

    monkeypatch.setattr(activation, "record", spy)
    return recorded


def test_short_circuit_records_an_activation(
    probe: tuple[Any, Any], apply: Probe, activations: list[tuple[str, str, str, str]]
):
    """A plugin layer that skipped inner changed control flow — that is the fact
    philosophy §6 measures, keyed by the ledger's own surface/identifier."""
    ns, _ = probe
    apply(lambda _inner, *_a, **_k: "blocked")

    assert ns.fn(1) == "blocked"
    assert activations == [("myplugin", "sdkWraps", "probe.fn", "inner_calls=0")]


def test_retry_records_an_activation(
    probe: tuple[Any, Any], apply: Probe, activations: list[tuple[str, str, str, str]]
):
    ns, _ = probe

    def double(inner, *a, **k):
        return f"{inner(*a, **k)}|{inner(*a, **k)}"

    apply(double)

    ns.fn(1)
    assert activations == [("myplugin", "sdkWraps", "probe.fn", "inner_calls=2")]


def test_transparent_wrap_records_nothing(
    probe: tuple[Any, Any], apply: Probe, activations: list[tuple[str, str, str, str]]
):
    """A layer that calls inner exactly once always runs once installed, so
    counting it would measure the installation rather than the shim."""
    ns, _ = probe
    apply(lambda inner, *a, **k: inner(*a, **k))

    ns.fn(1)
    assert activations == []


def test_counting_proxy_keeps_inner_transparent(probe: tuple[Any, Any], apply: Probe):
    """The activation counter hands the wrapper a proxy for `inner`; it must
    present as the callable it replaces, or a wrapper that introspects `inner`
    (signature, attached members) would break under telemetry."""
    ns, _ = probe
    seen: dict[str, Any] = {}

    def w(inner: Any, *a: Any, **k: Any) -> Any:
        seen["signature"] = str(inspect.signature(inner))
        seen["marker"] = inner.MARKER
        return inner(*a, **k)

    apply(w)

    assert ns.fn(1) == "fn(1,1,2)"
    assert seen == {"signature": "(x, y=1, *, z=2)", "marker": "attached"}


def test_activation_recording_never_perturbs_the_call(
    probe: tuple[Any, Any], apply: Probe, monkeypatch: pytest.MonkeyPatch
):
    """Side-channel contract: a broken event sink must not change what the
    wrapped call returns."""
    ns, _ = probe

    def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("sink down")

    monkeypatch.setattr(activation, "emit", boom)
    apply(lambda _inner, *_a, **_k: "blocked")

    assert ns.fn(1) == "blocked"


def test_undo_restores_and_empties(probe: tuple[Any, Any], apply: Probe):
    """The undo restores the original callable and empties the registry."""
    ns, fn = probe
    undo = apply(lambda _inner, *_a, **_k: "wrapped")
    assert ns.fn(1) == "wrapped"

    undo()
    assert ns.fn is fn  # restored to the captured original
    assert apply.stack("probe.fn") == []
    assert apply.wrappers() == {}


def test_undoing_newest_first_peels_one_layer_at_a_time(probe: tuple[Any, Any], apply: Probe):
    """With two layers stacked, undoing the outer one leaves the inner one installed and live."""
    ns, fn = probe
    undo_a = apply(lambda inner, *a, **k: f"a({inner(*a, **k)})", "plugin_a")
    undo_b = apply(lambda inner, *a, **k: f"b({inner(*a, **k)})", "plugin_b")
    assert ns.fn(0) == "b(a(fn(0,1,2)))"

    undo_b()
    assert [p for p, _ in apply.stack("probe.fn")] == ["plugin_a"]
    assert ns.fn(0) == "a(fn(0,1,2))"
    undo_a()
    assert ns.fn is fn


def test_wrap_captures_the_base_callable_below_a_metering_recorder(
    probe: tuple[Any, Any], apply: Probe
) -> None:
    """Task #3427: the SDK metering recorder (installed at SDK import, before
    plugins load) is not a wrap layer. A wrap captures and chains over the base
    callable below it — so the undo restores the base, and no stale recorder
    stays alive inside the wrap chain."""
    from ava.sdk_surface import metering

    ns, fn = probe
    ns.fn = metering._make_recorder(fn, "probe.fn", call_policy.SamplingPolicyOwner())

    undo = apply(lambda inner, *a, **kw: inner(*a, **kw))

    assert ns.fn("x") == "fn(x,1,2)"  # the chain still runs

    undo()
    assert ns.fn is fn  # the base, not the recorder proxy


def _bare(inner: Callable[[], Any]) -> Any:
    return inner()


def test_wrap_invalid_target_raises(probe: tuple[Any, Any]):
    with pytest.raises(wraps.WrapTargetError, match="dotted path"):
        wraps.apply_wrap("probe..fn", _bare, "myplugin", layers={})
    with pytest.raises(wraps.WrapTargetError, match="dotted path"):
        wraps.apply_wrap("_private.fn", _bare, "myplugin", layers={})


def test_wrap_noncallable_target_raises(probe: tuple[Any, Any]):
    ns, _ = probe
    ns.value = 3
    with pytest.raises(wraps.WrapTargetError, match="not callable"):
        wraps.apply_wrap("probe.value", _bare, "myplugin", layers={})


def test_wrap_missing_target_is_a_typed_refusal(probe: tuple[Any, Any]) -> None:
    with pytest.raises(wraps.WrapTargetError, match="does not resolve"):
        wraps.apply_wrap("probe.missing", _bare, "myplugin", layers={})


def test_wrap_descriptor_attribute_error_is_not_a_refusal(
    probe: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    error = AttributeError("descriptor implementation failed")

    class BrokenTarget:
        @property
        def broken(self) -> Any:
            raise error

    namespace, _fn = probe
    monkeypatch.setattr(namespace, "target", BrokenTarget(), raising=False)
    with pytest.raises(AttributeError) as caught:
        wraps.apply_wrap("probe.target.broken", _bare, "myplugin", layers={})
    assert caught.value is error
