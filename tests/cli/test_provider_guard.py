"""Provider account guard — health-probe checks 9-10 (`cli/commands/_provider_guard.py`).

Two invariants shape these tests:

- Failure messages are STABLE across runs — the probe's episode key is the
  exact message text, so a live reading in it (a ticking balance, a growing
  halted count) would reset the episode every tick and never grade. Live
  readings ride stderr detail lines only.
- Read trouble is fail-OPEN: a missing key, an unreadable balance, or an
  unreachable agent table means "cannot judge", not "unhealthy".

The probe-level wiring (alert edge fired, recovery resolved from the on-disk
episode store, no rollback counting) is pinned in `tests/cli/test_cluster_health.py`
next to the other checks' integration tests.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr

from cli.commands import _provider_guard
from shared.config import settings


def _secret(value: str) -> SecretStr:
    return SecretStr(value)


def _balance_payload(
    total: str, *, currency: str = "CNY", available: bool = True
) -> dict[str, Any]:
    """A DeepSeek /user/balance-shaped payload."""
    return {
        "is_available": available,
        "balance_infos": [{"currency": currency, "total_balance": total}],
    }


# ─── check 9: balance runway ─────────────────────────────────────────────────


def test_balance_at_the_minimum_passes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The minimum is inclusive: at the threshold the account is still funded."""
    monkeypatch.setattr(settings.lm, "deepseek_api_key", _secret("sk-test"))
    monkeypatch.setattr(settings.alerts, "provider_guard_balance_min_cny", 500.0)

    def _fetch(*_args: object, **_kwargs: object) -> dict[str, Any]:
        return _balance_payload("500.00")

    monkeypatch.setattr(_provider_guard, "_fetch_balance", _fetch)

    assert _provider_guard._balance_failure() is None
    assert capsys.readouterr().err == ""


def test_balance_below_minimum_reports_a_stable_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The message names the configured threshold, never the live balance."""
    monkeypatch.setattr(settings.lm, "deepseek_api_key", _secret("sk-test"))
    monkeypatch.setattr(settings.alerts, "provider_guard_balance_min_cny", 500.0)
    totals = iter(["100.00", "99.50"])

    def _fetch(*_args: object, **_kwargs: object) -> dict[str, Any]:
        return _balance_payload(next(totals))

    monkeypatch.setattr(_provider_guard, "_fetch_balance", _fetch)

    first = _provider_guard._balance_failure()
    second = _provider_guard._balance_failure()

    assert first is not None
    assert first == second
    assert "AVA_PROVIDER_GUARD_BALANCE_MIN_CNY=\u00a5500" in first
    assert "100.00" not in first and "99.50" not in first
    err = capsys.readouterr().err
    assert "100.00" in err and "99.50" in err  # live readings stay on stderr


def test_balance_unavailable_account_fails_even_when_funded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings.lm, "deepseek_api_key", _secret("sk-test"))

    def _fetch(*_args: object, **_kwargs: object) -> dict[str, Any]:
        return _balance_payload("9999.00", available=False)

    monkeypatch.setattr(_provider_guard, "_fetch_balance", _fetch)

    failure = _provider_guard._balance_failure()

    assert failure is not None
    assert "is_available=false" in failure


def test_balance_without_a_key_skips_without_fetching(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Hosts without the key configured (e.g. runners) pass with a note."""
    monkeypatch.setattr(settings.lm, "deepseek_api_key", None)
    fetches: list[bool] = []

    def _fetch(*_args: object, **_kwargs: object) -> dict[str, Any]:
        fetches.append(True)
        return _balance_payload("0.00")

    monkeypatch.setattr(_provider_guard, "_fetch_balance", _fetch)

    assert _provider_guard._balance_failure() is None
    assert fetches == []
    assert "DEEPSEEK_API_KEY not configured" in capsys.readouterr().err


def test_balance_read_error_is_fail_open(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A provider API blip is not evidence of a drained account."""
    monkeypatch.setattr(settings.lm, "deepseek_api_key", _secret("sk-test"))

    def _fetch(*_args: object, **_kwargs: object) -> dict[str, Any]:
        raise _provider_guard.BalanceReadError("HTTP 502")

    monkeypatch.setattr(_provider_guard, "_fetch_balance", _fetch)

    assert _provider_guard._balance_failure() is None
    assert "HTTP 502" in capsys.readouterr().err


def test_balance_without_a_cny_entry_skips(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only the account's funded currency can be judged against a CNY minimum."""
    monkeypatch.setattr(settings.lm, "deepseek_api_key", _secret("sk-test"))

    def _fetch(*_args: object, **_kwargs: object) -> dict[str, Any]:
        return _balance_payload("100.00", currency="USD")

    monkeypatch.setattr(_provider_guard, "_fetch_balance", _fetch)

    assert _provider_guard._balance_failure() is None
    assert "no CNY balance entry" in capsys.readouterr().err


def test_balance_disabled_skips_before_the_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.alerts, "provider_guard_balance_enabled", False)

    def _fetch(*_args: object, **_kwargs: object) -> dict[str, Any]:
        raise AssertionError("a disabled check must not read the provider")

    monkeypatch.setattr(_provider_guard, "_fetch_balance", _fetch)

    assert _provider_guard._balance_failure() is None


