"""Contract: the compaction triggers point at the ava.self.compact contract in commands/compact.md."""

import pytest

from agent.hooks.compact import COMPACTION_INSTRUCTION


@pytest.fixture
def _ava_compact_loaded():
    """Compact is now built-in (Issue #1284). The wrapper function lives in
    agent.hooks.compact; state fields are on BaseAgentState. Returns
    (state_cls, wrap_fn) for tests to call.

    Teardown clears hook registrations to prevent leakage into other tests.
    """
    from agent.hooks.compact import _compact_reminder
    from agent.state import build_agent_state, clear_plugin_registrations

    clear_plugin_registrations()

    yield build_agent_state(), _compact_reminder

    clear_plugin_registrations()


def test_compact_triggers_point_at_the_contract(_ava_compact_loaded):
    """Each compaction trigger (forced/auto instruction, the reminder nudge, the
    /compact command) is a short opener that defers to the `ava.self.compact`
    contract rather than carrying its own copy of the template."""
    from agent.hooks import compact as _p
    from base.paths import repo_root

    compact_md = (repo_root() / "commands" / "compact.md").read_text(encoding="utf-8")
    assert "ava.self.compact" in COMPACTION_INSTRUCTION
    assert "ava.self.compact" in _p.COMPACT_REMINDER_NOTE
    assert "ava.self.compact" in compact_md
