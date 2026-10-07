"""AVA_SDK_DISABLE removes pieces of the agent-facing SDK as it installs.

Each subprocess test imports ava and installs the SDK surface under a controlled
env, so the disable machinery (the first step of the install) sees the right
state. In-process re-import is fragile because the install mutates sys.modules +
globals and the slot holds one value; subprocess isolation is the only honest way
to verify "agent never sees X".
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path


def _install_preamble() -> str:
    """Import `ava` and install the SDK surface — the point the env `AVA_SDK_DISABLE`
    entries apply.

    The disable machinery runs as the first step of the SDK install
    (`ava.sdk_surface.install`), not at `import ava`: a real agent child reaches the
    load itself (a launched child at import, an exec child explicitly), so a bare
    script loads explicitly."""
    return "import ava\nava.ensure_plugins_loaded()\n"


def _write_isolated_home(home: Path) -> None:
    """An `$AVA_HOME` whose plugins.json disables every repo builtin plugin.

    The disable machinery under test needs no plugin surface; this keeps the real
    load light (no DB-touching builtins) and hermetic. Mirrors
    `ava/tests/test_watcher_plugin_load.py`'s recipe."""
    import json

    from base import paths

    builtins = [
        p.name
        for p in paths.repo_plugins_dir().iterdir()
        if p.is_dir() and (p / "plugin.py").exists()
    ]
    config = {"plugins": {name: {"enabled": False} for name in builtins}}
    (home / "plugins.json").write_text(json.dumps(config))


def _run(script: str, *, env_disable: str | None = None) -> tuple[int, str, str]:
    env: dict[str, str] = {}
    if env_disable is not None:
        env["AVA_SDK_DISABLE"] = env_disable
    import tempfile

    with tempfile.TemporaryDirectory() as home:
        _write_isolated_home(Path(home))
        proc = subprocess.run(  # noqa: S603 — fixed argv, sys.executable is trusted
            [sys.executable, "-c", _install_preamble() + textwrap.dedent(script)],
            capture_output=True,
            text=True,
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": home,
                "AVA_HOME": home,
                **env,
                **_pass_through_env(),
            },
            check=False,
        )
    return proc.returncode, proc.stdout, proc.stderr


def _pass_through_env() -> dict[str, str]:
    """Settings requires DB / Redis URLs at import time — forward from the test env.

    AVA_CONFIG_FETCH=skip rides along too: without it the subprocess is a pure
    agent-runner (no serve flag) and its Settings import would fetch from a
    gateway (the suite pins the skip so no test process ever does).
    """
    import os

    forward = ("AVA_DB_URL", "AVA_REDIS_URL", "AVA_CONFIG_FETCH")
    return {k: os.environ[k] for k in forward if k in os.environ}


def test_default_no_disable_full_surface() -> None:
    code, out, err = _run("""
        import ava
        names = sorted(ava.__all_for_ava__)
        print(','.join(names))
    """)
    assert code == 0, err
    names = out.strip().split(",")
    for required in ("agents", "watcher", "self", "files", "shell"):
        assert required in names, (required, names)


def test_disable_module_hides_from_parent_and_raises_legibly_on_use() -> None:
    code, out, err = _run(
        """
        import ava
        assert not hasattr(ava, 'watcher'), 'watcher should be gone from ava package'
        assert 'watcher' not in ava.__all_for_ava__
        # import succeeds — returns the disabled sentinel
        import ava.watcher as w
        try:
            w.anything
        except AttributeError as e:
            msg = str(e)
            assert 'disabled by AVA_SDK_DISABLE' in msg, msg
            assert "'watcher'" in msg, msg
            print('ok')
        else:
            raise AssertionError('attribute access on disabled module did not raise')
        """,
        env_disable="watcher",
    )
    assert code == 0, err
    assert out.strip() == "ok"


