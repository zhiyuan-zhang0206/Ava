"""`ava.sdk_surface.plugin_loader.scan_and_load` behavior guards: dir scan +
plugin.py import side effects, enabled-set filtering, relative-sibling
imports, and the fail-soft skip of a broken plugin.
"""

import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

from ava.sdk_surface.plugin_loader import scan_and_load


@pytest.fixture(autouse=True)
def _drop_fake_plugin_modules() -> Iterator[None]:
    """The `scan_and_load` tests import the fake plugin packages they write under
    tmp dirs (`plugins.codex_usage.plugin` / `.refresh`); a leftover in
    `sys.modules` poisons a later same-process file's negatives (a boot loader
    must never import a *disabled* plugin — `test_plugin_load_containment` read
    the leftovers as exactly that). Drop whatever a test here adds."""
    before = {name for name in sys.modules if name == "plugins" or name.startswith("plugins.")}
    yield
    for name in [
        n for n in sys.modules if (n == "plugins" or n.startswith("plugins.")) and n not in before
    ]:
        del sys.modules[name]


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


def test_scan_and_load_skips_broken_plugin_and_loads_the_rest(
    tmp_path: Path, loguru_records: list[dict]
):
    """Fail-soft contract (user ruling 2026-09-11, after the 2026-08-28 and
    2026-09-10 incidents): a plugin that raises at import is skipped with a
    loud report; the remaining plugins still load. This loader is the agent
    host's boot path, where a propagating error used to take the whole host
    down on every restart."""
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "plugin.py").write_text("raise RuntimeError('plugin bug')")
    good = tmp_path / "good"
    good.mkdir()
    (good / "plugin.py").write_text("LOADED = True")

    assert scan_and_load(tmp_path) == ["good"]
    # loud: an ERROR naming the plugin
    assert any("bad" in r["message"] and "failed to load" in r["message"] for r in loguru_records)
    # the half-executed module left nothing behind
    assert "plugins.bad.plugin" not in sys.modules


def test_scan_and_load_relative_sibling_import_resolves(tmp_path: Path):
    """The boot loader execs plugin.py under the same dotted package name the
    graph-build loader uses, so a package-relative sibling import works on
    both production paths — one loader contract, no dev/prod mismatch
    (issue #2161: the boot loader exec'd a top-level name and died on
    `from . import refresh`)."""
    plugin = tmp_path / "codex_usage"
    plugin.mkdir()
    (plugin / "plugin.py").write_text("from . import refresh\nMARK = refresh.MARK\n")
    (plugin / "refresh.py").write_text("MARK = 'x'\n")

    assert scan_and_load(tmp_path) == ["codex_usage"]
    module = sys.modules["plugins.codex_usage.plugin"]
    assert module.MARK == "x"
    assert sys.modules["plugins.codex_usage.refresh"].MARK == "x"


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


def test_scan_and_load_skips_dot_prefixed_dirs(tmp_path: Path):
    """Atomic-install residue (.name.staging / .name.backup-<pid>) must not be
    exec'd by the agent-host boot loader — the same ghost-plugin guard as
    _discover_plugins (QA nit, PR #880 review)."""
    _make_plugin(tmp_path, "real")
    ghost = tmp_path / ".real.backup-1234"
    ghost.mkdir()
    (ghost / "plugin.py").write_text('raise RuntimeError("ghost must not load")\n')
    assert scan_and_load(tmp_path) == ["real"]
