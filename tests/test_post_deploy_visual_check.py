"""Pure contracts for the post-deploy visual regression runner."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import pytest

from scripts.post_deploy_visual_check import (
    _accept_wave,
    _container_command,
    _cookie_mount,
    _expected_capture_names,
    _validate_demo_target,
)
from scripts.post_deploy_visual_policy import (
    STRUCTURAL_SPECS,
    SURFACE_ROUTES,
    THEMES,
    VIEWPORTS,
    DiffRegion,
    attribute_surface,
    classify_stable_diff,
    decide_escalation,
    extract_gateway_sha,
    extract_gateway_started_at,
    route_for_surface,
    unexpected_pixel_surfaces,
    validate_wave_id,
)


def test_surface_routes_cover_the_approved_matrix() -> None:
    assert set(STRUCTURAL_SPECS) == {
        "login",
        "home",
        "fleet",
        "control",
        "run-timeline",
    }
    assert set(SURFACE_ROUTES) == set(STRUCTURAL_SPECS)
    assert VIEWPORTS == {"desktop": (1280, 800), "narrow": (390, 844)}
    assert THEMES == ("light", "dark")
    assert route_for_surface("login") == "/login"
    assert route_for_surface("home-composer") == "/"
    assert route_for_surface("fleet-main") == "/fleet"
    assert route_for_surface("control-nav") == "/control"
    assert route_for_surface("run-timeline-main") == "/insights/run/1"


def test_surface_attribution_matches_route_and_component_paths() -> None:
    attribution = attribute_surface(
        "home-composer",
        [
            "ui/web/src/components/composer.tsx",
            "ui/web/src/app/fleet/page.tsx",
        ],
    )

    assert attribution.expected is True
    assert attribution.matched_paths == ("ui/web/src/components/composer.tsx",)
    assert attribute_surface("home-sidebar", ["ui/web/src/components/agent-sidebar.tsx"]).expected
    assert attribute_surface("missing-surface", ["ui/web/src/app/page.tsx"]).expected is False
    assert attribute_surface("control-nav", ["docs/readme.md"]).expected is False


def test_two_frame_filter_keeps_only_overlapping_diff_regions() -> None:
    first = [DiffRegion(0, 0, 10, 10, 100), DiffRegion(40, 40, 5, 5, 25)]
    second = [DiffRegion(2, 2, 10, 10, 90), DiffRegion(80, 80, 5, 5, 25)]

    result = classify_stable_diff(first, second, total_pixels=10_000)

    assert result.stable_regions == (DiffRegion(2, 2, 8, 8, 64),)
    assert result.changed_pixels == 64
    assert result.drifted is True


def test_two_frame_filter_ignores_one_frame_animation_and_threshold_noise() -> None:
    first = [DiffRegion(0, 0, 2, 2, 4)]
    second = [DiffRegion(50, 50, 2, 2, 4)]
    assert classify_stable_diff(first, second, total_pixels=100).drifted is False

    stable = [DiffRegion(0, 0, 10, 1, 10)]
    assert classify_stable_diff(stable, stable, total_pixels=10_000).drifted is False

    over = [DiffRegion(0, 0, 11, 1, 11)]
    assert classify_stable_diff(over, over, total_pixels=10_000).drifted is True


def test_escalation_structural_failure_is_immediate_p0() -> None:
    decision = decide_escalation(
        structural_failure_count=2,
        unexpected_surfaces=("home-composer",),
        previous_counts={},
        deployment_wave=True,
    )

    assert decision.severity == "P0"
    assert decision.next_counts == {"home-composer": 1}

    single = decide_escalation(
        structural_failure_count=1,
        unexpected_surfaces=(),
        previous_counts={},
        deployment_wave=False,
    )
    assert single.severity == "P0"
    assert single.next_counts == {}


def test_escalation_promotes_second_deployment_wave_but_not_daily_sentinel() -> None:
    first = decide_escalation(0, ("control-nav",), {}, deployment_wave=True)
    assert first.severity == "P2"
    assert first.next_counts == {"control-nav": 1}

    second = decide_escalation(0, ("control-nav",), first.next_counts, deployment_wave=True)
    assert second.severity == "P0"
    assert second.next_counts == {"control-nav": 2}

    daily = decide_escalation(0, ("control-nav",), second.next_counts, deployment_wave=False)
    assert daily.severity == "P2"
    assert daily.next_counts == second.next_counts


def test_escalation_resets_clean_surfaces_on_a_deployment_wave() -> None:
    decision = decide_escalation(0, (), {"control-nav": 2}, deployment_wave=True)
    assert decision.severity is None
    assert decision.next_counts == {}


def test_gateway_started_at_requires_the_public_health_contract() -> None:
    assert extract_gateway_started_at({"name": "gateway", "started_at": 123.5}) == 123.5
    assert extract_gateway_sha({"sha": "abc1234"}) == "abc1234"
    with pytest.raises(ValueError, match="Git commit"):
        extract_gateway_sha({"sha": None})


def test_unexpected_pixel_surfaces_exclude_missing_baselines() -> None:
    diffs: list[dict[str, object]] = [
        {"crop_surface": "home-header", "classification": "unexpected"},
        {"crop_surface": "control-nav", "classification": "baseline-missing"},
        {"crop_surface": "login-card", "classification": "expected"},
        {"crop_surface": "home-composer", "classification": "unexpected"},
    ]

    assert unexpected_pixel_surfaces(diffs) == ("home-composer", "home-header")


def test_pixel_capture_names_lock_the_static_crop_set() -> None:
    names = _expected_capture_names()
    assert len(names) == 44
    crops = {"-".join(name.split("-")[:2]) for name in names}
    assert crops == {
        "login-card",
        "home-sidebar",
        "home-header",
        "home-composer",
        "control-header",
        "control-nav",
    }
    assert all(not name.startswith("fleet") for name in names)
    assert all(not name.startswith("run-timeline") for name in names)


def test_wave_id_rejects_path_traversal() -> None:
    assert validate_wave_id("abc123-demo_1.2") == "abc123-demo_1.2"
    with pytest.raises(ValueError, match="wave SHA"):
        validate_wave_id("../outside")


def test_health_url_defaults_to_base_url_api_health() -> None:
    from scripts.post_deploy_visual_check import _parser

    args = _parser().parse_args(["--base-url", "http://gate.example:3000"])
    assert args.health_url is None
    args = _parser().parse_args(
        [
            "--base-url",
            "http://gate.example:3000",
            "--health-url",
            "http://gate.example:8000/api/health",
        ]
    )
    assert args.health_url == "http://gate.example:8000/api/health"


def test_demo_target_is_confined_to_the_local_preview_port_range() -> None:
    assert _validate_demo_target("http://host.docker.internal:3001") is None
    with pytest.raises(ValueError, match="demo container target"):
        _validate_demo_target("https://production.example:3001")


def test_cookie_mount_requires_a_regular_0600_file(tmp_path: Path) -> None:
    cookie = tmp_path / "cookie.txt"
    cookie.write_text("ava_session=value")
    with pytest.raises(PermissionError, match="0600"):
        _cookie_mount(cookie)

    cookie.chmod(0o600)
    mount, target = _cookie_mount(cookie)
    assert mount == f"{cookie}:/run/ava-visual-cookie:ro"
    assert target == "/run/ava-visual-cookie"


def test_container_recipe_is_pinned_and_passes_only_the_mounted_cookie_path(
    tmp_path: Path,
) -> None:
    cookie = tmp_path / "cookie.txt"
    cookie.write_text("ava_session=value")
    cookie.chmod(0o600)
    wave = tmp_path / "abc123"
    input_file = wave / "_input.json"
    args = argparse.Namespace(base_url="https://gateway.example", wave_sha="abc123")

    command = _container_command(
        args,
        output_root=tmp_path,
        input_file=input_file,
        cookie_file=cookie,
    )

    assert "mcr.microsoft.com/playwright/python:v1.59.0-noble" in command
    assert "--cookie-file" in command
    assert "/run/ava-visual-cookie" in command
    assert "-e" not in command


def test_accept_wave_requires_complete_captures_and_resets_counter(tmp_path: Path) -> None:
    wave = tmp_path / "abc123"
    captures = wave / "captures"
    captures.mkdir(parents=True)
    (wave / "probes.json").write_text(json.dumps({"structural_failures": [], "matrix": [{}] * 20}))
    for name in _expected_capture_names():
        (captures / name).write_bytes(name.encode())
    (tmp_path / "state.json").write_text(
        json.dumps({"unexpected_wave_counts": {"home-composer": 1}})
    )
    args = argparse.Namespace(output_root=tmp_path, accept_wave="abc123", accepted_by="qa")

    assert _accept_wave(args) == 0

    golden = tmp_path / "golden" / "abc123" / "captures"
    assert len(list(golden.glob("*-golden-[12].png"))) == 44
    state = json.loads((tmp_path / "state.json").read_text())
    assert state == {"golden_sha": "abc123", "unexpected_wave_counts": {}}
    audit = json.loads((tmp_path / "acceptance-audit.jsonl").read_text())
    assert audit["accepted_by"] == "qa"
    assert audit["wave_sha"] == "abc123"
    assert datetime.fromisoformat(audit["accepted_at"]).tzinfo is not None


def test_accept_wave_rejects_incomplete_matrix(tmp_path: Path) -> None:
    wave = tmp_path / "abc123"
    (wave / "captures").mkdir(parents=True)
    (wave / "probes.json").write_text(json.dumps({"structural_failures": [], "matrix": [{}] * 20}))
    args = argparse.Namespace(output_root=tmp_path, accept_wave="abc123", accepted_by="qa")

    with pytest.raises(RuntimeError, match="44 required captures"):
        _accept_wave(args)