def test_disable_attribute_keeps_module_removes_function() -> None:
    code, out, err = _run(
        """
        import ava
        assert hasattr(ava, 'self'), 'ava.self module should remain'
        assert hasattr(ava.self, 'restart'), 'restart should remain'
        assert not hasattr(ava.self, 'terminate'), 'terminate should be gone'
        try:
            ava.self.terminate
        except AttributeError:
            print('ok')
        """,
        env_disable="self.terminate",
    )
    assert code == 0, err
    assert out.strip() == "ok"


def test_disable_nested_module_swaps_sentinel_and_keeps_siblings() -> None:
    # A dotted entry that resolves to a nested submodule (shell.sessions) is
    # disabled *as a module*: gone from its parent, sys.modules entry swapped
    # for the sentinel, and attribute access raises the same legible
    # "disabled by AVA_SDK_DISABLE" error as a top-level module — not a raw
    # AttributeError. Sibling members (shell.run) survive.
    code, out, err = _run(
        """
        import ava
        assert hasattr(ava, 'shell'), 'ava.shell should remain'
        assert hasattr(ava.shell, 'run'), 'ava.shell.run should remain'
        assert not hasattr(ava.shell, 'sessions'), 'ava.shell.sessions should be gone'
        import ava.shell.sessions as sess
        try:
            sess.new
        except AttributeError as e:
            msg = str(e)
            assert 'disabled by AVA_SDK_DISABLE' in msg, msg
            assert 'shell.sessions' in msg, msg
            print('ok')
        else:
            raise AssertionError('attribute access on disabled nested module did not raise')
        """,
        env_disable="shell.sessions",
    )
    assert code == 0, err
    assert out.strip() == "ok"


def test_multiple_disables_in_one_env() -> None:
    code, out, err = _run(
        """
        import ava
        assert not hasattr(ava, 'watcher')
        assert not hasattr(ava, 'agents')
        assert not hasattr(ava.self, 'terminate')
        assert hasattr(ava, 'shell'), 'shell not in disable list, should remain'
        print('ok')
        """,
        env_disable="watcher,agents,self.terminate",
    )
    assert code == 0, err
    assert out.strip() == "ok"


def test_disabled_module_absent_from_help_overview() -> None:
    # help(ava) is concatenated into the system prompt; a disabled module must
    # not leave a `from . import <name>` marker, or the agent would think the
    # feature is still there. Sibling modules still render.
    code, out, err = _run(
        """
        import io, contextlib, ava
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ava.help()
        output = buf.getvalue()
        assert 'from . import agents' not in output, output
        assert 'from . import files' in output, output
        print('ok')
        """,
        env_disable="agents",
    )
    assert code == 0, err
    assert out.strip() == "ok"


def test_whitespace_in_disable_list_is_tolerated() -> None:
    code, out, err = _run(
        """
        import ava
        assert not hasattr(ava, 'watcher')
        assert not hasattr(ava, 'agents')
        print('ok')
        """,
        env_disable=" watcher , agents ",
    )
    assert code == 0, err
    assert out.strip() == "ok"


# ── apply_sdk_disable re-entrant / cumulative tests ──────────────────────


def test_apply_sdk_disable_is_idempotent() -> None:
    """Calling apply_sdk_disable twice with the same entries is a no-op."""
    code, out, err = _run(
        """
        import ava
        from ava.sdk_surface.sdk_disable import apply_sdk_disable
        # The install applied the env entries (the preamble loaded the surface)
        assert not hasattr(ava, 'watcher')
        # Second call with same entry should be a no-op — no crash, no duplicate
        apply_sdk_disable(['watcher'])
        assert not hasattr(ava, 'watcher')
        print('ok')
        """,
        env_disable="watcher",
    )
    assert code == 0, err
    assert out.strip() == "ok"


def test_apply_sdk_disable_is_cumulative() -> None:
    """New entries not in the env list are applied on top."""
    code, out, err = _run(
        """
        import ava
        from ava.sdk_surface.sdk_disable import apply_sdk_disable
        # The env baseline disabled watcher at install time
        assert not hasattr(ava, 'watcher')
        assert hasattr(ava, 'agents'), 'agents still present before second call'
        # Apply additional disable on top
        apply_sdk_disable(['agents'])
        assert not hasattr(ava, 'agents'), 'agents should be gone after second call'
        assert not hasattr(ava, 'watcher'), 'watcher should remain gone'
        assert hasattr(ava, 'files'), 'files should remain'
        print('ok')
        """,
        env_disable="watcher",
    )
    assert code == 0, err
    assert out.strip() == "ok"


