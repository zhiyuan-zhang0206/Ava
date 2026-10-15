from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path

import psycopg

from base import db, paths
from base.cluster.machine import MachineIdentity
from base.db.code_version_gate import ProcessDbGate
from base.packages.extensions import adopt as adopt
from base.packages.extensions import install_registry
from base.packages.extensions import registry as reg

_AsMachine = Callable[[Path], AbstractContextManager[MachineIdentity]]

_SKILL_MD = """---
name: {name}
description: a test skill, use when exercising the adoption sweep
---

# {name}

{body}
"""


def _install_locally(
    name: str,
    *,
    body: str = "Original instructions.",
    origin: install_registry.PackageOrigin = "user",
    enabled: bool = True,
    trust: install_registry.TrustTier = "unreviewed",
) -> Path:
    dest = paths.skills_dir(paths.ava_home()) / name
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "SKILL.md").write_text(_SKILL_MD.format(name=name, body=body), encoding="utf-8")
    install_registry.register(
        install_registry.InstalledPackage(
            name=name, type="skill", origin=origin, enabled=enabled, trust=trust
        )
    )
    return dest


def test_sweep_uploads_a_pre_registry_install(
    tmp_path: Path,
    as_machine: _AsMachine,
    db_conn: psycopg.Connection,
    *,
    database_gate: ProcessDbGate,
) -> None:
    with as_machine(tmp_path / "home-a") as installing_machine:
        _install_locally("sweep-demo")
        with db.pool(gate=database_gate) as pool:
            result = adopt.adopt_local_installs(
                pool,
                skills_root=paths.skills_dir(paths.ava_home()),
                machine_name=installing_machine.name(),
            )
        assert result.adopted == ["sweep-demo"]

    row = reg.get(db_conn, "sweep-demo")
    assert row is not None
    assert row.source == "local:home-a"
    assert row.trust == "unreviewed"
    assert row.content_hash is not None


def test_a_swept_name_crosses_to_a_machine_that_never_had_it(
    tmp_path: Path,
    as_machine: _AsMachine,
    db_conn: psycopg.Connection,
    *,
    database_gate: ProcessDbGate,
) -> None:
    with as_machine(tmp_path / "home-a") as installing_machine:
        src = _install_locally("crossing-demo")
        with db.pool(gate=database_gate) as pool:
            adopt.adopt_local_installs(
                pool,
                skills_root=paths.skills_dir(paths.ava_home()),
                machine_name=installing_machine.name(),
            )
        original = (src / "SKILL.md").read_text(encoding="utf-8")

    with as_machine(tmp_path / "home-b"):
        home_b = tmp_path / "home-b"
        from base.packages.extensions import materialize as mat

        result = mat.materialize_skills(db_conn, dest_root=paths.skills_dir(paths.ava_home()))
        assert result.landed == ["crossing-demo"]
        assert (home_b / "skills" / "crossing-demo" / "SKILL.md").read_text(
            encoding="utf-8"
        ) == original


def test_the_sweep_is_idempotent(
    tmp_path: Path,
    as_machine: _AsMachine,
    db_conn: psycopg.Connection,
    *,
    database_gate: ProcessDbGate,
) -> None:
    _ = db_conn
    with as_machine(tmp_path / "home-a") as installing_machine:
        _install_locally("idem-sweep")
        with db.pool(gate=database_gate) as pool:
            first = adopt.adopt_local_installs(
                pool,
                skills_root=paths.skills_dir(paths.ava_home()),
                machine_name=installing_machine.name(),
            )
        with db.pool(gate=database_gate) as pool:
            second = adopt.adopt_local_installs(
                pool,
                skills_root=paths.skills_dir(paths.ava_home()),
                machine_name=installing_machine.name(),
            )

    assert first.adopted == ["idem-sweep"]
    assert second.adopted == []
    assert second.already_claimed == ["idem-sweep"]


def test_two_machines_with_identical_content_merge_silently(
    tmp_path: Path,
    as_machine: _AsMachine,
    db_conn: psycopg.Connection,
    *,
    database_gate: ProcessDbGate,
) -> None:
    _ = db_conn
    for home in ("home-a", "home-b"):
        with as_machine(tmp_path / home) as installing_machine:
            _install_locally("twin-demo", body="Identical on both.")
            with db.pool(gate=database_gate) as pool:
                result = adopt.adopt_local_installs(
                    pool,
                    skills_root=paths.skills_dir(paths.ava_home()),
                    machine_name=installing_machine.name(),
                )

    assert result.conflicts == []
    assert result.already_claimed == ["twin-demo"]


