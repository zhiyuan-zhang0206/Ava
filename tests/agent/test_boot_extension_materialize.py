"""Process boot lands the cluster's installed skills before anything reads them.

Converge (`ava start`, `ava converge`) already materializes, so the restart path
was covered. What it does not cover is the case with no operator in it: a machine
that was down when someone ran `ava skill install` elsewhere, or a hosted daemon
that has been up since before the install. `future/infra/extension-ownership.md`
S2 puts the same pass at process boot for exactly that window.

The long-lived host runs this once before serving any agent.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import psycopg
import pytest

from agent._process_boot import land_cluster_extensions
from shared import db, paths
from shared import extension_registry as reg

_SKILL_MD = """---
name: {name}
description: a test skill, use when exercising boot materialization
---

# {name}

Instructions.
"""


def _write_skill(root: Path, name: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text(_SKILL_MD.format(name=name), encoding="utf-8")
    return root


def test_boot_lands_a_skill_this_machine_never_installed(
    tmp_path: Path, unit_home: Path, db_conn: psycopg.Connection
) -> None:
    """The offline window, closed with no operator in the loop.

    The row is written as though another machine installed it; this machine has
    never seen the source tree, and the only path by which the content can reach
    its skills directory is the boot pass.
    """
    _ = db_conn  # per-test truncate
    src = _write_skill(tmp_path / "src" / "boot-demo", "boot-demo")
    reg.register_tree(
        db.pool(), root=src, name="boot-demo", kind="skill", source="local:some-other-machine"
    )
    assert not (unit_home / "skills" / "boot-demo").exists(), (
        "this machine must start without it, or the test proves nothing"
    )

    land_cluster_extensions()

    assert (paths.skills_dir() / "boot-demo" / "SKILL.md").read_text(encoding="utf-8") == (
        src / "SKILL.md"
    ).read_text(encoding="utf-8")


def test_an_unreadable_registry_does_not_stop_the_boot(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boot fails fast on what an agent cannot work without. A skills directory
    that is one install behind is not that: the lag is recoverable, the next
    start retries, and refusing to boot would turn it into an outage.

    Concretely reachable, not hypothetical — a cluster whose `ava_runner` grant
    predates the `extensions` table answers this query with `permission denied`
    until its grants are re-affirmed.
    """

    def _refuse(**_kwargs: object) -> psycopg.Connection:
        raise psycopg.errors.InsufficientPrivilege("permission denied for table extensions")

    monkeypatch.setattr(db, "connect", _refuse)

    land_cluster_extensions()  # must not raise

    assert not (unit_home / "skills").exists()


def test_the_hosted_daemon_lands_them_once_per_process() -> None:
    """The hosted runner runs this with the other process-scope halves at daemon
    boot, not per agent: the skills directory is a fact about the machine, shared
    by every agent the host serves.

    Skipping this step would leave an offline machine on its old skill image.
    """
    from services.agent_host.daemon import run

    src = inspect.getsource(run)
    assert src.index("init_process_scope()") < src.index("land_cluster_extensions()")
    assert src.index("land_cluster_extensions()") < src.index("load_process_extensions()")
    assert src.count("land_cluster_extensions()") == 1, (
        "once per process — a second call would re-hash every skill tree for no gain"
    )
