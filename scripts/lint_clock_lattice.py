"""Forbid lattice-vocabulary timing constants outside the clock-lattice modules.

Run: `.venv/bin/python scripts/lint_clock_lattice.py [path ...]` (defaults to
scanning the non-test source dirs). Also run automatically via pre-commit hook.

## Why

The clock lattice (`shared/timing.py`) is the single authority for every timing
constant that must hold an ORDER relative to its neighbours — boot stall < launch
confirm < boot budget < reap grace, NO_PROGRESS < LOCK_TTL, the lease TTLs, the
controller scan cadence, the wedged derivation. The orderings are load-bearing:
the 2026-07-30 spawn incident happened because a launch-confirm window was raised
without its neighbouring reap grace, and the relation existed only in prose.

A bare `_SOME_REAP_GRACE_S = 100` in a new module is that incident's seedling —
the name carries lattice vocabulary, so it reads as part of the lattice, but
nothing knows its neighbours. This lint is the code-level guard: lattice
vocabulary may only appear where the lattice can see it.

## The rule

A module-level constant whose name contains lattice vocabulary (`STALL`, `GRACE`,
`REAP`, `BUDGET`, `WEDGED`, `NO_PROGRESS`, `LOCK_TTL`, `UPDATER_LEASE`,
`SETTLE_TTL`, `LAUNCH_CONFIRM`, `LEASE_TTL`, `LEASE_RENEW`, `SCAN_INTERVAL`,
`REAP_INTERVAL`) must be one of:

1. **Defined in a lattice family module** — `shared/timing.py`,
   `shared/boot_timing.py`, `shared/deploy_timing.py`, `shared/stop_timing.py`,
   `shared/schedule_timing.py`, `shared/cluster_lock.py`,
   `shared/host_deploy_state.py`. These are the lattice's homes; registering a
   new clock there and in `CLOCKS` is the correct way to add one.
2. **An alias of a registered clock** — the assignment's value is a bare
   reference to a clock registered in `shared.timing.CLOCKS`
   (e.g. `_ROLLOUT_STALL_TIMEOUT_S: float = NO_PROGRESS_TIMEOUT_S`). The value is
   still defined once; the alias is just a local name.
3. **Explicitly exempt** in `_INDEPENDENT_CLOCKS` below — the constant is either
   not a clock at all (a flag name, a SQL key, a collection) or a genuinely
   independent clock with no lattice neighbour, each with a stated reason.

Scope: non-test code only (tests monkeypatch clocks smaller on purpose).
Settings fields are class-body definitions in `shared/config/` and are the
operator-overridable configuration authority, not module constants — they are not
scanned, and `shared/timing.py` registers them by reference.

Error format `file:line: <reason>` + non-zero exit.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

_SCAN_DIRS = (
    "agent",
    "ava",
    "ava_builtins",
    "cli",
    "gateway",
    "ops",
    "scripts",
    "services",
    "shared",
)

_TEST_PATTERNS = (
    re.compile(r"(^|/)tests?/"),
    re.compile(r"(^|/)test_[^/]+\.py$"),
    re.compile(r"_test\.py$"),
)

# Lattice vocabulary: a module-level constant carrying any of these substrings is
# presumed to be part of the clock lattice until proven otherwise.
_LATTICE_TERMS = (
    "STALL",
    "GRACE",
    "REAP",
    "BUDGET",
    "WEDGED",
    "NO_PROGRESS",
    "LOCK_TTL",
    "UPDATER_LEASE",
    "SETTLE_TTL",
    "LAUNCH_CONFIRM",
    "LEASE_TTL",
    "LEASE_RENEW",
    "SCAN_INTERVAL",
    "REAP_INTERVAL",
)

# The lattice family modules: lattice vocabulary may be DEFINED here (and only
# here). `shared/cluster_lock.py` and `shared/host_deploy_state.py` hold the
# deploy-lease and updater-lease clocks respectively.
_FAMILY_MODULES = (
    "shared/timing.py",
    "shared/boot_timing.py",
    "shared/deploy_timing.py",
    "shared/stop_timing.py",
    "shared/schedule_timing.py",
    "shared/cluster_lock.py",
    "shared/host_deploy_state.py",
)

_CONST_NAME = re.compile(r"^_?[A-Z][A-Z0-9_]*$")


# Registered clock names, for the alias rule (rule 2): the assignment's value must
# be a bare reference to one of these. Read from `shared/timing.py` once, at
# import, with a pure AST parse (no import of the app's settings stack), so the
# lint runs anywhere the source tree is present — the same zero-dependency shape
# as every other scripts/ lint.
def _parse_registered_clocks() -> frozenset[str]:
    tree = ast.parse((_REPO_ROOT / "shared" / "timing.py").read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        target = node.targets[0] if isinstance(node, ast.Assign) else node.target
        if (
            isinstance(target, ast.Name)
            and target.id == "CLOCKS"
            and isinstance(node.value, ast.Dict)
        ):
            for key in node.value.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    names.add(key.value)
    return frozenset(names)


_REGISTERED_CLOCKS: frozenset[str] = _parse_registered_clocks()


def _registered_clock_names() -> frozenset[str]:
    return _REGISTERED_CLOCKS


# Explicit exemptions: `(relative_path, NAME)` -> why this lattice-vocabulary
# constant may live outside the family modules. Either it is not a clock at all
# (a flag, a key, a collection), or it is a genuinely independent clock with no
# lattice neighbour. Every entry states which.
_INDEPENDENT_CLOCKS: dict[tuple[str, str], str] = {
    ("agent/_boot_deadline.py", "STALL_FLAG"): "argv flag name (--boot-stall-seconds), not a clock",
    (
        "agent/_boot_deadline.py",
        "BUDGET_FLAG",
    ): "argv flag name (--boot-budget-seconds), not a clock",
    (
        "agent/graph/_node_log.py",
        "_STALL_GUARD_EXEMPT",
    ): "frozenset of exempt node kinds, not a clock",
    (
        "shared/exec_process_domain.py",
        "KILL_GRACE_S",
    ): "independent: SIGINT/SIGTERM -> SIGKILL grace ladder for the exec child, no lattice neighbour",
    (
        "shared/proc.py",
        "_TERMINATE_GRACE_S",
    ): "independent: TERM->KILL ladder wait in the terminate step, no lattice neighbour",
    (
        "shared/proc.py",
        "_REAP_TIMEOUT_S",
    ): "independent: single wait_procs bound when reaping a process tree, no lattice neighbour",
    (
        "shared/pty_sessions/host.py",
        "_REAP_POLL_S",
    ): "independent: waitpid poll after SIGKILL to collect the zombie, no lattice neighbour",
    (
        "shared/redis_listener.py",
        "_CONSUME_ABANDON_GRACE",
    ): "independent: pubsub consume-abandon window, no lattice neighbour",
    (
        "services/agent_host/daemon.py",
        "_STRAY_STOP_GRACE_S",
    ): "independent: SIGTERM drain window for a roster-gated stray at hosted bring-up; "
    "the watchdog round is the backstop but no ordering safety depends on this value "
    "(it only bounds how long bring-up waits before handing over), no lattice neighbour",
    (
        "shared/events/contract.py",
        "DELIVERY_STALLED_KEYS",
    ): "SQL key set for the delivery_stalled view, not a clock",
    (
        "shared/events/contract.py",
        "DELIVERY_POISONED_KEYS",
    ): "SQL key set for the delivery_poisoned view, not a clock",
    (
        "ava_builtins/plugins/lm_anthropic/provider.py",
        "_CLAUDE_EXTENDED_THINKING_DEFAULT_BUDGET",
    ): "LLM thinking-token budget, not a wall-clock constant",
    ("cli/commands/_update_phase_b.py", "POLL_STALLED"): "poll status value, not a clock",
    ("cli/commands/_update_phase_b.py", "POLL_NO_PROGRESS"): "poll status value, not a clock",
    (
        "cli/commands/_update_phase_b.py",
        "_STALL_CONFIRMATIONS",
    ): "set of stalled confirmations, not a clock",
    ("ops/updater_reap.py", "REAP_CLEARED_QUALIFIER"): "SQL qualifier string, not a clock",
    (
        "ops/agent_launch.py",
        "_LAUNCH_CONFIRM_POLL_INTERVAL_SEC",
    ): "independent: poll granularity (50 ms), orders nothing",
    (
        "gateway/schedule_runner.py",
        "_STALL_TIMEOUT_S",
    ): "independent family (schedule breaker): settings alias, not part of the audited lattice",
    (
        "gateway/schedule_runner.py",
        "_STALL_CHECK_INTERVAL_S",
    ): "independent family (schedule breaker): settings alias, not part of the audited lattice",
    (
        "ava_builtins/skills/ava-use-claude-code-and-codex/reference/watch_work.py",
        "STALL_SECONDS",
    ): "example script (skill reference), not cluster runtime — its own stall judgment, no lattice neighbour",
}


def _scan_file(path: Path) -> list[str]:
    rel = path.relative_to(_REPO_ROOT).as_posix()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return []  # other lints / the compiler own syntax errors
    errors: list[str] = []
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if not isinstance(target, ast.Name) or not _CONST_NAME.match(target.id):
                continue
            name = target.id
            if not any(term in name for term in _LATTICE_TERMS):
                continue
            if rel in _FAMILY_MODULES:
                continue  # rule 1: the lattice's homes
            if (rel, name) in _INDEPENDENT_CLOCKS:
                continue  # rule 3: explicit exemption
            # rule 2: alias of a registered clock
            value = node.value
            if isinstance(value, ast.Name) and value.id in _registered_clock_names():
                continue
            errors.append(
                f"{path}:{node.lineno}: {name} — lattice-vocabulary clock outside "
                "the lattice family modules; define it in shared/timing.py (and "
                "register it in CLOCKS) or make it an alias of a registered clock"
            )
    return errors


def _scan(paths: list[Path]) -> list[str]:
    errors: list[str] = []
    for path in paths:
        if path.is_dir():
            for p in sorted(path.rglob("*.py")):
                rel = p.relative_to(_REPO_ROOT).as_posix()
                if any(pat.search(rel) for pat in _TEST_PATTERNS):
                    continue
                errors.extend(_scan_file(p))
        elif path.suffix == ".py":
            errors.extend(_scan_file(path))
    return errors


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    paths = [Path(a) for a in argv] if argv else [_REPO_ROOT / d for d in _SCAN_DIRS]
    errors = _scan(paths)
    for err in errors:
        print(err, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