def test_a_disagreement_is_refused_with_both_machines_named(
    tmp_path: Path,
    as_machine: _AsMachine,
    db_conn: psycopg.Connection,
    *,
    database_gate: ProcessDbGate,
) -> None:
    with as_machine(tmp_path / "home-a") as installing_machine:
        _install_locally("clash-demo", body="A's version.")
        with db.pool(gate=database_gate) as pool:
            adopt.adopt_local_installs(
                pool,
                skills_root=paths.skills_dir(paths.ava_home()),
                machine_name=installing_machine.name(),
            )

    with as_machine(tmp_path / "home-b") as installing_machine:
        local = _install_locally("clash-demo", body="B's version.")
        with db.pool(gate=database_gate) as pool:
            result = adopt.adopt_local_installs(
                pool,
                skills_root=paths.skills_dir(paths.ava_home()),
                machine_name=installing_machine.name(),
            )

        assert result.adopted == []
        assert [c.name for c in result.conflicts] == ["clash-demo"]
        assert result.conflicts[0].claimed_by == "local:home-a"
        assert result.conflicts[0].local_hash != result.conflicts[0].cluster_hash
        assert "B's version." in (local / "SKILL.md").read_text(encoding="utf-8"), (
            "B's copy must be left exactly as it was"
        )

    row = reg.get(db_conn, "clash-demo")
    assert row is not None
    assert row.source == "local:home-a", "A's row must not have been overwritten either"


def test_converge_managed_packages_are_not_adopted(
    tmp_path: Path,
    as_machine: _AsMachine,
    db_conn: psycopg.Connection,
    *,
    database_gate: ProcessDbGate,
) -> None:
    _ = db_conn
    with as_machine(tmp_path / "home-a") as installing_machine:
        _install_locally("repo-skill", origin="repo")
        _install_locally("plugin-skill", origin="plugin")
        with db.pool(gate=database_gate) as pool:
            result = adopt.adopt_local_installs(
                pool,
                skills_root=paths.skills_dir(paths.ava_home()),
                machine_name=installing_machine.name(),
            )

    assert result.adopted == []
    assert result.already_claimed == []


def test_a_locally_disabled_skill_adopts_disabled(
    tmp_path: Path,
    as_machine: _AsMachine,
    db_conn: psycopg.Connection,
    *,
    database_gate: ProcessDbGate,
) -> None:
    with as_machine(tmp_path / "home-a") as installing_machine:
        _install_locally("off-demo", enabled=False)
        with db.pool(gate=database_gate) as pool:
            adopt.adopt_local_installs(
                pool,
                skills_root=paths.skills_dir(paths.ava_home()),
                machine_name=installing_machine.name(),
            )

    row = reg.get(db_conn, "off-demo")
    assert row is not None
    assert row.default_enabled is False
    assert [e.name for e in reg.list_enabled(db_conn, kind="skill")] == []


def test_local_reviewed_trust_travels(
    tmp_path: Path,
    as_machine: _AsMachine,
    db_conn: psycopg.Connection,
    *,
    database_gate: ProcessDbGate,
) -> None:
    with as_machine(tmp_path / "home-a") as installing_machine:
        _install_locally("trusted-demo", trust="reviewed")
        with db.pool(gate=database_gate) as pool:
            adopt.adopt_local_installs(
                pool,
                skills_root=paths.skills_dir(paths.ava_home()),
                machine_name=installing_machine.name(),
            )

    row = reg.get(db_conn, "trusted-demo")
    assert row is not None
    assert row.trust == "reviewed"


def test_reviewed_row_survives_a_later_unreviewed_machine(
    tmp_path: Path,
    as_machine: _AsMachine,
    db_conn: psycopg.Connection,
    *,
    database_gate: ProcessDbGate,
) -> None:
    with as_machine(tmp_path / "home-a") as installing_machine:
        _install_locally("shared-trust-demo", body="Same bytes.", trust="reviewed")
        with db.pool(gate=database_gate) as pool:
            adopt.adopt_local_installs(
                pool,
                skills_root=paths.skills_dir(paths.ava_home()),
                machine_name=installing_machine.name(),
            )

    with as_machine(tmp_path / "home-b") as installing_machine:
        _install_locally("shared-trust-demo", body="Same bytes.", trust="unreviewed")
        with db.pool(gate=database_gate) as pool:
            result = adopt.adopt_local_installs(
                pool,
                skills_root=paths.skills_dir(paths.ava_home()),
                machine_name=installing_machine.name(),
            )
        assert result.conflicts == []
        assert result.already_claimed == ["shared-trust-demo"]

    row = reg.get(db_conn, "shared-trust-demo")
    assert row is not None
    assert row.trust == "reviewed", "a later unreviewed sweep must not downgrade a review"


def test_a_tracked_name_missing_from_disk_is_reported_not_invented(
    tmp_path: Path,
    as_machine: _AsMachine,
    db_conn: psycopg.Connection,
    *,
    database_gate: ProcessDbGate,
) -> None:
    with as_machine(tmp_path / "home-a") as installing_machine:
        install_registry.register(
            install_registry.InstalledPackage(name="ghost-demo", type="skill", origin="user")
        )
        with db.pool(gate=database_gate) as pool:
            result = adopt.adopt_local_installs(
                pool,
                skills_root=paths.skills_dir(paths.ava_home()),
                machine_name=installing_machine.name(),
            )

    assert result.missing_tree == ["ghost-demo"]
    assert result.adopted == []
    assert reg.get(db_conn, "ghost-demo") is None
