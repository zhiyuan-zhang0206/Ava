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
- Consume a popped value with the .env-file fallback when the read must stay in
  a gateway process (base.host.env.runtime_config.read_env_aliases) and register it in
  _FALLBACK_CONSUMED_READS — an explicit, pinned exemption, never a blanket skip

This is the structural enforcement the orchestrator asked for — the consumption
matrix is the true source of ownership.

On 2026-09-06, four failures on ``refs/pull/1871/merge`` correctly caught
``base/deploy/timing.py`` entering the gateway closure through a schedule-manager
import before the PR's in-branch fix landed. That episode was a real guard
finding, not a flake; do not weaken the scan to make such failures disappear.
"""

from __future__ import annotations

import ast
import os
from collections.abc import Callable
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
    "services/entrypoints/im_bridge/",
    "services/wake/heartbeat/",
    "services/derived/labeler/",
    "services/upkeep/events_maintenance/",
    "services/derived/memory_indexer/",
    "services/derived/memory_search/",
    "services/wake/delivery_watchdog/",
    "services/frontend/",
    "services/backup/artifact/",
    "services/backup/walg/",
)


def _repo_root() -> Path:
    # This test lives at cli/commands/lifecycle/tests/startup/test_gateway_consumer_guard.py — three
    # levels below the repo root. parent.parent would land on tests/ and the
    # scan would silently cover nothing (the guard became a no-op and let the
    # GEMINI_API_KEY / AVA_MODEL / AVA_LABELER_MODEL P0 through on 2026-08-06).
    return Path(__file__).resolve().parents[5]


def _production_py_files(root: Path, prefix: str) -> list[Path]:
    """Every .py file under one source root, a package's own `tests/` excluded."""
    target = root / prefix
    if not target.is_dir():
        return []
    return [py for py in target.rglob("*.py") if "tests" not in py.relative_to(root).parts]


def _gateway_py_files() -> list[Path]:
    """Every .py file under gateway-side source roots (excluding tests)."""
    root = _repo_root()
    files: list[Path] = []
    for prefix in _GATEWAY_SOURCE_ROOTS:
        for py_file in _production_py_files(root, prefix):
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
            # reads the domain inside that branch (e.g. ava/skills/composer_commands.py's
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
    .env file, like base/lm/factory.py after the gateway pop). The guard
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
    from base.config import _FIELDS, field_alias

    name = field  # field names are globally unique
    if name not in _FIELDS:
        return None
    return field_alias(name)


