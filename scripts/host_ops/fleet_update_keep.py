#!/usr/bin/env python

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from meizel.contracts.analysis import analyze


@dataclass(frozen=True, slots=True)
class Component:
    name: str
    dirs: tuple[str, ...]
    watch: tuple[str, ...]
    services: tuple[str, ...]


# meiva:ref scripts/host_ops/docs/fleet-update-keep.meiva.okf.md#component-keep-decision
COMPONENTS: tuple[Component, ...] = (
    Component(
        name="pty-sessions",
        dirs=("services/agent_runner/pty_sessions", "base/sessions/pty"),
        watch=("services/supervision/healthchecks/protocol_probe.py",),
        services=("pty-sessions",),
    ),
    Component(
        name="browser",
        dirs=("services/desktop/browser", "meiva_builtins/mcps/chrome"),
        watch=(
            "meiva/mcps/_browser.py",
            "services/supervision/healthchecks/browser.py",
            "services/supervision/healthchecks/browser_mcp.py",
        ),
        services=("browser", "browser-mcp"),
    ),
)


@dataclass(frozen=True, slots=True)
class Surface:
    files: frozenset[str]
    roots: int


def _under(path: str, directory: str) -> bool:
    return path == directory or path.startswith(directory + "/")


# meiva:ref scripts/host_ops/docs/fleet-update-keep.meiva.okf.md#component-roots-and-watch
def roots_at(files: frozenset[str], component: Component) -> list[str]:
    return sorted(
        f
        for f in files
        if any(_under(f, d) for d in component.dirs) and "/tests/" not in f and "/docs/" not in f
    )


def closure(adjacency: dict[str, set[str]], roots: list[str]) -> set[str]:
    seen = set(roots)
    stack = list(roots)
    while stack:
        for dependency in adjacency.get(stack.pop(), ()):
            if dependency not in seen:
                seen.add(dependency)
                stack.append(dependency)
    return seen


def component_surfaces(root: Path) -> dict[str, Surface]:
    analysis = analyze(root)
    files = analysis.universe.files
    adjacency: dict[str, set[str]] = defaultdict(set)
    for fact in analysis.facts:
        adjacency[fact.src].add(fact.dst)
    surfaces: dict[str, Surface] = {}
    for component in COMPONENTS:
        roots = roots_at(files, component)
        surface = closure(adjacency, roots) | set(roots)
        surface |= {w for w in component.watch if w in files}
        surfaces[component.name] = Surface(frozenset(surface), len(roots))
    return surfaces


def hit_files(surface: frozenset[str], changed: list[str]) -> list[str]:
    return [c for c in changed if any(_under(c, node) for node in surface)]


def _rev(repo: Path, ref: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", f"{ref}^{{commit}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode or not done.stdout.strip():
        raise RuntimeError(f"{ref} is not a commit in {repo}: git fetch origin")
    return done.stdout.strip()


def _changed(repo: Path, old: str, new: str) -> list[str]:
    done = subprocess.run(
        ["git", "-C", str(repo), "diff", "--name-only", "--no-renames", "-z", old, new],
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode:
        raise RuntimeError(f"git diff {old[:12]}..{new[:12]} failed: {done.stderr.strip()}")
    return [path for path in done.stdout.split("\0") if path]


# meiva:ref scripts/host_ops/docs/fleet-update-keep.meiva.okf.md#scratch-worktree-cleanup
def _worktree(repo: Path, parent: Path, name: str, revision: str) -> Path:
    target = parent / name
    done = subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-q", "--detach", str(target), revision],
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode:
        raise RuntimeError(f"git worktree add {name} failed: {done.stderr.strip()}")
    return target


def _dismantle(repo: Path, parent: Path) -> None:
    for name in ("old", "new"):
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "remove", "--force", str(parent / name)],
            capture_output=True,
            text=True,
            check=False,
        )
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "prune"],
        capture_output=True,
        text=True,
        check=False,
    )
    shutil.rmtree(parent, ignore_errors=True)


# meiva:ref scripts/host_ops/docs/fleet-update-keep.meiva.okf.md#keep-decision-fail-closed
def decide(repo: Path, old: str, new: str) -> dict[str, object]:
    old_sha, new_sha = _rev(repo, old), _rev(repo, new)
    changed = _changed(repo, old_sha, new_sha)
    verdicts: list[dict[str, object]] = []
    keep_services: list[str] = []
    if old_sha == new_sha:
        for component in COMPONENTS:
            verdicts.append(
                _verdict(
                    component,
                    keep=True,
                    reason="OLD == NEW",
                    surface_size=None,
                    hit_count=0,
                    hits=[],
                )
            )
            keep_services.extend(component.services)
    else:
        scratch = Path(tempfile.mkdtemp(prefix="meiva-fleet-update-keep-"))
        try:
            old_root = _worktree(repo, scratch, "old", old_sha)
            new_root = _worktree(repo, scratch, "new", new_sha)
            old_surfaces = component_surfaces(old_root)
            new_surfaces = component_surfaces(new_root)
        finally:
            _dismantle(repo, scratch)
        surfaces = {
            name: old_surfaces[name].files | new_surfaces[name].files for name in old_surfaces
        }
        root_counts = {
            name: old_surfaces[name].roots + new_surfaces[name].roots for name in old_surfaces
        }
        for component in COMPONENTS:
            surface = surfaces[component.name]
            missing_watch = [w for w in component.watch if w not in surface]
            if not root_counts[component.name] or missing_watch:
                reasons: list[str] = []
                if not root_counts[component.name]:
                    reasons.append("no root file at either revision")
                if missing_watch:
                    reasons.append(
                        f"watch files absent at both revisions: {', '.join(missing_watch)}"
                    )
                verdicts.append(
                    _verdict(
                        component,
                        keep=False,
                        reason="; ".join(reasons) + "; restarting",
                        surface_size=None,
                        hit_count=0,
                        hits=[],
                    )
                )
                continue
            hits = hit_files(surface, changed)
            reason = (
                "no changed file reaches it"
                if not hits
                else f"{len(hits)} changed file(s) reach it"
            )
            verdicts.append(
                _verdict(
                    component,
                    keep=not hits,
                    reason=reason,
                    surface_size=len(surface),
                    hit_count=len(hits),
                    hits=hits,
                )
            )
            if not hits:
                keep_services.extend(component.services)
    return {
        "repo": str(repo),
        "old": old_sha,
        "new": new_sha,
        "changed_files": len(changed),
        "components": verdicts,
        "keep_services": keep_services,
    }


def _verdict(
    component: Component,
    *,
    keep: bool,
    reason: str,
    surface_size: int | None,
    hit_count: int,
    hits: list[str],
) -> dict[str, object]:
    return {
        "name": component.name,
        "services": list(component.services),
        "keep": keep,
        "reason": reason,
        "surface": surface_size,
        "hit_count": hit_count,
        "hits": hits[:20],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fleet_update_keep.py", description="Decide keep/restart per update component."
    )
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="the checkout to diff")
    parser.add_argument(
        "--old", required=True, help="the revision every host runs before the update"
    )
    parser.add_argument("--new", required=True, help="the revision the update switches to")
    args = parser.parse_args(argv)
    try:
        payload = decide(args.repo.resolve(), args.old, args.new)
    except Exception as exc:
        print(f"keep decision failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
