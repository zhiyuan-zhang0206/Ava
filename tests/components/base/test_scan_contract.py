from pathlib import Path

from base.packages.skills import scan


def test_meiva_own_skills_carry_no_critical_findings() -> None:
    repo = Path(__file__).resolve().parents[3]
    packages = [
        d
        for base in ("meiva_builtins/skills", "meiva_builtins/plugins")
        for d in (repo / base).rglob("*")
        if d.is_dir() and (d / "SKILL.md").is_file()
    ]
    assert packages, "expected first-party skill packages to scan"
    offenders = {
        d.relative_to(repo).as_posix(): scan.criticals(scan.scan_package(d)) for d in packages
    }
    assert {k: v for k, v in offenders.items() if v} == {}
