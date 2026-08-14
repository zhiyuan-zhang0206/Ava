"""AVA_SDK_DISABLE removes pieces of the agent-facing SDK at ava import time.

Each subprocess test re-imports ava under a controlled env so the disable
machinery (which runs at module init) sees the right state. In-process
re-import is fragile because the ava package mutates sys.modules + globals;
subprocess isolation is the only honest way to verify "agent never sees X".
"""

from __future__ import annotations

import subprocess
import sys
import textwrap


def _run(script: str, *, env_disable: str | None = None) -> tuple[int, str, str]:
    env: dict[str, str] = {}
    if env_disable is not None:
        env["AVA_SDK_DISABLE"] = env_disable
    import tempfile

    with tempfile.TemporaryDirectory() as home:
        proc = subprocess.run(  # noqa: S603 — fixed argv, sys.executable is trusted
            [sys.executable, "-c", textwrap.dedent(script)],
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin", "HOME": home, **env, **_pass_through_env()},
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


# ── _apply_sdk_disable re-entrant / cumulative tests ──────────────────────


def test_apply_sdk_disable_is_idempotent() -> None:
    """Calling _apply_sdk_disable twice with the same entries is a no-op."""
    code, out, err = _run(
        """
        import ava
        # First call at import time (via env) disabled watcher
        assert not hasattr(ava, 'watcher')
        # Second call with same entry should be a no-op — no crash, no duplicate
        ava._apply_sdk_disable(['watcher'])
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
        # Env disabled watcher at import time
        assert not hasattr(ava, 'watcher')
        assert hasattr(ava, 'agents'), 'agents still present before second call'
        # Apply additional disable on top
        ava._apply_sdk_disable(['agents'])
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
        # Env disabled self.terminate at import time
        assert not hasattr(ava.self, 'terminate')
        assert hasattr(ava.self, 'restart'), 'restart should remain'
        # Apply additional disable
        ava._apply_sdk_disable(['self.restart'])
        assert not hasattr(ava.self, 'terminate'), 'terminate still gone'
        assert not hasattr(ava.self, 'restart'), 'restart now gone too'
        print('ok')
        """,
        env_disable="self.terminate",
    )
    assert code == 0, err
    assert out.strip() == "ok"


def test_apply_sdk_disable_applied_entries_tracked() -> None:
    """_applied_disable_entries reflects both env and manual calls."""
    code, out, err = _run(
        """
        import ava
        # After import: env entries are tracked
        assert 'watcher' in ava._applied_disable_entries
        # Add a new one
        ava._apply_sdk_disable(['agents'])
        assert 'agents' in ava._applied_disable_entries
        assert 'watcher' in ava._applied_disable_entries
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
