"""Rate-limit contract tests for POST /api/auth/login.

The login endpoint is the gateway's only unauthenticated credential check,
so it is protected by a per-IP failure lockout (shared/rate_limit.py):
``gateway.login_max_failures`` consecutive failures lock the IP for
``gateway.login_lockout_seconds`` (defaults 5 / 900);
a successful login resets the counter; lockout expiry restores normal
operation. While locked the endpoint answers 429 + ``Retry-After`` and does
not evaluate the credential at all.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from shared import config, rate_limit
from shared.rate_limit import login_limiter

_SECRET = "test-cluster-secret"  # noqa: S105 — test fixture


class _FakeClock:
    """Stand-in for the ``time`` module — ``time.time()`` reads a mutable value."""

    def __init__(self) -> None:
        self.t = 1_000_000.0

    def time(self) -> float:
        return self.t


@pytest.fixture(autouse=True)
def _patch_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable auth with a known secret, mirroring test_auth.py."""
    monkeypatch.setattr(config.settings.gateway, "auth_middleware_enabled", True)
    monkeypatch.setattr(config.settings.data_plane, "cluster_secret", _SECRET)


@pytest.fixture(autouse=True)
def _reset_limiter() -> Iterator[None]:
    """The limiter is a process-wide singleton; each test starts (and ends)
    with a clean slate so no test leaks a lockout into the next one."""
    login_limiter.reset()
    yield
    login_limiter.reset()


def _wrong_login(
    client: TestClient,
    password: str = "wrong-password",  # noqa: S107 — test fixture
) -> None:
    resp = client.post("/api/auth/login", json={"password": password})
    assert resp.status_code == 401
    assert resp.json()["code"] == "invalid_password"


# ── Endpoint contract ─────────────────────────────────────────────────


def test_sixth_consecutive_failure_is_locked_out() -> None:
    with TestClient(app) as client:
        for _ in range(config.settings.gateway.login_max_failures):
            _wrong_login(client)
        resp = client.post("/api/auth/login", json={"password": "wrong-password"})
    assert resp.status_code == 429
    body = resp.json()
    assert body["code"] == "login_rate_limited"
    assert body["detail"] == "too many failed login attempts"
    assert body["retryable"] is True
    retry_after = int(resp.headers["retry-after"])
    assert 0 < retry_after <= config.settings.gateway.login_lockout_seconds
    assert body["retry_after_seconds"] == retry_after


def test_correct_password_is_not_evaluated_while_locked() -> None:
    """While locked the credential never reaches the comparison — a correct
    password also gets 429, so the lockout cannot be probed away."""
    with TestClient(app) as client:
        for _ in range(config.settings.gateway.login_max_failures):
            _wrong_login(client)
        resp = client.post("/api/auth/login", json={"password": _SECRET})
    assert resp.status_code == 429


def test_successful_login_resets_failure_counter() -> None:
    with TestClient(app) as client:
        # A success after 4 failures clears the streak...
        for _ in range(config.settings.gateway.login_max_failures - 1):
            _wrong_login(client)
        assert client.post("/api/auth/login", json={"password": _SECRET}).status_code == 200
        # ...so five more failures are needed before the next lockout.
        for _ in range(config.settings.gateway.login_max_failures):
            _wrong_login(client)
        resp = client.post("/api/auth/login", json={"password": "wrong-password"})
    assert resp.status_code == 429


def test_lockout_expires_and_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClock()
    monkeypatch.setattr(rate_limit, "time", fake)
    with TestClient(app) as client:
        for _ in range(config.settings.gateway.login_max_failures):
            _wrong_login(client)
        assert (
            client.post("/api/auth/login", json={"password": "wrong-password"}).status_code == 429
        )
        fake.t += config.settings.gateway.login_lockout_seconds + 1
        # Expired: a wrong password is a plain 401 again, the correct one works.
        _wrong_login(client)
        resp = client.post("/api/auth/login", json={"password": _SECRET})
    assert resp.status_code == 200


def test_lockout_is_per_ip() -> None:
    attacker = TestClient(app, client=("203.0.113.7", 40000))
    with attacker:
        for _ in range(config.settings.gateway.login_max_failures):
            _wrong_login(attacker)
        assert (
            attacker.post("/api/auth/login", json={"password": "wrong-password"}).status_code == 429
        )
    # A different IP is unaffected — its correct password still logs in.
    with TestClient(app, client=("203.0.113.8", 40000)) as other:
        resp = other.post("/api/auth/login", json={"password": _SECRET})
    assert resp.status_code == 200


def test_lockout_threshold_and_window_follow_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lockout numbers resolve from config at call time — a cluster can
    tighten the policy without a new release (task #3696: every user-facing
    limit carries a config home)."""
    fake = _FakeClock()
    monkeypatch.setattr(rate_limit, "time", fake)
    monkeypatch.setattr(config.settings.gateway, "login_max_failures", 2)
    monkeypatch.setattr(config.settings.gateway, "login_lockout_seconds", 123)
    with TestClient(app) as client:
        _wrong_login(client)  # failure 1 — below the configured threshold
        _wrong_login(client)  # failure 2 — trips it
        resp = client.post("/api/auth/login", json={"password": "wrong-password"})
        assert resp.status_code == 429
        assert 0 < int(resp.headers["retry-after"]) <= 123
        fake.t += 124  # the configured window has passed
        _wrong_login(client)  # expired: a plain 401 again


# ── Limiter unit behavior ─────────────────────────────────────────────


def test_limiter_locks_at_threshold_and_restarts_after_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeClock()
    monkeypatch.setattr(rate_limit, "time", fake)
    limiter = rate_limit.LoginRateLimiter()
    ip = "203.0.113.9"

    for _ in range(config.settings.gateway.login_max_failures - 1):
        assert limiter.lockout_remaining(ip) == 0
        limiter.record_failure(ip)
    assert limiter.lockout_remaining(ip) == 0

    limiter.record_failure(ip)
    assert limiter.lockout_remaining(ip) == config.settings.gateway.login_lockout_seconds
    assert limiter.lockout_remaining("203.0.113.10") == 0  # other IP untouched

    # Lockout expires and the streak starts fresh: one failure after the
    # window is nowhere near a lockout again.
    fake.t += config.settings.gateway.login_lockout_seconds + 1
    assert limiter.lockout_remaining(ip) == 0
    limiter.record_failure(ip)
    for _ in range(config.settings.gateway.login_max_failures - 2):
        limiter.record_failure(ip)
    assert limiter.lockout_remaining(ip) == 0
    limiter.record_failure(ip)
    assert limiter.lockout_remaining(ip) == config.settings.gateway.login_lockout_seconds


def test_limiter_success_resets_streak(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClock()
    monkeypatch.setattr(rate_limit, "time", fake)
    limiter = rate_limit.LoginRateLimiter()
    ip = "203.0.113.11"

    for _ in range(config.settings.gateway.login_max_failures - 1):
        limiter.record_failure(ip)
    limiter.record_success(ip)
    for _ in range(config.settings.gateway.login_max_failures - 1):
        limiter.record_failure(ip)
    assert limiter.lockout_remaining(ip) == 0
    limiter.record_failure(ip)
    assert limiter.lockout_remaining(ip) == config.settings.gateway.login_lockout_seconds
