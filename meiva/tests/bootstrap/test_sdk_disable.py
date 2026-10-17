"""MEIVA_SDK_DISABLE removes pieces of the agent-facing SDK as it installs.

Each subprocess test runs a scenario of `child_sdk_disable.py`, which imports meiva and
installs the SDK surface under a controlled env, so the disable machinery (the first step
of the install) sees the right state. In-process re-import is fragile because the install mutates sys.modules +
globals and the slot holds one value; subprocess isolation is the only honest way
to verify "agent never sees X".
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_CHILD = Path(__file__).with_name("child_sdk_disable.py")


def _write_isolated_home(home: Path) -> None:
    """An `$MEIVA_HOME` whose plugins.json disables every repo builtin plugin.

    The disable machinery under test needs no plugin surface; this keeps the real
    load light (no DB-touching builtins) and hermetic. Mirrors
    `meiva/tests/namespaces/watcher/test_watcher_plugin_load.py`'s recipe."""
    import json

    from base import paths

    builtins = [
        p.name
        for p in paths.repo_plugins_dir().iterdir()
        if p.is_dir() and (p / "plugin.py").exists()
    ]
    config = {"plugins": {name: {"enabled": False} for name in builtins}}
    (home / "plugins.json").write_text(json.dumps(config))


def _run(scenario: str, *, env_disable: str | None = None) -> tuple[int, str, str]:
    env: dict[str, str] = {}
    if env_disable is not None:
        env["MEIVA_SDK_DISABLE"] = env_disable
    import tempfile

    with tempfile.TemporaryDirectory() as home:
        _write_isolated_home(Path(home))
        proc = subprocess.run(  # noqa: S603 — fixed argv, sys.executable is trusted
            [sys.executable, str(_CHILD), scenario],
            capture_output=True,
            text=True,
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": home,
                "MEIVA_HOME": home,
                **env,
                **_pass_through_env(),
            },
            check=False,
        )
    return proc.returncode, proc.stdout, proc.stderr


def _pass_through_env() -> dict[str, str]:
    """Settings requires DB / Redis URLs at import time — forward from the test env.

    MEIVA_CONFIG_FETCH=skip rides along too: without it the subprocess is a pure
    agent-runner (no serve flag) and its Settings import would fetch from a
    gateway (the suite pins the skip so no test process ever does).
    """
    import os

    forward = ("MEIVA_DB_URL", "MEIVA_REDIS_URL", "MEIVA_CONFIG_FETCH")
    return {k: os.environ[k] for k in forward if k in os.environ}


def test_default_no_disable_full_surface() -> None:
    code, out, err = _run("full_surface")
    assert code == 0, err
    names = out.strip().split(",")
    for required in ("agents", "watcher", "self", "files", "shell"):
        assert required in names, (required, names)


def test_disable_module_hides_from_parent_and_raises_legibly_on_use() -> None:
    code, out, err = _run("module_hides_from_parent", env_disable="watcher")
    assert code == 0, err
    assert out.strip() == "ok"


def test_disable_attribute_keeps_module_removes_function() -> None:
    code, out, err = _run("attribute_keeps_module", env_disable="self.terminate")
    assert code == 0, err
    assert out.strip() == "ok"


def test_disable_nested_module_swaps_sentinel_and_keeps_siblings() -> None:
    # A dotted entry that resolves to a nested submodule (shell.sessions) is
    # disabled *as a module*: gone from its parent, sys.modules entry swapped
    # for the sentinel, and attribute access raises the same legible
    # "disabled by MEIVA_SDK_DISABLE" error as a top-level module — not a raw
    # AttributeError. Sibling members (shell.run) survive.
    code, out, err = _run("nested_module_sentinel", env_disable="shell.sessions")
    assert code == 0, err
    assert out.strip() == "ok"


def test_multiple_disables_in_one_env() -> None:
    code, out, err = _run("multiple_disables", env_disable="watcher,agents,self.terminate")
    assert code == 0, err
    assert out.strip() == "ok"


def test_disabled_module_absent_from_help_overview() -> None:
    # help(meiva) is concatenated into the system prompt; a disabled module must
    # not leave a `from . import <name>` marker, or the agent would think the
    # feature is still there. Sibling modules still render.
    code, out, err = _run("help_overview", env_disable="agents")
    assert code == 0, err
    assert out.strip() == "ok"


def test_whitespace_in_disable_list_is_tolerated() -> None:
    code, out, err = _run("watcher_and_agents_gone", env_disable=" watcher , agents ")
    assert code == 0, err
    assert out.strip() == "ok"


# ── apply_sdk_disable re-entrant / cumulative tests ──────────────────────


def test_apply_sdk_disable_is_idempotent() -> None:
    """Calling apply_sdk_disable twice with the same entries is a no-op."""
    code, out, err = _run("apply_is_idempotent", env_disable="watcher")
    assert code == 0, err
    assert out.strip() == "ok"


def test_apply_sdk_disable_is_cumulative() -> None:
    """New entries not in the env list are applied on top."""
    code, out, err = _run("apply_is_cumulative", env_disable="watcher")
    assert code == 0, err
    assert out.strip() == "ok"


def test_apply_sdk_disable_dotted_cumulative() -> None:
    """Dotted entries can be added cumulatively on top of env entries."""
    code, out, err = _run("apply_dotted_cumulative", env_disable="self.terminate")
    assert code == 0, err
    assert out.strip() == "ok"


def test_apply_sdk_disable_applied_entries_tracked() -> None:
    """The installation records both env and manual entries."""
    code, out, err = _run("applied_entries_tracked", env_disable="watcher")
    assert code == 0, err
    assert out.strip() == "ok"


def test_help_still_renders_when_skills_is_disabled() -> None:
    """Regression: the help renderer marks a skills container's child walk as an
    index render (so it emits no "loaded" attribution), and reaches the skills
    module to do it. `MEIVA_SDK_DISABLE=skills` deletes that global — resolving it
    by name would NameError and take down `meiva.help()` for EVERY namespace on a
    cluster running without the skills surface."""
    code, out, err = _run("help_with_skills_disabled", env_disable="skills")
    assert code == 0, err
    assert out.strip().endswith("ok")


def test_env_disable_refuses_a_framework_module() -> None:
    """Disabling framework code (identity, the surface machinery) would break the
    framework, not scope the agent's view — the SDK install fails fast instead."""
    code, _out, err = _run("noop", env_disable="sdk_surface.agent_identity")
    assert code != 0
    assert "ValueError" in err, err
    assert (
        "entry 'sdk_surface.agent_identity' names the framework module meiva.sdk_surface" in err
    ), err


def test_runtime_disable_refuses_a_framework_module_and_its_members() -> None:
    code, out, err = _run("runtime_refuses_framework_module")
    assert code == 0, err
    assert out.splitlines()[:2] == [
        "refused sdk_surface True",
        "refused sdk_surface.agent_identity.agent_id True",
    ], out


def test_unknown_names_and_already_disabled_namespaces_still_apply() -> None:
    """A name that is no real `meiva` submodule (a plugin namespace registered
    later) stays disable-able, and a member of a namespace an earlier entry
    already disabled does not trip the guard."""
    code, out, err = _run("unknown_and_already_disabled_apply")
    assert code == 0, err
    assert "['agents', 'agents.spawn', 'not_a_real_namespace']" in out, out