def _scan_floor() -> int:
    """The minimum number of files the gateway-side scan must cover.

    A path-depth regression in `_repo_root()` (2026-08-06: parent.parent on a
    tests/components/base/ file resolved to tests/, scanning 1 file) silently turns the
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
    from base.host.env.registry import agent_runner_cluster_aliases

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
# The direct scan above misses reads that happen inside base/ functions the
# gateway calls (2026-08-06 third cut: validate_model_config reads
# settings.lm.*_api_key and every spawn 400'd after the pop). This scan walks
# the repo-internal import closure of the gateway-side roots and flags
# settings.<domain>.<field> reads whose alias is in the pop set, minus an
# explicit allowlist of modules that are import-reachable but agent-only at
# runtime (never executed by a gateway process).
_AGENT_ONLY_ALLOWLIST = frozenset(
    {
        # build_chat_model — agent-side only; the gateway reaches
        # base/lm/factory.py solely for validate_model_config, which reads
        # keys via get_field with a .env-file fallback (see
        # base/lm/tests/test_lm_factory.py).
        "base/lm/_providers.py",
        # The two settings reads here live in chrome_mcp_socket() /
        # permissions_helper_socket() — socket-path helpers called only by
        # agent-runner-side services (browser MCP daemon, permissions helper).
        # Verified 2026-08-06: no gateway-side caller in the import closure.
        "base/paths/__init__.py",
    }
)

# Reads of a popped alias that are deliberately fallback-consumed: the module
# reads the Settings value and — when the gateway profile has popped the
# env var (Task #856) — falls back to this unit's `.env` file via
# `base.host.env.runtime_config.read_env_aliases()`, the sanctioned gateway-side
# source (same shape as base/lm/factory.py::_ensure_provider_key). Each
# entry is an explicit, reviewable exemption keyed by (module, field); the
# companion test below pins every entry to fallback code that actually
# exists, so the registry can never outlive the code it excuses.
_FALLBACK_CONSUMED_READS: dict[tuple[str, str], str] = {
    ("ops/lifecycle/billing_recovery.py", "deepseek_api_key"): (
        "the provider-balance probe runs in-process on the gateway (the POST "
        "route executes it); falls back to the unit .env file (task #3956)"
    ),
}


_GATEWAY_CLOSURE_PACKAGES = (
    "base",
    "gateway",
    "services",
    "agent",
    "ops",  # gateway HTTP routes execute ops/* in-process (task #3956)
)


def _module_name_of(path: Path) -> str:
    return str(path.relative_to(_repo_root()).with_suffix("")).replace("/", ".")


def _resolve_module_file(name: str) -> Path | None:
    """The repo file a dotted module name resolves to, or None if it is external."""
    root = _repo_root()
    as_module = root / (name.replace(".", "/") + ".py")
    if as_module.exists():
        return as_module
    as_package = root / name.replace(".", "/") / "__init__.py"
    if as_package.exists():
        return as_package
    return None


# Import edges the closure walk does not follow because the importing function never runs in a
# gateway process: (importing file, imported module) -> (function holding the import, why). Narrow
# on purpose: one edge, one function. `ava/sdk_surface/settings.py` reaches `ava.shell.sessions` (and through
# it ava.security's `settings.agent` read) only inside `shell_sessions()`. The gateway reaches
# `ava.sdk_surface.settings` solely because `ava.skills` imports it to record `skill_invoked`, which the
# gateway never does (it imports `ava.skills.composer_commands` for /api/commands). Pinned below:
# the import must still sit in that function and no gateway-side source may name the function.
_GATEWAY_UNREACHABLE_EDGES: dict[tuple[str, str], tuple[str, str]] = {
    ("ava/sdk_surface/settings.py", "ava.shell.sessions"): (
        "shell_sessions",
        "agent-process shell sessions; the gateway never asks for an agent's shell",
    ),
}


def _walk_import_closure(
    roots: tuple[str, ...],
    imported_modules: Callable[[ast.AST], list[str]],
) -> set[Path]:
    """Root .py files + every repo file they transitively import.

    `imported_modules` maps one AST node to the dotted module names it pulls in
    (empty for non-import nodes), so each closure scan owns its import policy.
    """
    root = _repo_root()
    frontier: list[str] = []
    for prefix in roots:
        frontier.extend(_module_name_of(py) for py in _production_py_files(root, prefix))
    seen: set[str] = set()
    closure: set[Path] = set()
    while frontier:
        m = frontier.pop()
        if m in seen:
            continue
        seen.add(m)
        p = _resolve_module_file(m)
        if p is None:
            continue
        closure.add(p)
        try:
            tree = ast.parse(p.read_text(errors="replace"))
        except (OSError, SyntaxError):
            continue
        rel = p.relative_to(root).as_posix()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and (rel, node.module) in _GATEWAY_UNREACHABLE_EDGES
            ):
                continue
            frontier.extend(imported_modules(node))
    return closure


def _gateway_closure_imports(node: ast.AST) -> list[str]:
    if isinstance(node, ast.ImportFrom):
        if node.module and node.module.split(".")[0] in _GATEWAY_CLOSURE_PACKAGES:
            return [node.module]
        return []
    if isinstance(node, ast.Import):
        return [
            alias.name
            for alias in node.names
            if alias.name.split(".")[0] in _GATEWAY_CLOSURE_PACKAGES
        ]
    return []


@lru_cache
def _repo_internal_import_closure(roots: tuple[str, ...]) -> set[Path]:
    """Modules (repo-internal) transitively imported by the gateway-side roots."""
    return _walk_import_closure(roots, _gateway_closure_imports)


def test_gateway_closure_reads_do_not_hit_popped_keys() -> None:
    """settings reads in base/ modules reachable from gateway-side code must
    not resolve to popped aliases, unless the module is agent-only at runtime."""
    from base.config import _FIELDS, field_alias
    from base.host.env.registry import agent_runner_cluster_aliases

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
                if (rel, field) in _FALLBACK_CONSUMED_READS:
                    # Explicit, reviewable exemption: the module consumes this
                    # value with the .env-file fallback (pinned below) — not a
                    # blanket skip; the entry names both module and field.
                    continue
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
            "\nFix: consume the key with a .env-file fallback and register the read "
            "in _FALLBACK_CONSUMED_READS (explicit + pinned; see "
            "base/runtime_config.read_env_aliases), change the field's capability, "
            "or add the module to _AGENT_ONLY_ALLOWLIST only if it is genuinely "
            "never executed by a gateway process."
        )
        pytest.fail(msg)


# ── Profile domain sets == consumption matrix (bidirectional, Task #856 PR-B) ──
#
# PROCESS_PROFILES in base/config names which config DOMAINS each process kind
# constructs. The sets must equal the consumption matrix: the domains actually
# read by that kind's source + repo-internal import closure (base/ code runs
# in every kind). Two directions, both asserted:
#   1. matrix ⊆ profile — a new settings.<domain> read in a kind's code without
#      the domain in its profile would fail at runtime (fail-fast) after this
#      test fails first at CI.
#   2. profile ⊆ matrix — a domain in a profile that nothing consumes is dead
#      weight and hides future cross-profile reads (the capability-axis
#      confusion that caused the 2026-08-06 #1570 P0).
# The profile sets are NOT the capability display axis — capability is
# config-panel grouping only, orthogonal to process ownership (see
# base/config/__init__.py docstring).
_KIND_ROOTS: dict[str, tuple[str, ...]] = {
    "gateway": (
        "gateway/",
        "services/entrypoints/im_bridge/",
        "services/wake/heartbeat/",
        "services/derived/labeler/",
        "services/upkeep/events_maintenance/",
        "services/derived/memory_indexer/",
        "services/wake/delivery_watchdog/",
        "services/backup/artifact/",
        "services/backup/walg/",
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
        "services/agent_runner/agent_host/",
    ),
    "runner": (
        "ops/",
        "services/agent_runner/agent_ops/",
        "services/watchdog/",
        "services/desktop/browser/",
        "services/entrypoints/gate/",
        "services/desktop/permissions_helper/",
        "services/desktop/computer/",
        "services/supervision/healthchecks/",
    ),
}

# Repo-internal package prefixes the closure walk follows (everything that can
# be imported by a process of any kind).
_CLOSURE_PACKAGES = (
    "base",
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


def _kind_closure_imports(node: ast.AST) -> list[str]:
    if isinstance(node, ast.ImportFrom):
        if node.module and node.module.split(".")[0] in _CLOSURE_PACKAGES and node.level == 0:
            # `from base import telemetry` names a MODULE inside the
            # package — push the package AND the full dotted path, or the
            # submodule never enters the frontier (the existing gateway
            # closure scan had this blind spot: base/log/__init__.py's lazy
            # `from base import telemetry` did not pull telemetry.py in).
            return [node.module] + [
                f"{node.module}.{alias.name}" for alias in node.names if alias.name != "*"
            ]
        return []
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names if alias.name.split(".")[0] in _CLOSURE_PACKAGES]
    return []


@lru_cache
def _kind_closure(roots: tuple[str, ...]) -> set[Path]:
    """Root .py files + the repo-internal import closure, for one process kind."""
    return _walk_import_closure(roots, _kind_closure_imports)


def _closure_domains(closure: set[Path]) -> set[str]:
    """Config-domain reads, excluding declared aggregate runtime facts.

    Unknown aggregate attributes still enter the matrix and fail its profile
    check; only dump-excluded Settings fields outside the domain registry are
    classified as non-domain facts.
    """
    from base.config import Settings
    from base.host.env.config_registry import DOMAIN_ATTRS

    aggregate_facts = {
        name
        for name, field in Settings.model_fields.items()
        if field.exclude is True and name not in DOMAIN_ATTRS
    }
    domains: set[str] = set()
    for py_file in closure:
        try:
            src_text = py_file.read_text(errors="replace")
        except OSError:
            continue
        for domain, _field in _extract_settings_reads(src_text):
            if domain not in aggregate_facts:
                domains.add(domain)
    return domains


def test_profile_consumption_distinguishes_runtime_facts_from_domains(tmp_path: Path) -> None:
    """Declared boot facts are not domains; an unknown domain stays visible."""
    source = tmp_path / "consumer.py"
    source.write_text(
        "settings.env_boot.db_authority_refusal\n"
        "settings.data_plane.db_url\n"
        "settings.undeclared_domain.value\n"
    )
    assert _closure_domains({source}) == {"data_plane", "undeclared_domain"}


def test_agent_host_launches_under_the_agent_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hosted agent-host daemon consumes the agent domain set (it runs the
    agent kernel in-process, and services/agent_runner/agent_host/ is in the agent kind's
    roots above). The root launcher, its only launch path, must therefore
    start it with the `agent` profile — a `runner` profile crashed it at import
    (2026-08-30 soak startup), and a marker-less launch (full construction)
    would silently mask any future cross-profile read instead of failing fast."""
    from base.config import PROCESS_PROFILES
    from cli.commands.lifecycle import root_driver
    from ops.roster import build_services

    def delivery(_cls: str) -> dict[str, str]:
        return {"AVA_DB_URL": "postgresql://ava_g0_runner@fixture/ava"}

    def token_delivery(_cls: str) -> dict[str, str]:
        return {"AVA_API_TOKEN": "fixture-token"}

    # The launcher binds the database login and API token next to the marker;
    # neither delivery is under test, so keep them independent of this host's
    # home (a scratch dir with no installed unit capability, which
    # api_delivery would otherwise refuse).
    monkeypatch.setattr("cli.commands.data_plane.bringup.db_delivery", delivery)
    monkeypatch.setattr("cli.commands.data_plane.bringup.api_delivery", token_delivery)
    agent_host = next(spec for spec in build_services() if spec.session == "agent-host")
    profile = root_driver._service_extra_env(agent_host)["AVA_PROCESS_PROFILE"]
    assert profile in PROCESS_PROFILES, f"{profile} is not a process profile"
    assert profile == "agent"


