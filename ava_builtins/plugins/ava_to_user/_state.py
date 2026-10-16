from __future__ import annotations

from pydantic import BaseModel, Field

OVER_LIMIT = "over_limit"


# ava:ref ava_builtins/plugins/ava_to_user/docs/ava_to_user.ava.okf.md#soft-limit-reminder
class AvaToUserState(BaseModel):
    panel_written: bool = False
    reminded: set[str] = Field(default_factory=set)
    last_seen_compact: int = 0
