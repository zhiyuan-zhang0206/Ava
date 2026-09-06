"""Guard: gateway-side services must not read agent-runner cluster keys.

If a gateway daemon (im_bridge, heartbeat, etc.) reads a settings field whose
alias is in AGENT_RUNNER_CLUSTER_ALIASES, the gateway pop would remove that
field's env var — and the daemon would get a default value instead of the
operator-configured one. This test scans gateway-side source for settings reads
and asserts none of them resolve to agent-runner cluster aliases.

The "right" fix when this fires is either:
- Change the domain's default capability to "gateway" in _DOMAIN_MODELS (if the
  field is genuinely owned by gateway-side code, like telegram/feishu for im_bridge)
- Move the field to a gateway-scope domain (services, daemon, gateway)
- Move the read to a different process (agent-side code should not run in gateway)

This is the structural enforcement the orchestrator asked for — the consumption
matrix is the true source of ownership.

On 2026-09-06, four failures on ``refs/pull/1871/merge`` correctly caught
``shared/timing.py`` entering the gateway closure through a schedule-manager
import before the PR's in-branch fix landed. That episode was a real guard
finding, not a flake; do not weaken the scan to make such failures disappear.
"""

from __future__ import annotations

import ast
import os
from functools import lru_cache
from pathlib import Path
from typing import cast

import pytest

# Intentional import-time isolation: pin settings-lite before any project import
# can construct config from an inherited environment.
os.environ["AVA_CONFIG_FETCH"] = (
    "skip"  # assignment, not setdefault: a setdefault would silently keep an inherited value (the login-shell .env leak class) instead of pinning settings-lite
)

# Gateway-side source roots — daemons that run under the gateway profile.
_GATEWAY_SOURCE_ROOTS = (
    "gateway/",
    "services/im_bridge/",
    "services/heartbeat/",
    "services/labeler/",
    "services/events_maintenance/",
    "services/memory_indexer/",
    "services/memory_search/",
    "services/milvus/",
    "services/delivery_watchdog/",
    "services/frontend/",
    "services/pitr/",
)


def _repo_root() -> Path:
    # This test lives at tests/shared/test_gateway_consumer_guard.py — three
    # levels below the repo root. parent.parent would land on tests/ and the
    # scan would silently cover nothing (the guard became a no-op and let the
    # GEMINI_API_KEY / AVA_MODEL / AVA_LABELER_MODEL P0 through on 2026-08-06).
    return Path(__file__).resolve().parent.parent.parent


def _gateway_py_files() -> list[Path]:
    """Every .py file under gateway-side source roots (excluding tests)."""
    root = _repo_root()
    files: list[Path] = []
    for prefix in _GATEWAY_SOURCE_ROOTS:
        target = root / prefix
        if target.is_dir():
            for py_file in target.rglob("*.py"):
                if "test_" not in str(py_file) and not str(py_file).endswith("_test.py"):
                    files.append(py_file)
    return files


def _extract_settings_reads(source: str) -> list[tuple[str, str]]:
    """Parse Python source and return [(domain, field), ...] for every
    `settings.<domain>.<field>` attribute access."""
    # AST-based extraction — more reliable than regex for nested access.
    # We look for Attribute nodes where the value is `settings.<domain>.<field>`.
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    results: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        # Check if this is settings.<domain>.<field>
        if not isinstance(node.value, ast.Attribute):
            continue
        if not isinstance(node.value.value, ast.Name):
            continue
        if node.value.value.id != "settings":
            continue
        domain = node.value.attr
        field = node.attr
        if _guarded_by_has_domain(tree, node, domain):
            # Sanctioned cross-profile read: the module first checks
            # `settings.has_domain("<domain>")` in an enclosing `if` and only
            # reads the domain inside that branch (e.g. ava/_commands.py's
            # commands_enabled for the gateway's /api/commands dropdown).
            # The gate proves the author thought about the profile boundary.
            continue
        results.append((domain, field))
    return results


