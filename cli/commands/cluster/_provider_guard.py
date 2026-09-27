"""Provider-account guard — health-probe checks 9-10 (`ava cluster health-probe`).

Two alert-only checks on the LLM provider account the fleet bills to, added
after the 2026-09-18 outage: the DeepSeek balance hit zero around 05:02, every
deepseek-model call answered HTTP 402, 23 agents were halted over 5h43m, and
the first signal anyone acted on was the owner waking up.

9. **Balance runway** — reads the account balance and fails while it sits
   below a configurable minimum, so a top-up lands *before* the account runs
   dry. The Grafana billing rule (`ava-ops-llm-billing-quota`) reports the
   first post-arrears rejection — already too late; this check is the
   pre-arrears signal.
10. **Blocked agents** — fails while at least N agents are halted by
    permanent provider rejections (`agents_meta.permanent_reject_streak >= 2`,
    the durable recovery-breaker halt in `shared/recovery_breaker.py`). The
    Grafana rule covers billing rejections as events and resolves 15 minutes
    after the last one; this check covers every permanent class (auth /
    forbidden / model-not-found included) as a *state* — it fires while the
    fleet is still halted and resolves only as agents recover.

Both checks ride the health probe's existing machinery on purpose: the 300s
OS cron on gateway hosts, the edge-triggered alert ingest (one row plus one IM
per edge, with the local-ingest fallback when the gateway is unreachable), and
the WARNING -> ERROR episode grading. No new daemon, no new schedule, and no
dependency on anything the outage kills — in particular not on the LLM call
chain, which is dead the moment the account is.

Alert hygiene: the episode key is the exact failure message
(`_health_alerts._alert_failure`), so the messages here are deliberately
*stable* across runs — they name the configured threshold, never a live
value. A balance ticking down (or a growing halted count) inside the message
would reset the episode on every probe tick and the alert would crawl back to
its three-minute WARNING grade forever instead of firing. Live readings ride a
stderr detail line for the cron log instead.

Both checks are fail-open on read errors: a provider API blip or an
unreachable database is not evidence of a drained account, matching
`_schema_health`'s rule for its own DB flake.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from shared.config import settings
from shared.recovery_breaker import HALT_AFTER_CONSECUTIVE_PERMANENT_REJECTS


class BalanceReadError(RuntimeError):
    """A balance read that could not produce a payload, with a sanitized reason."""


def run_provider_guard(home: Path, *, alert_failure: Callable[[Path, str], None]) -> int | None:
    """Run checks 9-10 for the health probe; return 1 on failure, None on pass.

    `alert_failure` is the probe's edge-alert entry point
    (`_health_alerts._alert_failure`), passed in rather than imported so the
    caller's module attribute stays the single test seam.
    """
    failure = provider_guard_failure()
    if failure is not None:
        print(failure, file=sys.stderr)
        alert_failure(home, failure)
        return 1
    print("  ✓ provider account guard")
    return None


def provider_guard_failure() -> str | None:
    """The first failing provider-guard check as a ready-to-alert line, if any.

    Balance first (the root cause), blocked agents second; a run reports one
    failure at a time, like the probe's earlier checks.
    """
    balance = _balance_failure()
    if balance is not None:
        return f"FAIL: provider balance — {balance}"
    blocked = _blocked_agents_failure()
    if blocked is not None:
        return f"FAIL: provider blocked agents — {blocked}"
    return None


def _balance_failure() -> str | None:
    """Check 9: None when disabled, unconfigured, unreadable, or funded enough."""
    guard = settings.alerts
    if not guard.provider_guard_balance_enabled:
        return None
    key = settings.lm.deepseek_api_key
    if key is None or not key.get_secret_value().strip():
        print(
            "  (provider balance check skipped: DEEPSEEK_API_KEY not configured on this host)",
            file=sys.stderr,
        )
        return None
    try:
        payload = _fetch_balance(
            guard.provider_guard_balance_url,
            key.get_secret_value(),
            guard.provider_guard_balance_timeout_seconds,
        )
        total_cny = _cny_balance(payload)
    except BalanceReadError as exc:
        print(f"  (provider balance check skipped: {exc})", file=sys.stderr)
        return None
    if total_cny is None:
        print(
            "  (provider balance check skipped: no CNY balance entry in the response)",
            file=sys.stderr,
        )
        return None
    available = payload.get("is_available") is not False
    minimum = guard.provider_guard_balance_min_cny
    if available and total_cny >= minimum:
        return None
    print(
        f"  (provider balance detail: \u00a5{total_cny:.2f}, available={available}, "
        f"minimum=\u00a5{minimum:g})",
        file=sys.stderr,
    )
    if not available:
        return (
            "the provider reports the account as unavailable (is_available=false); "
            "check the account's billing state"
        )
    return (
        "account balance below the configured minimum "
        f"(AVA_PROVIDER_GUARD_BALANCE_MIN_CNY=\u00a5{minimum:g}); top up the "
        "provider account before it runs dry"
    )


def _fetch_balance(url: str, key: str, timeout: float) -> dict[str, Any]:
    """One authenticated read of the provider balance endpoint."""
    import httpx

    try:
        response = httpx.get(
            url,
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as exc:
        raise BalanceReadError(f"HTTP {exc.response.status_code}") from None
    except (httpx.HTTPError, ValueError) as exc:
        raise BalanceReadError(type(exc).__name__) from None
    if not isinstance(payload, dict):
        raise BalanceReadError("response is not a JSON object")
    return cast("dict[str, Any]", payload)


def _cny_balance(payload: dict[str, Any]) -> float | None:
    """The CNY `total_balance` from a DeepSeek /user/balance payload, or None.

    The guard compares in CNY — the account's funded currency — only; a
    payload without a CNY entry cannot be judged against a CNY minimum and
    passes with a stderr note rather than guessing at FX.
    """
    infos = payload.get("balance_infos")
    if not isinstance(infos, list):
        raise BalanceReadError("response has no balance_infos")
    for entry in cast("list[object]", infos):
        if not isinstance(entry, dict):
            continue
        fields = cast("dict[str, Any]", entry)
        if fields.get("currency") != "CNY":
            continue
        raw = fields.get("total_balance")
        try:
            return float(cast("str | float | int", raw))
        except (TypeError, ValueError):
            raise BalanceReadError("CNY total_balance is not numeric") from None
    return None


def _blocked_agents_failure() -> str | None:
    """Check 10: None when disabled, database-unreadable, or under threshold."""
    guard = settings.alerts
    if not guard.provider_guard_blocked_agents_enabled:
        return None
    count = _halted_agents_count(guard.provider_guard_blocked_agents_window_hours)
    if count is None:
        print(
            "  (provider blocked-agents check skipped: agent table unreachable)",
            file=sys.stderr,
        )
        return None
    if count < guard.provider_guard_blocked_agents_min:
        return None
    print(
        f"  (provider blocked-agents detail: {count} agent(s) halted by permanent "
        f"provider rejections within {guard.provider_guard_blocked_agents_window_hours:g}h)",
        file=sys.stderr,
    )
    return (
        "multiple agents are halted by permanent provider rejections "
        f"(threshold AVA_PROVIDER_GUARD_BLOCKED_AGENTS_MIN="
        f"{guard.provider_guard_blocked_agents_min}); automatic recovery stays off "
        "until a turn succeeds — resolve the provider cause, then revive the "
        "affected agents"
    )


def _halted_agents_count(window_hours: float) -> int | None:
    """Count agents halted by permanent provider rejections inside the window.

    `permanent_reject_streak >= HALT_AFTER_CONSECUTIVE_PERMANENT_REJECTS` is
    the recovery breaker's durable "halted" flag and the single source of
    truth for the state (task #3617); `last_turn_fatal_at` bounds it to the
    active wave. A completed turn clears both columns in one statement, so the
    pair stays coherent. None when the query cannot run — the caller reports
    "cannot judge" and passes, never a guessed healthy verdict.
    """
    import shared.db

    try:
        with shared.db.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM agents_meta"
                " WHERE permanent_reject_streak >= %s"
                "   AND last_turn_fatal_at > now() - make_interval(secs => %s)",
                (HALT_AFTER_CONSECUTIVE_PERMANENT_REJECTS, window_hours * 3600.0),
            )
            row = cur.fetchone()
    except Exception:
        return None
    return int(row[0]) if row is not None else None
