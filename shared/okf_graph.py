"""OKF knowledge-graph: parse `*.ava.okf.md` bundles into D3 graph data, and
render that data into the self-contained D3 visualization page.

Two consumers share this module:
  - `scripts/build_okf_data.py` — CLI, writes `graph_data.json` to disk (the
    manually-refreshed convention described in `index.ava.okf.md`, used for
    reviewing doc-bundle changes as a diff).
  - `gateway/routers/okf_graph.py` — serves the rendered page live over
    `GET /api/okf/graph`, rebuilt fresh from the current tree on every
    request. It never reads the checked-in `graph_data.json`, so the served
    graph cannot go stale between manual rebuilds.

See `index.ava.okf.md` for the OKF format itself (frontmatter, [[wikilinks]],
filesystem-derived hierarchy).
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast

import yaml

from shared.frontmatter import parse_frontmatter_typed
from shared.notes import normalize_tags

EXT = ".ava.okf.md"
ROOT_NAME = f"index{EXT}"
WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")

# okf-d3-template.html's inline `window.GRAPH_DATA = /*__GRAPH_DATA_JSON__*/;` —
# render_html() does a literal string replace of this placeholder.
DATA_PLACEHOLDER = "/*__GRAPH_DATA_JSON__*/"


def posix(p: str) -> str:
    return p.replace(os.sep, "/")


def find_files(bundle_dir: str | Path) -> list[str]:
    out: list[str] = []
    for root, dirs, files in os.walk(bundle_dir):
        # `.github/` is walked despite being hidden: its overview node lives
        # inside it (`.github/.github.ava.okf.md`). Every other hidden dir
        # (`.git`, `.venv`, `.claude`, worktrees, ...) stays excluded.
        dirs[:] = [d for d in dirs if (not d.startswith(".") or d == ".github") and d != ".git"]
        for f in files:
            if f.endswith(EXT):
                rel = posix(os.path.relpath(str(Path(root) / f), bundle_dir))
                out.append(rel)
    return sorted(out)


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Adapter over `shared.frontmatter.parse_frontmatter_typed` — keeps this
    module's historical lenient contract: absent/bad frontmatter → `({}, text)`,
    values keep their YAML types, and the body's leading newline is stripped.

    The legacy tolerance for an opener/closer line with trailing spaces
    (`"--- "`) is preserved for files written before the shared parser, which
    only recognizes a bare `---` fence.
    """
    parsed = parse_frontmatter_typed(text)
    if parsed is not None:
        fm, body = parsed
        return fm, body.lstrip("\n")
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            try:
                loaded = yaml.safe_load("\n".join(lines[1:i]))
            except yaml.YAMLError:
                loaded = None
            fm = cast("dict[str, Any]", loaded) if isinstance(loaded, dict) else {}
            return fm, "\n".join(lines[i + 1 :]).lstrip("\n")
    return {}, text


def find_root(all_paths: Iterable[str]) -> str | None:
    """The bundle's single root node — the `index.ava.okf.md` closest to the top.

    In this repo the apex lives in the index layer (`okf/index.ava.okf.md`),
    which holds only cross-domain nodes — the domain overviews are co-located
    with their code inside the directory they describe (`agent/agent.ava.okf.md`
    inside `agent/`). The shallowest `*/index.ava.okf.md` wins; ties break
    lexically, keeping the choice deterministic.

    `None` when a bundle carries no index at all — every node is then a root of
    its own subtree, which is the only sane reading of a bundle with no apex.
    """
    candidates = [p for p in all_paths if Path(p).name == ROOT_NAME]
    if not candidates:
        return None
    return min(candidates, key=lambda p: (p.count("/"), p))


def compute_parent(path: str, all_paths: set[str], root: str | None) -> str | None:
    """Hierarchy parent of `path`, resolved against the paths that actually exist.

    The rule is purely filesystem-derived, as `index.ava.okf.md` documents:
    a directory's overview node is the same-named file **inside** the
    directory (`<dir>/<dir>.ava.okf.md`, the canonical co-located position
    per the 2026-08-12 ruling), so `a/b/c.ava.okf.md` parents to
    `a/b/b.ava.okf.md` when it exists — the legacy sibling position
    (`a/b.ava.okf.md` beside `a/b/`) was removed 2026-08-13 with the last
    nested overviews moved inside. Only the cross-domain index layer in
    `okf/` has no filesystem parent and falls back to `root` (the apex,
    `okf/index.ava.okf.md`); a bundle-root file's parent is `root`, not a
    literal `index.ava.okf.md`.

    Every returned parent is a path that exists, which is what makes a
    dangling tree edge structurally impossible and leaves the graph with
    exactly one root.
    """
    if root is not None and path == root:
        return None

    parent_dir = posix(str(Path(path).parent))
    if parent_dir in {"", "."}:
        return root

    # Canonical: the overview file inside the parent directory, named after it
    # (`agent/agent.ava.okf.md` is the overview of `agent/`).
    internal = posix(str(Path(parent_dir) / (Path(parent_dir).name + EXT)))
    if internal != path and internal in all_paths:
        return internal

    return root


