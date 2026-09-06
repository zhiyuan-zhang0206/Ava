"""Host and agent boot scopes.

The daemon initializes tracing, materializes cluster skills, and loads external
plugins once per process. Each agent receives its workspace, desktop permission
notice, and model under its bound configuration. SDK restrictions are applied
inside the disposable execution child, whose module state is isolated.
"""

import asyncio
import os
from pathlib import Path

from langchain_core.language_models.chat_models import BaseChatModel

import ava
from ava.shell import sessions
from shared.config.turn_view import turn_settings
from shared.lm.factory import build_chat_model
from shared.log import logger
from shared.paths import workspace_dir
from shared.watcher import TEMPLATE_VERSION
from shared.watcher_registry import watcher_rows

from .startup import (
    _notify_desktop_permissions_at_startup,
)


def _apply_per_agent_sdk_disable() -> None:
    """Apply per-agent sdk_disable delta after config_overlay framework scope.

    sdk_disable is applied at ``ava`` import time from the env var
    ``AVA_SDK_DISABLE``.  Per-agent overlay additions to sdk_disable are
    set on settings by ``apply_config_overlay`` and must be applied on top
    of the env baseline — ``ava._apply_sdk_disable`` is idempotent, so
    only genuinely new entries take effect.
    """

    if not turn_settings.agent.sdk_disable:
        return
    env_entries = set(ava._sdk_disable_entries)
    new_disable = [e for e in turn_settings.agent.sdk_disable if e not in env_entries]
    if new_disable:
        ava._apply_sdk_disable(new_disable)


def _apply_per_agent_eval_isolation() -> None:
    """Apply the SDK and memory-pool boundaries for an isolated eval agent.

    This runs after plugins have registered their namespaces: the eval boundary
    must rebind the live `ava.memory` surface rather than affect the plugin's
    import-time default path.
    """
    if not turn_settings.agent.eval_isolation:
        return

    allowed_network = set(turn_settings.agent.eval_network_allowlist)
    disabled = ["agents.get_last_message", "tasks", "mcps", "ui"]
    if "web" not in allowed_network:
        disabled.append("web")
    if "understand" not in allowed_network:
        disabled.append("understand")
    ava._apply_sdk_disable(disabled)

    agent_id = int(os.environ["AVA_AGENT_ID"])
    isolated_pool = workspace_dir(agent_id) / "memory-pool"
    isolated_pool.mkdir(parents=True, exist_ok=True)
    memory = getattr(ava, "memory", None)
    if memory is not None:
        memory.PATH = ava.const(isolated_pool, doc=memory.PATH.__doc__)
        memory.search = _isolated_memory_search
        memory.search_detailed = _isolated_memory_search


def _isolated_memory_search(_query: str, _k: int = 5) -> list[tuple[Path, str, list[str]]]:
    """Return no shared-memory results for an isolated evaluation agent."""
    return []


def init_process_scope() -> None:
    """Process-scope boot: start OTLP trace init.

    Only the cheap decisions (disk guards + collector preflight) run here;
    the heavy traceloop import + init proceeds on a daemon thread, so the boot
    path stays sub-second. OpenLLMetry must still be installed before the
    first turn (the LangChain callback-manager wrap and the SDK instrumentors
    are call-time, and the turn root span needs the provider set) — that
    ordering is enforced by `shared.trace.ensure_init_resolved` inside
    `turn_span`, which is what the first graph invocation waits on.

    Process scope, not agent scope: `initialize_tracing` installs the global
    tracer provider, and the span attribution that distinguishes agents is the
    per-turn root span (`shared.trace.turn_span`), not the provider. The hosted
    runner calls this once at daemon boot.
    """
    from shared.trace import initialize_tracing

    initialize_tracing()