def _guarded_by_has_domain(tree: ast.AST, node: ast.AST, domain: str) -> bool:
    """Whether `node` sits inside an `if settings.has_domain("<domain>"):` guard.

    The sanctioned cross-profile pattern (Task #856): a module that genuinely
    needs one field from a domain its process profile excludes checks
    `settings.has_domain(<domain>)` first and reads only inside the branch —
    the alternative branch resolves the value profile-safely (e.g. from the
    .env file, like shared/lm/factory.py after the gateway pop). The guard
    proves the author thought about the profile boundary, so the read is not
    a fail-fast violation. Anything else — an unguarded read — stays flagged.
    """
    parents: dict[int, ast.AST] = {}

    def _link(parent: ast.AST) -> None:
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent
            _link(child)

    _link(tree)
    parent = parents.get(id(node))
    while parent is not None:
        if isinstance(parent, ast.If):
            test = parent.test
            if (
                isinstance(test, ast.Call)
                and isinstance(test.func, ast.Attribute)
                and test.func.attr == "has_domain"
                and len(test.args) == 1
                and isinstance(test.args[0], ast.Constant)
                and test.args[0].value == domain
            ):
                return True
        parent = parents.get(id(parent))
    return False


def _settings_field_alias(domain: str, field: str) -> str | None:
    """Return the env alias for a settings field, or None if not found."""
    from shared.config import _FIELDS, field_alias

    name = field  # field names are globally unique
    if name not in _FIELDS:
        return None
    return field_alias(name)


def _scan_floor() -> int:
    """The minimum number of files the gateway-side scan must cover.

    A path-depth regression in `_repo_root()` (2026-08-06: parent.parent on a
    tests/shared/ file resolved to tests/, scanning 1 file) silently turns the
    guard into a no-op and lets the gateway pop remove keys gateway-side
    processes consume (P0: GEMINI_API_KEY/AVA_MODEL/AVA_LABELER_MODEL). The
    floor is well under today's 92 files so a legitimate new service or a
    directory rename has slack, but any root-resolution break collapses below
    it and fails here."""
    return 50


def test_gateway_services_dont_read_agent_runner_cluster_keys() -> None:
    """Every settings read in gateway-side source must NOT resolve to an
    agent-runner cluster alias — those aliases are popped from the gateway
    process's os.environ."""
    from shared.env_registry import agent_runner_cluster_aliases

    runner_cluster_aliases = agent_runner_cluster_aliases()

    files = _gateway_py_files()
    assert len(files) >= _scan_floor(), (
        f"guard scan covers only {len(files)} files (< {_scan_floor()}) — "
        f"_repo_root() likely resolves to the wrong directory again; a shrunken "
        f"scan silently disables this guard (the 2026-08-06 P0 failure mode)"
    )

    violations: list[tuple[str, str, str]] = []  # (file, domain.field, alias)

    for py_file in files:
        try:
            source = py_file.read_text()
        except OSError:
            continue
        for domain, field in _extract_settings_reads(source):
            alias = _settings_field_alias(domain, field)
            if alias is None:
                continue
            if alias in runner_cluster_aliases:
                violations.append(
                    (
                        str(py_file.relative_to(_repo_root())),
                        f"{domain}.{field}",
                        alias,
                    )
                )

    if violations:
        msg = (
            f"Gateway-side code reads {len(violations)} field(s) whose env aliases "
            f"are in the agent-runner cluster aliases. The gateway pop would remove "
            f"these from os.environ, causing the daemon to read a default value "
            f"instead of the operator-configured one:\n"
        )
        for file, access, alias in sorted(violations):
            msg += f"  {file}: settings.{access} → {alias}\n"
        msg += (
            "\nFix: change the domain's default capability to 'gateway' in "
            "_DOMAIN_MODELS, or move the field to a gateway-scope domain."
        )
        pytest.fail(msg)


# ── Indirect consumption: settings reads in the gateway import closure ──
#
# The direct scan above misses reads that happen inside shared/ functions the
# gateway calls (2026-08-06 third cut: validate_model_config reads
# settings.lm.*_api_key and every spawn 400'd after the pop). This scan walks
# the repo-internal import closure of the gateway-side roots and flags
# settings.<domain>.<field> reads whose alias is in the pop set, minus an
# explicit allowlist of modules that are import-reachable but agent-only at
# runtime (never executed by a gateway process).
_AGENT_ONLY_ALLOWLIST = frozenset(
    {
        # build_chat_model — agent-side only; the gateway reaches
        # shared/lm/factory.py solely for validate_model_config, which reads
        # keys via get_field with a .env-file fallback (see
        # tests/shared/test_lm_factory.py).
        "shared/lm/_providers.py",
        # The two settings reads here live in chrome_mcp_socket() /
        # permissions_helper_socket() — socket-path helpers called only by
        # agent-runner-side services (browser MCP daemon, permissions helper).
        # Verified 2026-08-06: no gateway-side caller in the import closure.
        "shared/paths.py",
    }
)


