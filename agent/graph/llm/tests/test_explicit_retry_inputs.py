"""Retry policy reads belong to their configuration owner and remain live."""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import patch

import pytest

from agent.graph.llm import _retry
from agent.graph.llm_errors import LlmLedger
from base.config import ConfigBoot
from base.lm.catalog import ModelCatalog


def test_two_owner_retry_inputs_are_live_and_unknown_failures_do_not_read_them(
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
) -> None:
    def no_jitter(_low: float, _high: float) -> float:
        return 0.0

    monkeypatch.setattr(_retry.random, "uniform", no_jitter)
    with patch.dict(os.environ):
        first, second = ConfigBoot(), ConfigBoot()
        for owner, initial in ((first, 2.0), (second, 7.0)):
            owner.set_field("llm_retry_initial_interval_seconds", initial)
            owner.set_field("llm_retry_max_interval_seconds", 100.0)
            owner.set_field("llm_stall_retry_max_consecutive", 0)
        reads: list[tuple[str, str]] = []

        def wait(owner: ConfigBoot, name: str, exc: Exception) -> float | None:
            def read(field: str) -> Any:
                reads.append((name, field))
                return getattr(owner.view.lm, field)

            return _retry.retry_wait(
                exc,
                1,
                model="deepseek-flash",
                agent_id=0,
                ledger=LlmLedger(),
                catalog=model_catalog,
                max_attempts_pin=3,
                read_lm=read,
            )

        assert wait(first, "first", RuntimeError("unknown")) is None
        assert reads == []
        failure = _retry.LLMStreamStallTimeoutError("stall")
        assert wait(first, "first", failure) == 2.0
        assert wait(second, "second", failure) == 7.0
        first.set_field("llm_retry_initial_interval_seconds", 5.0)
        assert wait(first, "first", failure) == 5.0
        assert reads == [
            (owner, field)
            for owner in ("first", "second", "first")
            for field in (
                "llm_stall_retry_max_consecutive",
                "llm_retry_max_interval_seconds",
                "llm_retry_initial_interval_seconds",
            )
        ]
