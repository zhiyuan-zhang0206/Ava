"""Forbid bare os.environ / os.getenv — runtime config must go through shared.config.Settings.

Run: `.venv/bin/python scripts/lint_no_os_environ.py [path ...]` (defaults to scanning the whole repo).
Also run automatically via pre-commit hook before commit.

## Why

`shared/config.py:Settings` is the single source of truth for runtime
config. Scattered `os.environ.get("AVA_X")` causes:
- defaults drift from Settings
- types (bool/int/float) are unvalidated; ValueError surfaces only at use, not at startup
- the frontend Control page does not see newly-added vars
- review cannot trace "where does this variable come from"

## Two rules

### Rule 1: Non-test code

Scan all non-test .py files under `agent/`, `shared/`,
`gateway/`, `services/`, `ava/`, `scripts/` (test_*.py / *_test.py /
tests/ excluded). Any reference to `os.environ` or `os.getenv` is an error,
unless the file is in _ALLOWED_FILES (Settings itself + .env loader +
bootstrap that cannot depend on Settings).

Adding a new _ALLOWED_FILES entry requires demonstrating "cannot go through
Settings" — bootstrap ordering constraints; SDK-internal raw env (e.g.
LangChain consuming ANTHROPIC_API_KEY directly) are owned by Settings
fields and do not need this exemption. No inline exemption mechanism —
avoids scattered hard-to-audit `# noqa`-style escape hatches.
The allowlist is self-auditing: an entry becomes an error as soon as its file
no longer contains a raw-environment violation that needs suppressing.

### Rule 2: Test code

Scan all `monkeypatch.setenv("X", ...)` / `monkeypatch.delenv("X")` /
`os.environ["X"] = ...` calls in `tests/`, `test_*.py`, `*_test.py`; if
X is in Settings's **model_fields alias set**, error — `Settings` is a
BaseSettings module-load singleton, env is read once at import time, so
later setenv/delenv cannot reach `settings.x`, and the test silently no-ops.
Must switch to `monkeypatch.setattr(settings, "<field_name>", value)`.

The alias set is dynamically read from `the config field registry` — adding a
new field to Settings auto-syncs the ban list; no manual maintenance.
Historical bugs: PR #327 hit this pattern twice (test_loop_main's
AVA_MCP_SOCKET, test_daemon_health's AVA_SCHEDULER_HEALTH_PORT, plus the
milvus_client fixture's AVA_MILVUS_URI).

Error format `file:line: <line content>` + non-zero exit.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

# Project root (this script lives under scripts/)
_REPO_ROOT = Path(__file__).resolve().parent.parent

# Scan directories — only OUR code, do not scan .venv / vendor / node_modules.
_SCAN_DIRS = (
    "agent",
    "shared",
    "gateway",
    "services",
    "ava",
    "scripts",
)

# File-level exemption — must demonstrate "cannot go through Settings":
# bootstrap ordering / dynamic env enumeration. When adding a new entry,
# include a one-line inline comment explaining why.
_ALLOWED_FILES = frozenset(
    {
        "scripts/legacy_lkg/prepare.py",  # CI fixed-base reconstruction, before either app Settings exists; never a production entry.
        "scripts/legacy_lkg/cold_boot.py",  # CI private normal-process env and pre-Settings home rejection proof.
        "shared/config/__init__.py",  # Settings aggregate; role-derives the gateway-config fetch before sub-models construct
        "shared/config/_base.py",  # _unit_home reads AVA_HOME to root path-field defaults at field-construction time
        "shared/config/data_plane.py",  # _self_machine_host reads AVA_MACHINE_HOST/AVA_HOME at sub-model construction time — the settings singleton does not exist yet, sibling sub-models are unreachable, and shared.machine imports settings (circular)
        "shared/dotenv_boot.py",  # load_dotenv ~/.ava/.env, must run before Settings import
        "shared/runtime_config.py",  # path bootstrap; cannot import Settings (circular dep)
        "shared/config/service_read.py",  # warn_deprecated_env_aliases inspects the RAW env for the legacy AVA_PRIMARY_GATEWAY_URL alias — Settings' AliasChoices resolution would mask which name was actually set
        "cli/commands/config.py",  # the settings-free repair path (ava config --local) reads AVA_GATEWAY_URL / AVA_CLUSTER_SECRET from the raw env/.env WITHOUT constructing Settings — a broken .env is exactly the scenario it repairs, and constructing Settings would fail first; same raw-env class as shared/config/service_read.py
        "shared/bootstrap.py",  # fetches config from the gateway and os.environ.update()s it BEFORE Settings is built; importing shared.config here is the import cycle this module exists to break
        "shared/external_caller.py",  # per-invocation external child profile, consumed by SDK identity bootstrap before Settings; caller provenance is not cluster config and must not enter its persisted Settings projection
        "services/agent_ops/bootstrap.py",  # restricted prepared observer consumes an explicit pre-resolved child projection before ordinary Settings can fetch the stopped gateway
        "services/page_server/daemon.py",  # spawns the page-server child with a per-launch PAGE_SERVER_TOKEN overlaid on the inherited env — the token is a fresh secrets.token_hex(16) per spawn, a dynamic child-env handoff Settings (boot-time static) cannot model, same class as shared/session_env
        "services/page_server/server.py",  # reads the per-launch PAGE_SERVER_TOKEN its daemon parent set in the child env — the token is minted per spawn by the daemon, Settings (boot-time static) cannot model it
        "scripts/lint_no_os_environ.py",  # this script itself has "os.environ" in strings
        "scripts/prove_runtime_prepare.py",  # CI scratch/checkout guards and sanitized child environments must be read before installed Settings exists.
        "scripts/prove_runtime_consumer.py",  # CI-only isolated child environment and missing-home negative control, not runtime configuration.
        "scripts/prove_runtime_migration.py",  # CI-only runner scratch guard must not become an application setting.
        "scripts/prove_runtime_otel.py",  # CI-only scratch/home guard; never production collector configuration.
        "scripts/prove_runtime_plugins.py",  # CI-only private home and CI guard, not runtime plugin settings.
        "scripts/prove_ops_bootstrap.py",  # CI-only scratch/DB guards and sanitized pre-Settings child projection.
        "scripts/check_model_updates.py",  # tracker selects provider API-key aliases dynamically and must prefer the live process env before its `.env` fallback
        "scripts/lint_fixture_scope.py",  # same reason: it MATCHES the string "os.environ" against a test module's AST to find env mutation in a fixture body
        "shared/session_env.py",  # forward_env_dict builds the child env from the LIVE env (incl. AVA_* vars Settings does not model); that is exactly what must be forwarded
        "shared/editable_install.py",  # editable_import_gate starts an isolated venv subprocess from the live inherited environment while removing VIRTUAL_ENV/PYTHONPATH; this process-boundary sanitation cannot use Settings' startup snapshot
        "shared/pty_sessions/host.py",  # the pty child (post-fork, pre-exec) builds its environment from the 0600 envfile dict overlaid on the host's inherited env — the same whole-environment child handoff as shared.session_env / shared.env_registry; Settings cannot enumerate non-modeled keys and the overlay must reflect the parent's live env
        "ava_builtins/skills/telegram-send-file/scripts/send_file.py",  # the telegram config domain is EXCLUDED from the agent process profile (Task #856 consumption matrix), so Settings cannot construct it in the skill's runtime context — the env aliases (the same values Settings itself reads from) are the only access path; same class as the child-env handoff entries
        "shared/env_registry.py",  # child_env builds the parent->child forwarding dict from the LIVE env (the registry's allowlist keys + passthrough rows); Settings cannot enumerate non-modeled keys and the dict must reflect the parent env, not its own snapshot — same child-env handoff as shared.session_env
        "shared/trace.py",  # sets TRACELOOP_TRACE_CONTENT=false for the traceloop-sdk instrumentors — the SDK's ONLY content-tracing switch (no Python API equivalent); Ava's own config surface is the AVA_TRACE_STRIP_CONTENT settings field, which drives this env translation
        "shared/gitenv.py",  # git_env copies the live env for a git subprocess (which needs PATH/HOME/SSH_AUTH_SOCK) and layers GIT_TERMINAL_PROMPT/GIT_SSH_COMMAND on top; git plumbing + a whole-environment child handoff, not Ava runtime config
        "shared/process_env.py",  # centralized process-protocol seam: copies the complete live env, consumes one-shot markers, and adopts a child's committed handoff; Settings cannot model dynamic per-process state
        "ops/agent_launch.py",  # agent_spawn_env_dict copies the registry's forward view (shared/env_registry.py child_env) from the live env into a detached child's env — the same child-env handoff as shared.session_env; Settings cannot enumerate non-modeled keys and the dict must reflect the parent's live env, not its own snapshot
        "scripts/migration_smoke.py",  # builds a psql subprocess env (PGHOST/PGPORT/... from a throwaway native Postgres); PG* are libpq plumbing, not Ava runtime config
        "scripts/coverage_gates.py",  # BACKEND_COVERAGE_THRESHOLD is a ci.yml workflow knob for the pre-merge gate, not runtime config — Settings models the deployed runtime, and importing shared.config would drag the settings singleton into a pure CI report parser
        "scripts/ci_utils.py",  # CI_QUEUE and TRUNK_API_TOKEN are per-invocation CI-orchestration inputs; Settings models deployment config, and its singleton cannot preserve the required live environment read for this standalone merge watcher
        "shared/platform.py",  # process-platform plumbing that Settings cannot model: ensure_utf8_stdio sets Python runtime encoding knobs for child interpreters; launchd_job_label reads the per-process XPC_SERVICE_NAME scheduler identity
        "shared/platform_probes.py",  # display_available reads DISPLAY/WAYLAND_DISPLAY to detect X11/Wayland; these are OS display-server vars, not ava runtime config; no Settings field models them. Single source of truth shared by the browser daemon / MCP loader / host-config validators
        "ava/watcher.py",  # _spawn() bootstrap code uses os.environ.get in a string literal for the child process bootstrap
        "ava/_boot.py",  # _try_establish_from_env() reads os.environ["AVA_AGENT_ID"] as a lazy fallback; the env key is the only channel for child processes (shell sessions, watchers) to discover their parent agent
        "ava/_attach.py",  # attach() reads the one-shot AVA_EXEC_REQUEST_FILE child-protocol marker at call time; it is not Settings config and only an exec child receives it
        "shared/observability.py",  # endpoint_override_is_explicit must distinguish operator-set observability URLs from Settings' identical loopback defaults; Settings preserves the value but not whether it was explicit
        "shared/turn_identity.py",  # effective_agent_id() reads the ambient AVA_AGENT_ID as the outermost identity fallback (the same per-process identity channel as ava/_boot.py / ava/_mcp_remote.py); the turn contextvar layers above it and Settings models neither  # _current_agent_id() reads the ambient AVA_AGENT_ID to stamp MCP daemon envelopes; the key is the process identity channel, not Settings-managed, and importing ava.self here is circular (moved from ava/mcps.py, 2026-08-13 #1229)
        "services/computer/mcp_wrapper.py",  # _agent_id() reads the ambient AVA_AGENT_ID to stamp computer-mcp requests; same identity channel, not Settings-managed
        "agent/_process_boot.py",  # boot sets os.environ["AVA_AGENT_ID"] so child processes inherit the agent identity; the env forward must run before child spawn and cannot route through Settings (the same forward agent/loop.py previously owned)
        "agent/loop.py",  # run() pops the per-agent config-overlay / birth-config env vars ($AVA_AGENT_CONFIG_OVERLAY / $AVA_AGENT_BIRTH_CONFIG) before spawning children; argv is world-readable via ps (issue #974) and the payloads are per-agent launch secrets, not Settings fields
        "agent/exec_child.py",  # the exec child reads its per-launch protocol env (AVA_EXEC_REQUEST_FILE / AVA_EXEC_RESULT_FILE, one-shot spawn handoff) and pops the re-emitted per-agent overlay maps before child spawn — the same child-env handoff class as agent/loop.py's pop
        "agent/graph/_exec_subprocess.py",  # _build_child_env copies the LIVE parent env and layers the exec protocol vars on top — a whole-environment child handoff (same class as shared/session_env.py / ops/agent_launch.py); Settings cannot enumerate non-modeled keys and the dict must reflect the parent env
        "shared/lm/provider_api.py",  # plugin keys are not Settings fields; require_key reads the live process env for the bootstrap plugin-secrets channel on split runners, the same class as child-env handoff entries
        "scripts/migrate_skill_identity.py",  # standalone R2-B migration tool: must target an arbitrary AVA_HOME (--ava-home overrides) and build a psql subprocess env at call time; importing shared.config would freeze the settings singleton to the process's own home at import and drag the whole config stack into a script that must run against foreign / fresh homes
        "scripts/guard_editable_venv.py",  # dependency-free pre-uv preflight must inspect inherited VIRTUAL_ENV before a project environment can be trusted or Settings can import
    }
)

# Transitional grandfathered list is empty — all originally grandfathered files have migrated to Settings.
_GRANDFATHERED: frozenset[str] = frozenset()

# Test files — pytest fixtures mocking env is a legitimate use
_TEST_PATTERNS = (
    re.compile(r"(^|/)tests?/"),
    re.compile(r"(^|/)test_[^/]+\.py$"),
    re.compile(r"_test\.py$"),
)

# Match os.environ. / os.environ[ / os.getenv(  (word boundary on both sides).
_OS_ENV_PATTERN = re.compile(r"\bos\.(environ|getenv)\b")

# Match only `monkeypatch.setenv("X"...)` / `monkeypatch.delenv("X"...)` —
# pytest fixture calls, which always happen after Settings import (inside
# the test function). Deliberately does **not** match `os.environ["X"] = ...`
# — some env in conftest is bootstrap before Settings import, or used for
# subprocesses (gateway / agent fork); neither is the settings-singleton bug.
_TEST_SETENV_PATTERN = re.compile(
    r"\bmonkeypatch\.(?:setenv|delenv)\s*\(\s*['\"]([A-Z_][A-Z0-9_]*)['\"]"
)


def _settings_managed_aliases() -> frozenset[str]:
    """Read every alias from the config field registry — auto-syncs when Settings adds a field.

    Returns env var names (alias) that are owned by Settings, so any
    monkeypatch.setenv on them is a NOOP at runtime (Settings is module-load
    singleton; env reads happen at __init__ time, not on attribute access).
    """
    from shared.config import FIELD_INFOS

    aliases: set[str] = set()
    for field in FIELD_INFOS.values():
        if field.alias:
            aliases.add(field.alias)
    return frozenset(aliases)


def _is_test_file(rel_path: str) -> bool:
    return any(p.search(rel_path) for p in _TEST_PATTERNS)


def _character_column(line: str, byte_column: int) -> int:
    """Translate an AST UTF-8 byte offset into a Python string offset."""
    return len(line.encode("utf-8")[:byte_column].decode("utf-8"))


def _source_lines_without_docstrings(source: str) -> list[str]:
    """Blank actual module, class, and function docstrings from source lines.

    Other string literals stay visible because bootstrap and generated-code
    strings are executable data whose raw-environment references still require
    an exemption.
    """
    lines = source.splitlines()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # Be conservative for an invalid file: scan every source character.
        return lines

    docstring_owners = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for owner in ast.walk(tree):
        if not isinstance(owner, docstring_owners) or not owner.body:
            continue
        statement = owner.body[0]
        if not (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
            and statement.end_lineno is not None
            and statement.end_col_offset is not None
        ):
            continue
        for lineno in range(statement.lineno, statement.end_lineno + 1):
            line = lines[lineno - 1]
            start = (
                _character_column(line, statement.col_offset) if lineno == statement.lineno else 0
            )
            end = (
                _character_column(line, statement.end_col_offset)
                if lineno == statement.end_lineno
                else len(line)
            )
            lines[lineno - 1] = f"{line[:start]}{' ' * (end - start)}{line[end:]}"
    return lines


def _scan_file(
    path: Path,
    rel_path: str,
    managed_envs: frozenset[str],
    *,
    honor_exemptions: bool = True,
) -> list[tuple[int, str, str]]:
    """Return error list [(lineno, line_stripped, error_kind), ...].

    error_kind = "naked-env" (Rule 1) | "setenv-managed" (Rule 2).
    """
    if honor_exemptions and (rel_path in _ALLOWED_FILES or rel_path in _GRANDFATHERED):
        return []
    is_test = _is_test_file(rel_path)
    violations: list[tuple[int, str, str]] = []
    source = path.read_text(encoding="utf-8")
    original_lines = source.splitlines()
    for lineno, code_line in enumerate(_source_lines_without_docstrings(source), start=1):
        line = original_lines[lineno - 1]
        # `#` splits the line into code and comment; only check the code part.
        code, _, _ = code_line.partition("#")
        if is_test:
            # Rule 2: monkeypatch.setenv/delenv in tests changing a Settings-managed env (silent no-op).
            for m in _TEST_SETENV_PATTERN.finditer(code):
                env_name = m.group(1)
                if env_name in managed_envs:
                    violations.append((lineno, line.strip(), f"setenv-managed:{env_name}"))
        elif _OS_ENV_PATTERN.search(code):
            # Rule 1: bare os.environ in non-test code.
            violations.append((lineno, line.strip(), "naked-env"))
    return violations


def _unused_file_exemptions() -> list[str]:
    """Return exemptions that no longer suppress a raw-environment violation."""
    unused: list[str] = []
    for rel_path in sorted(_ALLOWED_FILES | _GRANDFATHERED):
        path = _REPO_ROOT / rel_path
        violations = (
            _scan_file(path, rel_path, frozenset(), honor_exemptions=False)
            if path.is_file()
            else []
        )
        if not any(kind == "naked-env" for _, _, kind in violations):
            unused.append(rel_path)
    return unused


def _iter_py_files(roots: list[Path]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        if root.is_file() and root.suffix == ".py":
            files.append(root)
        elif root.is_dir():
            files.extend(root.rglob("*.py"))
    return files


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    # argv non-empty = pre-commit passed the changed-file list; empty = default scan of all _SCAN_DIRS + tests/.
    if argv:
        targets = [Path(a).resolve() for a in argv]
    else:
        targets = [_REPO_ROOT / d for d in _SCAN_DIRS] + [_REPO_ROOT / "tests"]

    py_files = _iter_py_files(targets)
    managed_envs = _settings_managed_aliases()

    unused_exemptions = _unused_file_exemptions()
    for rel_path in unused_exemptions:
        print(f"{rel_path}: unused raw-environment exemption -> remove it from the allowlist")
    total_violations = len(unused_exemptions)
    for path in sorted(py_files):
        try:
            rel = path.relative_to(_REPO_ROOT).as_posix()
        except ValueError:
            # Path not under repo — pre-commit usually passes absolute paths so this is rare; safety net.
            rel = path.as_posix()
        violations = _scan_file(path, rel, managed_envs)
        for lineno, content, kind in violations:
            total_violations += 1
            if kind.startswith("setenv-managed:"):
                env = kind.split(":", 1)[1]
                print(
                    f"{rel}:{lineno}: monkeypatch.setenv/delenv on `{env}` — Settings is a module-load "
                    f"singleton; env changes do not reach the settings instance field. Use "
                    f'`monkeypatch.setattr(settings, "<field_name>", value)` instead.'
                )
            else:
                print(
                    f"{rel}:{lineno}: bare os.environ/getenv usage -> route through shared.config.settings"
                )
            print(f"    {content}")

    if total_violations:
        print(
            f"\n{total_violations} violations total. See the docstring at the top of "
            "scripts/lint_no_os_environ.py for the exemption procedure and the rationale "
            "for using setattr in tests.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
