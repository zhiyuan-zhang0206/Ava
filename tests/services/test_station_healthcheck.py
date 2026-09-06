"""`services.heartbeat.station_probe` unit tests — the remote-station probe.

WP4 (task #1946): the gateway probes a REMOTE observatory station through
the reachability contract (the address the station advertises in
machine_units) with the cluster bearer, and FAILS OPEN — a failed probe
never blocks or restarts local business; it records an alert after two
consecutive failures and resolves on recovery. With no AVA_OBSERVABILITY_URL
configured the check is a no-op.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from datetime import UTC, datetime

import pytest

import shared.db
from services.heartbeat import station_probe as hc
from shared.config import settings


@pytest.fixture(autouse=True)
def _clean_state() -> None:
    """Reset the in-process probe state between tests (the module dict is
    process-global, same as the watchdog would hold it)."""
    hc._state["failures"] = 0
    hc._state["transition_since"] = None
    with shared.db.connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM alerts WHERE labels->>'alertname' = %s", (hc._ALERTNAME,))
        cur.execute("DELETE FROM machine_units WHERE machine_name LIKE 'station-test%'")
        conn.commit()


@pytest.fixture(autouse=True)
def _no_remote_observatory(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default: no remote observatory (the no-op posture); tests opt in."""
    monkeypatch.setattr(settings.observability, "observability_url", "")
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "cluster-token")
    monkeypatch.setattr(hc.settings.observability, "telemetry_otlp_port", 4318)


def _insert_station_unit(name: str = "station-test-a", url: str = "http://10.0.0.9:4318") -> None:
    with shared.db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO machine_units "
            "(machine_name, home, serve_gateway, serve_agent_runner, "
            "serve_observability_station, url, up_since_at, stopped_at) "
            "VALUES (%s, %s, false, false, true, %s, now(), NULL)",
            (name, "~/.ava_station", url),
        )
        conn.commit()


def test_main_is_noop_without_remote_observatory(monkeypatch: pytest.MonkeyPatch) -> None:
    """No AVA_OBSERVABILITY_URL -> nothing to probe: no DB read, no network,
    no alert (the local observatory is the lgtm check's job)."""
    calls: list[str] = []

    def _fail_if_called(url: str) -> bool:
        calls.append(url)
        return False

    monkeypatch.setattr(hc, "_station_answers", _fail_if_called)
    hc.main()
    assert calls == []


def test_resolve_target_uses_advertised_machine_units_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """The probe target is the station's ADVERTISED machine_units url — the
    reachability contract — not the configured base."""
    monkeypatch.setattr(settings.observability, "observability_url", "http://10.0.0.9")
    _insert_station_unit()
    target = hc.resolve_target()
    assert target is not None
    assert target.url == "http://10.0.0.9:4318"
    assert target.advertised is True
    assert target.name == "station-test-a"


def test_resolve_target_falls_back_to_configured_base_without_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AVA_OBSERVABILITY_URL set but no station unit registered -> warn loudly
    and probe the configured base + OTLP port (fail-open: still give signal)."""
    monkeypatch.setattr(settings.observability, "observability_url", "http://10.0.0.46")
    target = hc.resolve_target()
    assert target is not None
    assert target.url == "http://10.0.0.46:4318"
    assert target.advertised is False


def test_resolve_target_skips_hybrid_gateway_station_units(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hybrid gateway+station unit advertises its GATEWAY url (unit_dial_url
    lets the gateway capability win), not the OTLP ingress — probing it would
    hit the gateway API and alert forever (QA #1156 NIT-2). Only units whose
    advertised url carries the OTLP ingress port qualify."""
    monkeypatch.setattr(settings.observability, "observability_url", "http://10.0.0.46")
    _insert_station_unit(url="http://10.0.0.9:8000")  # gateway-form advertisement
    target = hc.resolve_target()
    assert target is not None
    assert target.url == "http://10.0.0.46:4318"
    assert target.advertised is False


def test_resolve_target_none_without_observability_url() -> None:
    assert hc.resolve_target() is None


