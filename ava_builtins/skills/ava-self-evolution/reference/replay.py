#!/usr/bin/env python3
"""Re-run a dataset's replay-safe tasks to verify a proposed skill change.

OFF BY DEFAULT. This is the optional verification step: a failure is already
in the trace, so most iteration reads the dataset without re-running. Reach for
replay only when a specific proposal is worth confirming empirically.

Baseline is the dataset itself: each record's original outcome was produced
under the *old* code at collection time, so replaying the same task prompt
under the *current* tree is the old-vs-new comparison (see `compare.py`).

Safety. Replay reuses the eval harness (`evals.run_all`), which runs each task
in a fresh agent against a **separate, disposable eval database** — so DB state
never touches production. But the OS and network are NOT sandboxed here: a task
that runs a shell command, sends a message, edits files, or hits an external
API would still have real side effects. So replay only ever runs the safe
subset (`is_replay_safe`): tasks whose tool calls are pure read/compute. Broad
replay of file-mutating code tasks needs the eval harness's container mode and
is deliberately out of scope for this script.

Run (dry-run — lists what would replay and why the rest is skipped):

    .venv/bin/python skills/ava-self-evolution/reference/replay.py

Run for real (spends model tokens, spins fresh agents):

    .venv/bin/python skills/ava-self-evolution/reference/replay.py --run
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from shared.config import settings
from shared.paths import ava_home

# Tool-call prefixes that make a run unsafe to replay: fleet/user-facing side
# effects, process lifecycle, arbitrary OS (shell can rm / curl / git push),
# scheduling, and real file writes. A run is replay-safe iff NONE of its
# tools_called match these — leaving pure read/compute tasks.
UNSAFE_PREFIXES = (
    "ava.agents.",
    "ava.ui.",
    "ava.self.",
    "ava.shell.",
    "ava.watcher.",
    "ava.files.edit",
    "ava.files.write",
    "ava.files.delete",
    "ava.mcps.",
)


def is_replay_safe(rec: dict[str, Any]) -> tuple[bool, str]:
    """Return (safe, reason). A run is safe only if every tool it called is
    pure read/compute and it actually carries a task prompt to re-run."""
    if not rec["task_prompt"].strip():
        return False, "no task prompt"
    hits = sorted({tool for tool in rec["tools_called"] if tool.startswith(UNSAFE_PREFIXES)})
    if hits:
        return False, "side-effect tools: " + ", ".join(hits)
    return True, "pure read/compute"


def load_records(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _eval_db_url() -> str:
    """A separate database for replay agents, derived from the runtime one so
    it lands on the same server. `evals.run_all` creates it + applies schema if
    missing, and it is never production data."""
    import psycopg

    return psycopg.conninfo.make_conninfo(settings.data_plane.db_url, dbname="ava_selfevo_replay")


def _out_path(week: str) -> Path:
    out_dir = ava_home() / "self_evolution" / "replay"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"{week}.json"


def _outcome(rec: dict[str, Any], result: dict[str, Any] | None) -> dict[str, Any]:
    ok = result is not None and result["invoke_error"] is None and bool(result["chat_messages"])
    return {
        "agent_id": rec["agent_id"],
        "original_label": rec["label"],
        "replay_ok": ok,
        "replay_error": None if result is None else result["invoke_error"],
        "replay_turns": None if result is None else result["turns_invoked"],
        "replay_tools": {} if result is None else result["tool_calls"],
    }


def replay(records: list[dict[str, Any]], model: str | None = None) -> list[dict[str, Any]]:
    """Run every replay-safe task once under the current tree; return one
    outcome per task. Heavy eval deps are imported here so the pure helpers
    above stay importable without them.

    The eval harness moved to the private MyAva repo (2026-08-12), so `evals`
    is only importable when MyAva is on sys.path — normally via an editable
    install into the Ava venv (`uv pip install -e ~/MyAva`, see the
    run-swe-bench skill). MYAVA_REPO overrides the default location.
    """
    myava = Path(__import__("os").environ.get("MYAVA_REPO", "~/MyAva")).expanduser()
    try:
        from evals.cases._types import EvalCase
        from evals.driver import RunConfig, run_all
    except ImportError as exc:  # pragma: no cover — env-dependent
        raise SystemExit(
            "replay needs the eval harness from the MyAva repo. Install it into "
            f"this venv first: uv pip install -e {myava}  "
            "(or set MYAVA_REPO to the checkout path)"
        ) from exc

    selected = [r for r in records if is_replay_safe(r)[0]]
    # str.format runs over user_msgs in the harness (for `{tmpdir}`-style
    # placeholders); a real prompt may contain literal braces, so neutralize
    # them — replay uses no placeholders.
    cases = [
        EvalCase(
            name=f"replay-{r['agent_id']}",
            user_msgs=[r["task_prompt"].replace("{", "{{").replace("}", "}}")],
            check=lambda _result: (True, "replay"),
        )
        for r in selected
    ]
    cfg = RunConfig(db_url=_eval_db_url(), model=model or settings.lm.llm_model)
    records_out = run_all(cfg, cases)
    by_name = {rr.result.case_name: rr.to_json_dict() for rr in records_out}
    return [_outcome(r, by_name.get(f"replay-{r['agent_id']}")) for r in selected]


def _default_dataset() -> Path:
    from collect import _default_week, dataset_path  # sibling script

    return dataset_path(_default_week())


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Re-run a dataset's replay-safe tasks (off by default; --run to execute).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("dataset", nargs="?", default=None, help="dataset JSONL (default: this week's)")
    p.add_argument("--run", action="store_true", help="actually replay (default: dry-run listing)")
    p.add_argument("--model", default=None, help="model override (default: cluster default)")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    path = Path(args.dataset) if args.dataset else _default_dataset()
    records = load_records(path)
    verdicts = [(r, *is_replay_safe(r)) for r in records]
    safe = [r for r, ok, _ in verdicts if ok]
    if not args.run:
        print(f"[dry-run] {len(safe)}/{len(records)} runs are replay-safe in {path.name}")
        for r, ok, why in verdicts:
            mark = "replay" if ok else "skip"
            print(f"  [{mark}] #{r['agent_id']} ({why})")
        print("\nre-run with --run to execute.")
        return
    outcomes = replay(records)
    week = path.stem
    out = _out_path(week)
    out.write_text(json.dumps(outcomes, ensure_ascii=False, indent=2), encoding="utf-8")
    ok = sum(1 for o in outcomes if o["replay_ok"])
    print(f"replayed {len(outcomes)} safe tasks ({ok} ok) -> {out}")


if __name__ == "__main__":
    main()
