"""The single WAL-G key: off by default, writable, host-scoped, never distributed."""

from __future__ import annotations

from base.config.domains.storage.walg import WalgSettings
from base.host.env.config_registry import DOMAIN_MODELS, fields, schema_extra


def test_the_domain_is_registered_once_under_the_gateway_capability() -> None:
    rows = [row for row in DOMAIN_MODELS if row[0] == "walg"]

    assert rows == [("walg", "WAL-G backup", "WalgSettings", "gateway")]


def test_the_key_is_the_domains_only_field_and_off_by_default() -> None:
    assert list(WalgSettings.model_fields) == ["walg_config_file"]
    assert WalgSettings.model_fields["walg_config_file"].alias == "AVA_WALG_CONFIG_FILE"
    assert WalgSettings().walg_config_file is None


def test_the_key_can_be_set_and_unset_and_stays_on_its_host() -> None:
    """Writable, so `ava config unset` can always remove it; host scope and
    `bootstrap: False` keep a gateway path from being pushed to other machines."""
    ref = fields()["walg_config_file"]
    extra = schema_extra(ref.info)

    assert ref.domain == "walg"
    assert extra["writable"] is True
    assert extra["scope"] == "host"
    assert extra["remote_writable"] is False
    assert extra["bootstrap"] is False
    assert extra["restart_required"] == "all"
    assert extra["sensitive"] is False
