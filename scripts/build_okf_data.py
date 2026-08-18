#!/usr/bin/env python3
"""Build graph_data.json from an Ava OKF bundle (.ava.okf.md files).

CLI wrapper over `shared/okf_graph.py`'s `build_graph_data()` — writes the
result to disk and prints build stats + unresolved-wikilink diagnostics.
`gateway/routers/okf_graph.py` calls `build_graph_data()` directly instead
(no file write, always rebuilt fresh for the live `GET /api/okf/graph` route).

Output: graph_data.json — nodes, treeEdges, crossEdges, tags.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Re-exported: scripts/lint_ava_okf.py does `from build_okf_data import resolve_wikilink`.
from shared.okf_graph import build_graph_data
from shared.okf_graph import resolve_wikilink as resolve_wikilink


def _print_stats(
    node_count: int,
    tree_edges: list[Any],
    cross_edges: list[Any],
    raw_links: list[tuple[str, str, str | None]],
    all_tags: list[Any],
) -> None:
    """Print build statistics to stderr."""
    unresolved = [(s, t) for s, t, r in raw_links if r is None]
    print(f"nodes: {node_count}", file=sys.stderr)
    print(f"tree edges: {len(tree_edges)}", file=sys.stderr)
    resolved_count = len([r for _, _, r in raw_links if r])
    print(
        f"cross edges: {len(cross_edges)} (from {resolved_count} resolved wikilinks)",
        file=sys.stderr,
    )
    print(f"unresolved wikilinks: {len(unresolved)}", file=sys.stderr)
    print(f"tags: {len(all_tags)}", file=sys.stderr)
    if unresolved:
        for s, t in unresolved[:10]:
            print(f"  unresolved: {s} -> {t}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Build OKF graph data for D3 viz")
    parser.add_argument("bundle_dir", help="Path to bundle directory containing .ava.okf.md files")
    parser.add_argument("out_file", nargs="?", default="tmp/graph_data.json")
    parser.add_argument("--name", default="OKF", help="Bundle name")
    args = parser.parse_args()

    raw_links: list[tuple[str, str, str | None]] = []
    data = build_graph_data(args.bundle_dir, name=args.name, raw_links_out=raw_links)

    with Path(args.out_file).open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
        fh.write("\n")

    _print_stats(len(data["nodes"]), data["treeEdges"], data["crossEdges"], raw_links, data["tags"])


if __name__ == "__main__":
    main()
