"""Contract: the compaction triggers point at the ava.self.compact contract in ava_builtins/commands/compact.md."""

import pytest

from agent.hooks.compact import COMPACTION_INSTRUCTION
from base.packages.plugins.extensions import EMPTY


@pytest.fixture
def _ava_compact_loaded():
    """Compact is built-in (Issue #1284). The wrapper function lives in
    agent.hooks.compact; state fields are on BaseAgentState. Returns
    (state_cls, wrap_fn) for tests to call.
    """
    from agent.hooks.compact import _compact_reminder
    from agent.state import build_agent_state

    return build_agent_state(EMPTY), _compact_reminder


def test_compact_triggers_point_at_the_contract(_ava_compact_loaded):
    """Each compaction trigger (forced/auto instruction, the reminder nudge, the
    /compact command) names the `ava.self.compact` contract. Forced compaction
    appends the SDK-owned text; other triggers can discover it on demand."""
    from agent.hooks import compact as _p
    from base.paths import repo_root

    compact_md = (repo_root() / "ava_builtins" / "commands" / "compact.md").read_text(
        encoding="utf-8"
    )
    assert "ava.self.compact" in COMPACTION_INSTRUCTION
    assert "ava.self.compact" in _p.COMPACT_REMINDER_NOTE
    assert "ava.self.compact" in compact_md
