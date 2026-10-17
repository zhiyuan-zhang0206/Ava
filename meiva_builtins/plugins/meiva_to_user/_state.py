from __future__ import annotations

from pydantic import BaseModel, Field

OVER_LIMIT = "over_limit"


# meiva:link meiva_builtins/plugins/meiva_to_user/docs/meiva_to_user.meiva.okf.md
class MeivaToUserState(BaseModel):
    panel_written: bool = False
    reminded: set[str] = Field(default_factory=set)
    last_seen_compact: int = 0
