"""`ava.extend` wrap primitive + `ava._extend.scan_and_load` behavior guards.

Wrap primitive:
- register + install: `wrap(target, wrapper)` replaces the ava callable
- introspection: `stack(target)` lists (plugin, wrapper) innermost-first;
  `wrappers()` maps every target
- determinism: registration order == plugin load order, last registered outermost
- metadata: rendered signature drops `inner`, docstring is the wrapper's own or
  the inherited one, function-attached members carry through
- control flow: short-circuit (skip inner) + retry (call inner twice)
- plugin attribution via PluginContext
- clear_wraps restores originals + empties the registry (the reload-free teardown)
- target errors: malformed dotted path, non-callable target

scan_and_load: dir scan + plugin.py import side effects, enabled-set filtering.

A fake `ava.probe` namespace holds the wrap targets so the real SDK is never
patched; the fixture clears wraps then removes the namespace.
"""

import inspect
import textwrap
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import ava
from ava import _extend
from ava._extend import scan_and_load, wrap
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
    _extend.clear_wraps()  # restore probe.fn before the namespace disappears
    delattr(ava, "probe")


def test_wrap_installs_and_stack_lists(probe: tuple[Any, Any]):
    """wrap replaces the target; stack reports one (plugin, wrapper) layer."""
    ns, fn = probe

    def w(inner, *a, **k):
        return inner(*a, **k)

    returned = wrap("probe.fn", w)  # pyright: ignore[reportUnknownArgumentType]
    assert returned is w  # returns the wrapper so the caller keeps a reference
    assert ns.fn is not fn  # target replaced by the chained closure
    assert _extend.stack("probe.fn") == [("<unknown>", w)]
    assert ava.probe.fn(9) == "fn(9,1,2)"  # still calls through


def test_wrappers_maps_all_targets(probe: tuple[Any, Any]):
    wrap("probe.fn", lambda inner, *a, **k: inner(*a, **k))  # pyright: ignore[reportUnknownArgumentType]
    allmap = _extend.wrappers()
    assert set(allmap) == {"probe.fn"}
    assert len(allmap["probe.fn"]) == 1


def test_plugin_attribution_from_context(probe: tuple[Any, Any]):
    """Inside PluginContext the layer is attributed to that plugin."""

    def w(inner, *a, **k):
        return inner(*a, **k)

    with PluginContext("myplugin"):
        wrap("probe.fn", w)  # pyright: ignore[reportUnknownArgumentType]
    assert _extend.stack("probe.fn") == [("myplugin", w)]


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
    assert [p for p, _ in _extend.stack("probe.fn")] == ["plugin_a", "plugin_b"]
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


def test_clear_wraps_restores_and_empties(probe: tuple[Any, Any]):
    """clear_wraps restores the original callable and empties the registry."""
    ns, fn = probe
    wrap("probe.fn", lambda _inner, *_a, **_k: "wrapped")  # pyright: ignore[reportUnknownArgumentType]
    assert ns.fn(1) == "wrapped"

    _extend.clear_wraps()
    assert ns.fn is fn  # restored to the captured original
    assert _extend.stack("probe.fn") == []
    assert _extend.wrappers() == {}


def test_wrap_invalid_target_raises(probe: tuple[Any, Any]):
    with pytest.raises(_extend.WrapTargetError, match="dotted path"):
        wrap("probe..fn", lambda inner: inner())  # pyright: ignore[reportUnknownArgumentType]
    with pytest.raises(_extend.WrapTargetError, match="dotted path"):
        wrap("_private.fn", lambda inner: inner())  # pyright: ignore[reportUnknownArgumentType]


def test_wrap_noncallable_target_raises(probe: tuple[Any, Any]):
    ns, _ = probe
    ns.value = 3
    with pytest.raises(_extend.WrapTargetError, match="not callable"):
        wrap("probe.value", lambda inner: inner())  # pyright: ignore[reportUnknownArgumentType]


# ---- scan_and_load (moved from ava._wraps into ava._extend) ----------------


def test_scan_and_load_returns_empty_when_dir_missing(tmp_path: Path):
    assert scan_and_load(tmp_path / "nope") == []


def test_scan_and_load_loads_plugins_in_sorted_order(tmp_path: Path):
    for name in ("zebra", "alpha", "mango"):
        plugin_dir = tmp_path / name
        plugin_dir.mkdir()
        (plugin_dir / "plugin.py").write_text(
            textwrap.dedent(f"""
                # plugin {name} loaded
                LOADED = "{name}"
            """).strip()
        )
    assert scan_and_load(tmp_path) == ["alpha", "mango", "zebra"]


def test_scan_and_load_skips_non_directories_and_missing_plugin_py(tmp_path: Path):
    (tmp_path / "not_a_plugin.txt").write_text("noise")
    empty = tmp_path / "empty_subdir"
    empty.mkdir()
    (empty / "other.py").write_text("# no plugin.py here")
    valid = tmp_path / "valid"
    valid.mkdir()
    (valid / "plugin.py").write_text("VALID = True")
    assert scan_and_load(tmp_path) == ["valid"]


def test_scan_and_load_propagates_plugin_errors(tmp_path: Path):
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "plugin.py").write_text("raise RuntimeError('plugin bug')")
    with pytest.raises(RuntimeError, match="plugin bug"):
        scan_and_load(tmp_path)


def test_scan_and_load_expands_user_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert scan_and_load("~/no_plugins") == []


def _make_plugin(root: Path, name: str) -> None:
    p = root / name
    p.mkdir()
    (p / "plugin.py").write_text(f'LOADED = "{name}"\n')


def test_scan_and_load_default_uses_paths_plugins_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from shared.config import settings

    monkeypatch.setattr(settings.general, "ava_home", tmp_path / "ava")
    plugins = tmp_path / "ava" / "plugins"
    plugins.mkdir(parents=True)
    _make_plugin(plugins, "auto_discovered")
    assert scan_and_load() == ["auto_discovered"]


def test_scan_and_load_explicit_enabled_set(tmp_path: Path):
    for name in ("foo", "bar", "baz"):
        _make_plugin(tmp_path, name)
    assert scan_and_load(tmp_path, enabled={"foo", "baz"}) == ["baz", "foo"]


def test_scan_and_load_explicit_enabled_empty_set(tmp_path: Path):
    _make_plugin(tmp_path, "foo")
    assert scan_and_load(tmp_path, enabled=set()) == []


def test_scan_and_load_enabled_skips_names_not_in_set(tmp_path: Path):
    _make_plugin(tmp_path, "foo")
    _make_plugin(tmp_path, "ghost")
    assert scan_and_load(tmp_path, enabled={"foo"}) == ["foo"]