def land_cluster_extensions() -> None:
    """Process-scope boot: land the cluster's installed skills onto this machine.

    The boot-side sibling of `cli/commands/_converge_extensions.py`
    (`materialize_cluster_extensions`), over the same
    `shared.extension_materialize.materialize_skills`. Converge covers the
    operator path — `ava start`, `ava converge`; this covers the one that needs
    no operator at all, which is what closes the offline window: a machine that
    was down when someone ran `ava skill install` elsewhere catches up the moment
    anything on it next starts, and an agent never boots against a tree older
    than the registry row it could have read
    (`future/infra/extension-ownership.md` S2).

    Process scope, not agent scope: the skills directory is a fact about the
    MACHINE, identical for every agent on it. The hosted runner therefore calls
    this once at daemon boot, beside the other two process-scope halves, rather
    than per agent.

    Runs before `load_process_extensions` so the ordering stays correct when
    plugins become registry-owned in a later slice. Today it does not matter —
    skills are read per turn and plugins still come from the checkout — which is
    exactly why it is worth fixing now rather than after the ordering has a
    consequence.

    Failures are logged, not raised, and that is a different judgement from the
    one boot usually makes. Boot fails fast on the things an agent cannot work
    without; a stale skills directory is not one of them, the registry retries on
    the next start, and refusing to boot over it would convert a recoverable lag
    into an outage. Same stance as converge, and the opposite of the install
    path's, which is where the fact is CREATED.

    On a cluster with no installed extensions this is one indexed query
    returning no rows.
    """
    from shared import db, extension_materialize, paths

    try:
        with db.connect() as conn:
            result = extension_materialize.materialize_skills(conn, dest_root=paths.skills_dir())
    except Exception as exc:
        logger.warning(
            "[extensions] could not read the cluster registry at boot ({}); this "
            "machine keeps whatever skills it already has and retries on the next start",
            exc,
        )
        return
    if result.changed:
        logger.info(
            "[extensions] landed {} skill(s), updated {}",
            len(result.landed),
            len(result.updated),
        )


def load_process_extensions() -> None:
    """Process-scope boot: import every external plugin under `$AVA_HOME/plugins`.

    Import side effects are the registration (hooks, Layer A wraps, system-prompt
    contributions), and they must land before the first exec node runs agent code
    — plugins may monkey-patch `ava.X.y`.

    **Exactly once per process.** Repeating it is not a supported way to pick up
    a newly installed plugin: plugin-spec-v2's S4 dispose contract is
    unimplemented, so a second load leaks whatever the first allocated and forks
    class identity for anything that captured a plugin class before it (PR #154
    made the module object stable, which removes a different obstacle, not this
    one). Newly installed plugins take effect on the next runner restart.
    """
    ava._extend.scan_and_load()


async def boot_agent_scope(agent_id: int) -> BaseChatModel:
    """Agent-scope boot: workspace pre-create, screen-capture notice, chat model.

    Everything here is a fact about ONE agent, so the hosted runner runs it per
    agent (cached, keyed on the agent's stored config) while the process-scope
    half above runs once for the whole daemon.

    The workspace pre-create is unconditional, not gated by
    `settings.agent.workspace_in_system_prompt`: the folder is the relative-path
    base for `ava.files` / `ava.shell.run`, so it must exist even when the prompt
    section advertising it is off (bench runners).

    The chat model is built from `turn_settings.lm.llm_model`, so callers must
    bind this agent's framework-scope config first through
    `shared.config.turn_view.bind_agent_config`.

    Building it eagerly is safe even though the trace init is still in flight:
    traceloop's LangChain wrap injects its callback handler into every
    callback manager at CONFIGURATION time (per run / per call), so a model
    constructed before the init completes still produces spans on its first
    call — and that first call happens inside a turn, after turn_span has
    waited for the init.

    Returns:
        This agent's chat model.
    """
    workspace_dir(agent_id)
    # When converge detected an unavailable desktop permission, notify once
    # (idempotent -- clears claimed status files after). Must run after the
    # SDK/plugin load so ava.ui.notify is available.
    await _notify_desktop_permissions_at_startup()
    return build_chat_model(turn_settings.lm.llm_model)


async def reconcile_agent_watchers(agent_id: int) -> bool:
    """Restore watcher intent under this turn's identity and configuration.

    Reconcile is best effort and may return after a registry or spawn failure.
    Keep boot recovery pending until the remaining desired rows actually have
    current sessions; an action list alone is not proof of completion.
    """

    def reconcile_and_verify() -> bool:
        for action in ava.watcher.reconcile():
            logger.info("watcher reconcile: {}", action, agent_id=agent_id)
        running = [row for row in watcher_rows(agent_id) if row["status"] == "running"]
        if not running:
            return True
        alive = sessions.list()
        generation = sessions._current_session_generation()
        return all(
            row["generation"] == generation
            and row["session_id"] in alive
            and sessions._session_generation(row["session_id"]) == generation
            and (row["kind"] != "cron" or (row["template_version"] or 0) >= TEMPLATE_VERSION)
            for row in running
        )

    try:
        # to_thread carries the host's admitted identity and config bindings.
        return await asyncio.to_thread(reconcile_and_verify)
    except Exception:
        logger.opt(exception=True).warning("watcher recovery remains pending", agent_id=agent_id)
        return False
