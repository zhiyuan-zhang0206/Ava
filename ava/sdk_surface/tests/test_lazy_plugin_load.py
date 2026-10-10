"""ava.__getattr__ env-gated lazy plugin-namespace load + ava.ensure_plugins_loaded.

A process an agent launched (AVA_AGENT_ID forwarded, no bootstrap to hook — a
bare `python x.py` in a persistent shell session) self-loads plugin namespaces
on the first unknown `ava.X`; gateway / cli / the agent process itself keep the
fail-fast AttributeError.

These lock the gating matrix + the once-per-process load (the installation slot) so a future edit can't silently
(a) start loading plugins in the gateway / cli, (b) re-run load_extensions in
the agent process (which would uninstall and reinstall the whole SDK surface under it), or (c) turn a dunder probe
into a plugin load.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Callable, Generator, Iterator, Sequence
from contextlib import contextmanager
from importlib.abc import Loader, MetaPathFinder
from importlib.machinery import ModuleSpec
from importlib.util import spec_from_loader
from types import ModuleType, SimpleNamespace

import pytest

import ava
from ava.sdk_surface import install
from base.agents.sdk import call_policy
from base.config.service_read import ConfigAuthority
from base.lm.catalog import ModelCatalog
from base.packages.plugins.extensions import (
    ExtensionRegistry,
    PluginContributions,
    SdkMember,
    SdkNamespace,
)
from tests.fixtures.pin_agent import pin_agent, pin_no_identity


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # These tests exercise lazy installation, independently of live sampling refresh.
    monkeypatch.setattr(call_policy, "policy", call_policy.SamplingPolicy)
    # Each test drives the installation slot + agent identity explicitly;
    # snapshot-restore so nothing leaks between tests. Any SDK surface already
    # installed in this process (ava.memory, ava.tasks, ava.cwd ...) is taken out
    # for the test and put back after, so the lazy loads below start from a surface
    # with no plugin namespaces and never wipe the others.
    prior = install.installed()
    install.uninstall()
    install.clear_load_attempt()  # a failed attempt from an earlier test is not ours
    yield
    install.uninstall()
    install.clear_load_attempt()
    if prior is not None:
        install.install(
            prior.registry,
            catalog=prior.catalog,
            authority=prior.authority,
            delivery_sender=prior.delivery_sender,
        )


def _installing(
    *plugins: tuple[str, PluginContributions],
    catalog: ModelCatalog,
    authority: ConfigAuthority,
) -> None:
    """What a real `load_extensions` does to the surface: install these plugins' declarations."""
    install.install(ExtensionRegistry(plugins), catalog=catalog, authority=authority)


def _spy_loader(monkeypatch: pytest.MonkeyPatch, *, register: str | None) -> list[int]:
    """Replace agent.extensions.load_extensions (reached by ensure_plugins_loaded
    via importlib) with a spy that records calls and optionally registers a namespace.
    Avoids the heavy, DB-touching real load in a unit test."""
    from agent import extensions

    calls: list[int] = []

    def fake(*, catalog: ModelCatalog, authority: ConfigAuthority, surface: bool = False) -> None:
        calls.append(1)
        if register is not None:
            _installing(
                (
                    register,
                    PluginContributions(
                        sdk_namespaces=(
                            SdkNamespace(
                                register, SimpleNamespace(ping=lambda: "pong", __doc__="t")
                            ),
                        )
                    ),
                ),
                catalog=catalog,
                authority=authority,
            )
            installed = install.installed()
            assert installed is not None
            assert installed.require_catalog() is catalog
            assert installed.authority is authority

    monkeypatch.setattr(extensions, "load_extensions", fake)
    return calls


def _as_launched_child(monkeypatch: pytest.MonkeyPatch) -> None:
    pin_no_identity()
    monkeypatch.setenv("AVA_AGENT_ID", "42")


def test_lazy_load_fires_in_launched_child(monkeypatch: pytest.MonkeyPatch) -> None:
    _as_launched_child(monkeypatch)
    calls = _spy_loader(monkeypatch, register="lazytasks")

    assert (
        ava.lazytasks.ping() == "pong"
    )  # first access triggers the load  # type: ignore[attr-defined]
    assert calls == [1]
    assert install.installed() is not None


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
    pin_no_identity()
    monkeypatch.delenv("AVA_AGENT_ID", raising=False)
    calls = _spy_loader(monkeypatch, register=None)

    with pytest.raises(AttributeError, match=r"module 'ava' has no attribute 'nope_xyz'"):
        _ = ava.nope_xyz  # type: ignore[attr-defined]
    assert calls == []