def test_fetch_balance_maps_http_failure_to_a_sanitized_read_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    def _get(*_args: object, **_kwargs: object) -> httpx.Response:
        return httpx.Response(
            500,
            text="<html>upstream error</html>",
            request=httpx.Request("GET", "https://balance.test"),
        )

    monkeypatch.setattr(httpx, "get", _get)

    with pytest.raises(_provider_guard.BalanceReadError, match="HTTP 500"):
        _provider_guard._fetch_balance("https://balance.test", "sk-test", 5.0)


# ─── check 10: halted agents ─────────────────────────────────────────────────


def test_blocked_agents_at_threshold_reports_a_stable_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(settings.alerts, "provider_guard_blocked_agents_min", 3)
    counts = iter([3, 7])

    def _count(_window_hours: float) -> int | None:
        return next(counts)

    monkeypatch.setattr(_provider_guard, "_halted_agents_count", _count)

    first = _provider_guard._blocked_agents_failure()
    second = _provider_guard._blocked_agents_failure()

    assert first is not None
    assert first == second
    assert "AVA_PROVIDER_GUARD_BLOCKED_AGENTS_MIN=3" in first
    err = capsys.readouterr().err
    assert "3 agent(s)" in err and "7 agent(s)" in err


def test_blocked_agents_below_threshold_is_silent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(settings.alerts, "provider_guard_blocked_agents_min", 3)

    def _count(_window_hours: float) -> int | None:
        return 2

    monkeypatch.setattr(_provider_guard, "_halted_agents_count", _count)

    assert _provider_guard._blocked_agents_failure() is None
    assert capsys.readouterr().err == ""


def test_blocked_agents_unreadable_table_is_fail_open(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _count(_window_hours: float) -> int | None:
        return None

    monkeypatch.setattr(_provider_guard, "_halted_agents_count", _count)

    assert _provider_guard._blocked_agents_failure() is None
    assert "agent table unreachable" in capsys.readouterr().err


def test_blocked_agents_disabled_skips_before_the_query(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.alerts, "provider_guard_blocked_agents_enabled", False)

    def _count(_window_hours: float) -> int | None:
        raise AssertionError("a disabled check must not query")

    monkeypatch.setattr(_provider_guard, "_halted_agents_count", _count)

    assert _provider_guard._blocked_agents_failure() is None


def test_halted_agents_count_reads_the_recovery_breaker_halt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`permanent_reject_streak` is the durable halt flag (task #3617) and the
    window bounds the count to the active wave."""
    import shared.db
    from shared.recovery_breaker import HALT_AFTER_CONSECUTIVE_PERMANENT_REJECTS

    queries: list[tuple[str, tuple[object, ...]]] = []

    class _Cursor:
        def __enter__(self) -> _Cursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, query: str, params: tuple[object, ...] = ()) -> None:
            queries.append((query, params))

        def fetchone(self) -> tuple[int]:
            return (4,)

    class _Connection:
        def __enter__(self) -> _Connection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def cursor(self) -> _Cursor:
            return _Cursor()

    monkeypatch.setattr(shared.db, "connect", _Connection)

    assert _provider_guard._halted_agents_count(24) == 4
    query, params = queries[0]
    assert "permanent_reject_streak >= %s" in query
    assert "last_turn_fatal_at > now() - make_interval" in query
    assert params == (HALT_AFTER_CONSECUTIVE_PERMANENT_REJECTS, 24 * 3600.0)


def test_halted_agents_count_db_error_is_fail_open(monkeypatch: pytest.MonkeyPatch) -> None:
    import shared.db

    def _down(*_args: object, **_kwargs: object) -> object:
        raise ConnectionError("db down")

    monkeypatch.setattr(shared.db, "connect", _down)

    assert _provider_guard._halted_agents_count(24) is None


# ─── the combined entry point ────────────────────────────────────────────────


def test_provider_guard_failure_reports_balance_before_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One failure at a time, root cause first: a drained balance IS the cause
    of the halted agents, and funding the account is what unsticks them."""

    def _balance() -> str:
        return "balance detail"

    def _blocked() -> str:
        return "blocked detail"

    monkeypatch.setattr(_provider_guard, "_balance_failure", _balance)
    monkeypatch.setattr(_provider_guard, "_blocked_agents_failure", _blocked)

    assert (
        _provider_guard.provider_guard_failure() == "FAIL: provider balance \u2014 balance detail"
    )

    def _fine() -> None:
        return None

    monkeypatch.setattr(_provider_guard, "_balance_failure", _fine)
    assert (
        _provider_guard.provider_guard_failure()
        == "FAIL: provider blocked agents \u2014 blocked detail"
    )
