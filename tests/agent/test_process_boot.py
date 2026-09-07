"""Eval-isolation boot enforcement runs after plugins have registered the SDK."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap


def _run(script: str) -> tuple[int, str, str]:
    with tempfile.TemporaryDirectory() as home:
        proc = subprocess.run(  # noqa: S603 -- fixed argv, sys.executable is trusted
            [sys.executable, "-c", textwrap.dedent(script)],
            capture_output=True,
            text=True,
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": home,
                "AVA_HOME": home,
                "AVA_CONFIG_FETCH": "skip",
                **_settings_env(),
            },
            check=False,
        )
    return proc.returncode, proc.stdout, proc.stderr


def _settings_env() -> dict[str, str]:
    return {key: os.environ[key] for key in ("AVA_DB_URL", "AVA_REDIS_URL") if key in os.environ}


def test_eval_isolation_disables_network_and_result_sdk_surfaces() -> None:
    code, out, err = _run("""
        import os
        from types import SimpleNamespace

        import ava
        from shared.config import settings
        from shared.plugin_context import PluginContext

        with PluginContext("ava_memory"):
            from ava_builtins.plugins.ava_memory import plugin
        ava.tasks = SimpleNamespace(list=lambda: [])
        ava.__all_for_ava__.append("tasks")

        original_path_doc = ava.memory.PATH.__doc__
        os.environ["AVA_AGENT_ID"] = "417"
        ava._boot.establish(417, owns_loop=True)
        settings.agent.eval_isolation = True
        settings.agent.eval_network_allowlist = []

        from agent._process_boot import _apply_per_agent_eval_isolation
        _apply_per_agent_eval_isolation()

        assert not hasattr(ava, "web")
        assert not hasattr(ava, "understand")
        assert not hasattr(ava, "mcps")
        assert not hasattr(ava, "ui")
        assert not hasattr(ava.agents, "get_last_message")
        assert not hasattr(ava, "tasks")
        assert ava.memory.PATH.name == "memory-pool"
        assert ava.memory.PATH.parent.name == "417"
        assert ava.memory.PATH.is_dir()
        assert ava.memory.PATH.__doc__ == original_path_doc
        assert ava.memory.search("shared result") == []
        assert ava.memory.search_detailed("shared result") == []
        print("ok")
    """)
    assert code == 0, err
    assert out.strip() == "ok"


def test_eval_network_allowlist_preserves_explicitly_allowed_web() -> None:
    code, out, err = _run("""
        import os
        import ava
        from shared.config import settings
        from shared.plugin_context import PluginContext

        with PluginContext("ava_memory"):
            from ava_builtins.plugins.ava_memory import plugin
        os.environ["AVA_AGENT_ID"] = "418"
        ava._boot.establish(418, owns_loop=True)
        settings.agent.eval_isolation = True
        settings.agent.eval_network_allowlist = ["web"]

        from agent._process_boot import _apply_per_agent_eval_isolation
        _apply_per_agent_eval_isolation()

        assert hasattr(ava, "web")
        assert not hasattr(ava, "understand")
        print("ok")
    """)
    assert code == 0, err
    assert out.strip() == "ok"


def test_eval_isolation_off_leaves_sdk_and_memory_unchanged() -> None:
    code, out, err = _run("""
        import ava
        from shared.config import settings
        from shared.plugin_context import PluginContext

        with PluginContext("ava_memory"):
            from ava_builtins.plugins.ava_memory import plugin
        original_path = ava.memory.PATH
        settings.agent.eval_isolation = False

        from agent._process_boot import _apply_per_agent_eval_isolation
        _apply_per_agent_eval_isolation()

        assert hasattr(ava, "web")
        assert hasattr(ava, "understand")
        assert hasattr(ava, "mcps")
        assert hasattr(ava, "ui")
        assert hasattr(ava.agents, "get_last_message")
        assert ava.memory.PATH == original_path
        print("ok")
    """)
    assert code == 0, err
    assert out.strip() == "ok"