def test_station_answers_bearer_otlp_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 2xx on POST <url>/v1/traces with the cluster bearer = alive."""
    seen: dict[str, object] = {}

    class _Resp:
        status = 200

        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *_a: object) -> None:
            return None

    def _open(req: urllib.request.Request, **kw: object) -> _Resp:
        seen["url"] = req.full_url
        seen["auth"] = req.headers.get("Authorization")
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", _open)
    assert hc._station_answers("http://10.0.0.9:4318") is True
    assert seen["url"] == "http://10.0.0.9:4318/v1/traces"
    assert seen["auth"] == "Bearer cluster-token"


def test_station_answers_http_error_is_not_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-2xx answer (401 auth, 415 body, 5xx) proves the listener is up
    but the ingestion path is not serving — not alive."""

    def _raise(_req: object, **_kw: object) -> None:
        raise urllib.error.HTTPError("http://x/v1/traces", 401, "unauthorized", {}, None)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(urllib.request, "urlopen", _raise)
    assert hc._station_answers("http://10.0.0.9:4318") is False


def test_station_answers_connection_failure_is_not_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(_req: object, **_kw: object) -> None:
        raise OSError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", _raise)
    assert hc._station_answers("http://10.0.0.9:4318") is False


def test_station_answers_without_secret_warns_and_fails_open(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No cluster secret -> cannot authenticate the probe; warn and treat as
    alive rather than alerting on a config problem (fail-open)."""
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "")

    def _fail_if_called(_req: object, **_kw: object) -> None:
        pytest.fail("must not dial without a secret")

    monkeypatch.setattr(urllib.request, "urlopen", _fail_if_called)
    assert hc._station_answers("http://10.0.0.9:4318") is True


def test_alert_fires_after_two_consecutive_failures_and_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two consecutive failed probes fire the alert; a successful probe
    resolves every open row for the alertname (the machine-offline pattern)."""
    monkeypatch.setattr(hc.settings.alerts, "transition_warning_seconds", 0)
    monkeypatch.setattr(hc.settings.alerts, "transition_error_seconds", 3600)
    monkeypatch.setattr("shared.alerts.notify_im", lambda _text: True)  # pyright: ignore[reportUnknownArgumentType]
    target = hc._StationTarget(url="http://10.0.0.9:4318", advertised=True, name="station-test-a")
    now = datetime.now(UTC)

    # first failure: below the consecutive-failure threshold — no alert yet
    hc._alert_edges(target, ok=False, now=now)
    with shared.db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM alerts WHERE labels->>'alertname' = %s",
            (hc._ALERTNAME,),
        )
        row = cur.fetchone()
        assert row is not None and row[0] == 0

    # second consecutive failure: fires
    hc._alert_edges(target, ok=False, now=now)
    with shared.db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status, labels->>'severity' FROM alerts WHERE labels->>'alertname' = %s",
            (hc._ALERTNAME,),
        )
        row = cur.fetchone()
    # the alerts table stores the open status as 'unresolved' (the wire
    # status is 'firing'; upsert_alert normalizes)
    assert row is not None and row[0] == "unresolved" and row[1] == "warning"

    hc._alert_edges(target, ok=True, now=now)
    with shared.db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM alerts WHERE labels->>'alertname' = %s",
            (hc._ALERTNAME,),
        )
        rows = cur.fetchall()
    assert rows and all(r[0] == "resolved" for r in rows)


def test_main_probes_advertised_station_and_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: with a registered station and a healthy probe, main()
    runs clean and no alert fires."""
    monkeypatch.setattr(settings.observability, "observability_url", "http://10.0.0.9")
    _insert_station_unit()
    monkeypatch.setattr(hc, "_station_answers", lambda _url: True)  # pyright: ignore[reportUnknownArgumentType]
    hc.main()  # must not raise
    with shared.db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM alerts WHERE labels->>'alertname' = %s",
            (hc._ALERTNAME,),
        )
        row = cur.fetchone()
        assert row is not None and row[0] == 0
