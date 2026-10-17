"""Contract: the compaction triggers point at the meiva.self.compact contract in meiva_builtins/commands/compact.md."""

import pytest

from agent.graph.hooks.compact import COMPACTION_INSTRUCTION
from base.packages.plugins.extensions import EMPTY


@pytest.fixture
def meiva_compact_loaded():
    """Compact is built-in (Issue #1284). The wrapper function lives in
    agent.graph.hooks.compact; state fields are on BaseAgentState. Returns
    (state_cls, wrap_fn) for tests to call.
    """
    from agent.graph.hooks.compact import compact_reminder
    from agent.state import build_agent_state

    return build_agent_state(EMPTY), compact_reminder


def test_compact_triggers_point_at_the_contract(meiva_compact_loaded):
    """Each compaction trigger (forced/auto instruction, the reminder nudge, the
    /compact command) names the `meiva.self.compact` contract. Forced compaction
    appends the SDK-owned text; other triggers can discover it on demand."""
    from agent.graph.hooks import compact as _p
    from base.paths import repo_root

    compact_md = (repo_root() / "meiva_builtins" / "commands" / "compact.md").read_text(
        encoding="utf-8"
    )
    assert "meiva.self.compact" in COMPACTION_INSTRUCTION
    assert "meiva.self.compact" in _p.COMPACT_REMINDER_NOTE
    assert "meiva.self.compact" in compact_md
