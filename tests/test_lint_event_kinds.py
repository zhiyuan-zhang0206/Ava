"""Event-registry zero-enforcement gate — R2-C \u5355\u4e00\u4e8b\u5b9e\u6e90\u7eaa\u5f8b.

`base/events/contract.py` \u7684 `EVENTS` \u662f event_name \u552f\u4e00\u4e8b\u5b9e\u6765\u6e90\uff1b\u672c\u6a21\u5757\u662f
**\u9a8c\u8bc1\u8005**\uff08\u4e0d\u662f\u63a5\u7f1d——\u6ce8\u518c\u8868\u4e4b\u5916\u4e0d\u518d\u6709\u624b\u5de5\u5feb\u7167\uff09\u3002\u4e94\u4e2a\u5b88\u536b\uff1a

1. **\u524d\u5411**\uff1a\u751f\u4ea7\u4ee3\u7801\u91cc\u6bcf\u4e2a\u9759\u6001 `event=` \u5b57\u9762\u91cf\u5fc5\u987b\u5df2\u6ce8\u518c\u3002\u672a\u6ce8\u518c =
   `telemetry.emit` fail-fast\uff08\u65b0\u4ee3\u7801\uff09\u6216\u9759\u9ed8\u843d category=log 30d\uff08\u65e7\u8def\u5f84\uff09\uff0c
   \u4e8c\u8005\u90fd\u662f\u4e8b\u6545\u3002
2. **\u53cd\u5411**\uff1a`EVENTS` \u6bcf\u4e2a\u6761\u76ee\u5fc5\u987b\u6709\u751f\u4ea7\u8005\uff08`event=` / `label=` /
   `event_type=` \u5b57\u9762\u91cf\uff0c\u6216\u4e0b\u65b9 SQL/\u52a8\u6001\u6e05\u5355\uff09——\u9632\u5220\u9664/\u6539\u540d\u4e8b\u4ef6\u7559\u4e0b\u6ce8\u518c\u8868
   \u6b8b\u7559\uff08category \u6620\u5c04\u7ee7\u7eed\u5b58\u6d3b\u4e00\u4e2a\u65e0\u4eba\u4ea7\u751f\u7684\u4e8b\u4ef6\uff09\u3002
3. **\u6587\u6863\u6f02\u79fb**\uff1a`base/events/registry.md` \u5fc5\u987b\u4e0e\u751f\u6210\u5668\u8f93\u51fa\u9010\u5b57\u4e00\u81f4\uff08R2-C\uff1a
   \u6587\u6863\u662f\u751f\u6210\u7269\uff09\u3002
4. **SQL \u952e\u6ce8\u5165**\uff1a\u8bfb\u53d6\u7aef SQL \u91cc\u7684 attributes \u952e\u5b57\u9762\u91cf\uff08`->>'` \u4e0e `?`
   \u4e24\u79cd\u5f62\u6001\uff09\u5fc5\u987b\u662f\u4e00\u4e2a payload TypedDict \u7684\u58f0\u660e\u952e\uff08`registered_payload_keys`\uff09
   ——"\u6539\u540d\u4e8b\u4ef6 = \u6ce8\u518c\u8868\u4e00\u884c + \u6240\u6709\u5f15\u7528\u70b9\u7f16\u8bd1/\u6d4b\u8bd5\u5931\u8d25"\u7684 SQL \u534a\u8fb9\u3002
5. **label-only \u6d3e\u751f**\uff1a\u751f\u4ea7\u4ee3\u7801\u91cc loguru \u8bb0\u5f55\u8c03\u7528\u94fe\uff08`logger[.bind/.opt]*(...).<level>`\uff09\u5408\u5e76\u540e\u7684
   keywords \u542b `label=`\u3001\u4e0d\u542b `event=` \u65f6\uff0clabel \u5fc5\u987b\u662f `EVENTS` \u4e2d\u7684\u5df2\u6ce8\u518c\u540d\uff1b\u52a8\u6001\uff08\u975e\u5b57\u9762\u91cf\uff09
   label fail-closed——\u9759\u6001\u4e0d\u53ef\u8bc1\u3002\u5426\u5219 sink \u5185 emit \u629b ValueError\uff1a\u884c\u4e22\u5931 + traceback\u3002

\u9650\u5236\uff08\u7ee7\u627f\u81ea scan_kinds\uff09\uff1a\u9759\u6001\u5b57\u9762\u91cf\u626b\u63cf\u770b\u4e0d\u5230\u52a8\u6001\u4ea7\u751f\u7684\u540d\u5b57\uff08\u53d8\u91cf
`event_type`\u3001dict \u503c\u3001\u4e09\u5143\u8868\u8fbe\u5f0f\uff09——\u5b83\u4eec\u5728 `_SQL_OR_DYNAMIC_KINDS` \u91cc\u9010\u6761
\u6807\u6ce8\u4ea7\u751f\u70b9\u3002
"""

