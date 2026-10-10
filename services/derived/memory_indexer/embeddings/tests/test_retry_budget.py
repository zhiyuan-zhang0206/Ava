"""Provider retry budgets cover every attempt, gap, and deadline."""

from dataclasses import replace

import pytest

from base.config import settings
from base.host.net.resilience import MAX_RETRY_AFTER_RESPECT_S, ExponentialBackoff
from services.derived.memory_indexer.embeddings import factory, gemini


def test_batch_budget_covers_retry_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.services, "embedding_backend", "gemini")

    def assert_coverage() -> float:
        # Recompute each retry gap and the cancellation deadline per attempt.
        policy = gemini._EMBED_POLICY
        worst_batch = policy.max_attempts * settings.services.memory_embed_timeout_seconds
        worst_batch += sum(
            max(policy.backoff(attempt), MAX_RETRY_AFTER_RESPECT_S) + 2 * policy.jitter_span
            for attempt in range(policy.max_attempts - 1)
        )
        provider_budget = factory.worst_case_batch_seconds(
            settings.services.embedding_backend,
            timeout_seconds=settings.services.memory_embed_timeout_seconds,
        )
        assert provider_budget >= worst_batch
        return provider_budget

    original = assert_coverage()
    monkeypatch.setattr(
        gemini,
        "_EMBED_POLICY",
        replace(gemini._EMBED_POLICY, backoff=ExponentialBackoff(base=100, factor=2, cap=1000)),
    )
    assert_coverage()  # Later backoffs exceed Retry-After: wrong indices now fail.
    monkeypatch.setattr(
        settings.services,
        "memory_embed_timeout_seconds",
        settings.services.memory_embed_timeout_seconds + 300.0,
    )
    assert assert_coverage() > original
