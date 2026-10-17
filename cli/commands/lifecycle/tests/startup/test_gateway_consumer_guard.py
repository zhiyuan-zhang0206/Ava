from __future__ import annotations

import ast
import os
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import cast

import pytest

from cli.commands.data_plane import bringup

os.environ["MEIVA_CONFIG_FETCH"] = "skip"

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
    return Path(__file__).resolve().parents[5]


def _production_py_files(root: Path, prefix: str) -> list[Path]:
    target = root / prefix
    if not target.is_dir():
        return []
    return [py for py in target.rglob("*.py") if "tests" not in py.relative_to(root).parts]


def _gateway_py_files() -> list[Path]:
    root = _repo_root()
    files: list[Path] = []
    for prefix in _GATEWAY_SOURCE_ROOTS:
        for py_file in _production_py_files(root, prefix):
            if "test_" not in str(py_file) and not str(py_file).endswith("_test.py"):
                files.append(py_file)
    return files


def _extract_settings_reads(source: str) -> list[tuple[str, str]]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    results: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        if not isinstance(node.value, ast.Attribute):
            continue
        if not isinstance(node.value.value, ast.Name):
            continue
        if node.value.value.id != "settings":
            continue
        domain = node.value.attr
        field = node.attr
        if _guarded_by_has_domain(tree, node, domain):
            continue
        results.append((domain, field))
    return results


def _guarded_by_has_domain(tree: ast.AST, node: ast.AST, domain: str) -> bool:
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
    from base.config import FIELD_REFS, field_alias

    name = field
    if name not in FIELD_REFS:
        return None
    return field_alias(name)


def _scan_floor() -> int:
    return 50


def test_gateway_services_dont_read_agent_runner_cluster_keys() -> None:
    from base.host.env.registry import agent_runner_cluster_aliases

    runner_cluster_aliases = agent_runner_cluster_aliases()

    files = _gateway_py_files()
    assert len(files) >= _scan_floor(), (
        f"guard scan covers only {len(files)} files (< {_scan_floor()}) — "
        f"_repo_root() likely resolves to the wrong directory again; a shrunken "
        f"scan silently disables this guard (the 2026-08-06 P0 failure mode)"
    )

    violations: list[tuple[str, str, str]] = []

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


_AGENT_ONLY_ALLOWLIST = frozenset(
    {
        "base/lm/_providers.py",
        "base/sessions/helperproc.py",
    }
)

_FALLBACK_CONSUMED_READS: dict[tuple[str, str], str] = {
    ("meiva_builtins/plugins/lm_deepseek/provider.py", "deepseek_api_key"): (
        "gateway in-process drafts (reply-suggestion) need the deepseek key the "
        "gateway profile popped; consume-only .env fallback in "
        "_require_deepseek_key()"
    ),
}


_GATEWAY_CLOSURE_PACKAGES = (
    "base",
    "gateway",
    "services",
    "agent",
    "ops",
)


def _module_name_of(path: Path) -> str:
    return str(path.relative_to(_repo_root()).with_suffix("")).replace("/", ".")


def _resolve_module_file(name: str) -> Path | None:
    root = _repo_root()
    as_module = root / (name.replace(".", "/") + ".py")
    if as_module.exists():
        return as_module
    as_package = root / name.replace(".", "/") / "__init__.py"
    if as_package.exists():
        return as_package
    return None


_GATEWAY_UNREACHABLE_EDGES: dict[tuple[str, str], tuple[str, str]] = {}


def _walk_import_closure(
    roots: tuple[str, ...],
    imported_modules: Callable[[ast.AST], list[str]],
) -> set[Path]:
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
    return _walk_import_closure(roots, _gateway_closure_imports)


def test_gateway_closure_reads_do_not_hit_popped_keys() -> None:
    from base.config import FIELD_REFS, field_alias
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
            if field not in FIELD_REFS:
                continue
            alias = field_alias(field)
            if alias in runner_cluster_aliases:
                if (rel, field) in _FALLBACK_CONSUMED_READS:
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
            "base/host/env/dotenv_file.read_env_aliases), change the field's capability, "
            "or add the module to _AGENT_ONLY_ALLOWLIST only if it is genuinely "
            "never executed by a gateway process."
        )
        pytest.fail(msg)


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
        "meiva/",
        "meiva_builtins/",
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

_CLOSURE_PACKAGES = (
    "base",
    "gateway",
    "services",
    "agent",
    "ops",
    "meiva",
    "meiva_builtins",
    "db",
    "ui",
    "cli",
)


