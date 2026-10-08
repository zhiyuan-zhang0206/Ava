"""One real write/flush/ACK fault per original continuation; never fabricate a result."""

from typing import Any

import psycopg
import pytest

from services.agent_runner.agent_host.invocation.compact import apply as compact_apply
from services.agent_runner.agent_host.invocation.compact import execute as compact_execute


def install(monkeypatch: pytest.MonkeyPatch, stage: str) -> list[str]:
    injected: list[str] = []
    if stage in ("before_result", "after_result"):
        original = compact_execute.save_result

        async def save(*args: Any, **kwargs: Any) -> Any:
            if stage == "before_result" and not injected:
                injected.append(stage)
                raise psycopg.OperationalError("original result write response lost")
            result = await original(*args, **kwargs)
            if not injected:
                injected.append(stage)
                raise psycopg.OperationalError("original result commit response lost")
            return result

        monkeypatch.setattr(compact_execute, "save_result", save)
    elif stage == "after_apply_checkpoint":
        original_flush = compact_apply.flush_checkpoint

        async def flush(*args: Any, **kwargs: Any) -> Any:
            result = await original_flush(*args, **kwargs)
            if not injected:
                injected.append(stage)
                raise psycopg.OperationalError("original flush response lost")
            return result

        monkeypatch.setattr(compact_apply, "flush_checkpoint", flush)
    elif stage == "after_ack":
        original_ack = compact_apply.acknowledge

        async def ack(*args: Any, **kwargs: Any) -> bool:
            result = await original_ack(*args, **kwargs)
            if result and not injected:
                injected.append(stage)
                raise psycopg.OperationalError("original application ACK response lost")
            return result

        monkeypatch.setattr(compact_apply, "acknowledge", ack)
    elif stage != "none":
        raise ValueError("unknown compact fault stage")
    return injected
