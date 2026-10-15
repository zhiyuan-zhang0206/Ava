import re
import tomllib
from pathlib import PurePosixPath
from typing import cast
from urllib.parse import urlsplit

from scripts.content_lint.code_comments.model import Contract, Finding, Kind

IDENTIFIER = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")
FENCE = re.compile(r"^( {0,3})(`{3,}|~{3,})([^\r\n]*)$")
COMMON_FIELDS = frozenset({"id", "kind", "statement", "code", "tests"})
KIND_FIELDS: dict[Kind, frozenset[str]] = {
    Kind.INVARIANT: frozenset(),
    Kind.EXTERNAL: frozenset({"source", "applies_to"}),
    Kind.DECISION: frozenset({"decision"}),
    Kind.API: frozenset({"api"}),
}


def canonical_path(value: str) -> bool:
    path = PurePosixPath(value)
    return (
        bool(value)
        and not path.is_absolute()
        and ".." not in path.parts
        and str(path) == value
        and "\\" not in value
    )


def text_field(data: dict[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a nonempty string")
    return value


def list_field(data: dict[str, object], key: str, *, required: bool = True) -> tuple[str, ...]:
    value = data.get(key, [])
    if not isinstance(value, list):
        raise TypeError(f"{key} must be an array of nonempty strings")
    items: list[str] = []
    for item in cast(list[object], value):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{key} must be an array of nonempty strings")
        items.append(item)
    if required and not items:
        raise ValueError(f"{key} must not be empty")
    if len(set(items)) != len(items):
        raise ValueError(f"{key} contains duplicates")
    return tuple(items)


def validate_owner(path: str) -> None:
    if not path.endswith(".ava.okf.md") or "docs" not in PurePosixPath(path).parts[:-1]:
        raise ValueError("contracts belong in a component's docs/*.ava.okf.md")
    if path.startswith(("docs/decisions/", "docs/postmortems/", "future/")):
        raise ValueError("historical records and plans cannot own current contracts")


def validate_external_source(source: str) -> None:
    url = urlsplit(source)
    if url.scheme != "https" or not url.netloc or url.username or url.password:
        raise ValueError("external source must be an HTTPS URL without credentials")


def make_contract(path: str, line: int, body: str) -> Contract:
    validate_owner(path)
    data = tomllib.loads(body)
    kind = Kind(text_field(data, "kind"))
    unknown = data.keys() - COMMON_FIELDS - KIND_FIELDS[kind]
    if unknown:
        raise ValueError(f"unknown fields: {', '.join(sorted(unknown))}")
    identifier = text_field(data, "id")
    if IDENTIFIER.fullmatch(identifier) is None:
        raise ValueError("id must use lowercase words separated by hyphens")
    statement = text_field(data, "statement").strip()
    code = list_field(data, "code")
    tests = list_field(data, "tests", required=kind != Kind.DECISION)
    extra = {key: text_field(data, key) for key in KIND_FIELDS[kind]}
    if kind == Kind.EXTERNAL:
        validate_external_source(extra["source"])
    if kind == Kind.DECISION:
        decision = extra["decision"]
        if (
            not canonical_path(decision)
            or not decision.startswith("docs/decisions/")
            or not decision.endswith(".md")
        ):
            raise ValueError("decision must name an exact docs/decisions/*.md record")
    if kind == Kind.API and len(code) != 1:
        raise ValueError("api contracts bind exactly one definition")
    return Contract(path, line, identifier, kind, statement, code, tests, **extra)


def closes_fence(match: re.Match[str] | None, fence: str) -> bool:
    return bool(
        match and match[2][0] == fence[0] and len(match[2]) >= len(fence) and not match[3].strip()
    )


def parse_document(path: str, text: str) -> tuple[list[Contract], list[Finding]]:
    contracts: list[Contract] = []
    findings: list[Finding] = []
    fence = ""
    active = False
    opening = 0
    body: list[str] = []
    for line, content in enumerate(text.splitlines(), 1):
        match = FENCE.fullmatch(content)
        if fence:
            if closes_fence(match, fence):
                if active:
                    try:
                        contracts.append(make_contract(path, opening, "\n".join(body)))
                    except (ValueError, TypeError) as error:
                        findings.append(Finding(path, opening, "contract-schema", str(error)))
                fence = ""
                active = False
            elif active:
                body.append(content)
            continue
        if match:
            fence = match[2]
            info = match[3].strip()
            active = info == "ava-contract"
            opening = line
            body = []
            if info.startswith("ava-contract") and not active:
                findings.append(
                    Finding(path, line, "contract-schema", "contract fence takes no options")
                )
    if fence and active:
        findings.append(Finding(path, opening, "contract-schema", "unclosed contract fence"))
    return contracts, findings