def _kind_closure_imports(node: ast.AST) -> list[str]:
    if isinstance(node, ast.ImportFrom):
        if node.module and node.module.split(".")[0] in _CLOSURE_PACKAGES and node.level == 0:
            return [node.module] + [
                f"{node.module}.{alias.name}" for alias in node.names if alias.name != "*"
            ]
        return []
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names if alias.name.split(".")[0] in _CLOSURE_PACKAGES]
    return []


@lru_cache
def _kind_closure(roots: tuple[str, ...]) -> set[Path]:
    return _walk_import_closure(roots, _kind_closure_imports)


def _closure_domains(closure: set[Path]) -> set[str]:
    from base.config import Settings
    from base.config.config_registry import DOMAIN_ATTRS
    from cli.commands.lifecycle.tests.startup.config_read_facts import explicit_config_reads

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
        domains.update(domain for domain, _field in explicit_config_reads(src_text, str(py_file)))
        for domain, field in _extract_settings_reads(src_text):
            owner = Settings.model_fields.get(domain)
            if owner is not None:
                model = owner.annotation
                declarations = getattr(model, "model_fields", {})
                assert field in declarations or hasattr(model, field), (
                    f"settings read names an undeclared field: {domain}.{field}"
                )
            if domain not in aggregate_facts:
                domains.add(domain)
    return domains


def _process_profiles() -> tuple[str, ...]:
    from base.config.profiles import PROCESS_PROFILES

    return tuple(PROCESS_PROFILES)


@pytest.mark.parametrize("profile", _process_profiles())
def test_boot_authority_fact_is_retained_outside_profile_domains(
    profile: str, tmp_path: Path
) -> None:
    from base.config import Settings
    from base.host.env.dotenv_boot import EnvBootResult

    boot = EnvBootResult(db_authority_refusal="fixture refusal")
    runtime = Settings(profile=profile, env_boot=boot)
    assert runtime.env_boot is boot
    assert runtime.env_boot.db_authority_refusal == "fixture refusal"
    source = tmp_path / "consumer.py"
    source.write_text("settings.env_boot.db_authority_refusal\nsettings.lm.labeler_model\n")
    assert _closure_domains({source}) == {"lm"}


@pytest.mark.parametrize("read", ["lm.undeclared_field", "env_boot.undeclared_field"])
def test_profile_matrix_rejects_an_undeclared_settings_read(tmp_path: Path, read: str) -> None:
    source = tmp_path / "consumer.py"
    source.write_text(f"settings.{read}\n")
    with pytest.raises(AssertionError, match="undeclared"):
        _closure_domains({source})


def test_profile_consumption_distinguishes_runtime_facts_from_domains(tmp_path: Path) -> None:
    source = tmp_path / "consumer.py"
    source.write_text(
        "settings.env_boot.db_authority_refusal\n"
        "settings.data_plane.db_url\n"
        "settings.undeclared_domain.value\n"
    )
    assert _closure_domains({source}) == {"data_plane", "undeclared_domain"}


def test_agent_host_launches_under_the_agent_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    from base.config import PROCESS_PROFILES
    from cli.commands.lifecycle import root_driver
    from services.supervision.roster import build_services

    def delivery(_cls: str, *, remote_data_plane: bool) -> dict[str, str]:
        return {"MEIVA_DB_URL": "postgresql://meiva_g0_runner@fixture/meiva"}

    def token_delivery(_cls: str, *, remote_data_plane: bool) -> dict[str, str]:
        return {"MEIVA_API_TOKEN": "fixture-token"}

    monkeypatch.setattr(bringup, "db_delivery", delivery)
    monkeypatch.setattr(bringup, "api_delivery", token_delivery)
    agent_host = next(spec for spec in build_services() if spec.session == "agent-host")
    profile = root_driver.service_extra_env(agent_host, remote_data_plane=False)[
        "MEIVA_PROCESS_PROFILE"
    ]
    assert profile in PROCESS_PROFILES, f"{profile} is not a process profile"
    assert profile == "agent"


def test_profile_domains_match_consumption_matrix() -> None:
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
    return any(
        isinstance(inner, ast.ImportFrom) and inner.module == module
        for holder in ast.walk(tree)
        if isinstance(holder, ast.FunctionDef | ast.AsyncFunctionDef) and holder.name == function
        for inner in ast.walk(holder)
    )


def _uses_module_function(node: ast.AST, owner: str, function: str) -> bool:
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
    root = _repo_root()
    for (rel, field), why in _FALLBACK_CONSUMED_READS.items():
        src = (root / rel).read_text(errors="replace")
        assert "read_env_aliases" in src, f"{rel}: no read_env_aliases fallback ({why})"
        assert f'field_alias("{field}")' in src, (
            f"{rel}: fallback does not name field {field!r} ({why})"
        )
