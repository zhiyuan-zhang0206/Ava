"""`meizel.policy.toml`: the one place that lists every allowance the rules cannot grant themselves.

Some forms are allowed only where a person decided so: a file that loads code at run time
(`[loads]`), a file that runs code from outside the repository (`[external]`), and, should they
ever be allowed, default-argument injections. A manifest may use these forms only for the sites
listed here; the free-text reason in a manifest no longer counts. The file sits at the repository
root, and a change to it is reported by `policy diff` so a reviewer sees it at once.

```toml
[external]
"app/runner.py" = "executes the schedule script the user wrote"

[loads]
"app/loader.py" = ["ava_builtins"]

[default-arguments]
```
"""

from __future__ import annotations

import json
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from meizel.contracts.manifest import Manifest
from meizel.graph.model import Code, Diagnostic, MeizelError
from meizel.graph.universe import Universe

__all__ = ["POLICY", "Policy", "changes", "check", "load_policy", "policy_at", "render_policy"]

POLICY = "meizel.policy.toml"
_TABLES = ("external", "loads", "default-arguments")


@dataclass(slots=True)
class Policy:
    external: dict[str, str] = field(default_factory=dict[str, str])
    loads: dict[str, tuple[str, ...]] = field(default_factory=dict[str, tuple[str, ...]])
    default_arguments: tuple[str, ...] = ()
    lines: list[str] = field(default_factory=list[str])


def _problem(line: int, message: str) -> Diagnostic:
    return Diagnostic(POLICY, line, Code.POLICY_SYNTAX, message)


def parse_policy(text: str) -> tuple[Policy, list[Diagnostic]]:
    policy = Policy(lines=text.splitlines())
    problems: list[Diagnostic] = []
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        return policy, [_problem(1, f"invalid TOML: {error}")]
    for name in document:
        if name not in _TABLES:
            problems.append(
                _problem(1, f"unknown table {name!r}; the tables are {', '.join(_TABLES)}")
            )
    external = document.get("external", {})
    loads = document.get("loads", {})
    defaults = document.get("default-arguments", {})
    if not (isinstance(external, dict) and isinstance(loads, dict) and isinstance(defaults, dict)):
        return policy, [*problems, _problem(1, "every table must be a table")]
    for path, reason in cast("dict[str, object]", external).items():
        if isinstance(reason, str) and reason.strip():
            policy.external[path] = reason.strip()
        else:
            problems.append(_problem(1, f"[external] {path!r} needs a non-empty reason string"))
    for path, targets in cast("dict[str, object]", loads).items():
        if isinstance(targets, list) and all(
            isinstance(t, str) for t in cast("list[object]", targets)
        ):
            policy.loads[path] = tuple(cast("list[str]", targets))
        else:
            problems.append(_problem(1, f"[loads] {path!r} must be a list of directories"))
    policy.default_arguments = tuple(defaults)
    return policy, problems


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

    def show(value: object) -> str:
        return (
            "[" + ", ".join(cast("tuple[str, ...]", value)) + "]"
            if isinstance(value, tuple)
            else str(value)
        )

    def table(name: str, before: dict[str, object], after: dict[str, object]) -> None:
        lines.extend(
            f"+ [{name}] {k} = {show(after[k])}" for k in sorted(after.keys() - before.keys())
        )
        lines.extend(
            f"- [{name}] {k} = {show(before[k])}" for k in sorted(before.keys() - after.keys())
        )
        lines.extend(
            f"~ [{name}] {k}: {show(before[k])} -> {show(after[k])}"
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
    return lines
