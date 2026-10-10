"""Host and agent boot scopes.

The daemon initializes tracing, materializes cluster skills, and loads external
plugins once per process. Each agent receives its workspace, desktop permission
notice, and model under its bound configuration. SDK restrictions are applied
inside the disposable execution child, whose module state is isolated. The
heavy boot imports (the chat model, `.startup`) stay function-level in
`boot_agent_scope` so that importing this module — which the exec child does
for the SDK helpers — does not enter the LM stack (startup-path laziness,
task #3585).
"""

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import ava
from ava.sdk_surface import sdk_disable
from base.db import Database
from base.host.env.agent_slices import ModelOverrides, agent_setting
from base.log import logger
from base.paths import workspace_dir


def _apply_per_agent_sdk_disable(*, default_reader: Callable[[str, str], Any]) -> None:
    """Apply the per-agent sdk_disable list additively on the installed SDK surface.

    The env baseline ``AVA_SDK_DISABLE`` is applied by the SDK install while it
    builds the installation; the per-agent ``config_overlay`` sdk_disable value is
    set on settings by ``apply_config_overlay`` and must be applied on top —
    ``sdk_disable.apply_sdk_disable`` takes only entries not yet applied (the
    installation records its own disable set), so re-listing the env baseline is a
    no-op.
    """
    configured = agent_setting("sdk_disable", default_reader=default_reader)
    if not configured:
        return
    sdk_disable.apply_sdk_disable(list(configured))


def _apply_per_agent_eval_isolation(*, default_reader: Callable[[str, str], Any]) -> None:
    """Apply the SDK and memory-pool boundaries for an isolated eval agent.

    This runs after plugins have registered their namespaces: the eval boundary
    must rebind the live `ava.memory` surface rather than affect the plugin's
    import-time default path.
    """
    if not agent_setting("eval_isolation", default_reader=default_reader):
        return

    allowed_network = set(agent_setting("eval_network_allowlist", default_reader=default_reader))
    disabled = ["agents.get_last_message", "tasks", "mcps", "ui"]
    if "web" not in allowed_network:
        disabled.append("web")
    if "understand" not in allowed_network:
        disabled.append("understand")
    sdk_disable.apply_sdk_disable(disabled)

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
    ordering is enforced by `base.telemetry.tracing.ensure_init_resolved` inside
    `turn_span`, which is what the first graph invocation waits on.

    Process scope, not agent scope: `initialize_tracing` installs the global
    tracer provider, and the span attribution that distinguishes agents is the
    per-turn root span (`base.telemetry.tracing.turn_span`), not the provider. The hosted
    runner calls this once at daemon boot.
    """
    from base.telemetry.tracing import initialize_tracing

    initialize_tracing()


def land_cluster_extensions(db: Database) -> None:
    """Process-scope boot: land the cluster's installed skills onto this machine.

    The boot-side sibling of `cli/commands/extensions/materialize.py`
    (`materialize_cluster_extensions`), over the same
    `base.packages.extensions.materialize.materialize_skills`. Converge covers the
    operator path — `ava start`, `ava converge`; this covers the one that needs
    no operator at all, which is what closes the offline window: a machine that
    was down when someone ran `ava skill install` elsewhere catches up the moment
    anything on it next starts, and an agent never boots against a tree older
    than the registry row it could have read
    (`future/infra/extensions/extension-ownership.md` S2).

    Process scope, not agent scope: the skills directory is a fact about the
    MACHINE, identical for every agent on it. The hosted runner therefore calls
    this once at daemon boot, beside the other two process-scope halves, rather
    than per agent.

    Runs before the plugin load (`agent.extensions.load_extensions`, at host boot) so the ordering
    stays correct when plugins become registry-owned in a later slice. Today it does not matter —
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
    from base import paths
    from base.packages.extensions import materialize

    try:
        with db.connect() as conn:
            result = materialize.materialize_skills(conn, dest_root=paths.skills_dir())
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


# Return type is Any on purpose: the chat-model class must stay out of module
# scope (the exec child imports this module for the SDK helpers), and Pyright
# cannot resolve an annotation the module never imports.
async def boot_agent_scope(
    agent_id: int, llm_model: str, overrides: ModelOverrides, *, catalog: Any, llm_override: str
) -> Any:
    """Agent-scope boot: workspace pre-create, screen-capture notice, chat model.

    Everything here is a fact about ONE agent, so the hosted runner runs it per
    agent (cached, keyed on the agent's stored config) while the process-scope
    half above runs once for the whole daemon.

    The workspace pre-create is unconditional, not gated by
    `settings.agent.workspace_in_system_prompt`: the folder is the relative-path
    base for `ava.files` / `ava.shell.run`, so it must exist even when the prompt
    section advertising it is off (bench runners).

    The chat model is built for `llm_model`, the agent's model for the turn, with the
    agent's `overrides` (reasoning effort, thinking budget).

    Building it eagerly is safe even though the trace init is still in flight:
    traceloop's LangChain wrap injects its callback handler into every
    callback manager at CONFIGURATION time (per run / per call), so a model
    constructed before the init completes still produces spans on its first
    call — and that first call happens inside a turn, after turn_span has
    waited for the init.

    Returns:
        This agent's chat model and the binding selected by the same build.
    """
    workspace_dir(agent_id)
    # When converge detected an unavailable desktop permission, notify once
    # (idempotent -- clears claimed status files after). Must run after the
    # SDK/plugin load so ava.ui.notify is available.
    from .startup import notify_desktop_permissions_at_startup

    await notify_desktop_permissions_at_startup()
    from base.lm.factory import build_chat_model_bound

    return build_chat_model_bound(
        llm_model,
        agent_id=agent_id,
        overrides=overrides,
        catalog=catalog,
        llm_override=llm_override,
    )
