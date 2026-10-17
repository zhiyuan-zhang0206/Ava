#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

DEFAULT_RULES = Path(__file__).with_name("panel_jargon_rules.toml")
DEFAULT_PANEL_NAME = "to_user.md"
_FULLWIDTH_COLON = "\uff1a"
_DASH_CELLS = {"-", ":", " "}


@dataclass(frozen=True)
class Rule:
    id: str
    description: str
    pattern: re.Pattern[str]
    hard: bool


@dataclass(frozen=True)
class Ruleset:
    evidence_headings: tuple[str, ...]
    glossary_headings: tuple[str, ...]
    glossary_table_headers: tuple[str, ...]
    max_body_findings: int
    max_glossary_entries: int
    allowed_acronyms: tuple[str, ...]
    rules: tuple[Rule, ...]


@dataclass(frozen=True)
class Finding:
    line: int
    rule: str
    detail: str
    hard: bool


# meiva:link scripts/content_lint/docs/panel-jargon.meiva.okf.md
def load_ruleset(path: Path | None = None) -> Ruleset:
    source = path or DEFAULT_RULES
    data = tomllib.loads(source.read_text(encoding="utf-8"))
    panel = data["panel"]
    rules = [
        Rule(
            id=entry["id"],
            description=entry["description"],
            pattern=re.compile(entry["pattern"]),
            hard=bool(entry.get("hard", False)),
        )
        for entry in data["rule"]
    ]
    codewords = tuple(data.get("codeword", ()))
    if codewords:
        alternatives = "|".join(
            re.escape(word) for word in sorted(codewords, key=len, reverse=True)
        )
        rules.append(
            Rule(
                id="codeword",
                description="internal batch codeword",
                pattern=re.compile(rf"(?<![A-Za-z0-9])(?:{alternatives})(?![A-Za-z0-9])"),
                hard=True,
            )
        )
    return Ruleset(
        evidence_headings=tuple(panel["evidence_headings"]),
        glossary_headings=tuple(panel["glossary_headings"]),
        glossary_table_headers=tuple(
            header.lower() for header in panel.get("glossary_table_headers", ())
        ),
        max_body_findings=int(panel["max_body_findings"]),
        max_glossary_entries=int(panel["max_glossary_entries"]),
        allowed_acronyms=tuple(acronym.upper() for acronym in panel.get("allowed_acronyms", ())),
        rules=tuple(rules),
    )


def _heading_text(line: str) -> str | None:
    stripped = re.sub(r"^[-*+]\s+", "", line.strip())
    if stripped.startswith("#"):
        return stripped.lstrip("#").strip()
    if stripped.startswith("**") and stripped.endswith((_FULLWIDTH_COLON, ":")):
        return stripped.strip("*" + _FULLWIDTH_COLON + ":").strip()
    return None


# meiva:link scripts/content_lint/docs/panel-jargon.meiva.okf.md
def split_body(text: str, ruleset: Ruleset) -> tuple[list[str], list[str], list[str]]:
    lines = text.splitlines()
    body: list[str] = []
    evidence: list[str] = []
    glossary: list[str] = []
    section = "body"
    for line in lines:
        heading = _heading_text(line)
        if heading is not None:
            if any(marker in heading for marker in ruleset.glossary_headings):
                section = "glossary"
            elif any(marker in heading for marker in ruleset.evidence_headings):
                section = "evidence"
            elif section == "glossary":
                section = "body"
        if section == "body":
            body.append(line)
        elif section == "glossary":
            glossary.append(line)
        else:
            evidence.append(line)
    return body, evidence, glossary


def _glossary_terms(lines: Sequence[str], ruleset: Ruleset) -> set[str]:
    terms: set[str] = set()
    for line in lines:
        if _heading_text(line) is not None:
            continue
        for match in re.finditer(r"\*\*([^*]{1,40})\*\*", line):
            terms.add(match.group(1).strip())
        row = line.strip()
        if not row.startswith("|"):
            continue
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        if not cells or not cells[0] or set(cells[0]) <= _DASH_CELLS:
            continue
        if cells[0].lower() in ruleset.glossary_table_headers:
            continue
        terms.add(cells[0].strip("` "))
    return {term.strip("` ") for term in terms if term.strip("` ")}