def test_apply_sdk_disable_dotted_cumulative() -> None:
    """Dotted entries can be added cumulatively on top of env entries."""
    code, out, err = _run(
        """
        import ava
        from ava.sdk_surface.sdk_disable import apply_sdk_disable
        # The env baseline disabled self.terminate at install time
        assert not hasattr(ava.self, 'terminate')
        assert hasattr(ava.self, 'restart'), 'restart should remain'
        # Apply additional disable
        apply_sdk_disable(['self.restart'])
        assert not hasattr(ava.self, 'terminate'), 'terminate still gone'
        assert not hasattr(ava.self, 'restart'), 'restart now gone too'
        print('ok')
        """,
        env_disable="self.terminate",
    )
    assert code == 0, err
    assert out.strip() == "ok"


def test_apply_sdk_disable_applied_entries_tracked() -> None:
    """The installation records both env and manual entries."""
    code, out, err = _run(
        """
        import ava
        from ava.sdk_surface import install as sdk_install
        from ava.sdk_surface.sdk_disable import apply_sdk_disable
        # After the install: env entries are recorded on the installation
        assert 'watcher' in sdk_install.installed().disabled
        # Add a new one
        apply_sdk_disable(['agents'])
        assert 'agents' in sdk_install.installed().disabled
        assert 'watcher' in sdk_install.installed().disabled
        print('ok')
        """,
        env_disable="watcher",
    )
    assert code == 0, err
    assert out.strip() == "ok"


def test_help_still_renders_when_skills_is_disabled() -> None:
    """Regression: the help renderer marks a skills container's child walk as an
    index render (so it emits no "loaded" attribution), and reaches the skills
    module to do it. `AVA_SDK_DISABLE=skills` deletes that global — resolving it
    by name would NameError and take down `ava.help()` for EVERY namespace on a
    cluster running without the skills surface."""
    code, out, err = _run(
        """
        import ava
        assert not hasattr(ava, 'skills'), 'skills should be gone from ava package'
        ava.help(ava.files)
        ava.help(ava)
        print('ok')
        """,
        env_disable="skills",
    )
    assert code == 0, err
    assert out.strip().endswith("ok")


def test_env_disable_refuses_a_framework_module() -> None:
    """Disabling framework code (identity, the surface machinery) would break the
    framework, not scope the agent's view — the SDK install fails fast instead."""
    code, _out, err = _run("", env_disable="agent_identity")
    assert code != 0
    assert "ValueError" in err and "framework module ava.sdk_surface.agent_identity" in err, err


def test_runtime_disable_refuses_a_framework_module_and_its_members() -> None:
    code, out, err = _run("""
        from ava.sdk_surface.sdk_disable import apply_sdk_disable
        for entry in ("sdk_surface", "agent_identity.agent_id"):
            try:
                apply_sdk_disable([entry])
            except ValueError as exc:
                print("refused", entry, "framework module" in str(exc))
    """)
    assert code == 0, err
    assert out.splitlines()[:2] == [
        "refused sdk_surface True",
        "refused agent_identity.agent_id True",
    ], out


def test_unknown_names_and_already_disabled_namespaces_still_apply() -> None:
    """A name that is no real `ava` submodule (a plugin namespace registered
    later) stays disable-able, and a member of a namespace an earlier entry
    already disabled does not trip the guard."""
    code, out, err = _run("""
        from ava.sdk_surface import install as sdk_install
        from ava.sdk_surface.sdk_disable import apply_sdk_disable
        apply_sdk_disable(["not_a_real_namespace"])
        apply_sdk_disable(["agents"])
        apply_sdk_disable(["agents.spawn"])
        print(sorted(sdk_install.installed().disabled))
    """)
    assert code == 0, err
    assert "['agents', 'agents.spawn', 'not_a_real_namespace']" in out, out
