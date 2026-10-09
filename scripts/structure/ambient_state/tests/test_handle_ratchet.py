"""Every detected library-built handle fails without a stored allowance."""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest

from scripts.structure.ambient_state import busrule, dbhandle
from scripts.structure.ambient_state import handle_ratchet as ratchet


def test_a_dial_is_counted_whichever_package_it_is_in() -> None:
    tree = ast.parse(
        textwrap.dedent(
            """
            from base.db import Database, connect

            def f():
                connect()
                return Database.from_settings()
            """
        )
    )
    names = sorted(hit.name for hit in dbhandle.dials(tree))
    assert names == ["Database.from_settings", "connect"]
    assert [ratchet.kind_of(n) for n in names] == [ratchet.SELF_BUILT, ratchet.SHIM]


def test_a_self_built_bus_is_counted_whichever_package_it_is_in() -> None:
    tree = ast.parse(
        "from base.events.live.bus import EventBus\n\nbus = EventBus.from_settings()\n"
    )
    assert [hit.line for hit in busrule.builds(tree)] == [3]


def test_package_is_the_first_two_components() -> None:
    assert ratchet.package_of("base/agents/x/y.py") == "base/agents"
    assert ratchet.package_of("base/agents/x.py") == "base/agents"
    assert ratchet.package_of("ops/x.py") == "ops"


def test_every_detected_handle_fails_with_its_site() -> None:
    sites = {
        ("base/example", ratchet.SHIM): ["base/example/a.py:3", "base/example/b.py:9"],
        ("base/example", ratchet.SELF_BUILT): ["base/example/c.py:5"],
        ("base/example", ratchet.BUS_BUILT): ["base/example/d.py:7"],
    }
    problems = ratchet.errors(sites)
    assert {p.split(":")[0] for p in problems} == {
        "base/example/a.py",
        "base/example/b.py",
        "base/example/c.py",
        "base/example/d.py",
    }
    assert len(problems) == 4
    assert all("composition root" in problem for problem in problems)


def test_zero_sites_passes() -> None:
    assert ratchet.errors({}) == []


@pytest.mark.parametrize(
    "args", [["--write"], ["--baseline", "permit.json"], ["--allow", "base/example"]]
)
def test_cli_rejects_write_modes_and_new_exemptions(
    args: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert ratchet.main(args) == 1
    assert "accepts no arguments" in capsys.readouterr().err


def test_scan_rejects_sites_even_when_an_old_baseline_file_permits_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "base/example/handles.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "from base.db import Database, connect\n"
        "from base.events.live.bus import EventBus\n"
        "connect()\nDatabase.from_settings()\nEventBus.from_settings()\n"
    )
    old_baseline = tmp_path / "scripts/structure/ambient_state/handle_ratchet_baseline.json"
    old_baseline.parent.mkdir(parents=True)
    old_baseline.write_text('{"base/example": {"shim": 100, "self-built": 100, "bus-built": 100}}')
    monkeypatch.setattr(ratchet, "_REPO_ROOT", tmp_path)

    def tracked_files(_root: Path) -> list[str]:
        return ["base/example/handles.py"]

    monkeypatch.setattr(ratchet.lint_common, "tracked_files", tracked_files)
    assert ratchet.main([]) == 1
    errors = capsys.readouterr().err
    assert "base/example/handles.py:3" in errors
    assert "base/example/handles.py:4" in errors
    assert "base/example/handles.py:5" in errors
    assert old_baseline.read_text().startswith('{"base/example"')


def test_cli_passes_empty_scan_without_a_baseline_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ratchet, "_REPO_ROOT", tmp_path)

    def tracked_files(_root: Path) -> list[str]:
        return []

    monkeypatch.setattr(ratchet.lint_common, "tracked_files", tracked_files)
    assert ratchet.main([]) == 0
    assert list(tmp_path.iterdir()) == []
