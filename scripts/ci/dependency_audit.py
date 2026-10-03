#!/usr/bin/env python
"""Weekly dependency report: locked-dependency vulnerabilities and pinned-binary freshness.

Run by `.github/workflows/dependency-audit.yml` (weekly + manual). Three sections land in one
markdown report, kept in ONE tracked GitHub issue (found by its marker, updated in place, closed
when a run comes back clean):

- Python: `uv audit` over `uv.lock`. Its JSON carries no severity, so each advisory is looked up
  in OSV (GitHub's reviewed severity); an advisory OSV cannot grade is listed as `unknown` and
  counts as actionable rather than being dropped.
- Frontend: `npm audit` over `ui/web/package-lock.json`.
- Binaries: the version constants Ava downloads or installs outside the lockfiles (zonky Postgres,
  pgvector, wal-g, otelcol-contrib, uv, Grafana) against their upstream's newest release.

The issue is actionable when any vulnerability is high/critical/unknown or any pinned binary is
behind. The report never changes a pin: moving one is a dependency upgrade that needs approval.

Usage: `dependency_audit.py --out REPORT.md [--sync-issue]` (`GH_TOKEN` and `GITHUB_REPOSITORY`
for the GitHub calls).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MARKER = "<!-- weekly-dependency-audit -->"
_TITLE = "Weekly dependency audit: findings need attention"
_UV_AUDIT_VERSION = "0.11.16"  # first uv with `audit`; the CI uv (0.10.2) predates it
_SEVERITY_RANK = {"critical": 4, "high": 3, "moderate": 2, "low": 1, "unknown": 0}
_ACTIONABLE_SEVERITIES = frozenset({"critical", "high", "unknown"})

Fetch = Callable[[str], Any]  # decoded JSON: the shape is each upstream's, read by key


@dataclass(frozen=True)
class Vulnerability:
    ecosystem: str
    package: str
    version: str
    advisory: str
    severity: str
    fix: str
    summary: str


@dataclass(frozen=True)
class Binary:
    name: str
    pinned: str
    latest: str
    source: str

    @property
    def behind(self) -> bool:
        return _version_key(self.latest) > _version_key(self.pinned)


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version.lstrip("v")))


def _fetch_json(url: str) -> Any:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})  # noqa: S310 — fixed https upstream URLs
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        return json.load(response)


def _fetch_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310 — fixed https upstream URLs
        return response.read().decode()


def _gh_api(path: str) -> Any:
    result = subprocess.run(  # noqa: S603 — fixed gh invocation, path is a repo-relative API route
        ["gh", "api", path],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return json.loads(result.stdout)


# ── vulnerabilities ──────────────────────────────────────────────────────────


def osv_severity(advisory_ids: list[str], fetch: Fetch = _fetch_json) -> str:
    """GitHub's reviewed severity for the first id OSV can grade, else `unknown`."""
    for advisory_id in advisory_ids:
        try:
            record = fetch(f"https://api.osv.dev/v1/vulns/{advisory_id}")
        except OSError:
            continue
        if not isinstance(record, dict):
            continue
        record_json = cast("dict[str, Any]", record)
        specific = cast("dict[str, Any]", record_json.get("database_specific") or {})
        severity = specific.get("severity")
        if isinstance(severity, str) and severity.lower() in _SEVERITY_RANK:
            return severity.lower()
    return "unknown"


def python_vulnerabilities(audit_json: dict[str, Any], fetch: Fetch) -> list[Vulnerability]:
    found: list[Vulnerability] = []
    for entry in audit_json["vulnerabilities"]:
        dependency = entry["dependency"]
        ids = [entry["id"], *(a for a in entry.get("aliases", []) if a.startswith("GHSA-"))]
        found.append(
            Vulnerability(
                ecosystem="python",
                package=dependency["name"],
                version=dependency["version"],
                advisory=entry["display_id"],
                severity=osv_severity(ids, fetch),
                fix=", ".join(entry.get("fix_versions") or []) or "none",
                summary=entry["summary"],
            )
        )
    return found


def npm_vulnerabilities(audit_json: dict[str, Any]) -> list[Vulnerability]:
    found: list[Vulnerability] = []
    for name, entry in audit_json["vulnerabilities"].items():
        titles: list[str] = [via["title"] for via in entry["via"] if isinstance(via, dict)]
        fix = entry["fixAvailable"]
        if isinstance(fix, dict):
            fix_text = f"{fix['name']}@{fix['version']}" + (
                " (major)" if fix["isSemVerMajor"] else ""
            )
        else:
            fix_text = "available" if fix else "none"
        found.append(
            Vulnerability(
                ecosystem="npm",
                package=name,
                version=entry["range"],
                advisory="direct" if entry["isDirect"] else "transitive",
                severity=entry["severity"],
                fix=fix_text,
                summary="; ".join(titles) or "via " + ", ".join(map(str, entry["via"])),
            )
        )
    return found


