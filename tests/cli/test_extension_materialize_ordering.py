"""Where cluster-extension materialization is invoked from, and why not converge.

The registry read has a precondition: this cluster's Postgres is up AND its
schema is current. `ava start` satisfies that only late in its sequence:

    1)   converge host state
    2)   gateway brings up THIS cluster's pg/redis
    2.5) apply pending migrations
    2.6) verify schema version
    2.7) materialize cluster extensions   <- the first point the precondition holds

So a `CONVERGE_STEPS` entry — which the design doc originally described, and
which #201 shipped — runs before the database exists on a single box, and before
the `extensions` table exists on the rollout that creates it. It would have
failed on every start of the most common posture while printing a warning that
reads like a transient outage.

These pin the placement, because it is invisible at the call site: the function
looks equally correct in either home, and only the surrounding sequence says
which one works.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest


def test_materialization_is_not_a_converge_step() -> None:
    """Converge runs before the data plane is up (`ava start` step 1 vs step 2),
    so nothing in `CONVERGE_STEPS` may require the cluster database."""
    from cli.commands._converge import CONVERGE_STEPS

    offenders = [s.name for s in CONVERGE_STEPS if "extension" in s.name.lower()]
    assert not offenders, (
        f"{offenders} is a converge STEP, but converge runs before this cluster's "
        "Postgres is started and before migrations apply. Call it from the start "
        "sequence after the schema check instead — see cli/commands/_converge_extensions.py:"
        "materialize_cluster_extensions."
    )


def _instrument_cold_start_seams(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    """Stub every `_prepare_cold_start` seam, recording the ones this invariant
    is about. Mirrors the monkeypatched-seam style of
    `test_release_cold_start_uses_same_storage_readiness_without_source_or_schema_writes`
    in `tests/cli/test_start_runtime.py` rather than inspecting source text."""
    import cli.commands._converge as _converge_commands
    import cli.commands._repo as _repo_commands
    import cli.commands.start as _start_commands
    from cli.commands import _converge_extensions, _data_plane

    def converge_host(*_args: object, **_kwargs: object) -> None:
        return None

    def ensure_gateway_data_plane() -> int:
        return 0

    def prepare_gateway_schema() -> None:
        return None

    def migrate() -> None:
        calls.append("migrate")

    def complete_gateway_data_plane(*, refresh_schema: bool = True) -> None:
        del refresh_schema

    def schema_check() -> int:
        calls.append("schema-check")
        return 0

    def adopt() -> None:
        calls.append("adopt")

    def materialize() -> None:
        calls.append("materialize")

    monkeypatch.setattr(_converge_commands, "converge_host", converge_host)
    monkeypatch.setattr(_start_commands, "_ensure_gateway_data_plane", ensure_gateway_data_plane)
    monkeypatch.setattr(_data_plane, "prepare_gateway_schema", prepare_gateway_schema)
    monkeypatch.setattr(_start_commands, "cmd_migrations_apply", migrate)
    monkeypatch.setattr(_data_plane, "complete_gateway_data_plane", complete_gateway_data_plane)
    monkeypatch.setattr(_repo_commands, "_assert_schema_current_or_die", schema_check)
    monkeypatch.setattr(_converge_extensions, "adopt_local_extensions", adopt)
    monkeypatch.setattr(_converge_extensions, "materialize_cluster_extensions", materialize)


def test_start_materializes_after_the_schema_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ordering inside `_prepare_cold_start`, asserted via monkeypatched seams.

    Brittle-looking on purpose: the invariant IS the order of three calls, and
    nothing else in the process can observe it. If someone moves the
    materialization above the migration apply or the schema check, the
    registry read starts hitting a schema that may predate the `extensions`
    table, and the symptom is a warning that looks transient rather than a
    failure.
    """
    from cli.commands.start import _prepare_cold_start

    calls: list[str] = []
    _instrument_cold_start_seams(monkeypatch, calls)

    rc = _prepare_cold_start(Path("/repo"), frozenset({"gateway"}), (), runtime=None)

    assert rc == 0
    assert calls.index("migrate") < calls.index("materialize"), (
        "materialization must run AFTER pending migrations apply — otherwise the "
        "rollout that creates the extensions table reads it before it exists"
    )
    assert calls.index("schema-check") < calls.index("materialize"), (
        "materialization must run AFTER the schema-current check — reading the "
        "registry against a schema this checkout does not understand is exactly "
        "what that check exists to prevent"
    )


def test_standalone_converge_materializes_too() -> None:
    """`ava converge` is run against a cluster that is already up, so it has the
    precondition the start path has to wait for — and an operator running it
    expects the machine to end up caught up."""
    from cli.commands._converge import cmd_converge

    assert "materialize_cluster_extensions()" in inspect.getsource(cmd_converge)


def test_the_materializer_lives_beside_its_siblings() -> None:
    """`_converge_extensions.py`, next to `_converge_skills.py` — a subsystem
    reader, not another entry in `_converge.py`'s host-state roster. Extracting
    it is also what kept `_converge.py` under the 800-line ceiling."""
    from cli.commands import _converge_extensions

    assert hasattr(_converge_extensions, "materialize_cluster_extensions")


def test_start_adopts_before_it_materializes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both orders are correct, and one of them is tidier.

    An unclaimed local name is invisible to the materializer (it has no row) and
    a freshly adopted one hashes as `unchanged`, so neither order can produce a
    wrong result. Sweeping first means a single pass leaves this machine and the
    cluster agreeing; materializing first leaves the machine one converge behind
    on the names it just uploaded.
    """
    from cli.commands.start import _prepare_cold_start

    calls: list[str] = []
    _instrument_cold_start_seams(monkeypatch, calls)

    rc = _prepare_cold_start(Path("/repo"), frozenset({"gateway"}), (), runtime=None)

    assert rc == 0
    assert calls.index("adopt") < calls.index("materialize")


def test_standalone_converge_adopts_too() -> None:
    """`ava converge` is what an operator runs to make a machine correct without
    restarting it, and a machine holding un-adopted installs is not correct."""
    from cli.commands._converge import cmd_converge

    assert "adopt_local_extensions()" in inspect.getsource(cmd_converge)
