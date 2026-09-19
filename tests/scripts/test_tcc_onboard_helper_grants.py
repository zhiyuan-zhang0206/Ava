"""scripts/tcc-onboard-helper-grants.py: the tier model is data, and it stays honest.

The tier table is the machine-facing contract from design v1 (user decisions
2026-09-17): L2 is macmini's target, L3 gained Full Disk Access, and extended
groups must have their state read and reported. --fill-pending adds
best-effort triggers for appdata/media/icloud; fda/devtools stay read-only by
decision, and the guard keeps the experimental fill list behind an explicit
user-present attestation.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "tcc-onboard-helper-grants.py"
_spec = importlib.util.spec_from_file_location("tcc_onboard_under_test", _SCRIPT)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_l1_is_the_legacy_implemented_set() -> None:
    assert _mod.TIER_GROUPS["L1"] == _mod.IMPLEMENTED_GROUPS
    assert _mod.IMPLEMENTED_GROUPS == ("folders", "apple-events", "sr-ax")


def test_l0_is_empty_and_silent() -> None:
    assert _mod.TIER_GROUPS["L0"] == ()


def test_tiers_grow_monotonically_and_stay_known() -> None:
    l1, l2, l3 = (_mod.TIER_GROUPS[tier] for tier in ("L1", "L2", "L3"))
    assert set(l1) < set(l2) < set(l3)
    assert set(l3) <= set(_mod.ITEM_GROUPS)


def test_full_tier_carries_fda_and_the_extended_tier_does_not() -> None:
    assert "fda" in _mod.TIER_GROUPS["L3"]
    assert "fda" not in _mod.TIER_GROUPS["L2"]


def test_extended_groups_declare_their_preflight_services() -> None:
    for group in ("appdata", "media", "icloud", "fda", "devtools"):
        services = _mod.EXTENDED_GROUPS[group]
        assert services
        assert all(service.startswith("kTCCService") for service in services)


def test_preflight_matrix_covers_every_extended_service() -> None:
    covered = set(_mod.PREFLIGHT_SERVICES)
    for services in _mod.EXTENDED_GROUPS.values():
        assert set(services) <= covered


def test_fill_specs_partition_the_extended_groups() -> None:
    fillable = set(_mod.FILL_SPECS)
    never = set(_mod.NEVER_TRIGGERED_GROUPS)
    assert fillable == {"appdata", "media", "icloud"}
    assert never == {"fda", "devtools"}
    assert fillable | never == set(_mod.EXTENDED_GROUPS)


def test_fill_specs_reference_declared_services_by_home_relative_path() -> None:
    target_ids = [target[0] for specs in _mod.FILL_SPECS.values() for target in specs]
    assert len(target_ids) == len(set(target_ids))
    for group, specs in _mod.FILL_SPECS.items():
        assert specs
        for _, service, rel in specs:
            assert service in _mod.EXTENDED_GROUPS[group]
            assert rel and not rel.startswith(("/", "~"))


def test_scan_child_compiles() -> None:
    compile(_mod._SCAN_CHILD, "<scan-child>", "exec")


def test_state_status_is_granted_only_when_every_service_is() -> None:
    services = _mod.EXTENDED_GROUPS["media"]
    matrix = dict.fromkeys(services, "granted")
    assert _mod.state_status(services, matrix, "note") == "granted"
    matrix[services[-1]] = "denied"
    text = _mod.state_status(services, matrix, "note")
    assert "denied" in text
    assert "note" in text


def test_extended_note_states_the_trigger_posture() -> None:
    assert _mod.extended_note("fda", set()) == "no trigger method by decision"
    assert _mod.extended_note("devtools", set()) == "no trigger method by decision"
    assert "fill attempted" in _mod.extended_note("appdata", {"appdata"})
    assert "--fill-pending" in _mod.extended_note("media", set())


def test_fill_request_error_requires_the_user_present_guard() -> None:
    error = _mod.fill_request_error(fill_pending=True, confirm_user_present=False, check_only=False)
    assert error is not None
    assert "--confirm-user-present" in error
    assert (
        _mod.fill_request_error(fill_pending=True, confirm_user_present=True, check_only=False)
        is None
    )


def test_fill_request_error_refuses_the_check_only_combination() -> None:
    error = _mod.fill_request_error(fill_pending=True, confirm_user_present=True, check_only=True)
    assert error is not None
    assert "--check" in error


def test_fill_request_error_is_quiet_without_fill_pending() -> None:
    error = _mod.fill_request_error(fill_pending=False, confirm_user_present=False, check_only=True)
    assert error is None


def test_groups_for_tier_defaults_to_the_implemented_set() -> None:
    assert _mod.groups_for_tier(None) == _mod.IMPLEMENTED_GROUPS


def test_groups_for_tier_returns_the_table_entry() -> None:
    assert _mod.groups_for_tier("L2") == _mod.TIER_GROUPS["L2"]
    assert _mod.groups_for_tier("L0") == ()


def test_count_unresolved_follows_the_single_resolution_rule() -> None:
    assert _mod.count_unresolved({}) == 0
    statuses = {
        "granted-item": "granted",
        "already-granted-item": "already granted",
        "denied-item": "denied",
        "stale-item": "unresolved (timed out)",
        "missing-item": "missing",
        "unknown-item": "unknown",
    }
    assert _mod.count_unresolved(statuses) == 4