def _acronym_findings(
    body_lines: Sequence[str],
    glossary_terms: set[str],
    allowed: tuple[str, ...],
) -> list[Finding]:
    findings: list[Finding] = []
    known = {term.upper() for term in glossary_terms} | {word.upper() for word in allowed}
    pattern = re.compile(r"(?<![A-Za-z0-9])([A-Z]{2,6})(?![A-Za-z0-9])")
    for number, line in enumerate(body_lines, start=1):
        for match in pattern.finditer(line):
            token = match.group(1)
            if token in known:
                continue
            findings.append(
                Finding(line=number, rule="undefined-acronym", detail=token, hard=False)
            )
    return findings


# meiva:link scripts/content_lint/docs/panel-jargon.meiva.okf.md
def scan_panel(path: Path, ruleset: Ruleset) -> tuple[list[Finding], int]:
    body, _evidence, glossary = split_body(path.read_text(encoding="utf-8"), ruleset)
    terms = _glossary_terms(glossary, ruleset)
    findings: list[Finding] = []
    for rule in ruleset.rules:
        for number, line in enumerate(body, start=1):
            for match in rule.pattern.finditer(line):
                findings.append(
                    Finding(
                        line=number,
                        rule=rule.id,
                        detail=match.group(0).strip(),
                        hard=rule.hard,
                    )
                )
    findings.extend(_acronym_findings(body, terms, ruleset.allowed_acronyms))
    return sorted(findings, key=lambda finding: (finding.line, finding.rule)), len(terms)


def panel_paths(targets: Sequence[Path], name: str) -> list[Path]:
    found: list[Path] = []
    for target in targets:
        if target.is_dir():
            found.extend(sorted(path for path in target.rglob(name) if path.is_file()))
        elif target.is_file():
            found.append(target)
        else:
            raise FileNotFoundError(f"no such panel or directory: {target}")
    return found


# meiva:link scripts/content_lint/docs/panel-jargon.meiva.okf.md
def evaluate(path: Path, ruleset: Ruleset) -> tuple[list[Finding], int, bool]:
    findings, entries = scan_panel(path, ruleset)
    hard = any(finding.hard for finding in findings)
    passed = (
        not hard
        and len(findings) <= ruleset.max_body_findings
        and entries <= ruleset.max_glossary_entries
    )
    return findings, entries, passed


# meiva:link scripts/content_lint/docs/panel-jargon.meiva.okf.md
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report internal identifiers in the body of a user-facing panel."
    )
    parser.add_argument("targets", nargs="+", type=Path, help="panel files or directories")
    parser.add_argument(
        "--rules", type=Path, default=None, help="rule file (default: beside this script)"
    )
    parser.add_argument(
        "--name", default=DEFAULT_PANEL_NAME, help="panel file name inside directories"
    )
    parser.add_argument("--json", action="store_true", help="print one JSON document instead")
    args = parser.parse_args(argv)

    ruleset = load_ruleset(args.rules)
    results = []
    failed = 0
    for path in panel_paths(args.targets, args.name):
        findings, entries, passed = evaluate(path, ruleset)
        failed += 0 if passed else 1
        results.append(
            {
                "panel": str(path),
                "glossary_entries": entries,
                "findings": [finding.__dict__ for finding in findings],
                "passed": passed,
            }
        )

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=1))
    else:
        for result in results:
            for finding in result["findings"]:
                suffix = " [hard]" if finding["hard"] else ""
                print(
                    f"{result['panel']}:{finding['line']}: {finding['rule']}: "
                    f"{finding['detail']}{suffix}"
                )
            state = "ok" if result["passed"] else "FAIL"
            print(
                f"{result['panel']}: {state} - {len(result['findings'])} body finding(s), "
                f"{result['glossary_entries']} glossary entr(ies); budget "
                f"{ruleset.max_body_findings} finding(s), {ruleset.max_glossary_entries} entr(ies)"
            )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
