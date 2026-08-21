"""Adopting pre-registry installs into the cluster, and refusing to guess.

Everything a machine installed before the extension registry existed is a purely
local fact in its `installed.json`. The install path writes cluster rows going
forward and the materializer only ever reads them, so without the sweep that
content stays invisible to the cluster forever — a hole exactly the size of
everything installed so far.

The interesting half is not the upload. It is the case where two machines both
hold a name: identical trees are the same content by two routes and merge in
silence, while different trees are the one place in the model where two machines
disagree about what a name MEANS, with no evidence here for which is right.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path

import psycopg

from shared import db, install_registry, paths
from shared import extension_adopt as adopt
from shared import extension_registry as reg

_AsMachine = Callable[[Path], AbstractContextManager[Path]]

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
    """Put a skill on this machine the way a pre-registry install left it: a tree
    under the skills dir and a row in `installed.json`, and NOTHING in the
    cluster."""
    dest = paths.skills_dir() / name
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "SKILL.md").write_text(_SKILL_MD.format(name=name, body=body), encoding="utf-8")
    install_registry.register(
        install_registry.InstalledPackage(
            name=name, type="skill", origin=origin, enabled=enabled, trust=trust
        )
    )
    return dest


def test_sweep_uploads_a_pre_registry_install(
    tmp_path: Path, as_machine: _AsMachine, db_conn: psycopg.Connection
) -> None:
    """The claim: content that only ever existed on one machine becomes cluster
    content, with that machine named as where it came from."""
    with as_machine(tmp_path / "home-a"):
        _install_locally("sweep-demo")
        result = adopt.adopt_local_installs(db.pool(), skills_root=paths.skills_dir())
        assert result.adopted == ["sweep-demo"]

    row = reg.get(db_conn, "sweep-demo")
    assert row is not None
    assert row.source == "local:home-a"
    assert row.trust == "unreviewed"
    assert row.content_hash is not None


def test_a_swept_name_crosses_to_a_machine_that_never_had_it(
    tmp_path: Path, as_machine: _AsMachine, db_conn: psycopg.Connection
) -> None:
    """Adoption is only worth anything if the adopted content then behaves like
    any other cluster row — otherwise the sweep has moved bytes into a table
    nobody reads."""
    with as_machine(tmp_path / "home-a"):
        src = _install_locally("crossing-demo")
        adopt.adopt_local_installs(db.pool(), skills_root=paths.skills_dir())
        original = (src / "SKILL.md").read_text(encoding="utf-8")

    with as_machine(tmp_path / "home-b") as home_b:
        from shared import extension_materialize as mat

        result = mat.materialize_skills(db_conn, dest_root=paths.skills_dir())
        assert result.landed == ["crossing-demo"]
        assert (home_b / "skills" / "crossing-demo" / "SKILL.md").read_text(
            encoding="utf-8"
        ) == original


def test_the_sweep_is_idempotent(
    tmp_path: Path, as_machine: _AsMachine, db_conn: psycopg.Connection
) -> None:
    """It runs on every converge, so a second pass has to write nothing — or
    every start re-uploads a blob and `updated_at` stops meaning anything."""
    _ = db_conn
    with as_machine(tmp_path / "home-a"):
        _install_locally("idem-sweep")
        first = adopt.adopt_local_installs(db.pool(), skills_root=paths.skills_dir())
        second = adopt.adopt_local_installs(db.pool(), skills_root=paths.skills_dir())

    assert first.adopted == ["idem-sweep"]
    assert second.adopted == []
    assert second.already_claimed == ["idem-sweep"]


def test_two_machines_with_identical_content_merge_silently(
    tmp_path: Path, as_machine: _AsMachine, db_conn: psycopg.Connection
) -> None:
    """Same name, same bytes, two routes — that is one extension, not a
    disagreement, and it must not produce a warning an operator has to triage."""
    _ = db_conn
    for home in ("home-a", "home-b"):
        with as_machine(tmp_path / home):
            _install_locally("twin-demo", body="Identical on both.")
            result = adopt.adopt_local_installs(db.pool(), skills_root=paths.skills_dir())

    assert result.conflicts == []
    assert result.already_claimed == ["twin-demo"]


def test_a_disagreement_is_refused_with_both_machines_named(
    tmp_path: Path, as_machine: _AsMachine, db_conn: psycopg.Connection
) -> None:
    """The design's own S2 lock. Two machines, one name, different content:
    neither copy is touched, and the report says who else holds it — because
    the resolution is a person deciding, and they cannot decide without knowing
    where the other one is."""
    with as_machine(tmp_path / "home-a"):
        _install_locally("clash-demo", body="A's version.")
        adopt.adopt_local_installs(db.pool(), skills_root=paths.skills_dir())

    with as_machine(tmp_path / "home-b"):
        local = _install_locally("clash-demo", body="B's version.")
        result = adopt.adopt_local_installs(db.pool(), skills_root=paths.skills_dir())

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
    tmp_path: Path, as_machine: _AsMachine, db_conn: psycopg.Connection
) -> None:
    """Repo- and plugin-origin packages are derived state whose content comes
    from the checkout. Uploading them would put checkout bytes in a blob, and a
    `source='repo'` row is forbidden to carry one at all."""
    _ = db_conn
    with as_machine(tmp_path / "home-a"):
        _install_locally("repo-skill", origin="repo")
        _install_locally("plugin-skill", origin="plugin")
        result = adopt.adopt_local_installs(db.pool(), skills_root=paths.skills_dir())

    assert result.adopted == []
    assert result.already_claimed == []


def test_a_locally_disabled_skill_adopts_disabled(
    tmp_path: Path, as_machine: _AsMachine, db_conn: psycopg.Connection
) -> None:
    """The local flag is the only evidence of what the operator wanted. Adopting
    everything as enabled would silently switch on something somebody turned
    off, cluster-wide."""
    with as_machine(tmp_path / "home-a"):
        _install_locally("off-demo", enabled=False)
        adopt.adopt_local_installs(db.pool(), skills_root=paths.skills_dir())

    row = reg.get(db_conn, "off-demo")
    assert row is not None
    assert row.default_enabled is False
    assert [e.name for e in reg.list_enabled(db_conn, kind="skill")] == []


def test_local_reviewed_trust_travels(
    tmp_path: Path, as_machine: _AsMachine, db_conn: psycopg.Connection
) -> None:
    """User ruling 2026-08-21 (issue #218): trust is a cluster-level fact about
    CONTENT — "has a human reviewed these bytes" — not about the machine the
    reviewer sat at. A locally-`reviewed` package adopted into an EMPTY registry
    lands `reviewed`, carrying the review to every machine."""
    with as_machine(tmp_path / "home-a"):
        _install_locally("trusted-demo", trust="reviewed")
        adopt.adopt_local_installs(db.pool(), skills_root=paths.skills_dir())

    row = reg.get(db_conn, "trusted-demo")
    assert row is not None
    assert row.trust == "reviewed"


def test_reviewed_row_survives_a_later_unreviewed_machine(
    tmp_path: Path, as_machine: _AsMachine, db_conn: psycopg.Connection
) -> None:
    """Multi-machine convergence (issue #218): home-a adopts a reviewed package,
    then home-b — holding the IDENTICAL bytes unreviewed — sweeps. The review
    must survive: trust only ever rises for the same content."""
    with as_machine(tmp_path / "home-a"):
        _install_locally("shared-trust-demo", body="Same bytes.", trust="reviewed")
        adopt.adopt_local_installs(db.pool(), skills_root=paths.skills_dir())

    with as_machine(tmp_path / "home-b"):
        _install_locally("shared-trust-demo", body="Same bytes.", trust="unreviewed")
        result = adopt.adopt_local_installs(db.pool(), skills_root=paths.skills_dir())
        assert result.conflicts == []
        assert result.already_claimed == ["shared-trust-demo"]

    row = reg.get(db_conn, "shared-trust-demo")
    assert row is not None
    assert row.trust == "reviewed", "a later unreviewed sweep must not downgrade a review"


def test_a_tracked_name_missing_from_disk_is_reported_not_invented(
    tmp_path: Path, as_machine: _AsMachine, db_conn: psycopg.Connection
) -> None:
    """A row in `installed.json` with no tree behind it is a pre-existing local
    inconsistency. The sweep says so rather than registering an empty name that
    every other machine would then dutifully materialize."""
    with as_machine(tmp_path / "home-a"):
        install_registry.register(
            install_registry.InstalledPackage(name="ghost-demo", type="skill", origin="user")
        )
        result = adopt.adopt_local_installs(db.pool(), skills_root=paths.skills_dir())

    assert result.missing_tree == ["ghost-demo"]
    assert result.adopted == []
    assert reg.get(db_conn, "ghost-demo") is None
