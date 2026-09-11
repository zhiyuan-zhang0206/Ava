"""Plugin loader containment + parity — issue #2161, task #2985.

The 2026-09-10 agent-host incident: a hand-placed external plugin whose
`plugin.py` used `from . import refresh` was exec'd by the host-boot loader
(`ava._extend.scan_and_load`) under a top-level module name, raised
`ImportError: attempted relative import with no known parent package`, and took
the whole agent host down on every restart — even with the plugin disabled via
`ava plugins disable` (the boot loader ignored the enable config).

These lock the contract that came out of it (user ruling 2026-09-11):

- disabled = never imported, on every production load path;
- a broken plugin is contained (skipped, loud report), the rest still load;
- both production loaders — host boot (`load_process_extensions`) and graph
  build (`_load_extensions`) — agree on the plugin's module identity
  (`plugins.<name>.plugin`) and on package-relative sibling imports;
- the incident's restart loop cannot reproduce.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from shared import paths
from shared.config import settings
from shared.plugins_config import write_local

# Every dotted name a plugin module can be registered under.
_PLUGIN_MODULE_PREFIXES = ("ava_builtins.plugins.", "plugins.")


@pytest.fixture(autouse=True)
def _isolate_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = tmp_path / "repo_plugins"
    user = tmp_path / "user_plugins"
    repo.mkdir()
    user.mkdir()
    monkeypatch.setattr(paths, "repo_plugins_dir", lambda: repo)
    monkeypatch.setattr(paths, "plugins_dir", lambda: user)
    monkeypatch.setattr(paths, "plugins_config_path", lambda: tmp_path / "plugins.json")
    monkeypatch.setattr(settings.general, "ava_home", str(tmp_path / "ava"))
    monkeypatch.setattr(paths, "ava_home", lambda: tmp_path)


@pytest.fixture(autouse=True)
def _restore_plugin_modules() -> Iterator[None]:
    """Per-test tmp plugin dirs claim process-global dotted names; restore the
    plugin namespace afterwards so a module bound to a discarded directory
    cannot leak into the next test (issue #147's pollution class)."""
    before = {k: v for k, v in sys.modules.items() if k.startswith(_PLUGIN_MODULE_PREFIXES)}
    yield
    for key in [
        k for k in sys.modules if k.startswith(_PLUGIN_MODULE_PREFIXES) and k not in before
    ]:
        del sys.modules[key]
    sys.modules.update(before)


def _write_plugin(root: Path, name: str, body: str, extra: dict[str, str] | None = None) -> None:
    plugin_dir = root / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.py").write_text(body)
    for filename, content in (extra or {}).items():
        (plugin_dir / filename).write_text(content)


def test_boot_loader_never_imports_a_disabled_plugin(loguru_records: list[dict]) -> None:
    """Issue #2161's first half: `ava plugins disable` did not stop the boot
    loader — it imported every directory on disk, so the disabled broken plugin
    still crashed startup. Disabled must mean never imported, on both paths."""
    _write_plugin(
        paths.plugins_dir(),
        "codex_usage",
        "from . import refresh\nMARK = refresh.MARK\n",
        {"refresh.py": "MARK = 'x'\n"},
    )
    _write_plugin(paths.plugins_dir(), "good", "LOADED = True\n")
    write_local({"plugins": {"codex_usage": {"enabled": False}, "good": {"enabled": True}}})

    from agent._process_boot import load_process_extensions

    load_process_extensions()  # must not raise: the disabled plugin is never imported

    assert "plugins.codex_usage.plugin" not in sys.modules
    assert "plugins.good.plugin" in sys.modules

    from agent.graph import _build

    _build._load_extensions()  # graph build agrees on the same set

    assert "plugins.codex_usage.plugin" not in sys.modules
    assert "plugins.good.plugin" in sys.modules


def test_boot_loader_contains_broken_plugins_and_keeps_going(
    loguru_records: list[dict],
) -> None:
    """The incident matrix at the boot loader: relative import with no parent
    package, syntax error, top-level raise, missing sibling — each is skipped
    loudly and the healthy plugin still loads."""
    home = paths.plugins_dir()
    _write_plugin(home, "broken_relative", "from . import nope\n")
    _write_plugin(home, "broken_syntax", "def oops(:\n")
    _write_plugin(home, "broken_raise", "raise RuntimeError('boom')\n")
    _write_plugin(home, "good", "LOADED = True\n")
    write_local(
        {
            "plugins": {
                name: {"enabled": True}
                for name in ("broken_relative", "broken_syntax", "broken_raise", "good")
            }
        }
    )

    from agent._process_boot import load_process_extensions

    load_process_extensions()  # must not raise

    assert "plugins.good.plugin" in sys.modules
    for broken in ("broken_relative", "broken_syntax", "broken_raise"):
        assert f"plugins.{broken}.plugin" not in sys.modules, broken
    failed = [r for r in loguru_records if "failed to load" in r["message"]]
    assert sorted(r["message"].split()[2] for r in failed) == [
        "broken_raise",
        "broken_relative",
        "broken_syntax",
    ]


def test_both_loaders_agree_on_module_identity_and_relative_imports() -> None:
    """Loader parity: the boot loader execs `plugin.py` under the same dotted
    name the graph loader uses, so a package-relative sibling import resolves
    on both paths and both see one module object (reload-in-place)."""
    _write_plugin(
        paths.plugins_dir(),
        "codex_usage",
        "from . import refresh\nMARK = refresh.MARK\n",
        {"refresh.py": "MARK = 'x'\n"},
    )
    write_local({"plugins": {"codex_usage": {"enabled": True}}})

    from agent._process_boot import load_process_extensions

    load_process_extensions()
    boot_module = sys.modules["plugins.codex_usage.plugin"]
    assert boot_module.MARK == "x"

    from agent.graph import _build

    _build._load_extensions()

    assert sys.modules["plugins.codex_usage.plugin"] is boot_module


def test_host_boot_restart_loop_survives_a_broken_plugin(loguru_records: list[dict]) -> None:
    """Issue #2161's second half: the host failed to start on EVERY restart
    while the broken package sat on disk. Consecutive boots must each survive,
    each report, and each leave no half-executed module behind."""
    _write_plugin(paths.plugins_dir(), "codex_usage", "from . import refresh\n")
    write_local({"plugins": {"codex_usage": {"enabled": True}}})

    from agent._process_boot import load_process_extensions

    for _ in range(3):
        load_process_extensions()  # each call stands in for one host boot

    assert "plugins.codex_usage.plugin" not in sys.modules
    reports = [
        r for r in loguru_records if "codex_usage" in r["message"] and "failed to load" in r["message"]
    ]
    assert len(reports) == 3
