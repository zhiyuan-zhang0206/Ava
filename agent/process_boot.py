import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import ava
from ava.sdk_surface import sdk_disable
from base.cluster.machine import MachineIdentity
from base.db import Database
from base.host.env.agent_slices import ModelOverrides, agent_setting
from base.log import logger
from base.paths import workspace_dir


def apply_per_agent_sdk_disable(*, default_reader: Callable[[str, str], Any]) -> None:
    configured = agent_setting("sdk_disable", default_reader=default_reader)
    if not configured:
        return
    sdk_disable.apply_sdk_disable(list(configured))


def apply_per_agent_eval_isolation(*, default_reader: Callable[[str, str], Any]) -> None:
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


def _isolated_memory_search(_query: str, _k: int = 5) -> list[Any]:
    return []


def init_process_scope(*, machine: MachineIdentity, home: Path) -> None:
    from base.telemetry.tracing import initialize_tracing

    initialize_tracing(machine=machine, home=home)


def land_cluster_extensions(db: Database) -> None:
    from base import paths
    from base.packages.extensions import materialize

    try:
        with db.connect() as conn:
            result = materialize.materialize_skills(
                conn, dest_root=paths.skills_dir(paths.ava_home())
            )
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


async def boot_agent_scope(
    agent_id: int, llm_model: str, overrides: ModelOverrides, *, catalog: Any, llm_override: str
) -> Any:
    workspace_dir(agent_id)
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
