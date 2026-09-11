"""Hosted agent-host healthcheck launch environment."""

from __future__ import annotations

import pytest

from services.healthchecks import agent_host as healthcheck


def test_agent_host_env_carries_projected_runner_url(monkeypatch: pytest.MonkeyPatch) -> None:
    projected_url = "postgresql://ava_runner:runner-password@127.0.0.1:5433/ava"

    def _project(_url: str) -> str:
        return projected_url

    monkeypatch.setattr(
        healthcheck,
        "runner_db_url_projection",
        _project,
    )

    assert healthcheck._agent_host_env() == {
        "AVA_PROCESS_PROFILE": "agent",
        "AVA_DB_URL": projected_url,
    }


def test_main_waits_a_second_probe_round_before_respawning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One probe timeout must not respawn the host.

    The probe shares the machine (and the probe's own round budget) with up to
    ~50 concurrent turns, so a transient machine-level stall starves it past the
    5s bound without the host being dead — the 2026-09-11 respawn of a live host.
    A sustained condition still gets there: a wedged loop fails every round, so
    this only delays the respawn by one round while round 1 keeps logging.
    """
    captured: dict[str, object] = {}

    def _capture(label: str, log: object, **kwargs: object) -> None:
        captured["label"] = label
        captured.update(kwargs)

    def _noop_init(**_kw: object) -> None:
        return None

    monkeypatch.setattr(healthcheck, "init_gateway_process", _noop_init)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(healthcheck, "run_keepalive", _capture)  # pyright: ignore[reportUnknownArgumentType]

    healthcheck.main()

    assert captured["label"] == "agent-host"
    assert captured["consecutive_failures_before_respawn"] == 2
    assert captured["probe"] is healthcheck._probe
    assert captured["respawn"] is healthcheck._restart_daemon
