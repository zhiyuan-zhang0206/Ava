"""Event-registry zero-enforcement gate (single source of truth discipline).

`EVENTS` in `base/events/contract.py` is the only source of event names; this
module is its **verifier** (there is no hand-kept snapshot beside the registry).
Five guards:

1. **Forward**: every static `event=` literal in production code is registered.
   An unregistered name makes `telemetry.emit` fail fast (new code) or land
   silently as category=log with 30-day retention (old path); both are incidents.
2. **Reverse**: every `EVENTS` entry has a producer (an `event=` or
   `event_type=` literal, or an entry on the SQL/dynamic list below), so a
   deleted or renamed event cannot leave a stale registry row behind.
3. **Doc drift**: `base/events/registry.md` equals the generator output
   byte for byte (the doc is a generated artifact).
4. **SQL key injection**: attribute-key literals in read-side SQL (`->>'` and
   `?` forms) are declared keys of a payload TypedDict
   (`registered_payload_keys`), so renaming an event fails the SQL half too.
5. **No label-only records**: a loguru record call whose merged chain keywords
   carry `label=` but no `event=` would log as a bare `log` event, because
   `label` never names an event; the call must pass `event=`.

Limit (inherited from scan_kinds): static literal scanning cannot see names
produced dynamically (variable `event_type`, dict values, conditional
expressions); those carry their emission site on the declaration
(`EventSpec.site`) and are listed in `_SQL_OR_DYNAMIC_KINDS`.
"""

from __future__ import annotations

import re
from pathlib import Path

from base.events import scan_kinds  # namespace package — pythonpath = ["."]
from base.events.contract import EVENTS, registered_payload_keys, telemetry_events

_REPO = Path(__file__).resolve().parents[2]
_REGISTRY = _REPO / "base" / "events" / "registry.md"

# Kinds whose production site has no static `event=` literal, so scan_kinds
# cannot see them, carry their emission site on the declaration (`EventSpec.site`).
# Removing a kind from the code means removing the declaration (the reverse gate
# flags an orphaned entry).
_SQL_OR_DYNAMIC_KINDS = frozenset(name for name, spec in EVENTS.items() if spec.site)


def _code_kinds() -> tuple[set[str], set[str]]:
    """(event= literals, event_type= literals) from scan_kinds."""
    event_kinds, event_type_kinds, _sse_roles = scan_kinds.scan_code(_REPO)
    return set(event_kinds), set(event_type_kinds)


def test_every_static_event_kind_is_registered() -> None:
    """Forward gate: an `event=` kind outside `EVENTS` silently falls to
    category=log (30d retention) on the loguru path, and `telemetry.emit`
    fail-fasts on it elsewhere — either way it is a contract violation."""
    event_kinds, _ = _code_kinds()
    unregistered = sorted(k for k in event_kinds if k not in EVENTS)
    assert not unregistered, (
        "event= kind(s) missing from the base/events/declarations modules: "
        f"{unregistered}. Register each in the same PR that introduces it "
        "(one EventSpec line in its domain module — base/events/registry.md regenerates)."
    )


def test_production_scan_excludes_only_the_top_level_deploy_directory(tmp_path: Path) -> None:
    """`deploy/` at the root is service configuration; `base/deploy/` is a
    package, and skipping it by name would hide every producer inside it."""
    (tmp_path / "deploy").mkdir()
    (tmp_path / "deploy" / "render.py").write_text("")
    (tmp_path / "base" / "deploy").mkdir(parents=True)
    (tmp_path / "base" / "deploy" / "emit.py").write_text("")

    scanned = [rel for rel, _ in scan_kinds.iter_production_py(tmp_path)]

    assert scanned == ["base/deploy/emit.py"]


def test_registered_telemetry_events_have_producers() -> None:
    """Reverse drift guard: every EVENTS entry must have a code producer — an
    `event=` literal, an `event_type=` literal, or a
    documented SQL/dynamic emission. Catches stale registry entries (deleted
    or renamed kind still listed) that would keep the category mapping alive
    for a kind nobody emits."""
    event_kinds, event_type_kinds = _code_kinds()
    produced = event_kinds | event_type_kinds | _SQL_OR_DYNAMIC_KINDS
    orphaned = sorted(telemetry_events() - produced)
    assert not orphaned, (
        "EVENTS telemetry kind(s) with no producer in code: "
        f"{orphaned}. Remove them from the registry, or add the emission site "
        "(or `site=` on its declaration, naming where it emits) in the same PR."
    )


def test_registry_doc_matches_generated() -> None:
    """base/events/registry.md is a generated artifact (R2-C): it must equal
    the generator's output byte-for-byte. A registry change without running
    `scripts/codegen/gen_event_registry.py` fails here (and in the pre-commit
    `events-registry-fresh` hook)."""
    from scripts.codegen.gen_event_registry import render  # namespace package

    generated = render()
    current = _REGISTRY.read_text(encoding="utf-8")
    assert current == generated, (
        "base/events/registry.md is out of sync with the EVENTS registry — "
        "run .venv/bin/python scripts/codegen/gen_event_registry.py and commit the "
        "regenerated doc in the same PR."
    )