def test_no_lazy_load_in_agent_process(monkeypatch: pytest.MonkeyPatch) -> None:
    # owns_loop=True + established id = the agent process. A typo must fail fast,
    # not re-run load_extensions — which clears all hooks and would drop the
    # built-in ones build_graph registers after it.
    monkeypatch.setenv("AVA_AGENT_ID", "7")
    pin_agent(7, owns_loop=True)
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
    calls = _spy_loader(monkeypatch, register="lazytasks")

    ava.ensure_plugins_loaded()
    ava.ensure_plugins_loaded()

    assert calls == [1]  # loads at most once per process
    assert install.installed() is not None


@pytest.mark.parametrize(
    ("error_type", "message"),
    [
        (AttributeError, "original plugin boot error"),
        (ValueError, "original plugin boot error"),
        (RuntimeError, "original plugin boot error"),
        (OSError, "original plugin boot error"),
        (AttributeError, "partially initialized module 'agent.extensions' implementation bug"),
    ],
)
def test_ensure_plugins_loaded_preserves_unknown_failure_without_retry(
    monkeypatch: pytest.MonkeyPatch, error_type: type[Exception], message: str
) -> None:
    from agent import extensions

    error = error_type(message)
    calls: list[int] = []

    def boom(*, catalog: ModelCatalog, authority: ConfigAuthority, surface: bool = False) -> None:
        calls.append(1)
        raise error

    monkeypatch.setattr(extensions, "load_extensions", boom)
    for _ in range(2):
        with pytest.raises(error_type) as caught:
            ava.ensure_plugins_loaded()
        assert caught.value is error
    assert calls == [1]
    assert install.load_attempted()
    assert install.installed() is None


def test_failed_lazy_lookup_repeats_the_original_boot_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent import extensions

    _as_launched_child(monkeypatch)
    error = RuntimeError("lazy boot failed")

    def boom(*, catalog: ModelCatalog, authority: ConfigAuthority, surface: bool = False) -> None:
        raise error

    monkeypatch.setattr(extensions, "load_extensions", boom)
    for _ in range(2):
        with pytest.raises(RuntimeError) as caught:
            _ = ava.missing_after_failure
        assert caught.value is error


def test_faces_failure_is_not_marked_successful_and_repeats_original_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent import extensions

    calls = _spy_loader(monkeypatch, register="lazytasks")
    ava.ensure_plugins_loaded()
    surface = install.installed()
    assert surface is not None and not surface.faces
    error = RuntimeError("runtime faces boot failed")
    face_calls: list[int] = []

    def boom() -> None:
        face_calls.append(1)
        raise error

    monkeypatch.setattr(extensions, "load_agent_faces", boom)
    for full in (True, False, True):
        with pytest.raises(RuntimeError) as caught:
            ava.ensure_plugins_loaded(surface=not full)
        assert caught.value is error
    assert face_calls == calls == [1]
    assert install.installed() is surface and not surface.faces
    install.uninstall()
    assert not hasattr(ava, "lazytasks")


class _ReentrantLoader(Loader):
    def __init__(self, during_import: Callable[[], None], load: Callable[..., None]) -> None:
        self.during_import = during_import
        self.load = load

    def create_module(self, spec: ModuleSpec) -> ModuleType | None:
        return None

    def exec_module(self, module: ModuleType) -> None:
        self.during_import()
        vars(module)["load_extensions"] = self.load


class _ExtensionsFinder(MetaPathFinder):
    def __init__(self, loader: Loader) -> None:
        self.loader = loader

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None = None,
        target: ModuleType | None = None,
    ) -> ModuleSpec | None:
        if fullname == "agent.extensions":
            return spec_from_loader(fullname, self.loader)
        return None


