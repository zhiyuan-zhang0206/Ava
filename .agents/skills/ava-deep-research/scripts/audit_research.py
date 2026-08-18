#!/usr/bin/env python3
"""Deterministic audit for an ava-deep-research state file (and derived report).

The R2 move, applied to research: the report is a *derived view* of the
research state, and this script is the *verifier* of that derivation.
It enforces the invariants from `references/research-state.md`:

  1. Required sections present: question, plan, sources, learnings.
  2. Every source_id referenced by a learning or claim exists in sources.
  3. Every source has url, title, and accessed_at (visited => real).
  4. Every learning has >= 1 source; a `consensus` learning has >= 2 distinct.
  5. Source URLs are unique.
  6. confidence / verification values are from their fixed sets.
  7. Every inline citation [n] in the report resolves to a source id.
  8. When meta.budget.max_fetches is set, len(sources) <= max_fetches —
     the global fetch budget, enforced after dedup (unique sources are a
     lower bound on fetch calls).

Exit codes: 0 = clean, 1 = violations found, 2 = usage/IO error.
Stdlib only — runs on any python3.

Usage:
  python3 audit_research.py --state research_state.json [--report report.md]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

CONFIDENCE = {"consensus", "single", "contradicted", "unverified"}
VERIFICATION = {"formal", "process", "rubric", "self"}
SOURCE_KINDS = {"primary", "secondary", "tertiary"}
REQUIRED_SECTIONS = ("question", "plan", "sources", "learnings")
CITATION_RE = re.compile(r"\[(\s*\d+(?:\s*,\s*\d+)*\s*)\]")
_CODE_FENCE_RE = re.compile(r"```.*?```", re.S)
_INLINE_CODE_RE = re.compile(r"`[^`]*`")


def _load(path: str) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError(f"{path}: top level must be a JSON object")
    return data


def _check_question_plan(state: dict, out: list[str]) -> None:
    question = state["question"]
    if not isinstance(question, dict) or not question.get("three_part"):
        out.append("question.three_part must be a non-empty string")
    if not isinstance(question.get("so_what"), str) or not question["so_what"]:
        out.append("question.so_what must be a non-empty string")

    plan = state["plan"]
    if not isinstance(plan, list) or not plan:
        out.append("plan must be a non-empty list of sub-questions")
    for i, sub in enumerate(plan):
        if not isinstance(sub, dict) or not sub.get("sub_question"):
            out.append(f"plan[{i}]: missing sub_question")


def _check_sources(state: dict, out: list[str]) -> dict[int, dict]:
    sources = state["sources"]
    if not isinstance(sources, list):
        out.append("sources must be a list")
        return {}
    source_by_id: dict[int, dict] = {}
    urls: set[str] = set()
    for i, s in enumerate(sources):
        sid = s.get("id")
        if not isinstance(sid, int):
            out.append(f"sources[{i}]: id must be an integer, got {sid!r}")
            continue
        if sid in source_by_id:
            out.append(f"sources: duplicate source id {sid}")
            continue
        source_by_id[sid] = s
        for field in ("url", "title", "accessed_at"):
            if not isinstance(s.get(field), str) or not s[field]:
                out.append(f"sources[{i}] (id {sid}): missing {field}")
        url = s.get("url")
        if isinstance(url, str):
            if url in urls:
                out.append(f"sources: duplicate url {url}")
            urls.add(url)
        kind = s.get("kind")
        if kind is not None and kind not in SOURCE_KINDS:
            out.append(f"sources[{i}] (id {sid}): kind {kind!r} not in {sorted(SOURCE_KINDS)}")
        accessed = s.get("accessed_at")
        if isinstance(accessed, str):
            try:
                datetime.fromisoformat(accessed.replace("Z", "+00:00"))
            except ValueError:
                out.append(f"sources[{i}] (id {sid}): accessed_at not ISO-8601: {accessed!r}")
    return source_by_id


def _check_learnings(state: dict, source_by_id: dict[int, dict], out: list[str]) -> None:
    learnings = state["learnings"]
    if not isinstance(learnings, list):
        out.append("learnings must be a list")
        return
    for i, learning in enumerate(learnings):
        if not isinstance(learning.get("fact"), str) or not learning["fact"]:
            out.append(f"learnings[{i}]: missing fact")
        conf = learning.get("confidence")
        if conf not in CONFIDENCE:
            out.append(f"learnings[{i}]: confidence {conf!r} not in {sorted(CONFIDENCE)}")
        ids = learning.get("source_ids")
        if not isinstance(ids, list) or not ids:
            out.append(f"learnings[{i}]: source_ids must be a non-empty list")
            continue
        for sid in ids:
            if not isinstance(sid, int) or sid not in source_by_id:
                out.append(f"learnings[{i}]: source_id {sid!r} not in sources")
        if conf == "consensus" and len(set(ids)) < 2:
            out.append(f"learnings[{i}]: consensus requires >= 2 distinct sources")


def _check_budget(state: dict, out: list[str]) -> None:
    """Invariant 8: unique sources must not exceed the pre-registered fetch cap.

    Sources are URL-deduped (invariant 5) and every source was fetched
    (accessed_at), so len(sources) is a lower bound on actual fetch calls:
    exceeding it means the budget was blown even after dedup. The check is
    one-way on purpose — parallel workers each counting independently is the
    #1108 failure mode, and this is the audit-side enforcement of the
    per-worker caps the orchestrator distributes.
    """
    budget = state.get("meta", {}).get("budget", {})
    max_fetches = budget.get("max_fetches")
    if not isinstance(max_fetches, int):
        return  # no pre-registered cap — nothing to enforce
    n_sources = len(state.get("sources", []))
    if n_sources > max_fetches:
        out.append(
            f"budget exceeded: {n_sources} unique sources > "
            f"meta.budget.max_fetches {max_fetches} "
            "(split the cap across workers: sum of per-worker caps = max_fetches)"
        )


def _check_claims(state: dict, source_by_id: dict[int, dict], out: list[str]) -> None:
    claims = state.get("claims")
    if claims is None:
        return
    if not isinstance(claims, list):
        out.append("claims must be a list")
        return
    for i, c in enumerate(claims):
        if not isinstance(c.get("claim"), str) or not c["claim"]:
            out.append(f"claims[{i}]: missing claim")
        ver = c.get("verification")
        if ver not in VERIFICATION:
            out.append(f"claims[{i}]: verification {ver!r} not in {sorted(VERIFICATION)}")
        for sid in c.get("source_ids", []):
            if not isinstance(sid, int) or sid not in source_by_id:
                out.append(f"claims[{i}]: source_id {sid!r} not in sources")


def _problems(state: dict) -> list[str]:
    out: list[str] = []
    for section in REQUIRED_SECTIONS:
        if section not in state:
            out.append(f"missing required section: {section}")
            return out  # cannot check the rest meaningfully
    _check_question_plan(state, out)
    source_by_id = _check_sources(state, out)
    _check_learnings(state, source_by_id, out)
    _check_claims(state, source_by_id, out)
    _check_budget(state, out)
    return out


def _report_problems(report_path: str, source_ids: set[int]) -> list[str]:
    text = Path(report_path).read_text(encoding="utf-8")
    # Code fences and inline code spans are not citations — a code block may
    # legitimately contain bracketed numbers (indexing, years in strings).
    text = _CODE_FENCE_RE.sub(" ", text)
    text = _INLINE_CODE_RE.sub(" ", text)
    cited: set[int] = set()
    for num_list in CITATION_RE.findall(text):
        for part in num_list.split(","):
            cited.add(int(part.strip()))
    return [
        f"report: citation [{n}] does not resolve to a source id"
        for n in sorted(cited)
        if n not in source_ids
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True, help="path to research_state.json")
    parser.add_argument("--report", help="path to the derived report (markdown)")
    args = parser.parse_args(argv)

    try:
        state = _load(args.state)
    except (OSError, ValueError, TypeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    try:
        problems = _problems(state)
        if args.report:
            problems += _report_problems(
                args.report,
                {s["id"] for s in state.get("sources", []) if isinstance(s.get("id"), int)},
            )
    except (OSError, TypeError, AttributeError, KeyError, ValueError) as e:
        print(f"error: malformed state or report: {e}", file=sys.stderr)
        return 2

    if problems:
        print(f"FAIL: {len(problems)} violation(s):")
        for p in problems:
            print(f"  - {p}")
        return 1
    n_sources = len(state.get("sources", []))
    n_learnings = len(state.get("learnings", []))
    print(f"OK: {n_sources} sources, {n_learnings} learnings, all invariants hold")
    return 0


if __name__ == "__main__":
    sys.exit(main())