def test_profile_domains_match_consumption_matrix() -> None:
    """PROCESS_PROFILES domains == domains each kind's code + closure consumes."""
    from base.config import PROCESS_PROFILES
    from base.config.profiles import ProcessProfile

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


def _function_imports(tree: ast.AST, function: str, module: str) -> bool:
    """Whether a function named `function` in `tree` holds a `from <module> import ...`."""
    return any(
        isinstance(inner, ast.ImportFrom) and inner.module == module
        for holder in ast.walk(tree)
        if isinstance(holder, ast.FunctionDef | ast.AsyncFunctionDef) and holder.name == function
        for inner in ast.walk(holder)
    )


def _uses_module_function(node: ast.AST, owner: str, function: str) -> bool:
    """Whether `node` imports `function` from module `owner` or reads it off `owner`'s name."""
    if isinstance(node, ast.ImportFrom):
        return node.module == owner and any(alias.name == function for alias in node.names)
    if isinstance(node, ast.Attribute) and node.attr == function:
        leaf = owner.rsplit(".", 1)[1]
        holder = node.value
        return (isinstance(holder, ast.Name) and holder.id == leaf) or (
            isinstance(holder, ast.Attribute) and holder.attr == leaf
        )
    return False


def test_gateway_unreachable_edges_stay_pinned_to_real_code() -> None:
    """Every _GATEWAY_UNREACHABLE_EDGES entry must still be a real edge in its named function,
    and no gateway-side source may use that function of that module.

    The registry excuses one import edge from the closure walk; this pin fails when the import
    moves or disappears (stale entry) or when gateway code starts calling the function (the
    excuse no longer holds).
    """
    root = _repo_root()
    gateway_nodes = [
        node
        for prefix in _KIND_ROOTS["gateway"]
        for py in _production_py_files(root, prefix)
        for node in ast.walk(ast.parse(py.read_text(errors="replace")))
    ]
    for (rel, module), (function, why) in _GATEWAY_UNREACHABLE_EDGES.items():
        tree = ast.parse((root / rel).read_text(errors="replace"))
        assert _function_imports(tree, function, module), (
            f"{rel}: {function}() no longer imports {module} ({why})"
        )
        owner = Path(rel).with_suffix("").as_posix().replace("/", ".")
        assert not any(_uses_module_function(node, owner, function) for node in gateway_nodes), (
            f"gateway-side code uses {owner}.{function}: the {rel} -> {module} edge is "
            f"reachable now ({why})"
        )


def test_fallback_consumed_reads_stay_pinned_to_real_fallback_code() -> None:
    """Every _FALLBACK_CONSUMED_READS entry must cite fallback code that exists.

    The registry excuses a specific (module, field) read from the popped-alias
    scan; this pin fails the moment the fallback call disappears, so an entry
    can never silently outlive the code it excuses (no generically weakened
    scan).
    """
    root = _repo_root()
    for (rel, field), why in _FALLBACK_CONSUMED_READS.items():
        src = (root / rel).read_text(errors="replace")
        assert "read_env_aliases" in src, f"{rel}: no read_env_aliases fallback ({why})"
        assert f'field_alias("{field}")' in src, (
            f"{rel}: fallback does not name field {field!r} ({why})"
        )
