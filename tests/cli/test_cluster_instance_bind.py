"""Bounded wait for the reachable (tailscale) bind address before starting pg/redis.

On reboot brew/launchd can start `ava` before tailscale has assigned its address;
binding pg/redis to a not-yet-present address fails and takes the whole autostart
down. `_wait_for_reachable_bind` blocks (bounded) until the address is assigned, and
fails fast on timeout. A loopback-only single box never waits.
"""

import pytest

from cli.commands import _cluster_instance as _ci
from shared.config import settings


def test_addr_assigned_loopback_is_true() -> None:
    assert _ci._addr_assigned("127.0.0.1") is True


def test_addr_assigned_unassigned_ip_is_false() -> None:
    """192.0.2.0/24 (RFC 5737 TEST-NET-1) is never assigned to a real interface, so
    binding it raises EADDRNOTAVAIL — the exact 'address not up yet' signal."""
    assert _ci._addr_assigned("192.0.2.1") is False


def test_wait_returns_immediately_for_loopback_only_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """A single box reachable only at localhost never waits — loopback is always up."""
    monkeypatch.setattr(_ci, "reachable_host", lambda: "localhost")
    monkeypatch.setattr(
        _ci,
        "_addr_assigned",
        lambda _a: pytest.fail("must not probe on a loopback-only host"),  # pyright: ignore[reportUnknownArgumentType]
    )
    assert _ci._wait_for_reachable_bind() is True


def test_wait_returns_true_once_address_appears(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reachable address is absent on the first probe, present on the next — the
    tailscale-coming-up-late case — so the wait resolves True after retrying."""
    monkeypatch.setattr(_ci, "reachable_host", lambda: "100.64.0.5")
    monkeypatch.setattr(_ci.time, "sleep", lambda _s: None)  # pyright: ignore[reportUnknownArgumentType]
    seen: list[int] = []

    def _appears(_addr: str) -> bool:
        seen.append(1)
        return len(seen) >= 2

    monkeypatch.setattr(_ci, "_addr_assigned", _appears)
    assert _ci._wait_for_reachable_bind() is True
    assert len(seen) == 2


def test_wait_returns_false_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """The address never appears within the bound → fail fast (caller aborts start)."""
    monkeypatch.setattr(_ci, "reachable_host", lambda: "100.64.0.5")
    monkeypatch.setattr(_ci, "_BIND_WAIT_TIMEOUT_S", 0.0)
    monkeypatch.setattr(_ci.time, "sleep", lambda _s: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_ci, "_addr_assigned", lambda _a: False)  # pyright: ignore[reportUnknownArgumentType]
    assert _ci._wait_for_reachable_bind() is False


# ─── no-secret posture: loopback-only binds + trust-only pg_hba ──────────────


def test_bind_addrs_loopback_only_without_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """A no-secret cluster binds loopback alone, whatever the reachable address —
    an unauthenticated data plane must never be LAN-reachable."""
    monkeypatch.setattr(_ci, "reachable_host", lambda: "100.64.0.5")
    assert _ci._bind_addrs("") == ["127.0.0.1"]


def test_bind_addrs_includes_reachable_with_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """With a secret, the reachable address joins loopback as today (auth makes
    the non-loopback bind safe)."""
    monkeypatch.setattr(_ci, "reachable_host", lambda: "100.64.0.5")
    assert _ci._bind_addrs("s3cret") == ["127.0.0.1", "100.64.0.5"]


def test_pg_hba_body_no_scram_lines_without_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """No secret -> local trust + loopback trust only; no scram host lines, and
    no reachable/cidr lines either."""
    monkeypatch.setattr(settings.data_plane, "trusted_cidrs", "10.0.0.0/8")
    monkeypatch.setattr(_ci, "reachable_host", lambda: "100.64.0.5")
    body = _ci._pg_hba_body("")
    assert "scram" not in body
    assert body.splitlines() == [
        "local all all trust",
        "host all all 127.0.0.1/32 trust",
        "host all all ::1/128 trust",
    ]


def test_pg_hba_body_scram_with_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """With a secret the posture is unchanged: scram everywhere TCP, including
    the reachable host and trusted CIDRs."""
    monkeypatch.setattr(settings.data_plane, "trusted_cidrs", "10.0.0.0/8")
    monkeypatch.setattr(_ci, "reachable_host", lambda: "100.64.0.5")
    body = _ci._pg_hba_body("s3cret")
    assert body.splitlines() == [
        "local all all trust",
        "host all all 127.0.0.1/32 scram-sha-256",
        "host all all ::1/128 scram-sha-256",
        "host all all 100.64.0.5/32 scram-sha-256",
        "host all all 10.0.0.0/8 scram-sha-256",
    ]


# ─── Task #1113: the passed secret wins over ambient settings ────────────────


def test_bind_addrs_follows_passed_secret_not_ambient_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bring-up passes the cluster's OWN secret (install: the decided one;
    start: the authority-passed .env value). An ambient settings value inherited
    from a sibling cluster (a prod-sourced shell running an install) must not
    widen a no-secret cluster's bind to the LAN."""
    # Ambient settings carry a foreign secret — the leak Task #1113 reproduces.
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "foreign-sibling-secret")
    monkeypatch.setattr(_ci, "reachable_host", lambda: "100.64.0.5")
    assert _ci._bind_addrs("") == ["127.0.0.1"]


def test_pg_hba_body_follows_passed_secret_not_ambient_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hba is written from the passed cluster secret, never from ambient
    settings — otherwise a no-secret cluster born from a prod-sourced shell
    gets scram lines keyed to a FOREIGN secret (its own first-start migration
    then fails `fe_sendauth: no password supplied` against the active hba)."""
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "foreign-sibling-secret")
    monkeypatch.setattr(settings.data_plane, "trusted_cidrs", "10.0.0.0/8")
    monkeypatch.setattr(_ci, "reachable_host", lambda: "100.64.0.5")
    body = _ci._pg_hba_body("")
    assert "scram" not in body
    assert "foreign" not in body
    assert body.splitlines() == [
        "local all all trust",
        "host all all 127.0.0.1/32 trust",
        "host all all ::1/128 trust",
    ]
