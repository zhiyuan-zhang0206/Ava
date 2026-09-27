"""Trace-level leak detection for isolated self-evolution evaluation runs."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class LeakPaths:
    """Host paths that an isolated evaluation run must not read."""

    memory_pool: str
    self_evolution: str
    workspaces_root: str
    agent_workspace: str


@dataclass(frozen=True)
class LeakFinding:
    """One attempted result-separation bypass found in executed code."""

    surface: str
    evidence: str
    tool: str | None


_AGENT_RESULT_ENDPOINT = re.compile(
    r"/api/agents/(\d+)(?:/messages\b|/last-message\b|/pending\b|/activity\b|"
    r"/timeline\b|/events/stream\b|/events\b|/traces/)"
)
_GLOBAL_RESULT_ENDPOINTS = (
    "/api/events",
    "/api/memory/search",
    "/api/tasks",
)
_SDK_VECTOR_PREFIXES = (
    "ava.tasks.",
    "ava.mcps.",
    "ava.ui.",
    "ava.web.",
    "ava.understand.",
    "ava.agents.get_last_message",
)
_SDK_NAME = re.compile(r"ava(?:\.[A-Za-z_]\w*)+")


def _evidence(code: str, start: int, end: int) -> str:
    """Return a compact, single-line excerpt that retains the match."""
    excerpt = " ".join(code[max(0, start - 48) : min(len(code), end + 112)].split())
    return excerpt if len(excerpt) <= 160 else excerpt[:157] + "..."


def _path_finding(code: str, path: str, surface: str) -> LeakFinding | None:
    """Find one forbidden path-prefix read, excluding the isolated empty pool."""
    start = code.find(path)
    if start < 0:
        return None
    end = start + len(path)
    if code[end:].startswith("/memory-pool"):
        return None
    return LeakFinding(surface=surface, evidence=_evidence(code, start, end), tool=None)


def _sibling_memory_finding(code: str, memory_pool: str) -> LeakFinding | None:
    """Find direct ``.md`` siblings of the shared memory pool root."""
    parent = memory_pool.rsplit("/", 1)[0]
    match = re.search(re.escape(parent) + r"/[^/\s'\"`]+\.md\b", code)
    if match is None:
        return None
    return LeakFinding(
        surface="memory",
        evidence=_evidence(code, match.start(), match.end()),
        tool=None,
    )


def _other_workspace_finding(
    code: str, *, agent_id: int, workspaces_root: str
) -> LeakFinding | None:
    """Find a literal path into another agent's workspace."""
    pattern = re.compile(re.escape(workspaces_root.rstrip("/")) + r"/([^/\s'\"`]+)")
    for match in pattern.finditer(code):
        if match.group(1) != str(agent_id):
            return LeakFinding(
                surface="results",
                evidence=_evidence(code, match.start(), match.end()),
                tool=None,
            )
    return None


def _endpoint_findings(code: str, agent_id: int) -> list[LeakFinding]:
    """Return attempted gateway result-read calls outside the eval agent's run."""
    findings: list[LeakFinding] = []
    for match in _AGENT_RESULT_ENDPOINT.finditer(code):
        if int(match.group(1)) != agent_id:
            findings.append(
                LeakFinding(
                    surface="results",
                    evidence=_evidence(code, match.start(), match.end()),
                    tool=None,
                )
            )
    for endpoint in _GLOBAL_RESULT_ENDPOINTS:
        start = code.find(endpoint)
        if start >= 0:
            findings.append(
                LeakFinding(
                    surface="results",
                    evidence=_evidence(code, start, start + len(endpoint)),
                    tool=None,
                )
            )
    return findings


def _sdk_findings(code: str) -> list[LeakFinding]:
    """Return blocked SDK-vector attempts as non-invalidating audit evidence."""
    findings: list[LeakFinding] = []
    for prefix in _SDK_VECTOR_PREFIXES:
        start = code.find(prefix)
        if start < 0:
            continue
        name = _SDK_NAME.match(code, start)
        tool = (
            name.group(0)
            if name is not None and code[name.end() :].lstrip().startswith("(")
            else None
        )
        findings.append(
            LeakFinding(
                surface="sdk",
                evidence=_evidence(code, start, start + len(prefix)),
                tool=tool,
            )
        )
    return findings


def scan(code_bodies: list[str], *, agent_id: int, leak_paths: LeakPaths) -> list[LeakFinding]:
    """Scan executed code for attempts to read data outside an eval run.

    This deliberately uses case-sensitive literal and prefix matching. The
    audit is evidence for a narrow, known set of leaked capabilities, not a
    sandbox or a claim that arbitrary Python can be comprehensively analyzed.
    """
    findings: list[LeakFinding] = []
    for code in code_bodies:
        memory = _path_finding(code, leak_paths.memory_pool, "memory")
        if memory is not None:
            findings.append(memory)
        else:
            sibling_memory = _sibling_memory_finding(code, leak_paths.memory_pool)
            if sibling_memory is not None:
                findings.append(sibling_memory)

        results = _path_finding(code, leak_paths.self_evolution, "results")
        if results is not None:
            findings.append(results)
        workspace = _other_workspace_finding(
            code,
            agent_id=agent_id,
            workspaces_root=leak_paths.workspaces_root,
        )
        if workspace is not None:
            findings.append(workspace)
        findings.extend(_endpoint_findings(code, agent_id))
        findings.extend(_sdk_findings(code))
    return findings


def invalidated(findings: list[LeakFinding]) -> bool:
    """Whether a finding records a read that invalidates an evaluation score."""
    return any(finding.surface in {"memory", "results", "network"} for finding in findings)