def resolve_wikilink(current_path: str, target: str, all_paths: set[str]) -> str | None:
    """Resolve a wikilink target to a canonical bundle-relative path."""
    target = target.strip()
    if "://" in target:
        return None

    cur_dir = str(Path(current_path).parent)

    # Try variants
    candidates: list[str] = []
    # Exact match
    if target in all_paths:
        return target
    # Add .ava.okf.md extension
    if not target.endswith(EXT):
        candidates.append(target + EXT)
    # Relative from current file's directory
    if cur_dir:
        candidates.append(posix(os.path.normpath(str(Path(cur_dir) / target))))
        candidates.append(posix(os.path.normpath(str(Path(cur_dir) / (target + EXT)))))
    # Absolute from bundle root
    candidates.append(target.lstrip("/"))
    candidates.append(target.lstrip("/") + EXT if not target.endswith(EXT) else target.lstrip("/"))

    for c in candidates:
        if c in all_paths:
            return c

    # Basename match (last resort)
    base = Path(target).name
    if not base.endswith(EXT):
        base += EXT
    matches = [p for p in all_paths if Path(p).name == base]
    if len(matches) == 1:
        return matches[0]

    return None


def build_graph_data(
    bundle_dir: str | Path,
    name: str = "OKF",
    *,
    raw_links_out: list[tuple[str, str, str | None]] | None = None,
) -> dict[str, Any]:
    """Parse every `.ava.okf.md` file under `bundle_dir` into D3 graph data:
    nodes, tree edges (from the filesystem hierarchy), cross edges (from
    resolved `[[wikilinks]]`, deduplicated + weighted), and the set of all tags.

    `raw_links_out`, if given, is extended with every `(source, raw_target,
    resolved_or_None)` wikilink tuple seen — used by the CLI to report
    unresolved links; callers that only want the graph (e.g. the gateway
    route) can leave it `None`.
    """
    bundle_dir = str(bundle_dir)
    paths = find_files(bundle_dir)
    path_set = set(paths)
    root = find_root(path_set)

    nodes: dict[str, dict[str, Any]] = {}
    raw_links: list[tuple[str, str, str | None]] = []

    for path in paths:
        filepath = str(Path(bundle_dir) / path)
        with Path(filepath).open(encoding="utf-8") as fh:
            text = fh.read()

        fm, body = parse_frontmatter(text)

        tags = list(normalize_tags(fm.get("tags")))

        nodes[path] = {
            "id": path,
            "title": fm.get("title", path),
            "description": fm.get("description", ""),
            "type": fm.get("type", "doc"),
            "tags": tags,
            "parent": compute_parent(path, path_set, root),
            "body": body,  # full markdown body
        }

        # Extract wikilinks from body
        for m in WIKILINK_RE.finditer(body):
            target_raw = m.group(1).strip()
            resolved = resolve_wikilink(path, target_raw, path_set)
            raw_links.append((path, target_raw, resolved))

    # Build tree edges
    tree_edges: list[dict[str, Any]] = []
    for path, n in nodes.items():
        if n["parent"] is not None:
            tree_edges.append({"source": n["parent"], "target": path})

    # Build cross edges (non-tree wikilinks, deduplicated by unordered pair)
    tree_pairs: set[frozenset[str]] = {
        frozenset((path, n["parent"])) for path, n in nodes.items() if n["parent"]
    }

    pair_info: dict[tuple[str, str], dict[str, Any]] = {}
    pair_order: list[tuple[str, str]] = []
    for s, _raw, r in raw_links:
        if r is None:
            continue
        if s == r:
            continue
        if frozenset((s, r)) in tree_pairs:
            continue
        a, b = sorted((s, r))
        key = (a, b)
        if key not in pair_info:
            pair_info[key] = {"source": s, "target": r, "weight": 0}
            pair_order.append(key)
        pair_info[key]["weight"] += 1

    cross_edges = [pair_info[k] for k in pair_order]

    # Collect all unique tags
    all_tags: list[str] = sorted({tag for n in nodes.values() for tag in n["tags"]})

    if raw_links_out is not None:
        raw_links_out.extend(raw_links)

    return {
        "name": name,
        "nodes": list(nodes.values()),
        "treeEdges": tree_edges,
        "crossEdges": cross_edges,
        "tags": all_tags,
    }


def render_html(data: dict[str, Any], template_text: str) -> str:
    """Inject graph `data` into the D3 template, returning a self-contained
    HTML page (no external data fetch — see `DATA_PLACEHOLDER`)."""
    if DATA_PLACEHOLDER not in template_text:
        raise ValueError("Template missing data placeholder")
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return template_text.replace(DATA_PLACEHOLDER, blob)