@lru_cache
def _repo_internal_import_closure(roots: tuple[str, ...]) -> set[Path]:
    """Modules (repo-internal) transitively imported by the gateway-side roots."""
    root = _repo_root()

    def module_of(path: Path) -> str:
        return str(path.relative_to(root).with_suffix("")).replace("/", ".")

    def resolve_import(name: str) -> Path | None:
        cand = root / (name.replace(".", "/") + ".py")
        if cand.exists():
            return cand
        cand2 = root / name.replace(".", "/") / "__init__.py"
        if cand2.exists():
            return cand2
        return None

    frontier: list[str] = []
    for prefix in roots:
        target = root / prefix
        if target.is_dir():
            for py in target.rglob("*.py"):
                frontier.append(module_of(py))
    seen: set[str] = set()
    closure: set[Path] = set()
    while frontier:
        m = frontier.pop()
        if m in seen:
            continue
        seen.add(m)
        p = resolve_import(m)
        if p is None:
            continue
        closure.add(p)
        try:
            tree = ast.parse(p.read_text(errors="replace"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.split(".")[0]
                in (
                    "shared",
                    "gateway",
                    "services",
                    "agent",
                )
            ):
                frontier.append(node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in ("shared", "gateway", "services", "agent"):
                        frontier.append(alias.name)
    return closure


def test_gateway_closure_reads_do_not_hit_popped_keys() -> None:
    """settings reads in shared/ modules reachable from gateway-side code must
    not resolve to popped aliases, unless the module is agent-only at runtime."""
    from shared.config import _FIELDS, field_alias
    from shared.env_registry import agent_runner_cluster_aliases

    runner_cluster_aliases = agent_runner_cluster_aliases()

    closure = _repo_internal_import_closure(_GATEWAY_SOURCE_ROOTS)
    assert len(closure) >= 100, (
        f"import closure implausibly small ({len(closure)}) — root resolution regressed again"
    )
    violations: list[tuple[str, str, str]] = []
    for py_file in sorted(closure):
        rel = str(py_file.relative_to(_repo_root()))
        if rel in _AGENT_ONLY_ALLOWLIST:
            continue
        src = py_file.read_text(errors="replace")
        for domain, field in _extract_settings_reads(src):
            if field not in _FIELDS:
                continue
            alias = field_alias(field)
            if alias in runner_cluster_aliases:
                violations.append((rel, f"{domain}.{field}", alias))
    if violations:
        msg = (
            f"Gateway import closure reads {len(violations)} field(s) whose aliases "
            f"are in AGENT_RUNNER_CLUSTER_ALIASES — the gateway pop removes them "
            f"from os.environ (third cut, 2026-08-06):\n"
        )
        for file, access, alias in sorted(violations):
            msg += f"  {file}: settings.{access} → {alias}\n"
        msg += (
            "\nFix: consume the key with a .env-file fallback (see "
            "shared/lm/factory.py validate_model_config), change the field's "
            "capability, or add the module to _AGENT_ONLY_ALLOWLIST only if it "
            "is genuinely never executed by a gateway process."
        )
        pytest.fail(msg)


# ── Profile domain sets == consumption matrix (bidirectional, Task #856 PR-B) ──
#
# PROCESS_PROFILES in shared/config names which config DOMAINS each process kind
# constructs. The sets must equal the consumption matrix: the domains actually
# read by that kind's source + repo-internal import closure (shared/ code runs
# in every kind). Two directions, both asserted:
#   1. matrix ⊆ profile — a new settings.<domain> read in a kind's code without
#      the domain in its profile would fail at runtime (fail-fast) after this
#      test fails first at CI.
#   2. profile ⊆ matrix — a domain in a profile that nothing consumes is dead
#      weight and hides future cross-profile reads (the capability-axis
#      confusion that caused the 2026-08-06 #1570 P0).
# The profile sets are NOT the capability display axis — capability is
# config-panel grouping only, orthogonal to process ownership (see
# shared/config/__init__.py docstring).
_KIND_ROOTS: dict[str, tuple[str, ...]] = {
    "gateway": (
        "gateway/",
        "services/im_bridge/",
        "services/heartbeat/",
        "services/labeler/",
        "services/events_maintenance/",
        "services/memory_indexer/",
        "services/milvus/",
        "services/delivery_watchdog/",
        "services/pitr/",
    ),
    "agent": (
        "agent/",
        "ava/",
        "ava_builtins/",
        # The hosted agent-host daemon runs the agent kernel + plugins
        # in-process, so its config consumption is the agent kind's. Missing
        # from the roots it was invisible to the matrix: the healthcheck's
        # `runner` launch profile crashed at import (settings.agent read) and
        # CI could not see it (2026-08-30 soak startup).
        "services/agent_host/",
    ),
    "runner": (
        "ops/",
        "services/agent_ops/",
        "services/restarter/",
        "services/watchdog/",
        "services/browser/",
        "services/gate/",
        "services/permissions_helper/",
        "services/computer/",
        "services/healthchecks/",
    ),
}

# Repo-internal package prefixes the closure walk follows (everything that can
# be imported by a process of any kind).
_CLOSURE_PACKAGES = (
    "shared",
    "gateway",
    "services",
    "agent",
    "ops",
    "ava",
    "ava_builtins",
    "db",
    "ui",
    "cli",
)


@lru_cache
def _kind_closure(roots: tuple[str, ...]) -> set[Path]:
    """Root .py files + the repo-internal import closure, for one process kind."""
    root = _repo_root()

    def module_of(path: Path) -> str:
        return str(path.relative_to(root).with_suffix("")).replace("/", ".")

    def resolve_import(name: str) -> Path | None:
        cand = root / (name.replace(".", "/") + ".py")
        if cand.exists():
            return cand
        cand2 = root / name.replace(".", "/") / "__init__.py"
        if cand2.exists():
            return cand2
        return None

    frontier: list[str] = []
    for prefix in roots:
        target = root / prefix
        if target.is_dir():
            for py in target.rglob("*.py"):
                frontier.append(module_of(py))
    seen: set[str] = set()
    closure: set[Path] = set()
    while frontier:
        m = frontier.pop()
        if m in seen:
            continue
        seen.add(m)
        p = resolve_import(m)
        if p is None:
            continue
        closure.add(p)
        try:
            tree = ast.parse(p.read_text(errors="replace"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.split(".")[0] in _CLOSURE_PACKAGES
            ):
                # `from shared import telemetry` names a MODULE inside the
                # package — push the package AND the full dotted path, or the
                # submodule never enters the frontier (the existing gateway
                # closure scan had this blind spot: shared/log.py's lazy
                # `from shared import telemetry` did not pull telemetry.py in).
                if node.level == 0:
                    frontier.append(node.module)
                    for alias in node.names:
                        if alias.name != "*":
                            frontier.append(f"{node.module}.{alias.name}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in _CLOSURE_PACKAGES:
                        frontier.append(alias.name)
    return closure


def _closure_domains(closure: set[Path]) -> set[str]:
    """The set of `settings.<domain>` domains read anywhere in `closure`."""
    domains: set[str] = set()
    for py_file in closure:
        try:
            src_text = py_file.read_text(errors="replace")
        except OSError:
            continue
        for domain, _field in _extract_settings_reads(src_text):
            domains.add(domain)
    return domains


def test_agent_host_launches_under_the_agent_profile() -> None:
    """The hosted agent-host daemon consumes the agent domain set (it runs the
    agent kernel in-process, and services/agent_host/ is in the agent kind's
    roots above). Its healthcheck must therefore launch it with the `agent`
    profile — a `runner` profile crashed it at import (2026-08-30 soak startup),
    and a marker-less launch (full construction) would silently mask any future
    cross-profile read instead of failing fast."""
    from services.healthchecks.agent_host import _HOST_PROCESS_PROFILE
    from shared.config import PROCESS_PROFILES

    assert _HOST_PROCESS_PROFILE in PROCESS_PROFILES, (
        f"{_HOST_PROCESS_PROFILE} is not a process profile"
    )
    assert _HOST_PROCESS_PROFILE == "agent"


def test_profile_domains_match_consumption_matrix() -> None:
    """PROCESS_PROFILES domains == domains each kind's code + closure consumes."""
    from shared.config import PROCESS_PROFILES
    from shared.config.profiles import ProcessProfile

    for kind, roots in _KIND_ROOTS.items():
        closure = _kind_closure(roots)
        assert len(closure) >= 150, (
            f"{kind} closure implausibly small ({len(closure)}) — root resolution regressed again"
        )
        consumed = _closure_domains(closure)
        profile = set(PROCESS_PROFILES[cast(ProcessProfile, kind)])
        missing = consumed - profile
        assert not missing, (
            f"{kind} profile is missing domains its code consumes "
            f"(fail-fast would fire at runtime): {sorted(missing)}"
        )
        extra = profile - consumed
        assert not extra, (
            f"{kind} profile contains domains nothing in the kind's code or import "
            f"closure reads — a capability-axis artifact? Remove them or prove a "
            f"consumer: {sorted(extra)}"
        )
