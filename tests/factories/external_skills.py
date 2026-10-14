"""Filesystem, context and ledger inputs for external-skill transaction tests."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from base.clock.tests.manual_timers import ManualTimers
from base.cluster.machine import MachineIdentity
from base.config import ConfigBoot
from base.host.system.tests.fake_commands import fake_host
from base.sessions.tests.refuse_helper import refuse_helper
from base.telemetry import EventPipeline
from cli.commands.converge.spec import ConvergeCtx

SKILL = "operating-ava-cluster"


def skill_source(repo: Path, body: str = "operator v1\n") -> Path:
    source = repo / "ava_builtins" / "skills" / "platform" / SKILL
    (source / "references").mkdir(parents=True)
    (source / "SKILL.md").write_text(body)
    (source / "references" / "recovery.md").write_text("recover\n")
    return source


def skill_ctx(
    repo: Path,
    tmp_path: Path,
    *,
    operator_database: Callable[[], Any],
    producer: Callable[[], EventPipeline],
    command_config_machine: tuple[ConfigBoot, MachineIdentity],
) -> ConvergeCtx:
    config, machine = command_config_machine
    ava_home = tmp_path / "ava-home"
    (ava_home / "configs").mkdir(parents=True)
    return ConvergeCtx(
        repo=repo,
        ava_home=ava_home,
        roles=None,
        config=config,
        machine=machine,
        database_factory=operator_database,
        producer=producer,
        helper=refuse_helper,
        host=fake_host(),
        services=frozenset(),
        timers=ManualTimers(),
    )


def client_home(tmp_path: Path, name: str = ".codex") -> Path:
    home = tmp_path / "host-home"
    home.mkdir(exist_ok=True)
    client = home / name
    client.mkdir(exist_ok=True)
    assert client.resolve().is_relative_to(tmp_path.resolve())
    return client


def target_path(client: Path, tmp_path: Path) -> Path:
    target = client / "skills" / SKILL
    assert target.resolve(strict=False).is_relative_to(tmp_path.resolve())
    return target


def read_ledger(context: ConvergeCtx) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads(
            (context.ava_home / "configs" / "external-agent-skills" / "codex.json").read_text()
        ),
    )
