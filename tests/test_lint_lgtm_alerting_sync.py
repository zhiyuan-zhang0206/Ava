"""The LGTM Grafana alerting provisioning stays in sync with its source.

`deploy/local/lgtm/config/grafana/provisioning/alerting/` is what the running
LGTM Grafana container loads (mounted read-only into /etc/grafana/
provisioning/alerting). The source of truth is `dashboards/ops/alerts/` (the
3002-era home, still synced by `_sync_grafana_provisioning` until the 3002
retirement). Two divergent copies rot silently — rules drift and nobody
notices until an alert misfires. The rules file must be byte-identical; the
contact file may differ ONLY in the webhook URL line (container reaches the
gateway on the host via host.docker.internal) and the auth mechanism
(container Grafana 13.1.3 drops provisioning secureSettings, so the LGTM
copy authenticates with the notifier-native Authorization fields whose
credentials are migrated into encrypted secureSettings by the schema layer;
see the file's header comment for the verified details).
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "dashboards" / "ops" / "alerts"
_LGTM = (
    _REPO_ROOT / "deploy" / "local" / "lgtm" / "config" / "grafana" / "provisioning" / "alerting"
)

# The two allowed contact.yml divergences: the webhook URL (container -> host)
# and the auth settings block (13.1.3 secureSettings-dropping workaround).
_ALLOWED_URL = "          url: http://host.docker.internal:8000/api/alerts"
_SOURCE_URL = "          url: http://localhost:8000/api/alerts"
_ALLOWED_AUTH = """          authorization_scheme: Bearer
          authorization_credentials: $__env{OPS_ALERTS_WEBHOOK_TOKEN}"""
_SOURCE_AUTH = """          httpHeaderName1: X-Ops-Alerts-Token"""
_SOURCE_SECURE_BLOCK = """        secureSettings:
          httpHeaderValue1: $__env{OPS_ALERTS_WEBHOOK_TOKEN}
"""


def test_rules_yml_identical() -> None:
    assert (_LGTM / "rules.yml").read_text() == (_SRC / "rules.yml").read_text()


def test_contact_yml_only_known_divergences() -> None:
    lgtm = (_LGTM / "contact.yml").read_text()
    src = (_SRC / "contact.yml").read_text()
    # Normalize the known divergences (header note, URL, auth block), then compare.
    lgtm_norm = lgtm.replace(_ALLOWED_URL, _SOURCE_URL)
    lgtm_norm = lgtm_norm.replace(_ALLOWED_AUTH, _SOURCE_AUTH)
    lgtm_norm = lgtm_norm.split("apiVersion: 1", 1)[1]
    src_norm = src.split("apiVersion: 1", 1)[1]
    # The source keeps its secureSettings block (3002 host-side Grafana); the
    # LGTM copy has none (13.1.3 drops it). Normalize that too.
    src_norm = src_norm.replace(_SOURCE_SECURE_BLOCK, "")
    assert lgtm_norm == src_norm