@contextmanager
def _partial_extensions(
    monkeypatch: pytest.MonkeyPatch,
    during_import: Callable[[], None],
    load: Callable[..., None],
) -> Generator[None]:
    """Let Python mark a real reentrant import, then restore both module bindings."""
    import agent
    from agent import extensions

    prior_module = sys.modules["agent.extensions"]
    prior_parent_binding = vars(agent)["extensions"]
    prior_finders = sys.meta_path
    try:
        with monkeypatch.context() as patch:
            # Importlib replaces the parent's public binding when the import finishes.
            patch.setattr(agent, "extensions", extensions)
            patch.delitem(sys.modules, "agent.extensions")
            finder = _ExtensionsFinder(_ReentrantLoader(during_import, load))
            patch.setattr(sys, "meta_path", [finder, *prior_finders])
            importlib.import_module("agent.extensions")
            yield
    finally:
        assert sys.modules["agent.extensions"] is prior_module
        assert vars(agent)["extensions"] is prior_parent_binding
        assert sys.meta_path is prior_finders


def test_ensure_plugins_loaded_defers_while_the_loader_module_still_initializes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    loguru_records: list[dict],
) -> None:
    """A re-entrant import is not a failure (task #3234).

    A process that imports an `agent.*` module before `ava` reaches the eager
    load hits the loader while `agent.extensions` is still its own partial
    `sys.modules` entry: the attribute does not exist YET. That call must defer
    (no latch, no loud report) — and once the module is complete, the next call
    loads normally instead of being blocked by a latch for a load that never
    ran.
    """
    calls: list[int] = []

    def fake(*, catalog: ModelCatalog, authority: ConfigAuthority, surface: bool = False) -> None:
        calls.append(1)

    def during_import() -> None:
        ava.ensure_plugins_loaded()  # must not raise
        assert install.installed() is None and not install.load_attempted()
        assert calls == []
        assert "plugin load failed" not in capsys.readouterr().err
        assert not any("failed in this launched child" in r["message"] for r in loguru_records)

    with _partial_extensions(monkeypatch, during_import, fake):
        # The module completed; the next call retries a load that never ran.
        ava.ensure_plugins_loaded()
        assert calls == [1]
        assert install.load_attempted()


def test_lazy_miss_fails_fast_while_deferred_and_succeeds_after(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The deferred path must not re-enter `__getattr__` recursively.

    With the latch left off, a lazy miss whose load defers stops at the
    fail-fast AttributeError (no reload loop), and the first miss after the
    loader module is complete loads the plugin surface.
    """
    _as_launched_child(monkeypatch)

    calls: list[int] = []

    def fake(*, catalog: ModelCatalog, authority: ConfigAuthority, surface: bool = False) -> None:
        calls.append(1)
        _installing(
            (
                "deferred",
                PluginContributions(
                    sdk_namespaces=(
                        SdkNamespace(
                            "deferrednsp", SimpleNamespace(ping=lambda: "pong", __doc__="t")
                        ),
                    )
                ),
            ),
            catalog=catalog,
            authority=authority,
        )

    def during_import() -> None:
        with pytest.raises(AttributeError):
            _ = ava.deferrednsp  # type: ignore[attr-defined]
        assert calls == []
        assert install.installed() is None and not install.load_attempted()
        assert "plugin load failed" not in capsys.readouterr().err

    with _partial_extensions(monkeypatch, during_import, fake):
        assert ava.deferrednsp.ping() == "pong"  # type: ignore[attr-defined]
        assert calls == [1]
        assert install.installed() is not None


def _spy_member_loader(
    monkeypatch: pytest.MonkeyPatch, *, namespace: str, member: str
) -> list[int]:
    """Loader spy that registers a plugin MEMBER on an existing framework
    namespace (ava.self / ava.ui) — the shape ava_fleet uses for log/notify."""
    from agent import extensions

    calls: list[int] = []

    def fake(*, catalog: ModelCatalog, authority: ConfigAuthority, surface: bool = False) -> None:
        calls.append(1)
        _installing(
            (
                "member-plugin",
                PluginContributions(sdk_members=(SdkMember(namespace, member, lambda: "pong"),)),
            ),
            catalog=catalog,
            authority=authority,
        )

    monkeypatch.setattr(extensions, "load_extensions", fake)
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
    pin_no_identity()
    monkeypatch.delenv("AVA_AGENT_ID", raising=False)
    calls = _spy_member_loader(monkeypatch, namespace="self", member="lazylog")

    with pytest.raises(AttributeError):
        _ = ava.self.lazylog  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        _ = ava.ui.lazynotify  # type: ignore[attr-defined]
    assert calls == []