def _run_json(argv: list[str], cwd: Path) -> dict[str, Any]:
    """Run an audit tool that exits non-zero when it finds something: only unparsable
    output is a failure of the tool itself."""
    result = subprocess.run(  # noqa: S603 — fixed audit tool argv
        argv, capture_output=True, text=True, cwd=cwd, timeout=600, check=False
    )
    try:
        parsed: Any = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"{argv[0]} produced no JSON (rc={result.returncode}): {result.stderr[-400:]}"
        ) from exc
    if not isinstance(parsed, dict):
        raise TypeError(f"{argv[0]} JSON is not an object")
    return cast("dict[str, Any]", parsed)


# ── pinned binaries ──────────────────────────────────────────────────────────

# (display name, file, regex with one group for the pinned version). A constant that is
# renamed or moved makes `pinned_version` raise, so a stale entry cannot silently drop a row.
_PINS: tuple[tuple[str, str, str], ...] = (
    ("zonky Postgres", "base/cluster/dataplane/runtime_binaries.py", r'^_PG_VERSION = "([^"]+)"'),
    ("pgvector", "base/cluster/dataplane/runtime_binaries.py", r'^_PGVECTOR_VERSION = "([^"]+)"'),
    ("wal-g", "base/cluster/dataplane/walg_binary.py", r'^WALG_VERSION = "([^"]+)"'),
    (
        "otelcol-contrib",
        "base/deploy/release/collector_artifact.py",
        r'^OTELCOL_CONTRIB_VERSION = "([^"]+)"',
    ),
    ("uv", "base/host/brew_pin.py", r'^UV_VERSION = "([^"]+)"'),
    ("Grafana", "deploy/lgtm/docker-compose.yml", r"^\s*image: grafana/grafana:(\S+)$"),
)


def pinned_version(path: str, pattern: str, root: Path = _REPO_ROOT) -> str:
    matches = re.findall(pattern, (root / path).read_text(encoding="utf-8"), re.MULTILINE)
    if len(matches) != 1:
        raise RuntimeError(f"{path}: expected exactly one match for {pattern!r}, found {matches}")
    return matches[0]


def zonky_latest(pinned: str, metadata_xml: str) -> str:
    """Newest zonky release on the pinned Postgres major: a major bump is an expand step, never an
    in-place swap, so it is not what this row compares."""
    major = pinned.split(".", 1)[0]
    versions = re.findall(r"<version>([^<]+)</version>", metadata_xml)
    same_major = [v for v in versions if v.split(".", 1)[0] == major and re.fullmatch(r"[\d.]+", v)]
    return max(same_major, key=_version_key)


def latest_binaries(
    gh_api: Fetch, fetch_text: Callable[[str], str], root: Path = _REPO_ROOT
) -> list[Binary]:
    pins = {name: pinned_version(path, pattern, root) for name, path, pattern in _PINS}

    def release(repo: str) -> str:
        data = gh_api(f"repos/{repo}/releases/latest")
        return str(data["tag_name"])

    # pgvector publishes tags, not GitHub releases.
    tags = gh_api("repos/pgvector/pgvector/tags?per_page=100")
    pgvector_tags = [t["name"] for t in tags if re.fullmatch(r"v\d+\.\d+\.\d+", t["name"])]
    maven = "https://repo1.maven.org/maven2/io/zonky/test/postgres/embedded-postgres-binaries-linux-amd64"
    return [
        Binary(
            "zonky Postgres",
            pins["zonky Postgres"],
            zonky_latest(pins["zonky Postgres"], fetch_text(f"{maven}/maven-metadata.xml")),
            "maven central (same major)",
        ),
        Binary(
            "pgvector",
            pins["pgvector"],
            max(pgvector_tags, key=_version_key),
            "pgvector/pgvector tags",
        ),
        Binary("wal-g", pins["wal-g"], release("wal-g/wal-g"), "wal-g/wal-g releases"),
        Binary(
            "otelcol-contrib",
            pins["otelcol-contrib"],
            release("open-telemetry/opentelemetry-collector-releases"),
            "opentelemetry-collector-releases",
        ),
        Binary("uv", pins["uv"], release("astral-sh/uv"), "astral-sh/uv releases"),
        Binary("Grafana", pins["Grafana"], release("grafana/grafana"), "grafana/grafana releases"),
    ]


# ── report + issue ───────────────────────────────────────────────────────────


