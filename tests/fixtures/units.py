"""Explicit unit setup helpers; pytest fixtures live in capability modules."""

from pathlib import Path
from typing import Any

import pytest

from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog


def skip_authority_pass(_home: Path) -> None:
    """Stand-in for `dotenv_boot._enforce_cluster_env_authority` where a test
    exercises the rest of the boot and must not have its env rewritten."""


def use_env_files(
    monkeypatch: pytest.MonkeyPatch, env_file: Path, mirror_file: Path | None = None
) -> Path:
    """Name, through `AVA_HOME`, a home whose `.env` (and `mirror.env`) carry these
    files' content: for a test that hand-builds the files a boot pass reads.

    The home is a sibling directory of `env_file`; the variable is the only thing
    the boot reads, so nothing else needs patching."""
    home = env_file.parent / f"{env_file.stem}-home"
    home.mkdir(exist_ok=True)
    (home / ".env").write_text(env_file.read_text())
    if mirror_file is not None and mirror_file.exists():
        (home / "mirror.env").write_text(mirror_file.read_text())
    monkeypatch.setenv("AVA_HOME", str(home))
    return home


def spawn_agent(
    *,
    catalog: ModelCatalog,
    authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    spawner: str = "user",
    config: dict[str, object] | None = None,
    **kw: Any,
) -> int:
    """Allocate a real agent row and publish its normal host-dispatch wake."""
    from base.cluster.machine import machine_name
    from base.db import publish_inbound_wake
    from ops.agents.spawn import create_agent_row

    db, bus = Database.from_settings(gate=database_gate), EventBus.from_settings()
    agent_id, _, _prompt_id, _attempt_id = create_agent_row(
        db,
        bus,
        spawner=spawner,
        machine=machine_name(),
        config=config,
        catalog=catalog,
        authority=authority,
        **kw,
    )
    publish_inbound_wake(db, bus, agent_id, "0")
    return agent_id
