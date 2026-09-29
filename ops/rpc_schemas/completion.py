"""Platform completion metadata carried over the agent message wire."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator


class CompletionNoticeIn(BaseModel):
    """Platform-only completion metadata carried with a shell or watcher chat."""

    model_config = ConfigDict(extra="forbid")

    outcome: Literal["exit", "missed"]
    exit_code: int | None = None

    @model_validator(mode="after")
    def check_outcome(self) -> Self:
        """Require exit codes only for terminal process exits."""
        if self.outcome == "exit" and self.exit_code is None:
            raise ValueError("exit completion notices require exit_code")
        if self.outcome == "missed" and self.exit_code is not None:
            raise ValueError("missed completion notices cannot carry exit_code")
        return self