def is_actionable(vulnerabilities: list[Vulnerability], binaries: list[Binary]) -> bool:
    return any(v.severity in _ACTIONABLE_SEVERITIES for v in vulnerabilities) or any(
        b.behind for b in binaries
    )


def render(vulnerabilities: list[Vulnerability], binaries: list[Binary]) -> str:
    lines = [
        _MARKER,
        "",
        "Weekly dependency audit (`scripts/ci/dependency_audit.py`). No pin was changed.",
        "",
    ]
    for ecosystem, heading in (
        ("python", "Python (uv audit + OSV severity)"),
        ("npm", "Frontend (npm audit)"),
    ):
        rows = sorted(
            (v for v in vulnerabilities if v.ecosystem == ecosystem),
            key=lambda v: -_SEVERITY_RANK[v.severity],
        )
        counts = {s: sum(1 for v in rows if v.severity == s) for s in _SEVERITY_RANK}
        summary = ", ".join(f"{n} {s}" for s, n in counts.items() if n) or "none"
        lines += [f"## {heading}", "", f"Findings: {summary}.", ""]
        if rows:
            lines += [
                "| Severity | Package | Version | Advisory | Fix | Summary |",
                "|---|---|---|---|---|---|",
            ]
            lines += [
                f"| {v.severity} | {v.package} | {v.version} | {v.advisory} | {v.fix} | {_cell(v.summary)} |"
                for v in rows
            ]
            lines.append("")
    lines += [
        "## Pinned binaries",
        "",
        "| Binary | Pinned | Latest | Source | Status |",
        "|---|---|---|---|---|",
    ]
    lines += [
        f"| {b.name} | {b.pinned} | {b.latest} | {b.source} | {'behind' if b.behind else 'current'} |"
        for b in binaries
    ]
    lines.append("")
    return "\n".join(lines)


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")[:200]


def _gh(argv: list[str]) -> str:
    return subprocess.run(  # noqa: S603 — fixed gh invocation
        ["gh", *argv],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout


def sync_issue(
    repo: str, body: str, *, actionable: bool, gh: Callable[[list[str]], str] = _gh
) -> str:
    """Keep the single tracked issue: update or open it when actionable, close it when clean."""
    marked = f'select((.body // "") | contains("{_MARKER}"))'
    listing = gh(
        [
            "api",
            "--method",
            "GET",
            f"repos/{repo}/issues",
            "-f",
            "state=open",
            "-f",
            "per_page=100",
            "--paginate",
            "--jq",
            f'.[] | select(has("pull_request") | not) | {marked} | .number',
        ]
    )
    open_issues = listing.split()
    if actionable:
        if open_issues:
            gh(
                [
                    "api",
                    "--method",
                    "PATCH",
                    f"repos/{repo}/issues/{open_issues[0]}",
                    "-f",
                    f"body={body}",
                ]
            )
            return f"updated #{open_issues[0]}"
        created = gh(["issue", "create", "--repo", repo, "--title", _TITLE, "--body", body])
        return f"opened {created.strip()}"
    for number in open_issues:
        gh(
            [
                "api",
                "--method",
                "POST",
                f"repos/{repo}/issues/{number}/comments",
                "-f",
                "body=The weekly audit came back clean.",
            ]
        )
        gh(
            [
                "api",
                "--method",
                "PATCH",
                f"repos/{repo}/issues/{number}",
                "-f",
                "state=closed",
                "-f",
                "state_reason=completed",
            ]
        )
    return f"closed {len(open_issues)} issue(s)"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--sync-issue", metavar="OWNER/REPO", help="update the tracked issue in this repo"
    )
    args = parser.parse_args(argv)

    uv_json = _run_json(
        [
            "uvx",
            "--from",
            f"uv=={_UV_AUDIT_VERSION}",
            "uv",
            "audit",
            "--frozen",
            "--output-format",
            "json",
        ],
        _REPO_ROOT,
    )
    npm_json = _run_json(["npm", "audit", "--json"], _REPO_ROOT / "ui/web")
    vulnerabilities = [
        *python_vulnerabilities(uv_json, _fetch_json),
        *npm_vulnerabilities(npm_json),
    ]
    binaries = latest_binaries(_gh_api, _fetch_text)

    report = render(vulnerabilities, binaries)
    args.out.write_text(report, encoding="utf-8")
    actionable = is_actionable(vulnerabilities, binaries)
    print(
        f"actionable={actionable} vulnerabilities={len(vulnerabilities)} behind={sum(b.behind for b in binaries)}"
    )
    if args.sync_issue:
        print(sync_issue(args.sync_issue, report, actionable=actionable))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