_ATTRIBUTES_KEY_RE = re.compile(r"""attributes(?:->>|\s*\?\s*)'([^']+)'""")

# The SQL-fragment definition site — `_sql_keys` builds `attributes->>'<key>'`
# from registered payload keys; every other file must not hand-write literals.
_EXEMPT_SQL_KEY_FILES = frozenset({"base/events/contract.py"})


def test_attributes_key_literals_are_registered() -> None:
    """SQL key-injection gate: every attributes key literal (the `->>`
    and `?` forms) in the repo must be a declared payload key.

    A reader referencing a key no producer declares is a contract violation,
    not a query detail — it would silently NULL out after a payload rename.
    New read sites should consume the per-event SQL fragment constants
    (LLM_USAGE_KEYS etc.) from base/events/contract.py instead of writing
    literals; this gate is the safety net for hand-written ones.
    """
    unregistered: list[tuple[str, int, str]] = []
    for path in sorted(_REPO.rglob("*.py")):
        rel = path.relative_to(_REPO).as_posix()
        if any(
            seg in rel
            for seg in (
                ".venv",
                ".git",
                ".worktrees",
                "__pycache__",
                ".cache",
            )
        ):
            continue
        if rel in _EXEMPT_SQL_KEY_FILES:
            continue
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
        ):
            if line.lstrip().startswith("#"):
                continue
            for match in _ATTRIBUTES_KEY_RE.finditer(line):
                key = match.group(1)
                if key not in registered_payload_keys():
                    unregistered.append((rel, lineno, key))
    assert not unregistered, (
        "attributes key literal(s) not declared in any payload TypedDict "
        f"(base/events/contract.py): {unregistered[:10]}. Declare the key "
        "on the event's payload TypedDict, or use the registry SQL fragment "
        "constants."
    )


# ── fifth guard — no label-only records ────────────────────────────────────


def test_no_logger_call_names_its_event_through_label() -> None:
    """Label-only gate: `label` never names an event. A loguru record call
    whose merged chain keywords carry `label=` and no `event=` logs as a bare
    `log` event, so the call must pass an explicit registered `event=`."""
    findings = scan_kinds.scan_label_only_calls(_REPO)
    assert not findings, (
        "logger call(s) passing label= without event=: "
        f"{[(f.path, f.lineno, f.label) for f in findings]}. "
        "Pass event=<registered name>."
    )


# The guard's scanner, pinned on synthetic sources — positive (literal and
# dynamic labels) and negative (event= chains, non-logger `label=`
# surfaces). A scanner regression must not ride on the repo-wide scan alone.

_SYNTHETIC_IMPORT = "from base.log import logger\n"


def test_label_scan_flags_literal_and_dynamic_labels() -> None:
    """Literal label-only calls are found; a non-literal label is reported as
    dynamic (label=None)."""
    findings = scan_kinds.find_label_only_calls(
        _SYNTHETIC_IMPORT
        + 'logger.info("[{label}] {body}", label="exec", body="x")\n'
        + 'logger.warning("w", label=f"kind-{value}")\n'
        + 'logger.bind(label="bound").error("e")\n'
        + 'logger.opt(colors=True).info("m", label="opt-chain")\n',
        filename="synthetic.py",
    )
    assert [(f.lineno, f.label) for f in findings] == [
        (2, "exec"),
        (3, None),
        (4, "bound"),
        (5, "opt-chain"),
    ]


def test_label_scan_merges_chains_and_skips_event_or_non_logger() -> None:
    """Chains merge in write order (later link wins); any `event=` in the
    chain exempts the call; calls rooted outside the logger set are not
    matched (the ServiceProbe-style surfaces with their own `label=`)."""
    findings = scan_kinds.find_label_only_calls(
        _SYNTHETIC_IMPORT
        + 'logger.bind(label="outer").bind(label="inner").info("m")\n'
        + 'logger.bind(label="bound").info("m", label="call")\n'
        + 'logger.bind(event="registered_thing").info("m")\n'
        + 'logger.bind(event="registered_thing").info("m", label="x")\n'
        + 'logger.info("m")\n'
        + 'client.info("m", label="x")\n'
        + 'ServiceProbe(label="probe")\n'
        + '_render_skill_bodies(label="nope")\n',
        filename="synthetic.py",
    )
    assert [(f.lineno, f.label) for f in findings] == [(2, "inner"), (3, "call")]


def test_label_scan_covers_loguru_imports_and_module_aliases() -> None:
    """Every logger-root spelling is matched: the loguru-direct import, a
    renamed import, a `base.log` module alias, and the literal
    `base.log.logger` attribute chain."""
    findings = scan_kinds.find_label_only_calls(
        "import base.log as _log\n"
        "import base.log\n"
        "from loguru import logger as lg\n"
        "from base.log import logger\n"
        '_log.logger.info("m", label="alias")\n'
        'base.log.logger.warning("m", label="attr-chain")\n'
        'lg.info("m", label="renamed-import")\n'
        'logger.info("m", label="plain")\n',
        filename="synthetic.py",
    )
    assert [(f.lineno, f.label) for f in findings] == [
        (5, "alias"),
        (6, "attr-chain"),
        (7, "renamed-import"),
        (8, "plain"),
    ]
