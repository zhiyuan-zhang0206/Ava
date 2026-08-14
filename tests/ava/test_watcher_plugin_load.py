"""End-to-end (real child process): a watcher child can use plugin namespaces.

The bug this pins: a fresh process's `import ava` is the factory module — none
of the agent process's plugin setattrs are present — so `ava.tasks` (and every
other plugin-registered namespace) used to AttributeError inside a watcher. The
generated bootstrap now loads plugins before running the script, so the child
resolves them.

Runs the actual `_build_boot` output through `python <boot>` (no session needed),
against an isolated $AVA_HOME: every repo builtin is disabled and one minimal
external plugin registers `ava.probe`, so the real `_load_extensions` runs
without dragging in the DB-touching builtins. This exercises the whole path —
`_ensure_plugins_loaded` -> importlib -> `_load_extensions` -> external plugin
import -> `register_namespace` — in a genuinely separate interpreter.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from ava import watcher
from shared import paths


def _write_isolated_home(home: Path) -> None:
    """An $AVA_HOME whose plugins.json disables every repo builtin and enables one
    minimal external plugin that registers `ava.probe`."""
    plug = home / "plugins" / "probe_plugin"
    plug.mkdir(parents=True)
    (plug / "plugin.py").write_text(
        "import ava\n"
        "from types import SimpleNamespace\n"
        "ava.register_namespace('probe', SimpleNamespace(ok=lambda: 'yes', __doc__='probe plugin'))\n"
    )
    builtins = [
        p.name
        for p in paths.repo_plugins_dir().iterdir()
        if p.is_dir() and (p / "plugin.py").exists()
    ]
    config = {"plugins": {name: {"enabled": False} for name in builtins}}
    config["plugins"]["probe_plugin"] = {"enabled": True}
    (home / "plugins.json").write_text(json.dumps(config))


def test_watcher_child_resolves_plugin_namespace(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _write_isolated_home(home)

    marker = home / "marker.txt"
    script_path = home / "wscript.py"
    # The watcher script uses a plugin namespace (`ava.probe`) — the thing that
    # AttributeErrored before the fix — and records what it got.
    script_path.write_text(
        f"import ava, pathlib\npathlib.Path({str(marker)!r}).write_text(ava.probe.ok())\n"
    )
    boot_path = home / "wboot.py"
    boot_path.write_text(watcher._build_boot(script_path, None, 900000))

    env = {
        **os.environ,
        "AVA_HOME": str(home),
        "AVA_AGENT_ID": "123",
        "AVA_CONFIG_FETCH": "skip",
    }
    result = subprocess.run(  # noqa: S603 — sys.executable on a generated bootstrap file
        [sys.executable, str(boot_path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )

    assert marker.exists(), (
        f"watcher child did not resolve ava.probe (rc={result.returncode})\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert marker.read_text() == "yes"


def test_bare_shell_child_eager_loads_plugins(tmp_path: Path) -> None:
    # A persistent-shell child is a bare `python x.py` with AVA_AGENT_ID in env
    # and NO bootstrap. Lazy-on-miss cannot cover plugin wrappers on existing
    # members (no attribute miss ever fires), so `import ava` itself must load
    # plugins in this context.
    home = tmp_path / "home"
    home.mkdir()
    _write_isolated_home(home)

    marker = home / "marker.txt"
    script = home / "bare.py"
    script.write_text(
        "import ava, pathlib\n"
        f"pathlib.Path({str(marker)!r}).write_text("
        "f'{ava._plugins_loaded}:{ava.probe.ok()}')\n"
    )
    env = {
        **os.environ,
        "AVA_HOME": str(home),
        "AVA_AGENT_ID": "123",
        "AVA_CONFIG_FETCH": "skip",
    }
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert marker.exists(), (
        f"bare child did not eager-load plugins (rc={result.returncode})\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert marker.read_text() == "True:yes"


def test_bare_process_without_agent_id_stays_fail_fast(tmp_path: Path) -> None:
    # gateway / cli semantics: no AVA_AGENT_ID -> import ava does NOT load
    # plugins and unknown attributes keep the fail-fast AttributeError.
    home = tmp_path / "home"
    home.mkdir()
    _write_isolated_home(home)

    script = home / "bare.py"
    script.write_text(
        "import ava\n"
        "assert ava._plugins_loaded is False\n"
        "try:\n"
        "    ava.probe\n"
        "except AttributeError:\n"
        "    print('FAILFAST-OK')\n"
        "else:\n"
        "    raise SystemExit('lazy load fired without AVA_AGENT_ID')\n"
    )
    env = {**os.environ, "AVA_HOME": str(home), "AVA_CONFIG_FETCH": "skip"}
    env.pop("AVA_AGENT_ID", None)
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "FAILFAST-OK" in result.stdout
