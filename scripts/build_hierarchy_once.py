#!/usr/bin/env python3
"""One manual understanding-layer build for one agent (task #3704, P2a).

Builds the agent's retained checkpoint history into the sealed level tree and
upserts every node into `understanding_nodes`. This script is the manual
trigger path until the compact-driven worker lands with P2b; run it on the
gateway host at a coordinated window:

    .venv/bin/python scripts/build_hierarchy_once.py --agent-id 3187
    .venv/bin/python scripts/build_hierarchy_once.py --agent-id 3187 --dry-run

The run is re-runnable and idempotent by construction: the generation reuse
cache (`input_hash -> text`) skips unchanged inputs without a model call, and
the `(agent_id, depth, span_start, span_end)` upsert key rewrites a node
instead of stacking a duplicate. A failed node (provider error after retries,
or a budget no compression could fit) writes nothing; re-running retries
exactly those, and the exit code is 1 whenever any node failed.
"""

from __future__ import annotations

import argparse
from urllib.parse import urlsplit

from shared.agents.history.hierarchy.generate import build_generation_llm
from shared.agents.history.hierarchy.pipeline import MaterializedTree, build_agent_tree
from shared.agents.history.hierarchy.store import load_known_texts, write_tree
from shared.config import settings
from shared.lm.factory import close_chat_model

# How many failed-node lines the report shows before folding the rest into a
# count. The full set is one re-run away; the operator needs the flavor and
# the node ids, not a wall of repeated provider errors.
_MAX_ERRORS_SHOWN = 5


def _db_label() -> str:
    """The target database as host:port/path - never a full DSN (no secrets)."""
    url = urlsplit(settings.data_plane.db_url)
    return f"{url.hostname}:{url.port}{url.path or ''}"


def _report(tree: MaterializedTree, *, known_count: int) -> None:
    """Print what the build produced: levels, triggers, pending, failures."""
    per_level: dict[int, int] = {}
    per_trigger: dict[str, int] = {}
    for node in tree.nodes:
        per_level[node.level] = per_level.get(node.level, 0) + 1
        per_trigger[node.trigger] = per_trigger.get(node.trigger, 0) + 1
    levels = " ".join(f"L{level}={count}" for level, count in sorted(per_level.items()))
    triggers = " ".join(f"{name}={count}" for name, count in sorted(per_trigger.items()))
    print(f"generation cache: {known_count} known input(s)")
    print(f"built {len(tree.nodes)} node(s), max level {tree.max_level}: {levels or '(none)'}")
    print(f"  triggers: {triggers or '(none)'}")
    print(f"  pending groups: {len(tree.pending)}")
    if tree.errors:
        print(f"errors: {len(tree.errors)}")
        for result in tree.errors[:_MAX_ERRORS_SHOWN]:
            print(f"  - {result.nid}: {result.error}")
        hidden = len(tree.errors) - _MAX_ERRORS_SHOWN
        if hidden > 0:
            print(f"  ... and {hidden} more")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--agent-id", type=int, required=True, help="agent whose retained history to build"
    )
    parser.add_argument("--dry-run", action="store_true", help="build and report; write nothing")
    parser.add_argument(
        "--model", default=None, help="generation model (default: settings.lm.hierarchy_model)"
    )
    args = parser.parse_args(argv)

    model: str = args.model or settings.lm.hierarchy_model
    print(f"target: {_db_label()} | agent {args.agent_id} | model {model}")

    known = load_known_texts(args.agent_id)
    llm = build_generation_llm(model)
    try:
        tree = build_agent_tree(args.agent_id, llm=llm, model=model, known_texts=known)
    finally:
        close_chat_model(llm)
    _report(tree, known_count=len(known))

    if not tree.nodes and not tree.errors:
        print("nothing built - does this agent have retained checkpoint history?")
    if args.dry_run:
        print("dry-run - nothing written")
    else:
        written = write_tree(args.agent_id, tree.nodes, model=model)
        print(f"upserted {written} node row(s)")
    if tree.errors:
        print("failed node(s) are not written - re-running retries them")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
