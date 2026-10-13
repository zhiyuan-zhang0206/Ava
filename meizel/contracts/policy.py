"""`meizel.policy.toml` in the checker: validation against the manifests, rendering and `policy diff`.

The format and its reader live in `meizel.allowances`, shared with the lints whose allowances the
file also holds. Here: a manifest may use a dynamic-load or external-code form only for the sites
the policy lists (the free-text reason in a manifest no longer counts), and a change to the file is
reported by `policy diff` so a reviewer sees every widening at once.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import cast

from meizel.allowances import POLICY, Policy, parse
from meizel.contracts.manifest import Manifest
from meizel.graph.model import Code, Diagnostic, MeizelError
from meizel.graph.universe import Universe

__all__ = ["POLICY", "Policy", "changes", "check", "load_policy", "policy_at", "render_policy"]


def parse_policy(text: str) -> tuple[Policy, list[Diagnostic]]:
    policy, problems = parse(text)
    return policy, [Diagnostic(POLICY, 1, Code.POLICY_SYNTAX, message) for message in problems]


def load_policy(universe: Universe) -> tuple[Policy, list[Diagnostic]]:
    """The policy of the working tree; an absent file is an empty one, so nothing is allowed."""
    if POLICY not in universe.files:
        return Policy(), []
    return parse_policy((universe.root / POLICY).read_text(encoding="utf-8"))


def _site(manifest: Manifest, name: str) -> str:
    return f"{manifest.directory}/{name}" if manifest.directory else name


def check(manifests: dict[str, Manifest], policy: Policy) -> list[Diagnostic]:
    """Every `[external]` and `[loads]` site in a manifest that the policy does not list."""
    found: list[Diagnostic] = []
    for _, manifest in sorted(manifests.items()):
        for name in manifest.external:
            if _site(manifest, name) not in policy.external:
                found.append(_unlisted(manifest, name, "external", ""))
        for name, targets in manifest.loads.items():
            listed = policy.loads.get(_site(manifest, name), ())
            missing = [t for t in targets if t not in listed]
            if missing:
                found.append(_unlisted(manifest, name, "loads", ", ".join(missing)))
    return found


def _unlisted(manifest: Manifest, name: str, table: str, detail: str) -> Diagnostic:
    site = _site(manifest, name)
    what = f" -> {detail}" if detail else ""
    return Diagnostic(
        manifest.path,
        manifest.line_of(name),
        Code.UNLISTED_ALLOWANCE,
        f"[{table}] {site}{what} is not listed in {POLICY}",
        (
            f"a [{table}] allowance is a decision, not an edit: it exists only where {POLICY} lists it, "
            "and a reason written in the manifest does not count; rewrite the code as a static "
            "import, or ask the maintainers to list the site",
        ),
    )


def render_policy(external: dict[str, str], loads: dict[str, tuple[str, ...]]) -> str:
    """A policy file listing exactly these sites, sorted."""
    blocks = [
        "[external]\n"
        + "".join(f"{json.dumps(p)} = {json.dumps(r)}\n" for p, r in sorted(external.items()))
    ]
    blocks.append(
        "[loads]\n"
        + "".join(
            f"{json.dumps(p)} = [{', '.join(json.dumps(t) for t in targets)}]\n"
            for p, targets in sorted(loads.items())
        )
    )
    blocks.append("[default-arguments]\n")
    return "\n".join(blocks)


def policy_at(root: Path, ref: str) -> Policy:
    """The policy at a git ref; empty when the ref has none."""
    if ref.startswith("-"):
        raise MeizelError(f"{ref}: not a git ref")
    result = subprocess.run(  # noqa: S603 - fixed argv
        ["git", "-C", str(root), "show", f"{ref}:{POLICY}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return Policy()
    policy, problems = parse_policy(result.stdout)
    if problems:
        raise MeizelError(f"{ref}:{POLICY}: {problems[0].message}")
    return policy


def changes(old: Policy, new: Policy) -> list[str]:
    """A line per entry added, removed or changed between two policies."""
    lines: list[str] = []

    def text(value: object) -> str:
        if isinstance(value, tuple):
            return "[" + ", ".join(cast("tuple[str, ...]", value)) + "]"
        return str(value)

    def show(key: str, value: object) -> str:
        return f"{key} = {text(value)}" if text(value) else key

    def table(name: str, before: dict[str, object], after: dict[str, object]) -> None:
        lines.extend(
            f"+ [{name}] {show(k, after[k])}" for k in sorted(after.keys() - before.keys())
        )
        lines.extend(
            f"- [{name}] {show(k, before[k])}" for k in sorted(before.keys() - after.keys())
        )
        lines.extend(
            f"~ [{name}] {k}: {text(before[k])} -> {text(after[k])}"
            for k in sorted(before.keys() & after.keys())
            if before[k] != after[k]
        )

    table("external", dict(old.external), dict(new.external))
    table("loads", dict(old.loads), dict(new.loads))
    table(
        "default-arguments",
        dict.fromkeys(old.default_arguments, ""),
        dict.fromkeys(new.default_arguments, ""),
    )
    for rule, section in sorted(old.listed.keys() | new.listed.keys()):
        table(
            f"{rule}.{section}",
            dict(old.listed.get((rule, section), {})),
            dict(new.listed.get((rule, section), {})),
        )
    for rule, section in sorted(old.named.keys() | new.named.keys()):
        table(
            f"{rule}.{section}",
            dict.fromkeys(old.named.get((rule, section), ()), ""),
            dict.fromkeys(new.named.get((rule, section), ()), ""),
        )
    return lines
