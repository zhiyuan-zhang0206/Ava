"""ava.__getattr__ env-gated lazy plugin-namespace load + ava._ensure_plugins_loaded.

A process an agent launched (AVA_AGENT_ID forwarded, no bootstrap to hook — a
bare `python x.py` in a persistent shell session) self-loads plugin namespaces
on the first unknown `ava.X`; gateway / cli / the agent process itself keep the
fail-fast AttributeError.

These lock the gating matrix + the once-latch so a future edit can't silently
(a) start loading plugins in the gateway / cli, (b) re-run _load_extensions in
the agent process (which would clear the built-in repair/compact hooks that
build_graph — not _load_extensions — re-registers), or (c) turn a dunder probe
into a plugin load.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

import ava
import ava._boot as boot


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # Each test drives _plugins_loaded + boot identity explicitly; snapshot-restore
    # so nothing leaks between tests. Save existing plugin namespace objects before
    # clearing so they can be re-registered — never permanently wipe namespaces
    # registered by other plugins during import ava (ava.memory, ava.tasks, ava.cwd).
    monkeypatch.setattr(ava, "_plugins_loaded", False)
    monkeypatch.setattr(boot, "_agent_id", boot._agent_id)
    monkeypatch.setattr(boot, "_owns_loop", boot._owns_loop)
    # Save existing namespace objects before clearing
    _saved_ns: dict[str, Any] = {}
    for _name in list(ava._REGISTERED_NAMESPACES):
        _obj = getattr(ava, _name, None)
        if _obj is not None:
            _saved_ns[_name] = _obj
    ava.clear_registered_namespaces()
    yield
    ava.clear_registered_namespaces()
    # Restore saved plugin namespaces (setattr directly, bypass register_namespace
    # conflict checks since we know these were originally here)
    for _name, _obj in _saved_ns.items():
        setattr(ava, _name, _obj)
        if _name not in ava._REGISTERED_NAMESPACES:
            ava._REGISTERED_NAMESPACES[_name] = "<restored>"
        if _name not in ava.__all_for_ava__:
            ava.__all_for_ava__.append(_name)


def _spy_loader(monkeypatch: pytest.MonkeyPatch, *, register: str | None) -> list[int]:
    """Replace agent.graph._build._load_extensions (reached by _ensure_plugins_loaded
    via importlib) with a spy that records calls and optionally registers a namespace.
    Avoids the heavy, DB-touching real load in a unit test."""
    import agent.graph._build as build

    calls: list[int] = []

    def fake() -> None:
        calls.append(1)
        if register is not None:
            ava.register_namespace(register, SimpleNamespace(ping=lambda: "pong", __doc__="t"))

    monkeypatch.setattr(build, "_load_extensions", fake)
    return calls


def _as_launched_child(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(boot, "_agent_id", None)
    monkeypatch.setattr(boot, "_owns_loop", True)
    monkeypatch.setenv("AVA_AGENT_ID", "42")


def test_lazy_load_fires_in_launched_child(monkeypatch: pytest.MonkeyPatch) -> None:
    _as_launched_child(monkeypatch)
    calls = _spy_loader(monkeypatch, register="lazytasks")

    assert (
        ava.lazytasks.ping() == "pong"
    )  # first access triggers the load  # type: ignore[attr-defined]
    assert calls == [1]
    assert ava._plugins_loaded is True


def test_lazy_load_latches_once(monkeypatch: pytest.MonkeyPatch) -> None:
    _as_launched_child(monkeypatch)
    calls = _spy_loader(monkeypatch, register="lazytasks")

    _ = ava.lazytasks  # loads  # type: ignore[attr-defined]
    # A later unknown miss must NOT reload (latched) — it fails fast instead.
    with pytest.raises(AttributeError):
        _ = ava.still_unknown  # type: ignore[attr-defined]
    assert calls == [1]


def test_no_lazy_load_without_agent_id(monkeypatch: pytest.MonkeyPatch) -> None:
    # gateway / cli: no AVA_AGENT_ID -> behavior byte-identical to before the fix
    # (same AttributeError message, loader never touched).
    monkeypatch.setattr(boot, "_agent_id", None)
    monkeypatch.setattr(boot, "_owns_loop", True)
    monkeypatch.delenv("AVA_AGENT_ID", raising=False)
    calls = _spy_loader(monkeypatch, register=None)

    with pytest.raises(AttributeError, match=r"module 'ava' has no attribute 'nope_xyz'"):
        _ = ava.nope_xyz  # type: ignore[attr-defined]
    assert calls == []


def test_no_lazy_load_in_agent_process(monkeypatch: pytest.MonkeyPatch) -> None:
    # owns_loop=True + established id = the agent process. A typo must fail fast,
    # not re-run _load_extensions — which clears all hooks and would drop the
    # built-in ones build_graph registers after it.
    monkeypatch.setenv("AVA_AGENT_ID", "7")
    boot.establish(7, owns_loop=True)
    calls = _spy_loader(monkeypatch, register=None)

    with pytest.raises(AttributeError):
        _ = ava.nope_xyz  # type: ignore[attr-defined]
    assert calls == []


def test_underscore_names_never_lazy_load(monkeypatch: pytest.MonkeyPatch) -> None:
    # A dunder / private probe (copy, pickle, hasattr on `_x`) must not trigger a
    # plugin load even in a launched child — plugin namespaces are never
    # underscore-prefixed.
    _as_launched_child(monkeypatch)
    calls = _spy_loader(monkeypatch, register=None)

    with pytest.raises(AttributeError):
        _ = ava._some_private  # type: ignore[attr-defined]
    assert calls == []


def test_db_url_forward_wins_over_lazy_load(monkeypatch: pytest.MonkeyPatch) -> None:
    # DB_URL/REDIS_URL/GATEWAY_URL forward to _settings and must return before the
    # lazy branch — even in a launched child, accessing ava.DB_URL never loads.
    _as_launched_child(monkeypatch)
    calls = _spy_loader(monkeypatch, register=None)

    assert isinstance(ava.DB_URL, str)
    assert calls == []


def test_ensure_plugins_loaded_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _spy_loader(monkeypatch, register=None)

    ava._ensure_plugins_loaded()
    ava._ensure_plugins_loaded()

    assert calls == [1]  # latched: loads at most once per process
    assert ava._plugins_loaded is True


def _spy_member_loader(
    monkeypatch: pytest.MonkeyPatch, *, namespace: str, member: str
) -> list[int]:
    """Loader spy that registers a plugin MEMBER on an existing framework
    namespace (ava.self / ava.ui) — the shape ava_fleet uses for log/notify."""
    import agent.graph._build as build

    calls: list[int] = []

    def fake() -> None:
        calls.append(1)
        ava.register_namespace_member(namespace, member, lambda: "pong")

    monkeypatch.setattr(build, "_load_extensions", fake)
    return calls


def test_member_lazy_load_on_ava_self(monkeypatch: pytest.MonkeyPatch) -> None:
    # ava.self exists as a module, so ava.__getattr__ never fires for
    # ava.self.<missing member> — ava/self.py's own __getattr__ must trigger
    # the shared lazy load in a launched child.
    _as_launched_child(monkeypatch)
    calls = _spy_member_loader(monkeypatch, namespace="self", member="lazylog")

    assert ava.self.lazylog() == "pong"  # type: ignore[attr-defined]
    assert calls == [1]


def test_member_lazy_load_on_ava_ui(monkeypatch: pytest.MonkeyPatch) -> None:
    _as_launched_child(monkeypatch)
    calls = _spy_member_loader(monkeypatch, namespace="ui", member="lazynotify")

    assert ava.ui.lazynotify() == "pong"  # type: ignore[attr-defined]
    assert calls == [1]


def test_member_fail_fast_outside_child(monkeypatch: pytest.MonkeyPatch) -> None:
    # No AVA_AGENT_ID: gateway/cli semantics — missing members on ava.self /
    # ava.ui stay a fail-fast AttributeError and the loader never runs.
    monkeypatch.setattr(boot, "_agent_id", None)
    monkeypatch.setattr(boot, "_owns_loop", True)
    monkeypatch.delenv("AVA_AGENT_ID", raising=False)
    calls = _spy_member_loader(monkeypatch, namespace="self", member="lazylog")

    with pytest.raises(AttributeError):
        _ = ava.self.lazylog  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        _ = ava.ui.lazynotify  # type: ignore[attr-defined]
    assert calls == []
