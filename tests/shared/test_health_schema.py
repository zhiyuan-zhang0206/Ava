"""Contract tests for the fleet-wide health response envelope."""

from __future__ import annotations

import json

from shared.health_schema import DEGRADED, DOWN, OK, component, render


def test_component_omits_unknown_state_and_calculates_success_age() -> None:
    payload = component(
        "backup",
        OK,
        last_success=100.04,
        last_error=99.0,
        progress="idle",
        detail="last attempt recovered",
        now=160.04,
    )

    assert payload == {
        "name": "backup",
        "status": "ok",
        "last_success": 100.04,
        "last_error": 99.0,
        "age_s": 60.0,
        "progress": "idle",
        "detail": "last attempt recovered",
    }


def test_render_uses_component_worst_state_and_explains_it() -> None:
    status, body = render(
        {"name": "scheduler", "home": "/var/ava"},
        [
            component("loop", OK, progress="serving"),
            component("backup", DEGRADED, detail="dump failed"),
            component("replica", DOWN),
        ],
    )

    assert status == 503
    assert json.loads(body) == {
        "name": "scheduler",
        "home": "/var/ava",
        "status": "degraded",
        "readiness": "degraded",
        "components": [
            {"name": "loop", "status": "ok", "progress": "serving"},
            {"name": "backup", "status": "degraded", "detail": "dump failed"},
            {"name": "replica", "status": "down"},
        ],
        "degraded_reasons": ["backup: dump failed", "replica: down"],
    }


def test_render_preserves_identity_keys_over_generated_keys() -> None:
    status, body = render(
        {"status": "ok", "readiness": "external", "name": "gateway"},
        [component("db", DEGRADED, detail="PoolTimeout")],
        extra={"saturation": 0.5},
    )

    assert status == 503
    assert json.loads(body) == {
        "status": "ok",
        "readiness": "external",
        "name": "gateway",
        "components": [{"name": "db", "status": "degraded", "detail": "PoolTimeout"}],
        "degraded_reasons": ["db: PoolTimeout"],
        "saturation": 0.5,
    }


def test_render_marks_an_explicitly_stale_liveness_as_degraded() -> None:
    status, body = render({"name": "worker"}, [component("loop", OK)], stale_for=12.0)

    assert status == 503
    assert json.loads(body) == {
        "name": "worker",
        "status": "degraded",
        "liveness": "stale",
        "readiness": "degraded",
        "components": [{"name": "loop", "status": "ok"}],
        "degraded_reasons": [],
    }