from __future__ import annotations

import re
from pathlib import Path

from base.events import scan_kinds  # namespace package — pythonpath = ["."]
from base.events.contract import EVENTS, registered_payload_keys, telemetry_events

_REPO = Path(__file__).resolve().parents[1]
_REGISTRY = _REPO / "base" / "events" / "registry.md"

# Kinds whose production site has no static `event=` literal, so scan_kinds
# cannot see them, carry their emission site on the declaration (`EventSpec.site`);
# retired process-runner kinds are marked `retired` — their contracts stay readable
# for existing DB/OTLP history, and they are not live producers or permission to
# emit new process-runner events. Removing a kind from the code means removing the
# declaration (the reverse gate flags an orphaned entry).
_SQL_OR_DYNAMIC_KINDS = frozenset(name for name, spec in EVENTS.items() if spec.site)
_RETIRED_PROCESS_KINDS = frozenset(name for name, spec in EVENTS.items() if spec.retired)


def _code_kinds() -> tuple[set[str], set[str], set[str]]:
    """(event= literals, label= literals, event_type= literals) from scan_kinds."""
    event_kinds, label_kinds, event_type_kinds, _sse_roles = scan_kinds.scan_code(_REPO)
    return set(event_kinds), set(label_kinds), set(event_type_kinds)


def test_every_static_event_kind_is_registered() -> None:
    """Forward gate: an `event=` kind outside `EVENTS` silently falls to
    category=log (30d retention) on the loguru path, and `telemetry.emit`
    fail-fasts on it elsewhere — either way it is a contract violation."""
    event_kinds, _, _ = _code_kinds()
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
    `event=` literal, a `label=` fallback, an `event_type=` literal, or a
    documented SQL/dynamic emission. Catches stale registry entries (deleted
    or renamed kind still listed) that would keep the category mapping alive
    for a kind nobody emits."""
    event_kinds, label_kinds, event_type_kinds = _code_kinds()
    produced = event_kinds | label_kinds | event_type_kinds | _SQL_OR_DYNAMIC_KINDS
    assert not (_RETIRED_PROCESS_KINDS & produced), "retired process events have a new producer"
    produced |= _RETIRED_PROCESS_KINDS
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


# ── fifth guard — label-only derivation ─────────────────────────────────────


def test_label_only_calls_derive_registered_names() -> None:
    """Label-only gate: when a loguru record call's merged chain keywords
    carry `label=` and no `event=`, the label is the runtime fallback event
    name (resolution: event -> label -> "log"), so it must be registered — an
    unregistered label raises inside the sink and the row is lost with a
    traceback. A non-literal (dynamic) label fails closed: pass an explicit
    `event=` or a registered literal label."""
    findings = scan_kinds.scan_label_only_calls(_REPO)
    dynamic = [(f.path, f.lineno) for f in findings if f.label is None]
    assert not dynamic, (
        "label-only call(s) with a dynamic (non-literal) label — the static "
        f"gate cannot prove they are registered: {dynamic}. Pass an explicit "
        "event= or a literal label that is registered in EVENTS."
    )
    unregistered = [
        (f.path, f.lineno, f.label)
        for f in findings
        if f.label is not None and f.label not in EVENTS
    ]
    assert not unregistered, (
        "label-only call(s) with an unregistered label: "
        f"{unregistered}. Register the label in a base/events/declarations "
        "module (one EventSpec line), or pass an explicit event=."
    )


# The guard's scanner, pinned on synthetic sources — positive (literal),
# fail-closed (dynamic), and negative (event= chains, non-logger `label=`
# surfaces). A scanner regression must not ride on the repo-wide scan alone.

_SYNTHETIC_IMPORT = "from base.log import logger\n"


def test_label_scan_flags_literal_and_dynamic_labels() -> None:
    """Literal label-only calls are found; a non-literal label is reported as
    dynamic (label=None) — the fail-closed case the guard rejects."""
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
