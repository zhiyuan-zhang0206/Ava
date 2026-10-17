from __future__ import annotations

from base.host.permissions_helper.grants import catalog
from base.host.permissions_helper.grants.catalog import Grant, PermissionItem, RequestMode


def test_ids_are_unique_and_indexed() -> None:
    ids = [item.id for item in catalog.CATALOG]
    assert len(ids) == len(set(ids))
    assert set(ids) == set(catalog.ITEMS_BY_ID)


def test_every_item_states_why_where_and_how_to_verify() -> None:
    for item in catalog.CATALOG:
        assert isinstance(item, PermissionItem)
        assert item.why and item.why.endswith("."), item.id
        assert item.verify, item.id
        assert item.pane.startswith("Privacy_"), item.id
        assert catalog.settings_url(item).endswith(item.pane)


def test_services_are_tcc_service_names_and_all_read_by_the_probe() -> None:
    for item in catalog.CATALOG:
        assert item.services, item.id
        for service in item.services:
            assert service.startswith("kTCCService"), (item.id, service)
            assert service in catalog.PREFLIGHT_SERVICES


def test_required_set_is_the_product_desktop_surface() -> None:
    required = {item.id for item in catalog.REQUIRED_ITEMS}
    assert {
        "screen-recording",
        "accessibility",
        "desktop-folder",
        "documents-folder",
        "downloads-folder",
        *(f"automation-{suffix}" for suffix, _ in catalog.AUTOMATION_TARGETS),
    } == required
    assert all(not item.required for item in catalog.REPORT_ONLY_ITEMS)


def test_report_only_rows_are_never_prompted() -> None:
    for item in catalog.REPORT_ONLY_ITEMS:
        assert item.mode is RequestMode.REPORT_ONLY
        assert not item.required


def test_folder_items_declare_the_folder_their_request_touches() -> None:
    for item in catalog.FOLDER_ITEMS:
        assert item.mode is RequestMode.PROMPT
        assert item.home_subpath in ("Desktop", "Documents", "Downloads")


def test_grant_vocabulary_is_fail_fast() -> None:
    assert {member.value for member in Grant} == {
        "granted",
        "denied",
        "not_determined",
        "unknown",
    }
