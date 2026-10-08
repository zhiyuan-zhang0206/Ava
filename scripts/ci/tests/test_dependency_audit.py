"""scripts/ci/dependency_audit.py: severity grading, pin discovery, report and issue upkeep.

The audit tools and upstream registries are real network calls, exercised by the weekly workflow;
here they are replaced by recorded shapes.
"""

from __future__ import annotations

import urllib.error
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from scripts.ci import dependency_audit as audit


@pytest.mark.parametrize("exit_code", [0, 1])
def test_audit_accepts_complete_findings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, exit_code: int
) -> None:
    def run(*args: object, **kwargs: object) -> CompletedProcess[str]:
        return CompletedProcess(["npm", "audit"], exit_code, '{"vulnerabilities": {}}', "")

    monkeypatch.setattr(audit.subprocess, "run", run)
    assert audit._run_json(["npm", "audit"], tmp_path) == {"vulnerabilities": {}}


@pytest.mark.parametrize(
    ("exit_code", "stdout"),
    [(1, '{"error": {"code": "ENOAUDIT"}}'), (2, '{"vulnerabilities": {}}')],
)
def test_audit_rejects_registry_and_tool_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, exit_code: int, stdout: str
) -> None:
    def run(*args: object, **kwargs: object) -> CompletedProcess[str]:
        return CompletedProcess(["npm", "audit"], exit_code, stdout, "private registry details")

    monkeypatch.setattr(audit.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="npm reported an audit error") as error:
        audit._run_json(["npm", "audit"], tmp_path)
    assert "private registry details" not in str(error.value)


def _osv(severity: str | None):
    def fetch(url: str):
        if url.endswith("PYSEC-1"):
            raise urllib.error.URLError("no record")
        return {"database_specific": {"severity": severity} if severity else {}}

    return fetch


def test_severity_comes_from_the_first_id_osv_can_grade() -> None:
    assert audit.osv_severity(["PYSEC-1", "GHSA-a"], _osv("HIGH")) == "high"


def test_an_ungradable_advisory_is_unknown_not_dropped() -> None:
    assert audit.osv_severity(["GHSA-a"], _osv(None)) == "unknown"
    assert audit.osv_severity(["PYSEC-1"], _osv("HIGH")) == "unknown"


def test_python_findings_carry_osv_severity_and_fix_versions() -> None:
    uv_json = {
        "vulnerabilities": [
            {
                "dependency": {"name": "pyjwt", "version": "2.14.0"},
                "id": "PYSEC-1",
                "display_id": "PYSEC-1",
                "aliases": ["GHSA-a", "CVE-1"],
                "summary": "DoS",
                "fix_versions": ["2.15.0"],
            }
        ]
    }
    (found,) = audit.python_vulnerabilities(uv_json, _osv("CRITICAL"))
    assert (found.package, found.severity, found.fix) == ("pyjwt", "critical", "2.15.0")


def test_npm_findings_flatten_the_package_map() -> None:
    npm_json = {
        "vulnerabilities": {
            "brace-expansion": {
                "severity": "high",
                "isDirect": False,
                "range": "<1.1.21",
                "via": [{"title": "CPU denial of service"}],
                "fixAvailable": True,
            },
            "next": {
                "severity": "moderate",
                "isDirect": True,
                "range": "<16",
                "via": ["postcss"],
                "fixAvailable": {"name": "next", "version": "16.2.0", "isSemVerMajor": True},
            },
        }
    }
    brace, nxt = audit.npm_vulnerabilities(npm_json)
    assert (brace.severity, brace.advisory, brace.fix) == ("high", "transitive", "available")
    assert (nxt.fix, nxt.summary) == ("next@16.2.0 (major)", "via postcss")


def test_every_pinned_binary_constant_is_found_in_the_tree() -> None:
    for name, path, pattern in audit._PINS:
        assert audit.pinned_version(path, pattern), name


def test_zonky_latest_stays_on_the_pinned_major() -> None:
    metadata = "".join(f"<version>{v}</version>" for v in ("17.4.0", "17.11.0", "17.9.0", "18.6.0"))
    assert audit.zonky_latest("17.4.0", metadata) == "17.11.0"


def _binary(pinned: str, latest: str) -> audit.Binary:
    return audit.Binary("x", pinned, latest, "src")


def test_behind_compares_numerically_and_ignores_the_v_prefix() -> None:
    assert _binary("0.9.0", "v0.10.0").behind
    assert not _binary("v3.0.9", "v3.0.9").behind


def _vuln(severity: str) -> audit.Vulnerability:
    return audit.Vulnerability("python", "p", "1", "GHSA-x", severity, "2", "s")


def test_issue_is_actionable_on_high_unknown_or_a_behind_binary_only() -> None:
    current = [_binary("1.0", "1.0")]
    assert not audit.is_actionable([_vuln("moderate"), _vuln("low")], current)
    assert audit.is_actionable([_vuln("high")], current)
    assert audit.is_actionable([_vuln("unknown")], current)
    assert audit.is_actionable([], [_binary("1.0", "1.1")])


def test_report_orders_by_severity_and_carries_the_marker() -> None:
    report = audit.render([_vuln("low"), _vuln("critical")], [_binary("1.0", "1.1")])
    assert report.startswith(audit._MARKER)
    assert report.index("| critical |") < report.index("| low |")
    assert "| x | 1.0 | 1.1 | src | behind |" in report


class _FakeGh:
    def __init__(self, open_numbers: list[int]) -> None:
        self.calls: list[list[str]] = []
        self._open = open_numbers

    def __call__(self, argv: list[str]) -> str:
        self.calls.append(argv)
        if "--paginate" in argv:
            return "\n".join(map(str, self._open))
        if argv[:2] == ["issue", "create"]:
            return "https://github.com/o/r/issues/9\n"
        return ""


def test_actionable_run_updates_the_existing_issue_instead_of_opening_another() -> None:
    gh = _FakeGh([4])
    assert audit.sync_issue("o/r", "body", actionable=True, gh=gh) == "updated #4"
    assert not any(c[:2] == ["issue", "create"] for c in gh.calls)
    assert gh.calls[-1][:4] == ["api", "--method", "PATCH", "repos/o/r/issues/4"]


def test_actionable_run_with_no_issue_opens_one() -> None:
    gh = _FakeGh([])
    assert audit.sync_issue("o/r", "body", actionable=True, gh=gh).startswith("opened ")


def test_clean_run_closes_the_open_issue_and_a_clean_repo_does_nothing() -> None:
    gh = _FakeGh([4])
    assert audit.sync_issue("o/r", "body", actionable=False, gh=gh) == "closed 1 issue(s)"
    assert any("state=closed" in c for c in gh.calls)
    quiet = _FakeGh([])
    assert audit.sync_issue("o/r", "body", actionable=False, gh=quiet) == "closed 0 issue(s)"
    assert len(quiet.calls) == 1


def test_issue_lookup_filters_on_the_marker_and_skips_pull_requests() -> None:
    gh = _FakeGh([])
    audit.sync_issue("o/r", "body", actionable=False, gh=gh)
    jq = gh.calls[0][gh.calls[0].index("--jq") + 1]
    assert audit._MARKER in jq and 'has("pull_request") | not' in jq
