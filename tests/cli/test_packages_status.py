"""`ava packages status` — the read-only surface over registry schema v2.

Host version + channel line + per-package channel/policy/applied-rev/last
result/declared range, in text and `--json` (tasks #2915 / #3267).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cli.commands.packages import cmd_packages_status
from shared import install_registry as reg


def _register(name: str, **kw: object) -> reg.InstalledPackage:
    pkg = reg.InstalledPackage(name=name, type=kw.pop("type", "skill"), **kw)  # pyright: ignore[reportArgumentType]
    reg.register(pkg)
    return pkg


def test_empty_registry_prints_host_and_no_packages(
    unit_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cmd_packages_status() == 0
    out = capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    assert "registry v2" in out
    assert "(no tracked packages)" in out
    assert "channel core: not fetched yet" in out


def test_text_table_shows_resolved_policy_per_source_class(
    unit_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _register("repo-skill", origin="repo", origin_path="/x/ava_builtins/skills/repo-skill")
    _register("git-skill", origin="user", source="https://x/git-skill")
    _register("local-skill", origin="user")

    assert cmd_packages_status() == 0
    out = capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    lines = {ln.split()[0]: ln for ln in out.splitlines() if ln.startswith("  ") and "skill" in ln}
    assert (
        "core" in lines["repo-skill"]
        and "auto" in lines["repo-skill"]
        and "24h" in lines["repo-skill"]
    )
    assert "git" in lines["git-skill"] and "auto" in lines["git-skill"]
    assert "off" in lines["local-skill"]


def test_json_output_carries_host_channels_and_packages(
    unit_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _register("repo-skill", origin="repo", origin_path="/x/ava_builtins/skills/repo-skill")

    assert cmd_packages_status(json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)  # pyright: ignore[reportUnknownMemberType]
    assert payload["host"]["registry_schema"] == 2
    assert "+g" in payload["host"]["display"]
    assert payload["channels"] == {}
    (row,) = payload["packages"]
    assert row["name"] == "repo-skill"
    assert row["channel"] == "core"
    assert row["mode"] == "auto"
    assert row["interval_seconds"] == 86400
    assert row["applied_rev"] is None
    assert row["manifest"] is None


def test_invalid_manifest_reported_not_crashed(
    unit_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _register("broken", origin="repo", origin_path="/x/ava_builtins/skills/broken")
    manifest = unit_home / "skills" / "broken" / "ava-plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{ not json", encoding="utf-8")

    assert cmd_packages_status(json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)  # pyright: ignore[reportUnknownMemberType]
    (row,) = payload["packages"]
    assert row["manifest"] is None
    assert "not valid JSON" in row["manifest_error"]


def test_declared_range_displayed(unit_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _register("ranged", origin="repo", origin_path="/x/ava_builtins/skills/ranged")
    manifest = unit_home / "skills" / "ranged" / "ava-plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "apiVersion": 2,
                "name": "ranged",
                "version": "1.0.0",
                "requires_commit": "abcdef1234",
            }
        ),
        encoding="utf-8",
    )
    assert cmd_packages_status() == 0
    out = capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    assert "commit abcdef1" in out
