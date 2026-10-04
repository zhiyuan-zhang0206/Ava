"""One prompt-injection scan finding — the value `ava.security` raises and the agent host delivers.

A leaf in `base/` so the SDK (`ava.security`, which writes findings into the exec turn's state
update) and the agent (`BaseAgentState.security_findings`, which carries them through the
checkpoint, and the claim node, which raises its own) share one class. It is a checkpoint channel
value, so it is named in `base/agents/history/checkpoint_serde.py`.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class SecurityFindingEntry(BaseModel):
    """An injection pattern matched in some ingested content (no file body)."""

    model_config = ConfigDict(frozen=True)

    type: Literal["security"] = "security"
    source: str
    triggers: list[str]
