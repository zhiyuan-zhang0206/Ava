from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_CONTACT = (
    Path(__file__).resolve().parents[2]
    / "deploy/lgtm/config/grafana/provisioning/alerting/contact.yml"
)


def _document() -> dict[str, Any]:
    return yaml.safe_load(_CONTACT.read_text(encoding="utf-8"))


def _policy() -> dict[str, Any]:
    (policy,) = _document()["policies"]
    return policy


def _routes() -> dict[str, dict[str, Any]]:
    routes: dict[str, dict[str, Any]] = {}
    for route in _policy()["routes"]:
        label = route["object_matchers"][0][0]
        if route["receiver"] == "ava-alerts-telegram":
            routes[f"telegram:{label}"] = route
        else:
            routes["critical" if label == "severity" else "catch-all"] = route
    return routes


def test_every_alert_reaches_the_webhook_receiver() -> None:
    contacts = {c["name"]: c for c in _document()["contactPoints"]}
    (receiver,) = contacts["ava-alerts-webhook"]["receivers"]
    assert receiver["type"] == "webhook"
    assert receiver["disableResolveMessage"] is False
    policy = _policy()
    assert policy["receiver"] == "ava-alerts-webhook"
    assert policy["matchers"] == []
    assert _routes()["catch-all"]["object_matchers"] == [["alertname", "=~", ".+"]]
    assert list(_routes()) == [
        "telegram:attributes_check",
        "telegram:metric",
        "critical",
        "catch-all",
    ]


def test_instances_of_one_rule_are_merged_into_one_notification_group() -> None:
    policy = _policy()
    assert policy["group_by"] == ["alertname"]
    assert (policy["group_wait"], policy["group_interval"], policy["repeat_interval"]) == (
        "30s",
        "5m",
        "4h",
    )
    catch_all = _routes()["catch-all"]
    assert catch_all["group_by"] == ["alertname"]
    assert (catch_all["group_wait"], catch_all["group_interval"], catch_all["repeat_interval"]) == (
        "30s",
        "5m",
        "4h",
    )


def test_critical_alerts_skip_the_batching_wait() -> None:
    route = _routes()["critical"]
    assert route["object_matchers"] == [["severity", "=", "critical"]]
    assert "continue" not in route
    assert (route["group_wait"], route["group_interval"], route["repeat_interval"]) == (
        "0s",
        "1m",
        "1h",
    )


def test_gateway_liveness_also_reaches_telegram_directly_and_still_the_webhook() -> None:
    contacts = {c["name"]: c for c in _document()["contactPoints"]}
    (receiver,) = contacts["ava-alerts-telegram"]["receivers"]
    assert receiver["type"] == "telegram"
    assert receiver["settings"] == {
        "bottoken": "$__env{AVA_ALERTS_TELEGRAM_BOT_TOKEN}",
        "chatid": "$__env{AVA_ALERTS_TELEGRAM_CHAT_ID}",
    }
    for key, matcher in (
        ("telegram:attributes_check", ["attributes_check", "=", "gateway_liveness"]),
        ("telegram:metric", ["metric", "=", "health_probe_silent"]),
    ):
        route = _routes()[key]
        assert route["object_matchers"] == [matcher]
        assert route["continue"] is True
