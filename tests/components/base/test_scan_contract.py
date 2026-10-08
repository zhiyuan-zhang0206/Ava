"""Contract: scans every first-party skill of the repository for critical findings."""

from pathlib import Path

from base.packages.skills import scan


def test_ava_own_skills_carry_no_critical_findings() -> None:
    """The whole first-party skill library is the standing false-positive test:
    a rule that fires here is tuned wrong, not catching something."""
    repo = Path(__file__).resolve().parents[3]
    packages = [
        d
        for base in ("ava_builtins/skills", "ava_builtins/plugins")
        for d in (repo / base).rglob("*")
        if d.is_dir() and (d / "SKILL.md").is_file()
    ]
    assert packages, "expected first-party skill packages to scan"
    offenders = {
        d.relative_to(repo).as_posix(): scan.criticals(scan.scan_package(d)) for d in packages
    }
    assert {k: v for k, v in offenders.items() if v} == {}
